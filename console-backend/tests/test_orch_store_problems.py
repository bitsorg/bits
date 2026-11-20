# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-store's problems as issues (store_problems.py), read with an admin token."""

import base64
import os
import shutil
import tempfile
import unittest
from unittest import mock

import jwt
import requests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from console_backend import config, store_admin
from console_backend.orch import db, failures, store_problems, worker

from test_store_admin import ADMIN_SEED, ENV, Answer

T0 = 1_800_000_000_000          # ms
MIN = 60 * 1000


def problem(pid, kind="git_failed", subject="github.com/curl/curl", count=1, first=T0, last=None,
            message=None, level="error"):
    return {"id": pid, "first": first, "last": last or first, "count": count, "level": level,
            "kind": kind, "subject": subject,
            "message": message or "git %s: git fetch: exit status 1: fatal: bad revision '00007e0d45a2'" % subject}


class StoreProblemsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conn = db.connect(os.path.join(self.dir, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        failures.seed_signatures(self.conn)
        self.answer, self.asked = [], []

    def read(self, since):
        self.asked.append(since)
        return [p for p in self.answer if p["last"] >= since]

    def collect(self, at):
        return store_problems.collect(self.conn, self.read, now_ms=at)

    def issues(self):
        return failures.list_issues(self.conn)

    def test_one_issue_per_fault_counted_as_the_store_counts(self):
        self.answer = [problem(1, count=2), problem(2, subject="github.com/root-project/root"),
                       problem(3, kind="disk_low", subject="-data", level="warning",
                               message="-data's file system: 9 GiB free of 100 GiB")]
        self.assertEqual(self.collect(T0 + MIN), 3)
        got = {i["title"].split(":")[0]: i for i in self.issues()}
        git = got["git_failed"]
        self.assertEqual((git["source"], git["package"], git["count"]), ("store", "bits-store", 3))
        self.assertIsNone(git["signature"])
        self.assertEqual(got["disk_low"]["signature"]["cause"].split(" ")[0], "bits-store's")
        # not due again for five minutes; then a row that grew adds the difference only
        self.answer[0] = problem(1, count=5, last=T0 + 3 * MIN)
        self.assertEqual(self.collect(T0 + 2 * MIN), 0)
        self.assertEqual(self.collect(T0 + 7 * MIN), 1)
        self.assertEqual(self.asked, [0, T0])
        self.assertEqual({i["title"].split(":")[0]: i["count"] for i in self.issues()}["git_failed"], 6)
        self.assertEqual(self.collect(T0 + 13 * MIN), 0)          # nothing new: nothing counted

    def test_a_store_issue_shows_its_problems_to_overall_readers_only(self):
        self.answer = [problem(1)]
        self.collect(T0 + MIN)
        issue = self.issues()[0]["issue"]
        full = failures.get_issue(self.conn, issue)
        self.assertEqual([p["subject"] for p in full["problems"]], ["github.com/curl/curl"])
        self.assertEqual(full["failures"], [])
        self.assertEqual(failures.list_issues(self.conn, communities={"lcg"}), [])
        self.assertIsNone(failures.get_issue(self.conn, issue, communities={"lcg"}))

    def test_a_signature_added_later_names_the_issue(self):
        self.answer = [problem(1)]
        self.collect(T0 + MIN)
        self.conn.execute("INSERT INTO signatures (pattern, cause, action, safe_to_retry, state, proposed_by,"
                          " accepted_by, created_ms) VALUES ('reason:store:git_failed', 'c', 'a', 0, 'active',"
                          " 't', 't', 0)")
        self.assertEqual(failures.rematch(self.conn), 1)
        self.assertEqual(self.issues()[0]["signature"]["cause"], "c")

    def test_a_problem_not_in_bits_store_shape_is_skipped(self):
        self.answer = [{"id": 1, "last": T0}, problem(2)]
        self.assertEqual(self.collect(T0 + MIN), 1)


class ReadTest(unittest.TestCase):
    """store_admin.problems: a minute's admin token allowed GET /_admin/problems alone."""

    def setUp(self):
        self.settings = config.Settings(env=dict(ENV))
        self.calls = []

        def fake_get(session, url, params=None, headers=None, timeout=None, allow_redirects=True):
            self.calls.append((url, params, headers))
            return Answer(200, {"problems": [problem(1)], "kept_days": 30})
        p = mock.patch.object(requests.Session, "get", fake_get)
        p.start()
        self.addCleanup(p.stop)

    def test_read(self):
        got = store_admin.problems(self.settings, T0)
        self.assertEqual([p["id"] for p in got], [1])
        url, params, headers = self.calls[0]
        self.assertEqual((url, params), ("http://bits-store:8099/_admin/problems",
                                         [("since", str(T0)), ("limit", "1000")]))
        tok = headers["Authorization"].split(" ", 1)[1]
        pub = Ed25519PrivateKey.from_private_bytes(ADMIN_SEED).public_key()
        claims = jwt.decode(tok, pub, algorithms=["EdDSA"], audience=ENV["BITS_STORE_TOKEN_AUDIENCE"])
        self.assertEqual((claims["sub"], claims["cap"], claims["exp"] - claims["iat"]),
                         ("admin:bits-orchestrator", [{"m": "GET", "p": "/_admin/problems"}], 60))
        self.assertEqual(jwt.get_unverified_header(tok)["kid"], "admin-1")

    def test_the_worker_reads_them_when_the_admin_api_is_set(self):
        steps = lambda env: [n for n, _ in worker.Worker(None, config.Settings(env=env)).steps]
        self.assertIn("store_problems", steps(dict(ENV, BITS_ORCH_DB=os.devnull)))
        self.assertNotIn("store_problems", steps({k: v for k, v in ENV.items() if k != "BITS_STORE_ADMIN_URL"}))


if __name__ == "__main__":
    unittest.main()
