# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker's failing steps (events.failing_steps) and GET /ops/worker."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, main
from console_backend.orch import db, events


def _ev(conn, kind, payload=None, subject="worker"):
    with db.transaction(conn):
        events.append(conn, kind, subject, events.WORKER, payload)


class WorkerStateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "orch.db")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)

    def test_a_step_fails_from_its_first_error_until_it_recovers_or_the_worker_restarts(self):
        _ev(self.conn, "worker.started")
        _ev(self.conn, "worker.step_failed", {"step": "scan", "error": "a"})
        first = events.failing_steps(self.conn)["scan"]["since_ms"]
        _ev(self.conn, "worker.step_failed", {"step": "scan", "error": "b"})
        _ev(self.conn, "worker.step_failed", {"step": "dispatch", "error": "c"})
        _ev(self.conn, "worker.step_failed", {"step": "x", "error": "y"}, subject="other")
        got = events.failing_steps(self.conn)
        self.assertEqual(sorted(got), ["dispatch", "scan"])
        self.assertEqual(got["scan"], {"error": "b", "since_ms": first})
        _ev(self.conn, "worker.step_recovered", {"step": "dispatch", "error": None})
        self.assertEqual(sorted(events.failing_steps(self.conn)), ["scan"])
        _ev(self.conn, "worker.started")
        self.assertEqual(events.failing_steps(self.conn), {})

    def test_the_api_shows_errors_to_overall_admins_only(self):
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4", "BITS_ORCH_DB": self.path,
            "BITS_ADMINS_POLICY": "testbed @alice\n* @carol"})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        client = TestClient(main.app)
        h = lambda u: {"Authorization": "Bearer " + u}
        _ev(self.conn, "worker.started")
        _ev(self.conn, "worker.step_failed", {"step": "scan", "error": "scanner: 503"})
        self.assertEqual(client.get("/ops/worker").status_code, 401)
        self.assertEqual(client.get("/ops/worker", headers=h("dave")).status_code, 403)
        self.assertEqual(client.get("/ops/worker?community=other", headers=h("alice")).status_code, 403)
        mine = client.get("/ops/worker?community=testbed", headers=h("alice")).json()
        self.assertEqual(([f["step"] for f in mine["failing"]], "error" in mine["failing"][0],
                          mine["last_pass_ms"]), (["scan"], False, None))
        open(self.path + ".heartbeat", "w").close()
        all_ = client.get("/ops/worker", headers=h("carol")).json()
        self.assertEqual(all_["failing"][0]["error"], "scanner: 503")
        self.assertIsInstance(all_["last_pass_ms"], int)
        self.assertEqual(all_["interval_s"], 30.0)


if __name__ == "__main__":
    unittest.main()
