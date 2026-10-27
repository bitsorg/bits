# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Each release's risk over time (console_backend.orch.sbom_history)."""

import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from console_backend.orch import db, sbom_history, sbom_scan, vuln_triage


def _ms(day):
    d = datetime.datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=datetime.timezone.utc)
    return int(d.timestamp() * 1000)


class HistoryTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        for pid, community, package in ((1, "lcg", "LCG"), (2, "lcg", "LCG"), (3, "alice", "O2")):
            self.conn.execute("INSERT INTO pipelines (id, is_build, community, package, first_seen_ms)"
                              " VALUES (?, 1, ?, ?, 0)", (pid, community, package))
        # (pipeline, build, community, top_level, published, findings: (vuln, severity, risk, shipped))
        for pid, bid, community, top, day, findings in (
                (1, "lcg_opt-aaaaaaaaaaaa", "lcg", [], "2026-09-01",
                 [("CVE-1", "critical", 80.0, True), ("CVE-2", "high", 0.0, True),
                  ("CVE-T", "high", 40.0, True), ("CVE-S", "medium", 9.0, False)]),
                (2, "lcg_dbg-bbbbbbbbbbbb", "lcg", [], "2026-10-01", [("CVE-2", "high", 30.0, True)]),
                (0, "o2physics-cccccccccccc", "alice", ["O2Physics"], "2026-10-05",
                 [("CVE-3", "low", 1.0, True)])):
            self.conn.execute("INSERT INTO builds (pipeline_id, build_id, community, top_level,"
                              " published_at, recorded_ms) VALUES (?,?,?,?,?,0)",
                              (pid, bid, community, json.dumps(top), day + "T10:00:00Z"))
            self.conn.execute("INSERT INTO sbom_scans (build_id, db_built, scanned_ms, index_version)"
                              " VALUES (?, 'd', 1, ?)", (bid, sbom_scan.INDEX_VERSION))
            for vuln, sev, risk, shipped in findings:
                ref = "ref-" + vuln
                self.conn.execute("INSERT OR IGNORE INTO sbom_packages (build_id, ref, name, version,"
                                  " not_shipped) VALUES (?, ?, 'zlib', '1.3', ?)",
                                  (bid, ref, "" if shipped else "tool"))
                self.conn.execute("INSERT INTO sbom_findings (build_id, vuln_id, ref, severity, risk)"
                                  " VALUES (?,?,?,?,?)", (bid, vuln, ref, sev, risk))
        self.conn.commit()
        vuln_triage.add(self.conn, "lcg", "CVE-T", "not_affected", "not reachable", "bob",
                        justification="code_not_reachable")

    def _points(self, out, top):
        (r,) = [r for r in out["releases"] if r["top"] == top]
        return [(p["day"], p["build_id"][:7], p["critical"], p["high"], p["medium"], p["risk"])
                for p in r["points"]]

    def test_the_first_record_has_the_builds_at_their_publish_days(self):
        self.assertEqual(sbom_history.snapshot(self.conn, now=_ms("2026-10-10")), 5)    # 2 + 3 days
        out = sbom_history.history(self.conn, now=_ms("2026-10-10"))
        # Triaged and not shipped findings left out; a finding without grype's risk by severity.
        self.assertEqual(self._points(out, "LCG"), [
            ("2026-09-01", "lcg_opt", 1, 1, 0, 105.0),
            ("2026-10-01", "lcg_dbg", 0, 1, 0, 30.0),
            ("2026-10-10", "lcg_dbg", 0, 1, 0, 30.0)])
        (now,) = [r for r in out["releases"] if r["top"] == "LCG"][0]["points"][-1:]
        self.assertEqual(now["risks"], {"critical": 0.0, "high": 30.0, "medium": 0.0, "low": 0.0,
                                        "other": 0.0})
        (first,) = [r for r in out["releases"] if r["top"] == "LCG"][0]["points"][:1]
        self.assertEqual(first["risks"]["critical"], 80.0)           # recorded, by severity
        # A build of no pipeline: its release is the package it asked for.
        self.assertEqual([(r["community"], r["top"]) for r in out["releases"]],
                         [("alice", "O2Physics"), ("lcg", "LCG")])
        self.assertEqual([m["build_id"][:7] for m in out["builds"]], ["lcg_opt", "lcg_dbg", "o2physi"])

    def test_a_day_is_recorded_once_and_then_stands_for_that_day(self):
        self.assertEqual(sbom_history.snapshot(self.conn, now=_ms("2026-10-02")), 4)
        self.assertEqual(sbom_history.snapshot(self.conn, now=_ms("2026-10-02")), 0)     # once a day
        # A new vulnerability in the newest build after that day: the records keep the old risk.
        self.conn.execute("INSERT INTO sbom_findings (build_id, vuln_id, ref, severity, risk)"
                          " VALUES ('lcg_dbg-bbbbbbbbbbbb', 'CVE-9', 'ref-CVE-2', 'critical', 70)")
        self.conn.commit()
        out = sbom_history.history(self.conn, now=_ms("2026-10-10"))
        self.assertEqual(self._points(out, "LCG"), [
            ("2026-09-01", "lcg_opt", 1, 1, 0, 105.0),
            ("2026-10-01", "lcg_dbg", 0, 1, 0, 30.0),
            ("2026-10-02", "lcg_dbg", 0, 1, 0, 30.0),        # the record
            ("2026-10-10", "lcg_dbg", 1, 1, 0, 100.0)])      # today, afresh
        # The next day: the newest build of each release, and the alice build of 10-05 is
        # not before it.
        self.assertEqual(sbom_history.snapshot(self.conn, now=_ms("2026-10-03")), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sbom_history").fetchone()[0], 6)

    def test_a_build_scanned_after_the_first_record_gets_its_day(self):
        self.conn.execute("UPDATE sbom_scans SET db_built = '' WHERE build_id = 'lcg_opt-aaaaaaaaaaaa'")
        self.conn.commit()
        sbom_history.snapshot(self.conn, now=_ms("2026-10-02"))
        self.conn.execute("UPDATE sbom_scans SET db_built = 'd'")      # scanned now
        self.conn.commit()
        sbom_history.snapshot(self.conn, now=_ms("2026-10-03"))
        days = [p[0] for p in self._points(sbom_history.history(self.conn, now=_ms("2026-10-03")), "LCG")]
        self.assertEqual(days, ["2026-09-01", "2026-10-01", "2026-10-02", "2026-10-03"])

    def test_the_earlier_days_are_recorded_a_few_a_pass(self):
        with patch.object(sbom_history, "BACKFILL", 1):
            # Today's 2 each pass, and 1 of the 3 earlier days, until none is left.
            got = [sbom_history.snapshot(self.conn, now=_ms("2026-10-10")) for _ in range(4)]
        self.assertEqual(got, [3, 3, 3, 0])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sbom_history").fetchone()[0], 5)

    def test_a_build_of_two_pipelines_is_of_the_newest_s_release(self):
        self.conn.execute("INSERT INTO pipelines (id, is_build, community, package, first_seen_ms)"
                          " VALUES (5, 1, 'alice', 'O2', 0)")
        self.conn.execute("INSERT INTO builds (pipeline_id, build_id, community, published_at,"
                          " recorded_ms) VALUES (5, 'lcg_opt-aaaaaaaaaaaa', 'alice', '2026-10-07T00:00:00Z', 0)")
        self.conn.commit()
        got = sbom_history._builds(self.conn)["lcg_opt-aaaaaaaaaaaa"]
        self.assertEqual(got, ("alice", "O2", "2026-09-01"))      # the day it was first published

    def test_communities_and_days_narrow_it(self):
        sbom_history.snapshot(self.conn, now=_ms("2026-10-10"))
        out = sbom_history.history(self.conn, communities={"alice"}, days=30, now=_ms("2026-10-10"))
        self.assertEqual([r["top"] for r in out["releases"]], ["O2Physics"])
        out = sbom_history.history(self.conn, communities={"lcg"}, days=20, now=_ms("2026-10-10"))
        self.assertEqual([p[0] for p in self._points(out, "LCG")], ["2026-10-01", "2026-10-10"])
        self.assertEqual(sbom_history.history(self.conn, communities=set()),
                         {"releases": [], "builds": []})


if __name__ == "__main__":
    unittest.main()
