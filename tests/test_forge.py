# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the forge approval gate (bits_helpers/forge.py)."""

import os
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers import forge


class TestGitLabForge(unittest.TestCase):

    def test_from_env_needs_all_vars(self):
        self.assertIsNone(forge.GitLabForge.from_env({}))
        self.assertIsNone(forge.GitLabForge.from_env(
            {"CI_API_V4_URL": "u", "CI_PROJECT_ID": "1"}))  # missing iid/token

    def test_from_env_builds_and_parses_open_mr_form(self):
        gl = forge.GitLabForge.from_env({
            "CI_API_V4_URL": "https://gl/api/v4",
            "CI_PROJECT_ID": "42",
            "CI_OPEN_MERGE_REQUESTS": "grp/proj!9,other!3",
            "BITS_FORGE_TOKEN": "tok",
        })
        self.assertEqual(gl.mr_iid, "9")
        self.assertEqual(gl.context(), "42 MR !9")

    def test_list_approvers_parses_api_payload(self):
        gl = forge.GitLabForge("https://gl/api/v4", "42", "7", "tok")

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"approved_by": [
                    {"user": {"username": "alice"}},
                    {"user": {"username": "bob"}},
                    {"user": {}},          # tolerate malformed entry
                ]}

        with patch("requests.get", return_value=_Resp()) as g:
            self.assertEqual(gl.list_approvers(), {"alice", "bob"})
            # sent to the approvals endpoint with the token header
            args, kwargs = g.call_args
            self.assertTrue(args[0].endswith("/merge_requests/7/approvals"))
            self.assertEqual(kwargs["headers"]["PRIVATE-TOKEN"], "tok")


    def test_resolves_mr_from_merged_commit_on_branch_pipeline(self):
        # No MR iid (post-merge main pipeline) -> resolve via the commit.
        gl = forge.GitLabForge.from_env({
            "CI_API_V4_URL": "https://gl/api/v4", "CI_PROJECT_ID": "42",
            "CI_COMMIT_SHA": "deadbeef", "BITS_FORGE_TOKEN": "tok"})
        self.assertIsNone(gl.mr_iid)

        class _Resp:
            def __init__(self, payload):
                self._p = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self._p

        def _fake_get(url, headers=None, timeout=None):
            if url.endswith("/repository/commits/deadbeef/merge_requests"):
                return _Resp([{"iid": 5, "state": "merged"}])
            if url.endswith("/merge_requests/5/approvals"):
                return _Resp({"approved_by": [{"user": {"username": "alice"}}]})
            raise AssertionError("unexpected URL: %s" % url)

        with patch("requests.get", side_effect=_fake_get):
            self.assertEqual(gl.list_approvers(), {"alice"})
        self.assertEqual(gl.context(), "42 MR !5")


    def test_unmerged_mr_is_not_used_for_certification(self):
        gl = forge.GitLabForge.from_env({
            "CI_API_V4_URL": "https://gl/api/v4", "CI_PROJECT_ID": "42",
            "CI_COMMIT_SHA": "deadbeef", "BITS_FORGE_TOKEN": "tok"})

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return [{"iid": 5, "state": "opened"}]      # not merged

        def _fake_get(url, headers=None, timeout=None):
            if url.endswith("/repository/commits/deadbeef/merge_requests"):
                return _Resp()
            raise AssertionError("must not read approvals for an unmerged MR: %s" % url)

        with patch("requests.get", side_effect=_fake_get):
            self.assertEqual(gl.list_approvers(), set())   # fail-closed


class TestAdminPolicy(unittest.TestCase):

    def test_legacy_flat_list_is_overall(self):
        self.assertEqual(forge.load_admin_policy("@alice\n@bob\n"),
                         {"*": {"alice", "bob"}})

    def test_overall_and_per_group(self):
        p = forge.load_admin_policy("* @sup\nlcg @a @b\ncommon @c  # base\n")
        self.assertEqual(p["*"], {"sup"})
        self.assertEqual(p["lcg"], {"a", "b"})
        self.assertEqual(p["common"], {"c"})

    def test_approved_for_group_overall_covers_all(self):
        p = {"*": {"sup"}, "lcg": {"a"}}
        self.assertTrue(forge.approved_for_group(["sup"], p, "ship"))   # overall
        self.assertTrue(forge.approved_for_group(["a"], p, "lcg"))      # group admin
        self.assertFalse(forge.approved_for_group(["a"], p, "ship"))    # wrong group
        self.assertTrue(forge.approved_for_group(["sup"], p, None))     # untagged=common

    def test_verify_group_approval_reports_unmet(self):
        policy = {"lcg": {"a"}}
        fg = forge.StaticForge(["a"])
        ok, _, unmet = forge.verify_group_approval(fg, policy, {"lcg", "ship"})
        self.assertFalse(ok)
        self.assertEqual(unmet, ["ship"])
        ok2, _, unmet2 = forge.verify_group_approval(fg, policy, {"lcg"})
        self.assertTrue(ok2)
        self.assertEqual(unmet2, [])

    def test_group_refs_parsed_and_listed(self):
        p = forge.load_admin_policy("* &cern-bits-admins @backup\nlcg &alice-egroup\n")
        self.assertEqual(p["*"], {"&cern-bits-admins", "backup"})
        self.assertEqual(p["lcg"], {"&alice-egroup"})
        self.assertEqual(forge.admin_policy_grouprefs(p),
                         {"cern-bits-admins", "alice-egroup"})

    def test_resolve_expands_groups_keeps_literals(self):
        p = forge.load_admin_policy("* &grp @backup\n")
        resolved = forge.resolve_admin_policy(
            p, lambda ref: {"Alice", "Bob"} if ref == "grp" else None)
        self.assertEqual(resolved["*"], {"alice", "bob", "backup"})

    def test_resolve_failure_falls_back_to_literals(self):
        p = forge.load_admin_policy("* &grp @backup\n")
        resolved = forge.resolve_admin_policy(p, lambda ref: None)   # API failed
        self.assertEqual(resolved["*"], {"backup"})   # literal override still works

    def test_resolved_policy_authorises_group_member(self):
        p = forge.load_admin_policy("lcg &alice-egroup\n")
        resolved = forge.resolve_admin_policy(p, lambda ref: {"eulisse"})
        self.assertTrue(forge.approved_for_group(["eulisse"], resolved, "lcg"))
        self.assertFalse(forge.approved_for_group(["mallory"], resolved, "lcg"))

    def test_gitlab_group_members_paginates(self):
        pages = {1: [{"username": "a"}, {"username": "b"}], 2: []}

        class _Resp:
            def __init__(self, page):
                self._p = pages.get(page, [])

            def raise_for_status(self):
                pass

            def json(self):
                return self._p

        def _get(url, headers=None, params=None, timeout=None):
            self.assertIn("/groups/cern%2Fadmins/members/all", url)
            return _Resp(params["page"])

        with patch("requests.get", side_effect=_get):
            # first page has <100 -> single request returns both
            self.assertEqual(forge.gitlab_group_members("https://gl/api/v4", "tok", "cern/admins"),
                             {"a", "b"})


class TestTriggerPrimitives(unittest.TestCase):

    def test_parse_git_remote_forms(self):
        api, proj = forge.parse_git_remote("ssh://git@gitlab.cern.ch:7999/buncic/bits-manifests.git")
        self.assertEqual((api, proj), ("https://gitlab.cern.ch/api/v4", "buncic/bits-manifests"))
        self.assertEqual(forge.parse_git_remote("git@gitlab.cern.ch:buncic/bits-manifests.git"),
                         ("https://gitlab.cern.ch/api/v4", "buncic/bits-manifests"))
        self.assertEqual(forge.parse_git_remote("https://gitlab.cern.ch/buncic/bits-manifests.git"),
                         ("https://gitlab.cern.ch/api/v4", "buncic/bits-manifests"))
        self.assertEqual(forge.parse_git_remote("garbage"), (None, None))

    def test_resolve_token_precedence(self):
        with patch.dict(os.environ, {"BITS_CERTIFIER_TOKEN": "envtok"}, clear=False):
            self.assertEqual(forge.resolve_gitlab_token(), "envtok")
            self.assertEqual(forge.resolve_gitlab_token("explicit"), "explicit")

    def test_resolve_token_from_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".tok", delete=False) as fh:
            fh.write("# my token\nglpat-ABC123\n")
            path = fh.name
        try:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("BITS_CERTIFIER_TOKEN", "BITS_GITLAB_TOKEN", "GITLAB_TOKEN")}
            env["BITS_GITLAB_TOKEN_FILE"] = path
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(forge.resolve_gitlab_token(), "glpat-ABC123")
        finally:
            os.remove(path)

    def test_create_pipeline_posts_ref_and_returns_url(self):
        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"id": 42, "web_url": "https://gl/pipelines/42"}

        seen = {}

        def _post(url, headers=None, json=None, timeout=None):
            seen["url"] = url
            seen["ref"] = json.get("ref")
            seen["token"] = headers["PRIVATE-TOKEN"]
            return _Resp()

        with patch("requests.post", side_effect=_post):
            out = forge.gitlab_create_pipeline("https://gl/api/v4", "tok",
                                               "buncic/bits-manifests", ref="main")
        self.assertEqual(out, {"id": 42, "web_url": "https://gl/pipelines/42"})
        self.assertTrue(seen["url"].endswith("/projects/buncic%2Fbits-manifests/pipeline"))
        self.assertEqual(seen["ref"], "main")
        self.assertEqual(seen["token"], "tok")


class TestMRPrimitives(unittest.TestCase):

    class _Resp:
        def __init__(self, payload):
            self._p = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._p

    def test_create_commit_posts_actions(self):
        seen = {}

        def _post(url, headers=None, json=None, timeout=None):
            seen.update(url=url, payload=json)
            return self._Resp({"id": "abc"})

        with patch("requests.post", side_effect=_post):
            forge.gitlab_create_commit("https://gl/api/v4", "tok", "grp/proj",
                                       "certify/x", "main", "manifests/ship/x.json",
                                       '{"packages":[]}', "certify ship")
        self.assertTrue(seen["url"].endswith("/projects/grp%2Fproj/repository/commits"))
        self.assertEqual(seen["payload"]["branch"], "certify/x")
        self.assertEqual(seen["payload"]["start_branch"], "main")
        self.assertEqual(seen["payload"]["actions"][0]["file_path"], "manifests/ship/x.json")

    def test_create_merge_request_returns_iid_url(self):
        with patch("requests.post", return_value=self._Resp({"iid": 7, "web_url": "u"})):
            out = forge.gitlab_create_merge_request("https://gl/api/v4", "tok", "grp/proj",
                                                    "certify/x", "main", "Certify x")
        self.assertEqual(out, {"iid": 7, "web_url": "u"})

    def test_mr_author(self):
        with patch("requests.get", return_value=self._Resp({"author": {"username": "buncic"}})):
            self.assertEqual(forge.gitlab_mr_author("https://gl/api/v4", "tok", "p", 7), "buncic")

    def test_mr_iid_for_commit_prefers_merged(self):
        payload = [{"iid": 3, "state": "opened"}, {"iid": 5, "state": "merged"}]
        with patch("requests.get", return_value=self._Resp(payload)):
            self.assertEqual(forge.gitlab_mr_iid_for_commit("https://gl/api/v4", "t", "p", "sha"), 5)


if __name__ == "__main__":
    unittest.main()
