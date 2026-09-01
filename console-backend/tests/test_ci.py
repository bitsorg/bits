# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""B4 tests: GitLab CI ID token verification + CI-authorized signing.

A real RSA-signed JWT is verified against the test public key (JWKS fetch is
bypassed by passing the key), so signature/issuer/audience/expiry are exercised
for real; the sign path is tested with the proxy faked.
"""

import json
import os
import time
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from console_backend import ci_auth, config, main
from bits_helpers import trust

_ISS = "https://gitlab.example"
_AUD = "bits-console"


def _settings(ci_signers="proj/manifests *"):
    return config.Settings(env={
        "BITS_SIGN_PROXY_URL": "http://proxy/sign/bits",
        "BITS_OIDC_ISSUER": _ISS,
        "BITS_OIDC_CI_AUDIENCE": _AUD,
        "BITS_OIDC_JWKS_URL": "http://unused-in-tests",
        "BITS_CI_SIGNERS": ci_signers,
        "BITS_SESSION_COOKIE_SECURE": "0",
    })


def _rsa():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(priv, *, iss=_ISS, aud=_AUD, project="proj/manifests", ref="main",
           exp_delta=300):
    now = int(time.time())
    claims = {"iss": iss, "aud": aud, "sub": "job_1", "iat": now,
              "exp": now + exp_delta, "project_path": project, "ref": ref}
    return jwt.encode(claims, priv, algorithm="RS256")


class TestCIPolicy(unittest.TestCase):
    def test_authorized_and_denied(self):
        s = config.Settings(env={"BITS_CI_SIGNERS": "proj/a *\nproj/b lcg common"})
        pol = ci_auth.load_ci_signers(s)
        self.assertTrue(ci_auth.is_ci_authorized("proj/a", "anything", pol))
        self.assertTrue(ci_auth.is_ci_authorized("proj/b", "lcg", pol))
        self.assertFalse(ci_auth.is_ci_authorized("proj/b", "ship", pol))
        self.assertFalse(ci_auth.is_ci_authorized("proj/unknown", "lcg", pol))


class TestVerify(unittest.TestCase):
    def setUp(self):
        self.priv = _rsa()
        self.pub = self.priv.public_key()
        self.s = _settings()

    def test_valid_token(self):
        claims = ci_auth.verify_ci_token(_token(self.priv), self.s, signing_key=self.pub)
        self.assertEqual(claims["project_path"], "proj/manifests")

    def test_wrong_audience_rejected(self):
        with self.assertRaises(Exception):
            ci_auth.verify_ci_token(_token(self.priv, aud="someone-else"), self.s,
                                    signing_key=self.pub)

    def test_wrong_issuer_rejected(self):
        with self.assertRaises(Exception):
            ci_auth.verify_ci_token(_token(self.priv, iss="https://evil"), self.s,
                                    signing_key=self.pub)

    def test_expired_rejected(self):
        with self.assertRaises(Exception):
            ci_auth.verify_ci_token(_token(self.priv, exp_delta=-10), self.s,
                                    signing_key=self.pub)

    def test_wrong_key_rejected(self):
        with self.assertRaises(Exception):
            ci_auth.verify_ci_token(_token(self.priv), self.s,
                                    signing_key=_rsa().public_key())


class TestCISign(unittest.TestCase):
    def setUp(self):
        main.settings = _settings("proj/manifests lcg")   # may sign lcg only
        main.sessions = main.session.SessionStore(60)
        self.client = TestClient(main.app, follow_redirects=False)
        self.sign_key = Ed25519PrivateKey.generate()
        os.environ["BITS_SIGN_PROXY_TOKEN"] = "gate"

    def tearDown(self):
        os.environ.pop("BITS_SIGN_PROXY_TOKEN", None)

    def _manifest(self, group="lcg"):
        return json.dumps({"packages": [{"package": "A", "group": group}]}).encode()

    def _patch_proxy(self):
        return [
            patch.object(main.trust, "sign_bytes_via_proxy",
                         lambda data, url, tok: trust.sign_bytes(data, self.sign_key)),
            patch.object(main.trust, "proxy_pubkey",
                         lambda url, tok: (trust.key_id(self.sign_key.public_key()),
                                           self.sign_key.public_key())),
            patch.object(main.trust, "load_key_policy", return_value=None),
            # Bypass real JWT verification; return CI claims for the authorized project.
            patch.object(main.ci_auth, "verify_ci_token",
                         lambda token, settings: {"project_path": "proj/manifests",
                                                  "ref": "main"}),
        ]

    def _run(self, body, project_ok=True):
        ps = self._patch_proxy()
        if not project_ok:
            ps[-1] = patch.object(main.ci_auth, "verify_ci_token",
                                  lambda token, settings: {"project_path": "proj/rogue",
                                                           "ref": "main"})
        for p in ps:
            p.start()
        try:
            return self.client.post("/sign", content=body,
                                    headers={"Authorization": "Bearer faketoken"})
        finally:
            for p in ps:
                p.stop()

    def test_ci_signs_authorized_group(self):
        r = self._run(self._manifest("lcg"))
        self.assertEqual(r.status_code, 200)
        out = r.json()
        self.assertEqual(out["signed_by"], "ci:proj/manifests")
        trusted = {trust.key_id(self.sign_key.public_key()): self.sign_key.public_key()}
        self.assertEqual(trust.verify_bytes(self._manifest("lcg"), out["envelope"], trusted),
                         trust.key_id(self.sign_key.public_key()))

    def test_ci_denied_unauthorized_group(self):
        r = self._run(self._manifest("common"))   # project may sign lcg only
        self.assertEqual(r.status_code, 403)

    def test_ci_denied_unlisted_project(self):
        r = self._run(self._manifest("lcg"), project_ok=False)
        self.assertEqual(r.status_code, 403)

    def test_invalid_ci_token_401(self):
        with patch.object(main.ci_auth, "verify_ci_token",
                          side_effect=Exception("bad")):
            r = self.client.post("/sign", content=self._manifest(),
                                 headers={"Authorization": "Bearer bad"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
