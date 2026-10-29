# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The build queue: builds wait in the orchestrator and start one per build host."""

import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, forge_ops, main
from console_backend.orch import approvals, build_queue as bq, db, replay

import test_preapprove as tp

HOSTS = [{"name": "arm", "tag": "bits-host-arm", "arch": ""},
         {"name": "bits", "tag": "bits-host-bits", "arch": "x86_64"},
         {"name": "pc", "tag": "bits-host-pc", "arch": "x86_64"}]


def _build(tag="bits-build-x86_64", pkg="ROOT", **more):
    v = {"PACKAGE": pkg, "COMMUNITY": "lcg", "BITS_RUNNER_TAG": tag}
    v.update(more)
    return ("%s build" % pkg, [{"key": k, "value": x} for k, x in v.items()])


class FakeForge:
    def __init__(self):
        self.started, self.next_id, self.fail, self.deleted = [], 100, None, []
        self.runners = [{"id": i, "description": d, "status": "online", "paused": False, "tags": t,
                         "runner_type": "project_type", "is_shared": False}
                        for i, d, t in ((1, "bits.cern.ch", ["self-hosted", "bits-host-bits",
                                                              "bits-build-x86_64"]),
                                        (2, "pc-runner", ["bits-host-pc", "bits-build-x86_64"]),
                                        (3, "", ["bits-host-arm"]))]
        self.details = 0

    def list_runners(self, tag=""):
        if isinstance(self.runners, Exception):
            raise self.runners
        return [{k: v for k, v in r.items() if k != "tags"}     # GitLab's list has no tags
                for r in self.runners if not tag or tag in r["tags"]]

    def runner(self, rid):
        self.details += 1
        return {"id": rid, "tag_list": next(r["tags"] for r in self.runners if r["id"] == rid)}

    def delete_pipeline(self, pid):
        self.deleted.append(pid)

    def trigger_pipeline(self, ref, variables, name=None):
        if self.fail:
            e, self.fail = self.fail, None
            if isinstance(e, Exception):
                raise e
            return e
        self.next_id += 1
        self.started.append((self.next_id, {v["key"]: v["value"] for v in variables}))
        return {"id": self.next_id, "status": "created"}


class _Queue(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.hosts = [dict(h) for h in HOSTS]
        self.forge = FakeForge()
        bq._named.clear()
        bq._hosts_cache.clear()
        bq._missing.clear()

    def _queue(self, *builds):
        return bq.add(self.conn, "lcg", "alice", "main", list(builds))[1]

    def _pipeline(self, pid, outcome="running", tag="", jobs=(), token=None):
        v = {"PACKAGE": "X", "BITS_RUNNER_TAG": tag}
        if token is not None:
            v[bq.ENTRY_VAR] = token
        with db.transaction(self.conn):
            self.conn.execute("INSERT OR REPLACE INTO pipelines (id, is_build, outcome, variables,"
                              " jobs, first_seen_ms) VALUES (?, 1, ?, ?, ?, 0)",
                              (pid, outcome, json.dumps(v), json.dumps(list(jobs))))

    def _unpause(self):
        with db.transaction(self.conn):
            self.conn.execute("DELETE FROM kv WHERE key = 'queue.pause'")

    def _states(self):
        return [(r["id"], r["state"], r["host"], r["pipeline_id"])
                for r in self.conn.execute("SELECT * FROM build_queue ORDER BY id")]



class QueueTest(_Queue):
    def test_the_hosts_are_the_projects_runners_in_gitlab(self):
        from console_backend import forge_ops
        f = self.forge
        f.runners += [{"id": 4, "description": "", "status": "online", "paused": False,
                       "tags": ["bits-host-shared"], "runner_type": "instance_type", "is_shared": True},
                      {"id": 5, "description": "", "status": "online", "paused": False,
                       "tags": ["bits-host-", "docker"], "runner_type": "group_type", "is_shared": False}]
        self.assertEqual(bq.hosts(f), HOSTS)                  # no shared runner, no bare tag
        self.assertEqual(f.details, 4)
        self.assertEqual(bq.hosts(f), HOSTS)                  # read again only after HOSTS_TTL_S
        self.assertEqual(f.details, 4)
        bq._hosts_cache.clear()
        runner = f.runner
        f.runner = lambda rid: (_ for _ in ()).throw(forge_ops.ForgeError(403, "x")) if rid == 2 \
            else runner(rid)
        self.assertEqual([h["name"] for h in bq.hosts(f)], ["arm", "bits"])   # one it may not see
        bq._hosts_cache.clear()
        f.runner = lambda rid: (_ for _ in ()).throw(forge_ops.ForgeError(502, "x"))
        self.assertIsNone(bq.hosts(f))                        # GitLab down: cannot tell
        bq.dispatch(self.conn, FakeForge(), self.hosts)
        self.assertEqual(bq.last_hosts(self.conn), HOSTS)     # what the worker then takes
        bq.dispatch(self.conn, FakeForge(), [])               # none now: none shown or kept
        self.assertEqual(bq.last_hosts(self.conn), [])

    def test_a_host_whose_runners_disagree_takes_one_arch_and_says_so(self):
        self.forge.runners.append({"id": 6, "description": "", "status": "online", "paused": False,
                                   "tags": ["bits-host-pc", "bits-build-aarch64"],
                                   "runner_type": "project_type", "is_shared": False})
        bq._warned.clear()
        with self.assertLogs("bits_orchestrator.queue", "WARNING") as logs:
            pc = [h for h in bq.hosts(self.forge) if h["name"] == "pc"][0]
        self.assertEqual(pc["arch"], "x86_64")
        self.assertIn("build host pc: its runners have the architectures aarch64, x86_64; it takes x86_64",
                      "\n".join(logs.output))

    def test_one_build_per_host_oldest_first(self):
        a, b, c = self._queue(_build(pkg="A"), _build(pkg="B"), _build(pkg="C"))
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 2)
        self.assertEqual(self._states(), [(a, "dispatched", "bits", 101), (b, "dispatched", "pc", 102),
                                          (c, "queued", "", None)])
        started = dict(self.forge.started)
        self.assertEqual((started[101]["BITS_RUNNER_TAG"], started[101][bq.ENTRY_VAR]),
                         ("bits-host-bits", bq.get(self.conn, a)["token"]))
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)   # not recorded yet
        self._pipeline(101, "running")
        self._pipeline(102, "running")
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self._pipeline(102, "failed")                       # pc is free
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)
        self.assertEqual(self._states()[2], (c, "dispatched", "pc", 103))
        self.assertEqual([h["busy"] for h in bq.host_status(self.conn)], [False, True, False])

    def test_a_pinned_build_waits_for_its_host_only(self):
        a, b = self._queue(_build("bits-host-bits", pkg="A"), _build("bits-host-bits", pkg="B"))
        c, = self._queue(_build(pkg="C"))
        bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertEqual([s[1:3] for s in self._states()],
                         [("dispatched", "bits"), ("queued", ""), ("dispatched", "pc")])

    def test_a_pipeline_waiting_for_a_runner_keeps_a_host_free(self):
        self._pipeline(1, tag="bits-build-x86_64")          # it will take bits or pc
        self._queue(_build("bits-host-bits", pkg="A"), _build("bits-host-pc", pkg="B"))
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)
        self._queue(_build("bits-host-arm", pkg="C"))       # another arch (none): starts
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)

    def test_pipelines_from_outside_the_queue_hold_hosts(self):
        self._pipeline(1, tag="bits-host-bits")                          # pinned
        self._pipeline(2, tag="bits-build-x86_64",
                       jobs=[{"status": "running", "runner": "pc-runner"}])   # on pc's runner
        self._pipeline(3, outcome="success", tag="bits-host-bits")
        self._queue(_build())
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self._pipeline(2, outcome="success")
        self._pipeline(4, tag="bits-build-x86_64")                        # not on a runner yet
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self._pipeline(1, outcome="canceled")
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)

    def test_a_job_on_a_runner_described_otherwise_holds_its_host(self):
        from console_backend import forge_ops
        self._pipeline(1, tag="bits-build-x86_64",
                       jobs=[{"status": "running", "runner": "bits.cern.ch"}])   # bits' runner
        self._pipeline(2, jobs=[{"status": "running", "runner_id": 2}])   # pc's, by its id alone
        self._queue(_build())
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self.forge.runners = forge_ops.ForgeError(403, "forbidden")     # the last names stay
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self._pipeline(1, outcome="success")
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)
        self.assertEqual(self._states()[0][1:3], ("dispatched", "bits"))
        self.assertTrue([h["busy"] for h in bq.host_status(self.conn)][2])     # pc

    def test_a_host_whose_runner_is_offline_is_not_free(self):
        from console_backend import forge_ops
        self.forge.runners[1]["status"] = "offline"                     # pc's runner
        a, b = self._queue(_build(pkg="A"), _build(pkg="B"))
        c, = self._queue(_build("bits-host-pc", pkg="C"))
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)
        self.assertEqual([s[1:3] for s in self._states()],
                         [("dispatched", "bits"), ("queued", ""), ("queued", "")])
        self.assertEqual([(h["name"], h["offline"]) for h in bq.host_status(self.conn)],
                         [("arm", False), ("bits", False), ("pc", True)])   # bits.cern.ch: by its tag
        self.forge.runners[1].update(status="online", paused=True)       # paused: not free either
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self.assertEqual([(h["name"], h["offline"], h["paused"]) for h in bq.host_status(self.conn)],
                         [("arm", False, False), ("bits", False, False), ("pc", False, True)])
        self.forge.runners = forge_ops.ForgeError(403, "forbidden")     # cannot tell: as before
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)
        self.assertEqual(self._states()[1][1:3], ("dispatched", "pc"))

    def test_a_build_gitlab_dropped_unstarted_is_queued_again(self):
        a, b = self._queue(_build("bits-host-bits", pkg="A"), _build("bits-host-pc", pkg="B"))
        bq.dispatch(self.conn, self.forge, self.hosts)
        stuck = [{"name": "build", "status": "failed", "failure_reason": "stuck_or_timeout_failure"},
                 {"name": "publish", "status": "skipped"}]
        self._pipeline(101, "failed", jobs=stuck)
        self._pipeline(102, "failed", jobs=[{"status": "failed", "failure_reason": "script_failure",
                                             "runner": "pc-runner", "started_at": "t"}])
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)   # A again, on bits
        self.assertEqual(self._states(), [(a, "dispatched", "bits", 103), (b, "dispatched", "pc", 102)])
        self.assertTrue(bq.dropped(stuck) and not bq.dropped([dict(stuck[0], runner="bits")]))

    def test_builds_no_host_can_take_wait_then_are_refused(self):
        a, b, c = self._queue(_build("bits-host-gone"), _build("bits-build-aarch64"),
                              _build("bits-host-arm"))
        with db.transaction(self.conn):                     # long queued: no matter
            self.conn.execute("UPDATE build_queue SET created_ms = created_ms - ?", (3 * bq.HOST_GONE_MS,))
        bq.dispatch(self.conn, self.forge, self.hosts)       # a runner may be coming back
        self.assertEqual([s[1] for s in self._states()], ["queued", "queued", "dispatched"])
        self.assertIn("bits-host-gone is not a build host", bq.get(self.conn, a)["reason"])
        bq.dispatch(self.conn, self.forge, self.hosts)       # waiting for a host is not missing one
        for k in bq._missing:
            bq._missing[k] -= bq.HOST_GONE_MS + 1            # an hour since they went missing
        bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertEqual([s[1] for s in self._states()], ["refused", "refused", "dispatched"])
        with self.assertRaises(ValueError):
            bq.check_target([{"key": "BITS_RUNNER_TAG", "value": "self-hosted"}])
        with self.assertRaises(ValueError):
            bq.check_target([{"key": "BITS_RUNNER_TAG", "value": "bits-host-"}])
        self.assertEqual(bq.check_target([{"key": "BITS_RUNNER_TAG", "value": "bits-host-a"}]),
                         "bits-host-a")
        with self.assertRaises(RuntimeError):
            self._queue(_build())
            bq.dispatch(self.conn, self.forge, [])

    def test_gitlab_refusing(self):
        a, b = self._queue(_build(pkg="A"), _build(pkg="B"))
        # The job limit: back in its place, not counted, the pass ends.
        self.forge.fail = forge_ops.ForgeError(400, "Project exceeded the allowed number of jobs")
        with self.assertRaises(bq.Stop):
            bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertEqual([(s[1], s[2]) for s in self._states()], [("queued", ""), ("queued", "")])
        self.assertEqual(bq.get(self.conn, a)["attempts"], 0)
        self.assertIn("allowed number of jobs", bq.paused(self.conn)["why"])
        # Paused: nothing is tried for a while.
        with self.assertRaises(bq.Stop):
            bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertEqual(self.forge.started, [])
        self._unpause()
        # Created failed, with no jobs: the same, and the dead pipeline goes.
        self.forge.fail = {"id": 7, "status": "failed"}
        with self.assertRaises(bq.Stop):
            bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertEqual((bq.get(self.conn, a)["state"], self.forge.deleted), ("queued", [7]))
        self._unpause()
        # Refused for this build: counted, the others go on; refused in the end.
        for n in range(bq.MAX_ATTEMPTS):
            self.forge.fail = forge_ops.ForgeError(400, "variables are invalid")
            bq.dispatch(self.conn, self.forge, self.hosts)
            with db.transaction(self.conn):
                self.conn.execute("UPDATE build_queue SET state = 'canceled' WHERE id = ?"
                                  " AND state = 'dispatched'", (b,))
        r = bq.get(self.conn, a)
        self.assertEqual((r["state"], r["attempts"]), ("refused", bq.MAX_ATTEMPTS))
        self.assertIn("variables are invalid", r["reason"])

    def test_gitlabs_answer_lost(self):
        a, = self._queue(_build(pkg="A"))
        self.forge.fail = forge_ops.ForgeError(0, "timed out")
        with self.assertRaises(bq.Stop):
            bq.dispatch(self.conn, self.forge, self.hosts)
        r = bq.get(self.conn, a)
        self.assertEqual((r["state"], r["host"]), ("dispatching", "bits"))
        # It was created after all: recorded with this attempt's token, adopted.
        self._pipeline(7, token="an older attempt's")
        self._pipeline(8, token=r["token"])
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 0)
        self.assertEqual(self._states(), [(a, "dispatched", "bits", 8)])
        self.assertEqual(self.forge.started, [])

    def test_a_start_the_worker_did_not_finish(self):
        a, b = self._queue(_build(pkg="A"), _build(pkg="B"))
        with db.transaction(self.conn):
            self.conn.execute("UPDATE build_queue SET state = 'dispatching', host = 'bits',"
                              " updated_ms = 0, token = 't' || id")
        self._pipeline(55, token="t%d" % a)
        # A's pipeline is adopted (on bits); B never started: queued again, then on pc.
        self.assertEqual(bq.dispatch(self.conn, self.forge, self.hosts), 1)
        self.assertEqual([s[1:] for s in self._states()],
                         [("dispatched", "bits", 55), ("dispatched", "pc", 101)])
        self.assertEqual(bq.get(self.conn, b)["attempts"], 1)

    def test_cancel_and_listing(self):
        a, b = self._queue(_build(pkg="A"), _build(pkg="B"))
        self.assertTrue(bq.cancel(self.conn, b, "alice"))
        self.assertFalse(bq.cancel(self.conn, b, "alice"))
        bq.dispatch(self.conn, self.forge, self.hosts)
        self.assertFalse(bq.cancel(self.conn, a, "alice"))
        c, d = self._queue(_build("bits-host-bits", pkg="C"), _build("bits-host-bits", pkg="D"))
        rows = {r["id"]: r for r in bq.listing(self.conn)}
        self.assertEqual((rows[c]["ahead"], rows[d]["ahead"], rows[a]["state"], rows[a]["package"]),
                         (0, 1, "dispatched", "A"))
        self.assertEqual(bq.listing(self.conn, communities={"atlas"}), [])


class BatchApprovalTest(_Queue):
    """A passkey approves a batch's certifying builds; each, as it starts, gets
    the pre-approval of its pipeline the certifier reads."""

    def _grant(self, ids, groups=("lcg",)):
        rows = [bq.get(self.conn, i) for i in ids]
        digests = [r["digest"] for r in rows if r["certify"]]
        with db.transaction(self.conn):
            got = approvals.grant_queue(self.conn, rows[0]["batch"], "lcg", list(groups), digests,
                                        "alice", {"id": "cred"}, b"n" * 16)
        return got and got[0]

    def test_each_started_build_is_preapproved(self):
        ids = self._queue(_build(pkg="A", CERTIFY="true"), _build(pkg="B"),
                          _build(pkg="C", CERTIFY="true"))
        aid = self._grant(ids)
        self.assertIsNone(self._grant(ids))                 # one per batch
        self.assertTrue(approvals.verify_standing(approvals.get(self.conn, aid)))
        then = approvals.preapprove_queued
        bq.dispatch(self.conn, self.forge, self.hosts, then)       # A on bits, B on pc
        pre = replay.preapproval_at(self.conn, 101, db.now_ms())
        self.assertEqual((pre["approver"], json.loads(pre["groups"]), json.loads(pre["binding"])),
                         ("alice", ["lcg"], {"queue": bq.get(self.conn, ids[0])["batch"],
                                             "entry": ids[0], "approval": aid}))
        self.assertIsNone(replay.preapproval_at(self.conn, 102, db.now_ms()))   # B certifies not
        self.assertEqual(approvals.get(self.conn, aid)["runs_left"], 1)
        self._pipeline(101, "success")
        bq.dispatch(self.conn, self.forge, self.hosts, then)       # C on bits
        self.assertIsNotNone(replay.preapproval_at(self.conn, 103, db.now_ms()))
        self.assertEqual(approvals.get(self.conn, aid)["runs_left"], 0)

    def test_a_dropped_build_gives_its_run_back(self):
        a, = self._queue(_build("bits-host-bits", pkg="A", CERTIFY="true"))
        aid = self._grant([a])
        then = approvals.preapprove_queued
        bq.dispatch(self.conn, self.forge, self.hosts, then)
        self.assertEqual(approvals.get(self.conn, aid)["runs_left"], 0)
        self._pipeline(101, "failed", jobs=[{"status": "failed",
                                             "failure_reason": "stuck_or_timeout_failure"}])
        bq.dispatch(self.conn, self.forge, self.hosts, then)       # again, as 102
        self.assertIsNone(replay.preapproval_at(self.conn, 101, db.now_ms()))   # a retry certifies not
        self.assertIsNotNone(replay.preapproval_at(self.conn, 102, db.now_ms()))
        self.assertEqual(approvals.get(self.conn, aid)["runs_left"], 0)

    def test_a_revoked_batch_preapproves_nothing_more(self):
        ids = self._queue(_build(pkg="A", CERTIFY="true"), _build("bits-host-bits", pkg="B",
                                                                   CERTIFY="true"))
        aid = self._grant(ids)
        bq.dispatch(self.conn, self.forge, self.hosts, approvals.preapprove_queued)   # A
        self.assertIsNotNone(replay.preapproval_at(self.conn, 101, db.now_ms()))
        approvals.revoke(self.conn, aid, "alice")           # A's goes with it
        self.assertIsNone(replay.preapproval_at(self.conn, 101, db.now_ms()))
        self._pipeline(101, "success")
        bq.dispatch(self.conn, self.forge, self.hosts, approvals.preapprove_queued)   # B
        self.assertIsNone(replay.preapproval_at(self.conn, 102, db.now_ms()))
        self.assertIsNone(bq.get(self.conn, ids[1])["approval_id"])


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(setattr, main, "settings", main.settings)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "orch.db")
        c = db.connect(path)
        db.migrate(c)
        self.conn = c
        self.addCleanup(c.close)
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4", "BITS_ORCH_DB": path, "BITS_ORCH_QUEUE": "1",
            "BITS_ADMINS_POLICY": "lcg @alice\natlas @bob\n* @carol"})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)

    def _post(self, user, path, body):
        return self.client.post(path, json=body, headers={"Authorization": "Bearer " + user})

    def _get(self, user, path):
        return self.client.get(path, headers={"Authorization": "Bearer " + user})

    def _body(self, **more):
        name, variables = _build(**more)
        return {"community": "lcg", "builds": [{"name": name, "variables": variables}]}

    def test_queue_list_cancel(self):
        self.assertEqual(self._post("bob", "/ops/queue", self._body()).status_code, 403)
        r = self._post("alice", "/ops/queue", self._body(BITS_CERTIFIER="mallory"))
        self.assertEqual(r.status_code, 200, r.text)
        eid, = r.json()["entries"]
        v = {x["key"]: x["value"] for x in json.loads(bq.get(self.conn, eid)["variables"])}
        self.assertEqual(v["BITS_CERTIFIER"], "alice")
        listed = self._get("alice", "/ops/queue").json()
        self.assertEqual([(e["id"], e["ahead"]) for e in listed["entries"]], [(eid, 0)])
        self.assertIsNone(listed["paused"])
        self.assertEqual(self._get("bob", "/ops/queue").json()["entries"], [])
        with db.transaction(self.conn):                 # the hosts, for admins only
            self.conn.execute("INSERT INTO kv (key, value) VALUES ('queue.hosts', '[{\"name\": \"h1\"}]')")
        self.assertEqual(self._get("alice", "/ops/queue").json()["hosts"], [{"name": "h1"}])
        read = self._get("dave", "/ops/queue?community=lcg").json()  # any signed-in user
        self.assertEqual(([e["id"] for e in read["entries"]], read["hosts"]), ([eid], []))
        self.assertNotIn("requested_by", read["entries"][0])
        self.assertEqual(listed["entries"][0]["requested_by"], "alice")
        self.assertEqual(self._get("dave", "/ops/queue").json()["entries"], [])
        self.assertEqual(self._post("bob", "/ops/queue/%d/cancel" % eid, {}).status_code, 403)
        self.assertEqual(self._post("carol", "/ops/queue/%d/cancel" % eid, {}).status_code, 200)
        self.assertEqual(self._post("alice", "/ops/queue/%d/cancel" % eid, {}).status_code, 409)
        self.assertTrue(self.client.get("/healthz").json()["build_queue"])
        dup = self._body()
        dup["builds"][0]["variables"].append({"key": "PACKAGE", "value": "other"})
        self.assertEqual(self._post("alice", "/ops/queue", dup).status_code, 400)

    def test_ops_build_leaves_builds_to_the_queue(self):
        main.settings.forge_ops_token, main.settings.forge_project = "t", "1"
        name, variables = _build()
        r = self._post("alice", "/ops/build", {"community": "lcg", "variables": variables})
        self.assertEqual(r.status_code, 409, r.text)

    def test_a_batch_approved_with_a_passkey(self):
        from soft_webauthn import SoftWebauthnDevice
        from console_backend import credentials, webauthn_rp
        self.addCleanup(setattr, main, "credstore", main.credstore)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4", "BITS_ORCH_DB": main.settings.orch_db,
            "BITS_ORCH_QUEUE": "1", "BITS_ADMINS_POLICY": "lcg @alice\natlas @bob\n* @carol",
            "BITS_WEBAUTHN_RP_ID": tp.RP_ID, "BITS_WEBAUTHN_ORIGIN": tp.ORIGIN,
            "BITS_WEBAUTHN_REQUIRE_UV": "0",
            "BITS_WEBAUTHN_CREDENTIALS": os.path.join(d, "creds.json")})
        main.credstore = credentials.CredentialStore(main.settings.credentials_path)
        body = dict(self._body(CERTIFY="true"), certify_groups=["lcg"])
        r = self._post("alice", "/ops/queue", body).json()           # no passkey: queued all the same
        self.assertEqual((len(r["entries"]), r["approval"], r["approval_error"]),
                         (1, None, "no passkey enrolled; enrol first"))
        self.assertEqual(self._post("alice", "/ops/queue", dict(body, certify_groups=["atlas"])
                                    ).status_code, 403)
        dev = SoftWebauthnDevice()
        opts_json, chal = webauthn_rp.registration_options(main.settings, "alice", [])
        main.credstore.add("alice", webauthn_rp.verify_registration(
            main.settings, tp._attestation(dev, opts_json), chal))
        r = self._post("alice", "/ops/queue", body).json()
        self.assertEqual((r["approval"]["builds"], r["approval"]["groups"]), (1, ["lcg"]))
        eid, = r["entries"]
        # Started before the passkey answered: pre-approved on approval.
        with db.transaction(self.conn):
            self.conn.execute("UPDATE build_queue SET state = 'dispatched', pipeline_id = 77"
                              " WHERE id = ?", (eid,))
        asr = tp._assertion(dev, r["approval"]["publicKey"])
        ok = {"request_id": r["approval"]["request_id"], "assertion": asr}
        self.assertEqual(self._post("bob", "/ops/queue/approve", ok).status_code, 403)
        got = self._post("alice", "/ops/queue/approve", ok)
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["preapproved"], [77])
        self.assertEqual(self._post("alice", "/ops/queue/approve", ok).status_code, 400)
        self.assertEqual(replay.preapproval_at(self.conn, 77, db.now_ms())["approver"], "alice")
        # A batch's request is not a schedule's.
        r2 = self._post("alice", "/ops/queue", body).json()
        self.assertEqual(self._post("alice", "/approvals/standing/approve",
                                    {"request_id": r2["approval"]["request_id"],
                                     "assertion": asr}).status_code, 400)
        # Without certify_groups, or no build that certifies: no challenge.
        self.assertIsNone(self._post("alice", "/ops/queue", self._body()).json()["approval"])

    def test_requests_are_checked(self):
        for body in (self._body(tag="self-hosted"), self._body(pkg=""),
                     {"community": "lcg", "builds": []}, dict(self._body(), ref="evil"),
                     dict(self._body(), community="atlas")):
            self.assertEqual(self._post("carol", "/ops/queue", body).status_code, 400, body)
        main.settings.orch_queue = False
        self.assertEqual(self._post("alice", "/ops/queue", self._body()).status_code, 503)


if __name__ == "__main__":
    unittest.main()
