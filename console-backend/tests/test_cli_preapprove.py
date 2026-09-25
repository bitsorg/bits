# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""CLI pre-approval: a terminal asks for a build (deterministic build_id) to be
pre-approved, a human approves on another device with a passkey, and the backend
records an approved pre-approval that binds the build's packages."""

import json
import os
import shutil
import tempfile
import unittest

from fastapi.testclient import TestClient
from soft_webauthn import SoftWebauthnDevice

from console_backend import config, credentials, main, session, webauthn_rp
from test_cli_sign import ORIGIN, RP_ID, _assertion, _attestation

BID = "release_atlas_gcc14_opt_testbed-0123456789ab"
SHA = "ab" * 32


def _bom(arch="x86_64-el9-gcc14-opt", build_id=BID, pkgs=(("ROOT", "h1"), ("fmt", "h2"))):
    return {"build_id": build_id, "effective_architecture": arch,
            "packages": [{"package": n, "version": "1.0", "hash": h, "tarball_sha256": SHA}
                         for n, h in pkgs]}


class TestCliPreapprove(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "creds.json")
        main.settings = config.Settings(env={
            "BITS_WEBAUTHN_RP_ID": RP_ID, "BITS_WEBAUTHN_ORIGIN": ORIGIN,
            "BITS_WEBAUTHN_CREDENTIALS": path, "BITS_WEBAUTHN_REQUIRE_UV": "0",
            "BITS_ADMINS_POLICY": "lcg @alice"})
        main.credstore = credentials.CredentialStore(path)
        main.preapprovals = session.PreapprovalStore()
        main.cli_preapprovals = session.CliSignStore()
        self.client = TestClient(main.app, follow_redirects=False)
        self.device = SoftWebauthnDevice()
        opts_json, chal = webauthn_rp.registration_options(main.settings, "alice", [])
        cred = webauthn_rp.verify_registration(main.settings, _attestation(self.device, opts_json), chal)
        main.credstore.add("alice", cred)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _request(self, groups=("lcg",), boms=None, build_id=BID):
        body = {"build_id": build_id, "groups": list(groups), "host": "lxplus9", "user": "alice",
                "boms": boms if boms is not None else [_bom(), _bom("shared", pkgs=(("data", "h3"),))]}
        return self.client.post("/preapprove/cli/request", content=json.dumps(body))

    def _approve(self, rid, assertion=None):
        if assertion is None:
            pend = self.client.get("/preapprove/cli/" + rid).json()
            assertion = _assertion(self.device, pend["publicKey"])
        self.last = assertion
        return self.client.post("/preapprove/cli/%s/approve" % rid, json={"assertion": assertion})

    def test_full_flow_records_bound_preapproval(self):
        r = self._request()
        self.assertEqual(r.status_code, 200)
        rid, code = r.json()["request_id"], r.json()["code"]
        self.assertEqual(len(code), 6)
        self.assertEqual(r.json()["packages"], 3)
        self.assertEqual(self.client.get("/preapprove/cli/%s/result" % rid).json()["status"], "pending")
        pend = self.client.get("/preapprove/cli/" + rid).json()
        self.assertEqual((pend["code"], pend["build_id"], pend["verified"]), (code, BID, False))
        self.assertEqual(sorted(pend["architectures"]), ["shared", "x86_64-el9-gcc14-opt"])
        self.assertEqual(self._approve(rid).status_code, 200)
        res = self.client.get("/preapprove/cli/%s/result" % rid).json()
        self.assertEqual((res["status"], res["approved_by"]), ("approved", "alice"))
        pre = main.preapprovals.get(BID)
        self.assertEqual((pre["status"], pre["user"], pre["via"], pre["groups"]),
                         ("approved", "alice", "cli", ["lcg"]))
        self.assertIn(("shared", "h3", SHA, "data", "1.0"), [tuple(p) for p in pre["packages"]])
        # handled: a second approve is refused
        self.assertEqual(self._approve(rid, assertion=self.last).status_code, 400)
        self.assertEqual(self.client.get("/preapprove/cli/" + rid).json()["status"], "approved")

    def test_non_admin_denied_nothing_recorded(self):
        rid = self._request(groups=("atlas",)).json()["request_id"]
        self.assertEqual(self._approve(rid).status_code, 403)
        self.assertIsNone(main.preapprovals.get(BID))
        # the spent assertion ends the request
        self.assertEqual(self.client.get("/preapprove/cli/" + rid).json()["status"], "refused")

    def test_conflict_after_verify_ends_request(self):
        rid = self._request().json()["request_id"]
        main.preapprovals.put(BID, {"groups": ["lcg"], "user": "bob", "status": "approved"})
        self.assertEqual(self._approve(rid).status_code, 409)
        self.assertEqual(main.preapprovals.get(BID)["user"], "bob")
        self.assertEqual(self._approve(rid, assertion=self.last).status_code, 400)

    def test_unknown_credential_400(self):
        rid = self._request().json()["request_id"]
        self.assertEqual(self._approve(rid, assertion={"id": "nope", "rawId": "nope"}).status_code, 400)
        self.assertIsNone(main.preapprovals.get(BID))

    def test_already_preapproved_409(self):
        main.preapprovals.put(BID, {"groups": ["lcg"], "user": "alice", "status": "approved"})
        self.assertEqual(self._request().status_code, 409)

    def test_existing_pipeline_preapproval_untouched(self):
        # A console (pipeline-id) record is a different key and stays as it was.
        main.preapprovals.put("82836485", {"groups": ["lcg"], "user": "bob", "status": "pending"})
        rid = self._request().json()["request_id"]
        self.assertEqual(self._approve(rid).status_code, 200)
        self.assertEqual(main.preapprovals.get("82836485")["user"], "bob")

    def test_malformed_400(self):
        bad = [
            dict(boms=[]),
            dict(groups=()),
            dict(groups=("lcg/../x",)),
            dict(build_id="bad id"),
            # console pipeline ids are not CLI keys; the digest is lowercase hex
            dict(build_id="82836485", boms=[_bom(build_id="82836485")]),
            dict(build_id="release-0123456789AB", boms=[_bom(build_id="release-0123456789AB")]),
            dict(boms=[{"build_id": BID, "effective_architecture": "a",
                        "packages": [{"hash": "h", "tarball_sha256": "md5:" + SHA}]}]),
            dict(boms=[_bom(build_id="other-000000000000")]),
            dict(boms=[_bom(arch="x/../y")]),
            dict(boms=[{"build_id": BID, "effective_architecture": "a",
                        "packages": [{"hash": "h", "tarball_sha256": "nothex"}]}]),
        ]
        for kw in bad:
            self.assertEqual(self._request(**kw).status_code, 400, kw)
        self.assertEqual(self.client.post("/preapprove/cli/request", content=b"{").status_code, 400)

    def test_sha256_prefix_normalised(self):
        boms = [_bom()]
        boms[0]["packages"][0]["tarball_sha256"] = "sha256:" + SHA.upper()
        self.assertEqual(self._request(boms=boms).status_code, 200)

    def test_unknown_request_404(self):
        self.assertEqual(self.client.get("/preapprove/cli/nope").status_code, 404)
        self.assertEqual(self.client.get("/preapprove/cli/nope/result").status_code, 404)


if __name__ == "__main__":
    unittest.main()
