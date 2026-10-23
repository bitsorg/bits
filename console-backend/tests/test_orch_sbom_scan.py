# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""orch.sbom_scan: the builds' SBOMs scanned by the sbom-scanner service."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from console_backend.orch import db, events, sbom_scan


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

    def __init__(self, built="2026-10-23T06:30:15Z", summary=None, fail=None, updating=False):
        self.built, self.summary, self.fail, self.updating = built, summary or {}, fail, updating
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
                "sbom": {"bomFormat": "CycloneDX", "vulnerabilities": [{"id": "CVE-1"}]}}


class SbomScanTest(unittest.TestCase):
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
        self.conn.commit()
        ids = lambda **kw: [r["build_id"] for r in sbom_scan.listing(self.conn, **kw)]
        self.assertEqual(ids(), ["b-2", "b-1", "b-3"])
        self.assertEqual(ids(flagged=True), ["b-2", "b-1"])
        self.assertEqual(ids(communities={"atlas"}), ["b-1", "b-3"])
        self.assertEqual(ids(communities=set()), [])
        row = sbom_scan.listing(self.conn, communities={"lhcb"})[0]
        self.assertEqual((row["pipeline_id"], row["package"], row["critical"]), (2, "Athena", 1))


if __name__ == "__main__":
    unittest.main()
