# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""B1 skeleton tests: health endpoint + bits_helpers wiring."""

import unittest

from fastapi.testclient import TestClient

from console_backend.main import app


class TestHealth(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_healthz_ok(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        # bits_helpers must import in the test env (backend reuses it in B2/B3).
        self.assertTrue(body["bits_helpers"])
        self.assertIn("sign_proxy_configured", body)

    def test_no_secret_in_health(self):
        # The health response must never leak the gate token or key material.
        text = self.client.get("/healthz").text.lower()
        self.assertNotIn("token", text)
        self.assertNotIn("secret", text)


if __name__ == "__main__":
    unittest.main()
