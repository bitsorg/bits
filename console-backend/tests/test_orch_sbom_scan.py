# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""orch.sbom_scan: the builds' SBOMs scanned by the sbom-scanner service."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from console_backend.orch import db, events, sbom_scan, vuln_triage


class Store:
    def __init__(self, objects):
        self.objects = objects

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)

        class B:
            def __init__(s, d):
                s.d = d

            def read(s):
                return s.d
        return {"Body": B(self.objects[Key])}


class Scanner:
    """The service: its database date, and a canned answer per SBOM."""

    def __init__(self, built="2026-10-23T06:30:15Z", summary=None, fail=None, updating=False,
                 vulns=None):
        self.built, self.summary, self.fail, self.updating = built, summary or {}, fail, updating
        self.vulns = vulns if vulns is not None else [{"id": "CVE-1"}]
        self.scanned = []

    def get(self, url):
        assert url.endswith("/v1/status"), url
        return {"db": {"built": self.built} if self.built else None, "updating": self.updating}

    def post(self, url, body):
        assert url.endswith("/v1/scan"), url
        if self.fail:
            raise self.fail
        self.scanned.append(json.loads(body)["name"])
        return {"db": {"built": self.built}, "summary": self.summary,
                "sbom": {"bomFormat": "CycloneDX", "vulnerabilities": self.vulns}}


class Fixture(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        self.dir = os.path.join(d, "sbom-scans")
        db.migrate(self.conn)
        self.objects = {}
        for i, (bid, community) in enumerate([("b-1", "atlas"), ("b-2", "lhcb"), ("b-3", "atlas")]):
            self.conn.execute("INSERT INTO pipelines (id, is_build, community, package, version,"
                              " first_seen_ms) VALUES (?, 1, ?, 'Athena', '25', 0)", (i + 1, community))
            self.conn.execute("INSERT INTO builds (pipeline_id, build_id, community, recorded_ms)"
                              " VALUES (?, ?, ?, ?)", (i + 1, bid, community, i))
            self.objects["MANIFESTS/%s/sbom.cdx.json" % bid] = json.dumps({"name": bid}).encode()
        self.conn.commit()

    def _step(self, scanner, **kw):
        return sbom_scan.step(self.conn, (Store(self.objects), "bkt"), "http://scanner:8080", self.dir,
                              get=scanner.get, post=scanner.post, **kw)

    def _scans(self):
        return {r["build_id"]: dict(r) for r in self.conn.execute("SELECT * FROM sbom_scans")}

    def _events(self):
        return [e for e in events.since(self.conn, limit=100) if e["kind"] == "sbom.scanned"]


class SbomScanTest(Fixture):
    def test_newest_builds_are_scanned_first_a_few_a_pass(self):
        s = Scanner(summary={"critical": 1, "high": 2, "packages": 2, "vulnerabilities": 3})
        with patch.object(sbom_scan, "PER_PASS", 2):
            self.assertEqual(self._step(s), 2)
            self.assertEqual(s.scanned, ["b-3", "b-2"])
            self.assertEqual(self._step(s), 1)
        scan = self._scans()["b-1"]
        self.assertEqual((scan["critical"], scan["high"], scan["packages"], scan["vulnerabilities"],
                          scan["error"]), (1, 2, 2, 3, ""))
        self.assertEqual(sbom_scan.vulnerabilities(self.dir, "b-1"), [{"id": "CVE-1"}])
        self.assertIsNone(sbom_scan.vulnerabilities(self.dir, "b-9"))
        self.assertEqual(self._step(s), 0)                     # nothing is due any more

    def test_a_newer_database_scans_again_and_a_change_is_an_event(self):
        self._step(Scanner(summary={"high": 1}))
        self.assertEqual(len(self._events()), 3)               # first findings
        self._step(Scanner(built="2026-10-24T06:00:00Z", summary={"high": 1}))
        self.assertEqual(len(self._events()), 3)               # same counts: no event
        s2 = Scanner(built="2026-10-25T06:00:00Z", summary={"high": 1, "critical": 1})
        self.assertEqual(self._step(s2), 3)
        ev = self._events()[-1]
        self.assertEqual((ev["payload"]["critical"], ev["payload"]["was"]), (1, [0, 1, 0, 0, 0]))
        self.assertEqual(self._scans()["b-1"]["db_built"], "2026-10-25T06:00:00Z")

    def test_a_newer_publication_of_a_build_scans_it_again(self):
        self._step(Scanner())
        self.assertEqual(self._step(Scanner()), 0)
        self.conn.execute("UPDATE sbom_scans SET scanned_ms = 1000")
        self.conn.execute("INSERT INTO pipelines (id, is_build, community, first_seen_ms)"
                          " VALUES (9, 1, 'atlas', 0)")
        self.conn.execute("INSERT INTO builds (pipeline_id, build_id, community, recorded_ms)"
                          " VALUES (9, 'b-1', 'atlas', 2000)")    # published after that scan
        self.conn.commit()
        self.objects["MANIFESTS/b-1/sbom.cdx.json"] = b'{"name": "b-1 again"}'
        s = Scanner(summary={"high": 1})
        self.assertEqual(self._step(s), 1)
        self.assertEqual(s.scanned, ["b-1 again"])
        self.assertEqual(self._scans()["b-1"]["high"], 1)
        self.assertEqual(self._step(Scanner()), 0)             # and then not again

    def test_a_first_clean_scan_is_no_event(self):
        self._step(Scanner())
        self.assertEqual(self._events(), [])

    def test_a_failed_scan_keeps_the_last_good_counts(self):
        self._step(Scanner(summary={"high": 2}))
        late = Scanner(built="2026-10-24T06:00:00Z", fail=sbom_scan.Failed("scanner answered 500: boom"))
        self._step(late)
        scan = self._scans()["b-1"]
        self.assertEqual((scan["high"], scan["db_built"]), (2, "2026-10-23T06:30:15Z"))
        self.assertIn("boom", scan["error"])
        self.assertEqual(sbom_scan.vulnerabilities(self.dir, "b-1"), [{"id": "CVE-1"}])
        with patch.object(sbom_scan, "RETRY_MS", -1):
            self._step(Scanner(built="2026-10-24T06:00:00Z", summary={"high": 2}))
        self.assertEqual(self._scans()["b-1"]["error"], "")
        self.assertEqual(len(self._events()), 3)               # the same counts again: no event

    def test_a_missing_sbom_is_kept_and_retried_later(self):
        del self.objects["MANIFESTS/b-3/sbom.cdx.json"]
        self._step(Scanner())
        self.assertIn("no sbom.cdx.json", self._scans()["b-3"]["error"])
        self.objects["MANIFESTS/b-3/sbom.cdx.json"] = b'{"name": "b-3"}'
        self.assertEqual(self._step(Scanner()), 0)             # not before RETRY_MS
        with patch.object(sbom_scan, "RETRY_MS", -1):
            self.assertEqual(self._step(Scanner()), 1)

    def test_a_busy_or_updating_scanner_records_nothing(self):
        self.assertEqual(self._step(Scanner(fail=sbom_scan.Busy("busy"))), 0)
        self.assertEqual(self._step(Scanner(updating=True)), 0)
        self.assertEqual(self._scans(), {})

    def test_no_url_scans_nothing_and_no_database_is_an_error(self):
        self.assertEqual(sbom_scan.step(self.conn, None, "", ""), 0)
        with self.assertRaisesRegex(RuntimeError, "no valid vulnerability database"):
            self._step(Scanner(built=""))
        with self.assertRaises(RuntimeError):
            sbom_scan.step(self.conn, None, "http://scanner:8080", self.dir)

    def test_a_pass_stops_starting_scans_after_its_budget(self):
        ticks = iter([0, 0, sbom_scan.BUDGET_S + 1, sbom_scan.BUDGET_S + 2])
        self.assertEqual(self._step(Scanner(), clock=lambda: next(ticks)), 1)

    def test_an_odd_build_id_is_a_safe_file_name(self):
        p = sbom_scan._path(self.dir, "../x")
        self.assertEqual(os.path.dirname(p), self.dir)
        self.assertNotIn("..", os.path.basename(p))

    def test_listing_is_most_severe_first_and_scoped(self):
        self._step(Scanner())
        self.conn.execute("UPDATE sbom_scans SET high = 3 WHERE build_id = 'b-1'")
        self.conn.execute("UPDATE sbom_scans SET critical = 1 WHERE build_id = 'b-2'")
        self.conn.execute("UPDATE sbom_scans SET index_version = 0")     # the scanner's counts
        self.conn.commit()
        ids = lambda **kw: [r["build_id"] for r in sbom_scan.listing(self.conn, **kw)]
        self.assertEqual(ids(), ["b-2", "b-1", "b-3"])
        self.assertEqual(ids(flagged=True), ["b-2", "b-1"])
        self.assertEqual(ids(communities={"atlas"}), ["b-1", "b-3"])
        self.assertEqual(ids(communities=set()), [])
        row = sbom_scan.listing(self.conn, communities={"lhcb"})[0]
        self.assertEqual((row["pipeline_id"], row["package"], row["critical"]), (2, "Athena", 1))



def component(ref, name, version, purl="", cpe="", **props):
    c = {"bom-ref": ref, "name": name, "version": version,
         "properties": [{"name": k.replace("_", ":", 1), "value": v} for k, v in props.items()]}
    if purl:
        c["purl"] = purl
    if cpe:
        c["cpe"] = cpe
    return c


SBOM = {"bomFormat": "CycloneDX", "components": [
    component("libxml2@1", "libxml2", "2.9.13", cpe="cpe:2.3:a:xmlsoft:libxml2:2.9.13:*:*:*:*:*:*:*"),
    component("ROOT@1", "ROOT", "6.32.0", purl="pkg:github/root-project/root@v6-32-00",
              bits_pinned_by="defaults:lhcb", bits_build_requires="numpy"),
    component("zlib@system", "zlib1g", "1:1.3-1", purl="pkg:deb/ubuntu/zlib1g@1:1.3-1?arch=amd64",
              bits_provided_by="system", bits_package="zlib"),
    component("numpy@1", "numpy", "2.0.1", purl="pkg:pypi/numpy@2.0.1"),
    component("libxml2x@1", "libxml2", "2.90.0")],
    "metadata": {"component": {"bom-ref": "release", "name": "b"}},
    "dependencies": [{"ref": "release", "dependsOn": ["ROOT@1", "numpy@1"]},
                     {"ref": "ROOT@1", "dependsOn": ["libxml2@1"]},
                     {"ref": "numpy@1", "dependsOn": ["zlib@system"]}]}
VULNS = [{"id": "CVE-A", "ratings": [{"severity": "high"}],
          "properties": [{"name": "grype:risk", "value": "42.5"}, {"name": "grype:epss", "value": "0.5"},
                         {"name": "grype:kev", "value": "2024-01-02"}],
          "affects": [{"ref": "libxml2@1", "versions": [{"version": "2.10.0", "status": "unaffected"},
                                                         {"version": "2.9.14", "status": "unaffected"}]},
                      {"ref": "zlib@system"}]},
         {"id": "CVE-B", "ratings": [{"severity": "info"}], "affects": [{"ref": "libxml2@1"}]},
         {"id": "CVE-C", "ratings": [{"severity": "critical"}], "affects": [{"ref": "gone"}]}]


class SbomIndexTest(Fixture):
    def setUp(self):
        super().setUp()
        for bid in ("b-1", "b-2", "b-3"):
            self.objects["MANIFESTS/%s/sbom.cdx.json" % bid] = json.dumps(dict(SBOM, name=bid)).encode()
        self.conn.execute("UPDATE pipelines SET version = '1' || id")
        self.conn.commit()
        self.assertEqual(self._step(Scanner(summary={"high": 2, "other": 1}, vulns=VULNS)), 3)

    def test_a_scan_indexes_the_sbom(self):
        scan = self._scans()["b-1"]
        self.assertEqual((scan["components"], scan["identified"], scan["index_version"]),
                         (5, 3, sbom_scan.INDEX_VERSION))   # cpe, deb, pypi; not github, generic
        rows = self.conn.execute("SELECT * FROM sbom_packages WHERE build_id = 'b-1'"
                                 " AND ref = 'zlib@system'").fetchone()
        self.assertEqual((rows["name"], rows["alias"], rows["system"]), ("zlib1g", "zlib", 1))
        findings = self.conn.execute("SELECT vuln_id, ref, severity FROM sbom_findings"
                                     " WHERE build_id = 'b-1' ORDER BY 1, 2").fetchall()
        self.assertEqual([tuple(f) for f in findings], [("CVE-A", "libxml2@1", "high"),
                         ("CVE-A", "zlib@system", "high"), ("CVE-B", "libxml2@1", "other")])

    def test_a_scan_from_before_the_index_is_due_again(self):
        self.assertEqual(sbom_scan.due(self.conn, "2026-10-23T06:30:15Z", db.now_ms()), [])
        self.conn.execute("UPDATE sbom_scans SET index_version = 0 WHERE build_id = 'b-2'")
        self.conn.commit()
        self.assertEqual(sbom_scan.due(self.conn, "2026-10-23T06:30:15Z", db.now_ms()), ["b-2"])
        row = {r["build_id"]: r for r in sbom_scan.listing(self.conn)}["b-2"]
        self.assertIsNone(row["effective"])                    # not indexed yet: the raw counts

    def test_effective_counts_leave_out_what_the_triage_covers(self):
        row = {r["build_id"]: r for r in sbom_scan.listing(self.conn)}["b-1"]
        self.assertEqual((row["effective"]["high"], row["effective"]["other"], row["suppressed"]),
                         (2, 1, 0))
        vuln_triage.add(self.conn, "atlas", "CVE-A", "false_positive", "not our zlib", "ann",
                        package="zlib")                        # its bits name
        rows = {r["build_id"]: r for r in sbom_scan.listing(self.conn)}
        self.assertEqual((rows["b-1"]["effective"]["high"], rows["b-1"]["suppressed"]), (1, 1))
        self.assertEqual(rows["b-2"]["suppressed"], 0)          # lhcb's: not atlas's rule
        self.assertEqual(rows["b-1"]["high"], 2)                # the scan's own counts stay
        for vuln in ("cve-a", "CVE-B"):                         # any case
            vuln_triage.add(self.conn, "atlas", vuln, "will_not_fix", "accepted", "ann")
        ids = lambda **kw: [r["build_id"] for r in sbom_scan.listing(self.conn, **kw)]
        self.conn.execute("UPDATE sbom_scans SET scanned_ms = scanned_ms + 1000 WHERE build_id = 'b-3'")
        self.conn.commit()
        self.assertEqual(ids(), ["b-2", "b-3", "b-1"])          # b-1 and b-3: nothing open
        self.assertEqual(ids(flagged=True), ["b-2"])
        self.conn.execute("UPDATE sbom_scans SET identified = 0 WHERE build_id = 'b-3'")
        self.conn.commit()
        self.assertEqual(ids(flagged=True), ["b-2", "b-3"])     # not scannable: flagged too

    def test_which_builds_contain_a_package(self):
        found = lambda *a, **kw: sorted((r["build_id"], r["component_version"])
                                        for r in sbom_scan.packages(self.conn, *a, **kw))
        self.assertEqual(found("LIBXML2"), [(b, v) for b in ("b-1", "b-2", "b-3")
                                            for v in ("2.9.13", "2.90.0")])
        self.assertEqual(found("libxml2", "2.9"), [("b-1", "2.9.13"), ("b-2", "2.9.13"),
                                                    ("b-3", "2.9.13")])
        self.assertEqual(found("libxml2", "2.9.x", communities={"lhcb"}), [("b-2", "2.9.13")])
        self.assertEqual(found("libxml2", "2.9.1"), [])
        self.assertEqual(found("zlib", communities={"lhcb"}), [("b-2", "1:1.3-1")])
        self.assertEqual(found("libxml2", communities=set()), [])
        (row,) = [r for r in sbom_scan.packages(self.conn, "libxml2", "2.9.13", communities={"lhcb"})]
        self.assertEqual((row["version"], row["findings"]), ("12", [
            {"id": "CVE-A", "severity": "high", "suppressed": None},
            {"id": "CVE-B", "severity": "other", "suppressed": None}]))
        self.assertEqual(found("libxml2", "_.9%"), [])          # no wildcards of its own
        self.assertIn("zlib", sbom_scan.names(self.conn, {"atlas"}))
        self.assertEqual(sbom_scan.names(self.conn, set()), [])

    def test_which_builds_a_vulnerability_was_found_in(self):
        rid = vuln_triage.add(self.conn, "atlas", "CVE-A", "will_not_fix", "accepted", "ann",
                              package="libxml2", version="2.9.13")
        rows = sbom_scan.affected(self.conn, "CVE-A")
        self.assertEqual(sorted((r["build_id"], r["name"], r["suppressed"]) for r in rows), [
            ("b-1", "libxml2", rid), ("b-1", "zlib1g", None), ("b-2", "libxml2", None),
            ("b-2", "zlib1g", None), ("b-3", "libxml2", rid), ("b-3", "zlib1g", None)])
        self.assertEqual(sbom_scan.affected(self.conn, "CVE-C"), [])     # matched no component
        self.assertEqual(len(sbom_scan.affected(self.conn, "CVE-A", communities={"lhcb"})), 2)

    def test_a_rescan_of_the_same_sbom_keeps_its_package_rows(self):
        rowid = lambda: self.conn.execute("SELECT MIN(rowid) FROM sbom_packages").fetchone()[0]
        before, seen = rowid(), len(self._events())
        self._step(Scanner(built="2026-10-24T06:00:00Z", summary={"high": 2, "other": 1},
                           vulns=VULNS[:1]))
        self.assertEqual(rowid(), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sbom_findings WHERE"
                                           " build_id = 'b-1'").fetchone()[0], 2)
        self.assertEqual(len(self._events()), seen)            # same counts: no event

    def test_an_odd_sbom_is_indexed_as_far_as_it_goes(self):
        doc = dict(SBOM, name="b-3", components=[{"bom-ref": "x", "name": ["x"], "properties": 5}],
                   dependencies=[{"ref": "x", "dependsOn": "y"}])
        odd = [{"id": "CVE-A", "affects": [{"ref": ["x"]}, "y", {"ref": "x", "versions": 3}]}, "z"]
        self.assertEqual(sbom_scan.index(doc, odd)[:2], (1, 0))
        self.assertEqual(sbom_scan.index(doc, odd)[3], [("CVE-A", "x", "other", 0.0, 0.0, "", "[]")])

    def test_an_indexing_error_is_a_failed_scan_and_blocks_no_other(self):
        real = sbom_scan.index
        def index(doc, vulns):
            if doc.get("name") == "b-3":
                raise TypeError("odd")
            return real(doc, vulns)
        self.conn.execute("UPDATE sbom_scans SET index_version = 0")
        self.conn.commit()
        with patch.object(sbom_scan, "index", index):
            self.assertEqual(self._step(Scanner(vulns=VULNS)), 2)
        scans = self._scans()
        self.assertIn("could not index", scans["b-3"]["error"])
        self.assertEqual(scans["b-1"]["index_version"], sbom_scan.INDEX_VERSION)

    def test_threat_fixes_pins_and_dependencies_are_indexed(self):
        f = self.conn.execute("SELECT * FROM sbom_findings WHERE build_id = 'b-1' AND vuln_id = 'CVE-A'"
                              " AND ref = 'libxml2@1'").fetchone()
        self.assertEqual((f["risk"], f["epss"], f["kev"], json.loads(f["fixed_in"])),
                         (42.5, 0.5, "2024-01-02", ["2.10.0", "2.9.14"]))
        p = self.conn.execute("SELECT * FROM sbom_packages WHERE build_id = 'b-1' AND ref = 'ROOT@1'").fetchone()
        self.assertEqual((p["pinned_by"], json.loads(p["depends_on"])), ("defaults:lhcb", ["libxml2@1", "numpy@1"]))

    def test_what_the_stack_does_not_ship_is_left_out(self):
        doc = json.loads(json.dumps(SBOM))
        doc["components"] += [
            component("perf@1", "perf", "6.1", cpe="cpe:2.3:a:linux:perf:6.1:*:*:*:*:*:*:*", bits_sbom="tool"),
            component("elfutils@system", "elfutils", "0.190", bits_provided_by="system"),
            component("ssl@system", "openssl", "3.0", bits_provided_by="system")]
        doc["components"][1]["properties"].append({"name": "bits:build_requires", "value": "openssl"})
        doc["dependencies"][0]["dependsOn"].append("perf@1")             # asked for, still a tool
        doc["dependencies"].append({"ref": "perf@1", "dependsOn": ["elfutils@system"]})
        vulns = VULNS + [{"id": "CVE-P", "ratings": [{"severity": "critical"}],
                          "affects": [{"ref": r} for r in ("perf@1", "elfutils@system", "ssl@system")]}]
        self.objects["MANIFESTS/b-3/sbom.cdx.json"] = json.dumps(dict(doc, name="b-3")).encode()
        self.conn.execute("UPDATE sbom_scans SET index_version = 0 WHERE build_id = 'b-3'")
        self.conn.commit()
        self._step(Scanner(summary={"critical": 1, "high": 2, "other": 1}, vulns=vulns))
        why = {r["ref"]: r["not_shipped"] for r in self.conn.execute(
            "SELECT ref, not_shipped FROM sbom_packages WHERE build_id = 'b-3'")}
        self.assertEqual({r: w for r, w in why.items() if w},
                         {"perf@1": "tool", "elfutils@system": "build-only", "ssl@system": "build-only"})
        row = {r["build_id"]: r for r in sbom_scan.listing(self.conn)}["b-3"]
        self.assertEqual((row["effective"]["critical"], row["effective"]["high"], row["not_shipped"]),
                         (0, 2, 3))                               # zlib, used at run time, stays
        (perf,) = sbom_scan.packages(self.conn, "perf")
        self.assertEqual((perf["not_shipped"], perf["findings"][0]["id"]), ("tool", "CVE-P"))
        out = vuln_triage.annotate(vulns, {}, vuln_triage.Rules([]), "atlas", why)
        self.assertEqual([(v["bom-ref"] if "bom-ref" in v else v["id"], [a["ref"] for a in v["affects"]],
                           v.get("analysis", {}).get("state")) for v in out if v["id"] == "CVE-P"],
                         [("CVE-P-build-only", ["elfutils@system", "ssl@system"], "not_affected"),
                          ("CVE-P-tool", ["perf@1"], "not_affected")])

    def test_without_a_release_root_only_tools_are_left_out(self):
        doc = dict(SBOM, metadata={}, components=SBOM["components"] + [
            component("perf@1", "perf", "6.1", bits_sbom="tool")])
        rows = {p[0]: p[-1] for p in sbom_scan.index(doc, [])[2]}
        self.assertEqual({r: w for r, w in rows.items() if w}, {"perf@1": "tool"})

    def test_a_sbom_that_is_not_json_is_a_failed_scan(self):
        self.objects["MANIFESTS/b-1/sbom.cdx.json"] = b"[1"
        self.conn.execute("UPDATE sbom_scans SET index_version = 0")
        self.conn.commit()
        self._step(Scanner(vulns=VULNS))
        self.assertIn("not JSON", self._scans()["b-1"]["error"])


if __name__ == "__main__":
    unittest.main()
