# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Auth tests: the backend identifies a caller from the GitLab bearer token the
bits-console SPA already holds (no server-side OAuth/session), and CORS allows the
configured Pages frontend origin.
"""

import unittest

from fastapi.testclient import TestClient

from console_backend import config, main

FRONT = "https://bits-console.web.cern.ch"


def _configure(identity_ok=True):
    main.settings = config.Settings(env={
        "GITLAB_API_URL": "https://gitlab.example/api/v4",
        "BITS_ADMINS_POLICY": "* @root\nlcg @alice",
        "BITS_FRONTEND_ORIGIN": FRONT,
    })
    # Test double: the bearer token IS the username (no GitLab round-trip). When
    # identity_ok is False, every token is rejected (simulates an invalid token).
    main.identity.verify_gitlab_token = (
        (lambda api, tok, *a, **k: tok or None) if identity_ok
        else (lambda api, tok, *a, **k: None))


class TestBearerAuth(unittest.TestCase):
    def setUp(self):
        _configure()
        self.client = TestClient(main.app, follow_redirects=False)

    def _bearer(self, user):
        return {"Authorization": "Bearer %s" % user}

    def test_me_requires_a_bearer(self):
        self.assertEqual(self.client.get("/me").status_code, 401)

    def test_me_with_valid_bearer(self):
        r = self.client.get("/me", headers=self._bearer("alice"))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["user"], "alice")
        self.assertEqual(body["admin_groups"], ["lcg"])
        self.assertFalse(body["overall_admin"])

    def test_me_overall_admin(self):
        r = self.client.get("/me", headers=self._bearer("root"))
        self.assertTrue(r.json()["overall_admin"])

    def test_invalid_token_is_401(self):
        _configure(identity_ok=False)
        self.assertEqual(
            self.client.get("/me", headers=self._bearer("whoever")).status_code, 401)

    def test_malformed_authorization_header_401(self):
        self.assertEqual(
            self.client.get("/me", headers={"Authorization": "Basic x"}).status_code, 401)

    def test_no_access_token_stored_or_returned(self):
        # /me must not echo the bearer back to the caller anywhere.
        r = self.client.get("/me", headers=self._bearer("alice"))
        self.assertNotIn("Authorization", r.text)


class TestCORS(unittest.TestCase):
    def setUp(self):
        _configure()
        self.client = TestClient(main.app, follow_redirects=False)

    def test_preflight_from_frontend_is_allowed(self):
        r = self.client.options("/sign/request", headers={
            "Origin": FRONT,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers.get("access-control-allow-origin"), FRONT)
        self.assertIn("Authorization", r.headers.get("access-control-allow-headers", ""))

    def test_response_carries_allow_origin_for_frontend(self):
        r = self.client.get("/me", headers={
            "Origin": FRONT, "Authorization": "Bearer alice"})
        self.assertEqual(r.headers.get("access-control-allow-origin"), FRONT)

    def test_other_origin_gets_no_allow_header(self):
        r = self.client.get("/me", headers={
            "Origin": "https://evil.example", "Authorization": "Bearer alice"})
        self.assertIsNone(r.headers.get("access-control-allow-origin"))


if __name__ == "__main__":
    unittest.main()
