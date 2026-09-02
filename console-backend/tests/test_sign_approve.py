# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""C2 tests: digest-bound WebAuthn approval gating human signing.

A real software authenticator approves; the signature must verify over the exact
manifest, an approval made for a DIFFERENT digest must be rejected (content
binding), single-shot /sign is blocked once a passkey is enrolled, and a foreign
request cannot be approved.
"""

import base64
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from soft_webauthn import SoftWebauthnDevice
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from console_backend import config, credentials, main, webauthn_rp
from bits_helpers import trust

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
    return json.dumps({
        "id": bytes_to_base64url(att["rawId"]), "rawId": bytes_to_base64url(att["rawId"]),
        "type": att["type"],
        "response": {"clientDataJSON": bytes_to_base64url(att["response"]["clientDataJSON"]),
                     "attestationObject": bytes_to_base64url(att["response"]["attestationObject"])}})


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


class TestApproval(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "creds.json")
        main.settings = config.Settings(env={
            "BITS_WEBAUTHN_RP_ID": RP_ID, "BITS_WEBAUTHN_ORIGIN": ORIGIN,
            "BITS_WEBAUTHN_CREDENTIALS": path, "BITS_WEBAUTHN_REQUIRE_UV": "0",
            "BITS_SIGN_PROXY_URL": "http://proxy/sign/bits",
            "BITS_ADMINS_POLICY": "lcg @alice\ncommon @root",
            "BITS_SESSION_COOKIE_SECURE": "0"})
        main.sessions = main.session.SessionStore(60)
        main.credstore = credentials.CredentialStore(path)
        main.sign_requests = main.session.SignRequestStore()
        self.client = TestClient(main.app, follow_redirects=False)
        self.device = SoftWebauthnDevice()
        self.sign_key = Ed25519PrivateKey.generate()
        os.environ["BITS_SIGN_PROXY_TOKEN"] = "gate"
        # Enrol alice's passkey (same device authenticates below).
        opts_json, chal = webauthn_rp.registration_options(main.settings, "alice", [])
        cred = webauthn_rp.verify_registration(main.settings, _attestation(self.device, opts_json), chal)
        main.credstore.add("alice", cred)

    def tearDown(self):
        os.environ.pop("BITS_SIGN_PROXY_TOKEN", None)
        shutil.rmtree(self.dir, ignore_errors=True)

    def _manifest(self, pkg="A", group="lcg"):
        return json.dumps({"packages": [{"package": pkg, "group": group}]}).encode()

    def _patch_proxy(self):
        return [
            patch.object(main.trust, "sign_bytes_via_proxy",
                         lambda data, url, tok: trust.sign_bytes(data, self.sign_key)),
            patch.object(main.trust, "proxy_pubkey",
                         lambda url, tok: (trust.key_id(self.sign_key.public_key()),
                                           self.sign_key.public_key())),
            patch.object(main.trust, "load_key_policy", return_value=None),
        ]

    def _alice(self):
        return {"bits_session": main.sessions.create({"user": "alice", "token": "t"})}

    @staticmethod
    def _b64(body):
        return base64.b64encode(body).decode()

    def test_approval_signs_over_exact_bytes(self):
        c = self._alice()
        body = self._manifest()
        rj = self.client.post("/sign/request", content=body, cookies=c).json()
        assertion = _assertion(self.device, rj["publicKey"])
        ps = self._patch_proxy()
        for p in ps:
            p.start()
        try:
            appr = self.client.post("/sign/approve", cookies=c,
                                    json={"request_id": rj["request_id"], "assertion": assertion,
                                          "manifest": self._b64(body)})
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(appr.status_code, 200)
        env = appr.json()["envelope"]
        trusted = {trust.key_id(self.sign_key.public_key()): self.sign_key.public_key()}
        self.assertEqual(trust.verify_bytes(body, env, trusted),
                         trust.key_id(self.sign_key.public_key()))

    def test_single_shot_blocked_when_enrolled(self):
        c = self._alice()
        r = self.client.post("/sign", content=self._manifest(), cookies=c)
        self.assertEqual(r.status_code, 409)

    def test_webauthn_required_blocks_passkeyless_human(self):
        # root is a 'common' admin with NO passkey; with WebAuthn required, even a
        # single-shot /sign must be blocked (mandatory 2nd factor).
        main.settings.webauthn_required = True
        try:
            sid = main.sessions.create({"user": "root", "token": "t"})
            r = self.client.post("/sign", content=self._manifest(group="common"),
                                 cookies={"bits_session": sid})
            self.assertEqual(r.status_code, 409)
        finally:
            main.settings.webauthn_required = False

    def test_request_requires_admin(self):
        c = self._alice()
        r = self.client.post("/sign/request", content=self._manifest(group="common"), cookies=c)
        self.assertEqual(r.status_code, 403)

    def test_approval_for_a_different_digest_rejected(self):
        # Approve request A with an assertion produced for request B's challenge.
        c = self._alice()
        ra = self.client.post("/sign/request", content=self._manifest("A"), cookies=c).json()
        rb = self.client.post("/sign/request", content=self._manifest("B"), cookies=c).json()
        assertion_for_b = _assertion(self.device, rb["publicKey"])
        appr = self.client.post("/sign/approve", cookies=c,
                                json={"request_id": ra["request_id"], "assertion": assertion_for_b,
                                      "manifest": self._b64(self._manifest("A"))})
        self.assertEqual(appr.status_code, 403)

    def test_resubmitted_manifest_must_match_digest(self):
        c = self._alice()
        body = self._manifest("A")
        rj = self.client.post("/sign/request", content=body, cookies=c).json()
        assertion = _assertion(self.device, rj["publicKey"])
        # A different manifest than was approved must be rejected before signing.
        appr = self.client.post("/sign/approve", cookies=c,
            json={"request_id": rj["request_id"], "assertion": assertion,
                  "manifest": self._b64(self._manifest("TAMPERED"))})
        self.assertEqual(appr.status_code, 400)

    def test_request_is_single_use(self):
        c = self._alice()
        body = self._manifest("A")
        rj = self.client.post("/sign/request", content=body, cookies=c).json()
        payload = {"request_id": rj["request_id"],
                   "assertion": _assertion(self.device, rj["publicKey"]),
                   "manifest": self._b64(body)}
        ps = self._patch_proxy()
        for p in ps:
            p.start()
        try:
            first = self.client.post("/sign/approve", cookies=c, json=payload)
            second = self.client.post("/sign/approve", cookies=c, json=payload)
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 400)   # request consumed

    def test_foreign_request_rejected(self):
        c = self._alice()
        ra = self.client.post("/sign/request", content=self._manifest(), cookies=c).json()
        assertion = _assertion(self.device, ra["publicKey"])
        bob = {"bits_session": main.sessions.create({"user": "bob", "token": "t"})}
        appr = self.client.post("/sign/approve", cookies=bob,
                                json={"request_id": ra["request_id"], "assertion": assertion,
                                      "manifest": self._b64(self._manifest())})
        self.assertEqual(appr.status_code, 400)


if __name__ == "__main__":
    unittest.main()
