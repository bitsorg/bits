# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""B2 tests: server-side OAuth login/callback/session, without a real GitLab.

The token exchange and gitlab_identify are patched; we assert the flow logic —
PKCE+state on /login, single-use state, a server-side session cookie, /me
gated by it, and that the access token never reaches the client.
"""

import unittest
import urllib.parse
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, main


def _configure():
    # Point the app at a fake, fully-configured OIDC + non-secure cookie for http.
    main.settings = config.Settings(env={
        "GITLAB_API_URL": "https://gitlab.example/api/v4",
        "BITS_OIDC_AUTHORIZE_URL": "https://gitlab.example/oauth/authorize",
        "BITS_OIDC_TOKEN_URL": "https://gitlab.example/oauth/token",
        "BITS_CONSOLE_OIDC_CLIENT_ID": "cid",
        "BITS_OIDC_REDIRECT_URI": "https://console.example/oauth-callback",
        "BITS_SESSION_COOKIE_SECURE": "0",
    })
    main.sessions = main.session.SessionStore(60)
    main.login_states = main.session.LoginStateStore()


class TestAuthFlow(unittest.TestCase):
    def setUp(self):
        _configure()
        self.client = TestClient(main.app, follow_redirects=False)

    def _login_state(self):
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 302)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
        self.assertEqual(q["code_challenge_method"], ["S256"])
        self.assertIn("code_challenge", q)
        return q["state"][0]

    def test_login_redirects_with_pkce_and_state(self):
        state = self._login_state()
        self.assertTrue(state)

    def test_callback_establishes_session_and_me_works(self):
        state = self._login_state()
        with patch.object(main.oauth, "exchange_code", return_value="gl-access-tok"), \
             patch.object(main.forge, "gitlab_identify", return_value="alice"):
            r = self.client.get("/oauth-callback",
                                params={"code": "c", "state": state})
        self.assertEqual(r.status_code, 302)
        self.assertIn("bits_session", r.cookies)
        sid = r.cookies["bits_session"]
        # The access token must NOT be exposed to the client anywhere.
        self.assertNotIn("gl-access-tok", r.headers.get("set-cookie", ""))
        me = self.client.get("/me", cookies={"bits_session": sid})
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json(), {"user": "alice"})
        self.assertNotIn("gl-access-tok", me.text)

    def test_me_without_session_is_401(self):
        self.assertEqual(self.client.get("/me").status_code, 401)

    def test_bad_state_rejected(self):
        with patch.object(main.oauth, "exchange_code", return_value="x"), \
             patch.object(main.forge, "gitlab_identify", return_value="alice"):
            r = self.client.get("/oauth-callback",
                                params={"code": "c", "state": "forged"})
        self.assertEqual(r.status_code, 400)

    def test_state_is_single_use(self):
        state = self._login_state()
        with patch.object(main.oauth, "exchange_code", return_value="x"), \
             patch.object(main.forge, "gitlab_identify", return_value="alice"):
            first = self.client.get("/oauth-callback",
                                    params={"code": "c", "state": state})
            second = self.client.get("/oauth-callback",
                                     params={"code": "c", "state": state})
        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 400)   # state consumed

    def test_login_csrf_state_must_be_bound_to_browser(self):
        # Attacker's browser (A) starts a login and captures a live state.
        a = TestClient(main.app, follow_redirects=False)
        loc = a.get("/login").headers["location"]
        state = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query)["state"][0]
        # Victim's browser (B) has no matching state cookie -> must be rejected,
        # so the attacker cannot log the victim in under the attacker's identity.
        b = TestClient(main.app, follow_redirects=False)
        with patch.object(main.oauth, "exchange_code", return_value="x"), \
             patch.object(main.forge, "gitlab_identify", return_value="attacker"):
            r = b.get("/oauth-callback", params={"code": "c", "state": state})
        self.assertEqual(r.status_code, 400)

    def test_callback_error_param_rejected(self):
        r = self.client.get("/oauth-callback",
                            params={"error": "access_denied", "state": "x"})
        self.assertEqual(r.status_code, 400)

    def test_callback_missing_params_rejected(self):
        self.assertEqual(self.client.get("/oauth-callback").status_code, 400)

    def test_login_state_store_is_bounded(self):
        st = main.session.LoginStateStore(ttl_seconds=600, max_entries=5)
        for i in range(50):
            st.put("s%d" % i, "v")
        self.assertLessEqual(len(st._store), 5)

    def test_session_store_is_bounded(self):
        ss = main.session.SessionStore(ttl_seconds=600, max_entries=5)
        for i in range(50):
            ss.create({"user": "u%d" % i})
        self.assertLessEqual(len(ss._store), 5)

    def test_logout_clears_session(self):
        state = self._login_state()
        with patch.object(main.oauth, "exchange_code", return_value="x"), \
             patch.object(main.forge, "gitlab_identify", return_value="bob"):
            r = self.client.get("/oauth-callback", params={"code": "c", "state": state})
        sid = r.cookies["bits_session"]
        self.client.post("/logout", cookies={"bits_session": sid})
        self.assertEqual(self.client.get("/me", cookies={"bits_session": sid}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
