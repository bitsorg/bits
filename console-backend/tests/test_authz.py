# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""B2b tests: community-admin resolution, /me roles, and the signing gate."""

import unittest
from types import SimpleNamespace

from fastapi import HTTPException
from fastapi.testclient import TestClient

from console_backend import authz, config, main


def _configure(policy_text):
    main.settings = config.Settings(env={
        "GITLAB_API_URL": "https://gitlab.example/api/v4",
        "BITS_ADMINS_POLICY": policy_text,
        "BITS_SESSION_COOKIE_SECURE": "0",
    })
    main.sessions = main.session.SessionStore(60)


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


class TestMeRolesAndGate(unittest.TestCase):
    def setUp(self):
        _configure("* @root\nlcg @alice\ncommon @bob")
        self.client = TestClient(main.app, follow_redirects=False)

    def test_me_reports_admin_groups(self):
        sid = main.sessions.create({"user": "alice", "token": "t"})
        body = self.client.get("/me", cookies={"bits_session": sid}).json()
        self.assertEqual(body["user"], "alice")
        self.assertFalse(body["overall_admin"])
        self.assertEqual(body["admin_groups"], ["lcg"])

    def test_me_overall_admin(self):
        sid = main.sessions.create({"user": "root", "token": "t"})
        self.assertTrue(self.client.get("/me", cookies={"bits_session": sid}).json()["overall_admin"])

    def test_gate_allows_group_admin(self):
        sid = main.sessions.create({"user": "alice", "token": "t"})
        req = SimpleNamespace(cookies={"bits_session": sid})
        self.assertEqual(main.require_community_admin(req, "lcg"), "alice")

    def test_gate_403s_non_admin(self):
        sid = main.sessions.create({"user": "alice", "token": "t"})
        req = SimpleNamespace(cookies={"bits_session": sid})
        with self.assertRaises(HTTPException) as ctx:
            main.require_community_admin(req, "common")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_gate_401s_without_session(self):
        with self.assertRaises(HTTPException) as ctx:
            main.require_community_admin(SimpleNamespace(cookies={}), "lcg")
        self.assertEqual(ctx.exception.status_code, 401)

    def test_gate_rejects_empty_group_fail_closed(self):
        # bob is a 'common' admin; an empty/None group must NOT silently map to
        # 'common' and pass — it must fail closed with 400.
        sid = main.sessions.create({"user": "bob", "token": "t"})
        req = SimpleNamespace(cookies={"bits_session": sid})
        for g in ("", None):
            with self.assertRaises(HTTPException) as ctx:
                main.require_community_admin(req, g)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_gate_overall_admin_covers_any_group(self):
        sid = main.sessions.create({"user": "root", "token": "t"})
        req = SimpleNamespace(cookies={"bits_session": sid})
        self.assertEqual(main.require_community_admin(req, "common"), "root")


if __name__ == "__main__":
    unittest.main()
