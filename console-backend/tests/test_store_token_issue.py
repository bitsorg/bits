# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""POST /store/token: a community admin gets a store token for their group, signed
with the configured key; nobody else does."""

import base64
import hashlib
import unittest
import unittest.mock

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from console_backend import config, main
from console_backend.store_token import admin_capabilities

SEED = hashlib.sha256(b"bits-store test key 1").digest()
ENV = {"GITLAB_API_URL": "https://gl/api/v4", "BITS_ADMINS_POLICY": "lcg @alice\n* @carol",
       "BITS_STORE_TOKEN_KEY": base64.urlsafe_b64encode(SEED).rstrip(b"=").decode(),
       "BITS_STORE_TOKEN_KID": "console-1", "BITS_STORE_TOKEN_ISSUER": "https://bits.example.org",
       "BITS_STORE_TOKEN_AUDIENCE": "example-site/store", "BITS_STORE_TOKEN_TTL": "3600"}


def _configure(**over):
    main.settings = config.Settings(env=dict(ENV, **over))
    # the bearer string is the username (no GitLab)
    main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None


class StoreTokenIssueTest(unittest.TestCase):
    def setUp(self):
        _configure()
        self.client = TestClient(main.app)

    def _post(self, user, body):
        hdr = {"Authorization": "Bearer %s" % user} if user else {}
        return self.client.post("/store/token", headers=hdr, json=body)

    def test_admin_gets_a_token(self):
        r = self._post("alice", {"group": "lcg"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        pub = Ed25519PrivateKey.from_private_bytes(SEED).public_key()
        c = jwt.decode(r.json()["token"], pub, algorithms=["EdDSA"],
                       audience="example-site/store", issuer="https://bits.example.org")
        self.assertEqual((c["sub"], c["grp"], c["exp"] - c["iat"]), ("alice", "lcg", 3600))
        self.assertEqual(c["cap"], [{"m": m, "p": p} for m, p in admin_capabilities("lcg")])
        self.assertEqual(jwt.get_unverified_header(r.json()["token"])["kid"], "console-1")

    def test_overall_admin_any_group(self):
        self.assertEqual(self._post("carol", {"group": "lhcb"}).status_code, 200)

    def test_refused(self):
        for user, body, code in ((None, {"group": "lcg"}, 401), ("bob", {"group": "lcg"}, 403),
                                 ("alice", {"group": "lhcb"}, 403), ("alice", {}, 400),
                                 ("alice", {"group": "LCG/../x"}, 400), ("alice", {"group": 5}, 400)):
            self.assertEqual(self._post(user, body).status_code, code, (user, body))
        r = self.client.post("/store/token", headers={"Authorization": "Bearer alice"},
                             content=b'{"group": "lcg", "x": "' + b"y" * 5000 + b'"}')
        self.assertEqual(r.status_code, 413)

    def test_not_configured(self):
        _configure(BITS_STORE_TOKEN_KEY="")
        self.assertEqual(self._post("alice", {"group": "lcg"}).status_code, 503)
        _configure(BITS_STORE_TOKEN_KEY="not-a-seed")
        self.assertEqual(self._post("alice", {"group": "lcg"}).status_code, 503)
        with self.assertRaises(ValueError):
            _configure(BITS_STORE_TOKEN_TTL="30")

    def test_signed_by_the_security_proxy(self):
        from bits_helpers import trust
        key, seen = Ed25519PrivateKey.from_private_bytes(SEED), []

        def call(settings, fn, data, route=None, prefix=None, agent_socket=None, host=None):
            seen.append((fn, route, prefix, agent_socket, host))
            return trust.sign_bytes(data, key)               # what the proxy's route returns
        _configure(BITS_STORE_TOKEN_KEY="", BITS_STORE_TOKEN_SIGN_ROUTE="bits-store-token-sign",
                   BITS_STORE_TOKEN_SIGN_SOCKET="/run/store-token-signer/agent.sock",
                   BITS_STORE_TOKEN_SIGN_HOST="store-token-signer")
        with unittest.mock.patch.object(main.signproxy, "call", call):
            r = self._post("alice", {"group": "lcg"})
        self.assertEqual(r.status_code, 200, r.text)
        jwt.decode(r.json()["token"], key.public_key(), algorithms=["EdDSA"],
                   audience="example-site/store", issuer="https://bits.example.org")
        self.assertEqual(seen, [(trust.sign_bytes_via_proxy, "bits-store-token-sign", "/sign/store",
                                 "/run/store-token-signer/agent.sock", "store-token-signer")])

        def down(*a, **kw):
            raise RuntimeError("sign proxy unreachable")
        with unittest.mock.patch.object(main.signproxy, "call", down):
            self.assertEqual(self._post("alice", {"group": "lcg"}).status_code, 502)
        for odd in ({"sig": "not base64!"}, {"sig": "AAAA"}, {}):
            with unittest.mock.patch.object(main.signproxy, "call", lambda *a, **kw: odd):
                self.assertEqual(self._post("alice", {"group": "lcg"}).status_code, 502, odd)
        _configure(BITS_STORE_TOKEN_KEY="", BITS_STORE_TOKEN_SIGN_ROUTE="bits-store-token-sign")
        self.assertEqual(self._post("alice", {"group": "lcg"}).status_code, 503)   # no agent socket

    def test_capabilities(self):
        self.assertEqual(admin_capabilities("lcg"), [                     # as bits writes the store
            ("GET", "/store/**"), ("PUT", "/store/**"),
            ("GET", "/MANIFESTS/rev-index/**"), ("PUT", "/MANIFESTS/rev-index/**"),
            ("GET", "/manifests/lcg/**"), ("PUT", "/manifests/lcg/**"), ("GET", "/sources/**")])
        for g in ("", "LCG", "a/b", "..", "-x", "x" * 65):
            with self.assertRaises(ValueError, msg=g):
                admin_capabilities(g)


if __name__ == "__main__":
    unittest.main()
