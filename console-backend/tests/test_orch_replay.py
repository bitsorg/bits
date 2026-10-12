# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""M8a: the worker's own certification of a build, replayed beside the CI's."""

import json
from unittest.mock import patch

from bits_helpers import trust
from console_backend import config
from console_backend.orch import db, shadow

from test_orch_shadow import SETTINGS, _Base, _bom


class NoSuchKey(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}


class FakeS3:
    """The store's copies of the builds' BOMs (MANIFESTS/<build_id>/...)."""

    def __init__(self):
        self.docs = {}

    def list_objects_v2(self, Bucket, Prefix, **kw):
        return {"Contents": [{"Key": k} for k in sorted(self.docs) if k.startswith(Prefix)]}

    def get_object(self, Bucket, Key):
        import io
        if Key not in self.docs:
            raise NoSuchKey()
        return {"Body": io.BytesIO(json.dumps(self.docs[Key]).encode())}


class FakeConsole:
    def __init__(self):
        self.files = {}

    def file_raw(self, path, ref, max_bytes=0):
        return self.files.get((path, ref))


class ReplayTest(_Base):
    def setUp(self):
        super().setUp()
        self.settings = config.Settings(env=dict(SETTINGS, BITS_ADMINS_POLICY="lcg @alice\n* @carol"))
        self.s3, self.console = FakeS3(), FakeConsole()
        self.bom = _bom("b2", "el9", ("q", "h2"))            # source_pipeline_id 7
        self.store = {("el9", "h2"): "sh2"}
        self.s3.docs["MANIFESTS/b2/el9.json"] = dict(self.bom)
        self.gl.trees["aaaa000"] = {}
        self.gl.trees["aaaa111"] = {"manifests/lcg/b2.el9.host-1.json":
                                    dict(self.bom, certified_by=["bot"])}
        now = db.now_ms()
        self.conn.execute("INSERT INTO pipelines (id, is_build, community, sha, variables,"
                          " first_seen_ms) VALUES (7, 1, 'lcg', 'c0ffee', ?, ?)",
                          (json.dumps({"COMMUNITY": "LCG"}), now))
        self.conn.execute("INSERT INTO builds (pipeline_id, build_id, bom_keys, recorded_ms)"
                          " VALUES (7, 'b2', '[\"MANIFESTS/b2/el9.json\"]', ?)", (now,))
        self.approve("alice")

    def approve(self, user, subject="7", created=None, expires=None, revoked=None):
        now = db.now_ms()
        self.conn.execute("INSERT INTO approvals (kind, subject, groups, approver, created_ms,"
                          " expires_ms, revoked_ms) VALUES ('preapproval', ?, '[\"lcg\"]', ?, ?, ?, ?)",
                          (subject, user, created or now - 1000, expires or now + 3600_000, revoked))

    def replay(self, archs=("el9",), started=None, probes=None):
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", list(archs)))
        self.gl.jobs[5][0]["started_at"] = started          # None: now
        shadow.discover(self.conn, self.gl, self.settings)
        while shadow.compare_one(self.conn, self.gl, (self.s3, "bucket"), self.settings,
                                 self.cache, trusted=self.trusted,
                                 probe_factory=lambda c: probes or self.probe,
                                 console=self.console):
            pass
        return self.run_rows(5, "replay")

    def test_the_worker_certifies_what_the_ci_did(self):
        rows = self.replay()
        self.assertEqual({a: r["state"] for a, r in rows.items()}, {"el9": "match"})
        self.assertEqual(self.run_rows(5)["el9"]["state"], "match")
        self.assertIn("replay.match", self.kinds())

    def test_no_preapproval_no_certification(self):
        self.conn.execute("DELETE FROM approvals")
        self.approve("alice", expires=db.now_ms() - 1)         # expired before the CI signed
        self.approve("alice", revoked=db.now_ms() - 1)         # revoked
        self.approve("alice", subject="8")                     # another build's
        row = self.replay()["*"]
        self.assertEqual(row["state"], "refused")
        self.assertIn("no console pre-approval", json.loads(row["diff"])["error"])
        self.assertIn("replay.refused", self.kinds())

    def test_the_preapprover_must_administer_every_group(self):
        self.conn.execute("DELETE FROM approvals")
        self.approve("bob")
        self.assertIn("bob (who pre-approved pipeline 7) is not an admin for lcg",
                      json.loads(self.replay()["*"]["diff"])["error"])

    def test_the_boms_must_be_under_the_builds_group(self):
        self.console.files[("communities/LCG/ui-config.yaml", "c0ffee")] = b"certify_group: stacks\n"
        row = self.replay()["*"]
        self.assertEqual(row["state"], "refused")
        self.assertIn("certifies into stacks", json.loads(row["diff"])["error"])

    def test_without_the_store_copy_there_is_nothing_to_certify(self):
        self.s3.docs.clear()
        row = self.replay()["*"]
        self.assertEqual(row["state"], "missing")
        self.assertIn("no store copy", json.loads(row["diff"])["error"])

    def test_cli_builds_are_not_the_workers_in_m8a(self):
        bom = {k: v for k, v in self.bom.items() if k != "source_pipeline_id"}
        self.gl.trees["aaaa111"] = {"manifests/lcg/b2.el9.host-1.json": bom}
        self.assertIn("CLI build", json.loads(self.replay()["*"]["diff"])["error"])

    def test_the_store_copy_is_what_the_worker_certifies(self):
        self.s3.docs["MANIFESTS/b2/el9.json"] = dict(self.bom, extra="store")
        row = self.replay()["el9"]
        # A field the common manifest does not carry: the same result, but noted.
        self.assertEqual(row["state"], "match")
        self.assertEqual(json.loads(row["diff"])["replay"]["store_differs_from_mr"],
                         ["MANIFESTS/b2/el9.json"])
        # Other bytes claimed for the package: the store refutes the worker's input.
        self.s3.docs["MANIFESTS/b2/el9.json"]["packages"][0]["tarball_sha256"] = "sh-other"
        self.conn.execute("DELETE FROM sign_runs")
        self.conn.execute("UPDATE certifications SET state = 'pending'")
        while shadow.compare_one(self.conn, self.gl, (self.s3, "bucket"), self.settings,
                                 self.cache, trusted=self.trusted,
                                 probe_factory=lambda c: self.probe, console=self.console):
            pass
        rows = self.run_rows(5, "replay")
        self.assertEqual({a: r["state"] for a, r in rows.items()}, {"*": "refused"})
        self.assertIn("the worker refuses", json.loads(rows["*"]["diff"])["error"])

    def test_no_key_policy_is_a_refusal(self):
        with patch.object(trust, "load_key_policy", lambda dirs=None: None):
            row = self.replay()["*"]
        self.assertIn("fails closed", json.loads(row["diff"])["error"])

    def test_the_builds_objects_are_hashed_afresh(self):
        cache = shadow.DigestCache(self.conn)
        cache.put("k", "e", 1, "sha256:old")
        fresh = shadow._RememberOnly(cache)
        self.assertIsNone(fresh.get("k", "e", 1))
        fresh.put("k", "e", 1, "sha256:new")
        self.assertEqual(cache.get("k", "e", 1), "sha256:new")

    def test_only_the_pipelines_own_store_copy_counts(self):
        # The same build id, built again by another pipeline: its copy is not this one's.
        self.s3.docs["MANIFESTS/b2/zzhost-2026.el9.json"] = dict(self.bom, source_pipeline_id=9)
        self.assertEqual(self.replay()["el9"]["state"], "match")

    def test_the_pushs_deletions_are_applied(self):
        old = _bom("b1", "el9", ("p", "h1"))
        self.gl.trees["aaaa000"] = {"manifests/lcg/b1.el9.host-0.json": old}
        self.store[("el9", "h1")] = "sh1"
        self.assertEqual({a: r["state"] for a, r in self.replay().items()}, {"el9": "match"})

    def test_the_approval_must_be_in_force_when_the_ci_signed(self):
        started = "2026-10-11T09:30:00Z"
        at = shadow._ms(started)
        self.conn.execute("DELETE FROM approvals")
        self.approve("alice", created=at + 1000)               # granted after the CI started
        self.assertEqual(self.replay(started=started)["*"]["state"], "refused")
        self.conn.execute("DELETE FROM approvals")
        self.conn.execute("DELETE FROM sign_runs")
        self.conn.execute("UPDATE certifications SET state = 'pending'")
        self.approve("alice", created=at - 1000, expires=at + 3600_000,
                     revoked=at + 1000)                         # revoked after it signed
        self.assertEqual(self.replay(started=started)["el9"]["state"], "match")

    def test_an_overall_admin_may_preapprove_any_group(self):
        self.conn.execute("DELETE FROM approvals")
        self.approve("carol")
        self.assertEqual(self.replay()["el9"]["state"], "match")

    def test_the_builds_objects_go_to_the_fresh_probe_once(self):
        calls = []

        def base(arch, h, tarball=None):
            calls.append(("cached", h))
            return self.store.get((arch, h))

        def fresh(arch, h, tarball=None):
            calls.append(("fresh", h))
            return self.store.get((arch, h))
        self.assertEqual(self.replay(probes=(base, fresh))["el9"]["state"], "match")
        self.assertEqual([c for c in calls if c[1] == "h2"], [("fresh", "h2")])
