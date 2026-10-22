# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Which build published a CVMFS path, and who requested and approved it."""

import json
import os
import shutil
import tempfile
import unittest

from console_backend.orch import db, events, trace

P = "http://prepub:8080"


class TraceTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.calls = []

    def get(self, data):
        def get(url):
            self.calls.append(url)
            value = data.get(url)
            if isinstance(value, Exception):
                raise value
            return value
        return get

    def _rec(self, path, job, outcome="published", ts="2026-10-22T22:05:00.123Z", **kw):
        return dict({"ts": ts, "build_id": "1", "job_id": job, "repo": "bits.cern.ch",
                     "path": path, "outcome": outcome}, **kw)

    def _pipeline(self, pid, community="atlas", certifier="alice", variables=None):
        with db.transaction(self.conn):
            v = dict({"BITS_CERTIFIER": certifier} if certifier else {}, **(variables or {}))
            self.conn.execute(
                "INSERT INTO pipelines (id, is_build, community, package, version, variables,"
                " web_url, outcome, first_seen_ms) VALUES (?, 1, ?, 'Athena', '25.0.71', ?,"
                " ?, 'failed', 1)", (pid, community, json.dumps(v), "https://gl/p/%d" % pid))

    def _approve(self, subject, approver):
        with db.transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO approvals (kind, subject, groups, approver, created_ms, expires_ms)"
                " VALUES ('preapproval', ?, '[\"atlas\"]', ?, 1792700000000, 1792800000000)",
                (str(subject), approver))
            events.append(self.conn, "approval.granted", "approval:%d" % cur.lastrowid,
                          events.Actor("person", approver), {"approval": cur.lastrowid})

    def test_index_reads_new_and_recent_builds_only_published(self):
        pkg = "atlas/x86_64-el9-gcc15-dbg/Packages/openloops/2.1.4-1"
        data = {P + "/api/v1/measurements": ["latest", "16131386", "15907127"],
                P + "/api/v1/measurements/16131386": [
                    self._rec("/" + pkg + "/", "j1"), self._rec("x/y", "j2", outcome="failed"),
                    "junk"],
                P + "/api/v1/measurements/15907127": [
                    self._rec("old/pkg", "j0", ts="2026-10-01T06:00:00Z", replaced=True)]}
        self.assertEqual(trace.step(self.conn, [P + "/"], get=self.get(data)), 2)
        rows = self.conn.execute("SELECT * FROM published_paths ORDER BY job_id").fetchall()
        self.assertEqual([(r["path"], r["build_id"], r["replaced"]) for r in rows],
                         [("old/pkg", "15907127", 1), (pkg, "16131386", 0)])
        self.calls.clear()
        trace.step(self.conn, [P], get=self.get(data))       # all read just now: only the list
        self.assertEqual(self.calls, [P + "/api/v1/measurements"])

    def test_a_bad_or_empty_build_is_read_again_and_does_not_block_others(self):
        import requests
        data = {P + "/api/v1/measurements": ["3", "2", "1"],
                P + "/api/v1/measurements/3": requests.HTTPError("500"),
                P + "/api/v1/measurements/2": [],
                P + "/api/v1/measurements/1": [self._rec("a/b", "j", ts="2026-01-01T00:00:00Z")]}
        self.assertEqual(trace.step(self.conn, [P], get=self.get(data)), 3)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM published_paths").fetchone()[0], 1)
        with db.transaction(self.conn):              # as if read a while ago
            self.conn.execute("UPDATE traced_builds SET checked_ms = 0")
        self.calls.clear()
        trace.step(self.conn, [P], get=self.get(data))
        # 3 and 2 are recent (no records yet: from when first seen); 1 is old.
        self.assertEqual(sorted(self.calls), [P + "/api/v1/measurements",
                                              P + "/api/v1/measurements/2",
                                              P + "/api/v1/measurements/3"])

    def test_an_unreachable_prepub_does_not_stop_the_others(self):
        data = {"http://down/api/v1/measurements": OSError("refused"),
                P + "/api/v1/measurements": ["7"],
                P + "/api/v1/measurements/7": [self._rec("a/b", "j")]}
        self.assertEqual(trace.step(self.conn, ["http://down", P], get=self.get(data)), 1)

    def test_lookup_finds_the_package_above_the_file_and_who_approved(self):
        data = {P + "/api/v1/measurements": ["16131386"],
                P + "/api/v1/measurements/16131386": [self._rec("atlas/Packages/ROOT/6-1", "j1")]}
        trace.step(self.conn, [P], get=self.get(data))
        self._pipeline(16131386)
        self._approve(16131386, "bob")
        self._pipeline(15000000, community="lhcb", certifier="carol")
        with db.transaction(self.conn):
            self.conn.execute("INSERT INTO builds (pipeline_id, build_id, recorded_ms)"
                              " VALUES (15000000, 'release_gcc15_opt-c193e4dbe60c', 1)")
        out = trace.lookup(self.conn, "bits.cern.ch", "/atlas/Packages/ROOT/6-1/lib/libCore.so",
                           build_id="release_gcc15_opt-c193e4dbe60c", visible={"atlas"})
        self.assertEqual(out["published_path"], "atlas/Packages/ROOT/6-1")
        (pub,) = out["publishes"]
        self.assertEqual((pub["pipeline"], pub["requested_by"], pub["approval"]["approver"]),
                         (16131386, "alice", "bob"))
        self.assertTrue(pub["approval"]["event"]["hash"])
        self.assertEqual(pub["published_at"], "2026-10-22T22:05:00Z")
        (built,) = out["built_by_pipelines"]
        self.assertEqual(built, {"pipeline": 15000000, "known": True, "visible": False})
        self.assertEqual(trace.lookup(self.conn, "bits.cern.ch", "atlas/other")["publishes"], [])

    def test_the_approval_in_force_and_what_others_see(self):
        self._pipeline(5)
        self._approve(5, "first")
        with db.transaction(self.conn):
            self.conn.execute("UPDATE approvals SET revoked_ms = 1792700100000")
        self._approve(5, "second")
        self.assertEqual(trace.pipeline(self.conn, 5)["approval"]["approver"], "second")
        with db.transaction(self.conn):
            self.conn.execute("INSERT INTO published_paths (repo, path, build_id, job_id, ts_ms,"
                              " prepub) VALUES ('r', 'p', '5', 'j', 1, 'http://prepub')")
        (pub,) = trace.lookup(self.conn, "r", "p/x", visible={"lhcb"})["publishes"]
        self.assertEqual(pub, {"pipeline": 5, "known": True, "visible": False})

    def test_a_queued_build_names_its_requester(self):
        self._pipeline(9, certifier="")
        with db.transaction(self.conn):
            self.conn.execute(
                "INSERT INTO build_queue (batch, community, requested_by, ref, variables, digest,"
                " target, state, pipeline_id, created_ms, updated_ms) VALUES ('b1', 'atlas',"
                " 'dave', 'main', '{}', 'd', 'x', 'dispatched', 9, 1, 1)")
        self.assertEqual(trace.pipeline(self.conn, 9)["requested_by"], "dave")
        self._pipeline(10, certifier="mallory")                    # a variable anyone can set
        with db.transaction(self.conn):
            self.conn.execute("UPDATE build_queue SET pipeline_id = 10")
        p = trace.pipeline(self.conn, 10)
        self.assertEqual((p["requested_by"], p["requested_by_from"]), ("dave", "queue"))
        self.assertIsNone(trace.pipeline(self.conn, "release_x"))


if __name__ == "__main__":
    unittest.main()
