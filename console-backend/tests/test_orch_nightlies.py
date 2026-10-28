# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The nights of the schedules: firing them into the build queue, and their runs."""

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest

from console_backend.orch import approvals, build_queue as bq, db, nightlies as nl, schedules as sch

UTC = dt.timezone.utc
PLATFORMS = """
platforms:
  - {name: x86_64-el9, image: registry/el9, install_dir: el9-x86_64, arch: el9-x86_64-gcc}
  - {name: aarch64-el9, image: registry/el9-arm}
"""
COMMUNITY = """
title: LCG
platforms:
  - x86_64-el9|registry/lcg-el9||el9-lcg
  - aarch64-el9
"""


class Reader:
    def __init__(self):
        self.files = {"config/platforms.yaml": PLATFORMS, "communities/LCG/ui-config.yaml": COMMUNITY,
                      "config/monitoring.yaml": "build:\n  max_jobs_per_container: 16\n"}
        self.read = []

    def file_raw(self, path, ref, max_bytes=None):
        self.read.append((path, ref))
        return self.files.get(path, "").encode() or None


def _ms(y, mo, d, h, mi):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=UTC).timestamp() * 1000)


def _vars(variables):
    return {v["key"]: v["value"] for v in variables}


class VariablesTest(unittest.TestCase):
    def setUp(self):
        self.s = sch.check({"name": "n", "package": "ROOT", "version": "v6-36", "platforms": ["x86_64-el9"],
                            "time": "02:00", "defaults": "release::gcc15", "set": {"release": "LCG_110b"},
                            "extra_repos": "https://github.com/bitsorg/lcg.bits.git@LCG_110b",
                            "extra_build_args": "--keep-going\t--fetch-repos", "rebuild": True, "jobs": 8,
                            "cvmfs": True}, "LCG")
        r = Reader()
        self.cfg = nl.config(r, "LCG", "main")

    def test_the_platform_from_the_community_then_the_defaults(self):
        self.assertEqual(nl.resolve_platform("x86_64-el9", self.cfg["platforms"], self.cfg["community"]),
                         ("registry/lcg-el9", "el9-x86_64", "el9-lcg", "x86_64"))
        self.assertEqual(nl.resolve_platform("aarch64-el9", self.cfg["platforms"], self.cfg["community"]),
                         ("registry/el9-arm", "aarch64-el9", "aarch64-el9", "aarch64"))
        with self.assertRaises(nl.Unbuildable):
            nl.resolve_platform("other", [], {})
        with self.assertRaises(nl.Unbuildable):
            nl.resolve_platform("x", [], {"platforms": ["x|img with space"]})
        self.assertEqual(nl.resolve_platform("osx", [{"name": "osx"}], {}), ("", "osx", "osx", "x86_64"))

    def test_a_nights_build_is_what_the_gitlab_fan_out_sends(self):
        v = _vars(nl.variables(self.s, "x86_64-el9", self.cfg, None))
        self.assertEqual(v, {
            "PACKAGE": "ROOT", "PLATFORM": "x86_64-el9", "RUNNER_ARCH": "x86_64",
            "BITS_RUNNER_TAG": "bits-build-x86_64", "COMMUNITY": "LCG", "CVMFS_INSTALL_DIR": "el9-x86_64",
            "BITS_BUILD_ARGS": "--force-rebuild ROOT --architecture el9-lcg --docker --docker-image"
                               " registry/lcg-el9 -j 8 --docker-extra-args=--cpus=16 --sandbox off"
                               " --defaults release::gcc15 --set release=LCG_110b --keep-going --fetch-repos",
            "VERSION": "v6-36", "BITS_EXTRA_REPOS": "https://github.com/bitsorg/lcg.bits.git@LCG_110b",
            "PUBLISH_CVMFS": "true", "NIGHTLY_SUFFIX": " [nightly]"})
        v = _vars(nl.variables(self.s, "x86_64-el9", self.cfg, "alice"))
        self.assertEqual((v["CERTIFY"], v["BITS_CERTIFIER"]), ("true", "alice"))


class _Nights(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.reader = Reader()

    def _schedule(self, last_fired, **more):
        p = {"name": "ROOT nightly", "package": "ROOT", "platforms": ["x86_64-el9", "aarch64-el9"],
             "time": "02:00"}
        p.update(more)
        s = sch.create(self.conn, sch.check(p, "LCG"), "alice")
        with db.transaction(self.conn):
            self.conn.execute("UPDATE schedules SET last_fired_ms = ? WHERE id = ?", (last_fired, s["id"]))
        return sch.get(self.conn, s["id"])

    def _done(self):
        with db.transaction(self.conn):
            self.conn.execute("UPDATE schedule_runs SET state = 'passed'")

    def _runs(self):
        return [(r["night"], r["platform"], r["attempt"], r["state"], r["reason"]) for r in self.conn.execute(
            "SELECT * FROM schedule_runs ORDER BY night, platform, attempt")]



class StepTest(_Nights):
    def test_a_schedule_fires_once_at_its_time(self):
        s = self._schedule(_ms(2026, 10, 27, 12, 0))
        self.assertEqual(nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 1, 59)), 0)
        self.assertEqual(nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 2, 1)), 1)
        self.assertEqual(nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 2, 5)), 0)
        self.assertEqual(self._runs(), [("2026-10-28", "aarch64-el9", 1, "queued", ""),
                                        ("2026-10-28", "x86_64-el9", 1, "queued", "")])
        q = [(r["requested_by"], r["batch"].rsplit(":", 1)[0], r["community"],
              _vars(json.loads(r["variables"]))["PLATFORM"])
             for r in self.conn.execute("SELECT * FROM build_queue ORDER BY id")]
        self.assertEqual(q, [("schedule:%d" % s["id"], "schedule:%d:2026-10-28" % s["id"], "lcg", p)
                             for p in ("aarch64-el9", "x86_64-el9")])
        self.assertIn(("communities/LCG/ui-config.yaml", "main"), self.reader.read)

    def test_a_missed_night_fires_within_the_grace_and_older_ones_are_not_run(self):
        self._schedule(_ms(2026, 10, 25, 12, 0), platforms=["x86_64-el9"])
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 7, 0))      # 5 h late
        self.assertEqual([r[0::3] for r in self._runs()], [("2026-10-26", "not_run"), ("2026-10-27", "not_run"),
                                                           ("2026-10-28", "queued")])
        self._schedule(_ms(2026, 10, 27, 12, 0), name="late", platforms=["x86_64-el9"])
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 9, 0))      # 7 h late
        self.assertEqual(self._runs()[-1][0::3], ("2026-10-28", "not_run"))

    def test_a_platform_still_running_skips_the_next_night(self):
        self._schedule(_ms(2026, 10, 27, 12, 0))
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 2, 1))
        with db.transaction(self.conn):
            self.conn.execute("UPDATE schedule_runs SET state = 'passed' WHERE platform = 'aarch64-el9'")
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 29, 2, 1))
        self.assertEqual(self._runs()[2:], [("2026-10-29", "aarch64-el9", 1, "queued", ""),
                                            ("2026-10-29", "x86_64-el9", 1, "skipped",
                                             "the previous night still runs")])

    def test_a_night_that_cannot_be_built_is_not_run(self):
        self._schedule(_ms(2026, 10, 27, 12, 0), platforms=["x86_64-el9", "sparc"])
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 2, 1))
        self.assertEqual([r[1::2] for r in self._runs()], [("sparc", "not_run"), ("x86_64-el9", "queued")])
        del self.reader.files["communities/LCG/ui-config.yaml"]
        self._done()
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 29, 2, 1))
        self.assertEqual({r[3:] for r in self._runs()[2:]},
                         {("not_run", "communities/LCG/ui-config.yaml is not in the console project")})

    def test_gitlab_unreadable_leaves_the_night_for_the_next_pass_and_others_fire(self):
        from console_backend import forge_ops
        a = self._schedule(_ms(2026, 10, 27, 12, 0), platforms=["x86_64-el9"])
        sch.create(self.conn, sch.check({"name": "other", "package": "X", "platforms": ["p"], "time": "02:00"},
                                        "ATLAS"), "carol")
        with db.transaction(self.conn):
            self.conn.execute("UPDATE schedules SET last_fired_ms = ?", (_ms(2026, 10, 27, 12, 0),))
        self.reader.files.update({"communities/ATLAS/ui-config.yaml": "platforms: [p]\n"})
        real = self.reader.file_raw

        def flaky(path, ref, max_bytes=None):
            if "LCG" in path:
                raise forge_ops.ForgeError(503, "down")
            return real(path, ref)
        self.reader.file_raw = flaky
        with self.assertRaises(RuntimeError):
            nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 2, 1))
        self.assertEqual([r[1] for r in self._runs()], ["p"])                    # ATLAS's fired
        self.assertEqual(sch.get(self.conn, a["id"])["last_fired_ms"], _ms(2026, 10, 27, 12, 0))
        self.reader.file_raw = real
        self.assertEqual(nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 3, 0)), 1)

    def test_paused_and_changed_schedules_do_not_fire(self):
        s = self._schedule(_ms(2026, 10, 27, 12, 0))
        sch.set_enabled(self.conn, s["id"], False, "alice")
        self.assertEqual(nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 2, 1)), 0)

    def test_a_certifying_night_certifies_under_its_standing_approval(self):
        s = self._schedule(_ms(2026, 10, 27, 12, 0), certify=True, platforms=["x86_64-el9"])
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 2, 1))
        self.assertEqual(self._runs()[0][3:], ("queued", "built, not certified: no standing approval"))
        entry = self.conn.execute("SELECT * FROM build_queue").fetchone()
        self.assertNotIn("CERTIFY", _vars(json.loads(entry["variables"])))
        aid = approvals.grant_standing(self.conn, nl.subject(s["id"]), s["content_sha256"], ["lcg"], "alice",
                                       2, 30, {"id": "x"}, b"\0" * 16)
        self._done()
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 29, 2, 1))
        entry = self.conn.execute("SELECT * FROM build_queue ORDER BY id DESC").fetchone()
        v = _vars(json.loads(entry["variables"]))
        self.assertEqual((v["CERTIFY"], v["BITS_CERTIFIER"], entry["certify"]), ("true", "alice", 1))
        with db.transaction(self.conn):                      # it starts: its pipeline is pre-approved
            self.conn.execute("UPDATE build_queue SET pipeline_id = 77, state = 'dispatched' WHERE id = ?",
                              (entry["id"],))
            pre = approvals.preapprove_queued(self.conn, bq.get(self.conn, entry["id"]))
        self.assertIsNotNone(pre)
        self.assertEqual(approvals.get(self.conn, aid)["runs_left"], 1)
        self.assertEqual(approvals.get(self.conn, pre)["subject"], "77")
        with db.transaction(self.conn):                      # another's approval: not this pipeline's
            self.conn.execute("UPDATE approvals SET approver = 'bob' WHERE id = ?", (aid,))
            other = dict(bq.get(self.conn, entry["id"]))
            other.update(id=entry["id"], pipeline_id=78)
            self.assertIsNone(approvals.preapprove_queued(self.conn, other))
            self.conn.execute("UPDATE approvals SET approver = 'alice' WHERE id = ?", (aid,))
        self._done()
        s2 = self._schedule(_ms(2026, 10, 28, 12, 0), name="two", certify=True)   # 2 platforms, 1 run left
        approvals.grant_standing(self.conn, nl.subject(s2["id"]), s2["content_sha256"], ["lcg"], "alice",
                                 1, 30, {"id": "x"}, b"\1" * 16)
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 29, 2, 2))
        self.assertEqual({r["reason"] for r in self.conn.execute(
            "SELECT reason FROM schedule_runs WHERE schedule_id = ?", (s2["id"],))},
            {"built, not certified: the standing approval has 1 runs left"})
        sch.update(self.conn, s["id"], sch.check({"name": "ROOT nightly", "package": "ROOT", "time": "02:00",
                                                  "platforms": ["x86_64-el9", "aarch64-el9"], "certify": True},
                                                 "LCG"), "alice", 1)
        self._done()
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 30, 2, 1))    # new content: none
        self.assertEqual({r["reason"] for r in self.conn.execute(
            "SELECT reason FROM schedule_runs WHERE schedule_id = ? AND night = '2026-10-30'", (s["id"],))},
            {"built, not certified: no standing approval"})


class ResultsTest(_Nights):
    def _pipeline(self, pid, outcome):
        with db.transaction(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO pipelines (id, is_build, outcome, variables, jobs,"
                              " first_seen_ms) VALUES (?, 1, ?, '{}', '[]', 0)", (pid, outcome))

    def _start(self, entry, pid):
        with db.transaction(self.conn):
            self.conn.execute("UPDATE build_queue SET state = 'dispatched', pipeline_id = ? WHERE id = ?",
                              (pid, entry))

    def _fired(self):
        s = self._schedule(_ms(2026, 10, 27, 12, 0))
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 28, 2, 1))
        return s, [r["queue_entry"] for r in self.conn.execute(
            "SELECT queue_entry FROM schedule_runs ORDER BY platform")]      # aarch64, x86_64

    def test_a_run_follows_its_pipeline(self):
        s, (arm, x86) = self._fired()
        self._start(arm, 500)
        nl.results(self.conn, now=_ms(2026, 10, 28, 3, 0))
        self.assertEqual([r[3] for r in self._runs()], ["queued", "queued"])       # not recorded yet
        self._pipeline(500, "running")
        nl.results(self.conn, now=_ms(2026, 10, 28, 3, 0))
        self.assertEqual([r[3] for r in self._runs()], ["running", "queued"])
        self._pipeline(500, "failed")
        with db.transaction(self.conn):
            self.conn.execute("INSERT INTO issues (fingerprint, package, title, excerpt, first_seen, last_seen,"
                              " created_ms) VALUES ('f', 'tiff', 't', 'e', '', '', 0)")
            self.conn.execute("INSERT INTO failures (job_id, pipeline_id, package, job_name, excerpt, source,"
                              " fingerprint, issue_id, recorded_ms) VALUES (1, 500, 'tiff', 'build', 'e', 'log',"
                              " 'f', 1, 0)")
        nl.results(self.conn, now=_ms(2026, 10, 28, 4, 0))
        r = self.conn.execute("SELECT * FROM schedule_runs WHERE queue_entry = ?", (arm,)).fetchone()
        self.assertEqual((r["state"], r["failed_at"], r["pipeline_id"]), ("failed", "tiff (build)", 500))
        self._pipeline(500, "success")                                            # retried in GitLab
        with db.transaction(self.conn):
            self.conn.execute("INSERT INTO builds (pipeline_id, build_id, community, recorded_ms)"
                              " VALUES (500, 'b-1', 'lcg', 0)")
        nl.results(self.conn, now=_ms(2026, 10, 28, 6, 0))
        r = self.conn.execute("SELECT * FROM schedule_runs WHERE queue_entry = ?", (arm,)).fetchone()
        self.assertEqual((r["state"], r["build_id"], r["failed_at"]), ("passed", "b-1", ""))
        self.assertEqual(bq.get(self.conn, x86)["state"], "queued")
        nl.results(self.conn, now=_ms(2026, 10, 28, 15, 0))                       # 13 h without a host
        self.assertEqual(bq.get(self.conn, x86)["state"], "canceled")
        self.assertEqual(self._runs()[1][3:], ("not_run", "no free build host for 12 hours"))
        ev = self.conn.execute("SELECT kind, payload FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertEqual((ev["kind"], json.loads(ev["payload"])["entry"]), ("queue.canceled", x86))

    def test_a_canceled_build_is_not_run_and_frees_its_platform(self):
        s, (arm, x86) = self._fired()
        bq.cancel(self.conn, arm, "alice")
        nl.results(self.conn, now=_ms(2026, 10, 28, 3, 0))
        self.assertEqual(self._runs()[0][3], "not_run")
        nl.step(self.conn, self.reader, "main", UTC, now=_ms(2026, 10, 29, 2, 1))
        self.assertEqual([r[1::2] for r in self._runs()[2:]], [("aarch64-el9", "queued"), ("x86_64-el9", "skipped")])

    def test_run_now_and_the_matrix(self):
        s, (arm, x86) = self._fired()
        self._start(arm, 500)
        self._pipeline(500, "failed")
        nl.results(self.conn, now=_ms(2026, 10, 28, 3, 0))
        with self.assertRaises(sch.Invalid):
            with db.transaction(self.conn):
                nl.run_now(self.conn, s["id"], ["sparc"], nl.config(self.reader, "LCG", "main"), "main", UTC,
                           "alice", now=_ms(2026, 10, 28, 9, 0))
        with db.transaction(self.conn):                                           # Run again: the failed one
            got = nl.run_now(self.conn, s["id"], ["aarch64-el9"], nl.config(self.reader, "LCG", "main"), "main",
                             UTC, "alice", now=_ms(2026, 10, 28, 9, 0))
        self.assertEqual(got, {"aarch64-el9": "queued"})
        with db.transaction(self.conn):                                           # still waiting: nothing
            got = nl.run_now(self.conn, s["id"], [], nl.config(self.reader, "LCG", "main"), "main",
                             UTC, "alice", now=_ms(2026, 10, 28, 9, 5))
        self.assertEqual(got, {"aarch64-el9": "skipped", "x86_64-el9": "skipped"})
        with self.assertRaises(sch.Invalid):
            with db.transaction(self.conn):
                nl.run_now(self.conn, s["id"], ["aarch64-el9"] * 2, {}, "main", UTC, "alice")
        m = nl.matrix(self.conn, {"lcg"}, 3, UTC, now=_ms(2026, 10, 28, 9, 0))
        self.assertEqual(m["nights"], ["2026-10-26", "2026-10-27", "2026-10-28"])
        (row,) = m["rows"]
        self.assertEqual({p: (c["2026-10-28"]["state"], c["2026-10-28"]["attempt"]) for p, c in row["cells"].items()},
                         {"aarch64-el9": ("queued", 2), "x86_64-el9": ("queued", 1)})
        self.assertEqual(row["failed"]["2026-10-28"], 0)                         # its latest attempt
        self.assertEqual(nl.matrix(self.conn, set(), 3, UTC)["rows"], [])


if __name__ == "__main__":
    unittest.main()
