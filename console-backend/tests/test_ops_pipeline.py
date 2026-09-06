# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 3.1: /ops/pipeline/{id}/{cancel,retry} + DELETE — backend actuates the
lifecycle op with its own token, gated by admin rights for the PIPELINE's community
(resolved from the pipeline's COMMUNITY variable)."""

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
    # alice admins testbed; dave admins common; carol is overall (bits-)admin via '*'.
    main.settings = config.Settings(env={
        "GITLAB_API_URL": "https://gl/api/v4",
        "BITS_ADMINS_POLICY": "testbed @alice\ncommon @dave\n* @carol",
        "BITS_FORGE_OPS_TOKEN": "optok",
        "BITS_FORGE_PROJECT": "42",
    })
    main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None


def _vars(community):
    return FakeResp(200, [{"key": "COMMUNITY", "value": community}])


class TestOpsPipeline(unittest.TestCase):
    def setUp(self):
        _configure()
        self.client = TestClient(main.app, follow_redirects=False)

    def _hdr(self, user):
        return {"Authorization": "Bearer %s" % user} if user else {}

    def test_unauthenticated_401(self):
        self.assertEqual(self.client.post("/ops/pipeline/15/cancel").status_code, 401)

    @patch("console_backend.forge_ops.requests.request")
    @patch("console_backend.forge_ops.requests.get")
    def test_cancel_admin_of_pipeline_community(self, get, req):
        get.return_value = _vars("testbed")          # pipeline belongs to testbed
        req.return_value = FakeResp(200, {})
        r = self.client.post("/ops/pipeline/15/cancel", headers=self._hdr("alice"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(req.call_args.args[0], "POST")
        self.assertIn("/pipelines/15/cancel", req.call_args.args[1])
        self.assertEqual(req.call_args.kwargs["headers"].get("PRIVATE-TOKEN"), "optok")

    @patch("console_backend.forge_ops.requests.get")
    def test_cancel_denied_other_community(self, get):
        get.return_value = _vars("alice")            # alice admins testbed, NOT alice-community
        self.assertEqual(self.client.post("/ops/pipeline/15/cancel",
                                          headers=self._hdr("alice")).status_code, 403)

    @patch("console_backend.forge_ops.requests.get")
    def test_cancel_denied_plain_user(self, get):
        get.return_value = _vars("testbed")
        self.assertEqual(self.client.post("/ops/pipeline/15/cancel",
                                          headers=self._hdr("bob")).status_code, 403)

    @patch("console_backend.forge_ops.requests.request")
    @patch("console_backend.forge_ops.requests.get")
    def test_retry_authorized(self, get, req):
        get.return_value = _vars("testbed")
        req.return_value = FakeResp(200, {})
        self.assertEqual(self.client.post("/ops/pipeline/7/retry",
                                          headers=self._hdr("alice")).status_code, 200)

    @patch("console_backend.forge_ops.requests.request")
    @patch("console_backend.forge_ops.requests.get")
    def test_delete_bits_admin_any_community(self, get, req):
        get.return_value = _vars("cms")              # carol not cms admin, but overall
        req.return_value = FakeResp(200, {})
        r = self.client.delete("/ops/pipeline/9", headers=self._hdr("carol"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(req.call_args.args[0], "DELETE")

    @patch("console_backend.forge_ops.requests.get")
    def test_unresolvable_community_needs_overall_admin(self, get):
        get.return_value = FakeResp(200, [])         # no COMMUNITY var
        # a mere group-admin cannot act on an un-community'd pipeline...
        self.assertEqual(self.client.post("/ops/pipeline/3/cancel",
                                          headers=self._hdr("alice")).status_code, 403)

    @patch("console_backend.forge_ops.requests.get")
    def test_unresolvable_community_denies_common_admin(self, get):
        get.return_value = FakeResp(200, [])         # no COMMUNITY var
        # ...and, specifically, the 'common' admin must NOT slip through: an empty
        # community must not fall back to the 'common' group (would expose every
        # untagged Pages/push/legacy pipeline in the shared project to delete).
        self.assertEqual(self.client.request("DELETE", "/ops/pipeline/3",
                                             headers=self._hdr("dave")).status_code, 403)

    @patch("console_backend.forge_ops.requests.request")
    @patch("console_backend.forge_ops.requests.get")
    def test_unresolvable_community_ok_for_overall(self, get, req):
        get.return_value = FakeResp(200, [])
        req.return_value = FakeResp(200, {})
        # ...but the overall admin can.
        self.assertEqual(self.client.post("/ops/pipeline/3/cancel",
                                          headers=self._hdr("carol")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
