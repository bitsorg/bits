# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""C3 tests: cross-device (CLI-initiated) signing — terminal submits, a human
approves in the browser with a passkey, the CLI polls the result."""

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


class TestCliSign(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "creds.json")
        main.settings = config.Settings(env={
            "BITS_WEBAUTHN_RP_ID": RP_ID, "BITS_WEBAUTHN_ORIGIN": ORIGIN,
            "BITS_WEBAUTHN_CREDENTIALS": path, "BITS_WEBAUTHN_REQUIRE_UV": "0",
            "BITS_SIGN_PROXY_URL": "http://proxy/sign/bits",
            "BITS_ADMINS_POLICY": "lcg @alice",
            "BITS_SESSION_COOKIE_SECURE": "0"})
        main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None
        main.credstore = credentials.CredentialStore(path)
        main.cli_signs = main.session.CliSignStore()
        self.client = TestClient(main.app, follow_redirects=False)
        self.device = SoftWebauthnDevice()
        self.sign_key = Ed25519PrivateKey.generate()
        os.environ["BITS_SIGN_PROXY_TOKEN"] = "gate"
        opts_json, chal = webauthn_rp.registration_options(main.settings, "alice", [])
        cred = webauthn_rp.verify_registration(main.settings, _attestation(self.device, opts_json), chal)
        main.credstore.add("alice", cred)

    def tearDown(self):
        os.environ.pop("BITS_SIGN_PROXY_TOKEN", None)
        shutil.rmtree(self.dir, ignore_errors=True)

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
        ]

    def test_request_returns_id_no_url(self):
        r = self.client.post("/sign/cli/request", content=self._manifest())
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertIn("request_id", j)
        self.assertIn("digest", j)
        self.assertEqual(j["groups"], ["lcg"])
        self.assertNotIn("approve_url", j)   # the CLI builds the approve URL itself

    def test_pending_is_open_admin_enforced_at_approve(self):
        # GET is unauthenticated now (the 192-bit req_id is the secret) and does no
        # admin check — it returns discoverable options (empty allowCredentials).
        rid = self.client.post("/sign/cli/request",
                               content=self._manifest("common")).json()["request_id"]
        pend = self.client.get("/sign/cli/" + rid)   # no bearer
        self.assertEqual(pend.status_code, 200)
        self.assertEqual(pend.json()["publicKey"].get("allowCredentials", []), [])
        # Admin is enforced at APPROVE, from the passkey's user: alice admins lcg,
        # not common -> 403 (before any signing).
        assertion = _assertion(self.device, pend.json()["publicKey"])
        r = self.client.post("/sign/cli/" + rid + "/approve", json={"assertion": assertion})
        self.assertEqual(r.status_code, 403)

    def test_full_cross_device_flow(self):
        body = self._manifest("lcg")
        rid = self.client.post("/sign/cli/request", content=body).json()["request_id"]
        # CLI polling: still pending
        self.assertEqual(self.client.get("/sign/cli/" + rid + "/result").json()["status"], "pending")
        # Approver reviews + approves with a passkey — NO login/bearer; identity
        # comes from the assertion's credential.
        pend = self.client.get("/sign/cli/" + rid).json()
        self.assertEqual(pend["status"], "pending")
        self.assertIn("A", pend["manifest"])
        assertion = _assertion(self.device, pend["publicKey"])
        ps = self._patch_proxy()
        for p in ps:
            p.start()
        try:
            appr = self.client.post("/sign/cli/" + rid + "/approve",
                                    json={"assertion": assertion})
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(appr.status_code, 200)
        # CLI polling: signed, envelope verifies over the exact manifest
        res = self.client.get("/sign/cli/" + rid + "/result").json()
        self.assertEqual(res["status"], "signed")
        trusted = {trust.key_id(self.sign_key.public_key()): self.sign_key.public_key()}
        self.assertEqual(trust.verify_bytes(body, res["envelope"], trusted),
                         trust.key_id(self.sign_key.public_key()))
        self.assertEqual(res["signed_by"], "alice")

    def test_result_unknown_404(self):
        self.assertEqual(self.client.get("/sign/cli/nope/result").status_code, 404)


if __name__ == "__main__":
    unittest.main()
