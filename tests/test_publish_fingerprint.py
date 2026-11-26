# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""publish_one reads and hashes a package's whole tree for its FINGERPRINT only on a
dry run, the one that prints it; a real publish skips that read."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import bits_helpers.cvmfs_publish as cp


class FingerprintTest(unittest.TestCase):
  def publish(self, submit):
    t = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, t, True)
    root = os.path.join(t, "src", "tc", "GCC", "14-1")
    os.makedirs(root)
    with open(os.path.join(root, ".meta.json"), "w") as fh:
      json.dump({}, fh)
    tar = os.path.join(t, "GCC-14-1.tc.tar.gz")
    subprocess.run(["tar", "-czf", tar, "-C", os.path.join(t, "src"), "tc"], check=True)
    tm = {"prefix": "/cvmfs/r/g", "path": "{prefix}/{pkg}/{version}/{arch}"}
    ctx = {"repo": "r", "tars_root": t, "arch": "el9", "tmpl_prefix": "", "templates": tm,
           "prefix_fallback": "/cvmfs/r", "tmp_dir": t, "submit": submit}
    spec = {"package": "GCC", "version": "14", "revision": "1", "effective_architecture": "tc"}
    seen = []
    with mock.patch.object(cp, "tar_path", lambda s, r, a: tar), \
         mock.patch.object(cp, "published_state", lambda c, p: {"exists": False}), \
         mock.patch.object(cp, "tree_fingerprint", return_value="FP") as fingerprint, \
         mock.patch.object(cp, "_publish_tar", lambda c, p, *a, **k: seen.append(k["fp"]) or "J"):
      cp.publish_one(spec, ctx)
    return fingerprint.call_count, seen

  def test_only_a_dry_run_reads_the_tree(self):
    self.assertEqual(self.publish(submit=True), (0, [None]))
    self.assertEqual(self.publish(submit=False), (1, ["FP"]))


if __name__ == "__main__":
  unittest.main()
