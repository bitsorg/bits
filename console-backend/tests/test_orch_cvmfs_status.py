# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker's CVMFS status document (cvmfs-status.json in the package registry)."""

import json
import os
import shutil
import tempfile
import unittest

from console_backend.orch import cvmfs_status, db
from test_orch_reconcile import FakeS3

ARCH = "x86_64-el9-gcc14-opt"


class FakeForge:
    def __init__(self, doc=None):
        self.files = {} if doc is None else {cvmfs_status.FILE: json.dumps(doc).encode()}
        self.puts = 0

    def generic_file(self, package, version, name):
        assert (package, version) == (cvmfs_status.PACKAGE, cvmfs_status.VERSION)
        return self.files.get(name)

    def put_generic_file(self, package, version, name, data):
        self.files[name] = data
        self.puts += 1

    def doc(self):
        return json.loads(self.files[cvmfs_status.FILE])


def _bom(arch, pkgs, build_arch=ARCH):
    return {"build_id": "b", "effective_architecture": arch, "architecture": build_arch,
            "published_at": "2026-10-20T10:00:00Z", "requested_packages": ["LCG"],
            "packages": [{"package": n, "version": v, "revision": r, "hash": n}
                         for n, v, r in pkgs]}


class CvmfsStatusTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.s3 = FakeS3({"MANIFESTS/b1/a.json": _bom(ARCH, [("LCG", "110", "2"), ("ROOT", "6", "1")]),
                          "MANIFESTS/b1/t.json": _bom("x86_64-el9-gcc14", [("GCC-Toolchain", "v14", "1")]),
                          "MANIFESTS/b2/a.json": _bom(ARCH, [("ROOT", "6", "1")])})
        self.store = (self.s3, "bkt")

    def _build(self, pid, bid, keys, outcome="success", publish="true", package="LCG", at=1000):
        v = {"PACKAGE": package, "VERSION": "110", "PLATFORM": "x86_64-el9", "COMMUNITY": "LCG",
             "PUBLISH_CVMFS": publish}
        with db.transaction(self.conn):
            self.conn.execute("INSERT OR IGNORE INTO pipelines (id, is_build, outcome, variables,"
                              " first_seen_ms) VALUES (?, 1, ?, ?, 0)",
                              (pid, outcome, json.dumps(v)))
            self.conn.execute("INSERT INTO builds (pipeline_id, build_id, bom_keys, published_at,"
                              " recorded_ms) VALUES (?, ?, ?, ?, ?)",
                              (pid, bid, json.dumps(keys), "2026-10-20T10:00:00Z", at))

    def test_a_record_per_published_build_once(self):
        old = {"packages": [{"pkg_id": "ROOT-6-1-x86_64-el9-gcc13-opt", "package": "ROOT"},
                            {"pkg_id": "LCG-110-2-%s" % ARCH, "package": "LCG", "status": ""}],
               "updated_at": "2026-01-01T00:00:00Z"}
        forge = FakeForge(old)
        self._build(1, "b1", ["MANIFESTS/b1/a.json", "MANIFESTS/b1/t.json"])
        self._build(2, "b2", ["MANIFESTS/b2/a.json"], outcome="failed", at=1001)
        self._build(3, "b2", ["MANIFESTS/b2/a.json"], publish="false", at=1002)
        self.assertEqual(cvmfs_status.step(self.conn, forge, self.store), 1)
        doc = forge.doc()
        self.assertEqual([p["pkg_id"] for p in doc["packages"]],
                         ["ROOT-6-1-x86_64-el9-gcc13-opt", "LCG-110-2-%s" % ARCH])   # kept, replaced
        rec = doc["packages"][1]
        self.assertEqual((rec["status"], rec["run_id"], rec["version"], rec["community"]),
                         ("published", "1", "110", "LCG"))
        self.assertEqual(doc["updated_at"], "2026-10-20T10:00:00Z")
        # Nothing new: no upload.
        self.assertEqual(cvmfs_status.step(self.conn, forge, self.store), 0)
        self.assertEqual(forge.puts, 1)

    def test_the_hosts_arch_older_boms_record_is_not_the_builds(self):
        self.s3.objects["MANIFESTS/b3/a.json"] = json.dumps(
            _bom(ARCH, [("LCG", "110", "3")], build_arch="x86_64-el9")).encode()
        forge = FakeForge()
        self._build(1, "b3", ["MANIFESTS/b3/a.json"])
        cvmfs_status.step(self.conn, forge, self.store)
        self.assertEqual(forge.doc()["packages"][0]["pkg_id"], "LCG-110-3-%s" % ARCH)

    def test_a_pipeline_retried_to_success_and_a_store_error(self):
        forge = FakeForge()
        self._build(1, "b1", ["MANIFESTS/b1/a.json"], outcome="failed")
        self.assertEqual(cvmfs_status.step(self.conn, forge, self.store), 0)
        with db.transaction(self.conn):               # retried: success, indexed again
            self.conn.execute("UPDATE pipelines SET outcome = 'success', indexed_ms = 5000")
        real = self.s3.get_object

        def flaky(Bucket, Key):
            raise OSError("connection reset")
        self.s3.get_object = flaky
        with self.assertRaises(OSError):              # not skipped: looked at again
            cvmfs_status.step(self.conn, forge, self.store)
        self.s3.get_object = real
        self.assertEqual(cvmfs_status.step(self.conn, forge, self.store), 1)

    def test_an_empty_registry_and_a_package_not_in_the_boms(self):
        forge = FakeForge()
        self._build(1, "b2", ["MANIFESTS/b2/a.json"])            # LCG is not in b2's BOMs
        self._build(2, "b1", ["MANIFESTS/b1/a.json"], at=1001)
        self.assertEqual(cvmfs_status.step(self.conn, forge, self.store), 1)
        self.assertEqual([p["pkg_id"] for p in forge.doc()["packages"]], ["LCG-110-2-%s" % ARCH])

    def test_an_unreadable_document_is_never_overwritten(self):
        forge = FakeForge()
        forge.files[cvmfs_status.FILE] = b"<html>"
        self._build(1, "b1", ["MANIFESTS/b1/a.json"])
        with self.assertRaises(RuntimeError):
            cvmfs_status.step(self.conn, forge, self.store)
        self.assertEqual((forge.puts, forge.files[cvmfs_status.FILE]), (0, b"<html>"))
        # The cursor did not move: the build is written once the document is fixed.
        forge.files.clear()
        self.assertEqual(cvmfs_status.step(self.conn, forge, self.store), 1)


class GenericPackageTest(unittest.TestCase):
    def test_upload_and_read_paths(self):
        from unittest import mock
        from console_backend import forge_ops
        f = forge_ops.GitLabForge("https://gl/api/v4", "tok", "232657")
        seen = []

        class R:
            status_code, text = 201, ""
        with mock.patch.object(forge_ops.requests, "put",
                               lambda url, **kw: seen.append((url, kw["headers"], kw["data"])) or R()):
            f.put_generic_file("cvmfs-status", "latest", "cvmfs-status.json", b"{}")
        self.assertEqual(seen, [("https://gl/api/v4/projects/232657/packages/generic/cvmfs-status/"
                                 "latest/cvmfs-status.json", {"PRIVATE-TOKEN": "tok"}, b"{}")])
        R.status_code = 403
        with mock.patch.object(forge_ops.requests, "put", lambda url, **kw: R()):
            with self.assertRaises(forge_ops.ForgeError):
                f.put_generic_file("cvmfs-status", "latest", "cvmfs-status.json", b"{}")
        with mock.patch.object(f, "_raw", lambda path, n: path):
            self.assertEqual(f.generic_file("cvmfs-status", "latest", "cvmfs-status.json"),
                             "/packages/generic/cvmfs-status/latest/cvmfs-status.json")
