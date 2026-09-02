# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""B3 tests: the community-admin-gated /sign endpoint (Mode 1).

The proxy is faked with a real Ed25519 key so the returned envelope actually
verifies over the exact submitted bytes; authz is exercised for real.
"""

import json
import os
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from console_backend import config, main
from bits_helpers import trust


def _configure(policy="lcg @alice\ncommon @root"):
    main.settings = config.Settings(env={
        "GITLAB_API_URL": "https://gitlab.example/api/v4",
        "BITS_ADMINS_POLICY": policy,
        "BITS_SIGN_PROXY_URL": "http://proxy/sign/bits",
        "BITS_SESSION_COOKIE_SECURE": "0",
    })
    main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None


def _manifest(group="lcg"):
    return json.dumps({"architecture": "slc7_x86-64",
                       "packages": [{"package": "A", "hash": "h1", "group": group}]}).encode()


class TestSign(unittest.TestCase):
    def setUp(self):
        _configure()
        self.priv = Ed25519PrivateKey.generate()
        self.client = TestClient(main.app, follow_redirects=False)
        os.environ["BITS_SIGN_PROXY_TOKEN"] = "gate"

    def tearDown(self):
        os.environ.pop("BITS_SIGN_PROXY_TOKEN", None)

    def _session(self, user):
        return {"Authorization": "Bearer %s" % user}

    def _patched(self, policy=None):
        # Fake proxy: sign with our known key; pubkey/keyid from it.
        return [
            patch.object(main.trust, "sign_bytes_via_proxy",
                         lambda data, url, tok: trust.sign_bytes(data, self.priv)),
            patch.object(main.trust, "proxy_pubkey",
                         lambda url, tok: (trust.key_id(self.priv.public_key()),
                                           self.priv.public_key())),
            patch.object(main.trust, "load_key_policy", return_value=policy),
        ]

    def test_sign_requires_session(self):
        r = self.client.post("/sign", content=_manifest())
        self.assertEqual(r.status_code, 401)

    def test_sign_happy_path_verifies_over_exact_bytes(self):
        body = _manifest("lcg")
        sid = self._session("alice")
        ps = self._patched()
        for p in ps:
            p.start()
        try:
            r = self.client.post("/sign", content=body, headers=sid)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r.status_code, 200)
        out = r.json()
        env = out["envelope"]
        trusted = {trust.key_id(self.priv.public_key()): self.priv.public_key()}
        self.assertEqual(trust.verify_bytes(body, env, trusted),
                         trust.key_id(self.priv.public_key()))
        self.assertEqual(out["groups"], ["lcg"])
        self.assertEqual(out["signed_by"], "alice")
        # Neither token appears anywhere in the response.
        self.assertNotIn("gate", r.text)
        self.assertNotIn("\"t\"", r.text)

    def test_sign_denied_for_non_admin_group(self):
        # alice admins lcg, not common; a manifest with a common package -> 403.
        body = _manifest("common")
        sid = self._session("alice")
        ps = self._patched()
        for p in ps:
            p.start()
        try:
            r = self.client.post("/sign", content=body, headers=sid)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r.status_code, 403)

    def test_sign_blocked_by_key_policy(self):
        # authz ok (alice/lcg) but the proxy key is not authorized for lcg.
        body = _manifest("lcg")
        sid = self._session("alice")
        deny_policy = {"default": []}   # key authorized for nothing
        ps = self._patched(policy=deny_policy)
        for p in ps:
            p.start()
        try:
            r = self.client.post("/sign", content=body, headers=sid)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r.status_code, 403)

    def test_sign_bad_json_400(self):
        sid = self._session("alice")
        r = self.client.post("/sign", content=b"not json", headers=sid)
        self.assertEqual(r.status_code, 400)

    def test_sign_rejects_oversized_body(self):
        sid = self._session("alice")
        orig = main._MAX_BODY
        main._MAX_BODY = 10
        try:
            r = self.client.post("/sign", content=_manifest("lcg"),
                                 headers=sid)
        finally:
            main._MAX_BODY = orig
        self.assertEqual(r.status_code, 413)

    def test_sign_malformed_packages_no_500(self):
        # A non-dict package must not crash (500): it's treated as 'common', and
        # alice (not a common admin) is denied — fail-closed with 403.
        sid = self._session("alice")
        ps = self._patched()
        for p in ps:
            p.start()
        try:
            r = self.client.post("/sign", content=b'{"packages": ["junk"]}',
                                 headers=sid)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r.status_code, 403)

    def test_sign_no_proxy_token_503(self):
        os.environ.pop("BITS_SIGN_PROXY_TOKEN", None)
        sid = self._session("alice")
        r = self.client.post("/sign", content=_manifest(), headers=sid)
        self.assertEqual(r.status_code, 503)


if __name__ == "__main__":
    unittest.main()
