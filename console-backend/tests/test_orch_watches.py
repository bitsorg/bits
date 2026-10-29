# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Commit CI: the branches a schedule follows, and its commit runs (orch/watches.py)."""

import datetime as dt
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from console_backend.orch import db, events, nightlies as nl, schedules as sch, watches as w
from test_orch_nightlies import Reader, _vars

UTC = dt.timezone.utc
REPO = "https://github.com/bitsorg/lcg.bits.git"
ROOT = "https://github.com/root-project/root.git"
T0 = int(dt.datetime(2026, 10, 28, 10, 0, tzinfo=UTC).timestamp() * 1000)
MIN = 60 * 1000


def _pkt(text):
    b = text.encode()
    return b"%04x" % (len(b) + 4) + b


class Resp:
    def __init__(self, status, content=b"", chunks=None):
        self.status_code, self.chunks, self.closed = status, chunks or [content], False

    def iter_content(self, n):
        return iter(self.chunks)

    def close(self):
        self.closed = True


def _addr(ip):
    return lambda host, port, **kw: [(2, 1, 6, "", (ip, port))]


ANSWER = (_pkt("%s refs/heads/main-old\n" % ("1" * 40))
          + _pkt("%s refs/heads/main symref-target:x\n" % ("2" * 40)) + b"0000")


@patch("console_backend.orch.watches.socket.getaddrinfo", _addr("140.82.121.3"))
class HeadTest(unittest.TestCase):
    @patch("console_backend.orch.watches.requests.post")
    def test_the_branchs_own_line_of_a_protocol_v2_answer(self, post):
        post.return_value = Resp(200, ANSWER)
        self.assertEqual(w.head(REPO, "main"), "2" * 40)
        args = post.call_args
        self.assertEqual(args.args[0], REPO + "/git-upload-pack")
        self.assertIn(b"ref-prefix refs/heads/main\n", args.kwargs["data"])
        self.assertEqual(args.kwargs["headers"]["Git-Protocol"], "version=2")
        self.assertEqual((args.kwargs["allow_redirects"], args.kwargs["stream"]), (False, True))
        self.assertIsNotNone(args.kwargs["auth"])                   # not ~/.netrc's
        self.assertTrue(post.return_value.closed)
        post.return_value = Resp(200, b"0000")
        self.assertEqual(w.head(REPO, "nope"), "")
        w.head("https://gitlab.cern.ch/atlas/athena", "main")       # GitLab wants the .git
        self.assertEqual(post.call_args.args[0], "https://gitlab.cern.ch/atlas/athena.git/git-upload-pack")

    @patch("console_backend.orch.watches.requests.post")
    def test_anything_but_a_whole_answer_is_an_error_not_no_branch(self, post):
        for bad in (Resp(401, b"Repository not found."), Resp(302), Resp(200, b"zzzz"), Resp(200, b""),
                    Resp(200, ANSWER[:-4]), Resp(200, ANSWER[:30]), Resp(200, _pkt("ERR upload-pack: not our ref") + b"0000"),
                    Resp(200, b"001e# service=git-upload-pack\n0000"),
                    Resp(200, _pkt("%s refs/heads/main\n" % ("g" * 40)) + b"0000"),
                    Resp(200, chunks=[b"0" * 8192] * 9)):
            post.return_value = bad
            with self.assertRaises(w.WatchError, msg=bad.chunks[0][:40]):
                w.head(REPO, "main")
        with patch("console_backend.orch.watches.time.monotonic", side_effect=[0, 0, 99]):
            post.return_value = Resp(200, chunks=[b"0", b"0"])
            with self.assertRaisesRegex(w.WatchError, "no whole answer"):
                w.head(REPO, "main")

    @patch("console_backend.orch.watches.requests.post")
    def test_only_a_public_https_host(self, post):
        post.return_value = Resp(200, ANSWER)
        for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "fd00::1", "192.168.0.1"):
            with patch("console_backend.orch.watches.socket.getaddrinfo", _addr(ip)):
                with self.assertRaisesRegex(w.WatchError, "not a public address"):
                    w.head(REPO, "main")
        for url in ("http://github.com/x/y.git", "https://github.com:99999/x/y.git"):
            with self.assertRaises(w.WatchError):
                w.head(url, "main")
        post.assert_not_called()


class _Watched(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.heads = {(REPO, "LCG_110"): "a" * 40, (ROOT, "master"): "b" * 40}
        self.reader = Reader()

    def get(self, url, branch):
        h = self.heads.get((url, branch))
        if isinstance(h, Exception):
            raise h
        return h

    def schedule(self, **more):
        p = {"name": "dev3", "package": "ROOT", "platforms": ["x86_64-el9"], "time": "02:00",
             "variants": ["gcc15::opt", "clang::opt"], "certify": True, "cvmfs": True,
             "watch": "%s@LCG_110;%s@master" % (REPO, ROOT)}
        p.update(more)
        return sch.create(self.conn, sch.check(p, "LCG"), "alice")

    def step(self, now):
        return w.step(self.conn, self.reader, "main", UTC, now=now, every_s=120, get_head=self.get)

    def runs(self):
        return [(r["variant"], r["fired_by"], r["state"]) for r in self.conn.execute(
            "SELECT * FROM schedule_runs ORDER BY id")]


class WatchTest(_Watched):
    def test_a_branch_first_seen_is_not_built(self):
        self.schedule()
        self.assertEqual(self.step(T0), 0)
        self.assertEqual(self.runs(), [])
        self.assertEqual({(h["branch"], h["sha"], h["built_sha"]) for h in w.heads(self.conn, 1)},
                         {("LCG_110", "a" * 40, "a" * 40), ("master", "b" * 40, "b" * 40)})

    def test_a_moved_head_builds_every_cell_once_it_held_still_and_certifies_nothing(self):
        self.schedule()
        self.step(T0)
        self.heads[(ROOT, "master")] = "c" * 40
        self.assertEqual(self.step(T0 + 1 * MIN), 0)                 # not read again so soon
        self.assertEqual(self.step(T0 + 3 * MIN), 0)                 # read: moved, not settled
        self.heads[(ROOT, "master")] = "d" * 40                      # another push
        self.assertEqual(self.step(T0 + 6 * MIN), 0)
        self.assertEqual(self.step(T0 + 10 * MIN), 0)                # 4 minutes still: not yet
        self.assertEqual(self.step(T0 + 12 * MIN), 1)                # 6: the newest heads
        self.assertEqual(self.runs(), [("clang::opt", "commit", "queued"), ("gcc15::opt", "commit", "queued")])
        r = self.conn.execute("SELECT * FROM schedule_runs").fetchone()
        self.assertEqual(json.loads(r["commits"]), {"%s@LCG_110" % REPO: "a" * 40, "%s@master" % ROOT: "d" * 40})
        for q in self.conn.execute("SELECT * FROM build_queue"):
            v = _vars(json.loads(q["variables"]))
            self.assertNotIn("CERTIFY", v)
            self.assertNotIn("PUBLISH_CVMFS", v)
            self.assertEqual(q["certify"], 0)
        self.assertEqual(self.step(T0 + 20 * MIN), 0)                # built: nothing new
        kinds = [e["kind"] for e in events.since(self.conn, limit=1000)]
        self.assertEqual(kinds.count("schedule.head_moved"), 2)

    def test_a_run_still_going_holds_the_next_until_it_ends(self):
        self.schedule()
        self.step(T0)
        self.heads[(REPO, "LCG_110")] = "e" * 40
        self.step(T0 + 3 * MIN)
        self.step(T0 + 9 * MIN)
        self.assertEqual(len(self.runs()), 2)
        self.heads[(REPO, "LCG_110")] = "f" * 40
        self.step(T0 + 12 * MIN)
        self.assertEqual(self.step(T0 + 20 * MIN), 0)                # its first commit run goes on
        with db.transaction(self.conn):
            self.conn.execute("UPDATE schedule_runs SET state = 'passed'")
        self.assertEqual(self.step(T0 + 21 * MIN), 1)
        self.assertEqual(json.loads(self.conn.execute("SELECT commits FROM schedule_runs ORDER BY id DESC")
                                    .fetchone()[0])["%s@LCG_110" % REPO], "f" * 40)

    def test_an_unreadable_repository_is_retried_and_the_others_still_fire(self):
        self.schedule()
        self.schedule(name="other", watch="%s@LCG_110" % REPO, variants=[])
        self.step(T0)
        self.heads[(ROOT, "master")] = w.WatchError("root: HTTP 503")
        self.heads[(REPO, "LCG_110")] = "9" * 40
        with self.assertRaises(w.WatchError):
            self.step(T0 + 3 * MIN)
        with self.assertRaises(w.WatchError):
            self.step(T0 + 9 * MIN)
        fired = {r["schedule_id"] for r in self.conn.execute("SELECT * FROM schedule_runs")}
        self.assertEqual(fired, {1, 2})                              # 1: its LCG_110 moved, ROOT as it was

    def test_a_failing_repository_fails_the_step_until_a_read_reads_it(self):
        self.schedule()
        self.step(T0)
        self.heads[(ROOT, "master")] = ValueError("Port out of range")   # not a WatchError either
        self.heads[(REPO, "LCG_110")] = "9" * 40
        for m in (3, 4):                                             # read at 3; 4 is between reads
            with self.assertRaisesRegex(w.WatchError, "Port out of range"):
                self.step(T0 + m * MIN)
        self.heads[(ROOT, "master")] = "b" * 40
        self.step(T0 + 5 * MIN)                                      # read again: no error
        self.step(T0 + 6 * MIN)
        self.assertEqual(self.step(T0 + 9 * MIN), 1)                 # LCG_110, read at 3, still fires

    def test_a_failed_repository_is_not_asked_again_in_the_pass(self):
        self.schedule(watch="%s@master;%s@v6-36" % (ROOT, ROOT))
        asked = []

        def get(url, branch):
            asked.append(branch)
            raise w.WatchError("root: HTTP 503")
        with self.assertRaises(w.WatchError):
            w.step(self.conn, self.reader, "main", UTC, now=T0, every_s=120, get_head=get)
        self.assertEqual(asked, ["master"])

    def test_pushed_back_to_what_was_built_waits_anew(self):
        self.schedule()
        self.step(T0)
        self.heads[(REPO, "LCG_110")] = "e" * 40
        self.step(T0 + 3 * MIN)
        self.heads[(REPO, "LCG_110")] = "a" * 40                     # force-pushed back
        self.step(T0 + 6 * MIN)
        self.assertEqual(self.step(T0 + 12 * MIN), 0)
        self.heads[(REPO, "LCG_110")] = "f" * 40
        for m in range(30, 57, 3):                                   # a push each read: waits 30 from 30
            self.heads[(REPO, "LCG_110")] = "%02d" % m * 20
            self.assertEqual(self.step(T0 + m * MIN), 0, m)
        self.heads[(REPO, "LCG_110")] = "57" * 20
        self.assertEqual(self.step(T0 + 60 * MIN), 1)

    def test_a_paused_schedule_or_a_dropped_branch_forgets_its_heads(self):
        s = self.schedule()
        self.step(T0)
        p = dict(name="dev3", package="ROOT", platforms=["x86_64-el9"], time="02:00", watch="%s@LCG_110" % REPO)
        sch.update(self.conn, s["id"], sch.check(p, "LCG"), "alice", s["revision"])
        self.step(T0 + 3 * MIN)
        self.assertEqual([h["branch"] for h in w.heads(self.conn, s["id"])], ["LCG_110"])
        sch.set_enabled(self.conn, s["id"], False, "alice")
        self.step(T0 + 6 * MIN)
        self.assertEqual(w.heads(self.conn, s["id"]), [])

    def test_commit_rows_beside_the_nightly_cells(self):
        self.schedule(variants=[])
        self.step(T0)
        self.heads[(REPO, "LCG_110")] = "7" * 40
        self.step(T0 + 3 * MIN)
        self.step(T0 + 9 * MIN)
        m = nl.matrix(self.conn, {"lcg"}, 2, UTC, now=T0 + 10 * MIN)
        (row,) = m["rows"]
        self.assertEqual(row["cells"], {})
        (c,) = row["commits"]
        self.assertEqual((c["commits"]["%s@LCG_110" % REPO], list(c["cells"]), c["cells"]["x86_64-el9"]["state"]),
                         ("7" * 40, ["x86_64-el9"], "queued"))

    def test_a_branch_pushed_to_all_the_time_still_builds(self):
        self.schedule()
        self.step(T0)
        for i in range(1, 16):                                       # a push every 3 minutes
            self.heads[(REPO, "LCG_110")] = "%02d" % i * 20
            fired = self.step(T0 + i * 3 * MIN)
            if fired:
                break
        self.assertEqual(i * 3, 33)                                  # the first read MAX_WAIT_MS after the first

    def test_a_branch_gone_or_new_builds_nothing_nor_holds_the_others(self):
        self.schedule()
        self.heads[(ROOT, "master")] = ""                            # not there yet
        self.step(T0)
        self.heads[(ROOT, "master")] = "c" * 40                      # created: seen, not built
        self.step(T0 + 3 * MIN)
        self.assertEqual(self.step(T0 + 9 * MIN), 0)
        self.heads[(ROOT, "master")] = ""                            # deleted
        self.heads[(REPO, "LCG_110")] = "e" * 40
        self.step(T0 + 12 * MIN)
        self.assertEqual(self.step(T0 + 18 * MIN), 1)
        r = self.conn.execute("SELECT commits FROM schedule_runs").fetchone()
        self.assertEqual(json.loads(r[0]), {"%s@LCG_110" % REPO: "e" * 40})

    def test_a_nightly_is_not_skipped_for_a_commit_run_nor_the_other_way(self):
        s = self.schedule(variants=[])
        self.step(T0)
        self.heads[(REPO, "LCG_110")] = "e" * 40
        self.step(T0 + 3 * MIN)
        self.step(T0 + 9 * MIN)                                      # a commit run, queued
        cfg = nl.config(self.reader, "LCG", "main")
        with db.transaction(self.conn):
            self.assertEqual(nl.run_now(self.conn, s["id"], [], cfg, "main", UTC, "alice", now=T0 + 10 * MIN),
                             {"x86_64-el9": "queued"})
        self.heads[(REPO, "LCG_110")] = "f" * 40
        self.step(T0 + 12 * MIN)
        self.assertEqual(self.step(T0 + 18 * MIN), 0)                # its own commit run holds it
        with db.transaction(self.conn):
            self.conn.execute("UPDATE schedule_runs SET state = 'passed' WHERE fired_by = 'commit'")
        self.assertEqual(self.step(T0 + 19 * MIN), 1)                # the manual run does not

    def test_an_unreadable_configuration_is_said_after_the_others_fired(self):
        self.schedule()
        self.step(T0)
        self.heads[(REPO, "LCG_110")] = "e" * 40
        self.step(T0 + 3 * MIN)
        with patch.object(nl, "config", side_effect=RuntimeError("no build.yaml")):
            with self.assertRaisesRegex(w.WatchError, "no build.yaml"):
                self.step(T0 + 9 * MIN)
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.step(T0 + 10 * MIN), 1)

    def test_a_deleted_schedule_leaves_no_heads(self):
        s = self.schedule()
        self.step(T0)
        sch.delete(self.conn, s["id"], "alice")
        self.step(T0 + 3 * MIN)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM schedule_watches").fetchone()[0], 0)

    def test_each_fire_its_own_row_even_with_the_same_heads(self):
        s = self.schedule(variants=[])
        self.step(T0)
        for k, sha in enumerate(("e", "f", "e"), 1):                 # e, then f, then back to e
            self.heads[(REPO, "LCG_110")] = sha * 40
            self.step(T0 + k * 20 * MIN)
            self.assertEqual(self.step(T0 + k * 20 * MIN + 6 * MIN), 1)
            with db.transaction(self.conn):
                self.conn.execute("UPDATE schedule_runs SET state = 'passed'")
        rows = nl._commit_rows(self.conn, s["id"], T0 + 90 * MIN)
        self.assertEqual([r["commits"]["%s@LCG_110" % REPO][0] for r in rows], ["e", "f", "e"])
        self.assertEqual(nl._commit_rows(self.conn, s["id"], T0 + 31 * 86400 * 1000), [])
        for k in range(4, 14):
            self.heads[(REPO, "LCG_110")] = "%02d" % k * 20
            self.step(T0 + k * 20 * MIN)
            self.assertEqual(self.step(T0 + k * 20 * MIN + 6 * MIN), 1)
            with db.transaction(self.conn):
                self.conn.execute("UPDATE schedule_runs SET state = 'passed'")
        rows = nl._commit_rows(self.conn, s["id"], T0 + 300 * MIN)
        self.assertEqual([r["commits"]["%s@LCG_110" % REPO][:2] for r in rows], ["%02d" % k for k in range(13, 3, -1)])


class WatchListTest(unittest.TestCase):
    def test_what_a_schedule_may_follow_and_its_hash(self):
        p = {"name": "n", "package": "ROOT", "platforms": ["x86_64-el9"], "time": "02:00"}
        plain = sch.check(p, "lcg")
        self.assertEqual(plain["watch"], "")
        s = sch.check(dict(p, watch="%s@master;%s@LCG_110" % (ROOT, REPO)), "lcg")
        self.assertEqual(sch.watches(s), [(REPO, "LCG_110"), (ROOT, "master")])
        self.assertEqual(sch.content_sha256(s), sch.content_sha256(plain))   # when, not what: no new approval
        for bad in (REPO, "http://x/y@main", "https://h.org:99999/x@main", "%s@a;%s@a" % (REPO, REPO), ";".join("%s@b%d" % (REPO, i) for i in range(9))):
            with self.assertRaises(sch.Invalid, msg=bad):
                sch.check(dict(p, watch=bad), "lcg")


if __name__ == "__main__":
    unittest.main()
