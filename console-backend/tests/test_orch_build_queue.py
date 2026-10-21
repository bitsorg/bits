# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The build queue: builds wait in the orchestrator and start one per build host."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, forge_ops, main
from console_backend.orch import build_queue as bq, db

HOSTS = """
hosts:
  - {name: bits, tag: bits-host-bits, arch: x86_64}
  - {name: pc, tag: bits-host-pc, arch: x86_64, runner: pc-runner}
  - {name: arm, tag: bits-host-arm}
"""


def _build(tag="bits-build-x86_64", pkg="ROOT", **more):
    v = {"PACKAGE": pkg, "COMMUNITY": "lcg", "BITS_RUNNER_TAG": tag}
    v.update(more)
    return ("%s build" % pkg, [{"key": k, "value": x} for k, x in v.items()])


class FakeForge:
    def __init__(self):
        self.started, self.next_id, self.fail, self.deleted = [], 100, None, []

    def delete_pipeline(self, pid):
        self.deleted.append(pid)

    def trigger_pipeline(self, ref, variables, name=None):
        if self.fail:
            e, self.fail = self.fail, None
            if isinstance(e, Exception):
                raise e
            return e
        self.next_id += 1
        self.started.append((self.next_id, {v["key"]: v["value"] for v in variables}))
        return {"id": self.next_id, "status": "created"}


class QueueTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.hosts = bq.parse_hosts(HOSTS)
        self.forge = FakeForge()

    def _queue(self, *builds):
        return bq.add(self.conn, "lcg", "alice", "main", list(builds))[1]

    def _pipeline(self, pid, outcome="running", tag="", jobs=(), token=None):
        v = {"PACKAGE": "X", "BITS_RUNNER_TAG": tag}
        if token is not None:
            v[bq.ENTRY_VAR] = token
        with db.transaction(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO pipelines (id, is_build, outcome, variables,"
                              " jobs, first_seen_ms) VALUES (?, 1, ?, ?, ?, 0)",
                              (pid, outcome, json.dumps(v), json.dumps(list(jobs))))

    def _unpause(self):
        with db.transaction(self.conn):
            self.conn.execute("DELETE FROM kv WHERE key = 'queue.pause'")

    def _states(self):
        return [(r["id"], r["state"], r["host"], r["pipeline_id"])
                for r in self.conn.execute("SELECT * FROM build_queue ORDER BY id")]

    def test_the_hosts_file(self):
        self.assertEqual([(h["name"], h["arch"], h["runner"]) for h in self.hosts],
                         [("bits", "x86_64", "bits"), ("pc", "x86_64", "pc-runner"), ("arm", "", "arm")])
        self.assertEqual(bq.parse_hosts("hosts:\n  - {name: x, tag: other}\n"), [])
        self.assertEqual(bq.parse_hosts(""), [])

    def test_one_build_per_host_oldest_first(self):
        a, b, c = self._queue(_build(pkg="A"), _build(pkg="B"), _build(pkg="C"))
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 2)
        self.assertEqual(self._states(), [(a, "dispatched", "bits", 101), (b, "dispatched", "pc", 102),
                                          (c, "queued", "", None)])
        started = dict(self.forge.started)
        self.assertEqual((started[101]["BITS_RUNNER_TAG"], started[101][bq.ENTRY_VAR]),
                         ("bits-host-bits", bq.get(self.conn, a)["token"]))
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)   # not recorded yet
        self._pipeline(101, "running")
        self._pipeline(102, "running")
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self._pipeline(102, "failed")                       # pc is free
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)
        self.assertEqual(self._states()[2], (c, "dispatched", "pc", 103))
        self.assertEqual([h["busy"] for h in bq.host_status(self.conn)], [True, False, False])

    def test_a_pinned_build_waits_for_its_host_only(self):
        a, b = self._queue(_build("bits-host-bits", pkg="A"), _build("bits-host-bits", pkg="B"))
        c, = self._queue(_build(pkg="C"))
        bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertEqual([s[1:3] for s in self._states()],
                         [("dispatched", "bits"), ("queued", ""), ("dispatched", "pc")])

    def test_a_pipeline_waiting_for_a_runner_keeps_a_host_free(self):
        self._pipeline(1, tag="bits-build-x86_64")          # it will take bits or pc
        self._queue(_build("bits-host-bits", pkg="A"), _build("bits-host-pc", pkg="B"))
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)
        self._queue(_build("bits-host-arm", pkg="C"))       # another arch (none): starts
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)

    def test_pipelines_from_outside_the_queue_hold_hosts(self):
        self._pipeline(1, tag="bits-host-bits")                          # pinned
        self._pipeline(2, tag="bits-build-x86_64",
                       jobs=[{"status": "running", "runner": "pc-runner"}])   # on pc's runner
        self._pipeline(3, outcome="success", tag="bits-host-bits")
        self._queue(_build())
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self._pipeline(2, outcome="success")
        self._pipeline(4, tag="bits-build-x86_64")                        # not on a runner yet
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self._pipeline(1, outcome="canceled")
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)

    def test_builds_no_host_can_take_are_refused(self):
        a, b, c = self._queue(_build("bits-host-gone"), _build("bits-build-aarch64"),
                              _build("bits-host-arm"))
        bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertEqual([s[1] for s in self._states()], ["refused", "refused", "dispatched"])
        with self.assertRaises(ValueError):
            bq.check_target([{"key": "BITS_RUNNER_TAG", "value": "self-hosted"}])
        with self.assertRaises(ValueError):
            bq.check_target([{"key": "BITS_RUNNER_TAG", "value": "bits-host-"}])
        self.assertEqual(bq.check_target([{"key": "BITS_RUNNER_TAG", "value": "bits-host-a"}]),
                         "bits-host-a")
        with self.assertRaises(RuntimeError):
            self._queue(_build())
            bq.dispatch(self.conn, self.forge, [])

    def test_gitlab_refusing(self):
        a, b = self._queue(_build(pkg="A"), _build(pkg="B"))
        # The job limit: back in its place, not counted, the pass ends.
        self.forge.fail = forge_ops.ForgeError(400, "Project exceeded the allowed number of jobs")
        with self.assertRaises(bq.Stop):
            bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertEqual([(s[1], s[2]) for s in self._states()], [("queued", ""), ("queued", "")])
        self.assertEqual(bq.get(self.conn, a)["attempts"], 0)
        # Paused: nothing is tried for a while.
        with self.assertRaises(bq.Stop):
            bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertEqual(self.forge.started, [])
        self._unpause()
        # Created failed, with no jobs: the same, and the dead pipeline goes.
        self.forge.fail = {"id": 7, "status": "failed"}
        with self.assertRaises(bq.Stop):
            bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertEqual((bq.get(self.conn, a)["state"], self.forge.deleted), ("queued", [7]))
        self._unpause()
        # Refused for this build: counted, the others go on; refused in the end.
        for n in range(bq.MAX_ATTEMPTS):
            self.forge.fail = forge_ops.ForgeError(400, "variables are invalid")
            bq.dispatch(self.conn, self.forge, self.hosts)
            with db.transaction(self.conn):
                self.conn.execute("UPDATE build_queue SET state = 'canceled' WHERE id = ?"
                                  " AND state = 'dispatched'", (b,))
        r = bq.get(self.conn, a)
        self.assertEqual((r["state"], r["attempts"]), ("refused", bq.MAX_ATTEMPTS))
        self.assertIn("variables are invalid", r["reason"])

    def test_gitlabs_answer_lost(self):
        a, = self._queue(_build(pkg="A"))
        self.forge.fail = forge_ops.ForgeError(0, "timed out")
        with self.assertRaises(bq.Stop):
            bq.dispatch(self.conn, self.forge, self.hosts)
        r = bq.get(self.conn, a)
        self.assertEqual((r["state"], r["host"]), ("dispatching", "bits"))
        # It was created after all: recorded with this attempt's token, adopted.
        self._pipeline(7, token="an older attempt's")
        self._pipeline(8, token=r["token"])
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self.assertEqual(self._states(), [(a, "dispatched", "bits", 8)])
        self.assertEqual(self.forge.started, [])

    def test_a_start_the_worker_did_not_finish(self):
        a, b = self._queue(_build(pkg="A"), _build(pkg="B"))
        with db.transaction(self.conn):
            self.conn.execute("UPDATE build_queue SET state = 'dispatching', host = 'bits',"
                              " updated_ms = 0, token = 't' || id")
        self._pipeline(55, token="t%d" % a)
        # A's pipeline is adopted (on bits); B never started: queued again, then on pc.
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)
        self.assertEqual([s[1:] for s in self._states()],
                         [("dispatched", "bits", 55), ("dispatched", "pc", 101)])
        self.assertEqual(bq.get(self.conn, b)["attempts"], 1)

    def test_cancel_and_listing(self):
        a, b = self._queue(_build(pkg="A"), _build(pkg="B"))
        self.assertTrue(bq.cancel(self.conn, b, "alice"))
        self.assertFalse(bq.cancel(self.conn, b, "alice"))
        bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertFalse(bq.cancel(self.conn, a, "alice"))
        c, d = self._queue(_build("bits-host-bits", pkg="C"), _build("bits-host-bits", pkg="D"))
        rows = {r["id"]: r for r in bq.listing(self.conn)}
        self.assertEqual((rows[c]["ahead"], rows[d]["ahead"], rows[a]["state"], rows[a]["package"]),
                         (0, 1, "dispatched", "A"))
        self.assertEqual(bq.listing(self.conn, communities={"atlas"}), [])


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, main, "settings", main.settings)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "orch.db")
        c = db.connect(path)
        db.migrate(c)
        self.conn = c
        self.addCleanup(c.close)
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4", "BITS_ORCH_DB": path, "BITS_ORCH_QUEUE": "1",
            "BITS_ADMINS_POLICY": "lcg @alice\natlas @bob\n* @carol"})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def _post(self, user, path, body):
        return self.client.post(path, json=body, headers={"Authorization": "Bearer " + user})

    def _get(self, user, path):
        return self.client.get(path, headers={"Authorization": "Bearer " + user})

    def _body(self, **more):
        name, variables = _build(**more)
        return {"community": "lcg", "builds": [{"name": name, "variables": variables}]}

    def test_queue_list_cancel(self):
        self.assertEqual(self._post("bob", "/ops/queue", self._body()).status_code, 403)
        r = self._post("alice", "/ops/queue", self._body(BITS_CERTIFIER="mallory"))
        self.assertEqual(r.status_code, 200, r.text)
        eid, = r.json()["entries"]
        v = {x["key"]: x["value"] for x in json.loads(bq.get(self.conn, eid)["variables"])}
        self.assertEqual(v["BITS_CERTIFIER"], "alice")
        listed = self._get("alice", "/ops/queue").json()
        self.assertEqual([(e["id"], e["ahead"]) for e in listed["entries"]], [(eid, 0)])
        self.assertEqual(self._get("bob", "/ops/queue").json()["entries"], [])
        self.assertEqual(self._post("bob", "/ops/queue/%d/cancel" % eid, {}).status_code, 403)
        self.assertEqual(self._post("carol", "/ops/queue/%d/cancel" % eid, {}).status_code, 200)
        self.assertEqual(self._post("alice", "/ops/queue/%d/cancel" % eid, {}).status_code, 409)
        self.assertTrue(self.client.get("/healthz").json()["build_queue"])
        dup = self._body()
        dup["builds"][0]["variables"].append({"key": "PACKAGE", "value": "other"})
        self.assertEqual(self._post("alice", "/ops/queue", dup).status_code, 400)

    def test_ops_build_leaves_builds_to_the_queue(self):
        main.settings.forge_ops_token, main.settings.forge_project = "t", "1"
        name, variables = _build()
        r = self._post("alice", "/ops/build", {"community": "lcg", "variables": variables})
        self.assertEqual(r.status_code, 409, r.text)

    def test_requests_are_checked(self):
        for body in (self._body(tag="self-hosted"), self._body(pkg=""),
                     {"community": "lcg", "builds": []}, dict(self._body(), ref="evil"),
                     dict(self._body(), community="atlas")):
            self.assertEqual(self._post("carol", "/ops/queue", body).status_code, 400, body)
        main.settings.orch_queue = False
        self.assertEqual(self._post("alice", "/ops/queue", self._body()).status_code, 503)


if __name__ == "__main__":
    unittest.main()
