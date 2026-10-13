# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""M8a: the worker certifies, signs and writes builds itself."""

import json
import os
import tempfile
from unittest.mock import patch

from bits_helpers import trust
from console_backend import config
from console_backend.orch import actions, certified, certifier, db, events

from test_orch_replay import FakeConsole, FakeS3
from test_orch_shadow import SETTINGS, _Base, _bom


class FakeWriter:
    def __init__(self, fail=None):
        self.puts, self.fail = [], fail

    def put_object(self, Bucket, Key, Body, ContentType=None):
        if self.fail:
            e, self.fail = self.fail, None
            raise e
        self.puts.append((Key, Body))


class Deps:
    def __init__(self, test):
        self.t = test
        self.conn, self.settings = test.conn, test.settings
        self.sign = lambda body: trust.sign_bytes(body, trust.load_private_key(test.key))
        self.writer, self.probe, self.key_id = test.writer, test.probe, test.kid

    def reader(self):
        return self.t.console

    def store(self):
        return (self.t.s3, "bucket")


class CertifierBase(_Base):
    def setUp(self):
        super().setUp()
        self.settings = config.Settings(env=dict(
            SETTINGS, BITS_ADMINS_POLICY="lcg @alice\n* @carol", BITS_ORCH_CERTIFY="1",
            BITS_ORCH_MANIFESTS_PROJECT="buncic/bits-manifests"))
        self.s3, self.console, self.writer = FakeS3(), FakeConsole(), FakeWriter()
        self.kid = next(iter(self.trusted))
        p = patch.object(trust, "load_trusted_keys", lambda dirs=None: self.trusted)
        p.start()
        self.addCleanup(p.stop)
        self.bom = _bom("b2", "el9", ("q", "h2"))            # source_pipeline_id 7
        self.store = {("el9", "h2"): "sh2"}
        self.s3.docs["MANIFESTS/b2/host-1.el9.json"] = dict(self.bom)
        # The set, imported from bits-manifests at aaaa000 (version 1).
        empty = tempfile.mkdtemp(dir=self.dir)
        os.makedirs(os.path.join(empty, "manifests"))
        certified.apply_tree(self.conn, os.path.join(empty, "manifests"), 4, "aaaa000")
        now = db.now_ms()
        self.conn.execute("INSERT INTO kv (key, value) VALUES ('certify.since', ?)", (str(now - 1),))
        self.conn.execute(
            "INSERT INTO pipelines (id, is_build, community, sha, variables, outcome,"
            " first_seen_ms) VALUES (7, 1, 'lcg', 'c0ffee', ?, 'success', ?)",
            (json.dumps({"COMMUNITY": "LCG", "CERTIFY": "true"}), now))
        self.conn.execute("INSERT INTO builds (pipeline_id, build_id, bom_keys, recorded_ms)"
                          " VALUES (7, 'b2', '[\"MANIFESTS/b2/host-1.el9.json\"]', ?)", (now,))
        self.deps = Deps(self)

    def approve(self, user="alice", subject="7", **kw):
        now = db.now_ms()
        cur = self.conn.execute(
            "INSERT INTO approvals (kind, subject, groups, approver, created_ms, expires_ms,"
            " revoked_ms) VALUES ('preapproval', ?, '[\"lcg\"]', ?, ?, ?, ?)",
            (subject, user, kw.get("created", now - 1000), kw.get("expires", now + 3600_000),
             kw.get("revoked")))
        return cur.lastrowid

    def certify(self):
        certifier.trigger(self.conn, self.settings, self.console)
        return actions.run_due(self.conn, self.deps, lambda a, c: True, kinds={certifier.KIND})

    def rows(self):
        return {r["arch"]: r for r in self.conn.execute(
            "SELECT * FROM certify_runs WHERE pipeline_id = 7 AND build_id = 'b2'")}


class CertifierTest(CertifierBase):
    def test_a_preapproved_build_is_certified_signed_and_written(self):
        self.approve()
        self.assertEqual(self.certify(), 1)
        rows = self.rows()
        self.assertEqual({a: r["state"] for a, r in rows.items()}, {"*": "signed", "el9": "signed"})
        keys = [k for k, _b in self.writer.puts]
        self.assertEqual(keys, ["CANARY/common-manifest-el9.json",
                                "CANARY/common-manifest-el9.json.sig"])        # .json first
        body, sig = self.writer.puts[0][1], json.loads(self.writer.puts[1][1])
        self.assertEqual(trust.verify_bytes(body, sig, self.trusted), self.kid)
        m = json.loads(body)
        self.assertEqual((m["certified_by"], [p["hash"] for p in m["packages"]]), (["alice"], ["h2"]))
        self.assertIn("expires", m)                       # the CI's 30 days, by default
        self.assertNotIn("source_commit", m)
        a = self.conn.execute("SELECT * FROM actions WHERE kind = 'certify'").fetchone()
        self.assertEqual((a["state"], a["approved_by_id"], a["requested_by_kind"]),
                         ("done", "alice", "worker"))
        self.assertIn("certify.signed", self.kinds())

    def test_nobody_approves_a_certification_in_the_console(self):
        with self.assertRaises(actions.ActionError):
            actions.request(self.conn, certifier.KIND, {"pipeline": "7", "build_id": "b2"},
                            events.Actor("person", "carol"), "lcg", True)
        # No pre-approval: the build waits, and no action exists to approve.
        certifier.trigger(self.conn, self.settings, self.console)
        self.assertEqual(self.rows()["*"]["state"], "waiting")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM actions").fetchone())
        self.approve()
        certifier.trigger(self.conn, self.settings, self.console)
        a = self.conn.execute("SELECT * FROM actions").fetchone()
        self.assertEqual(a["state"], "approved")
        self.conn.execute("UPDATE actions SET state = 'proposed'")      # even if it were
        with self.assertRaises(actions.ActionError):
            actions.approve(self.conn, a["id"], events.Actor("person", "carol"), True)
        certifier.trigger(self.conn, self.settings, self.console)        # no second action
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0], 1)

    def test_a_build_waits_a_day_for_its_preapproval(self):
        self.conn.execute("UPDATE builds SET recorded_ms = recorded_ms - 2 * 86400000")
        self.conn.execute("UPDATE kv SET value = '0' WHERE key = 'certify.since'")
        certifier.trigger(self.conn, self.settings, self.console)
        row = self.rows()["*"]
        self.assertEqual((row["state"], row["reason"]),
                         ("refused", "no console pre-approval of pipeline 7"))

    def test_what_is_not_certified(self):
        self.conn.execute("UPDATE pipelines SET variables = ?",
                          (json.dumps({"COMMUNITY": "LCG"}),))
        certifier.trigger(self.conn, self.settings, self.console)
        self.assertEqual(self.rows()["*"]["state"], "skipped")
        self.conn.execute("DELETE FROM certify_runs")
        self.conn.execute("UPDATE pipelines SET variables = ?",
                          (json.dumps({"COMMUNITY": "LCG", "CERTIFY": "true"}),))
        self.console.files[("communities/LCG/ui-config.yaml", "c0ffee")] = \
            b"certify_manifests_remote: https://gitlab.cern.ch/other/manifests\n"
        self.approve()
        certifier.trigger(self.conn, self.settings, self.console)
        self.assertIn("other/manifests", self.rows()["*"]["reason"])
        off = config.Settings(env=dict(SETTINGS))
        self.assertEqual(certifier.trigger(self.conn, off, self.console), 0)

    def test_the_canary_prefix_stays_out_of_the_production_manifests(self):
        bad = config.Settings(env=dict(SETTINGS, BITS_ORCH_CERTIFY="1",
                                       BITS_ORCH_CANARY_PREFIX="MANIFESTS/canary"))
        with self.assertRaises(RuntimeError):
            certifier.trigger(self.conn, bad, self.console)

    def test_a_rerun_signs_the_same_bytes(self):
        self.approve()
        self.writer.fail = OSError("store writer gone")
        self.certify()
        a = self.conn.execute("SELECT * FROM actions").fetchone()
        self.assertEqual(a["state"], "approved")                       # Retry: backing off
        first = bytes(self.rows()["el9"]["manifest"])
        self.conn.execute("UPDATE actions SET not_before_ms = 0")
        actions.run_due(self.conn, self.deps, lambda a, c: True, kinds={certifier.KIND})
        self.assertEqual(self.rows()["el9"]["state"], "signed")
        self.assertEqual(self.writer.puts[0][1], first)                # computed once

    def test_refusals(self):
        cases = [
            (lambda: self.approve("bob"), "bob (who pre-approved pipeline 7) is not an admin"),
            (lambda: (self.approve(), self.s3.docs.__setitem__(
                "MANIFESTS/b2/host-1.el9.json", dict(self.bom, source_pipeline_id=9))),
             "is of pipeline 9, not 7"),
        ]
        for setup, why in cases:
            with self.subTest(why=why):
                for t in ("certify_runs", "actions", "approvals"):
                    self.conn.execute("DELETE FROM %s" % t)
                self.s3.docs["MANIFESTS/b2/host-1.el9.json"] = dict(self.bom)
                setup()
                self.certify()
                self.assertEqual(self.rows()["*"]["state"], "refused")
                self.assertIn(why, self.rows()["*"]["reason"])
                self.assertEqual(self.writer.puts, [])
        with patch.object(trust, "load_key_policy", lambda dirs=None: None):
            for t in ("certify_runs", "actions", "approvals"):
                self.conn.execute("DELETE FROM %s" % t)
            self.s3.docs["MANIFESTS/b2/host-1.el9.json"] = dict(self.bom)
            self.approve()
            self.certify()
        self.assertIn("fails closed", self.rows()["*"]["reason"])

    def test_a_revoked_approval_stops_the_run(self):
        aid = self.approve()
        certifier.trigger(self.conn, self.settings, self.console)
        self.conn.execute("UPDATE approvals SET revoked_ms = ? WHERE id = ?", (db.now_ms(), aid))
        actions.run_due(self.conn, self.deps, lambda a, c: True, kinds={certifier.KIND})
        self.assertEqual(self.rows()["*"]["state"], "refused")
        self.assertEqual(self.writer.puts, [])

    def test_the_daily_limit_pauses_certifying(self):
        self.settings.orch_certify_daily_signs = 0
        self.approve()
        with self.assertRaises(RuntimeError):
            certifier.trigger(self.conn, self.settings, self.console)

    def test_a_rerun_after_the_approval_is_revoked_signs_nothing(self):
        aid = self.approve()
        self.writer.fail = OSError("store writer gone")
        self.certify()                                                  # computed, signed
        self.assertEqual(self.rows()["el9"]["state"], "signing")
        self.assertIsNotNone(self.rows()["el9"]["signature"])           # kept at once
        self.conn.execute("UPDATE approvals SET revoked_ms = ? WHERE id = ?", (db.now_ms(), aid))
        self.conn.execute("UPDATE actions SET not_before_ms = 0")
        actions.run_due(self.conn, self.deps, lambda a, c: True, kinds={certifier.KIND})
        rows = self.rows()
        self.assertEqual((rows["*"]["state"], rows["el9"]["state"]), ("refused", "refused"))
        self.assertIsNone(rows["el9"]["manifest"])
        self.assertEqual(self.writer.puts, [])

    def test_a_retried_write_reuses_the_signature(self):
        self.approve()
        signed = []
        sign = self.deps.sign
        self.deps.sign = lambda body: signed.append(1) or sign(body)
        self.writer.fail = OSError("store writer gone")
        self.certify()
        self.conn.execute("UPDATE actions SET not_before_ms = 0")
        actions.run_due(self.conn, self.deps, lambda a, c: True, kinds={certifier.KIND})
        self.assertEqual((len(signed), self.rows()["el9"]["state"]), (1, "signed"))

    def test_the_daily_limit_defers_a_run_without_counting_an_attempt(self):
        self.approve()
        certifier.trigger(self.conn, self.settings, self.console)
        self.settings.orch_certify_daily_signs = 0
        actions.run_due(self.conn, self.deps, lambda a, c: True, kinds={certifier.KIND})
        a = self.conn.execute("SELECT * FROM actions").fetchone()
        self.assertEqual((a["state"], a["attempts"]), ("approved", 0))
        self.assertGreater(a["not_before_ms"], db.now_ms() + 3000_000)
        self.assertNotIn("*", {k for k, r in self.rows().items() if r["state"] == "refused"})
        self.assertIn("action.deferred", self.kinds())

    def test_the_limit_counts_signatures_not_updates(self):
        self.approve()
        self.certify()
        self.assertEqual(certifier.signed_today(self.conn), 1)
        self.conn.execute("UPDATE certify_runs SET updated_ms = ?", (db.now_ms(),))
        self.assertEqual(certifier.signed_today(self.conn), 1)

    def test_an_existing_action_keeps_its_approval(self):
        first = self.approve()
        certifier.trigger(self.conn, self.settings, self.console)
        self.conn.execute("UPDATE certify_runs SET state = 'waiting'")      # looked at again
        second = self.approve()
        certifier.trigger(self.conn, self.settings, self.console)
        rows = self.conn.execute("SELECT approval_id FROM actions").fetchall()
        self.assertEqual([r["approval_id"] for r in rows], [first])
        self.assertNotEqual(first, second)

    def test_an_architecture_is_a_plain_name(self):
        self.approve()
        bad = dict(self.bom, effective_architecture="el9/../../../../tmp/zz",
                   packages=[dict(p, effective_architecture="el9/../../../../tmp/zz")
                             for p in self.bom["packages"]])
        self.s3.docs["MANIFESTS/b2/host-1.el9.json"] = bad
        self.certify()
        self.assertIn("not a plain name", self.rows()["*"]["reason"])
        self.assertEqual(self.writer.puts, [])

    def test_gitlab_failing_refuses_no_build(self):
        from console_backend import forge_ops
        self.approve()
        raw = self.console.file_raw
        self.console.file_raw = lambda *a, **k: (_ for _ in ()).throw(forge_ops.ForgeError(401, "x"))
        with self.assertRaises(forge_ops.ForgeError):                  # the step fails
            certifier.trigger(self.conn, self.settings, self.console)
        self.assertEqual(self.rows(), {})
        self.console.file_raw = lambda *a, **k: (_ for _ in ()).throw(forge_ops.ForgeError(404, "x"))
        certifier.trigger(self.conn, self.settings, self.console)
        self.assertEqual(self.rows()["*"]["state"], "waiting")         # looked at again
        self.assertIn("404", self.rows()["*"]["reason"])
        self.console.file_raw = raw
        self.assertEqual(self.certify(), 1)
        self.assertEqual(self.rows()["*"]["state"], "signed")

    def test_a_preapproved_build_is_not_hidden_by_waiting_ones(self):
        now = db.now_ms()
        self.conn.execute("UPDATE kv SET value = '0' WHERE key = 'certify.since'")
        self.conn.execute(
            "INSERT INTO pipelines (id, is_build, community, sha, variables, outcome,"
            " first_seen_ms) VALUES (8, 1, 'lcg', 'c0ffee', ?, 'success', ?)",
            (json.dumps({"COMMUNITY": "LCG", "CERTIFY": "true"}), now))
        for i in range(3):                                 # older, never pre-approved
            self.conn.execute("INSERT INTO builds (pipeline_id, build_id, bom_keys, recorded_ms)"
                              " VALUES (8, ?, '[]', ?)", ("a%d" % i, now - 1000 + i))
        with patch.object(certifier, "PER_PASS", 1):
            for _ in range(4):
                certifier.trigger(self.conn, self.settings, self.console)
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM certify_runs WHERE"
                                               " state = 'waiting'").fetchone()[0], 4)
            self.approve()
            self.assertEqual(certifier.trigger(self.conn, self.settings, self.console), 1)
        self.assertEqual(self.rows()["*"]["state"], "signing")

    def test_certified_manifests_expire(self):
        for v in ("0", "-1", "nan", "inf", "1e9"):
            with self.assertRaises(ValueError):
                config.Settings(env=dict(SETTINGS, BITS_ORCH_CERTIFY_VALID_DAYS=v))

    def test_the_console_cannot_request_a_certification(self):
        from fastapi.testclient import TestClient
        from console_backend import main
        main.settings = config.Settings(env={"GITLAB_API_URL": "https://gl/api/v4",
                                             "BITS_ORCH_DB": self.path,
                                             "BITS_ADMINS_POLICY": "* @carol"})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        r = TestClient(main.app).post("/ops/actions", headers={"Authorization": "Bearer carol"},
                                      json={"kind": "certify",
                                            "params": {"pipeline": "7", "build_id": "b2"}})
        self.assertEqual(r.status_code, 400)
        self.assertIn("not requested by a person", r.json()["detail"])
