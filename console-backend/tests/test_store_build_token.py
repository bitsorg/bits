# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""POST /ops/ci/store-token: a build job gets a store token for its pipeline's
community and platform, signed with the build key, from what the worker knows of
the pipeline; anything else is refused."""

import base64
import hashlib
import sqlite3
import time
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from console_backend import audit, ci_auth, config, forge_ops, main
from console_backend.orch import kinds, reconcile
from console_backend.store_token import build_capabilities

from test_orch_reconcile import _DB

USER_SEED = hashlib.sha256(b"user key").digest()
BUILD_SEED = hashlib.sha256(b"build key").digest()
b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
ENV = {"GITLAB_API_URL": "https://gl/api/v4", "BITS_FORGE_PROJECT": "42",
       "BITS_STORE_TOKEN_KEY": b64(USER_SEED), "BITS_STORE_TOKEN_KID": "console-1",
       "BITS_STORE_BUILD_TOKEN_KEY": b64(BUILD_SEED), "BITS_STORE_BUILD_TOKEN_KID": "builds-1",
       "BITS_STORE_TOKEN_ISSUER": "https://bits.example.org",
       "BITS_STORE_TOKEN_AUDIENCE": "example-site/store"}


class StoreBuildTokenTest(_DB):
    def setUp(self):
        super().setUp()
        self.configure()
        p = patch.object(kinds.Deps, "reader", lambda d: self.gl)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.gl.add(5, community="testbed")
        self._pass()
        self.jobs = {77: {"status": "running", "pipeline": {"id": 5}}}      # GitLab's jobs

        def get_job(jid):
            if jid not in self.jobs:
                raise forge_ops.ForgeError(404, "404 Not found")
            return self.jobs[jid]
        self.gl.get_job = get_job
        self.claims = {"project_id": "42", "ref_protected": "true", "pipeline_id": "5",
                       "job_id": "77", "exp": int(time.time()) + 3600}

    def configure(self, **over):
        main.settings = config.Settings(env={k: v for k, v in dict(ENV, BITS_ORCH_DB=self.path,
                                                                   **over).items() if v is not None})

    def ask(self, **over):
        claims = dict(self.claims, **over)
        with patch.object(ci_auth, "verify_ci_token", lambda *a, **k: claims):
            return self.client.post("/ops/ci/store-token", headers={"Authorization": "Bearer jwt"})

    def test_a_running_builds_job_gets_its_token(self):
        r = self.ask()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("cache-control"), "no-store")
        tok = r.json()["token"]
        pub = Ed25519PrivateKey.from_private_bytes(BUILD_SEED).public_key()
        c = jwt.decode(tok, pub, algorithms=["EdDSA"], audience="example-site/store",
                       issuer="https://bits.example.org")
        self.assertEqual(jwt.get_unverified_header(tok)["kid"], "builds-1")
        self.assertEqual((c["sub"], c["grp"]), ("build:77", "testbed"))
        self.assertEqual(c["cap"], [{"m": m, "p": p} for m, p in build_capabilities("el9")])
        self.assertLessEqual(c["exp"], self.claims["exp"])          # never past the job
        self.assertGreater(c["exp"], self.claims["exp"] - 10)
        with self.assertRaises(jwt.InvalidSignatureError):         # not the user key
            jwt.decode(tok, Ed25519PrivateKey.from_private_bytes(USER_SEED).public_key(),
                       algorithms=["EdDSA"], audience="example-site/store")

    def test_its_capabilities(self):
        self.assertEqual(build_capabilities("x86_64-el9"), [
            ("GET", "/store/x86_64-el9*/**"), ("PUT", "/store/x86_64-el9*/**"),
            ("GET", "/MANIFESTS/rev-index/x86_64-el9*/**"), ("PUT", "/MANIFESTS/rev-index/x86_64-el9*/**"),
            ("GET", "/store/share/**"), ("PUT", "/store/share/**"),
            ("GET", "/MANIFESTS/rev-index/share/**"), ("PUT", "/MANIFESTS/rev-index/share/**"),
            ("GET", "/sources/**")])
        for bad in ("", "../x", "x/y", "*", None):
            with self.assertRaises(ValueError):
                build_capabilities(bad)

    def test_a_pipeline_the_worker_has_not_seen_yet_is_read_from_gitlab(self):
        self.gl.add(6, community="other")
        self.jobs[78] = {"status": "running", "pipeline": {"id": 6}}
        r = self.ask(pipeline_id="6", job_id="78")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(("get", 6), self.gl.calls)
        self.assertEqual(self._row(6)["community"], "other")
        self.jobs[79] = {"status": "running", "pipeline": {"id": 9}}
        self.assertEqual(self.ask(pipeline_id="9", job_id="79").status_code, 403)   # GitLab has none

    def test_recorded_by_the_worker_meanwhile(self):
        self.gl.add(6, community="other")
        self.jobs[78] = {"status": "running", "pipeline": {"id": 6}}
        real = reconcile.record_pipeline

        def race(conn, reader, p):
            real(conn, reader, p)
            raise sqlite3.IntegrityError("UNIQUE constraint failed: pipelines.id")
        with patch.object(reconcile, "record_pipeline", race):
            self.assertEqual(self.ask(pipeline_id="6", job_id="78").status_code, 200)

    def test_only_a_running_job_of_its_pipeline(self):
        self.jobs[77]["status"] = "success"                           # finished, still in time
        self.assertEqual(self.ask().status_code, 403)
        self.jobs[77] = {"status": "running", "pipeline": {"id": 6}}  # another pipeline's
        self.assertEqual(self.ask().status_code, 403)
        self.assertEqual(self.ask(job_id="80").status_code, 403)       # GitLab has none
        self.jobs[77] = {"status": "running", "pipeline": {"id": 5}}
        self.conn.execute("UPDATE pipelines SET outcome = 'failed' WHERE id = 5")
        self.conn.commit()
        self.assertEqual(self.ask().status_code, 200)                  # a retried job

    def test_gitlab_unavailable_or_not_configured(self):
        def down(jid):
            raise forge_ops.ForgeError(503, "unavailable")
        self.gl.get_job = down
        self.assertEqual(self.ask().status_code, 502)

        def unconfigured(d):
            raise kinds.NotConfigured("no GitLab")
        with patch.object(kinds.Deps, "reader", unconfigured):
            self.assertEqual(self.ask().status_code, 503)

    def test_audited(self):
        seen = []
        with patch.object(audit, "record", lambda kind, **kw: seen.append((kind, kw))):
            self.ask(user_login="alice", ref="main")
            self.jobs[77]["status"] = "canceled"
            self.ask(user_login="alice")
        (k1, ok), (k2, no) = seen
        self.assertEqual((k1, ok["user"], ok["group"], ok["principal"], ok["pipeline"], ok["user_login"],
                          ok["ref"], ok["platform"]),
                         ("store_token", "build:77", "testbed", "build", 5, "alice", "main", "el9"))
        self.assertEqual((k2, no["user"], no["principal"], no["user_login"]),
                         ("store_token_denied", "build:77", "build", "alice"))
        seen.clear()
        self.jobs[77]["status"] = "running"
        with patch.object(audit, "record", lambda kind, **kw: seen.append((kind, kw))):
            self.ask(exp=int(time.time()) + 30)
        self.assertEqual((seen[0][0], seen[0][1]["group"]), ("store_token_denied", "testbed"))

    def test_a_reply_gitlab_cannot_have_sent_is_not_a_refusal(self):
        def html(jid):
            raise ValueError("Expecting value: line 1 column 1")     # requests' JSON error
        self.gl.get_job = html
        self.assertEqual(self.ask().status_code, 502)

    def test_only_a_build_pipeline_with_a_platform(self):
        self.gl.add(7, source="push")                                  # not a build
        self.jobs[81] = {"status": "running", "pipeline": {"id": 7}}
        self.assertEqual(self.ask(pipeline_id="7", job_id="81").status_code, 403)
        self.gl.add(8)
        self.gl.variables[8] = [v for v in self.gl.variables[8] if v["key"] != "PLATFORM"]
        self.jobs[82] = {"status": "running", "pipeline": {"id": 8}}
        self.assertEqual(self.ask(pipeline_id="8", job_id="82").status_code, 403)
        self.conn.execute("UPDATE pipelines SET is_build = 0 WHERE id = 5")   # its platform kept
        self.conn.commit()
        self.assertEqual(self.ask().status_code, 403)

    def test_the_lifetime(self):
        self.configure(BITS_STORE_BUILD_TOKEN_MAX_TTL="600")
        c = jwt.decode(self.ask().json()["token"], options={"verify_signature": False})
        self.assertEqual(c["exp"] - c["iat"], 600)
        self.assertEqual(self.ask(exp=int(time.time()) + 30).status_code, 403)
        self.assertEqual(self.ask(exp="soon").status_code, 403)
        self.assertEqual(self.ask(job_id=None).status_code, 403)

    def test_the_ci_token_is_checked_as_for_hints(self):
        self.assertEqual(self.ask(project_id="7").status_code, 403)
        self.assertEqual(self.ask(ref_protected="false").status_code, 403)
        self.assertEqual(self.client.post("/ops/ci/store-token").status_code, 401)

    def test_off_without_the_build_key(self):
        self.configure(BITS_STORE_BUILD_TOKEN_KEY=None)
        self.assertEqual(self.ask().status_code, 503)
        self.configure(BITS_STORE_BUILD_TOKEN_KID=None)
        self.assertEqual(self.ask().status_code, 503)
        self.configure(BITS_STORE_BUILD_TOKEN_KEY="not a seed")
        self.assertEqual(self.ask().status_code, 503)
        with self.assertRaises(ValueError):                           # never the user key's id
            self.configure(BITS_STORE_BUILD_TOKEN_KID="console-1")


if __name__ == "__main__":
    unittest.main()
