# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""What an overall admin does to a build host, as actions (orch/hosts.py)."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, forge_ops, main
from console_backend.orch import actions, db, events, kinds

CAROL = events.Actor("person", "carol")


class FakeForge:
    def __init__(self, runners=None, fail=None):
        self.runners = runners if runners is not None else [
            {"id": 1, "paused": False, "runner_type": "project_type", "tags": ["bits-host-a"]},
            {"id": 2, "paused": True, "runner_type": "project_type", "tags": ["bits-host-a"]},
            {"id": 3, "paused": False, "runner_type": "instance_type", "is_shared": True,
             "tags": ["bits-host-a"]},
            {"id": 4, "paused": False, "runner_type": "project_type", "tags": ["bits-host-b"]}]
        self.fail = fail
        self.calls = []

    def list_runners(self, tag=""):
        return [r for r in self.runners if tag in r["tags"]]

    def set_runner_paused(self, rid, paused):
        if self.fail:
            raise self.fail
        self.calls.append(("paused", rid, paused))

    def trigger_pipeline(self, ref, variables=None, name=None):
        if self.fail:
            raise self.fail
        self.calls.append(("pipeline", ref, {v["key"]: v["value"] for v in variables}))
        return {"id": 77, "web_url": "https://gl/p/77", "status": "created"}


class _DB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "orch.db")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)


class HostActionsTest(_DB):
    def setUp(self):
        super().setUp()
        self.forge = FakeForge()
        self.deps = kinds.Deps(config.Settings(env={"BITS_FORGE_REFS": "main,next"}))
        self.deps.forge = lambda: self.forge

    def _run(self, kind, host="a"):
        row, _ = actions.request(self.conn, kind, {"host": host}, CAROL, "", True)
        actions.run_due(self.conn, self.deps, lambda a, c: True)
        return actions.get(self.conn, row["id"])

    def test_pause_and_resume_change_only_the_hosts_own_runners(self):
        done = self._run("host.pause")
        self.assertEqual((done["state"], done["result"]),
                         ("done", {"runners": [1, 2], "changed": [1]}))
        self.assertEqual(self.forge.calls, [("paused", 1, True)])
        self.forge.calls.clear()
        self.assertEqual(self._run("host.resume")["result"]["changed"], [2])
        self.assertEqual(self.forge.calls, [("paused", 2, False)])

    def test_a_host_with_no_runner_fails_and_changes_nothing(self):
        for kind in ("host.pause", "host.refresh"):
            done = self._run(kind, host="gone")
            self.assertEqual(done["state"], "failed")
            self.assertIn("bits-host-gone", done["error"])
        self.assertEqual(self.forge.calls, [])

    def test_refresh_is_a_pipeline_pinned_to_the_host(self):
        done = self._run("host.refresh")
        self.assertEqual(done["result"], {"pipeline": 77, "web_url": "https://gl/p/77"})
        self.assertEqual(self.forge.calls, [("pipeline", "main", {
            "REFRESH_IMAGES_JOB": "true", "BITS_RUNNER_TAG": "bits-host-a"})])

    def test_gitlab_down_is_retried_a_refusal_fails(self):
        self.forge.fail = forge_ops.ForgeError(502, "bad gateway")
        self.assertEqual(self._run("host.pause")["state"], "approved")     # again later
        self.forge.fail = forge_ops.ForgeError(403, "forbidden")
        row, _ = actions.request(self.conn, "host.resume", {"host": "b"}, CAROL, "", True)
        self.forge.runners[3]["paused"] = True
        actions.run_due(self.conn, self.deps, lambda a, c: True)
        done = actions.get(self.conn, row["id"])
        self.assertEqual(done["state"], "failed")
        self.assertIn("403", done["error"])

    def test_a_refusal_midway_says_what_was_changed_before_it(self):
        self.forge.runners[1]["paused"] = False
        calls = []

        def refuse_second(rid, paused):
            calls.append(rid)
            if len(calls) == 2:
                raise forge_ops.ForgeError(403, "forbidden")
        self.forge.set_runner_paused = refuse_second
        done = self._run("host.pause")
        self.assertEqual(done["state"], "failed")
        self.assertIn("runner 2: forge 403: forbidden; changed before it: [1]", done["error"])

    def test_a_host_name_is_checked(self):
        for bad in ("", "-a", "a b", "a/b", "a" * 64, "a;b"):
            with self.assertRaises(actions.ActionError, msg=bad):
                actions.request(self.conn, "host.pause", {"host": bad}, CAROL, "", True)


class SetRunnerPausedTest(unittest.TestCase):
    @patch("console_backend.forge_ops.requests.request")
    def test_puts_the_runner_outside_the_project(self, req):
        req.return_value = type("R", (), {"status_code": 200, "json": lambda s: {"id": 9}})()
        forge_ops.GitLabForge("https://gl/api/v4", "tok", "42").set_runner_paused(9, True)
        self.assertEqual(req.call_args.args[:2], ("PUT", "https://gl/api/v4/runners/9"))
        self.assertEqual(req.call_args.kwargs["json"], {"paused": True})


class HostApiTest(_DB):
    def setUp(self):
        super().setUp()
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4",
            "BITS_ADMINS_POLICY": "testbed @alice\n* @carol",
            "BITS_FORGE_OPS_TOKEN": "optok", "BITS_FORGE_PROJECT": "42",
            "BITS_ORCH_DB": self.path})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def _post(self, user, body):
        return self.client.post("/ops/actions", headers={"Authorization": "Bearer " + user},
                                json=body)

    def test_only_an_overall_admin_acts_on_a_host(self):
        body = {"kind": "host.pause", "params": {"host": "a"}}
        self.assertEqual(self._post("alice", body).status_code, 403)
        r = self._post("carol", body)
        self.assertEqual((r.status_code, r.json()["subject"], r.json()["community"]),
                         (201, "host:a", ""))
        self.assertEqual(self._post("carol", {"kind": "host.pause",
                                              "params": {"host": "a b"}}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
