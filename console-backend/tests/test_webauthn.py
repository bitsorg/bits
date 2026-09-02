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
           "BITS_WEBAUTHN_REQUIRE_UV": "0",       # soft authenticator can't do UV
           "BITS_ENROLLMENT_AUTHORITY": "0"}      # self-service path (C6 tested separately)
    if creds_path:
        env["BITS_WEBAUTHN_CREDENTIALS"] = creds_path
    return config.Settings(env=env)


class TestEnrollEndpoints(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "creds.json")
        main.settings = _settings(path)
        main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None
        main.reg_challenges = main.session.RegChallengeStore()
        main.credstore = credentials.CredentialStore(path)
        self.client = TestClient(main.app, follow_redirects=False)
        self.device = SoftWebauthnDevice()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_begin_requires_auth(self):
        self.assertEqual(self.client.post("/webauthn/register/begin").status_code, 401)

    def test_begin_requires_config(self):
        main.settings = config.Settings(env={})
        r = self.client.post("/webauthn/register/begin",
                             headers={"Authorization": "Bearer alice"})
        self.assertEqual(r.status_code, 503)

    def test_full_enrollment(self):
        c = {"Authorization": "Bearer alice"}
        begin = self.client.post("/webauthn/register/begin", headers=c)
        self.assertEqual(begin.status_code, 200)
        pk = begin.json()["publicKey"]
        att = _attestation(self.device, json.dumps(pk))
        finish = self.client.post("/webauthn/register/finish",
                                  json={"attestation": json.loads(att)}, headers=c)
        self.assertEqual(finish.status_code, 200)
        self.assertEqual(finish.json()["status"], "enrolled")
        self.assertEqual(len(main.credstore.get("alice")), 1)

    def test_finish_without_begin_400(self):
        r = self.client.post("/webauthn/register/finish", content=b"{}",
                             headers={"Authorization": "Bearer alice"})
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


class TestRegChallengeStore(unittest.TestCase):
    def test_bounded_and_single_use(self):
        st = main.session.RegChallengeStore(ttl_seconds=600, max_entries=5)
        for i in range(50):
            st.put("u%d" % i, {"reg_challenge": "c"})
        self.assertLessEqual(len(st._store), 5)      # bounded (anti-DoS)
        st.put("alice", {"reg_challenge": "x"})
        self.assertIsNotNone(st.pop("alice"))
        self.assertIsNone(st.pop("alice"))           # single-use


if __name__ == "__main__":
    unittest.main()
