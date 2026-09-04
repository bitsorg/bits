# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""F1 tests: build/publish pre-approval. A logged-in admin passkey-approves a BUILD
by build_id (before its manifest exists); CI consumes it later to sign."""

import json
import os
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient
from soft_webauthn import SoftWebauthnDevice
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

from console_backend import config, credentials, main, session, webauthn_rp

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


class TestPreapprove(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "creds.json")
        main.settings = config.Settings(env={
            "BITS_WEBAUTHN_RP_ID": RP_ID, "BITS_WEBAUTHN_ORIGIN": ORIGIN,
            "BITS_WEBAUTHN_CREDENTIALS": path, "BITS_WEBAUTHN_REQUIRE_UV": "0",
            "BITS_SIGN_PROXY_URL": "http://proxy/sign/bits",
            "BITS_ADMINS_POLICY": "lcg @alice"})
        main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None
        main.credstore = credentials.CredentialStore(path)
        main.preapprovals = session.PreapprovalStore()
        self.client = TestClient(main.app, follow_redirects=False)
        self.device = SoftWebauthnDevice()
        opts_json, chal = webauthn_rp.registration_options(main.settings, "alice", [])
        cred = webauthn_rp.verify_registration(main.settings, _attestation(self.device, opts_json), chal)
        main.credstore.add("alice", cred)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _alice(self):
        return {"Authorization": "Bearer alice"}

    def _request(self, build_id="pipe-123", groups=("lcg",), headers=None):
        return self.client.post("/preapprove/request",
                                headers=self._alice() if headers is None else headers,
                                json={"build_id": build_id, "groups": list(groups)})

    def test_flow_records_preapproval(self):
        r = self._request()
        self.assertEqual(r.status_code, 200)
        pk = r.json()["publicKey"]
        a = self.client.post("/preapprove/approve", headers=self._alice(),
                             json={"build_id": "pipe-123", "assertion": _assertion(self.device, pk)})
        self.assertEqual(a.status_code, 200)
        self.assertEqual(a.json()["status"], "approved")
        rec = main.preapprovals.get("pipe-123")
        self.assertEqual(rec["status"], "approved")
        self.assertEqual(rec["user"], "alice")
        self.assertEqual(rec["groups"], ["lcg"])

    def test_request_requires_auth(self):
        self.assertEqual(self._request(headers={}).status_code, 401)

    def test_request_denied_for_non_admin_group(self):
        # alice admins lcg, not common
        self.assertEqual(self._request(groups=["common"]).status_code, 403)

    def test_approve_unknown_build_400(self):
        r = self._request()
        pk = r.json()["publicKey"]
        a = self.client.post("/preapprove/approve", headers=self._alice(),
                             json={"build_id": "other", "assertion": _assertion(self.device, pk)})
        self.assertEqual(a.status_code, 400)

    def test_approve_is_single_use(self):
        pk = self._request().json()["publicKey"]
        body = {"build_id": "pipe-123", "assertion": _assertion(self.device, pk)}
        self.assertEqual(self.client.post("/preapprove/approve", headers=self._alice(), json=body).status_code, 200)
        # second approve: status is now "approved", not "pending" -> rejected
        self.assertEqual(self.client.post("/preapprove/approve", headers=self._alice(), json=body).status_code, 400)

    def test_reapprove_of_approved_build_409(self):
        pk = self._request().json()["publicKey"]
        self.client.post("/preapprove/approve", headers=self._alice(),
                         json={"build_id": "pipe-123", "assertion": _assertion(self.device, pk)})
        # a fresh request for the same already-approved build must not wipe it
        self.assertEqual(self._request().status_code, 409)

    def test_bad_input_400(self):
        self.assertEqual(self.client.post("/preapprove/request", headers=self._alice(),
                         json={"build_id": {"x": 1}, "groups": ["lcg"]}).status_code, 400)
        self.assertEqual(self.client.post("/preapprove/request", headers=self._alice(),
                         json={"build_id": "b", "groups": ["lcg", 7]}).status_code, 400)

    def test_eviction_keeps_approved(self):
        st = session.PreapprovalStore(ttl_seconds=1000, max_entries=3)
        st.put("a", {"user": "u", "status": "approved"})
        st.put("b", {"user": "u", "status": "pending"})
        st.put("c", {"user": "u", "status": "pending"})
        st.put("d", {"user": "u", "status": "pending"})   # forces eviction of a pending, not "a"
        self.assertIsNotNone(st.get("a"))                 # approved survived
        self.assertIsNotNone(st.get("d"))


if __name__ == "__main__":
    unittest.main()
