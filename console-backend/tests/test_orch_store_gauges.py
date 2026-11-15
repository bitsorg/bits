# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The worker pushes the store's gauges for Monitoring: objects by architecture and
by certified build, on a schedule."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from console_backend import config
from console_backend.orch import db, store_gauges, worker


class Paged:
    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, _op):
        objs = self.objects

        class P:
            def paginate(self, Bucket, **kw):
                return [{"Contents": [{"Key": k, "Size": s} for k, s in objs.items()]}]
        return P()


class StoreGaugesTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.pushed = []
        self.s3 = Paged({"TARS/el9/store/aa/aa11/ROOT-1-1.el9.tar.gz": 100,
                         "TARS/el9/store/bb/bb22/zlib-1-1.el9.tar.gz": 10,
                         "TARS/el10/store/cc/cc33/ROOT-1-1.el10.tar.gz": 7,
                         "MANIFESTS/b-1/p5-NOTICE": 1})

    def certify(self, build_id, hashes, removed=None):
        body = json.dumps({"build_id": build_id, "packages": [{"hash": h} for h in hashes]}).encode()
        sha = hashlib.sha256(body).hexdigest()
        self.conn.execute("INSERT OR IGNORE INTO boms (sha256, body, size, first_ms) VALUES (?,?,?,0)",
                          (sha, body, len(body)))
        self.conn.execute("INSERT INTO certified (path, grp, arch, build_id, bom_sha256, added_version,"
                          " added_ms, removed_version) VALUES (?, 'lcg', 'el9', ?, ?, 1, 0, ?)",
                          ("lcg/%s.json" % build_id, build_id, sha, removed))
        self.conn.commit()

    def step(self, now_ms, url="http://vm:8428"):
        return store_gauges.step(self.conn, (self.s3, "bkt"), url, 3600, now_ms=now_ms,
                                 push=lambda u, body, timeout: self.pushed.append((u, body)))

    def test_by_architecture_and_certified_build(self):
        self.certify("release_x-1", ["aa11"])
        self.certify("release_old", ["bb22"], removed=2)           # no longer certified
        self.assertEqual(self.step(10_000_000), 4)
        (url, body), = self.pushed
        self.assertEqual(url, "http://vm:8428")
        self.assertIn('bits_store_bytes{arch="el9"} 110', body)
        self.assertIn('bits_store_bytes{arch="el10"} 7', body)
        self.assertIn('bits_store_manifest_bytes{manifest="release_x-1",arch="el9",signed="1"} 100', body)
        self.assertIn('bits_store_manifest_bytes{manifest="(uncertified)",arch="el9",signed="0"} 10', body)
        self.assertIn("bits_store_objects_total 4", body)

    def test_at_most_every_period(self):
        self.step(10_000_000)
        self.assertEqual(self.step(10_000_000 + 3599_000), 0)
        self.assertEqual(self.step(10_000_000 + 3600_000), 4)
        self.assertEqual(len(self.pushed), 2)

    def test_a_shared_hash_goes_to_the_first_build_by_id(self):
        self.certify("release_b", ["aa11"])
        self.certify("release_a", ["aa11"])
        self.step(10_000_000)
        body = self.pushed[0][1]
        self.assertIn('manifest="release_a",arch="el9",signed="1"} 100', body)
        self.assertNotIn('manifest="release_b",arch="el9"', body)

    def test_off_without_a_url_and_an_error_without_a_store(self):
        self.assertEqual(self.step(1, url=""), 0)
        with self.assertRaisesRegex(RuntimeError, "BITS_ORCH_STORE"):
            store_gauges.step(self.conn, None, "http://vm", 3600, now_ms=1)
        self.assertEqual(self.pushed, [])

    def test_a_failure_is_tried_again_in_five_minutes(self):
        def down(*a, **k):
            raise OSError("vm down")
        with self.assertRaises(OSError):
            store_gauges.step(self.conn, (self.s3, "bkt"), "http://vm", 3600, now_ms=10_000_000, push=down)
        self.assertEqual(self.step(10_000_000 + 299_000), 0)            # not every pass
        self.assertEqual(self.step(10_000_000 + 300_000), 4)

    def test_the_worker_runs_it_only_with_a_metrics_url(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        base = {"BITS_ORCH_DB": os.path.join(d, "o.db")}
        steps = lambda env: [n for n, _ in worker.Worker(self.conn, config.Settings(env=env)).steps]
        self.assertNotIn("store_gauges", steps(base))
        self.assertIn("store_gauges", steps(dict(base, BITS_ORCH_METRICS_URL="http://vm:8428")))
        s = config.Settings(env=dict(base, BITS_ORCH_STORE_GAUGES_S="10"))
        self.assertEqual(s.orch_store_gauges_s, 300.0)                 # not more often than 5 min


if __name__ == "__main__":
    unittest.main()
