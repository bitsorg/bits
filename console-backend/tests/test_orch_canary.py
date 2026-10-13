# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""M8a until M8b: the canary — the worker's certifications compared with the CI's."""

import json

from console_backend import config
from console_backend.orch import canary, certifier, shadow

from test_orch_certifier import CertifierBase
from test_orch_shadow import SETTINGS


class CanaryTest(CertifierBase):
    def compared(self):
        return {r["arch"]: r for r in canary.annotate(self.conn, certifier.listing(self.conn, 7))}

    def mine(self):
        return canary.content_digest(json.loads(bytes(self.rows()["el9"]["manifest"])))

    def test_the_canary_matches_the_ci(self):
        """The CI certifies the same build on the commit the set was at: the
        canary's manifest and the CI's are the same, timestamps aside."""
        self.approve()
        self.certify()
        self.gl.trees["aaaa000"] = {}
        self.gl.trees["aaaa111"] = {"manifests/lcg/b2.el9.host-1.json":
                                    dict(self.bom, certified_by=["bot"])}
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el9"]))
        shadow.discover(self.conn, self.gl, self.settings)
        while shadow.compare_one(self.conn, self.gl, (self.s3, "bucket"), self.settings,
                                 self.cache, trusted=self.trusted,
                                 probe_factory=lambda c: self.probe, console=self.console):
            pass
        self.assertEqual(canary.match(self.conn, self.settings), 1)
        row = self.compared()["el9"]
        self.assertEqual((row["compare"], row["ci_certification"]), ("match", 5))
        row = self.rows()["el9"]
        self.assertIsNone(row["manifest"])                             # kept until compared
        self.assertEqual(canary.summary(self.conn), {"match": 1})

    def test_rebased_unmatched_and_missing(self):
        self.approve()
        self.certify()
        self.conn.execute("INSERT INTO certifications (pipeline_id, sha, created_ms, updated_ms)"
                          " VALUES (5, 'aaaa111', 0, 0)")
        self.conn.execute("INSERT INTO ci_outputs (certification, pipeline_id, build_id, arch,"
                          " content, sha, before_sha, created_ms) VALUES (5, 7, 'b2', 'el9', 'x',"
                          " 'aaaa111', 'bbbb000', 0)")
        canary.match(self.conn, self.settings)
        self.assertEqual(self.compared()["el9"]["compare"], "rebased")    # another set version
        self.conn.execute("INSERT INTO ci_outputs (certification, pipeline_id, build_id, arch,"
                          " content, sha, before_sha, created_ms) VALUES (5, 8, 'b3', 'el9', 'y',"
                          " 'aaaa111', 'aaaa000', 0)")
        self.conn.execute("DELETE FROM canary_compare")
        self.conn.execute("UPDATE certify_runs SET created_ms = 0 WHERE arch = 'el9'")
        self.conn.execute("UPDATE ci_outputs SET compare = 'rebased' WHERE build_id = 'b2'")
        canary.match(self.conn, self.settings)
        self.assertEqual(self.compared()["el9"]["compare"], "unmatched")
        self.assertEqual(self.conn.execute("SELECT compare FROM ci_outputs WHERE build_id = 'b3'")
                         .fetchone()[0], "missing")
        self.assertIn("canary.missing", self.kinds())

    def test_the_ci_first_then_the_canary(self):
        self.conn.execute("INSERT INTO certifications (pipeline_id, sha, created_ms, updated_ms)"
                          " VALUES (5, 'aaaa111', 0, 0)")
        self.approve()
        self.certify()
        mine = self.mine()
        self.conn.execute("INSERT INTO ci_outputs (certification, pipeline_id, build_id, arch,"
                          " content, sha, before_sha, created_ms) VALUES (5, 7, 'b2', 'el9', ?,"
                          " 'aaaa111', 'aaaa000', 0)", (mine,))
        self.assertEqual(canary.match(self.conn, self.settings), 1)
        self.assertEqual(self.compared()["el9"]["compare"], "match")

    def test_two_ci_outputs_in_one_pass(self):
        self.conn.execute("INSERT INTO certifications (pipeline_id, sha, created_ms, updated_ms)"
                          " VALUES (5, 'aaaa111', 0, 0), (6, 'aaaa111', 0, 0)")
        self.approve()
        self.certify()
        mine = self.mine()
        for c in (5, 6):
            self.conn.execute("INSERT INTO ci_outputs (certification, pipeline_id, build_id, arch,"
                              " content, sha, before_sha, created_ms) VALUES (?, 7, 'b2', 'el9', ?,"
                              " 'aaaa111', 'aaaa000', 0)", (c, mine))
        self.assertEqual(canary.match(self.conn, self.settings), 1)
        self.assertEqual([r[0] for r in self.conn.execute(
            "SELECT compare FROM ci_outputs ORDER BY certification")], ["match", "again"])
        self.assertEqual(self.compared()["el9"]["ci_certification"], 5)

    def test_the_cis_side_is_kept_only_while_the_worker_certifies(self):
        off = config.Settings(env=dict(SETTINGS))
        self.assertEqual(canary.record_ci(self.conn, off, 5, "s", "b", self.dir, None, {}), 0)

