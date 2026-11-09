# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read tokens: signed, recorded, GET of a few routes only, their communities only."""
import base64
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, main
from console_backend.orch import db, read_tokens
from test_orch_failures import _DB, bits_failure

KEY = "k" * 40


class _Tokens(_DB):
    ENV = {"GITLAB_API_URL": "https://gl/api/v4", "BITS_ADMINS_POLICY": "lcg @alice\n* @carol",
           "BITS_FORGE_PROJECT": "42", "BITS_READ_TOKEN_KEY": KEY}

    def setUp(self):
        super().setUp()
        main.settings = config.Settings(env=dict(self.ENV, BITS_ORCH_DB=self.path))
        self.gitlab = []
        p = patch.object(main.identity, "verify_gitlab_token",
                         lambda a, t, *x, **k: self.gitlab.append(t) or t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self._fail(1, bits_failure())
        self._fail(2, bits_failure(line=50), community="atlas")
        self._pass()

    def _mint(self, communities, days=30, sub="claude"):
        return read_tokens.mint(self.conn, main.settings, sub, communities, days, "carol")[0]

    def _get(self, token, path):
        return self.client.get(path, headers={"Authorization": ("Bearer %s" % token).encode("latin-1")})


class ReadTokenTest(_Tokens):

    def _pipelines(self, token, query=""):
        r = self._get(token, "/ops/failures" + query)
        return r.status_code, sorted(f["pipeline"] for f in r.json().get("failures", []))

    def test_a_token_reads_its_communities(self):
        lcg, every = self._mint(["LCG"]), self._mint(["*"])
        self.assertEqual(self._pipelines(lcg), (200, [1]))
        self.assertEqual(self._pipelines(lcg, "?pipeline=2"), (200, []))
        self.assertEqual(self._pipelines(lcg, "?community=atlas")[0], 403)
        self.assertEqual(self._pipelines(every), (200, [1, 2]))
        self.assertEqual(self._get(lcg, "/ops/builds/1").status_code, 200)
        self.assertEqual(self._get(lcg, "/ops/builds/2").status_code, 404)
        self.assertIn("variables", self._get(every, "/ops/builds/2").json())

    def test_the_chats_records_are_for_a_token_of_every_community(self):
        lcg, every = self._mint(["lcg"]), self._mint(["*"])
        for path in ("/ops/chat/gaps", "/ops/chat/feedback"):
            self.assertEqual(self._get(lcg, path).status_code, 403, path)
            self.assertEqual(self._get(every, path).status_code, 200, path)

    def test_only_a_get_of_its_routes(self):
        every = self._mint(["*"])
        for path in ("/ops/builds", "/ops/issues", "/ops/queue", "/ops/chat"):
            self.assertEqual(self._get(every, path).status_code, 403, path)
        r = self.client.post("/ops/chat/feedback", json={"vote": "up", "question": "q", "answer": "a"},
                             headers={"Authorization": "Bearer %s" % every})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._get(every, "/ops/pipeline/1/variables").status_code, 401)
        self.assertEqual(self.gitlab, [])                 # never sent to GitLab

    def test_what_is_not_a_token(self):
        good = self._mint(["lcg"])
        head, _, sig = good.rpartition(".")
        payload = json.loads(base64.urlsafe_b64decode(head[len(read_tokens.PREFIX):] + "=="))
        wider = read_tokens.PREFIX + base64.urlsafe_b64encode(
            json.dumps(dict(payload, communities=["*"])).encode()).rstrip(b"=").decode()
        for bad in (head + "." + sig[:-2] + "AA", wider + "." + sig, head, read_tokens.PREFIX + "x.y",
                    head + ".\xff"):
            self.assertEqual(self._get(bad, "/ops/failures").status_code, 401, bad)
        self.assertEqual(self._get(good, "/ops/failures").status_code, 200)
        r = self.client.get("/ops/failures", headers={"Authorization": "Bearer  %s" % good})
        self.assertEqual(r.status_code, 200)                     # spaces around it
        with patch.object(db, "now_ms", return_value=db.now_ms() + 31 * 86400 * 1000):
            self.assertEqual(self._get(good, "/ops/failures").status_code, 401)
        self.assertEqual(read_tokens.revoke(self.conn, payload["jti"], "carol"), 1)
        self.assertEqual(self._get(good, "/ops/failures").status_code, 401)
        other = self._mint(["lcg"])
        with db.transaction(self.conn):
            self.conn.execute("DELETE FROM read_tokens")
        self.assertEqual(self._get(other, "/ops/failures").status_code, 401)
        third = self._mint(["lcg"])
        main.settings.read_token_key = "j" * 40         # the key rotated
        self.assertEqual(self._get(third, "/ops/failures").status_code, 401)
        main.settings.read_token_key = ""               # read tokens off
        self.assertEqual(self._get(third, "/ops/failures").status_code, 401)

    def test_what_a_token_is_minted_for(self):
        for sub, communities, days in (("a b", ["lcg"], 1), ("x", [], 1), ("x", ["l c g"], 1),
                                       ("x", ["lcg"], 0), ("x", ["lcg"], read_tokens.MAX_DAYS + 1),
                                       ("x", ["*", "lcg"], 1), ("x", "lcg", 1), ("x", ["lcg"], True)):
            with self.assertRaises(ValueError):
                read_tokens.mint(self.conn, main.settings, sub, communities, days, "carol")
        main.settings.read_token_key = "short"
        with self.assertRaises(ValueError):
            self._mint(["lcg"])

    def test_its_last_use_is_kept(self):
        token = self._mint(["lcg"])
        used = lambda: self.conn.execute("SELECT last_used_ms FROM read_tokens").fetchone()[0]
        self.assertEqual(used(), 0)
        self._get(token, "/ops/failures")
        self.assertGreater(used(), 0)


class JobLogTest(_Tokens):
    """GET /ops/builds/{pipeline}/jobs/{job}/log: a job's log or bits log, redacted,
    for the build's community's admins and read tokens of it."""

    def setUp(self):
        super().setUp()
        self.gl.artifacts[10] = "bits: building arrow\npassword=hunter2 token: abc123\n"
        p = patch("console_backend.orch.kinds.Deps.reader", lambda deps: self.gl)
        p.start()
        self.addCleanup(p.stop)

    def test_for_the_communitys_admins(self):
        r = self._get("alice", "/ops/builds/1/jobs/10/log")
        self.assertEqual((r.status_code, r.headers["content-type"]), (200, "text/plain; charset=utf-8"))
        self.assertIn("BUILD FAILED", r.text)
        self.assertNotIn("hunter2", r.text)
        self.assertEqual(self._get("carol", "/ops/builds/2/jobs/20/log").status_code, 200)
        self.assertEqual(self._get("alice", "/ops/builds/2/jobs/20/log").status_code, 403)
        self.assertEqual(self._get("dave", "/ops/builds/1/jobs/10/log").status_code, 403)

    def test_the_bits_logs_and_what_is_not_there(self):
        r = self._get("carol", "/ops/builds/1/jobs/10/log?file=bits-build.log")
        self.assertEqual(r.status_code, 200)
        self.assertIn("building arrow", r.text)
        self.assertNotIn("hunter2", r.text)
        self.assertNotIn("abc123", r.text)
        r = self._get("carol", "/ops/builds/2/jobs/20/log?file=bits-publish.log")
        self.assertEqual(r.status_code, 404)
        self.assertIn("has no bits-publish.log (not written", r.text)
        self.gl.logs.pop(20, None)
        r = self._get("carol", "/ops/builds/2/jobs/20/log")
        self.assertEqual(r.status_code, 404)
        self.assertIn("no longer has the log of job 20", r.text)
        self.assertEqual(self._get("carol", "/ops/builds/1/jobs/10/log?file=../etc").status_code, 400)
        self.assertEqual(self._get("carol", "/ops/builds/1/jobs/20/log").status_code, 404)   # not its job
        self.assertEqual(self._get("carol", "/ops/builds/99/jobs/10/log").status_code, 404)

    def test_how_much_is_read(self):
        asked = []
        real = self.gl.job_log_tail
        self.gl.job_log_tail = lambda job, n: asked.append(n) or real(job, n)
        for n in (10, 10 ** 9, ""):
            self._get("carol", "/ops/builds/1/jobs/10/log" + ("?bytes=%s" % n if n != "" else ""))
        self.assertEqual(asked, [1024, 4 << 20, 1 << 20])

    def test_a_gitlab_error_is_said(self):
        def broken(job, n):
            raise RuntimeError("GitLab is down: https://oauth2:glpat-abcdefghij0123456789@gl/x")
        self.gl.job_log_tail = broken
        r = self._get("carol", "/ops/builds/1/jobs/10/log")
        self.assertEqual(r.status_code, 502)
        self.assertIn("GitLab is down", r.text)
        self.assertNotIn("glpat-abcdefghij0123456789", r.text)

    def test_a_secret_the_tail_cut_from_its_name_is_left_out(self):
        log = "x" * 2000 + "\npassword=hunter2hunter2\n" + "y" * 1500 + "\nthe end\n"
        self.gl.job_log_tail = lambda job, n: log.encode()[-n:]
        r = self._get("carol", "/ops/builds/1/jobs/10/log?bytes=%d" % (len(log) - log.index("hunter2")))
        self.assertEqual(r.status_code, 200)
        self.assertIn("the end", r.text)
        self.assertNotIn("hunter2", r.text)
        whole = self._get("carol", "/ops/builds/1/jobs/10/log?bytes=4096")
        self.assertIn("xxx", whole.text)                  # not cut: the first line is kept

    def test_a_read_token_reads_its_communitys_logs_and_it_is_recorded(self):
        token = self._mint(["lcg"])
        self.assertEqual(self._get(token, "/ops/builds/1/jobs/10/log?file=bits-build.log").status_code, 200)
        self.assertEqual(self._get(token, "/ops/builds/2/jobs/20/log").status_code, 404)
        rows = self.conn.execute("SELECT subject, actor_kind, actor_id, payload FROM events"
                                 " WHERE kind = 'log.read'").fetchall()
        self.assertEqual(len(rows), 1)
        subject, kind, who, payload = rows[0]
        self.assertEqual((subject, kind, who, json.loads(payload)["file"]),
                         ("pipeline:1", "person", "read-token:claude", "bits-build.log"))


if __name__ == "__main__":
    unittest.main()
