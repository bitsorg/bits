# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""What a finished build's job spent its time on (the console's timeline)."""

import json
import os
import shutil
import tempfile
import unittest

from console_backend import forge_ops
from console_backend.orch import db, phases

T0 = 1792600000          # seconds


def _iso(s):
    import datetime
    return datetime.datetime.fromtimestamp(s, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _job(jid=7, start=T0 + 60, end=T0 + 1000, stage="build"):
    return {"id": jid, "stage": stage, "name": "bits-prepub-build", "status": "success",
            "started_at": _iso(start), "finished_at": _iso(end), "runner": "pcphsft204"}


def _rec(end, total, commit, outcome="published"):
    return {"ts": _iso(end).replace("Z", ".000456789Z"), "total_s": total, "commit_s": commit,
            "outcome": outcome}


class FakeReader:
    def __init__(self, files=None, fail=None):
        self.files, self.fail, self.calls = files or {}, fail, []

    def job_artifact(self, job_id, name):
        self.calls.append((job_id, name))
        if self.fail:
            raise self.fail
        return self.files.get((job_id, name))


class SegmentsTest(unittest.TestCase):
    def test_the_job_its_marks_and_prepubs_records(self):
        m = phases.marks("build_start %d\nbuild_end %d\npublish_start %d\npublish_end %d\nnoise\n"
                         % (T0 + 100, T0 + 700, T0 + 710, T0 + 990))
        recs = [_rec(T0 + 900, 150, 50), _rec(T0 + 980, 200, 30),   # overlapping packages
                _rec(T0 + 990, 0, 0), {"ts": "bad"}, "x"]
        segs = phases.segments((T0 + 10) * 1000, _job(), m, recs)
        ms = lambda s: (T0 + s) * 1000
        self.assertEqual(segs, [["queued", ms(10), ms(60)], ["setup", ms(60), ms(100)],
                                ["build", ms(100), ms(700)], ["prepub", ms(750), ms(950)],
                                ["gateway", ms(850), ms(900)], ["gateway", ms(950), ms(980)]])

    def test_without_artifacts_the_jobs_own_times(self):
        ms = lambda s: (T0 + s) * 1000
        self.assertEqual(phases.segments(None, _job(), {}, []), [["build", ms(60), ms(1000)]])
        m = {"publish_start": ms(500), "publish_end": ms(900)}
        self.assertEqual(phases.segments(ms(0), _job(), m, []),
                         [["queued", ms(0), ms(60)], ["build", ms(60), ms(500)],
                          ["publish", ms(500), ms(900)]])
        self.assertEqual(phases.segments(ms(0), {"id": 1}, {}, []), [])

    def test_within_the_jobs_times(self):
        ms = lambda s: (T0 + s) * 1000
        m = {"build_start": ms(1200)}                      # the runner's clock ahead
        recs = [_rec(T0 - 100, 50, 10),                    # an earlier attempt's package
                {"ts": _iso(T0 + 990), "total_s": float("nan")}, _rec(T0 + 990, 100, -5)]
        self.assertEqual(phases.segments(ms(0), _job(), m, recs),
                         [["queued", ms(0), ms(60)], ["setup", ms(60), ms(1000)],
                          ["prepub", ms(890), ms(990)],
                          ["build", ms(1000), ms(1000)]])

    def test_where_a_failed_job_stopped(self):
        failed = dict(_job(), status="failed")
        m = lambda *marks: {k: T0 for k in marks}
        self.assertEqual(phases.failed_in(failed, {}), "")                  # marks not kept
        self.assertEqual(phases.failed_in(failed, m("build_start")), "build")
        self.assertEqual(phases.failed_in(failed, m("build_start", "build_end")), "build")
        self.assertEqual(phases.failed_in(failed, m("build_start", "build_end", "build_ok")), "certify")
        self.assertEqual(phases.failed_in(failed, m("build_end", "build_ok", "publish_start")), "publish")
        # the publish's own end is written before its finalize, which can fail
        self.assertEqual(phases.failed_in(failed, m("build_ok", "publish_start", "publish_end")), "publish")
        self.assertEqual(phases.failed_in(failed, m("build_ok", "publish_start", "publish_end",
                                                     "finish_start")), "finish")
        self.assertEqual(phases.failed_in(failed, m("build_ok", "finish_start")), "finish")  # no publish
        self.assertEqual(phases.failed_in(_job(), m("build_start")), "")      # it passed

    def test_the_latest_build_attempt(self):
        jobs = [_job(5), _job(9), _job(11, stage="test"), dict(_job(12), started_at=None)]
        self.assertEqual(phases.build_job(jobs)["id"], 9)
        self.assertIsNone(phases.build_job([]))


class StepTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)

    def _pipeline(self, pid, jobs, outcome="success", seen=None):
        with db.transaction(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO pipelines (id, is_build, outcome, jobs,"
                              " created_at, first_seen_ms) VALUES (?, 1, ?, ?, ?, ?)",
                              (pid, outcome, json.dumps(jobs), _iso(T0), seen or db.now_ms()))

    def test_recorded_once_again_when_retried_and_served(self):
        self._pipeline(100, [_job(7)])
        self._pipeline(101, [_job(8)], outcome="running")
        self._pipeline(102, [_job(9)], seen=1)                      # too old: artifacts expired
        reader = FakeReader({(7, phases.PHASES_FILE): b"build_start %d\n" % (T0 + 100),
                             (7, "measurements-100.json"): json.dumps(
                                 [_rec(T0 + 990, 100, 40)]).encode()})
        self.assertEqual(phases.step(self.conn, reader), 1)
        got = phases.for_pipelines(self.conn, [100, 101])
        self.assertEqual(list(got), [100])
        self.assertEqual(got[100]["host"], "pcphsft204")
        self.assertEqual([s[0] for s in got[100]["segments"]],
                         ["queued", "setup", "build", "prepub", "gateway"])
        self.assertEqual(phases.step(self.conn, reader), 0)        # done
        self._pipeline(100, [_job(7), _job(13, start=T0 + 2000, end=T0 + 3000)])
        self.assertEqual(phases.step(self.conn, FakeReader()), 1)  # retried: again, job 13
        self.assertEqual(phases.for_pipelines(self.conn, [100])[100]["segments"][-1][0], "build")

    def test_a_failed_publish_is_recorded_and_served(self):
        self._pipeline(100, [dict(_job(7), status="failed")], outcome="failed")
        reader = FakeReader({(7, phases.PHASES_FILE): b"build_start %d\nbuild_end %d\nbuild_ok %d\npublish_start %d\n"
                             % (T0 + 100, T0 + 500, T0 + 500, T0 + 600)})
        phases.step(self.conn, reader)
        self.assertEqual(phases.for_pipelines(self.conn, [100])[100]["failed_in"], "publish")
        self._pipeline(100, [dict(_job(7), status="failed"), _job(13, start=T0 + 2000, end=T0 + 3000)],
                       outcome="success")
        phases.step(self.conn, FakeReader({(13, phases.PHASES_FILE): b"build_start %d\n" % (T0 + 2100)}))
        self.assertEqual(phases.for_pipelines(self.conn, [100])[100]["failed_in"], "")   # retried: passed

    def test_the_queues_request_time_and_gitlab_failing(self):
        self._pipeline(100, [_job(7)])
        with db.transaction(self.conn):
            self.conn.execute("INSERT INTO build_queue (batch, community, requested_by, ref,"
                              " variables, digest, target, state, pipeline_id, created_ms,"
                              " updated_ms) VALUES ('b', 'lcg', 'a', 'main', '[]', 'd',"
                              " 'bits-build-x86_64', 'dispatched', 100, ?, 0)", ((T0 - 600) * 1000,))
        with self.assertRaises(forge_ops.ForgeError):
            phases.step(self.conn, FakeReader(fail=forge_ops.ForgeError(503, "down")))
        self.assertEqual(phases.for_pipelines(self.conn, [100]), {})
        phases.step(self.conn, FakeReader(fail=forge_ops.ForgeError(403, "no")))   # not transient
        self.assertEqual(phases.for_pipelines(self.conn, [100])[100]["segments"][0],
                         ["queued", (T0 - 600) * 1000, (T0 + 60) * 1000])


if __name__ == "__main__":
    unittest.main()
