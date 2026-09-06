# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 3.4: /ops/admin/* — infrastructure ops not scoped to a community (set the
BITS_DESIRED_REF CI variable; trigger the builder-image refresh). Overall (bits-)
admin only; the CI-variable key is allowlisted so it can't write arbitrary vars."""

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, main


class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._p = payload if payload is not None else {}
        self.text = str(payload)

    def json(self):
        return self._p


def _configure():
    # alice admins 'alice' (group-admin, NOT overall); carol is overall via '*'.
    main.settings = config.Settings(env={
        "GITLAB_API_URL": "https://gl/api/v4",
        "BITS_ADMINS_POLICY": "alice @alice\n* @carol",
        "BITS_FORGE_OPS_TOKEN": "optok",
        "BITS_FORGE_PROJECT": "42",
    })
    main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None


class TestOpsAdmin(unittest.TestCase):
    def setUp(self):
        _configure()
        self.client = TestClient(main.app, follow_redirects=False)

    def _hdr(self, user):
        return {"Authorization": "Bearer %s" % user} if user else {}

    # ---- ci-var --------------------------------------------------------------
    def test_ci_var_unauthenticated_401(self):
        r = self.client.post("/ops/admin/ci-var", json={"key": "BITS_DESIRED_REF", "value": "v"})
        self.assertEqual(r.status_code, 401)

    def test_ci_var_group_admin_denied(self):
        # alice is a group-admin, not an overall admin — infra ops need overall.
        r = self.client.post("/ops/admin/ci-var", headers=self._hdr("alice"),
                             json={"key": "BITS_DESIRED_REF", "value": "v"})
        self.assertEqual(r.status_code, 403)

    @patch("console_backend.forge_ops.requests.request")
    def test_ci_var_update_when_present(self, req):
        req.return_value = FakeResp(200, {})
        r = self.client.post("/ops/admin/ci-var", headers=self._hdr("carol"),
                             json={"key": "BITS_DESIRED_REF", "value": "v2.1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(req.call_args.args[0], "PUT")
        self.assertIn("/variables/BITS_DESIRED_REF", req.call_args.args[1])
        self.assertEqual(req.call_args.kwargs["headers"].get("PRIVATE-TOKEN"), "optok")

    @patch("console_backend.forge_ops.requests.request")
    def test_ci_var_create_when_absent(self, req):
        req.side_effect = [FakeResp(404), FakeResp(201, {})]   # PUT 404 -> POST
        r = self.client.post("/ops/admin/ci-var", headers=self._hdr("carol"),
                             json={"key": "BITS_DESIRED_REF", "value": ""})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(req.call_args.args[0], "POST")        # last call created it

    def test_ci_var_key_not_allowlisted(self):
        r = self.client.post("/ops/admin/ci-var", headers=self._hdr("carol"),
                             json={"key": "BITS_FORGE_OPS_TOKEN", "value": "steal"})
        self.assertEqual(r.status_code, 400)

    # ---- refresh -------------------------------------------------------------
    def test_refresh_group_admin_denied(self):
        r = self.client.post("/ops/admin/refresh", headers=self._hdr("alice"), json={})
        self.assertEqual(r.status_code, 403)

    @patch("console_backend.forge_ops.requests.post")
    def test_refresh_overall_admin(self, post):
        post.return_value = FakeResp(201, {"id": 5, "web_url": "u", "status": "created"})
        r = self.client.post("/ops/admin/refresh", headers=self._hdr("carol"),
                             json={"tag": "bits-host-a"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], 5)
        sent = post.call_args.kwargs["json"]["variables"]
        keys = {v["key"]: v["value"] for v in sent}
        self.assertEqual(keys.get("REFRESH_IMAGES_JOB"), "true")
        self.assertEqual(keys.get("BITS_RUNNER_TAG"), "bits-host-a")

    @patch("console_backend.forge_ops.requests.post")
    def test_refresh_no_tag_all_hosts(self, post):
        post.return_value = FakeResp(201, {"id": 6, "web_url": "u", "status": "created"})
        r = self.client.post("/ops/admin/refresh", headers=self._hdr("carol"), json={})
        self.assertEqual(r.status_code, 200)
        keys = {v["key"] for v in post.call_args.kwargs["json"]["variables"]}
        self.assertIn("REFRESH_IMAGES_JOB", keys)
        self.assertNotIn("BITS_RUNNER_TAG", keys)   # no tag -> not pinned


if __name__ == "__main__":
    unittest.main()
