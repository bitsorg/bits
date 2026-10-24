# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""orch.sbom_plan: upgrade plans from the scanned builds."""

import os
import shutil
import tempfile
import unittest

from console_backend.orch import db, sbom_plan, sbom_scan, vuln_triage


def comp(name, version, *deps, **props):
    return ({"bom-ref": name + "@1", "name": name, "version": version, "cpe": "cpe:x",
             "properties": [{"name": "bits:" + k, "value": v} for k, v in props.items()]},
            {"ref": name + "@1", "dependsOn": [d + "@1" for d in deps]})


def vuln(vid, severity, fixes_by_ref, risk=0, kev=""):
    props = [{"name": "grype:risk", "value": str(risk)}] if risk else []
    if kev:
        props.append({"name": "grype:kev", "value": kev})
    return {"id": vid, "ratings": [{"severity": severity}], "properties": props,
            "affects": [{"ref": r + "@1", "versions": [{"version": f, "status": "unaffected"} for f in fx]}
                        for r, fx in fixes_by_ref.items()]}


class PlanTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.dir = os.path.join(d, "scans")
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.n = 0

    def build(self, bid, community, components, vulns, platform="x86_64-el9"):
        self.n += 1
        self.conn.execute("INSERT INTO pipelines (id, is_build, community, package, version, platform,"
                          " first_seen_ms) VALUES (?, 1, ?, 'LCG', '1', ?, 0)", (self.n, community, platform))
        self.conn.execute("INSERT INTO builds (pipeline_id, build_id, community, recorded_ms, published_at)"
                          " VALUES (?, ?, ?, ?, ?)", (self.n, bid, community, self.n, "2026-10-%02d" % self.n))
        self.conn.commit()
        doc = {"components": [c for c, _ in components], "metadata": {"component": {"bom-ref": "release"}},
               "dependencies": [{"ref": "release", "dependsOn": ["app@1"]}] + [d for _, d in components]}
        sbom_scan._scanned(self.conn, self.dir, bid, "sha%d" % self.n, "db", {}, vulns, doc)

    def lcg(self, bid="release_gcc15_opt-aaaaaaaaaaaa", curl="8.1.0", community="lcg", pin=None, **kw):
        comps = [comp("app", "1", "curl", "go", "zlib"), comp("curl", curl, "zlib", **({"pinned_by": pin} if pin else {})),
                 comp("go", "1.12.5"), comp("zlib", "1:1.2-3", provided_by="system", package="zlib"),
                 comp("left", "1.0")]
        vulns = kw.get("vulns", [
            vuln("CVE-K", "high", {"go": ["1.20.10", "1.21.3"]}, risk=78.75, kev="2023-10-10"),
            vuln("CVE-C1", "critical", {"curl": ["8.4.0"]}, risk=9),
            vuln("CVE-C2", "medium", {"curl": ["8.6.0", "9.0.0rc1"]}, risk=2),
            vuln("CVE-M", "high", {"go": ["2.0.0"]}, risk=5),
            vuln("CVE-Z", "low", {"zlib": ["1:1.2-4"]}, risk=1),
            vuln("CVE-N", "medium", {"left": []}, risk=3)])
        self.build(bid, community, comps, vulns)

    def test_a_release_is_planned_in_waves(self):
        self.lcg()
        (pl,) = sbom_plan.plan(self.conn)["releases"]
        self.assertEqual((pl["line"], pl["risk"], pl["findings"]), ("release_gcc15_opt", 98.75, 6))
        w = {x["wave"]: x for x in pl["waves"]}
        (go,) = w[0]["packages"]                        # known exploited; its major upgrade comes along
        self.assertEqual((go["name"], go["target"], go["major"], go["kev"]), ("go", "2.0.0", True, ["CVE-K"]))
        (curl,) = w[1]["packages"]                      # the newest fix it needs, no release candidate
        self.assertEqual((curl["target"], curl["fixes"], curl["rebuild"]), ("8.6.0", ["CVE-C1", "CVE-C2"], 2))
        self.assertEqual((w[0]["rebuild"], w[0]["risk_left"], w[1]["risk_left"]), (2, 15.0, 4.0))
        self.assertEqual([(h["name"], h["target"]) for h in pl["host"]], [("zlib", "1:1.2-4")])
        self.assertEqual([(u["name"], u["unfixed"]) for u in pl["unfixed"]], [("left", ["CVE-N"])])

    def test_triaged_findings_are_left_out(self):
        self.lcg()
        vuln_triage.add(self.conn, "lcg", "CVE-K", "false_positive", "not that go", "ann", package="go")
        vuln_triage.add(self.conn, "lcg", "CVE-M", "false_positive", "not that go", "ann", package="go")
        (pl,) = sbom_plan.plan(self.conn)["releases"]
        self.assertEqual([x["wave"] for x in pl["waves"]], [1])
        self.assertEqual(pl["triaged"], 2)

    def test_the_newest_build_of_a_line_counts_and_lines_are_per_platform(self):
        self.lcg()
        self.lcg(bid="release_gcc15_opt-bbbbbbbbbbbb", curl="8.6.0")
        self.build("release_gcc15_opt-cccccccccccc", "lcg", [comp("app", "1")], [], platform="aarch64-el9")
        lines = sbom_plan.release_lines(self.conn)
        self.assertEqual([(r["build_id"][-4:], r["platform"]) for r in lines],
                         [("cccc", "aarch64-el9"), ("bbbb", "x86_64-el9")])

    def test_one_version_for_all_communities_unless_pinned(self):
        self.lcg()
        self.lcg(bid="release_lhcb_opt-bbbbbbbbbbbb", community="lhcb", curl="8.4.0", pin="defaults:lhcb")
        self.lcg(bid="release_atlas_opt-cccccccccccc", community="atlas", curl="8.8.0", vulns=[])
        c = {x["name"]: x for x in sbom_plan.plan(self.conn)["converged"]}["curl"]
        # 8.6.0 is enough, but ATLAS already builds 8.8.0 and it scanned clean: reuse it
        self.assertEqual((c["version"], c["in_use_clean"]), ("8.8.0", ["release_atlas_opt-cccccccccccc"]))
        acts = {r["community"]: (r["version"], r["action"]) for r in c["releases"]}
        self.assertEqual(acts, {"lcg": ("8.1.0", "upgrade"), "lhcb": ("8.4.0", "pinned"),
                                "atlas": ("8.8.0", "keep")})
        self.assertEqual(c["pinned"], 1)
        self.assertNotIn("zlib", {x["name"] for x in sbom_plan.plan(self.conn)["converged"]})   # the host's

    def test_a_version_in_use_that_is_vulnerable_is_not_the_answer(self):
        self.lcg()                                          # curl 8.1.0 needs 8.6.0
        self.lcg(bid="release_lhcb_opt-bbbbbbbbbbbb", community="lhcb", curl="8.6.0",
                 vulns=[vuln("CVE-X", "high", {"curl": ["8.7.1"]}, risk=7)])
        c = {x["name"]: x for x in sbom_plan.plan(self.conn, {"lcg", "lhcb"})["converged"]}["curl"]
        self.assertEqual((c["version"], c["in_use_clean"]), ("8.7.1", []))
        self.assertEqual(sbom_plan.plan(self.conn, set()), {"releases": [], "converged": []})

    def test_a_version_in_use_with_unfixed_findings_is_not_reused(self):
        self.lcg()                                          # curl 8.1.0 needs 8.6.0
        self.lcg(bid="release_lhcb_opt-bbbbbbbbbbbb", community="lhcb", curl="9.0.0",
                 vulns=[vuln("CVE-U", "critical", {"curl": []}, risk=50)])
        c = {x["name"]: x for x in sbom_plan.plan(self.conn)["converged"]}["curl"]
        self.assertEqual(c["version"], "8.6.0")

    def test_versions_compare(self):
        k = sbom_plan._vkey
        self.assertTrue(k("1.10.0") > k("1.9.9") > k("v1.9"))
        self.assertTrue(k("1:1.0") > k("2.0"))          # an epoch first
        self.assertTrue(k("8.0p1") > k("8.0") > k("8.0rc1"))
        self.assertEqual(sbom_plan._need(["9.0.0rc1", "8.6.0", "8.0"], "8.1"), "8.6.0")
        self.assertIsNone(sbom_plan._need(["2.0b1"], "1.0"))
        self.assertEqual(sbom_plan._need(["1.1.1c"], "1.1.1"), "1.1.1c")          # OpenSSL letters
        self.assertEqual(sbom_plan._need(["0.0~git20240101.4a5b6c-1"], "0.0~git2023"),
                         "0.0~git20240101.4a5b6c-1")                               # not a pre-release
        self.assertEqual(sbom_plan._need(["2.0.devel1", "1.5.arch"], "1.0"), "1.5.arch")
        self.assertEqual(sbom_plan._major("1:3.0.7"), sbom_plan._major("3.0.8"))


if __name__ == "__main__":
    unittest.main()
