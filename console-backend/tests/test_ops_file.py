# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 3.3: /ops/file — the backend commits a schedule file with its own token
after checking the caller admins the community the PATH belongs to. The path is
pinned to communities/<community>/pipelines/<name>.json so a community-A admin can
never write B's tree or any other repo file, and the branch is pinned to the
allowlist."""

import base64
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
    main.settings = config.Settings(env={
        "GITLAB_API_URL": "https://gl/api/v4",
        "BITS_ADMINS_POLICY": "alice @alice\n* @carol",
        "BITS_FORGE_OPS_TOKEN": "optok",
        "BITS_FORGE_PROJECT": "42",
    })
    main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None


B64 = base64.b64encode(b"{}\n").decode()
PATH = "communities/alice/pipelines/nightly.json"


class TestOpsFile(unittest.TestCase):
    def setUp(self):
        _configure()
        self.client = TestClient(main.app, follow_redirects=False)

    def _hdr(self, user):
        return {"Authorization": "Bearer %s" % user} if user else {}

    def _put(self, user, path=PATH, content=B64, branch="main"):
        body = {"path": path, "content": content, "branch": branch, "message": "m"}
        return self.client.post("/ops/file", headers=self._hdr(user), json=body)

    def test_unauthenticated_401(self):
        self.assertEqual(self._put(None).status_code, 401)

    @patch("console_backend.forge_ops.requests.request")
    @patch("console_backend.forge_ops.requests.get")
    def test_put_create_when_absent(self, get, req):
        get.return_value = FakeResp(404)                 # file does not exist yet
        req.return_value = FakeResp(201, {})
        r = self._put("alice")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(req.call_args.args[0], "POST")  # create
        self.assertIn("/repository/files/", req.call_args.args[1])
        self.assertEqual(req.call_args.kwargs["headers"].get("PRIVATE-TOKEN"), "optok")
        self.assertEqual(req.call_args.kwargs["json"]["encoding"], "base64")

    @patch("console_backend.forge_ops.requests.request")
    @patch("console_backend.forge_ops.requests.get")
    def test_put_update_when_present(self, get, req):
        get.return_value = FakeResp(200, {"file_path": "x"})  # already exists
        req.return_value = FakeResp(200, {})
        self.assertEqual(self._put("alice").status_code, 200)
        self.assertEqual(req.call_args.args[0], "PUT")   # update

    def test_put_denied_other_community(self):
        # alice admins 'alice', not 'cms' — path community is authoritative
        self.assertEqual(self._put("alice", path="communities/cms/pipelines/n.json").status_code, 403)

    def test_put_denied_plain_user(self):
        self.assertEqual(self._put("bob").status_code, 403)

    @patch("console_backend.forge_ops.requests.request")
    @patch("console_backend.forge_ops.requests.get")
    def test_bits_admin_any_community(self, get, req):
        get.return_value = FakeResp(404)
        req.return_value = FakeResp(201, {})
        self.assertEqual(self._put("carol", path="communities/cms/pipelines/n.json").status_code, 200)

    def test_path_must_be_pinned(self):
        for bad in ("communities/alice/other/n.json",       # not pipelines/
                    "communities/alice/pipelines/n.yaml",   # not .json
                    ".gitlab-ci.yml",                        # arbitrary repo file
                    "communities/alice/pipelines/../../x.json",  # traversal
                    "communities/ /pipelines/n.json",        # empty/space community -> NOT 'common'
                    "communities/alice/pipelines/n.json\nEVIL.json",  # trailing-newline ride
                    "communities/a/b/pipelines/n.json"):     # encoded/extra separator
            self.assertEqual(self._put("carol", path=bad).status_code, 400, bad)

    def test_branch_must_be_allowed(self):
        self.assertEqual(self._put("alice", branch="wip").status_code, 400)

    def test_missing_content_400(self):
        r = self.client.post("/ops/file", headers=self._hdr("alice"),
                             json={"path": PATH, "branch": "main"})
        self.assertEqual(r.status_code, 400)

    @patch("console_backend.forge_ops.requests.request")
    def test_delete_authorized(self, req):
        req.return_value = FakeResp(200, {})
        r = self.client.request("DELETE", "/ops/file", headers=self._hdr("alice"),
                                json={"path": PATH, "branch": "main"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(req.call_args.args[0], "DELETE")

    def test_delete_denied_other_community(self):
        r = self.client.request("DELETE", "/ops/file", headers=self._hdr("alice"),
                                json={"path": "communities/cms/pipelines/n.json"})
        self.assertEqual(r.status_code, 403)

    @patch("console_backend.forge_ops.requests.request")
    @patch("console_backend.forge_ops.requests.get")
    def test_path_community_is_case_insensitive(self, get, req):
        get.return_value = FakeResp(404)
        req.return_value = FakeResp(201, {})
        # dir case is ALICE; authz lowercases to 'alice'
        self.assertEqual(self._put("alice", path="communities/ALICE/pipelines/n.json").status_code, 200)


if __name__ == "__main__":
    unittest.main()
