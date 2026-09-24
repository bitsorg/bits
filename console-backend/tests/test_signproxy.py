# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""signproxy: resolve the sign route from the security-proxy agent socket."""

import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, main, signproxy


class FakeAgent:
    """A UNIX-socket server answering like security-proxy's agent socket."""

    def __init__(self, path, port=40001, token="t1"):
        self.path, self.port, self.token, self.asked = path, port, token, []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                route = conn.makefile().readline().strip()
                self.asked.append(route)
                if route == "bits-manifest-sign":
                    resp = {"port": self.port, "host": "127.0.0.1",
                            "service": route, "token": self.token}
                else:
                    resp = {"error": "unknown service %r" % route}
                conn.sendall((json.dumps(resp) + "\n").encode())

    def close(self):
        self.srv.close()


class TestSignProxy(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sock = os.path.join(self.tmp, "agent.sock")
        self.agent = FakeAgent(self.sock)
        self.s = config.Settings(env={"BITS_SIGN_PROXY_AGENT_SOCKET": self.sock})
        signproxy._cache.clear()

    def tearDown(self):
        self.agent.close()
        signproxy._cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_endpoint_from_agent_and_cached(self):
        self.assertTrue(self.s.sign_proxy_configured())
        self.assertEqual(signproxy.endpoint(self.s),
                         ("http://security-proxy:40001/sign/bits", "t1"))
        self.agent.token = "t2"
        self.assertEqual(signproxy.endpoint(self.s)[1], "t1")      # cached
        self.assertEqual(signproxy.endpoint(self.s, refresh=True)[1], "t2")
        self.assertEqual(self.agent.asked, ["bits-manifest-sign"] * 2)

    def test_call_refreshes_and_retries_after_restart(self):
        signproxy.endpoint(self.s)                     # cache port 40001 / t1
        self.agent.port, self.agent.token = 40002, "t2"  # proxy restarted
        seen = []

        def fn(data, url, token):
            seen.append((url, token))
            if token != "t2":
                raise RuntimeError("sign proxy %s unreachable" % url)
            return "signed:" + data
        self.assertEqual(signproxy.call(self.s, fn, "x"), "signed:x")
        self.assertEqual(seen, [("http://security-proxy:40001/sign/bits", "t1"),
                                ("http://security-proxy:40002/sign/bits", "t2")])

    def test_call_does_not_retry_when_endpoint_unchanged(self):
        calls = []

        def fn(url, token):
            calls.append(url)
            raise RuntimeError("HTTP 500")
        with self.assertRaises(RuntimeError):
            signproxy.call(self.s, fn)
        self.assertEqual(len(calls), 1)

    def test_cache_expires(self):
        signproxy.endpoint(self.s)
        self.agent.token = "t2"
        later = signproxy._now() + signproxy.CACHE_SECONDS + 1
        with patch.object(signproxy, "_now", return_value=later):
            self.assertEqual(signproxy.endpoint(self.s)[1], "t2")

    def test_oserror_also_triggers_refresh(self):
        signproxy.endpoint(self.s)
        self.agent.token = "t2"

        def fn(url, token):
            if token == "t1":
                raise TimeoutError("read timed out")
            return token
        self.assertEqual(signproxy.call(self.s, fn), "t2")

    def test_refresh_failure_is_reported(self):
        signproxy.endpoint(self.s)
        self.agent.close()
        os.unlink(self.sock)                       # proxy gone entirely

        def fn(url, token):
            raise RuntimeError("unreachable")
        with self.assertRaisesRegex(RuntimeError, "agent socket"):
            signproxy.call(self.s, fn)

    def test_sign_endpoint_retries_through_http_layer(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from bits_helpers import trust
        priv = Ed25519PrivateKey.generate()
        signproxy.endpoint(self.s)                  # cached t1
        self.agent.token = "t2"                     # rotated
        seen = []

        def fake_sign(data, url, tok):
            seen.append(tok)
            if tok != "t2":
                raise RuntimeError("sign proxy %s failed: HTTP 401 Unauthorized" % url)
            return trust.sign_bytes(data, priv)
        st = config.Settings(env={"BITS_SIGN_PROXY_AGENT_SOCKET": self.sock,
                                  "GITLAB_API_URL": "https://gitlab.example/api/v4",
                                  "BITS_ADMINS_POLICY": "lcg @alice",
                                  "BITS_SESSION_COOKIE_SECURE": "0"})
        body = json.dumps({"architecture": "a", "packages": [
            {"package": "A", "hash": "h1", "group": "lcg"}]}).encode()
        with patch.object(main, "settings", st), \
                patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None), \
                patch.object(main.trust, "sign_bytes_via_proxy", fake_sign), \
                patch.object(main.trust, "proxy_pubkey",
                             lambda url, tok: (trust.key_id(priv.public_key()), priv.public_key())), \
                patch.object(main.trust, "load_key_policy", return_value=None):
            r = TestClient(main.app).post("/sign", content=body,
                                          headers={"Authorization": "Bearer alice"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, ["t1", "t2"])

    def test_agent_errors_are_runtime_errors(self):
        bad = config.Settings(env={"BITS_SIGN_PROXY_AGENT_SOCKET": self.sock,
                                   "BITS_SIGN_PROXY_ROUTE": "nope"})
        with self.assertRaisesRegex(RuntimeError, "no token for route 'nope'"):
            signproxy.endpoint(bad)
        gone = config.Settings(env={"BITS_SIGN_PROXY_AGENT_SOCKET":
                                    os.path.join(self.tmp, "missing.sock")})
        with self.assertRaisesRegex(RuntimeError, "agent socket"):
            signproxy.endpoint(gone)

    def test_static_fallback_and_unavailable(self):
        st = config.Settings(env={"BITS_SIGN_PROXY_URL": "http://p/sign/bits"})
        with patch.dict(os.environ, {"BITS_SIGN_PROXY_TOKEN": "gate"}):
            self.assertEqual(signproxy.endpoint(st), ("http://p/sign/bits", "gate"))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BITS_SIGN_PROXY_TOKEN", None)
            with self.assertRaisesRegex(signproxy.Unavailable, "token"):
                signproxy.check(st)
        with self.assertRaisesRegex(signproxy.Unavailable, "not configured"):
            signproxy.check(config.Settings(env={}))

    def test_trust_pubkey_endpoint_uses_agent(self):
        main._pubkey_cache.update(kid=None, exp=0.0)
        got = []

        def fake_pubkey(url, token):
            got.append((url, token))
            return "kid16", None
        with patch.object(main, "settings", self.s), \
                patch.object(main.trust, "proxy_pubkey", fake_pubkey):
            r = TestClient(main.app).get("/trust/pubkey")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"key_id": "kid16"})
        self.assertEqual(got, [("http://security-proxy:40001/sign/bits", "t1")])
        main._pubkey_cache.update(kid=None, exp=0.0)

    def test_agent_failure_maps_to_502(self):
        gone = config.Settings(env={"BITS_SIGN_PROXY_AGENT_SOCKET":
                                    os.path.join(self.tmp, "missing.sock")})
        main._pubkey_cache.update(kid=None, exp=0.0)
        with patch.object(main, "settings", gone):
            r = TestClient(main.app).get("/trust/pubkey")
        self.assertEqual(r.status_code, 502)
        self.assertIn("agent socket", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
