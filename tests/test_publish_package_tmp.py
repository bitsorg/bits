# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""`bits publish PACKAGE --cvmfs-target` makes its working copy and its tar under the
work dir, not the system temp dir (often small), and removes them whatever stops it."""

import json
import os
import shutil
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bits_helpers import publish
from bits_helpers.checksum import checksum_file


class Parser:
  def error(self, msg):
    raise SystemExit(msg)


class PackageTmpTest(unittest.TestCase):
  def run_publish(self, relocate):
    work, systmp = tempfile.mkdtemp(), tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, work)
    self.addCleanup(shutil.rmtree, systmp)
    root = os.path.join(work, "el9", "zlib", "1.3-1")
    os.makedirs(os.path.join(root, "lib"))
    with open(os.path.join(root, ".build-hash"), "w") as f:
      f.write("ab" * 20)
    for name, text in ((".meta.json", "{}"),
                       ("relocate-me.sh", ': "$BITS_RELOCATE_STRIP_PP"\n' + relocate)):
      with open(os.path.join(root, name), "w") as f:
        f.write(text)
    store = os.path.join(work, "TARS", "el9", "store", "ab", "ab" * 20)
    os.makedirs(store)
    with tarfile.open(os.path.join(store, "zlib-1.3-1.el9.tar.gz"), "w:gz") as tf:
      tf.add(root, "el9/zlib/1.3-1")
    with open(os.path.join(work, "bits-manifest-1.json"), "w") as f:
      json.dump({"packages": [{"package": "zlib", "hash": "ab" * 20, "tarball_sha256":
                               checksum_file(os.path.join(store, "zlib-1.3-1.el9.tar.gz"))}]}, f)
    args = SimpleNamespace(publishView=None, fromManifest=None, package="zlib", version=None,
                           workDir=work, architecture="el9", cvmfsTarget="/cvmfs/r/x", scratchDir=None,
                           prepubUrl="https://prepub", prepubToken=None, prepubRepo=None,
                           prepubPath=None, prepubWebhook=None, prepubPollInterval=10,
                           prepubTimeout=60, prepubNoVerifyTls=False, dryRun=False)
    tars = []
    with patch.object(publish, "_find_installroot", return_value=root), \
         patch("bits_helpers.cvmfs_publish.signed_index", return_value={}), \
         patch.object(tempfile, "tempdir", systmp), \
         patch.object(publish, "_publish_tree",
                      lambda tree, *a, **kw: tars.append(kw.get("tmp_dir"))):
      try:
        publish.doPublish(args, Parser())
      except SystemExit:
        pass
    return work, systmp, tars

  def test_under_the_work_dir_and_removed(self):
    work, systmp, tars = self.run_publish("exit 0\n")
    self.assertEqual(len(tars), 1)
    self.assertTrue(tars[0].startswith(os.path.join(work, "TMP") + os.sep), tars[0])
    self.assertEqual((os.listdir(systmp), os.listdir(os.path.join(work, "TMP"))), ([], []))

  def test_removed_when_relocation_fails(self):
    work, systmp, tars = self.run_publish("exit 3\n")
    self.assertEqual(tars, [])
    self.assertEqual((os.listdir(systmp), os.listdir(os.path.join(work, "TMP"))), ([], []))


if __name__ == "__main__":
  unittest.main()
