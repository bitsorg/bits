# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator's M3: approvals in the database. The sign path's tests run
again against the database-backed store, and its own properties are tested:
surviving a restart, expiry, revocation, the sign cap across processes, standing
approvals."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, main
from console_backend.orch import approvals, db, events

import test_cli_preapprove as tcp
import test_preapprove as tp
import test_sign_preapproved as tsp


class _WithDB:
    """Run a sign-path test class with approved pre-approvals in a database:
    every Settings the tests build gets BITS_ORCH_DB."""

    def setUp(self):
        self._dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._dir, True)
        self.db_path = os.path.join(self._dir, "orch.db")
        c = db.connect(self.db_path)
        db.migrate(c)
        c.close()
        real_init = config.Settings.__init__
        path = self.db_path

        def init(s, env=None):
            real_init(s, env)
            s.orch_db = path
        p = patch.object(config.Settings, "__init__", init)
        p.start()
        self.addCleanup(p.stop)
        # The sign-path tests replace these module functions and never put them
        # back; keep them from leaking into the tests that run after these.
        for mod, names in ((main.ci_auth, ("verify_ci_token", "load_ci_signers",
                                           "is_ci_authorized")),
                           (main.identity, ("verify_gitlab_token",))):
            for name in names:
                self.addCleanup(setattr, mod, name, getattr(mod, name))
        super().setUp()
        main.settings.orch_db = path
        main.preapprovals = approvals.Preapprovals(lambda: main.settings)

    def _rows(self):
        c = db.connect(self.db_path)
        try:
            return c.execute("SELECT * FROM approvals").fetchall()
        finally:
            c.close()


class TestSignPreapprovedDB(_WithDB, tsp.TestSignPreapproved):
    @unittest.skip("sets the in-memory sign counter by hand; the cap is tested below")
    def test_preapproval_status_lookup(self):
        pass

    def test_cli_preapproval_refusals_do_not_consume(self):
        # As the in-memory test, where an unused record has signs 0 rather than none.
        with patch.object(self, "assertNotIn",
                          lambda key, rec: self.assertEqual(rec.get(key, 0), 0)):
            super().test_cli_preapproval_refusals_do_not_consume()

    def test_exhausted_after_the_cap(self):
        self._preapprove("p1", ["lcg"])
        for _ in range(main._PREAPPROVAL_SIGN_CAP):
            self.assertEqual(self._sign("p1", "lcg").status_code, 200)
        self.assertEqual(self._sign("p1", "lcg").status_code, 429)
        self.assertEqual(self.client.get("/preapproval/p1").json()["status"], "exhausted")

    def test_an_unusable_store_fails_the_sign_closed(self):
        self._preapprove("p1", ["lcg"])
        main.settings.orch_db = self.db_path + ".gone"
        self.assertEqual(self._sign("p1", "lcg").status_code, 503)

    def test_a_sign_is_an_event_and_the_record_outlives_the_process(self):
        self._preapprove("p1", ["lcg"])
        self.assertEqual(self._sign("p1", "lcg").status_code, 200)
        main.preapprovals = approvals.Preapprovals(lambda: main.settings)   # a restart
        rec = main.preapprovals.get("p1")
        self.assertEqual((rec["status"], rec["user"], rec["signs"]), ("approved", "alice", 1))
        c = db.connect(self.db_path)
        kinds = [e["kind"] for e in events.since(c)]
        self.assertEqual(kinds, ["approval.granted", "approval.used"])
        self.assertEqual(events.verify(c)[1], None)
        c.close()


class TestPreapproveDB(_WithDB, tp.TestPreapprove):
    @unittest.skip("memory eviction does not apply to the database")
    def test_eviction_keeps_approved(self):
        pass

    def test_the_approval_keeps_its_assertion(self):
        self.test_flow_records_preapproval()
        (r,) = self._rows()
        self.assertEqual((r["kind"], r["via"], r["approver"]), ("preapproval", "console", "alice"))
        self.assertIn("clientDataJSON", r["assertion"])


class TestCliPreapproveDB(_WithDB, tcp.TestCliPreapprove):
    def test_the_bound_packages_are_stored(self):
        self.test_full_flow_records_bound_preapproval()
        (r,) = self._rows()
        self.assertEqual(r["via"], "cli")
        self.assertTrue(json.loads(r["packages"]))
        self.assertTrue(r["challenge"])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "orch.db")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.settings = config.Settings(env={"BITS_ORCH_DB": self.path})

    def _store(self, cap=16):
        return approvals.Preapprovals(lambda: self.settings, sign_cap=cap)

    def _approve(self, s, bid="77", user="alice"):
        return s.approve(bid, {"groups": ["lcg"], "user": user, "challenge": b"c"},
                         assertion={"id": "cred"})

    def test_pending_in_memory_approved_in_the_database(self):
        s = self._store()
        s.put("77", {"groups": ["lcg"], "user": "alice", "status": "pending", "challenge": b"c"})
        self.assertEqual(s.get("77")["status"], "pending")
        self.assertIsNone(self._store().get("77"))        # another process: not there
        self.assertTrue(self._approve(s))
        self.assertEqual(self._store().get("77")["status"], "approved")
        self.assertFalse(self._approve(s))                 # already approved

    def test_the_sign_cap_holds_across_processes(self):
        a, b = self._store(cap=3), self._store(cap=3)
        self._approve(a)
        got = [bool(x) for x in (a.reserve_sign("77", 16), b.reserve_sign("77", 16),
                                 a.reserve_sign("77", 16), b.reserve_sign("77", 16))]
        self.assertEqual(got, [True, True, True, False])
        b.release_sign("77", a.get("77")["id"])
        self.assertTrue(a.reserve_sign("77", 16))
        self.assertFalse(a.reserve_sign("nope", 16))

    def test_a_sign_is_given_back_to_the_approval_it_was_taken_on(self):
        s = self._store()
        self._approve(s)
        first = s.get("77")["id"]
        slot = s.reserve_sign("77", 16, first)
        self.assertEqual(slot, first)
        approvals.revoke(self.conn, first, "carol")            # meanwhile: revoked and re-granted
        self._approve(s, user="bob")
        second = s.get("77")["id"]
        self.assertIsNone(s.reserve_sign("77", 16, first))     # not the expected one any more
        s.release_sign("77", slot)                             # the failed sign of the first
        signs = dict(self.conn.execute("SELECT id, signs FROM approvals").fetchall())
        self.assertEqual(signs, {first: 0, second: 0})

    def test_expiry_and_revocation(self):
        s = self._store()
        self._approve(s)
        self.conn.execute("UPDATE approvals SET expires_ms = 1")
        self.assertIsNone(s.get("77"))
        self.assertTrue(self._approve(s, user="bob"))     # a fresh one may be given
        (r,) = approvals.listing(self.conn)
        self.assertTrue(approvals.revoke(self.conn, r["id"], "carol"))
        self.assertFalse(approvals.revoke(self.conn, r["id"], "carol"))
        self.assertIsNone(s.get("77"))
        self.assertFalse(s.reserve_sign("77", 16))
        self.assertEqual(approvals.listing(self.conn), [])
        self.assertEqual(len(approvals.listing(self.conn, active_only=False)), 2)

    def test_an_unusable_database_fails_closed(self):
        self.settings.orch_db = os.path.join(self.dir, "missing.db")
        with self.assertRaises(approvals.StoreError):
            self._store().get("77")

    def test_standing_runs_and_days(self):
        sha = "ab" * 32
        sched = "communities/lcg/pipelines/nightly.json"
        aid = approvals.grant_standing(self.conn, sched, sha, ["lcg"], "alice", 2, 30,
                                       {"id": "c"}, b"chal")
        self.assertIsNotNone(aid)
        self.assertIsNone(approvals.grant_standing(self.conn, sched, sha, ["lcg"], "alice", 2, 30,
                                                   {}, b"x"))         # one per schedule content
        self.assertIsNone(approvals.use_standing(self.conn, sched, "cd" * 32, ["lcg"], 1))
        self.assertIsNone(approvals.use_standing(self.conn, sched, sha, ["lcg", "atlas"], 1))
        self.assertIsNotNone(approvals.use_standing(self.conn, sched, sha, ["lcg"], 1))
        self.assertIsNotNone(approvals.use_standing(self.conn, sched, sha, ["lcg"], 2))
        self.assertIsNone(approvals.use_standing(self.conn, sched, sha, ["lcg"], 3))   # runs spent
        for runs, days in ((0, 1), (101, 1), (1, 31), (1, 0)):
            with self.assertRaises(ValueError):
                approvals.grant_standing(self.conn, sched, "ee" * 32, ["lcg"], "a", runs, days,
                                         {}, b"x")
        kinds = [e["kind"] for e in events.since(self.conn)]
        self.assertEqual(kinds, ["approval.granted", "approval.used", "approval.used"])
        self.assertIsNone(approvals.use_standing(self.conn, sched, sha, [], 4))
        (row,) = approvals.listing(self.conn, active_only=False)
        self.assertTrue(approvals.verify_standing(row))
        self.conn.execute("UPDATE approvals SET groups = '[\"lcg\", \"atlas\"]'")
        self.assertFalse(approvals.verify_standing(
            self.conn.execute("SELECT * FROM approvals").fetchone()))   # tampered

    def test_listing_by_community_before_the_limit(self):
        for i in range(5):
            self._approve(self._store(), bid="o%d" % i, user="bob")
        self.conn.execute("UPDATE approvals SET groups = '[\"atlas\"]'")
        self._approve(self._store(), bid="mine")
        got = approvals.listing(self.conn, limit=2, communities={"lcg"}, user="nobody")
        self.assertEqual([r["subject"] for r in got], ["mine"])
        self.assertEqual(len(approvals.listing(self.conn, communities=set(), user="bob")), 5)


class BackupTest(unittest.TestCase):
    def test_a_daily_consistent_copy_keeping_a_week(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(conn.close)
        db.migrate(conn)
        with db.transaction(conn):
            events.append(conn, "x", "s", events.WORKER)
        out = os.path.join(d, "copies")
        for day in range(10):
            db.backup(conn, out, keep=7, now=86400 * (20000 + day))
        self.assertIsNone(db.backup(conn, out, keep=7, now=86400 * 20009))   # today's exists
        files = sorted(os.listdir(out))
        self.assertEqual(len(files), 7)
        copy = db.connect(os.path.join(out, files[-1]))
        self.assertEqual(events.verify(copy), (1, None))
        copy.close()


class ApiTest(unittest.TestCase):
    def setUp(self):
        for name in ("settings", "credstore", "preapprovals"):
            self.addCleanup(setattr, main, name, getattr(main, name))
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "orch.db")
        c = db.connect(self.path)
        db.migrate(c)
        self.conn = c
        self.addCleanup(c.close)
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4",
            "BITS_ADMINS_POLICY": "lcg @alice\natlas @bob\n* @carol",
            "BITS_ORCH_DB": self.path, "BITS_WEBAUTHN_RP_ID": tp.RP_ID,
            "BITS_WEBAUTHN_ORIGIN": tp.ORIGIN,
            "BITS_WEBAUTHN_CREDENTIALS": os.path.join(self.dir, "creds.json")})
        main.credstore = main.credentials.CredentialStore(main.settings.credentials_path)
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.sched = "communities/lcg/pipelines/nightly.json"

    def _post(self, user, path, body):
        return self.client.post(path, json=body, headers={"Authorization": "Bearer " + user})

    def _get(self, user, path):
        return self.client.get(path, headers={"Authorization": "Bearer " + user})

    def test_a_standing_request_is_checked_before_any_passkey(self):
        body = {"schedule": self.sched, "sha256": "ab" * 32, "groups": ["lcg"], "runs": 5, "days": 30}
        self.assertEqual(self._post("bob", "/approvals/standing/request", body).status_code, 403)
        for bad in ({"days": 31}, {"runs": 0}, {"runs": True}, {"sha256": "xyz"},
                    {"schedule": "communities/lcg/../x.json"}, {"groups": []}):
            self.assertEqual(self._post("alice", "/approvals/standing/request",
                                        dict(body, **bad)).status_code, 400, bad)
        r = self._post("alice", "/approvals/standing/request", body)
        self.assertEqual(r.status_code, 400)                # no passkey enrolled
        self.assertIn("passkey", r.json()["detail"])

    def test_the_standing_flow_with_a_passkey(self):
        from soft_webauthn import SoftWebauthnDevice
        from console_backend import credentials, webauthn_rp
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4",
            "BITS_ADMINS_POLICY": "lcg @alice\natlas @bob\n* @carol", "BITS_ORCH_DB": self.path,
            "BITS_WEBAUTHN_RP_ID": tp.RP_ID, "BITS_WEBAUTHN_ORIGIN": tp.ORIGIN,
            "BITS_WEBAUTHN_REQUIRE_UV": "0",
            "BITS_WEBAUTHN_CREDENTIALS": os.path.join(self.dir, "creds.json")})
        main.credstore = credentials.CredentialStore(main.settings.credentials_path)
        dev = SoftWebauthnDevice()
        opts_json, chal = webauthn_rp.registration_options(main.settings, "alice", [])
        main.credstore.add("alice", webauthn_rp.verify_registration(
            main.settings, tp._attestation(dev, opts_json), chal))
        body = {"schedule": self.sched, "sha256": "ab" * 32, "groups": ["lcg"], "runs": 5, "days": 30}
        rq = self._post("alice", "/approvals/standing/request", body)
        self.assertEqual(rq.status_code, 200, rq.text)
        rq = rq.json()
        asr = tp._assertion(dev, rq["publicKey"])
        self.assertEqual(self._post("bob", "/approvals/standing/approve",
                                    {"request_id": rq["request_id"], "assertion": asr}).status_code, 403)
        ok = self._post("alice", "/approvals/standing/approve",
                        {"request_id": rq["request_id"], "assertion": asr})
        self.assertEqual(ok.status_code, 200, ok.text)
        again = self._post("alice", "/approvals/standing/approve",
                           {"request_id": rq["request_id"], "assertion": asr})
        self.assertEqual(again.status_code, 400)            # single use
        listed = self._get("alice", "/approvals?kind=standing").json()["approvals"]
        self.assertEqual([(a["runs_left"], a["binding"]["schedule"]) for a in listed],
                         [(5, self.sched)])
        self.assertEqual(self._get("bob", "/approvals").json()["approvals"], [])
        aid = listed[0]["id"]
        self.assertEqual(self._post("bob", "/approvals/%d/revoke" % aid, {}).status_code, 403)
        self.assertEqual(self._post("alice", "/approvals/%d/revoke" % aid, {}).status_code, 200)
        self.assertEqual(self._post("carol", "/approvals/%d/revoke" % aid, {}).status_code, 409)
        self.assertEqual(self._get("alice", "/approvals").json()["approvals"], [])
        self.assertEqual(len(self._get("carol", "/approvals?all=1").json()["approvals"]), 1)
        (row,) = approvals.listing(self.conn, active_only=False)
        self.assertIn("clientDataJSON", row["assertion"])

    def test_an_unusable_store_is_503_on_the_sign_path(self):
        main.preapprovals = approvals.Preapprovals(lambda: main.settings)
        main.settings.orch_db = os.path.join(self.dir, "missing.db")
        r = self.client.get("/preapproval/123")
        self.assertEqual(r.status_code, 503)

    def test_a_groupless_preapproval_is_revoked_by_its_approver_or_overall_admins(self):
        s = approvals.Preapprovals(lambda: main.settings)
        s.approve("p9", {"groups": [], "user": "alice", "challenge": b"c"})
        aid = s.get("p9")["id"]
        self.assertEqual(self._post("bob", "/approvals/%d/revoke" % aid, {}).status_code, 403)
        self.assertNotIn("lcg", self._post("bob", "/approvals/%d/revoke" % aid, {}).text)
        self.assertEqual([a["id"] for a in self._get("alice", "/approvals").json()["approvals"]],
                         [aid])                              # its approver sees it
        self.assertEqual(self._get("bob", "/approvals").json()["approvals"], [])
        self.assertEqual(self._post("alice", "/approvals/%d/revoke" % aid, {}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
