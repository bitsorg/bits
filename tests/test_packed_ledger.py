# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits_helpers.packed_ledger: the tarballs builds here packed, the only ones the
signed-reuse gate exempts."""

import hashlib
import os
import shutil
import tempfile
import unittest

from bits_helpers import packed_ledger


def record(work, tar):
  """As build_template.sh records a tarball it packed."""
  entry = packed_ledger._entry(work, tar)
  os.makedirs(os.path.dirname(entry), exist_ok=True)
  with open(tar, "rb") as f, open(entry, "w") as out:
    out.write(hashlib.sha256(f.read()).hexdigest() + "\n")


class PackedLedgerTest(unittest.TestCase):
  def setUp(self):
    self.work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.work)
    self.dir = os.path.join(self.work, "TARS", "el9", "store", "ab", "ab12")
    os.makedirs(self.dir)
    self.tar = os.path.join(self.dir, "zlib-1.0-1.el9.tar.gz")
    with open(self.tar, "wb") as f:
      f.write(b"packed")

  def test_only_what_was_recorded_as_it_was(self):
    self.assertFalse(packed_ledger.packed_here(self.work, self.tar))       # no record: a download
    record(self.work, self.tar)
    self.assertTrue(packed_ledger.packed_here(self.work, self.tar))
    self.assertTrue(os.path.isfile(os.path.join(
      self.work, "PACKED", "TARS", "el9", "store", "ab", "ab12", "zlib-1.0-1.el9.tar.gz.sha256")))
    with open(self.tar, "wb") as f:                                         # replaced since
      f.write(b"other")
    self.assertFalse(packed_ledger.packed_here(self.work, self.tar))

  def test_a_link_is_never_packed_here(self):
    record(self.work, self.tar)
    real = self.tar + ".real"
    os.rename(self.tar, real)
    os.symlink(real, self.tar)
    self.assertFalse(packed_ledger.packed_here(self.work, self.tar))

  def test_a_refused_tarball_is_kept_aside(self):
    with open(self.tar + ".sha256", "w") as f:
      f.write("sha256:x 6\n")
    where = packed_ledger.quarantine(self.work, self.tar)
    self.assertEqual(where, os.path.join(self.work, "QUARANTINE", "TARS", "el9", "store", "ab", "ab12"))
    self.assertEqual(sorted(os.listdir(where)), ["zlib-1.0-1.el9.tar.gz", "zlib-1.0-1.el9.tar.gz.sha256"])
    self.assertEqual(os.listdir(self.dir), [])
    with open(self.tar, "wb") as f:                                         # refused again
      f.write(b"second")
    again = packed_ledger.quarantine(self.work, self.tar)
    self.assertEqual(again, where + ".1")                                   # the first kept
    with open(os.path.join(where, "zlib-1.0-1.el9.tar.gz"), "rb") as f:
      self.assertEqual(f.read(), b"packed")



class BuildScriptRecordTest(unittest.TestCase):
  """build_template.sh records the tarball it packed where packed_ledger looks, before the
  tarball takes its place (so another bits process never finds it unrecorded)."""

  def test_where_and_when(self):
    template = os.path.join(os.path.dirname(packed_ledger.__file__), "build_template.sh")
    with open(template) as f:
      text = f.read()
    entry = '"$WORK_DIR/PACKED/TARS/$HASH_PATH/$PACKAGE_WITH_REV.sha256"'
    self.assertLess(text.index(entry), text.index('mv "$_dst" "$_final"'))
    work = "/w"
    self.assertEqual(packed_ledger._entry(work, "/w/TARS/el9/store/ab/ab12/z-1-1.el9.tar.gz"),
                     "/w/PACKED/TARS/el9/store/ab/ab12/z-1-1.el9.tar.gz.sha256")


if __name__ == "__main__":
  unittest.main()
