# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 2.1: backend OIDC login + session. The backend runs the OIDC code flow and
issues its OWN session; the browser then holds no GitLab token."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, main, session

FRONTEND = "https://bits-console.web.cern.ch"


def _configure():
    main.settings = config.Settings(env={
        "BITS_OIDC_LOGIN_CLIENT_ID": "cid",
        "BITS_OIDC_LOGIN_CLIENT_SECRET": "secret",
        "BITS_OIDC_LOGIN_REDIRECT": "https://bits.cern.ch/auth/callback",
        "BITS_OIDC_TOKEN_URL": "https://gl/oauth/token",
        "BITS_OIDC_AUTHORIZE_URL": "https://gl/oauth/authorize",
        "BITS_OIDC_ISSUER": "https://gl",
        "BITS_OIDC_JWKS_URL": "https://gl/jwks",
        "BITS_LOGIN_RETURN_ALLOW": FRONTEND,
        "BITS_SESSION_TTL": "3600",
        "BITS_ADMINS_POLICY": "testbed @alice",
    })
    main.oidc_states = session.OidcStateStore()
    main.sessions = session.SessionStore(ttl_seconds=3600)


class TestOidcLogin(unittest.TestCase):
    def setUp(self):
        _configure()
        self.client = TestClient(main.app, follow_redirects=False)

    def test_login_redirects_to_authorize(self):
        r = self.client.get("/auth/login", params={"return_to": FRONTEND})
        self.assertEqual(r.status_code, 302)
        loc = r.headers["location"]
        self.assertTrue(loc.startswith("https://gl/oauth/authorize?"))
        self.assertIn("code_challenge_method=S256", loc)
        self.assertIn("state=", loc)
        self.assertIn("max_age=", loc)   # P2.3: force re-auth once SSO session > TTL
        # browser-binding cookie is set (login-CSRF defense)
        self.assertIn("bits_login_state=", r.headers.get("set-cookie", ""))

    def test_login_rejects_foreign_return(self):
        r = self.client.get("/auth/login", params={"return_to": "https://evil.example"})
        self.assertEqual(r.status_code, 400)

    def test_login_503_when_unconfigured(self):
        main.settings = config.Settings(env={})
        self.assertEqual(self.client.get("/auth/login").status_code, 503)

    def test_callback_bad_state_400(self):
        r = self.client.get("/auth/callback", params={"code": "x", "state": "nope"})
        self.assertEqual(r.status_code, 400)

    @patch("console_backend.auth_oidc.verify_id_token")
    @patch("console_backend.auth_oidc.exchange_code")
    def test_callback_issues_session(self, exch, verify):
        exch.return_value = {"id_token": "id.jwt.tok", "access_token": "at"}
        verify.return_value = {"preferred_username": "alice",
                               "groups_direct": ["testbed", "alice"]}
        # seed a valid state as /auth/login would
        main.oidc_states.put("st1", {"return": FRONTEND, "verifier": "v"})
        r = self.client.get("/auth/callback", params={"code": "c", "state": "st1"},
                            cookies={"bits_login_state": "st1"})
        self.assertEqual(r.status_code, 302)
        loc = r.headers["location"]
        self.assertIn(FRONTEND + "#bits_session=", loc)
        token = loc.split("bits_session=", 1)[1]
        sess = main.sessions.get(token)
        self.assertEqual(sess["user"], "alice")
        self.assertIn("testbed", sess["groups"])
        # state is single-use
        self.assertIsNone(main.oidc_states.pop("st1"))

    def test_callback_requires_browser_cookie(self):
        # login-CSRF: a valid state param with NO matching browser cookie is refused,
        # so an attacker can't feed a victim a pre-made callback link.
        main.oidc_states.put("st2", {"return": FRONTEND, "verifier": "v"})
        r = self.client.get("/auth/callback", params={"code": "c", "state": "st2"})
        self.assertEqual(r.status_code, 400)

    def test_session_bearer_identifies_user(self):
        main.settings.__dict__  # ensure configured
        main.sessions.put("sesstok", "alice", ["testbed"])
        main.identity.verify_gitlab_token = lambda *a, **k: None  # not a GitLab token
        r = self.client.get("/me", headers={"Authorization": "Bearer sesstok"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["user"], "alice")

    def test_endpoints_discovered_from_issuer(self):
        from console_backend import auth_oidc
        s = config.Settings(env={
            "BITS_OIDC_LOGIN_CLIENT_ID": "cid",
            "BITS_OIDC_LOGIN_REDIRECT": "https://bits.cern.ch/auth/callback",
            "BITS_OIDC_ISSUER": "https://gl"})   # no explicit authorize/token/jwks URLs
        self.assertTrue(s.oidc_login_configured())
        with patch.object(auth_oidc, "_discover", return_value={
                "authorization_endpoint": "https://gl/oauth/authorize",
                "token_endpoint": "https://gl/oauth/token",
                "jwks_uri": "https://gl/oauth/discovery/keys"}):
            self.assertEqual(auth_oidc.endpoints(s),
                             ("https://gl/oauth/authorize", "https://gl/oauth/token",
                              "https://gl/oauth/discovery/keys"))

    def test_logout_revokes_session(self):
        main.sessions.put("sesstok", "alice", ["testbed"])
        r = self.client.post("/auth/logout", headers={"Authorization": "Bearer sesstok"})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(main.sessions.get("sesstok"))


if __name__ == "__main__":
    unittest.main()
