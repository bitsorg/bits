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


class ReadTokenTest(_DB):
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


if __name__ == "__main__":
    unittest.main()
