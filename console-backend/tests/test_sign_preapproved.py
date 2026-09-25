# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""F2/F4 tests: CI signs a pre-approved build. Requires a CI OIDC identity AND an
approved human pre-approval for the build_id; signs via the proxy, stamps
'pre-approved by X'. Bounded multi-use (one build signs once per arch), capped. The
proxy is faked with a real key."""

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
        main.ci_auth.verify_ci_token = lambda token, settings: {"project_path": "grp/manifests", "ref": "main",
                                                                "ref_protected": "true"}
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

    def test_ci_sign_with_preapproval_verifies_and_records(self):
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
        # bounded multi-use: the record persists (a build signs per-arch), sign counted
        rec = main.preapprovals.get("p1")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["signs"], 1)

    # CLI pre-approvals: bound to the build's packages and the MR author.
    CLI_BID = "rel-0123456789ab"
    SHA = "ab" * 32

    def _cli_record(self, user="alice"):
        main.preapprovals.put(self.CLI_BID, {
            "groups": ["lcg"], "user": user, "status": "approved", "via": "cli",
            "packages": [["x86_64-el9", "h1", self.SHA, "A", "1"],
                         ["shared", "h9", self.SHA, "D", "1"]]})

    def _cli_sign(self, certifier="alice", arch="x86_64-el9", pkgs=(("h1", SHA),)):
        body = json.dumps({"architecture": arch, "packages": [
            {"package": "A", "hash": h, "tarball_sha256": sha, "group": "lcg"}
            for h, sha in pkgs] + [{"package": "Other", "hash": "h2",
                                    "tarball_sha256": self.SHA, "group": "lcg"}]}).encode()
        q = "?build_id=" + self.CLI_BID + ("&certifier=" + certifier if certifier else "")
        ps = self._patched()
        for p in ps:
            p.start()
        try:
            return self.client.post("/sign/preapproved" + q, content=body, headers=self._ci())
        finally:
            for p in ps:
                p.stop()

    def test_cli_preapproval_signs_when_bound(self):
        self._cli_record()
        r = self._cli_sign()                       # certifier == approver
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["preapproved_by"], "alice")
        self.assertEqual(main.preapprovals.get(self.CLI_BID)["signs"], 1)

    def test_cli_preapproval_other_admin_certifier(self):
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gitlab.example/api/v4",
            "BITS_ADMINS_POLICY": "lcg @alice @carol",
            "BITS_SIGN_PROXY_URL": "http://proxy/sign/bits"})
        self._cli_record()
        self.assertEqual(self._cli_sign(certifier="carol").status_code, 200)

    def test_cli_preapproval_refusals_do_not_consume(self):
        self._cli_record()
        cases = [
            dict(certifier=""),                              # no MR author
            dict(certifier="mallory"),                       # not approver, not admin
            dict(pkgs=()),                                   # approved package missing
            dict(pkgs=(("h1", "cd" * 32),)),                 # approved package changed
            dict(arch="aarch64-el9"),                        # arch not approved
            dict(pkgs=(("h1", self.SHA), ("h1", "cd" * 32))),  # approved hash, second copy
            dict(arch="x\n86"),                              # malformed arch
        ]
        for kw in cases:
            r = self._cli_sign(**kw)
            self.assertEqual(r.status_code, 403, kw)
        self.assertNotIn("signs", main.preapprovals.get(self.CLI_BID))

    def test_cli_preapproval_needs_protected_ref(self):
        self._cli_record()
        main.ci_auth.verify_ci_token = lambda token, settings: {
            "project_path": "grp/manifests", "ref": "feature", "ref_protected": "false"}
        self.assertEqual(self._cli_sign().status_code, 403)

    def test_cli_preapproval_malformed_manifest_is_403(self):
        self._cli_record()
        ps = self._patched()
        for p in ps:
            p.start()
        try:
            r = self.client.post("/sign/preapproved?build_id=%s&certifier=alice" % self.CLI_BID,
                                 content=json.dumps({"architecture": "x86_64-el9", "packages": [
                                     {"hash": ["x"], "group": "lcg"}]}).encode(), headers=self._ci())
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r.status_code, 403)

    def test_cli_preapproval_sha256_prefix_matches(self):
        self._cli_record()
        self.assertEqual(self._cli_sign(pkgs=(("h1", "sha256:" + self.SHA.upper()),)).status_code, 200)

    def test_console_preapproval_ignores_certifier(self):
        # Pipeline-id (console) records are unchanged: no binding, certifier unused.
        self._preapprove("p1", ["lcg"])
        ps = self._patched()
        for p in ps:
            p.start()
        try:
            r = self.client.post("/sign/preapproved?build_id=p1&certifier=mallory",
                                 content=_manifest("lcg"), headers=self._ci())
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r.status_code, 200)

    def test_no_preapproval_403(self):
        r = self._sign("nope", "lcg")
        self.assertEqual(r.status_code, 403)
        self.assertIn("no human pre-approval", r.json()["detail"])

    def test_group_outside_preapprover_authority_403(self):
        # alice admins lcg only (setUp policy); a manifest with a common package is
        # outside her authority -> 403, and the approval is NOT consumed.
        self._preapprove("p1", ["lcg"], user="alice")
        r = self._sign("p1", "common")
        self.assertEqual(r.status_code, 403)
        self.assertIn("is not an admin for", r.json()["detail"])
        self.assertIsNotNone(main.preapprovals.get("p1"))

    def test_cross_group_within_overall_admin_authority_signs(self):
        # An OVERALL admin's pre-approval covers manifest groups NOT in the declared
        # list (a cross-group build the SPA couldn't foresee) — it still signs.
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gitlab.example/api/v4",
            "BITS_ADMINS_POLICY": "* @alice",            # overall admin
            "BITS_SIGN_PROXY_URL": "http://proxy/sign/bits"})
        self._preapprove("p1", ["testbed"], user="alice")   # declared: testbed only
        r = self._sign("p1", "alice")                        # manifest carries group 'alice'
        self.assertEqual(r.status_code, 200)

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

    def test_failed_sign_releases_slot(self):
        self._preapprove("p1", ["lcg"])
        # deny via key policy -> _do_sign raises inside the try -> reserved slot released
        ps = self._patched(policy={"default": []})
        for p in ps:
            p.start()
        try:
            r = self.client.post("/sign/preapproved?build_id=p1",
                                 content=_manifest("lcg"), headers=self._ci())
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r.status_code, 403)
        self.assertEqual(main.preapprovals.get("p1").get("signs", 0), 0)   # budget not burned
        self.assertEqual(self._sign("p1", "lcg").status_code, 200)          # good sign still works
        self.assertEqual(main.preapprovals.get("p1")["signs"], 1)

    def test_missing_build_id_400(self):
        r = self.client.post("/sign/preapproved", content=_manifest("lcg"), headers=self._ci())
        self.assertEqual(r.status_code, 400)

    def test_bounded_multi_use_then_cap(self):
        # A build signs one manifest per arch, so the same pre-approval signs several
        # times — up to a cap, then 429.
        self._preapprove("p1", ["lcg"])
        cap = main._PREAPPROVAL_SIGN_CAP
        for _ in range(cap):
            self.assertEqual(self._sign("p1", "lcg").status_code, 200)
        self.assertEqual(self._sign("p1", "lcg").status_code, 429)   # cap reached
        self.assertEqual(main.preapprovals.get("p1")["signs"], cap)


if __name__ == "__main__":
    unittest.main()
