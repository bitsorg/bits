# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""F2 tests: CI signs a pre-approved build. Requires a CI OIDC identity AND an
approved human pre-approval for the build_id; signs via the proxy, stamps
'pre-approved by X', consumes the record. The proxy is faked with a real key."""

import json
import os
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from console_backend import config, main, session
from bits_helpers import trust

CI_TOKEN = "aaa.bbb.ccc"   # JWT-shaped (two dots) -> the CI path in _authorize_sign


def _manifest(group="lcg"):
    return json.dumps({"architecture": "slc7_x86-64",
                       "packages": [{"package": "A", "hash": "h1", "group": group}]}).encode()


class TestSignPreapproved(unittest.TestCase):
    def setUp(self):
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gitlab.example/api/v4",
            "BITS_ADMINS_POLICY": "lcg @alice",
            "BITS_SIGN_PROXY_URL": "http://proxy/sign/bits"})
        main.preapprovals = session.PreapprovalStore()
        # CI identity: JWT verifies to a project; authorize everything (override per test).
        main.ci_auth.verify_ci_token = lambda token, settings: {"project_path": "grp/manifests", "ref": "main"}
        main.ci_auth.load_ci_signers = lambda settings: {}
        main.ci_auth.is_ci_authorized = lambda project, group, pol: True
        main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None
        self.priv = Ed25519PrivateKey.generate()
        self.client = TestClient(main.app, follow_redirects=False)
        os.environ["BITS_SIGN_PROXY_TOKEN"] = "gate"

    def tearDown(self):
        os.environ.pop("BITS_SIGN_PROXY_TOKEN", None)

    def _ci(self):
        return {"Authorization": "Bearer " + CI_TOKEN}

    def _patched(self, policy=None):
        return [
            patch.object(main.trust, "sign_bytes_via_proxy",
                         lambda data, url, tok: trust.sign_bytes(data, self.priv)),
            patch.object(main.trust, "proxy_pubkey",
                         lambda url, tok: (trust.key_id(self.priv.public_key()),
                                           self.priv.public_key())),
            patch.object(main.trust, "load_key_policy", return_value=policy),
        ]

    def _preapprove(self, build_id="p1", groups=("lcg",), status="approved", user="alice"):
        main.preapprovals.put(build_id, {"groups": list(groups), "user": user,
                                         "challenge": b"x", "status": status})

    def _sign(self, build_id="p1", group="lcg"):
        ps = self._patched()
        for p in ps:
            p.start()
        try:
            return self.client.post("/sign/preapproved?build_id=" + build_id,
                                    content=_manifest(group), headers=self._ci())
        finally:
            for p in ps:
                p.stop()

    def test_ci_sign_with_preapproval_verifies_and_consumes(self):
        body = _manifest("lcg")
        self._preapprove("p1", ["lcg"])
        r = self._sign("p1", "lcg")
        self.assertEqual(r.status_code, 200)
        out = r.json()
        trusted = {trust.key_id(self.priv.public_key()): self.priv.public_key()}
        self.assertEqual(trust.verify_bytes(body, out["envelope"], trusted),
                         trust.key_id(self.priv.public_key()))
        self.assertEqual(out["preapproved_by"], "alice")
        self.assertEqual(out["approval"], "preapproved")
        # provenance is advisory (NOT inside the signed envelope)
        self.assertNotIn("approved_by", out["envelope"])
        # single-use: the pre-approval is consumed
        self.assertIsNone(main.preapprovals.get("p1"))

    def test_no_preapproval_403(self):
        r = self._sign("nope", "lcg")
        self.assertEqual(r.status_code, 403)
        self.assertIn("no human pre-approval", r.json()["detail"])

    def test_group_not_covered_by_preapproval_403(self):
        # pre-approval only covers lcg; manifest is a common package -> mismatch
        self._preapprove("p1", ["lcg"])
        r = self._sign("p1", "common")
        self.assertEqual(r.status_code, 403)
        self.assertIn("does not cover", r.json()["detail"])
        # the approval was NOT consumed (still available for the right manifest)
        self.assertIsNotNone(main.preapprovals.get("p1"))

    def test_human_bearer_rejected(self):
        # a non-JWT bearer is a human token -> this endpoint is CI-only
        self._preapprove("p1", ["lcg"])
        ps = self._patched()
        for p in ps:
            p.start()
        try:
            r = self.client.post("/sign/preapproved?build_id=p1",
                                 content=_manifest("lcg"),
                                 headers={"Authorization": "Bearer alice"})
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r.status_code, 403)
        self.assertIn("CI identity", r.json()["detail"])

    def test_ci_not_authorized_for_group_403(self):
        main.ci_auth.is_ci_authorized = lambda project, group, pol: False
        self._preapprove("p1", ["lcg"])
        r = self._sign("p1", "lcg")
        self.assertEqual(r.status_code, 403)

    def test_missing_build_id_400(self):
        r = self.client.post("/sign/preapproved", content=_manifest("lcg"), headers=self._ci())
        self.assertEqual(r.status_code, 400)

    def test_single_use_second_call_403(self):
        self._preapprove("p1", ["lcg"])
        self.assertEqual(self._sign("p1", "lcg").status_code, 200)
        self.assertEqual(self._sign("p1", "lcg").status_code, 403)   # consumed


if __name__ == "__main__":
    unittest.main()
