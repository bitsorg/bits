# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator's M0: database, event chain, actions, worker, API."""

import multiprocessing
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, forge_ops, main
from console_backend.orch import actions, db, events, kinds, worker

ALICE = events.Actor("person", "alice")
ADVISOR = events.Actor("advisor", "triage")


class FakeForge:
    """Pipelines 1-9 belong to 'testbed', 10+ to no community."""

    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail or {}

    def pipeline_community(self, pid):
        return "testbed" if int(pid) < 10 else ""

    def _do(self, what, pid):
        self.calls.append((what, pid))
        if what in self.fail:
            raise self.fail[what]

    def retry_pipeline(self, pid):
        self._do("retry", pid)

    def cancel_pipeline(self, pid):
        self._do("cancel", pid)


class _DB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "orch.db")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)


class DatabaseTest(_DB):
    def test_migrate_is_idempotent_and_versioned(self):
        self.assertEqual(db.migrate(self.conn), db.SCHEMA_VERSION)
        self.assertEqual(db.version(self.conn), db.SCHEMA_VERSION)
        db.check(self.conn)
        self.assertEqual(self.conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_newer_or_unmigrated_schema_is_refused(self):
        self.conn.execute("PRAGMA user_version=%d" % (db.SCHEMA_VERSION + 1))
        with self.assertRaises(db.SchemaError):
            db.check(self.conn)
        with self.assertRaises(db.SchemaError):
            db.migrate(self.conn)
        fresh = db.connect(os.path.join(self.dir, "new.db"))
        self.addCleanup(fresh.close)
        with self.assertRaises(db.SchemaError):
            db.check(fresh)

    def test_a_failed_transaction_leaves_nothing(self):
        with self.assertRaises(ZeroDivisionError):
            with db.transaction(self.conn):
                events.append(self.conn, "x", "s", ALICE)
                1 / 0
        self.assertEqual(events.since(self.conn), [])


def _hold_lock(path, ready, release):
    with db.worker_lock(path):
        ready.set()
        release.wait(10)


class LockTest(_DB):
    def test_a_second_worker_is_refused(self):
        ready, release = multiprocessing.Event(), multiprocessing.Event()
        p = multiprocessing.Process(target=_hold_lock, args=(self.path, ready, release))
        p.start()
        try:
            self.assertTrue(ready.wait(10))
            with self.assertRaises(db.LockedError):
                with db.worker_lock(self.path):
                    pass
        finally:
            release.set()
            p.join(10)
        with db.worker_lock(self.path):       # free again once the holder exits
            pass


class EventsTest(_DB):
    def _add(self, n):
        for i in range(n):
            with db.transaction(self.conn):
                events.append(self.conn, "test.e", "s%d" % (i % 2), ALICE, {"i": i})

    def test_append_needs_a_transaction(self):
        with self.assertRaises(RuntimeError):
            events.append(self.conn, "x", "s", ALICE)

    def test_chain_verifies_and_since_pages(self):
        self._add(5)
        self.assertEqual(events.verify(self.conn), (5, None))
        got = events.since(self.conn, after=2, limit=2)
        self.assertEqual([e["seq"] for e in got], [3, 4])
        self.assertEqual(got[0]["actor"], {"kind": "person", "id": "alice"})
        self.assertEqual([e["seq"] for e in events.since(self.conn, subject="s1")], [2, 4])

    def test_an_edited_or_removed_event_breaks_the_chain(self):
        self._add(5)
        self.conn.execute("UPDATE events SET payload = '{\"i\":99}' WHERE seq = 3")
        self.assertEqual(events.verify(self.conn), (3, 3))
        self.conn.execute("DELETE FROM events")
        self._add(5)
        self.conn.execute("DELETE FROM events WHERE seq = 2")
        self.assertEqual(events.verify(self.conn)[1], 3)

    def test_actor_and_payload_limits(self):
        with self.assertRaises(ValueError):
            events.Actor("robot", "x")
        with self.assertRaises(ValueError):
            events.Actor("person", "")
        with self.assertRaises(ValueError):
            with db.transaction(self.conn):
                events.append(self.conn, "x", "s", ALICE, {"big": "x" * events.MAX_PAYLOAD})


class ValidateTest(unittest.TestCase):
    def test_schema_subset(self):
        s = {"type": "object", "required": ["a"], "additionalProperties": False,
             "properties": {"a": {"type": "string", "pattern": "[0-9]+"},
                            "b": {"type": "integer", "minimum": 1},
                            "c": {"type": "array", "items": {"enum": ["x", "y"]}}}}
        actions.validate(s, {"a": "12", "b": 3, "c": ["x"]})
        for bad in ({}, {"a": "1x"}, {"a": "1", "b": 0}, {"a": "1", "b": True},
                    {"a": "1", "c": ["z"]}, {"a": "1", "d": 1}, {"a": "1" * 2000}):
            with self.assertRaises(actions.ActionError, msg=bad):
                actions.validate(s, bad)


class ActionsTest(_DB):
    def setUp(self):
        super().setUp()
        self.forge = FakeForge()
        self.deps = kinds.Deps(None)
        self.deps.forge = lambda: self.forge
        self.allow = lambda actor, community: actor.kind == "person" and actor.id == "alice"

    def _req(self, pid="5", actor=ALICE, kind="pipeline.retry", **kw):
        return actions.request(self.conn, kind, {"pipeline": pid}, actor, "testbed",
                               kw.pop("authorized", True), **kw)

    def _kinds(self):
        return [e["kind"] for e in events.since(self.conn, limit=1000)]

    def test_catalogue_lists_kinds_with_schemas(self):
        cat = {c["kind"]: c for c in actions.catalogue()}
        self.assertIn("pipeline.retry", cat)
        self.assertEqual(cat["pipeline.cancel"]["params"]["required"], ["pipeline"])

    def test_an_authorized_person_is_approved_at_request(self):
        row, created = self._req()
        self.assertTrue(created)
        self.assertEqual((row["state"], row["approved_by_id"], row["subject"]),
                         ("approved", "alice", "pipeline:5"))
        self.assertEqual(self._kinds(), ["action.requested", "action.approved"])

    def test_the_same_idempotency_key_returns_the_first_action(self):
        a, _ = self._req(idempotency_key="k1")
        b, created = self._req(idempotency_key="k1")
        self.assertEqual((a["id"], created), (b["id"], False))
        c, _ = self._req()
        self.assertNotEqual(c["id"], a["id"])
        with self.assertRaises(actions.ActionError):          # same key, other params
            self._req(pid="6", idempotency_key="k1")
        # Keys belong to their requester: another person's "k1" is their own action.
        d, created = self._req(actor=events.Actor("person", "carol"), idempotency_key="k1")
        self.assertTrue(created)
        self.assertNotEqual(d["id"], a["id"])

    def test_bad_requests(self):
        with self.assertRaises(actions.ActionError):
            self._req(pid="12a")
        with self.assertRaises(actions.ActionError):
            self._req(pid="05")                    # one spelling per pipeline
        with self.assertRaises(actions.ActionError):
            self._req(kind="pipeline.explode")
        with self.assertRaises(actions.ActionError):
            self._req(authorized=False)

    def test_an_advisor_only_proposes_and_only_a_person_decides(self):
        row, _ = self._req(actor=ADVISOR, authorized=False)
        self.assertEqual(row["state"], "proposed")
        self.assertEqual(actions.run_due(self.conn, self.deps, self.allow), 0)
        with self.assertRaises(actions.ActionError):
            actions.approve(self.conn, row["id"], ADVISOR, True)
        with self.assertRaises(actions.ActionError):
            actions.approve(self.conn, row["id"], events.Actor("person", "mallory"), False)
        ok = actions.approve(self.conn, row["id"], ALICE, True)
        self.assertEqual((ok["state"], ok["approved_by_id"]), ("approved", "alice"))
        with self.assertRaises(actions.ActionError):
            actions.reject(self.conn, row["id"], ALICE, True)      # no longer proposed
        other, _ = self._req(actor=ADVISOR, authorized=False, pid="6")
        rej = actions.reject(self.conn, other["id"], ALICE, True, "not now")
        self.assertEqual((rej["state"], rej["error"]), ("rejected", "not now"))

    def test_run_due_runs_and_records(self):
        self._req()
        self.assertEqual(actions.run_due(self.conn, self.deps, self.allow), 1)
        self.assertEqual(self.forge.calls, [("retry", "5")])
        row = actions.listing(self.conn)[0]
        self.assertEqual((row["state"], row["result"], row["attempts"]),
                         ("done", {"status": "retried"}, 1))
        self.assertEqual(self._kinds()[-2:], ["action.started", "action.done"])
        self.assertEqual(events.verify(self.conn)[1], None)

    def test_transient_errors_back_off_then_fail(self):
        self.forge.fail["retry"] = forge_ops.ForgeError(503, "busy")
        row, _ = self._req()
        for attempt in range(1, actions.MAX_ATTEMPTS + 1):
            self.conn.execute("UPDATE actions SET not_before_ms = 0")
            self.assertEqual(actions.run_due(self.conn, self.deps, self.allow), 1)
            state = actions.get(self.conn, row["id"])
            self.assertEqual(state["attempts"], attempt)
        self.assertEqual(state["state"], "failed")
        self.assertIn("busy", state["error"])
        self.assertEqual(self._kinds().count("action.retrying"), actions.MAX_ATTEMPTS - 1)

    def test_backoff_delays_the_next_attempt(self):
        self.forge.fail["retry"] = forge_ops.ForgeError(0, "timeout")
        self._req()
        actions.run_due(self.conn, self.deps, self.allow)
        self.assertEqual(actions.run_due(self.conn, self.deps, self.allow), 0)

    def test_permanent_errors_fail_at_once(self):
        self.forge.fail["cancel"] = forge_ops.ForgeError(404, "no pipeline")
        row, _ = self._req(kind="pipeline.cancel")
        actions.run_due(self.conn, self.deps, self.allow)
        got = actions.get(self.conn, row["id"])
        self.assertEqual((got["state"], got["attempts"]), ("failed", 1))

    def test_a_lapsed_authority_fails_the_action_unrun(self):
        self._req()
        actions.run_due(self.conn, self.deps, lambda a, c: False)
        self.assertEqual(self.forge.calls, [])
        self.assertIn("no longer authorized", actions.listing(self.conn)[0]["error"])

    def test_an_unreadable_policy_retries(self):
        self._req()

        def broken(actor, community):
            raise OSError("gitlab down")
        actions.run_due(self.conn, self.deps, broken)
        row = actions.listing(self.conn)[0]
        self.assertEqual(row["state"], "approved")
        self.assertIn("gitlab down", row["error"])

    def test_a_later_action_waits_for_an_earlier_one_backing_off(self):
        self.forge.fail["retry"] = forge_ops.ForgeError(503, "busy")
        self._req()
        self._req(kind="pipeline.cancel")
        actions.run_due(self.conn, self.deps, self.allow)
        self.assertEqual(self.forge.calls, [("retry", "5")])    # the cancel waits
        del self.forge.fail["retry"]
        self.conn.execute("UPDATE actions SET not_before_ms = 0")
        actions.run_due(self.conn, self.deps, self.allow)
        self.assertEqual(self.forge.calls, [("retry", "5"), ("retry", "5"), ("cancel", "5")])

    def test_an_approver_kind_needs_someone_else(self):
        k = actions.Kind(name="test.second", summary="", params={"type": "object"},
                         approval="approver", subject=lambda p: "t", scope=lambda p, d: "",
                         run=lambda p, d: None)
        actions.register(k)
        self.addCleanup(actions.REGISTRY.pop, "test.second")
        row, _ = actions.request(self.conn, "test.second", {}, ALICE, "testbed", True)
        self.assertEqual(row["state"], "proposed")
        with self.assertRaises(actions.ActionError):
            actions.approve(self.conn, row["id"], ALICE, True)
        ok = actions.approve(self.conn, row["id"], events.Actor("person", "carol"), True)
        self.assertEqual(ok["state"], "approved")

    def test_listing_filters_communities_before_the_limit(self):
        self._req()
        for pid in range(10, 15):
            actions.request(self.conn, "pipeline.cancel", {"pipeline": str(pid)}, ALICE, "",
                            True)
        got = actions.listing(self.conn, communities={"testbed"}, limit=2)
        self.assertEqual([r["community"] for r in got], ["testbed"])
        self.assertEqual(actions.listing(self.conn, communities=set()), [])

    def test_one_running_action_per_subject(self):
        a, _ = self._req()
        b, _ = self._req()
        first = actions.claim_next(self.conn)
        self.assertEqual(first["id"], a["id"])
        self.assertIsNone(actions.claim_next(self.conn))   # b waits for a
        actions.finish(self.conn, first, result={})
        self.assertEqual(actions.claim_next(self.conn)["id"], b["id"])

    def test_recover_reruns_or_fails_interrupted_actions(self):
        a, _ = self._req()
        actions.claim_next(self.conn)
        unsafe = actions.Kind(name="test.unsafe", summary="", params={"type": "object"},
                              approval="requester", subject=lambda p: "u",
                              scope=lambda p, d: "", run=lambda p, d: None,
                              safe_to_rerun=False)
        actions.register(unsafe)
        self.addCleanup(actions.REGISTRY.pop, "test.unsafe")
        b, _ = actions.request(self.conn, "test.unsafe", {}, ALICE, "", True)
        actions.claim_next(self.conn)
        self.assertEqual(actions.recover(self.conn), 2)
        self.assertEqual(actions.get(self.conn, a["id"])["state"], "approved")
        self.assertEqual(actions.get(self.conn, b["id"])["state"], "failed")

    def test_an_action_that_keeps_crashing_the_worker_stops(self):
        a, _ = self._req()
        for _ in range(actions.MAX_ATTEMPTS):
            self.assertIsNotNone(actions.claim_next(self.conn))
            actions.recover(self.conn)
        self.assertEqual(actions.get(self.conn, a["id"])["state"], "failed")


class WorkerTest(_DB):
    def setUp(self):
        super().setUp()
        self.settings = config.Settings(env={})
        self.w = worker.Worker(self.conn, self.settings,
                               heartbeat=os.path.join(self.dir, "hb"))
        self.forge = FakeForge()
        self.w.deps.forge = lambda: self.forge
        self.w.authorize = lambda actor, community: True

    def test_a_pass_runs_actions_and_beats(self):
        self.w.start()
        actions.request(self.conn, "pipeline.retry", {"pipeline": "3"}, ALICE, "testbed", True)
        self.w.run_once()
        self.assertEqual(self.forge.calls, [("retry", "3")])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "hb")))

    def test_a_failing_step_is_recorded_once_and_its_recovery(self):
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("disk")
        self.w.steps = [("flaky", flaky)]
        with self.assertLogs("bits_orchestrator", "ERROR"):
            for _ in range(4):
                self.w.run_once()
        kinds_ = [e["kind"] for e in events.since(self.conn)]
        self.assertEqual(kinds_, ["worker.step_failed", "worker.step_recovered"])

    def test_the_worker_needs_a_resolve_token_for_group_policies(self):
        from console_backend.orch import authority
        self.assertEqual(authority.unresolvable(config.Settings(env={
            "BITS_ADMINS_POLICY": "testbed @alice"})), "")
        self.assertIn("RESOLVE_TOKEN", authority.unresolvable(config.Settings(env={
            "BITS_ADMINS_POLICY": "testbed &cern/bits-admins"})))
        self.assertIn("RESOLVE_TOKEN", authority.unresolvable(config.Settings(env={
            "BITS_ADMINS_GROUP": "bits-admins"})))
        self.assertEqual(authority.unresolvable(config.Settings(env={
            "BITS_ADMINS_GROUP": "bits-admins", "BITS_ADMIN_RESOLVE_TOKEN": "t"})), "")

    def test_the_real_checker_needs_a_person_admin(self):
        from console_backend.orch import authority
        s = config.Settings(env={"BITS_ADMINS_POLICY": "testbed @alice\n* @carol"})
        ok = authority.checker(s)
        self.assertTrue(ok(ALICE, "testbed"))
        self.assertFalse(ok(ALICE, "other"))
        self.assertFalse(ok(ALICE, ""))
        self.assertTrue(ok(events.Actor("person", "carol"), ""))
        self.assertFalse(ok(events.Actor("schedule", "nightly"), "testbed"))
        self.assertFalse(ok(ADVISOR, "testbed"))
        self.assertTrue(ok(events.WORKER, "testbed"))


class ApiTest(_DB):
    def setUp(self):
        super().setUp()
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4",
            "BITS_ADMINS_POLICY": "testbed @alice\nother @bob\n* @carol",
            "BITS_FORGE_OPS_TOKEN": "optok", "BITS_FORGE_PROJECT": "42",
            "BITS_ORCH_DB": self.path})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.forge = FakeForge()
        p = patch.object(forge_ops.GitLabForge, "from_settings", lambda s: self.forge)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def _h(self, user):
        return {"Authorization": "Bearer %s" % user} if user else {}

    def _post(self, user, body, path="/ops/actions"):
        return self.client.post(path, headers=self._h(user), json=body)

    def test_catalogue_needs_a_login(self):
        self.assertEqual(self.client.get("/ops/actions/catalogue").status_code, 401)
        r = self.client.get("/ops/actions/catalogue", headers=self._h("dave"))
        self.assertIn("pipeline.retry", [a["kind"] for a in r.json()["actions"]])

    def test_a_community_admin_requests_and_the_worker_runs(self):
        r = self._post("alice", {"kind": "pipeline.retry", "params": {"pipeline": "5"},
                                 "idempotency_key": "x"})
        self.assertEqual(r.status_code, 201, r.text)
        self.assertEqual((r.json()["state"], r.json()["community"]), ("approved", "testbed"))
        again = self._post("alice", {"kind": "pipeline.retry", "params": {"pipeline": "5"},
                                     "idempotency_key": "x"})
        self.assertEqual((again.status_code, again.json()["id"]), (200, r.json()["id"]))
        actions.run_due(self.conn, kinds.Deps(main.settings), lambda a, c: True)
        self.assertEqual(self.forge.calls, [("retry", "5")])
        got = self.client.get("/ops/actions/%d" % r.json()["id"], headers=self._h("alice"))
        self.assertEqual(got.json()["state"], "done")

    def test_authority_is_the_target_community(self):
        body = {"kind": "pipeline.retry", "params": {"pipeline": "5"}}
        self.assertEqual(self._post(None, body).status_code, 401)
        self.assertEqual(self._post("bob", body).status_code, 403)
        unscoped = {"kind": "pipeline.cancel", "params": {"pipeline": "12"}}
        self.assertEqual(self._post("alice", unscoped).status_code, 403)
        self.assertEqual(self._post("carol", unscoped).status_code, 201)
        self.assertEqual(self._post("alice", {"kind": "pipeline.retry",
                                              "params": {"pipeline": "x"}}).status_code, 400)
        self.assertEqual(self._post("alice", {"kind": "nope", "params": {}}).status_code, 400)

    def test_lists_and_reads_are_limited_to_ones_communities(self):
        mine = self._post("alice", {"kind": "pipeline.retry", "params": {"pipeline": "5"}}).json()
        self._post("carol", {"kind": "pipeline.cancel", "params": {"pipeline": "12"}})
        seen = lambda u: [a["id"] for a in self.client.get(
            "/ops/actions", headers=self._h(u)).json()["actions"]]
        self.assertEqual(seen("alice"), [mine["id"]])
        self.assertEqual(seen("bob"), [])
        self.assertEqual(len(seen("carol")), 2)
        self.assertEqual(self.client.get("/ops/actions/%d" % mine["id"],
                                         headers=self._h("bob")).status_code, 404)

    def test_a_proposal_is_approved_by_an_admin_of_its_community(self):
        row, _ = actions.request(self.conn, "pipeline.retry", {"pipeline": "5"}, ADVISOR,
                                 "testbed", False)
        url = "/ops/actions/%d/approve" % row["id"]
        self.assertEqual(self._post("bob", {}, url).status_code, 404)
        r = self._post("alice", {}, url)
        self.assertEqual((r.status_code, r.json()["state"]), (200, "approved"))
        self.assertEqual(self._post("alice", {}, url).status_code, 409)
        other, _ = actions.request(self.conn, "pipeline.retry", {"pipeline": "6"}, ADVISOR,
                                   "testbed", False)
        r = self._post("alice", {"reason": "flaky runner"},
                       "/ops/actions/%d/reject" % other["id"])
        self.assertEqual((r.json()["state"], r.json()["error"]), ("rejected", "flaky runner"))

    def test_events_are_for_overall_admins(self):
        self._post("alice", {"kind": "pipeline.retry", "params": {"pipeline": "5"}})
        self.assertEqual(self.client.get("/ops/events", headers=self._h("alice")).status_code, 403)
        r = self.client.get("/ops/events?after=1", headers=self._h("carol"))
        self.assertEqual([e["kind"] for e in r.json()["events"]], ["action.approved"])
        r = self.client.get("/ops/events?after=99&wait=1", headers=self._h("carol"))
        self.assertEqual(r.json()["events"], [])

    def test_an_unconfigured_forge_is_503_to_the_caller(self):
        with patch.object(forge_ops.GitLabForge, "from_settings", lambda s: None):
            r = self._post("alice", {"kind": "pipeline.retry", "params": {"pipeline": "5"}})
        self.assertEqual(r.status_code, 503)

    def test_another_users_key_is_not_theirs(self):
        body = {"kind": "pipeline.cancel", "params": {"pipeline": "12"}, "idempotency_key": "k"}
        first = self._post("carol", body).json()
        mine = self._post("alice", dict(body, params={"pipeline": "5"}))
        self.assertEqual(mine.status_code, 201)
        self.assertNotEqual(mine.json()["id"], first["id"])
        self.assertEqual(self._post("carol", dict(body, params={"pipeline": "13"})).status_code,
                         400)

    def test_unconfigured_or_unmigrated_is_503(self):
        main.settings.orch_db = ""
        r = self.client.get("/ops/actions", headers=self._h("alice"))
        self.assertEqual(r.status_code, 503)
        main.settings.orch_db = os.path.join(self.dir, "empty.db")
        r = self.client.get("/ops/actions", headers=self._h("alice"))
        self.assertEqual(r.status_code, 503)
        self.assertIn("has the worker started", r.json()["detail"])
        self.assertFalse(os.path.exists(main.settings.orch_db))     # not created
        db.connect(main.settings.orch_db).close()
        r = self.client.get("/ops/actions", headers=self._h("alice"))
        self.assertIn("not migrated", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
