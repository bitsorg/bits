# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator's M2: failure records, issues, signatures, backfill, API."""

import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, forge_ops, main
from console_backend.orch import db, events, failures, reconcile

from test_orch_reconcile import FakeReader, _job

ESC = "\x1b"


def _day(ago):
    """The UTC date *ago* days back: fixtures stay inside the collector's windows."""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() - ago * 86400))


DAY = _day(1)


def bits_failure(pkg="arrow", ver="23.0.1", path="/home/gitlab-runner/jobs/9911/sw/BUILD/a1b2c3d4e5/arrow",
                 error="flatbuffers/base.h:42:3: error: Non-compatible flatbuffers version included",
                 line=42):
    """A job log as GitLab serves it: section markers, colours, bits' failure."""
    return (
        "section_start:1760000000:step_script\r" + ESC + "[0K" + ESC + "[0;m$ bits build ...\n"
        + DAY + "T09:00:01Z Building arrow ...\n"
        "export PRIVATE-TOKEN: glpat-AbCdEf0123456789xyz\n"
        "cloning https://ci-bot:s3cr3tpass@gitlab.cern.ch/x/y.git\n"
        + ESC + "[31m" + ESC + "[1mBUILD FAILED:" + ESC + "[0m %s@%s\n" % (pkg, ver) +
        "=" * 70 + "\n\n"
        "Log File:\n  sw/BUILD/%s/log\n\n" % pkg +
        "Error excerpt:\n"
        "  Matched error lines (last 2):\n"
        "    %s/src/%s:%d:3: %s\n" % (path, "gen.cc", line, error) +
        "    make[2]: *** [CMakeFiles/arrow.dir/build.make:%d: gen.o] Error 1\n" % (line * 3) +
        "  Last 3 lines of log:\n"
        "    [ 71%%] Built target thing at 10:%02d:11\n" % (line % 60) +
        "    make: *** [Makefile:146: all] Error 2\n"
        "    password=hunter2 token: abc123\n"
        "\nEnvironment:\n"
        "  OS: el9\n"
        "  bits: 0.6 (bits@35b7773abc)\n"
        "section_end:1760000100:step_script\r" + ESC + "[0K\n"
        "ERROR: Job failed: exit code 1\n")


class LogReader(FakeReader):
    def __init__(self):
        super().__init__()
        self.logs, self.artifacts = {}, {}

    def job_log_tail(self, job_id, max_bytes=1 << 20):
        self.calls.append(("log", job_id))
        v = self.logs.get(job_id)
        if isinstance(v, Exception):
            raise v
        return v.encode() if isinstance(v, str) else v

    def job_artifact_tail(self, job_id, name, max_bytes=1 << 20):
        self.calls.append(("artifact", job_id, name))
        v = self.artifacts.get(job_id)
        return v.encode() if isinstance(v, str) else v

    def list_pipelines_before(self, updated_before, status="failed", page=1, per_page=100):
        self.calls.append(("before", updated_before, page))
        w = reconcile._when
        got = sorted((p for p in self.pipelines.values()
                      if w(p["updated_at"]) < w(updated_before) and p["status"] == status),
                     key=lambda p: (w(p["updated_at"]), p["id"]), reverse=True)
        return [dict(p) for p in got[(page - 1) * per_page: page * per_page]]


class _DB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "orch.db")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        failures.seed_signatures(self.conn)
        self.gl = LogReader()

    def _fail(self, pid, log, job_id=None, community="lcg", package="arrow", **kw):
        jid = job_id or pid * 10
        self.gl.add(pid, status="failed", community=community, package=package,
                    updated=kw.pop("updated", DAY + "T10:%02d:00Z" % (pid % 60)),
                    jobs=[_job(jid, "bits-prepub-build", "failed", web_url="https://gl/j/%d" % jid,
                               failure_reason=kw.pop("reason", "script_failure"),
                               finished_at=DAY + "T11:00:00Z",
                               runner={"description": "bits-build-01"})])
        self.gl.logs[jid] = log
        return jid

    def _pass(self):
        reconcile.discover(self.conn, self.gl)
        reconcile.settle(self.conn, self.gl)
        return failures.collect(self.conn, self.gl)


class ParseTest(unittest.TestCase):
    def test_bits_failure_blocks_are_found_and_cleaned(self):
        lines = failures.clean(bits_failure().encode())
        self.assertFalse(any(ESC in ln or "section_" in ln for ln in lines))
        (pkg, ver, excerpt, bv), = failures.parse(lines)
        self.assertEqual((pkg, ver, bv), ("arrow", "23.0.1", "0.6 (bits@35b7773abc)"))
        self.assertIn("Non-compatible flatbuffers version", excerpt)
        self.assertNotIn("Environment", excerpt)
        self.assertNotIn("Log File", excerpt)

    def test_several_packages_and_a_repeated_summary(self):
        log = bits_failure("ROOT", "6.32") + bits_failure("arrow") + bits_failure("ROOT", "6.32")
        self.assertEqual(sorted(p for p, *_ in failures.parse(failures.clean(log))), ["ROOT", "arrow"])

    def test_redaction(self):
        text = failures.redact(
            "PRIVATE-TOKEN: glpat-AbCdEf0123456789xyz\n"
            "Authorization: Bearer eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJlMTIz\n"
            "https://user:pa55word@host/x  ghp_abcdefghijklmnopqrstuvwxyz0123\n"
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG AKIAIOSFODNN7EXAMPLE\n"
            "password = \"two words\" api_key: k-123\n"
            "-----BEGIN RSA PRIVATE KEY-----\nMIIE\n-----END RSA PRIVATE KEY-----\n"
            "error: undefined reference to `token_count'\n")
        for secret in ("AbCdEf0123456789", "eyJhbGci", "pa55word", "abcdefghijklmnop",
                       "wJalrXUtnFEMI", "IOSFODNN7", "two words", "k-123", "MIIE"):
            self.assertNotIn(secret, text, secret)
        self.assertIn("https://<redacted>@host/x", text)
        self.assertIn("undefined reference to `token_count'", text)   # code is left alone

    def test_redaction_of_what_ci_logs_really_carry(self):
        lines = [
            "export PREPUB_API_TOKEN=abcd1234efgh", "BITS_FORGE_TOKEN=zzzz9999yyyy",
            "CI_JOB_TOKEN=qwerty12345", "CI_REGISTRY_PASSWORD=Sup3rS3cret", "client_secret=xyzw7788",
            'data: {"token": "tok-77aa88bb", "password":"pw-9988"}',
            "GET /api/v4/projects?private_token=ptk55667788&per_page=10",
            "git clone https://oauth2:gitlabtoken99@gitlab.cern.ch/a/b.git",
            "git clone https://plaintoken1234@github.com/a/b.git",
            "bits login --token tk9988776655 --password=hunter2hunter2",
            "AWS_ACCESS_KEY_ID=minioadmin12 aws_secret_access_key = 'ab cd ef gh'",
        ]
        out = failures.redact("\n".join(lines))
        for secret in ("abcd1234efgh", "zzzz9999yyyy", "qwerty12345", "Sup3rS3cret", "xyzw7788",
                       "tok-77aa88bb", "pw-9988", "ptk55667788", "gitlabtoken99", "plaintoken1234",
                       "tk9988776655", "hunter2hunter2", "minioadmin12", "ab cd ef gh"):
            self.assertNotIn(secret, out, secret)
        self.assertIn("per_page=10", out)
        self.assertIn("[MASKED]", failures.redact("TOKEN=[MASKED]") + "[MASKED]")

    def test_bits_error_lines_with_their_log_prefix(self):
        # bits' default formatter prefixes every line of an error with its level.
        plain = bits_failure()
        prefixed = "\n".join(("ERROR: " + ln) if i > 4 else ln
                              for i, ln in enumerate(plain.split("\n")))
        debug = "\n".join((DAY + "@09:00:01:ERROR:LCG:abc12345: " + ln) if i > 4 else ln
                           for i, ln in enumerate(plain.split("\n")))
        for log in (prefixed, debug):
            (pkg, ver, excerpt, bv), = failures.parse(failures.clean(log))
            self.assertEqual((pkg, ver, bv), ("arrow", "23.0.1", "0.6 (bits@35b7773abc)"))
            self.assertIn("Non-compatible flatbuffers version", excerpt)
            self.assertEqual(failures.fingerprint("arrow", failures.redact(excerpt))[0],
                             failures.fingerprint("arrow", failures.redact(
                                 failures.parse(failures.clean(plain))[0][2]))[0])

    def test_a_failure_without_bits_output_is_recognised_across_runs(self):
        def run(cid, job):
            log = ("$ ./deploy.sh\nfatal: could not read from remote repository\n"
                   "Uploading artifacts for failed job\n"
                   "Uploading artifacts... bits-build.log: found 1 matching artifact files\n"
                   "Uploading artifacts to coordinator... 201 Created  id=%d responseStatus=201"
                   " correlation_id=%s\nCleaning up project directory and file based variables\n"
                   "ERROR: Job failed: exit code 1\n" % (job, cid))
            ex = failures.last_lines(failures.clean(log))
            return failures.fingerprint("ROOT", ex)
        a, b = run("01JABCDEFG1234567890XYZ", 111), run("01JZZZZZZZ9876543210QQQ", 222)
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[1][-1], "fatal: could not read from remote repository")

    def test_the_real_error_line_beats_make_noise(self):
        ex = ("  Matched error lines (last 6):\n    src/a.cc:1:1: error: 'foo' was not declared\n"
              + "".join("    make[%d]: *** [x%d] Error 2\n" % (i, i) for i in range(5)))
        self.assertEqual(failures.key_lines(ex), ["src/a.cc:N:N: error: 'foo' was not declared"])

    def test_the_same_error_elsewhere_has_the_same_fingerprint(self):
        def fp(**kw):
            (_, _, ex, _), = failures.parse(failures.clean(bits_failure(**kw)))
            return failures.fingerprint("arrow", failures.redact(ex))[0]
        same = fp(path="/home/gitlab-runner/jobs/123/sw/BUILD/ffff0000aaaa/arrow", line=43)
        self.assertEqual(fp(), same)
        self.assertNotEqual(fp(), fp(error="fatal error: zlib.h: No such file or directory"))
        self.assertNotEqual(failures.fingerprint("arrow", "x: error: y")[0],
                            failures.fingerprint("ROOT", "x: error: y")[0])
        self.assertEqual(failures.fingerprint("x", "", "runner_system_failure")[1],
                         ["reason:runner_system_failure"])


class CollectTest(_DB):
    def test_a_failure_becomes_an_issue_named_by_its_signature(self):
        self._fail(1, bits_failure())
        self.assertEqual(self._pass(), 1)
        (f,) = failures.list_failures(self.conn)
        self.assertEqual((f["package"], f["version"], f["community"], f["source"], f["runner"],
                          f["bits_version"], f["log_url"]),
                         ("arrow", "23.0.1", "lcg", "log", "bits-build-01", "0.6 (bits@35b7773abc)",
                          "https://gl/j/10"))
        for secret in ("AbCdEf0123", "hunter2", "abc123"):
            self.assertNotIn(secret, f["excerpt"])
        (i,) = failures.list_issues(self.conn)
        self.assertEqual((i["count"], i["communities"], i["package"]), (1, ["lcg"], "arrow"))
        self.assertEqual(i["signature"]["cause"].split()[0], "Headers")
        kinds = [e["kind"] for e in events.since(self.conn, limit=100)]
        self.assertEqual(kinds[-2:], ["issue.opened", "failure.recorded"])
        self.assertEqual(self._pass(), 0)              # collected once

    def test_repeats_are_one_issue_across_communities(self):
        self._fail(1, bits_failure())
        self._fail(2, bits_failure(path="/other/jobs/7/sw/BUILD/99887766aa/arrow", line=50),
                   community="atlas")
        self._fail(3, bits_failure(error="fatal error: zlib.h: No such file or directory"))
        self._pass()
        issues = {i["count"]: i for i in failures.list_issues(self.conn)}
        self.assertEqual(sorted(issues), [1, 2])
        self.assertEqual(issues[2]["communities"], ["atlas", "lcg"])
        self.assertIsNone(issues[1]["signature"])

    def test_without_bits_output_the_last_lines_and_the_reason(self):
        self._fail(1, "Preparing environment\nERROR: Job failed (system failure): pod lost\n",
                   reason="runner_system_failure")
        self._pass()
        (f,) = failures.list_failures(self.conn)
        self.assertEqual(f["package"], "arrow")          # the pipeline's own package
        self.assertIn("pod lost", f["excerpt"])
        (i,) = failures.list_issues(self.conn)
        self.assertEqual(i["signature"]["action"], "Retry on another host")

    def test_a_log_cut_at_gitlabs_limit_is_read_from_the_artifact(self):
        jid = self._fail(1, "lots of output\nJob's log exceeded limit of 4194304 bytes.\n")
        self.gl.artifacts[jid] = bits_failure()
        self._pass()
        (f,) = failures.list_failures(self.conn)
        self.assertEqual((f["source"], f["version"]), ("artifact", "23.0.1"))
        self.assertIn(("artifact", jid, "bits-build.log"), self.gl.calls)

    def test_an_unreadable_artifact_falls_back_to_the_log(self):
        jid = self._fail(1, "lots of output\nJob's log exceeded limit of 4194304 bytes.\n")
        self.gl.artifacts[jid] = forge_ops.ForgeError(403, "forbidden")
        real = self.gl.job_artifact_tail

        def art(job_id, name, max_bytes=1 << 20):
            v = self.gl.artifacts.get(job_id)
            if isinstance(v, Exception):
                raise v
            return real(job_id, name, max_bytes)
        self.gl.job_artifact_tail = art
        with self.assertLogs("bits_orchestrator", "WARNING"):
            self.assertEqual(self._pass(), 1)
        (f,) = failures.list_failures(self.conn)
        self.assertEqual(f["source"], "log")

    def test_an_empty_log_is_source_none(self):
        self._fail(1, "")
        self._pass()
        self.assertEqual(failures.list_failures(self.conn)[0]["source"], "none")

    def test_a_failed_pipeline_with_no_failed_job_is_still_recorded(self):
        self.gl.add(1, status="failed", community="lcg", package="ROOT", jobs=[])
        self._pass()
        (f,) = failures.list_failures(self.conn)
        self.assertEqual((f["job"], f["failure_reason"], f["source"]), (-1, "pipeline:failed", "none"))
        self.assertEqual(self._pass(), 0)

    def test_a_job_failing_after_the_build_is_collected_too(self):
        self._fail(1, bits_failure())
        self._pass()
        self.gl.jobs[1] = self.gl.jobs[1] + [_job(11, "notify", "failed", stage="status")]
        self.gl.logs[11] = "curl: (6) Could not resolve host: hooks.example\n"
        self.gl.add(1, status="failed", community="lcg", package="arrow",
                    updated=DAY + "T13:00:00Z", jobs=self.gl.jobs[1])
        self._pass()
        self.assertEqual(sorted(f["job"] for f in failures.list_failures(self.conn)), [10, 11])

    def test_an_unreadable_log_is_retried_later_and_holds_up_nothing(self):
        self._fail(1, forge_ops.ForgeError(502, "bad gateway"))
        self._fail(2, bits_failure())
        with self.assertLogs("bits_orchestrator", "WARNING"):
            self.assertEqual(self._pass(), 1)
        row = self.conn.execute("SELECT collected_ms FROM pipelines WHERE id = 1").fetchone()
        self.assertLess(row[0], 0)
        self.gl.logs[10] = bits_failure()
        self.assertEqual(failures.collect(self.conn, self.gl), 0)          # not yet
        self.conn.execute("UPDATE pipelines SET collected_ms = -1 WHERE id = 1")
        self.assertEqual(failures.collect(self.conn, self.gl), 1)

    def test_a_retry_that_fails_again_is_collected_again(self):
        self._fail(1, bits_failure())
        self._pass()
        self.gl.add(1, status="failed", community="lcg", package="arrow", updated=DAY + "T12:00:00Z",
                    jobs=[_job(10, "bits-prepub-build", "failed"),
                          _job(12, "bits-prepub-build", "failed", failure_reason="script_failure")])
        self.gl.logs[12] = bits_failure()
        # running in between (the retry), then failed: collected anew
        self.conn.execute("UPDATE pipelines SET outcome = 'running' WHERE id = 1")
        self._pass()
        self.assertEqual([f["job"] for f in failures.list_failures(self.conn)], [12, 10])
        self.assertEqual(failures.list_issues(self.conn)[0]["count"], 2)

    def test_rematch_names_issues_after_a_new_signature(self):
        self._fail(1, bits_failure(error="fatal error: zlib.h: No such file or directory"))
        self._pass()
        self.assertIsNone(failures.list_issues(self.conn)[0]["signature"])
        with db.transaction(self.conn):
            self.conn.execute("INSERT INTO signatures (pattern, cause, action, proposed_by,"
                              " accepted_by, created_ms) VALUES ('zlib\\.h', 'zlib missing',"
                              " 'Add zlib', 'alice', 'carol', 0)")
        self.assertEqual(failures.rematch(self.conn), 1)
        self.assertEqual(failures.list_issues(self.conn)[0]["signature"]["cause"], "zlib missing")

    def test_seeds_once(self):
        n = len(failures.list_signatures(self.conn))
        self.assertEqual(failures.seed_signatures(self.conn), 0)
        self.assertEqual(len(failures.list_signatures(self.conn)), n)

    def test_a_new_seed_reaches_a_database_seeded_before_it(self):
        from console_backend.orch import reconcile
        with db.transaction(self.conn):                # as the first four left it
            self.conn.execute("DELETE FROM signatures WHERE id > ?", (failures.FIRST_SEEDS,))
            self.conn.execute("UPDATE signatures SET state = 'retired' WHERE id = 1")
            reconcile._kv_set(self.conn, "signatures.seeded", "1")
        self._fail(1, bits_failure(error="failed to add '/alice/x/.meta.json' to catalog: UNIQUE constraint"
                                         " failed: catalog.md5path_1, catalog.md5path_2"))
        self._pass()
        self.assertIsNone(failures.list_issues(self.conn)[0]["signature"])
        self.assertEqual(failures.seed_signatures(self.conn), len(failures.SEEDS) - failures.FIRST_SEEDS)
        self.assertIn("already published", failures.list_issues(self.conn)[0]["signature"]["cause"])
        sigs = failures.list_signatures(self.conn)
        self.assertEqual(len(sigs), len(failures.SEEDS))
        self.assertEqual(sigs[0]["state"], "retired")       # a retired seed stays retired


class BackfillTest(_DB):
    def test_older_failed_pipelines_are_read_page_by_page_until_the_horizon(self):
        for i in range(1, 46):
            self._fail(i, bits_failure(), updated=_day(10 + i % 28) + "T10:00:00Z")
        self._fail(99, bits_failure(), updated=_day(400) + "T00:00:00Z")   # beyond 365 days
        n = 0
        while failures.backfill(self.conn, self.gl, days=365, lookback_days=7, budget=20):
            n += 1
            self.assertLess(n, 10)
        ids = {r[0] for r in self.conn.execute("SELECT id FROM pipelines")}
        self.assertTrue(set(range(1, 46)) <= ids)
        self.assertEqual(reconcile._kv_get(self.conn, "backfill.done"), "1")
        self.assertEqual(failures.backfill(self.conn, self.gl, days=365), 0)
        # They settle and are collected like any other.
        reconcile.settle(self.conn, self.gl)
        reconcile.settle(self.conn, self.gl)
        while failures.collect(self.conn, self.gl):
            pass
        self.assertEqual(failures.list_issues(self.conn)[0]["count"], 45 + (99 in ids))

    def test_asking_for_more_days_later_carries_on(self):
        self._fail(1, bits_failure(), updated=_day(30) + "T10:00:00Z")
        self._fail(2, bits_failure(), updated=_day(200) + "T10:00:00Z")
        while failures.backfill(self.conn, self.gl, days=60, budget=1):
            pass
        ids = {r[0] for r in self.conn.execute("SELECT id FROM pipelines")}
        self.assertIn(1, ids)
        self.assertEqual(failures.backfill(self.conn, self.gl, days=60, budget=1), 0)
        while failures.backfill(self.conn, self.gl, days=400, budget=1):
            pass
        self.assertIn(2, {r[0] for r in self.conn.execute("SELECT id FROM pipelines")})

    def test_off_by_default(self):
        self.assertEqual(failures.backfill(self.conn, self.gl, days=0), 0)
        self.assertEqual(self.gl.calls, [])


class TailTest(unittest.TestCase):
    def _resp(self, status, chunks):
        class R:
            status_code = status

            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def iter_content(s, n):
                return iter(chunks)
        return R()

    def test_asks_for_the_end_and_caps_a_whole_body(self):
        f = forge_ops.GitLabForge("https://gl/api/v4", "t", "42")
        seen = {}

        def get(url, headers, timeout, stream, allow_redirects=True):
            seen["range"] = headers.get("Range")
            return self._resp(206, [b"x" * 10, b"END"])
        with patch.object(forge_ops.requests, "get", get):
            self.assertEqual(f.job_log_tail(5, max_bytes=8), b"xxxxxEND")
        self.assertEqual(seen["range"], "bytes=-8")
        with patch.object(forge_ops.requests, "get", lambda *a, **k: self._resp(200, [b"y" * 1000] * 10)):
            self.assertIsNone(f._tail("/x", 8, max_total=5000))
        with patch.object(forge_ops.requests, "get", lambda *a, **k: self._resp(404, [])):
            self.assertIsNone(f.job_artifact_tail(5, "bits-build.log"))


class ApiTest(_DB):
    def setUp(self):
        super().setUp()
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4",
            "BITS_ADMINS_POLICY": "lcg @alice\natlas @bob\n* @carol",
            "BITS_FORGE_PROJECT": "42", "BITS_ORCH_DB": self.path})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self._fail(1, bits_failure())
        self._fail(2, bits_failure(line=50), community="atlas")
        self._fail(3, bits_failure(error="fatal error: zlib.h: No such file or directory"))
        self._pass()

    def _get(self, user, path):
        return self.client.get(path, headers={"Authorization": "Bearer %s" % user})

    def test_issues_and_their_occurrences_are_limited_to_ones_communities(self):
        counts = lambda u, q="": sorted(i["count"] for i in self._get(u, "/ops/issues" + q).json()["issues"])
        self.assertEqual(counts("carol"), [1, 2])
        self.assertEqual(counts("alice"), [1, 1])          # only lcg's occurrences
        self.assertEqual(counts("bob"), [1])
        (mine,) = self._get("bob", "/ops/issues").json()["issues"]
        self.assertEqual((mine["communities"], mine["platforms"]), (["atlas"], ["el9"]))
        self.assertNotIn("lcg", str(mine))
        self.assertEqual(counts("dave"), [])
        self.assertEqual(counts("alice", "?community=atlas"), [])
        shared = mine["issue"]
        occ = self._get("bob", "/ops/issues/%d" % shared).json()["failures"]
        self.assertEqual([f["community"] for f in occ], ["atlas"])
        self.assertEqual(len(self._get("carol", "/ops/issues/%d" % shared).json()["failures"]), 2)
        only_lcg = [i["issue"] for i in self._get("alice", "/ops/issues").json()["issues"]
                    if i["issue"] != shared][0]
        self.assertEqual(self._get("bob", "/ops/issues/%d" % only_lcg).status_code, 404)

    def test_failures_and_signatures(self):
        got = self._get("bob", "/ops/failures").json()["failures"]
        self.assertEqual([f["pipeline"] for f in got], [2])
        self.assertNotIn("hunter2", got[0]["excerpt"])
        read = lambda q: [f["pipeline"] for f in self._get("dave", "/ops/failures" + q).json()["failures"]]
        self.assertEqual((read(""), read("?community=atlas")), ([], [2]))   # any signed-in user
        self.assertEqual([f["pipeline"] for f in self._get("carol", "/ops/failures?pipeline=3")
                          .json()["failures"]], [3])
        sigs = self._get("dave", "/ops/signatures").json()["signatures"]
        self.assertEqual(len(sigs), len(failures.SEEDS))
        self.assertEqual(self.client.get("/ops/issues").status_code, 401)


if __name__ == "__main__":
    unittest.main()
