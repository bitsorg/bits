# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""C1 tests: WebAuthn enrolment + the digest-bound assertion building block.

A real software authenticator (soft_webauthn) produces genuine attestation and
assertion, verified by py_webauthn — so registration, the credential store, and
the digest==challenge binding are exercised end to end.
"""

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from soft_webauthn import SoftWebauthnDevice
from fastapi.testclient import TestClient
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from console_backend import config, credentials, main, webauthn_rp

RP_ID = "example.org"
ORIGIN = "https://example.org"


def _attestation(device, options_json):
    o = json.loads(options_json)
    pkcco = {"publicKey": {
        "rp": o["rp"],
        "user": {"id": base64url_to_bytes(o["user"]["id"]),
                 "name": o["user"]["name"], "displayName": o["user"]["displayName"]},
        "challenge": base64url_to_bytes(o["challenge"]),
        "pubKeyCredParams": o["pubKeyCredParams"],
    }}
    att = device.create(pkcco, ORIGIN)
    return json.dumps({
        "id": bytes_to_base64url(att["rawId"]),
        "rawId": bytes_to_base64url(att["rawId"]),
        "type": att["type"],
        "response": {
            "clientDataJSON": bytes_to_base64url(att["response"]["clientDataJSON"]),
            "attestationObject": bytes_to_base64url(att["response"]["attestationObject"]),
        },
    })


def _assertion(device, options_json):
    o = json.loads(options_json)
    pkcro = {"publicKey": {
        "challenge": base64url_to_bytes(o["challenge"]), "rpId": o["rpId"],
        "allowCredentials": [{"type": "public-key", "id": base64url_to_bytes(c["id"])}
                             for c in o.get("allowCredentials", [])],
    }}
    asr = device.get(pkcro, ORIGIN)
    resp = {k: bytes_to_base64url(asr["response"][k])
            for k in ("clientDataJSON", "authenticatorData", "signature")}
    if asr["response"].get("userHandle"):
        resp["userHandle"] = bytes_to_base64url(asr["response"]["userHandle"])
    return json.dumps({"id": bytes_to_base64url(asr["rawId"]),
                       "rawId": bytes_to_base64url(asr["rawId"]),
                       "type": asr["type"], "response": resp})


def _settings(creds_path=None):
    env = {"BITS_WEBAUTHN_RP_ID": RP_ID, "BITS_WEBAUTHN_ORIGIN": ORIGIN,
           "BITS_SESSION_COOKIE_SECURE": "0",
           "BITS_WEBAUTHN_REQUIRE_UV": "0"}   # soft authenticator can't do UV
    if creds_path:
        env["BITS_WEBAUTHN_CREDENTIALS"] = creds_path
    return config.Settings(env=env)


class TestEnrollEndpoints(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "creds.json")
        main.settings = _settings(path)
        main.sessions = main.session.SessionStore(60)
        main.credstore = credentials.CredentialStore(path)
        self.client = TestClient(main.app, follow_redirects=False)
        self.device = SoftWebauthnDevice()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_begin_requires_session(self):
        self.assertEqual(self.client.post("/webauthn/register/begin").status_code, 401)

    def test_begin_requires_config(self):
        main.settings = config.Settings(env={"BITS_SESSION_COOKIE_SECURE": "0"})
        sid = main.sessions.create({"user": "alice", "token": "t"})
        r = self.client.post("/webauthn/register/begin", cookies={"bits_session": sid})
        self.assertEqual(r.status_code, 503)

    def test_full_enrollment(self):
        sid = main.sessions.create({"user": "alice", "token": "t"})
        c = {"bits_session": sid}
        begin = self.client.post("/webauthn/register/begin", cookies=c)
        self.assertEqual(begin.status_code, 200)
        att = _attestation(self.device, begin.text)
        finish = self.client.post("/webauthn/register/finish", content=att, cookies=c)
        self.assertEqual(finish.status_code, 200)
        self.assertEqual(finish.json()["status"], "enrolled")
        self.assertEqual(len(main.credstore.get("alice")), 1)

    def test_finish_without_begin_400(self):
        sid = main.sessions.create({"user": "alice", "token": "t"})
        r = self.client.post("/webauthn/register/finish", content=b"{}",
                             cookies={"bits_session": sid})
        self.assertEqual(r.status_code, 400)


class TestStore(unittest.TestCase):
    def test_persists_across_reload(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "creds.json")
            s1 = credentials.CredentialStore(path)
            s1.add("alice", {"id": "x", "public_key": "p", "sign_count": 0})
            s1.add("alice", {"id": "x", "public_key": "p", "sign_count": 0})  # idempotent
            s2 = credentials.CredentialStore(path)
            self.assertEqual(len(s2.get("alice")), 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestDigestBinding(unittest.TestCase):
    def test_assertion_binds_to_the_digest(self):
        s = _settings()
        dev = SoftWebauthnDevice()
        opts_json, chal = webauthn_rp.registration_options(s, "alice", [])
        cred = webauthn_rp.verify_registration(s, _attestation(dev, opts_json), chal)

        digest = hashlib.sha256(b"the-manifest").digest()
        aopts = webauthn_rp.authentication_options(s, digest, [cred])
        assertion = _assertion(dev, aopts)
        # Correct digest verifies.
        new_count = webauthn_rp.verify_authentication(s, assertion, digest, cred)
        self.assertGreaterEqual(new_count, cred["sign_count"])
        # A DIFFERENT digest must NOT verify (content binding — negative control).
        other = hashlib.sha256(b"a-different-manifest").digest()
        with self.assertRaises(Exception):
            webauthn_rp.verify_authentication(s, assertion, other, cred)

    def test_uv_required_rejects_presence_only(self):
        # With UV required (prod default), a user-presence-only assertion (all the
        # soft authenticator can make) must be rejected — real passkeys set UV.
        s_nouv = _settings()                                   # UV off
        s_uv = config.Settings(env={"BITS_WEBAUTHN_RP_ID": RP_ID,
                                    "BITS_WEBAUTHN_ORIGIN": ORIGIN})  # UV default on
        dev = SoftWebauthnDevice()
        opts_json, chal = webauthn_rp.registration_options(s_nouv, "alice", [])
        cred = webauthn_rp.verify_registration(s_nouv, _attestation(dev, opts_json), chal)
        digest = hashlib.sha256(b"m").digest()
        assertion = _assertion(dev, webauthn_rp.authentication_options(s_nouv, digest, [cred]))
        webauthn_rp.verify_authentication(s_nouv, assertion, digest, cred)   # UV off: ok
        with self.assertRaises(Exception):
            webauthn_rp.verify_authentication(s_uv, assertion, digest, cred)  # UV on: reject


if __name__ == "__main__":
    unittest.main()
