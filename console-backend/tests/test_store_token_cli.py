# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""`bits login` across devices: a terminal asks for a group's store token, an admin
approves with a passkey, and only the terminal holding the poll secret gets the
token, once."""

import base64
import hashlib
import json
import os
import shutil
import tempfile
import unittest

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from soft_webauthn import SoftWebauthnDevice

from console_backend import config, credentials, main, session, webauthn_rp
from test_cli_sign import ORIGIN, RP_ID, _assertion, _attestation

SEED = hashlib.sha256(b"bits-store test key 1").digest()


class StoreTokenCliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "creds.json")
        main.settings = config.Settings(env={
            "BITS_WEBAUTHN_RP_ID": RP_ID, "BITS_WEBAUTHN_ORIGIN": ORIGIN,
            "BITS_WEBAUTHN_CREDENTIALS": path, "BITS_WEBAUTHN_REQUIRE_UV": "0",
            "BITS_ADMINS_POLICY": "lcg @alice\nlhcb @bob",
            "BITS_STORE_TOKEN_KEY": base64.urlsafe_b64encode(SEED).rstrip(b"=").decode(),
            "BITS_STORE_TOKEN_KID": "console-1", "BITS_STORE_TOKEN_ISSUER": "https://bits.example.org",
            "BITS_STORE_TOKEN_AUDIENCE": "example-site/store"})
        main.credstore = credentials.CredentialStore(path)
        main.cli_store_tokens = session.CliSignStore()
        self.client = TestClient(main.app, follow_redirects=False)
        self.device = SoftWebauthnDevice()
        opts_json, chal = webauthn_rp.registration_options(main.settings, "alice", [])
        main.credstore.add("alice", webauthn_rp.verify_registration(
            main.settings, _attestation(self.device, opts_json), chal))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _request(self, group="lcg"):
        r = self.client.post("/store/cli/request",
                             content=json.dumps({"group": group, "host": "lxplus9", "user": "alice"}))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _approve(self, rid, assertion=None):
        if assertion is None:
            assertion = _assertion(self.device, self.client.get("/store/cli/" + rid).json()["publicKey"])
        self.last = assertion
        return self.client.post("/store/cli/%s/approve" % rid, json={"assertion": assertion})

    def _result(self, rid, poll):
        return self.client.post("/store/cli/%s/result" % rid, json={"poll": poll})

    def test_flow_delivers_the_token_once(self):
        req = self._request()
        rid, poll = req["request_id"], req["poll"]
        self.assertNotEqual(rid, poll)
        self.assertEqual(self._result(rid, poll).json(), {"status": "pending"})
        pend = self.client.get("/store/cli/" + rid).json()
        self.assertEqual((pend["code"], pend["group"], pend["requested_by"]["host"], pend["hours"]),
                         (req["code"], "lcg", "lxplus9", 24))
        self.assertNotIn("poll", json.dumps(pend))
        self.assertEqual(self._approve(rid).status_code, 200)
        r = self._result(rid, poll)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        pub = Ed25519PrivateKey.from_private_bytes(SEED).public_key()
        c = jwt.decode(r.json()["token"], pub, algorithms=["EdDSA"],
                       audience="example-site/store", issuer="https://bits.example.org")
        self.assertEqual((c["sub"], c["grp"], c["exp"] - c["iat"]), ("alice", "lcg", 86400))
        self.assertEqual(r.json()["user"], "alice")
        # once: then only the status
        self.assertEqual(self._result(rid, poll).json(), {"status": "delivered"})
        self.assertEqual(self._approve(rid, assertion=self.last).status_code, 400)

    def test_the_link_is_not_the_poll_secret(self):
        req = self._request()
        rid = req["request_id"]
        self.assertEqual(self._approve(rid).status_code, 200)
        for poll in (rid, "", "x" * 43):
            self.assertEqual(self._result(rid, poll).status_code, 404, poll)
        self.assertEqual(self._result(rid, req["poll"]).json()["status"], "approved")

    def test_non_admin_refused_and_final(self):
        req = self._request("lhcb")      # alice does not admin lhcb
        self.assertEqual(self._approve(req["request_id"]).status_code, 403)
        self.assertEqual(self._result(req["request_id"], req["poll"]).json(), {"status": "refused"})
        self.assertEqual(self.client.get("/store/cli/" + req["request_id"]).json()["status"], "refused")

    def test_bad_requests(self):
        for body in ({}, {"group": "LCG"}, {"group": "a/b"}, [], "x"):
            self.assertEqual(self.client.post("/store/cli/request", content=json.dumps(body)).status_code,
                             400, body)
        req = self._request()
        self.assertEqual(self.client.post("/store/cli/%s/result" % req["request_id"],
                                          content=b"{}").status_code, 400)
        self.assertEqual(self.client.get("/store/cli/nope").status_code, 404)

    def test_not_configured(self):
        main.settings = config.Settings(env={"BITS_ADMINS_POLICY": "lcg @alice"})
        self.assertEqual(self.client.post("/store/cli/request",
                                          content=json.dumps({"group": "lcg"})).status_code, 503)


if __name__ == "__main__":
    unittest.main()
