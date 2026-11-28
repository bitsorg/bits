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

    def test_an_open_mr_is_never_taken_from_the_environment(self):
        # NEGATIVE CONTROL: read CI_OPEN_MERGE_REQUESTS again and this has an iid.
        env = {"CI_API_V4_URL": "https://gl/api/v4", "CI_PROJECT_ID": "42",
               "CI_OPEN_MERGE_REQUESTS": "grp/proj!9,other!3",
               "CI_MERGE_REQUEST_IID": "9", "BITS_FORGE_TOKEN": "tok"}
        self.assertIsNone(forge.GitLabForge.from_env(env))      # no commit: nothing
        gl = forge.GitLabForge.from_env(dict(env, CI_COMMIT_SHA="deadbeef"))
        self.assertIsNone(gl.mr_iid)                             # resolved from the commit

    def test_a_given_mr_counts_only_once_merged(self):
        gl = forge.GitLabForge("https://gl/api/v4", "42", "7", "tok")

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"iid": 7, "state": "opened"}

        def _fake_get(url, headers=None, timeout=None):
            if url.endswith("/merge_requests/7"):
                return _Resp()
            raise AssertionError("must not read approvals of an open MR: %s" % url)

        with patch("requests.get", side_effect=_fake_get):
            self.assertEqual(gl.list_approvers(), set())

    def test_list_approvers_parses_api_payload(self):
        gl = forge.GitLabForge("https://gl/api/v4", "42", "7", "tok")

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"state": "merged", "approved_by": [
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
            "CI_COMMIT_SHA": "deadbeef", "CI_COMMIT_BRANCH": "main",
            "BITS_FORGE_TOKEN": "tok"})
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
                return _Resp([
                    # merged elsewhere, or contains the commit without merging it: not these
                    {"iid": 3, "state": "merged", "target_branch": "dev",
                     "merge_commit_sha": "deadbeef"},
                    {"iid": 4, "state": "merged", "target_branch": "main",
                     "merge_commit_sha": "0ldc0mm1t", "sha": "f00"},
                    {"iid": 5, "state": "merged", "target_branch": "main",
                     "merge_commit_sha": "deadbeef"}])
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
        dev = {"state": "active", "access_level": 30}
        pages = {1: [dict(dev, username="a"), dict(dev, username="b", access_level=50),
                     dict(dev, username="guest", access_level=10),
                     dict(dev, username="reporter", access_level=20),
                     dict(dev, username="blocked", state="blocked"),
                     dict(dev, username="expired", expires_at="2000-01-01"),
                     dict(dev, username="later", expires_at="2999-01-01"),
                     dict(dev, username="awaiting", membership_state="awaiting"),
                     {"username": "unknown"}], 2: []}

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
                             {"a", "b", "later"})
            pages[1] = [{"username": "g", "state": "active", "access_level": 10}]
            with patch.object(forge, "warning") as said:
                got = forge.gitlab_group_members("https://gl/api/v4", "tok", "cern/admins")
            self.assertEqual(got, set())
            self.assertIn("counts nobody", said.call_args[0][0])


class TestTriggerPrimitives(unittest.TestCase):

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


class TestGitLabSubgroups(unittest.TestCase):
    def test_lists_all_visible_subgroups_not_only_memberships(self):
        # Without all_available GitLab returns only groups the token belongs to.
        seen = {}
        class _R:
            status_code, ok = 200, True
            def json(self): return [{"id": 7, "path": "lcg"}]
        def _get(url, headers=None, params=None, timeout=None):
            seen.update(url=url, params=params)
            return _R()
        with patch("requests.get", _get):
            out = forge.gitlab_subgroups("https://gl/api/v4", "t", "bits/admins")
        self.assertEqual(out, [{"id": 7, "path": "lcg"}])
        self.assertEqual(seen["params"].get("all_available"), "true")
        self.assertTrue(seen["url"].endswith("/groups/bits%2Fadmins/subgroups"))


if __name__ == "__main__":
    unittest.main()
