# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator's M1: reconciling builds from GitLab and S3, the read API,
and the CI hints."""

import json
import os
import shutil
import tempfile
import unittest
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import ci_auth, config, forge_ops, main
from console_backend.orch import db, events, kinds, reconcile, worker

# The pipelines' day: yesterday, so they stay inside the lookback window and
# short of being stale whenever the tests run.
DAY = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))


def _job(i, name, status, stage="build", **kw):
    return dict({"id": i, "name": name, "stage": stage, "status": status,
                 "allow_failure": False}, **kw)


class FakeReader:
    """GitLab as the worker reads it: pipelines (by id), their variables, jobs."""

    def __init__(self):
        self.pipelines, self.variables, self.jobs = {}, {}, {}
        self.calls = []

    def add(self, pid, status="running", updated=DAY + "T10:00:00Z", source="api",
            community="testbed", package="ROOT", jobs=None):
        self.pipelines[pid] = {"id": pid, "status": status, "source": source,
                               "name": "[%s] %s 6.32 (el9)" % (community, package),
                               "ref": "main", "sha": "abc", "web_url": "https://gl/p/%d" % pid,
                               "created_at": DAY + "T09:00:00Z", "updated_at": updated}
        self.variables[pid] = ([{"key": "COMMUNITY", "value": community.upper()},
                                {"key": "PACKAGE", "value": package},
                                {"key": "VERSION", "value": "6.32"},
                                {"key": "PLATFORM", "value": "el9"}] if package else [])
        self.jobs[pid] = jobs if jobs is not None else [_job(pid * 10, "build", "running")]

    def list_pipelines(self, updated_after, page=1, per_page=100):
        self.calls.append(("list", updated_after, page))
        w = reconcile._when
        got = sorted((p for p in self.pipelines.values() if w(p["updated_at"]) > w(updated_after)),
                     key=lambda p: (w(p["updated_at"]), p["id"]))
        return [dict(p) for p in got[(page - 1) * per_page: page * per_page]]   # a snapshot

    def get_pipeline(self, pid):
        self.calls.append(("get", pid))
        if pid not in self.pipelines:
            raise forge_ops.ForgeError(404, "404 Not found")
        return dict(self.pipelines[pid])

    def pipeline_variables(self, pid):
        self.calls.append(("vars", pid))
        return self.variables[pid]

    def pipeline_jobs(self, pid):
        self.calls.append(("jobs", pid))
        if isinstance(self.jobs.get(pid), Exception):
            raise self.jobs[pid]
        if pid not in self.pipelines:
            raise forge_ops.ForgeError(404, "404 Not found")
        return self.jobs[pid]


class FakeS3:
    def __init__(self, objects=None, page=2):
        self.objects = {k: json.dumps(v).encode() for k, v in (objects or {}).items()}
        self.page = page

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)

        class B:
            def __init__(s, d):
                s.d = d

            def read(s):
                return s.d
        return {"Body": B(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        chunk = keys[start:start + self.page]
        more = start + self.page < len(keys)
        return {"Contents": [{"Key": k} for k in chunk], "IsTruncated": more,
                "NextContinuationToken": str(start + self.page) if more else None}


def _bom(arch, n=2, build_id="b-1"):
    return {"build_id": build_id, "effective_architecture": arch, "published_at": DAY + "T11:00:00Z",
            "requested_packages": ["ROOT"],
            "packages": [{"package": "p%d" % i, "hash": "h%d" % i} for i in range(n)]}


class _DB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "orch.db")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.gl = FakeReader()

    def _kinds(self):
        return [e["kind"] for e in events.since(self.conn, limit=1000)]

    def _row(self, pid):
        return self.conn.execute("SELECT * FROM pipelines WHERE id = ?", (pid,)).fetchone()

    def _pass(self, store=None):
        reconcile.apply_hints(self.conn, self.gl)
        reconcile.discover(self.conn, self.gl)
        reconcile.settle(self.conn, self.gl)
        reconcile.index(self.conn, store)


class OutcomeTest(unittest.TestCase):
    def test_outcome_comes_from_the_latest_job_attempts(self):
        o = reconcile.outcome_from_jobs
        self.assertEqual(o("running", [_job(1, "build", "running")]), "running")
        self.assertEqual(o("running", [_job(1, "build", "success"),
                                       _job(2, "status", "success", stage="status")]), "success")
        # bits' progress status holds the pipeline running after every job ended.
        self.assertEqual(o("running", [_job(1, "build", "success")]), "success")
        # GitLab failed it on a timed-out status, but every job succeeded.
        self.assertEqual(o("failed", [_job(1, "build", "success")]), "success")
        # A failed build decides, even with a later job wedged.
        self.assertEqual(o("running", [_job(1, "build", "failed"),
                                       _job(2, "status", "waiting_for_resource", stage="status")]),
                         "failed")
        # A retried job: the latest attempt counts.
        self.assertEqual(o("running", [_job(1, "build", "failed"), _job(3, "build", "running")]),
                         "running")
        self.assertEqual(o("success", [_job(1, "build", "failed"), _job(3, "build", "success")]),
                         "success")
        self.assertEqual(o("failed", [_job(1, "build", "success"),
                                      _job(2, "lint", "failed", stage="test", allow_failure=True)]),
                         "success")
        self.assertEqual(o("canceled", [_job(1, "build", "canceled")]), "canceled")
        self.assertEqual(o("pending", []), "running")
        self.assertEqual(o("failed", []), "failed")


class ReconcileTest(_DB):
    def test_a_build_is_seen_settled_and_its_builds_recorded(self):
        self.gl.add(1)
        self._pass()
        r = self._row(1)
        self.assertEqual((r["is_build"], r["community"], r["package"], r["outcome"]),
                         (1, "testbed", "ROOT", "running"))
        self.assertEqual(self._kinds(), ["pipeline.seen"])
        # It finishes; the build index is in S3.
        self.gl.add(1, status="success", updated=DAY + "T12:00:00Z",
                    jobs=[_job(10, "build", "success", runner={"description": "bits-build-01"})])
        s3 = FakeS3({"BUILDS/by-pipeline/1/b-1.json": {"build_id": "b-1",
                                                       "keys": ["MANIFESTS/b-1/x.el9.json",
                                                                "MANIFESTS/b-1/x.share.json"]},
                     "MANIFESTS/b-1/x.el9.json": _bom("el9", 3),
                     "MANIFESTS/b-1/x.share.json": _bom("share", 1)}, page=1)
        self._pass((s3, "bkt"))
        r = self._row(1)
        self.assertEqual(r["outcome"], "success")
        self.assertEqual(json.loads(r["jobs"])[0]["runner"], "bits-build-01")
        got = reconcile.list_builds(self.conn)[0]["builds"]
        self.assertEqual(got, [{"build_id": "b-1", "state": "published", "arches": ["el9", "share"],
                                "packages": 4, "top_level": ["ROOT"],
                                "published_at": DAY + "T11:00:00Z"}])
        self.assertEqual(self._kinds(), ["pipeline.seen", "pipeline.success", "build.published"])
        # Nothing new: nothing is written again.
        self._pass((s3, "bkt"))
        self.assertEqual(len(self._kinds()), 3)
        self.assertEqual(events.verify(self.conn)[1], None)

    def test_variables_are_read_once_and_non_builds_are_kept_aside(self):
        self.gl.add(1)
        self.gl.add(2, source="push", package="")      # Pages / push pipeline
        self.gl.add(3, package="")                     # an api pipeline that is no build
        self._pass()
        self._pass()
        self.assertEqual([c for c in self.gl.calls if c[0] == "vars"], [("vars", 1), ("vars", 3)])
        self.assertEqual([b["pipeline"] for b in reconcile.list_builds(self.conn)], [1])

    def test_the_cursor_moves_and_overlaps(self):
        self.gl.add(1, updated=DAY + "T10:00:00Z")
        reconcile.discover(self.conn, self.gl)
        self.assertEqual(reconcile._kv_get(self.conn, "gitlab.updated_after"),
                         DAY + "T10:00:00Z")
        self.gl.calls.clear()
        reconcile.discover(self.conn, self.gl)
        self.assertEqual(self.gl.calls[0][1], DAY + "T09:55:00Z")   # 5 minutes back

    def _count(self):
        return self.conn.execute("SELECT count(*) FROM pipelines").fetchone()[0]

    def test_many_pages_continue_on_the_next_pass(self):
        for i in range(1, 251):
            self.gl.add(i, updated=DAY + "T10:%02d:%02dZ" % (i // 60, i % 60))
        reconcile.discover(self.conn, self.gl, max_rounds=1)
        self.assertEqual(self._count(), 100)
        reconcile.discover(self.conn, self.gl, max_rounds=1)
        reconcile.discover(self.conn, self.gl, max_rounds=1)
        self.assertEqual(self._count(), 250)

    def test_more_than_a_page_in_one_second(self):
        for i in range(1, 251):
            self.gl.add(i, updated=DAY + "T10:00:00Z")
        for _ in range(4):
            reconcile.discover(self.conn, self.gl, max_rounds=1)
        self.assertEqual(self._count(), 250)
        reconcile.discover(self.conn, self.gl)          # and then it settles down
        self.assertEqual(reconcile._kv_get(self.conn, "gitlab.continue"), "0")

    def test_a_pipeline_updating_while_pages_are_read_is_not_skipped(self):
        for i in range(1, 151):
            self.gl.add(i, status="success", updated=DAY + "T10:%02d:%02dZ" % (i // 60, i % 60),
                        jobs=[_job(i * 10, "build", "success")])
        real = self.gl.list_pipelines

        def shifting(updated_after, page=1, per_page=100):
            got = real(updated_after, page, per_page)
            if 1 in self.gl.pipelines and self.gl.pipelines[1]["updated_at"] < DAY + "T11":
                self.gl.pipelines[1]["updated_at"] = DAY + "T11:00:00Z"   # moves to the end
            return got
        self.gl.list_pipelines = shifting
        reconcile.discover(self.conn, self.gl)
        self.assertEqual(self._count(), 150)

    def test_times_with_offsets_and_fractions(self):
        # After the clocks go back: the later update has the smaller local time.
        self.gl.add(1, updated="2026-10-25T02:50:00.250+02:00")              # 00:50 UTC
        self.gl.add(2, updated="2026-10-25T02:10:00+01:00", community="x")   # 01:10 UTC
        reconcile.discover(self.conn, self.gl)
        self.assertEqual(reconcile._kv_get(self.conn, "gitlab.updated_after"),
                         "2026-10-25T01:10:00Z")

    def test_a_running_pipeline_is_rechecked_at_most_every_two_minutes(self):
        self.gl.add(1)
        self._pass()
        self.gl.calls.clear()
        self._pass()
        self.assertNotIn(("jobs", 1), self.gl.calls)
        self.conn.execute("UPDATE pipelines SET checked_ms = 1")
        self._pass()
        self.assertIn(("jobs", 1), self.gl.calls)

    def test_a_failed_build_with_a_wedged_status_job(self):
        self.gl.add(1, status="running", jobs=[
            _job(10, "build", "failed", failure_reason="script_failure"),
            _job(11, "update-status", "waiting_for_resource", stage="status")])
        self._pass()
        self.assertEqual(self._row(1)["outcome"], "failed")
        e = events.since(self.conn)[-1]
        self.assertEqual((e["kind"], e["payload"]["failed_jobs"]), ("pipeline.failed", ["build"]))
        # Later updates to the wedged job do not flap the outcome.
        self.gl.add(1, status="running", updated=DAY + "T13:00:00Z",
                    jobs=self.gl.jobs[1])
        self._pass()
        self.assertEqual(self._kinds(), ["pipeline.seen", "pipeline.failed"])

    def test_a_retry_runs_again_and_publishes(self):
        self.gl.add(1, status="failed", jobs=[_job(10, "build", "failed")])
        self._pass()
        self.gl.add(1, status="running", updated=DAY + "T13:00:00Z",
                    jobs=[_job(10, "build", "failed"), _job(12, "build", "running")])
        self._pass()
        self.assertEqual(self._row(1)["outcome"], "running")
        self.gl.add(1, status="success", updated=DAY + "T14:00:00Z",
                    jobs=[_job(10, "build", "failed"), _job(12, "build", "success")])
        s3 = FakeS3({"BUILDS/by-pipeline/1/b.json": {"build_id": "b", "keys": ["M/b.json"]},
                     "M/b.json": _bom("el9", build_id="b")})
        self._pass((s3, "bkt"))
        self.assertEqual(self._kinds(), ["pipeline.seen", "pipeline.failed", "pipeline.running",
                                         "pipeline.success", "build.published"])

    def test_several_builds_in_one_pipeline_and_bad_index_entries(self):
        self.gl.add(1, status="success", jobs=[_job(10, "build", "success")])
        s3 = FakeS3({"BUILDS/by-pipeline/1/opt.json": {"build_id": "opt", "keys": ["M/o.json"]},
                     "BUILDS/by-pipeline/1/dbg.json": {"build_id": "dbg",
                                                       "keys": ["M/d.json", "M/missing.json"]},
                     "BUILDS/by-pipeline/1/junk.json": ["not", "an", "index"],
                     "BUILDS/by-pipeline/10/x.json": {"build_id": "other", "keys": []},
                     "M/o.json": _bom("el9-opt", build_id="opt"),
                     "M/d.json": _bom("el9-dbg", build_id="dbg")})
        with self.assertLogs("bits_orchestrator", "WARNING"):
            self._pass((s3, "bkt"))
        got = {b["build_id"]: b["arches"] for b in reconcile.list_builds(self.conn)[0]["builds"]}
        self.assertEqual(got, {"opt": ["el9-opt"], "dbg": ["el9-dbg"]})

    def test_no_index_yet_is_retried_then_given_up(self):
        self.gl.add(1, status="success", jobs=[_job(10, "build", "success")])
        s3 = FakeS3({})
        self._pass((s3, "bkt"))
        self.assertEqual(reconcile.index(self.conn, (s3, "bkt")), 0)
        s3.objects.update({k: json.dumps(v).encode() for k, v in {
            "BUILDS/by-pipeline/1/b.json": {"build_id": "b", "keys": ["M/b.json"]},
            "M/b.json": _bom("el9", build_id="b")}.items()})
        self.assertEqual(reconcile.index(self.conn, (s3, "bkt")), 0)       # not yet due
        self.conn.execute("UPDATE pipelines SET indexed_ms = 1")
        self.assertEqual(reconcile.index(self.conn, (s3, "bkt")), 1)
        # Long after it finished, a miss is not looked at again (without a hint).
        self.gl.add(2, status="success", jobs=[_job(20, "build", "success")])
        self._pass((s3, "bkt"))
        self.conn.execute("UPDATE pipelines SET indexed_ms = 1, checked_ms = 1 WHERE id = 2")
        s3.objects["BUILDS/by-pipeline/2/c.json"] = json.dumps(
            {"build_id": "c", "keys": ["M/b.json"]}).encode()
        self.assertEqual(reconcile.index(self.conn, (s3, "bkt")), 0)
        reconcile.add_hint(self.conn, 2, "published")
        self._pass((s3, "bkt"))
        self.assertEqual(len(reconcile.list_builds(self.conn, pipelines=[2])[0]["builds"]), 1)

    def test_a_hint_fetches_a_pipeline_older_than_the_window(self):
        self.gl.add(7, status="success", updated="2020-01-01T00:00:00Z",
                    jobs=[_job(70, "build", "success")])
        self._pass()
        self.assertIsNone(self._row(7))
        reconcile.add_hint(self.conn, 7, "published")
        self._pass()
        self.assertEqual(self._row(7)["outcome"], "success")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hints").fetchone()[0], 0)

    def test_hints_gitlab_does_not_know_are_dropped_others_kept(self):
        reconcile.add_hint(self.conn, 99, "failed")      # GitLab: 404
        with self.assertLogs("bits_orchestrator", "WARNING"):
            reconcile.apply_hints(self.conn, self.gl)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hints").fetchone()[0], 0)
        self.gl.get_pipeline = lambda pid: (_ for _ in ()).throw(forge_ops.ForgeError(502, "x"))
        reconcile.add_hint(self.conn, 98, "failed")      # GitLab unwell: kept
        with self.assertLogs("bits_orchestrator", "WARNING"):
            reconcile.apply_hints(self.conn, self.gl)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hints").fetchone()[0], 1)
        self.conn.execute("UPDATE hints SET ts_ms = 1")  # a day later: dropped
        with self.assertLogs("bits_orchestrator", "WARNING"):
            reconcile.apply_hints(self.conn, self.gl)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM hints").fetchone()[0], 0)

    def test_one_pipeline_gitlab_cannot_answer_for_does_not_hold_up_the_others(self):
        self.gl.add(1)
        self.gl.add(2, updated=DAY + "T10:01:00Z")
        self._pass()
        del self.gl.pipelines[1]                          # deleted on GitLab
        self.gl.jobs[2] = [_job(20, "build", "success")]
        self.conn.execute("UPDATE pipelines SET checked_ms = 1")
        self._pass()
        self.assertEqual((self._row(1)["outcome"], self._row(2)["outcome"]), ("canceled", "success"))
        gone = [e for e in events.since(self.conn) if e["subject"] == "pipeline:1"][-1]
        self.assertTrue(gone["payload"]["gone"])
        self.gl.add(3, updated=DAY + "T10:02:00Z")
        self.gl.jobs[3] = forge_ops.ForgeError(502, "bad gateway")
        with self.assertLogs("bits_orchestrator", "WARNING"):
            self._pass()
        self.assertEqual(self._row(3)["outcome"], "running")
        self.assertGreater(self._row(3)["checked_ms"], 0)  # backs off, tried later

    def test_a_manual_job_does_not_hold_a_pipeline_running(self):
        self.gl.add(1, status="success", jobs=[_job(10, "build", "success"),
                                               _job(11, "deploy", "manual", stage="deploy",
                                                    allow_failure=True)])
        self._pass()
        self.assertEqual(self._row(1)["outcome"], "success")

    def test_a_pipeline_running_for_days_is_settled_from_gitlab(self):
        self.gl.add(1)
        self.gl.pipelines[1]["created_at"] = "2026-01-01T00:00:00Z"
        self._pass()
        self.assertEqual(self._row(1)["outcome"], "failed")
        self.assertTrue(events.since(self.conn)[-1]["payload"]["stale"])

    def test_a_failing_index_records_nothing_and_does_not_block_others(self):
        self.gl.add(1, status="success", jobs=[_job(10, "build", "success")])
        self.gl.add(2, status="success", updated=DAY + "T10:01:00Z",
                    jobs=[_job(20, "build", "success")])
        s3 = FakeS3({"BUILDS/by-pipeline/1/a.json": {"build_id": "a", "keys": ["M/a.json"]},
                     "BUILDS/by-pipeline/1/b.json": {"build_id": "b", "keys": ["M/b.json"]},
                     "BUILDS/by-pipeline/2/c.json": {"build_id": "c", "keys": ["M/c.json"]},
                     "M/a.json": _bom("el9", build_id="a"), "M/c.json": _bom("el9", build_id="c")})
        real = reconcile._bom_facts

        def boom(s3_, bucket, keys):
            if keys == ["M/b.json"]:
                raise RuntimeError("store hiccup")
            return real(s3_, bucket, keys)
        with patch.object(reconcile, "_bom_facts", boom), \
                self.assertLogs("bits_orchestrator", "WARNING"):
            self._pass((s3, "bkt"))
        got = {b["pipeline"]: [x["build_id"] for x in b["builds"]]
               for b in reconcile.list_builds(self.conn)}
        self.assertEqual(got, {1: [], 2: ["c"]})          # all of 1 or none of it
        reconcile.add_hint(self.conn, 1, "published")
        self._pass((s3, "bkt"))
        got = [x["build_id"] for x in reconcile.list_builds(self.conn, pipelines=[1])[0]["builds"]]
        self.assertEqual(got, ["a", "b"])

    def test_unreadable_variables_fall_back_to_the_name(self):
        self.gl.add(1, community="lcg", package="ROOT")
        self.gl.pipelines[1]["name"] = "[LCG] ROOT 6.32.08 (x86_64-el9) [nightly]"
        self.gl.add(2, community="lcg", package="", updated=DAY + "T10:01:00Z")
        self.gl.pipelines[2]["name"] = "Pages"                     # no build
        def refuse(pid):
            raise forge_ops.ForgeError(403, "GET /pipelines/%s/variables -> 403" % pid)
        self.gl.pipeline_variables = refuse
        with self.assertLogs("bits_orchestrator", "WARNING"):
            self._pass()
        r = self._row(1)
        self.assertEqual((r["is_build"], r["community"], r["package"], r["version"], r["platform"]),
                         (1, "lcg", "ROOT", "6.32.08", "x86_64-el9"))
        self.assertEqual(self._row(2)["is_build"], 0)
        self.assertEqual(reconcile._from_name("[ALICE] O2  (el9)")["VERSION"], "")

    def test_variables_are_read_with_the_ops_token(self):
        s = config.Settings(env={"GITLAB_API_URL": "https://gl/api/v4", "BITS_FORGE_PROJECT": "42",
                                 "BITS_FORGE_READ_TOKEN": "ro", "BITS_FORGE_OPS_TOKEN": "ops"})
        r = kinds.Deps(s).reader()
        self.assertEqual(r._token, "ro")
        self.assertEqual(r.pipeline_variables.__self__._token, "ops")
        s.forge_ops_token = ""
        self.assertFalse(hasattr(kinds.Deps(s).reader().pipeline_variables, "__self__")
                         and kinds.Deps(s).reader().pipeline_variables.__self__._token == "ops")

    def test_variable_names_are_case_insensitive(self):
        self.gl.add(1)
        self.gl.variables[1] = [{"key": "community", "value": "TestBed"},
                                {"key": "Package", "value": "ROOT"}]
        self._pass()
        self.assertEqual((self._row(1)["community"], self._row(1)["package"]), ("testbed", "ROOT"))

    def test_the_listing_without_finished_at_does_not_count_as_a_change(self):
        self.gl.add(1, status="success", jobs=[_job(10, "build", "success")])
        self._pass()
        self.gl.pipelines[1]["finished_at"] = DAY + "T10:30:00Z"
        reconcile.record_pipeline(self.conn, self.gl, self.gl.get_pipeline(1))
        self.assertEqual(self._row(1)["finished_at"], DAY + "T10:30:00Z")
        listed = {k: v for k, v in self.gl.pipelines[1].items() if k != "finished_at"}
        before = self._row(1)["checked_ms"]
        reconcile.record_pipeline(self.conn, self.gl, listed)
        self.assertEqual((self._row(1)["checked_ms"], self._row(1)["finished_at"]),
                         (before, DAY + "T10:30:00Z"))

    def test_list_filters(self):
        self.gl.add(1, community="testbed")
        self.gl.add(2, community="other", updated=DAY + "T10:01:00Z")
        self.gl.add(3, community="testbed", status="failed", updated=DAY + "T10:02:00Z",
                    jobs=[_job(30, "build", "failed")])
        self._pass()
        ids = lambda **kw: [b["pipeline"] for b in reconcile.list_builds(self.conn, **kw)]
        self.assertEqual(ids(), [3, 2, 1])
        self.assertEqual(ids(communities={"testbed"}), [3, 1])
        self.assertEqual(ids(communities=set()), [])
        self.assertEqual(ids(outcome="failed"), [3])
        self.assertEqual(ids(pipelines=[1, 2]), [2, 1])
        detail = reconcile.get_build(self.conn, 3)
        self.assertEqual((detail["jobs"][0]["status"], detail["variables"]["PACKAGE"]),
                         ("failed", "ROOT"))
        self.assertEqual([e["kind"] for e in detail["events"]], ["pipeline.seen", "pipeline.failed"])


class WorkerStepsTest(_DB):
    def test_the_worker_runs_reconcile_before_actions(self):
        w = worker.Worker(self.conn, config.Settings(env={}))
        self.assertEqual([n for n, _ in w.steps], ["actions", "hints", "discover", "settle", "index",
                                                    "collect", "backfill", "phases", "trace", "ship", "backup"])
        w.deps.reader = lambda: self.gl
        w.deps.store = lambda: None
        self.gl.add(1)
        w.run_once()
        self.assertEqual(self._row(1)["outcome"], "running")

    def test_the_cvmfs_status_step_follows_index_when_asked_for(self):
        w = worker.Worker(self.conn, config.Settings(env={"BITS_ORCH_CVMFS_STATUS": "1"}))
        names = [n for n, _ in w.steps]
        self.assertEqual(names[names.index("index") + 1], "cvmfs_status")
        w.steps = [s for s in w.steps if s[0] == "cvmfs_status"]
        with self.assertLogs("bits_orchestrator", "ERROR"):
            w.run_once()                      # no ops token, no store: says so
        got = [e for e in events.since(self.conn) if e["kind"] == "worker.step_failed"]
        self.assertIn("BITS_ORCH_CVMFS_STATUS needs", got[0]["payload"]["error"])

    def test_unconfigured_gitlab_is_one_event(self):
        w = worker.Worker(self.conn, config.Settings(env={}))
        w.steps = [s for s in w.steps if s[0] == "hints"]
        with self.assertLogs("bits_orchestrator", "ERROR"):
            w.run_once()
            w.run_once()
        got = [e for e in events.since(self.conn) if e["kind"] == "worker.step_failed"]
        self.assertEqual(len(got), 1)
        self.assertIn("GitLab not configured", got[0]["payload"]["error"])

    def test_the_store_is_built_from_the_real_s3_backend(self):
        from bits_helpers import sync
        d = kinds.Deps(config.Settings(env={"BITS_ORCH_STORE": "b3://bits-store::rw"}))
        with patch.object(sync.Boto3RemoteSync, "_s3_init",
                          lambda self: setattr(self, "s3", "client")):
            self.assertEqual(d.store(), ("client", "bits-store"))

    def test_store_settings(self):
        d = kinds.Deps(config.Settings(env={}))
        self.assertIsNone(d.store())
        d = kinds.Deps(config.Settings(env={"BITS_ORCH_STORE": "https://x/y"}))
        with self.assertRaises(kinds.NotConfigured):
            d.store()


class ApiTest(_DB):
    def setUp(self):
        super().setUp()
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4",
            "BITS_ADMINS_POLICY": "testbed @alice\nother @bob\n* @carol",
            "BITS_FORGE_PROJECT": "42", "BITS_ORCH_DB": self.path,
            "BITS_OIDC_ISSUER": "https://gl", "BITS_OIDC_CI_AUDIENCE": "bits",
            "BITS_OIDC_JWKS_URL": "https://gl/jwks"})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.gl.add(1, community="testbed")
        self.gl.add(2, community="other", updated=DAY + "T10:01:00Z")
        self._pass()

    def _get(self, user, path):
        return self.client.get(path, headers={"Authorization": "Bearer %s" % user})

    def test_builds_are_those_of_ones_communities(self):
        ids = lambda u, q="": [b["pipeline"] for b in self._get(u, "/ops/builds" + q).json()["builds"]]
        self.assertEqual(ids("alice"), [1])
        self.assertEqual(ids("bob"), [2])
        self.assertEqual(ids("carol"), [2, 1])
        self.assertEqual(ids("dave"), [])
        self.assertEqual(ids("alice", "?community=other"), [])
        self.assertEqual(ids("carol", "?community=OTHER"), [2])
        self.assertEqual(ids("carol", "?pipelines=1,99"), [1])
        self.assertEqual(self._get("alice", "/ops/builds?pipelines=x").status_code, 400)
        self.assertEqual(self._get("alice", "/ops/builds?outcome=weird").status_code, 400)
        self.assertEqual(self.client.get("/ops/builds").status_code, 401)

    def test_one_build_with_its_jobs_and_events(self):
        r = self._get("alice", "/ops/builds/1")
        self.assertEqual((r.status_code, r.json()["package"]), (200, "ROOT"))
        self.assertEqual([e["kind"] for e in r.json()["events"]], ["pipeline.seen"])
        self.assertEqual(self._get("bob", "/ops/builds/1").status_code, 404)
        self.assertEqual(self._get("carol", "/ops/builds/77").status_code, 404)

    def test_trace_names_the_publishing_pipeline_to_its_community(self):
        with db.transaction(self.conn):
            self.conn.execute("INSERT INTO published_paths (repo, path, build_id, job_id, ts_ms)"
                              " VALUES ('r', 'a/ROOT/6-1', '1', 'j', 1)")
        q = "/ops/trace?repo=r&path=/a/ROOT/6-1/lib/x.so"
        (pub,) = self._get("alice", q).json()["publishes"]
        self.assertEqual((pub["pipeline"], pub["visible"], pub["package"]), (1, True, "ROOT"))
        (pub,) = self._get("bob", q).json()["publishes"]
        self.assertEqual(pub, dict(pub, pipeline=1, visible=False))
        self.assertNotIn("approval", pub)
        self.assertEqual(self.client.get(q).status_code, 401)

    def test_a_builds_sbom_for_any_signed_in_user(self):
        from console_backend.orch import api

        class Missing(Exception):
            response = {"Error": {"Code": "NoSuchKey"}}

        class S3(FakeS3):
            def get_object(self, Bucket, Key):
                if Key not in self.objects:
                    raise Missing(Key)
                return FakeS3.get_object(self, Bucket, Key)
        rec = lambda pid, bid: self.conn.execute(
            "INSERT INTO builds (pipeline_id, build_id, community, arches, bom_keys, top_level,"
            " packages, published_at, recorded_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, bid, "testbed", "[]", "[]", "[]", 0, "", 0))
        rec(1, "b-1")
        self.conn.commit()
        s3 = S3({"MANIFESTS/b-1/sbom.cdx.json": {"bomFormat": "CycloneDX"},
                 "MANIFESTS/b-1/sbom.spdx.json": {"spdxVersion": "SPDX-2.3"}})
        with patch.object(api, "_store", lambda: (s3, "bkt")):
            r = self._get("dave", "/ops/builds/1/sbom")      # administers nothing
            self.assertEqual((r.status_code, r.json()), (200, {"bomFormat": "CycloneDX"}))
            r = self._get("bob", "/ops/builds/1/sbom?format=spdx")
            self.assertEqual(r.json(), {"spdxVersion": "SPDX-2.3"})
            self.assertEqual(self.client.get("/ops/builds/1/sbom").status_code, 401)
            self.assertEqual(self._get("dave", "/ops/builds/1/sbom?format=x").status_code, 400)
            self.assertEqual(self._get("dave", "/ops/builds/2/sbom").status_code, 404)
            rec(1, "b-2")
            self.conn.commit()
            r = self._get("dave", "/ops/builds/1/sbom")       # several builds: say which
            self.assertEqual(r.status_code, 400)
            self.assertIn("b-1, b-2", r.json()["detail"])
            self.assertEqual(self._get("dave", "/ops/builds/1/sbom?build=b-2").status_code, 404)
        with patch.object(api, "_store", lambda: None):
            self.assertEqual(self._get("dave", "/ops/builds/1/sbom?build=b-1").status_code, 503)

        def down():
            raise OSError("credentials proxy unavailable")
        with patch.object(api, "_store", down):
            self.assertEqual(self._get("dave", "/ops/builds/1/sbom?build=b-1").status_code, 503)

    def _ci(self, claims, body, kind="published"):
        seen = {}

        def verify(t, s, key=None, audience=None):
            seen["aud"] = audience
            return claims
        with patch.object(ci_auth, "verify_ci_token", verify):
            r = self.client.post("/ops/ci/" + kind, json=body,
                                 headers={"Authorization": "Bearer jwt"})
        self.assertEqual(seen.get("aud"), "bits-orchestrator")    # never the signing audience
        return r

    def test_ci_hints_need_an_audience_of_their_own(self):
        good = {"project_id": "42", "ref_protected": "true", "pipeline_id": "5"}
        main.settings.orch_ci_audience = ""
        with patch.object(ci_auth, "verify_ci_token", lambda *a, **k: good):
            r = self.client.post("/ops/ci/published", json={"pipeline": 5},
                                 headers={"Authorization": "Bearer jwt"})
        self.assertEqual(r.status_code, 503)
        main.settings.orch_ci_audience = main.settings.oidc_ci_audience
        with patch.object(ci_auth, "verify_ci_token", lambda *a, **k: good):
            r = self.client.post("/ops/ci/published", json={"pipeline": 5},
                                 headers={"Authorization": "Bearer jwt"})
        self.assertEqual(r.status_code, 503)

    def test_ci_hints_need_the_build_projects_token_for_that_pipeline(self):
        good = {"project_id": "42", "project_path": "bits/console", "ref_protected": "true",
                "pipeline_id": "5"}
        self.assertEqual(self._ci(good, {"pipeline": 5}).json(), {"status": "noted"})
        with patch.object(ci_auth, "_jwks_client") as jw:   # the real check: its own audience
            import jwt as _jwt
            with patch.object(_jwt, "decode", lambda *a, **k: dict(good, aud=k["audience"])):
                jw.return_value.get_signing_key_from_jwt.return_value.key = "k"
                c = ci_auth.verify_ci_token("t", main.settings, audience="bits-orchestrator")
        self.assertEqual(c["aud"], "bits-orchestrator")
        self.assertEqual(self._ci(good, {"pipeline": 5}, "failed").status_code, 200)
        self.assertEqual(self.conn.execute("SELECT kind FROM hints").fetchall()[0][0], "failed")
        self.assertEqual(self._ci(dict(good, project_id="7", project_path="x/y"),
                                  {"pipeline": 5}).status_code, 403)
        self.assertEqual(self._ci(dict(good, ref_protected="false"), {"pipeline": 5}).status_code,
                         403)
        self.assertEqual(self._ci(good, {"pipeline": 6}).status_code, 403)
        self.assertEqual(self._ci(good, {"pipeline": "x"}).status_code, 400)
        self.assertEqual(self.client.post("/ops/ci/published", json={"pipeline": 5}).status_code,
                         401)

        def bad(t, s, key=None, audience=None):
            raise Exception("bad signature")
        with patch.object(ci_auth, "verify_ci_token", bad):
            r = self.client.post("/ops/ci/published", json={"pipeline": 5},
                                 headers={"Authorization": "Bearer jwt"})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
