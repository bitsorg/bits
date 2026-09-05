# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 1: POST /ops/build — the backend triggers a build with its own forge token,
gated by the caller's identity + admin rights for the TARGET community."""

import unittest
from unittest.mock import patch

import requests
from fastapi.testclient import TestClient

from console_backend import config, forge_ops, main


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _configure(policy):
    # verify_gitlab_token stubbed so the bearer string IS the username.
    main.settings = config.Settings(env={
        "GITLAB_API_URL": "https://gl/api/v4",
        "BITS_ADMINS_POLICY": policy,
        "BITS_FORGE_OPS_TOKEN": "optok",
        "BITS_FORGE_PROJECT": "42",
    })
    main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None


class TestOpsBuild(unittest.TestCase):
    def setUp(self):
        # alice admins 'testbed'; carol is overall (bits-)admin via '*'.
        _configure("testbed @alice\n* @carol")
        self.client = TestClient(main.app, follow_redirects=False)

    def _post(self, user, community="testbed", extra=None):
        body = {"community": community, "ref": "main",
                "variables": [{"key": "COMMUNITY", "value": community}]}
        if extra:
            body.update(extra)
        headers = {"Authorization": "Bearer %s" % user} if user else {}
        return self.client.post("/ops/build", headers=headers, json=body)

    # ---- authorization matrix ------------------------------------------------
    def test_unauthenticated_401(self):
        self.assertEqual(self._post(None).status_code, 401)

    @patch("console_backend.forge_ops.requests.post")
    def test_group_admin_of_target_allowed(self, post):
        post.return_value = FakeResp(201, {"id": 777, "web_url": "u", "status": "created"})
        r = self._post("alice", community="testbed")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["id"], 777)
        # actuated with the backend's token, on the configured project
        self.assertIn("/projects/42/pipeline", post.call_args.args[0])
        self.assertEqual(post.call_args.kwargs["headers"].get("PRIVATE-TOKEN"), "optok")

    def test_group_admin_of_other_community_denied(self):
        # alice admins testbed, NOT alice-community — per-community scoping
        self.assertEqual(self._post("alice", community="alice").status_code, 403)

    def test_plain_user_denied(self):
        self.assertEqual(self._post("bob", community="testbed").status_code, 403)

    @patch("console_backend.forge_ops.requests.post")
    def test_bits_admin_any_community(self, post):
        post.return_value = FakeResp(201, {"id": 9, "web_url": "u", "status": "created"})
        self.assertEqual(self._post("carol", community="testbed").status_code, 200)
        self.assertEqual(self._post("carol", community="cms").status_code, 200)

    # ---- input + wiring ------------------------------------------------------
    def test_bad_payload_400(self):
        r = self.client.post("/ops/build", headers={"Authorization": "Bearer alice"},
                             json={"ref": "main"})   # missing community
        self.assertEqual(r.status_code, 400)

    def test_bad_ref_400(self):
        self.assertEqual(self._post("alice", extra={"ref": "bad ref;rm"}).status_code, 400)

    def test_community_variable_mismatch_400(self):
        # alice admins testbed; authorize as testbed but try to build as alice via
        # the COMMUNITY variable → refused before anything is triggered.
        r = self._post("alice", community="testbed",
                       extra={"variables": [{"key": "COMMUNITY", "value": "alice"}]})
        self.assertEqual(r.status_code, 400)

    def test_missing_community_variable_400(self):
        # No COMMUNITY variable at all → refused (can't be left unscoped).
        r = self._post("alice", community="testbed", extra={"variables": []})
        self.assertEqual(r.status_code, 400)

    def test_disallowed_ref_400(self):
        # Valid charset but not an allowed build ref (only 'main' by default).
        r = self._post("alice", extra={"ref": "feature/x"})
        self.assertEqual(r.status_code, 400)

    @patch("console_backend.forge_ops.requests.post")
    def test_matching_community_variable_ok(self, post):
        post.return_value = FakeResp(201, {"id": 5, "web_url": "u", "status": "created"})
        r = self._post("alice", community="testbed",
                       extra={"variables": [{"key": "COMMUNITY", "value": "Testbed"}]})
        self.assertEqual(r.status_code, 200)   # case-insensitive match

    def test_503_when_forge_unconfigured(self):
        main.settings = config.Settings(env={"GITLAB_API_URL": "https://gl/api/v4",
                                             "BITS_ADMINS_POLICY": "testbed @alice"})
        main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None
        self.assertEqual(self._post("alice").status_code, 503)

    @patch("console_backend.forge_ops.requests.post")
    def test_forge_error_502(self, post):
        post.return_value = FakeResp(403, {"message": "insufficient scope"})
        self.assertEqual(self._post("alice").status_code, 502)


if __name__ == "__main__":
    unittest.main()
