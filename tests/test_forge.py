# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the forge approval gate (bits_helpers/forge.py)."""

import os
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers import forge


class TestApprovedBy(unittest.TestCase):

    def test_case_insensitive_intersection(self):
        self.assertTrue(forge.approved_by(["Alice"], ["alice", "bob"]))
        self.assertTrue(forge.approved_by(["bob", "eve"], ["BOB"]))

    def test_no_overlap_is_false(self):
        self.assertFalse(forge.approved_by(["eve"], ["alice", "bob"]))

    def test_empty_sides_are_false(self):
        self.assertFalse(forge.approved_by([], ["alice"]))
        self.assertFalse(forge.approved_by(["alice"], []))


class TestLoadAdmins(unittest.TestCase):

    def test_plain_list_with_comments(self):
        text = "# admins\nalice\n\nbob   # lead\n"
        self.assertEqual(forge.load_admins(text), {"alice", "bob"})

    def test_at_handles_and_codeowners_style(self):
        text = "@Alice\n/manifests/lcg @bob @carol  # owners\n"
        self.assertEqual(forge.load_admins(text), {"alice", "bob", "carol"})

    def test_reads_from_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("@dave\n")
            path = fh.name
        try:
            self.assertEqual(forge.load_admins(path), {"dave"})
        finally:
            os.remove(path)


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


class TestVerifyApproval(unittest.TestCase):

    def test_ok_when_admin_approved(self):
        fg = forge.StaticForge(["alice", "eve"])
        ok, approvers = forge.verify_approval(fg, {"alice"})
        self.assertTrue(ok)
        self.assertEqual(approvers, {"alice", "eve"})

    def test_not_ok_when_only_non_admins_approved(self):
        fg = forge.StaticForge(["eve"])
        ok, _ = forge.verify_approval(fg, {"alice"})
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
