# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""B2b tests: community-admin resolution, /me roles, and the signing gate."""

import unittest

from fastapi.testclient import TestClient

from console_backend import authz, config, main


def _configure(policy_text):
    main.settings = config.Settings(env={
        "GITLAB_API_URL": "https://gitlab.example/api/v4",
        "BITS_ADMINS_POLICY": policy_text,
    })
    main.identity.verify_gitlab_token = lambda a, t, *x, **k: t or None


class TestAuthzUnit(unittest.TestCase):
    def test_literal_policy_no_network(self):
        s = config.Settings(env={"BITS_ADMINS_POLICY": "* @root\nlcg @alice\ncommon @bob"})
        pol = authz.resolve_policy(authz.load_policy(s), s, None)  # no &refs
        self.assertTrue(authz.is_admin_for("alice", "lcg", pol))
        self.assertFalse(authz.is_admin_for("alice", "common", pol))
        self.assertTrue(authz.is_admin_for("root", "anything", pol))   # overall
        overall, groups = authz.admin_groups("alice", pol)
        self.assertFalse(overall)
        self.assertEqual(groups, ["lcg"])
        self.assertTrue(authz.admin_groups("root", pol)[0])            # overall

    def test_case_insensitive(self):
        s = config.Settings(env={"BITS_ADMINS_POLICY": "lcg @Alice"})
        pol = authz.resolve_policy(authz.load_policy(s), s, None)
        self.assertTrue(authz.is_admin_for("alice", "lcg", pol))


class TestMeRoles(unittest.TestCase):
    def setUp(self):
        _configure("* @root\nlcg @alice\ncommon @bob")
        self.client = TestClient(main.app, follow_redirects=False)

    def _bearer(self, user):
        return {"Authorization": "Bearer %s" % user}

    def test_me_reports_admin_groups(self):
        body = self.client.get("/me", headers=self._bearer("alice")).json()
        self.assertEqual(body["user"], "alice")
        self.assertFalse(body["overall_admin"])
        self.assertEqual(body["admin_groups"], ["lcg"])

    def test_me_overall_admin(self):
        self.assertTrue(
            self.client.get("/me", headers=self._bearer("root")).json()["overall_admin"])

    def test_me_without_bearer_401(self):
        self.assertEqual(self.client.get("/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()
