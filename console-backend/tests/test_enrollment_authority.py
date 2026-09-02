# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""C6 tests: enrolment authority — first passkey needs a bits-admin grant, a
further passkey needs step-up with an existing one."""

import json
import os
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient
from soft_webauthn import SoftWebauthnDevice
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from console_backend import config, credentials, main

RP_ID = "example.org"
ORIGIN = "https://example.org"


def _attestation(device, options_json):
    o = json.loads(options_json)
    pkcco = {"publicKey": {
        "rp": o["rp"],
        "user": {"id": base64url_to_bytes(o["user"]["id"]),
                 "name": o["user"]["name"], "displayName": o["user"]["displayName"]},
        "challenge": base64url_to_bytes(o["challenge"]),
        "pubKeyCredParams": o["pubKeyCredParams"]}}
    att = device.create(pkcco, ORIGIN)
    return {"id": bytes_to_base64url(att["rawId"]), "rawId": bytes_to_base64url(att["rawId"]),
            "type": att["type"],
            "response": {"clientDataJSON": bytes_to_base64url(att["response"]["clientDataJSON"]),
                         "attestationObject": bytes_to_base64url(att["response"]["attestationObject"])}}


def _assertion(device, pk):
    pkcro = {"publicKey": {
        "challenge": base64url_to_bytes(pk["challenge"]), "rpId": pk["rpId"],
        "allowCredentials": [{"type": "public-key", "id": base64url_to_bytes(c["id"])}
                             for c in pk.get("allowCredentials", [])]}}
    asr = device.get(pkcro, ORIGIN)
    resp = {k: bytes_to_base64url(asr["response"][k])
            for k in ("clientDataJSON", "authenticatorData", "signature")}
    if asr["response"].get("userHandle"):
        resp["userHandle"] = bytes_to_base64url(asr["response"]["userHandle"])
    return {"id": bytes_to_base64url(asr["rawId"]), "rawId": bytes_to_base64url(asr["rawId"]),
            "type": asr["type"], "response": resp}


class TestEnrollmentAuthority(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "c.json")
        main.settings = config.Settings(env={
            "BITS_WEBAUTHN_RP_ID": RP_ID, "BITS_WEBAUTHN_ORIGIN": ORIGIN,
            "BITS_WEBAUTHN_CREDENTIALS": path, "BITS_WEBAUTHN_REQUIRE_UV": "0",
            "BITS_ADMINS_POLICY": "* @root\nlcg @alice",   # root overall, alice lcg
            "BITS_SESSION_COOKIE_SECURE": "0"})            # authority ON (default)
        main.sessions = main.session.SessionStore(60)
        main.credstore = credentials.CredentialStore(path)
        main.enroll_grants = main.session.EnrollmentGrantStore()
        self.client = TestClient(main.app, follow_redirects=False)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _sess(self, user):
        return {"bits_session": main.sessions.create({"user": user, "token": "t"})}

    def _begin(self, cookies):
        return self.client.post("/webauthn/register/begin", cookies=cookies).json()

    def _finish(self, payload, cookies):
        return self.client.post("/webauthn/register/finish", json=payload, cookies=cookies)

    def test_only_bits_admin_can_grant(self):
        # alice administers lcg but is not a bits (overall) admin -> 403
        r = self.client.post("/webauthn/grant", json={"user": "alice"}, cookies=self._sess("alice"))
        self.assertEqual(r.status_code, 403)
        # root is overall -> 200
        r = self.client.post("/webauthn/grant", json={"user": "alice"}, cookies=self._sess("root"))
        self.assertEqual(r.status_code, 200)

    def test_first_enrolment_denied_without_grant(self):
        c = self._sess("alice")
        att = _attestation(SoftWebauthnDevice(), json.dumps(self._begin(c)["publicKey"]))
        r = self._finish({"attestation": att}, c)
        self.assertEqual(r.status_code, 403)

    def test_first_enrolment_allowed_with_grant(self):
        self.client.post("/webauthn/grant", json={"user": "alice"}, cookies=self._sess("root"))
        c = self._sess("alice")
        att = _attestation(SoftWebauthnDevice(), json.dumps(self._begin(c)["publicKey"]))
        self.assertEqual(self._finish({"attestation": att}, c).status_code, 200)
        self.assertEqual(len(main.credstore.get("alice")), 1)

    def test_grant_is_single_use(self):
        self.client.post("/webauthn/grant", json={"user": "alice"}, cookies=self._sess("root"))
        c = self._sess("alice")
        att = _attestation(SoftWebauthnDevice(), json.dumps(self._begin(c)["publicKey"]))
        self._finish({"attestation": att}, c)                    # consumes the grant
        # a would-be second FIRST enrolment... but now alice has a cred, so it's a
        # step-up path; the point is the grant is gone. Verify via a fresh user:
        self.client.post("/webauthn/grant", json={"user": "bob"}, cookies=self._sess("root"))
        cb = self._sess("bob")
        a1 = _attestation(SoftWebauthnDevice(), json.dumps(self._begin(cb)["publicKey"]))
        self.assertEqual(self._finish({"attestation": a1}, cb).status_code, 200)   # grant used
        a2 = _attestation(SoftWebauthnDevice(), json.dumps(self._begin(cb)["publicKey"]))
        # bob now has a cred -> further enrolment needs step-up, not the (spent) grant
        self.assertEqual(self._finish({"attestation": a2}, cb).status_code, 403)

    def test_subsequent_enrolment_requires_stepup(self):
        # Bootstrap alice's first passkey (device1) via a grant.
        self.client.post("/webauthn/grant", json={"user": "alice"}, cookies=self._sess("root"))
        c = self._sess("alice")
        dev1 = SoftWebauthnDevice()
        a1 = _attestation(dev1, json.dumps(self._begin(c)["publicKey"]))
        self._finish({"attestation": a1}, c)
        dev2 = SoftWebauthnDevice()
        # Attempt WITHOUT step-up -> 403.
        b_no = self._begin(c)
        self.assertIn("stepup", b_no)
        a_no = _attestation(dev2, json.dumps(b_no["publicKey"]))
        self.assertEqual(self._finish({"attestation": a_no}, c).status_code, 403)
        # Attempt WITH a step-up assertion from the existing passkey (device1) -> 200.
        b_ok = self._begin(c)                       # fresh challenges
        a_ok = _attestation(dev2, json.dumps(b_ok["publicKey"]))
        stepup = _assertion(dev1, b_ok["stepup"])
        r = self._finish({"attestation": a_ok, "stepup": stepup}, c)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(main.credstore.get("alice")), 2)


if __name__ == "__main__":
    unittest.main()
