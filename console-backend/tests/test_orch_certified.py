# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator's M5: the certified set."""

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from console_backend import config, main
from console_backend.orch import certified, db, events, shadow

from test_orch_shadow import FakeManifests, FakeS3, _Base, _bom


def _write_tree(root, files):
    for path, doc in files.items():
        p = os.path.join(root, "manifests", path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(doc if isinstance(doc, bytes) else json.dumps(doc).encode())
    return os.path.join(root, "manifests")


class _DB(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.conn = db.connect(os.path.join(self.dir, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)

    def tree(self, name, files):
        return _write_tree(os.path.join(self.dir, name), files)

    def kinds(self):
        return [e["kind"] for e in events.since(self.conn, limit=1000)]

    def live(self):
        return {e["path"]: e for e in certified.listing(self.conn)["entries"]}


A = _bom("b1", "el9", ("p", "h1"))
B = _bom("b2", "el10", ("q", "h2"))
C = _bom("b3", "el9", ("r", "h3"))


class SetTest(_DB):
    def test_import_then_follow(self):
        v1 = self.tree("v1", {"lcg/a.json": A, "alice/b.json": B})
        self.assertEqual(certified.apply_tree(self.conn, v1, 10, "c1"), (1, 2, 0))
        live = self.live()
        self.assertEqual(set(live), {"lcg/a.json", "alice/b.json"})
        self.assertEqual((live["lcg/a.json"]["group"], live["lcg/a.json"]["arch"],
                          live["lcg/a.json"]["build_id"]), ("lcg", "el9", "b1"))
        added = [e for e in events.since(self.conn) if e["kind"] == "certified.added"]
        self.assertTrue(all(e["payload"]["imported"] for e in added))
        self.assertEqual(added[0]["subject"], "certified:alice/el10")
        self.assertIn("certified.imported", self.kinds())

        changed = dict(A, extra=1)
        v2 = self.tree("v2", {"lcg/a.json": changed, "lcg/c.json": C})
        self.assertEqual(certified.apply_tree(self.conn, v2, 11, "c2"), (2, 2, 2))
        self.assertEqual(set(self.live()), {"lcg/a.json", "lcg/c.json"})
        gone = certified.listing(self.conn, include_removed=True)["entries"]
        self.assertEqual(len(gone), 4)              # a and b removed, a again and c
        removed = [e["payload"] for e in events.since(self.conn, limit=100)
                   if e["kind"] == "certified.removed"]
        self.assertEqual(sorted(p["path"] for p in removed), ["alice/b.json", "lcg/a.json"])
        self.assertNotIn("imported", [e for e in events.since(self.conn, limit=100)
                                      if e["kind"] == "certified.added"][-1]["payload"])
        self.assertEqual(certified.apply_tree(self.conn, v2, 12, "c3"), (3, 0, 0))   # no change
        self.assertEqual(certified.current(self.conn), (3, 12, "c3"))
        self.assertEqual(events.verify(self.conn)[1], None)

    def test_materialize_gives_each_versions_bytes_back(self):
        v1 = self.tree("v1", {"lcg/a.json": A, "alice/b.json": B})
        certified.apply_tree(self.conn, v1, 10, "c1")
        v2 = self.tree("v2", {"lcg/a.json": dict(A, extra=1)})
        certified.apply_tree(self.conn, v2, 11, "c2")
        for version, src in ((1, v1), (2, v2)):
            out = certified.materialize(self.conn, version, os.path.join(self.dir, "m%d" % version))
            self.assertEqual({k: shadow._read(p) for k, p in shadow._files(out).items()},
                             {k: shadow._read(p) for k, p in shadow._files(src).items()})

    def test_an_unsafe_path_is_refused(self):
        v1 = self.tree("v1", {"lcg/a.json": A})
        certified.apply_tree(self.conn, v1, 10, "c1")
        self.conn.execute("UPDATE certified SET path = '../../evil.json'")
        with self.assertRaises(ValueError):
            certified.materialize(self.conn, 1, os.path.join(self.dir, "m"))

    def test_a_bom_bits_cannot_certify_is_kept_and_flagged(self):
        mixed = {"build_id": "bx", "packages": [
            {"hash": "h1", "tarball_sha256": "s", "effective_architecture": "el9"},
            {"hash": "h2", "tarball_sha256": "s", "effective_architecture": "el10"}]}
        v1 = self.tree("v1", {"lcg/m.json": mixed, "lcg/x.json": b"{nope"})
        certified.apply_tree(self.conn, v1, 10, "c1")
        live = self.live()
        self.assertIn("mixes architectures", live["lcg/m.json"]["problem"])
        self.assertEqual(live["lcg/x.json"]["problem"], "not JSON")

    def test_the_store_copy_is_matched_ignoring_certified_by(self):
        store_a = dict(A)
        git_a = dict(A, certified_by=["alice"])
        s3 = FakeS3({"MANIFESTS/b1/host.el9.json": json.dumps(store_a).encode(),
                     "MANIFESTS/b1/host.el9.json.sig": b"{}",
                     "MANIFESTS/b2/host.el10.json": json.dumps(dict(B, extra=1)).encode()})
        v1 = self.tree("v1", {"lcg/a.json": git_a, "alice/b.json": B, "lcg/c.json": C})
        certified.apply_tree(self.conn, v1, 10, "c1")
        self.assertEqual(certified.match_store(self.conn, (s3, "bucket")), 3)
        live = self.live()
        self.assertEqual((live["lcg/a.json"]["store_match"], live["lcg/a.json"]["store_key"]),
                         ("same", "MANIFESTS/b1/host.el9.json"))
        self.assertEqual(live["alice/b.json"]["store_match"], "differs")
        self.assertEqual(live["lcg/c.json"]["store_match"], "absent")
        self.assertEqual(certified.match_store(self.conn, (s3, "bucket")), 0)
        self.assertEqual(certified.listing(self.conn)["groups"],
                         {"lcg": {"same": 1, "absent": 1}, "alice": {"differs": 1}})


class SyncTest(_Base):
    def setUp(self):
        super().setUp()
        self.gl.trees["aaaa000"] = {"manifests/lcg/a.json": _bom("b1", "el9", ("p", "h1"))}
        self.gl.trees["aaaa111"] = dict(self.gl.trees["aaaa000"], **{
            "manifests/lcg/b.json": _bom("b2", "el9", ("q", "h2"))})
        self.store = {("el9", "h1"): "sh1", ("el9", "h2"): "sh2"}

    def test_the_set_follows_pipelines_in_order(self):
        self.gl.add(4, "aaaa000", "aaaa000")
        self.gl.add(5, "aaaa111", "aaaa000")
        self.gl.add(6, "deadbeef", "aaaa111")
        shadow.discover(self.conn, self.gl, self.settings)
        self.assertEqual(certified.sync(self.conn, self.gl, self.cache, limit=1), 1)
        self.assertEqual(certified.current(self.conn)[1], 4)
        self.gl.running = [6]                                 # not past one still running
        self.assertEqual(certified.sync(self.conn, self.gl, self.cache, branch="main"), 1)
        self.assertEqual(certified.followed(self.conn), 5)
        self.gl.running = []
        with self.assertLogs("bits_orchestrator.certified", "WARNING"):
            for _ in range(certified.MAX_ATTEMPTS):           # 6's commit is gone
                self.assertEqual(certified.sync(self.conn, self.gl, self.cache), 0)
        self.assertEqual((certified.followed(self.conn), certified.current(self.conn)[:2]),
                         (6, (2, 5)))
        self.assertIsNone(certified.version_of(self.conn, 6))
        self.assertIn("certified.gap", self.kinds())
        self.assertEqual(set(self.live_paths()), {"lcg/a.json", "lcg/b.json"})
        v = certified.listing(self.conn)
        self.assertEqual(v["digest"], certified.digest_of(certified.members(self.conn, 2)))

    def live_paths(self):
        return [e["path"] for e in certified.listing(self.conn)["entries"]]

    def test_the_set_computes_what_the_tree_computes(self):
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el9"]))
        self.run_shadow()
        self.assertEqual(self.run_rows(5, "set")["el9"]["state"], "match")
        self.assertEqual(self.run_rows(5, "set")["el9"]["manifest_sha256"],
                         self.run_rows(5)["el9"]["manifest_sha256"])
        self.assertEqual(shadow.summary(self.conn, mode="set"), {"match": 2})   # el9 and (tree)
        self.assertEqual(self.run_rows(5, "set")["(tree)"]["state"], "match")
        self.assertEqual(self.conn.execute("SELECT job_status FROM set_versions WHERE"
                                           " pipeline_id = 5").fetchone()[0], "success")

    def test_a_set_that_differs_from_the_tree_is_a_mismatch(self):
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el9"]))
        shadow.discover(self.conn, self.gl, self.settings)
        certified.sync(self.conn, self.gl, self.cache)
        self.conn.execute("DELETE FROM certified WHERE path = 'lcg/b.json'")   # the set lost one
        self.run_shadow(sync=False)
        self.assertEqual(self.run_rows(5)["el9"]["state"], "match")
        self.assertEqual(self.run_rows(5, "set")["el9"]["state"], "mismatch")
        self.assertEqual(json.loads(self.run_rows(5, "set")["(tree)"]["diff"])["only_tree"],
                         ["lcg/b.json"])
        self.assertIn("shadow.set_mismatch", self.kinds())

    def test_a_pipeline_the_set_skipped_says_so(self):
        self.gl.add(6, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el9"]))
        shadow.discover(self.conn, self.gl, self.settings)
        certified.sync(self.conn, self.gl, self.cache)
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el9"]))   # found after 6 was synced
        self.run_shadow()
        self.assertEqual(self.run_rows(5, "set")["*"]["state"], "unsynced")
        self.assertEqual(self.run_rows(6, "set")["el9"]["state"], "match")

    def test_comparisons_wait_for_the_set(self):
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el9"]))
        shadow.discover(self.conn, self.gl, self.settings)
        self.assertIsNone(shadow.compare_one(self.conn, self.gl, ("s3", "b"), self.settings,
                                             self.cache, trusted=self.trusted,
                                             probe_factory=lambda c: self.probe, upto=0))
        self.assertEqual(self.cert(5)["state"], "pending")


class ApiTest(_DB):
    def setUp(self):
        super().setUp()
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4", "BITS_ORCH_DB": os.path.join(self.dir, "orch.db"),
            "BITS_ADMINS_POLICY": "lcg @alice\n* @carol"})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        v1 = self.tree("v1", {"lcg/a.json": A, "alice/b.json": B})
        certified.apply_tree(self.conn, v1, 10, "c1")

    def _get(self, user, path):
        return self.client.get(path, headers={"Authorization": "Bearer %s" % user})

    def test_the_set_by_group(self):
        paths = lambda u, q="": [e["path"] for e in self._get(u, "/ops/certified" + q).json()["entries"]]  # noqa: E731
        self.assertEqual(sorted(paths("carol")), ["alice/b.json", "lcg/a.json"])
        self.assertEqual(paths("alice"), ["lcg/a.json"])
        self.assertEqual(paths("carol", "?group=alice"), ["alice/b.json"])
        self.assertEqual(paths("dave"), [])
        r = self._get("carol", "/ops/certified")
        self.assertEqual((r.json()["version"], r.json()["commit"]), (1, "c1"))
        self.assertEqual(self.client.get("/ops/certified").status_code, 401)

    def test_one_bom_by_sha256(self):
        sha = self._get("carol", "/ops/certified?group=alice").json()["entries"][0]["bom_sha256"]
        r = self._get("carol", "/ops/certified/bom/" + sha)
        self.assertEqual((r.status_code, r.json()["build_id"]), (200, "b2"))
        self.assertEqual(self._get("alice", "/ops/certified/bom/" + sha).status_code, 404)
        self.assertEqual(self._get("carol", "/ops/certified/bom/xyz").status_code, 400)


if __name__ == "__main__":
    unittest.main()
