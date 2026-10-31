# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Nightly schedules: what they are, when they run, and their API."""

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from console_backend import config, main
from console_backend.orch import db, schedules as sch

ZURICH = ZoneInfo("Europe/Zurich")


def _p(**more):
    p = {"name": "ROOT nightly", "package": "ROOT", "platforms": ["x86_64-el9", "aarch64-el9"],
         "time": "02:00", "defaults": "release::gcc15", "certify": True}
    p.update(more)
    return p


def _ms(y, mo, d, h, mi, tz=dt.timezone.utc):
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=tz).timestamp() * 1000)


class CheckTest(unittest.TestCase):
    def test_a_schedule_and_its_defaults(self):
        s = sch.check(_p(), "LCG")
        self.assertEqual((s["community"], s["days"], s["certify_groups"], s["enabled"], s["jobs"]),
                         ("lcg", sch.EVERY_DAY, ["lcg"], True, 0))

    def test_the_publish_route_and_its_defaults(self):
        s = sch.check(_p(), "lcg")
        self.assertEqual((s["publish_path"], s["direct_s3"]), ("", ""))
        s = sch.check(_p(publish_path="ingest", direct_s3=False), "lcg")
        self.assertEqual((s["publish_path"], s["direct_s3"]), ("ingest", "false"))
        self.assertEqual(sch.check(_p(direct_s3="true"), "lcg")["direct_s3"], "true")
        self.assertEqual(sch.content_sha256(s), sch.content_sha256(sch.check(_p(), "lcg")))   # not in the hash
        for bad in ({"publish_path": "prepub"}, {"publish_path": 1}, {"direct_s3": "yes"},
                    {"publish_path": False}, {"direct_s3": 0}, {"direct_s3": []},
                    {"publish_path": "staged", "direct_s3": True}):
            with self.assertRaises(sch.Invalid, msg=bad):
                sch.check(_p(**bad), "lcg")

    def test_what_is_refused(self):
        for bad in ({"package": "ROOT; rm"}, {"platforms": []}, {"platforms": ["a", "a"]},
                    {"time": "2:00"}, {"time": "24:00"}, {"days": 0}, {"days": 128},
                    {"extra_repos": "http://x/y@main"}, {"set": {"a b": "c"}}, {"jobs": -1},
                    {"certify": "yes"}, {"extra_build_args": "--x '$(id)'"}, {"name": ""},
                    {"platforms": ["x86_64-el9\n"]}, {"set": {"k\n": "v"}}, {"set": {"k": "v\n"}},
                    {"extra_build_args": "--a\x1cb"}, {"extra_build_args": "--a\u2028b"},
                    {"extra_repos": "https://h/p@-x"}, {"extra_build_args": "--keep-going --defaults x"},
                    {"extra_build_args": "--defaults=x"}):
            with self.assertRaises(sch.Invalid, msg=bad):
                sch.check(_p(**bad), "lcg")

    def test_variants_are_defaults_chains_and_count_in_the_hash_only_when_there(self):
        s = sch.check(_p(variants=["gcc15::opt", "clang::opt"]), "lcg")
        self.assertEqual((s["variants"], sch.cells(s)[:2]),
                         (["clang::opt", "gcc15::opt"], [("aarch64-el9", "clang::opt"), ("aarch64-el9", "gcc15::opt")]))
        plain = sch.check(_p(), "lcg")
        self.assertEqual(plain["variants"], [])
        self.assertEqual(sch.cells(plain), [("aarch64-el9", ""), ("x86_64-el9", "")])
        h = sch.content_sha256(plain)
        import hashlib                                          # the hash schedules had before variants
        before = hashlib.sha256(json.dumps({k: plain[k] for k in sch._CONTENT}, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(h, before)
        self.assertNotEqual(h, sch.content_sha256(s))
        self.assertEqual(sch.check(_p(variants=["gcc15::", "::clang"]), "lcg")["variants"], ["clang", "gcc15"])
        for bad in (["a b"], [""], ["x", "x"], "gcc15", ["v%d" % i for i in range(17)], ["::"], ["a", "a::"],
                    ["v" * 150]):
            with self.assertRaises(sch.Invalid, msg=bad):
                sch.check(_p(variants=bad), "lcg")
        with self.assertRaises(sch.Invalid):                       # 32 platforms x 3 variants: too many builds
            sch.check(_p(platforms=["p%d" % i for i in range(32)], variants=["a", "b", "c"]), "lcg")
        self.assertEqual((sch.cell_key("x86_64-el9", ""), sch.cell_key("x86_64-el9", "gcc15::opt")),
                         ("x86_64-el9", "x86_64-el9 gcc15::opt"))

    def test_migration_22_keeps_the_runs_and_lets_variants_share_a_night(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        conn = db.connect(os.path.join(d, "old.db"))
        self.addCleanup(conn.close)
        for n in range(21):                                        # a database of migration 21
            with db.transaction(conn):
                for stmt in db.MIGRATIONS[n]:
                    conn.execute(stmt)
                conn.execute("PRAGMA user_version=%d" % (n + 1))
        with db.transaction(conn):
            conn.execute("INSERT INTO schedules (community, community_dir, name, package, version, defaults,"
                         " extra_repos, set_vars, platforms, rebuild, debug, jobs, extra_build_args, days, time,"
                         " certify, cvmfs, certify_groups, enabled, content_sha256, revision, last_fired_ms,"
                         " created_by, created_ms, updated_by, updated_ms) VALUES ('lcg', 'LCG', 'n', 'ROOT', '',"
                         " '', '', '{}', '[\"x86_64-el9\"]', 0, 0, 0, '', 127, '02:00', 0, 0, '[\"lcg\"]', 1,"
                         " 'h', 1, 0, 'a', 0, 'a', 0)")
            conn.execute("INSERT INTO schedule_runs (schedule_id, night, platform, attempt, state, content_sha256,"
                         " created_ms, updated_ms) VALUES (1, '2026-10-28', 'x86_64-el9', 1, 'passed', 'h', 0, 0)")
        db.migrate(conn)
        r = conn.execute("SELECT * FROM schedule_runs").fetchone()
        self.assertEqual((r["platform"], r["variant"], r["state"]), ("x86_64-el9", "", "passed"))
        self.assertEqual(sch.get(conn, 1)["variants"], [])
        with db.transaction(conn):
            conn.execute("INSERT INTO schedule_runs (schedule_id, night, platform, variant, attempt, state,"
                         " content_sha256, created_ms, updated_ms) VALUES (1, '2026-10-28', 'x86_64-el9', 'gcc15', 1,"
                         " 'queued', 'h', 0, 0)")
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'"
                                            " AND tbl_name = 'schedule_runs'")}
        self.assertTrue({"schedule_runs_night", "schedule_runs_entry", "schedule_runs_state"} <= names)

    def test_the_content_hash_leaves_out_when_and_whether(self):
        h = sch.content_sha256(sch.check(_p(), "lcg"))
        self.assertEqual(h, sch.content_sha256(sch.check(_p(time="03:30", days=1, enabled=False), "lcg")))
        self.assertNotEqual(h, sch.content_sha256(sch.check(_p(platforms=["x86_64-el9"]), "lcg")))
        self.assertNotEqual(h, sch.content_sha256(sch.check(_p(defaults="release"), "lcg")))
        self.assertEqual(h, sch.content_sha256(sch.check(_p(platforms=["aarch64-el9", "x86_64-el9"]), "lcg")))
        r = sch.content_sha256(sch.check(_p(extra_repos="https://h/p@main"), "lcg"))
        self.assertEqual(r, sch.content_sha256(sch.check(_p(extra_repos="https://h/p@main;"), "lcg")))


class WhenTest(unittest.TestCase):
    def test_the_weekdays(self):
        s = sch.check(_p(days=0b0000001), "lcg")                   # Mondays only
        got = sch.occurrences(s, _ms(2026, 10, 26, 0, 0), _ms(2026, 11, 9, 23, 0), dt.timezone.utc)
        self.assertEqual([n for n, _ in got], ["2026-10-26", "2026-11-02", "2026-11-09"])

    def test_a_time_skipped_in_spring_runs_at_the_first_minute_after(self):
        s = sch.check(_p(time="02:30"), "lcg")
        (night, ms), = sch.occurrences(s, _ms(2026, 3, 28, 12, 0), _ms(2026, 3, 29, 12, 0), ZURICH)
        self.assertEqual((night, dt.datetime.fromtimestamp(ms / 1000, ZURICH).strftime("%H:%M")),
                         ("2026-03-29", "03:00"))

    def test_a_time_repeated_in_autumn_runs_once(self):
        s = sch.check(_p(time="02:30"), "lcg")
        got = sch.occurrences(s, _ms(2026, 10, 24, 12, 0), _ms(2026, 10, 25, 12, 0), ZURICH)
        self.assertEqual([(n, ms) for n, ms in got], [("2026-10-25", _ms(2026, 10, 25, 0, 30))])

    def test_the_next_run(self):
        s = sch.check(_p(time="02:00"), "lcg")
        self.assertEqual(sch.next_run_ms(s, _ms(2026, 10, 28, 1, 0), ZURICH), _ms(2026, 10, 29, 1, 0))


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, main, "settings", main.settings)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "orch.db")
        self.conn = db.connect(path)
        db.migrate(self.conn)
        self.addCleanup(self.conn.close)
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4", "BITS_ORCH_DB": path,
            "BITS_ADMINS_POLICY": "lcg @alice\natlas @bob\n* @carol"})
        for p in (patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None),
                  patch.object(sch, "time_zone", lambda: ZURICH)):
            p.start()
            self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def _do(self, method, user, path, body=None):
        return self.client.request(method, path, json=body, headers={"Authorization": "Bearer " + user})

    def test_admins_edit_a_communitys_schedules_and_anyone_reads_them(self):
        self.assertEqual(self._do("POST", "bob", "/ops/schedules", dict(_p(), community="lcg")).status_code, 403)
        self.assertEqual(self._do("POST", "alice", "/ops/schedules", dict(_p(time="9"), community="lcg"))
                         .status_code, 400)
        r = self._do("POST", "alice", "/ops/schedules", dict(_p(), community="LCG"))
        self.assertEqual(r.status_code, 200, r.text)
        s = r.json()
        self.assertEqual((s["community"], s["community_dir"], s["revision"], s["approval"]),
                         ("lcg", "LCG", 1, None))
        self.assertGreater(s["next_run_ms"], db.now_ms())
        self.assertEqual(self._do("POST", "alice", "/ops/schedules", dict(_p(), community="lcg"))
                         .status_code, 400)                                      # same name
        read = self._do("GET", "dave", "/ops/schedules?community=lcg").json()
        self.assertEqual((read["time_zone"], [x["id"] for x in read["schedules"]]), ("Europe/Zurich", [s["id"]]))
        self.assertNotIn("approval", read["schedules"][0])                        # admins only
        self.assertEqual(self._do("GET", "dave", "/ops/schedules").json()["schedules"], [])
        u = "/ops/schedules/%d" % s["id"]
        self.assertEqual(self._do("PUT", "bob", u, dict(_p(), revision=1)).status_code, 403)
        r = self._do("PUT", "alice", u, dict(_p(time="03:15"), revision=1))
        self.assertEqual(r.json()["community_dir"], "LCG")
        self.assertEqual((r.status_code, r.json()["revision"], r.json()["content_sha256"]),
                         (200, 2, s["content_sha256"]))                           # time: same content
        self.assertEqual(self._do("PUT", "alice", u, dict(_p(), revision=1)).status_code, 409)
        self.assertEqual(self._do("PUT", "alice", u, dict(_p(), revision="x")).status_code, 400)
        r = self._do("POST", "alice", u + "/pause")
        self.assertEqual((r.json()["enabled"], r.json()["next_run_ms"]), (False, None))
        r = self._do("PUT", "alice", u, dict(_p(time="01:00"), revision=3))   # no enabled: stays paused
        self.assertEqual((r.status_code, r.json()["enabled"]), (200, False))
        self.assertEqual(self._do("POST", "alice", "/ops/schedules",
                                  dict(_p(name="x", certify_groups=["atlas"]), community="lcg")).status_code, 403)
        self.assertTrue(self._do("POST", "alice", u + "/resume").json()["enabled"])
        self.assertEqual(self._do("POST", "alice", u + "/stop").status_code, 404)
        self.assertEqual(self._do("DELETE", "carol", u).json(), {"deleted": s["id"]})
        self.assertEqual(self._do("DELETE", "carol", u).status_code, 404)
        kinds = [(r["kind"], sorted(json.loads(r["payload"]))) for r in self.conn.execute(
            "SELECT kind, payload FROM events WHERE subject = ? ORDER BY seq", ("schedule:%d" % s["id"],))]
        self.assertEqual(kinds, [("schedule.created", ["after"]), ("schedule.updated", ["after", "before"]),
                                 ("schedule.paused", ["after", "before"]), ("schedule.updated", ["after", "before"]),
                                 ("schedule.resumed", ["after", "before"]), ("schedule.deleted", ["before"])])
        self.assertEqual(self.client.get("/ops/schedules").status_code, 401)

    def test_a_schedules_publish_route_is_kept(self):
        r = self._do("POST", "alice", "/ops/schedules",
                     dict(_p(publish_path="staged", direct_s3=False), community="lcg"))
        self.assertEqual((r.status_code, r.json()["publish_path"], r.json()["direct_s3"]), (200, "staged", "false"))
        read = self._do("GET", "dave", "/ops/schedules?community=lcg").json()["schedules"][0]
        self.assertEqual((read["publish_path"], read["direct_s3"]), ("staged", "false"))

    def test_a_change_may_come_from_the_console_and_the_app(self):
        main.settings.frontend_origin = "https://pages"            # another origin: a preflight
        r = self.client.options("/ops/schedules/1", headers={"Origin": "https://pages",
                                                            "Access-Control-Request-Method": "PUT"})
        self.assertIn("PUT", r.headers["Access-Control-Allow-Methods"])

    def test_the_matrix_for_anyone_and_run_now_for_admins(self):
        s = self._do("POST", "alice", "/ops/schedules", dict(_p(), community="LCG")).json()
        m = self._do("GET", "dave", "/ops/nightlies?community=lcg&nights=3").json()
        self.assertEqual((len(m["nights"]), [r["schedule"]["id"] for r in m["rows"]], m["time_zone"]),
                         (3, [s["id"]], "Europe/Zurich"))
        self.assertEqual(self._do("GET", "dave", "/ops/nightlies").json()["rows"], [])
        u = "/ops/schedules/%d/run" % s["id"]
        self.assertEqual(self._do("POST", "bob", u, {}).status_code, 403)
        self.assertEqual(self._do("POST", "alice", u, {}).status_code, 503)      # the queue is off
        self.assertEqual(self._do("POST", "alice", u, {"platforms": "x"}).status_code, 400)
        self.assertEqual(self._do("POST", "alice", u, {"cells": [1]}).status_code, 400)
        v = self._do("POST", "alice", "/ops/schedules", dict(_p(name="dev3", variants=["gcc15::opt"]),
                                                             community="LCG")).json()
        self.assertEqual(v["variants"], ["gcc15::opt"])
        row = [r for r in self._do("GET", "dave", "/ops/nightlies?community=lcg").json()["rows"]
               if r["schedule"]["id"] == v["id"]][0]
        self.assertEqual([c["key"] for c in row["schedule"]["cells"]],
                         ["aarch64-el9 gcc15::opt", "x86_64-el9 gcc15::opt"])


if __name__ == "__main__":
    unittest.main()
