# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""publish_one unpacks a tarball only when no member is absolute or climbs out, as
the build's own unpack checks (build_template.sh's tar-slip guard)."""

import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

import bits_helpers.cvmfs_publish as cp


class UnpackGuardTest(unittest.TestCase):
  def publish(self, extra):
    t = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, t, True)
    tar = os.path.join(t, "pub", "GCC-14-1.tc.tar.gz")
    os.makedirs(os.path.dirname(tar))
    with tarfile.open(tar, "w:gz") as tf:
      for name, data in (("tc/GCC/14-1/.meta.json", json.dumps({}).encode()),) + extra:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    tm = {"prefix": "/cvmfs/r/g", "path": "{prefix}/{pkg}/{version}/{arch}"}
    ctx = {"repo": "r", "tars_root": t, "arch": "el9", "tmpl_prefix": "", "templates": tm,
           "prefix_fallback": "/cvmfs/r", "tmp_dir": os.path.join(t, "pub"), "submit": True}
    spec = {"package": "GCC", "version": "14", "revision": "1", "effective_architecture": "tc"}
    with mock.patch.object(cp, "tar_path", lambda s, r, a: tar), \
         mock.patch.object(cp, "published_state", lambda c, p: {"exists": False}), \
         mock.patch.object(cp, "_publish_tar", lambda *a, **k: "J"):
      cp.publish_one(spec, ctx)
    return t

  def test_a_climbing_member_is_refused(self):
    for member in ("../escaped", "tc/GCC/14-1/../../../../escaped", "/tmp/escaped-abs"):
      with self.assertRaises(SystemExit):
        t = self.publish(((member, b"x"),))
      self.assertFalse(os.path.exists("/tmp/escaped-abs"))

  def test_a_plain_one_is_published(self):
    self.publish((("tc/GCC/14-1/bin/..hidden", b"x"),))


if __name__ == "__main__":
  unittest.main()
