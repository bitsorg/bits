# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""M8b: once the orchestrator owns the set, the backend signs nothing."""

import unittest

from fastapi.testclient import TestClient

from console_backend import config, main


class SignRetiredTest(unittest.TestCase):
    def setUp(self):
        self.old = main.settings
        self.addCleanup(setattr, main, "settings", self.old)
        self.client = TestClient(main.app, follow_redirects=False)

    def test_the_sign_routes_are_gone_once_the_orchestrator_owns_the_set(self):
        main.settings = config.Settings(env={"BITS_ORCH_OWNS_SET": "1"})
        for method, path in (("post", "/sign"), ("post", "/sign/preapproved?build_id=1"),
                             ("post", "/sign/cli/request"), ("get", "/sign/cli/x/result")):
            r = getattr(self.client, method)(path)
            self.assertEqual(r.status_code, 410, path)
        self.assertNotEqual(self.client.get("/healthz").status_code, 410)

    def test_before_the_switch_they_answer_as_before(self):
        main.settings = config.Settings(env={})
        self.assertNotEqual(self.client.post("/sign/preapproved?build_id=1").status_code, 410)
