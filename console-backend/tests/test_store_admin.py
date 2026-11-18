# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""GET /store-admin/<view>: bits-store's admin API for overall admins, each request
with a minute's admin token; bits-store's own answers passed on, its refusals 502."""

import base64
import hashlib
import unittest
from unittest import mock

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from console_backend import audit, config, main

USER_SEED = hashlib.sha256(b"bits-store test key 1").digest()
ADMIN_SEED = hashlib.sha256(b"bits-store test admin key").digest()   # bits-store's vectors' admin-1
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
ENV = {"GITLAB_API_URL": "https://gl/api/v4", "BITS_ADMINS_POLICY": "lcg @alice\n* @carol",
       "BITS_STORE_TOKEN_KEY": b64(USER_SEED), "BITS_STORE_TOKEN_KID": "console-1",
       "BITS_STORE_TOKEN_ISSUER": "https://bits.example.org", "BITS_STORE_TOKEN_AUDIENCE": "example-site/store",
       "BITS_STORE_ADMIN_URL": "http://bits-store:8099", "BITS_STORE_ADMIN_TOKEN_KEY": b64(ADMIN_SEED),
       "BITS_STORE_ADMIN_TOKEN_KID": "admin-1"}


class Answer:
    def __init__(self, code, body, text=""):
        self.status_code, self.body, self.text = code, body, text

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def _configure(**over):
    main.settings = config.Settings(env={k: v for k, v in dict(ENV, **over).items() if v is not None})
    main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None


class StoreAdminTest(unittest.TestCase):
    def setUp(self):
        _configure()
        self.client = TestClient(main.app)
        self.calls = []
        self.answer = Answer(200, {"volumes": []})

        def fake_get(session, url, params=None, headers=None, timeout=None, allow_redirects=True):
            self.calls.append((url, params, headers, allow_redirects, session.trust_env))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        p = mock.patch.object(requests.Session, "get", fake_get)
        p.start()
        self.addCleanup(p.stop)

        def fake_put(session, url, data=None, headers=None, timeout=None, allow_redirects=True):
            self.calls.append((url, data, headers, allow_redirects, session.trust_env))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        q = mock.patch.object(requests.Session, "put", fake_put)
        q.start()
        self.addCleanup(q.stop)
        self.audited = []
        a = mock.patch.object(audit, "record", lambda kind, **kw: self.audited.append((kind, kw)))
        a.start()
        self.addCleanup(a.stop)

    def get(self, user, path):
        return self.client.get(path, headers={"Authorization": "Bearer %s" % user} if user else {})

    def test_an_overall_admin_sees_the_store(self):
        r = self.get("carol", "/store-admin/objects?binding=%2Fstore&q=ROOT")
        self.assertEqual((r.status_code, r.json(), r.headers.get("cache-control")), (200, {"volumes": []}, "no-store"))
        url, params, headers, redirects, trust_env = self.calls[0]
        self.assertEqual((url, params), ("http://bits-store:8099/_admin/objects", [("binding", "/store"), ("q", "ROOT")]))
        self.assertFalse(redirects or trust_env)                 # no redirects, no proxy from the environment
        self.assertEqual(self.audited[-1][1]["view"], "objects")
        tok = headers["Authorization"].removeprefix("Bearer ")
        pub = Ed25519PrivateKey.from_private_bytes(ADMIN_SEED).public_key()
        c = jwt.decode(tok, pub, algorithms=["EdDSA"], audience="example-site/store", issuer="https://bits.example.org")
        self.assertEqual((c["sub"], c["exp"] - c["iat"], c["cap"]), ("admin:carol", 60, [{"m": "GET", "p": "/_admin/**"}]))
        self.assertEqual(jwt.get_unverified_header(tok)["kid"], "admin-1")

    def test_refused(self):
        for user, path, code in ((None, "/store-admin/summary", 401), ("alice", "/store-admin/summary", 403),
                                 ("bob", "/store-admin/summary", 403), ("carol", "/store-admin/upload", 404),
                                 ("carol", "/store-admin/objects?q=" + "x" * 3000, 400),
                                 ("carol", "/store-admin/objects?q=%zz", 400), ("carol", "/store-admin/objects?q=%ff", 400)):
            self.assertEqual(self.get(user, path).status_code, code, (user, path))
        self.assertEqual(self.calls, [])                         # bits-store never asked
        self.assertIn(("store_admin_denied", {"user": "alice", "view": "summary", "principal": "human"}), self.audited)
        self.assertEqual(main.store_admin.params("q=%C3%A9"), [("q", "\u00e9")])
        for bad in ("q=\u00e9", "q=%ff", "q=%z"):
            with self.assertRaises(ValueError, msg=bad):
                main.store_admin.params(bad)
        self.get("carol", "/store-admin/objects?q=%41%2Fb&kept=")
        self.assertEqual(self.calls[-1][1], [("q", "A/b"), ("kept", "")])
        _configure(BITS_STORE_ADMIN_URL=None)
        self.assertEqual(self.get("carol", "/store-admin/summary").status_code, 503)

    def test_the_stores_answers(self):
        self.answer = Answer(400, {"error": "limit"})
        r = self.get("carol", "/store-admin/objects?limit=0")
        self.assertEqual((r.status_code, r.json()), (400, {"error": "limit"}))
        for answer in (Answer(401, {"error": "x"}), Answer(403, {"error": "x"}), Answer(500, ValueError()),
                       Answer(200, ValueError()), Answer(200, [1]), requests.ConnectionError("down")):
            self.answer = answer
            self.assertEqual(self.get("carol", "/store-admin/summary").status_code, 502, answer)
        self.answer = Answer(404, ValueError())                  # bits-store's plain 404
        self.assertEqual(self.get("carol", "/store-admin/object?key=x").status_code, 404)

    def put(self, user, path, body):
        return self.client.put(path, content=body, headers={"Authorization": "Bearer %s" % user} if user else {})

    def test_an_overall_admin_acts_on_the_store(self):
        body = b'{"binding": "/restricted", "sha256": "sha256:ab", "why": "a licence"}'
        self.answer = Answer(200, {"added": True})
        r = self.put("carol", "/store-admin/withdraw", body)
        self.assertEqual((r.status_code, r.json(), r.headers.get("cache-control")), (200, {"added": True}, "no-store"))
        url, data, headers, redirects, trust_env = self.calls[-1]
        self.assertEqual((url, data, headers["Content-Type"]), ("http://bits-store:8099/_admin/withdraw", body, "application/json"))
        self.assertFalse(redirects or trust_env)
        tok = headers["Authorization"].removeprefix("Bearer ")
        pub = Ed25519PrivateKey.from_private_bytes(ADMIN_SEED).public_key()
        c = jwt.decode(tok, pub, algorithms=["EdDSA"], audience="example-site/store", issuer="https://bits.example.org")
        self.assertEqual((c["sub"], c["exp"] - c["iat"], c["cap"]), ("admin:carol", 60, [{"m": "PUT", "p": "/_admin/withdraw"}]))
        self.assertEqual(self.audited[-1], ("store_admin_action", {"user": "carol", "action": "withdraw", "code": 200,
                                            "principal": "human", "binding": "/restricted", "sha256": "sha256:ab",
                                            "why": "a licence"}))
        # a collection: its plan accepted, its apply refused by bits-store, both passed on and audited
        self.answer = Answer(202, {"id": "x"})
        self.assertEqual(self.put("carol", "/store-admin/gc/plan", b'{"grace":"168h"}').status_code, 202)
        self.assertEqual(self.calls[-1][:2], ("http://bits-store:8099/_admin/gc/plan", b'{"grace":"168h"}'))  # as sent
        self.assertEqual((self.audited[-1][1]["id"], self.audited[-1][1]["grace"]), ("x", "168h"))
        self.answer = Answer(400, {"error": "why"})
        r = self.put("carol", "/store-admin/withdraw", b'{"why": ""}')
        self.assertEqual((r.status_code, r.json()), (400, {"error": "why"}))
        self.answer = Answer(404, ValueError())                  # bits-store's plain 404
        self.assertEqual(self.put("carol", "/store-admin/lift", b"{}").json(), {"error": "not found"})
        self.answer = Answer(409, {"error": "applied already"})
        r = self.put("carol", "/store-admin/gc/apply", b'{"id": "x", "files": 3}')
        self.assertEqual((r.status_code, r.json()), (409, {"error": "applied already"}))
        self.assertEqual(self.audited[-1][1]["code"], 409)
        self.assertEqual((self.audited[-1][1]["id"], self.audited[-1][1]["files"]), ("x", "3"))
        for answer in (Answer(401, {"error": "x"}), Answer(500, ValueError()), requests.ConnectionError("down")):
            self.answer = answer
            self.assertEqual(self.put("carol", "/store-admin/delete", b"{}").status_code, 502, answer)
            self.assertEqual(self.audited[-1][1]["code"], 502)

    def test_actions_refused(self):
        n = len(self.calls)
        for user, path, body, code in ((None, "/store-admin/withdraw", b"{}", 401), ("alice", "/store-admin/withdraw", b"{}", 403),
                                       ("carol", "/store-admin/summary", b"{}", 404), ("carol", "/store-admin/gc", b"{}", 404),
                                       ("carol", "/store-admin/withdraw", b"[1]", 400), ("carol", "/store-admin/withdraw", b"{", 400),
                                       ("carol", "/store-admin/withdraw", b"{}" + b" " * 5000, 413)):
            self.assertEqual(self.put(user, path, body).status_code, code, (user, path, body[:9]))
        self.assertEqual(len(self.calls), n)                     # bits-store never asked
        self.assertIn(("store_admin_denied", {"user": "alice", "action": "withdraw", "principal": "human"}), self.audited)
        # what bits-store's decoder would read otherwise than the audit: a key in other case, twice
        for body in (b'{"why": "benign", "WHY": "real"}', b'{"key": "a", "key": "b"}', b'{"\xc5\xbfha256": "x"}'):
            self.assertEqual(self.put("carol", "/store-admin/delete", body).status_code, 400, body)
        self.assertEqual(len(self.calls), n)
        self.assertEqual(self.get("carol", "/store-admin/withdraw").status_code, 404)   # actions are PUTs

    def test_a_collections_log(self):
        self.answer = Answer(200, ValueError(), text="applied by admin:carol\ndeleted: x\n")
        r = self.get("carol", "/store-admin/gc/log?id=x")
        self.assertEqual((r.status_code, r.text, r.headers["content-type"].split(";")[0]),
                         (200, "applied by admin:carol\ndeleted: x\n", "text/plain"))
        self.assertEqual(self.calls[-1][0], "http://bits-store:8099/_admin/gc/log")

    def test_signed_by_the_security_proxy(self):
        from bits_helpers import trust
        key, seen = Ed25519PrivateKey.from_private_bytes(ADMIN_SEED), []

        def call(settings, fn, data, route=None, prefix=None, agent_socket=None, host=None):
            seen.append((route, prefix, agent_socket, host))
            return trust.sign_bytes(data, key)
        _configure(BITS_STORE_ADMIN_TOKEN_KEY="", BITS_STORE_ADMIN_TOKEN_SIGN_ROUTE="bits-store-admin-token-sign",
                   BITS_STORE_TOKEN_SIGN_SOCKET="/run/store-token-signer/agent.sock",
                   BITS_STORE_TOKEN_SIGN_HOST="store-token-signer")
        with mock.patch.object(main.signproxy, "call", call):
            r = self.get("carol", "/store-admin/summary")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, [("bits-store-admin-token-sign", "/sign/store-admin",
                                 "/run/store-token-signer/agent.sock", "store-token-signer")])
        tok = self.calls[-1][2]["Authorization"].removeprefix("Bearer ")
        jwt.decode(tok, key.public_key(), algorithms=["EdDSA"], audience="example-site/store",
                   issuer="https://bits.example.org")

    def test_settings(self):
        for over in ({"BITS_STORE_ADMIN_URL": "ftp://bits-store"}, {"BITS_STORE_ADMIN_URL": "http://bits-store:8099/x"},
                     {"BITS_STORE_ADMIN_URL": "http://u@bits-store:8099"}, {"BITS_STORE_ADMIN_TOKEN_KID": "console-1"},
                     {"BITS_STORE_ADMIN_TOKEN_KID": "builds-1", "BITS_STORE_BUILD_TOKEN_KID": "builds-1"}):
            with self.assertRaises(ValueError, msg=over):
                config.Settings(env=dict(ENV, **over))
        for off in ("BITS_STORE_ADMIN_TOKEN_KEY", "BITS_STORE_ADMIN_TOKEN_KID", "BITS_STORE_ADMIN_URL"):
            self.assertFalse(config.Settings(env=dict(ENV, **{off: ""})).store_admin_configured(), off)
        self.assertTrue(config.Settings(env=ENV).store_admin_configured())
        self.assertTrue(config.Settings(env=dict(ENV, BITS_STORE_ADMIN_TOKEN_KEY="",
                                                 BITS_STORE_ADMIN_TOKEN_SIGN_ROUTE="r")).store_admin_configured())


if __name__ == "__main__":
    unittest.main()
