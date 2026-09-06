# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Convention-based admin policy: the whole policy is derived from the bits-admins
GitLab group tree (root members = overall '*'; each subgroup path = a community,
its members = that community's admins), merged with any explicit BITS_ADMINS_POLICY.
A GitLab failure mid-refresh serves the last good policy rather than locking out."""

import unittest
from unittest.mock import patch

from console_backend import authz, config


# Group tree: root 'bits-admins' -> {buncic}; subgroups alice(101), lhcb(102).
# DIRECT members only (inherited=False), so overall coverage must come from '*'.
_MEMBERS = {
    # root carries the resolve-token's own bot account — must be filtered out.
    "bits-admins": {"buncic", "group_355494_bot_deadbeef"},
    101: {"alice1"},
    102: {"lhcb1"},
}
_SUBGROUPS = [{"id": 101, "path": "alice"}, {"id": 102, "path": "lhcb"}]


def _members(api, token, ref, *a, **k):
    return set(_MEMBERS[ref])


def _subgroups(api, token, ref, *a, **k):
    return list(_SUBGROUPS)


def _settings(policy="", group="bits-admins", resolve="rtok"):
    return config.Settings(env={
        "GITLAB_API_URL": "https://gl/api/v4",
        "BITS_ADMINS_POLICY": policy,
        "BITS_ADMINS_GROUP": group,
        "BITS_ADMIN_RESOLVE_TOKEN": resolve,
    })


class TestTreePolicy(unittest.TestCase):
    def setUp(self):
        authz._CACHE.clear()
        authz._STALE.clear()

    @patch("console_backend.authz.forge.gitlab_subgroups", _subgroups)
    @patch("console_backend.authz.forge.gitlab_group_members", _members)
    def test_overall_and_per_community(self):
        pol = authz.resolved_admin_policy(_settings(), None)
        # overall admin covers every community; a subgroup member only their own.
        self.assertTrue(authz.is_admin_for("buncic", "alice", pol))
        self.assertTrue(authz.is_admin_for("buncic", "anything", pol))   # via '*'
        self.assertTrue(authz.is_admin_for("alice1", "alice", pol))
        self.assertFalse(authz.is_admin_for("alice1", "lhcb", pol))
        self.assertFalse(authz.is_admin_for("lhcb1", "alice", pol))
        # the group access-token bot is never an admin
        self.assertNotIn("group_355494_bot_deadbeef", pol["*"])
        self.assertFalse(authz.is_admin_for("group_355494_bot_deadbeef", "alice", pol))

    @patch("console_backend.authz.forge.gitlab_subgroups", _subgroups)
    @patch("console_backend.authz.forge.gitlab_group_members", _members)
    def test_community_without_subgroup_is_overall_only(self):
        pol = authz.resolved_admin_policy(_settings(), None)
        # 'cms' has no subgroup -> only the overall admin may act there.
        self.assertTrue(authz.is_admin_for("buncic", "cms", pol))
        self.assertFalse(authz.is_admin_for("alice1", "cms", pol))

    @patch("console_backend.authz.forge.gitlab_subgroups", _subgroups)
    @patch("console_backend.authz.forge.gitlab_group_members", _members)
    def test_explicit_policy_supplements_tree(self):
        # A literal per-community admin for a community with no subgroup yet.
        pol = authz.resolved_admin_policy(_settings(policy="cms @carol"), None)
        self.assertTrue(authz.is_admin_for("carol", "cms", pol))
        self.assertTrue(authz.is_admin_for("alice1", "alice", pol))   # tree still applies

    @patch("console_backend.authz.forge.gitlab_group_members", _members)
    def test_serve_stale_on_refresh_failure(self):
        s = _settings()
        with patch("console_backend.authz.forge.gitlab_subgroups", _subgroups):
            good = authz.resolved_admin_policy(s, None)      # populates _STALE
        self.assertTrue(authz.is_admin_for("alice1", "alice", good))
        authz._CACHE.clear()                                  # force a refresh
        with patch("console_backend.authz.forge.gitlab_subgroups",
                   side_effect=RuntimeError("gitlab down")):
            served = authz.resolved_admin_policy(s, None)
        self.assertEqual(served, good)                        # last good, not a lockout

    def test_cold_start_failure_denies(self):
        s = _settings()
        with patch("console_backend.authz.forge.gitlab_group_members",
                   side_effect=RuntimeError("gitlab down")):
            with self.assertRaises(RuntimeError):             # no cache yet -> fail closed
                authz.resolved_admin_policy(s, None)

    @patch("console_backend.authz.forge.gitlab_group_members", _members)
    def test_stale_expires_then_fails_closed(self):
        s = _settings()
        with patch("console_backend.authz.forge.gitlab_subgroups", _subgroups):
            authz.resolved_admin_policy(s, None)             # populates _STALE
        key = (s.admin_policy_source, s.admins_group)
        pol, _exp = authz._STALE[key]
        authz._STALE[key] = (pol, 0)                          # age it past the bound
        authz._CACHE.clear()
        with patch("console_backend.authz.forge.gitlab_subgroups",
                   side_effect=RuntimeError("still down")):
            with self.assertRaises(RuntimeError):             # stale too old -> deny
                authz.resolved_admin_policy(s, None)

    def test_group_set_but_no_resolve_token_disables_tree(self):
        # B1 guard: with a group but NO resolve token the tree is off and the
        # (potentially per-user) explicit resolution is not cached/shared — only
        # the literal policy applies, exactly as before this feature.
        pol = authz.resolved_admin_policy(_settings(policy="* @buncic", resolve=""), None)
        self.assertTrue(authz.is_admin_for("buncic", "alice", pol))    # via '*'
        self.assertFalse(authz.is_admin_for("alice1", "alice", pol))   # tree not applied
        self.assertEqual(authz._CACHE, {})                             # nothing cached

    def test_no_group_no_token_uses_explicit_only(self):
        # Neither convention nor resolve token: old literal-policy path, no GitLab.
        pol = authz.resolved_admin_policy(_settings(policy="* @buncic", group="", resolve=""), None)
        self.assertTrue(authz.is_admin_for("buncic", "whatever", pol))
        self.assertFalse(authz.is_admin_for("mallory", "alice", pol))


if __name__ == "__main__":
    unittest.main()
