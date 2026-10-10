# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""The work directory (sw/) is created when a command runs, not for --help."""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class WorkDirTest(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.tmp, True)
    self.env = dict(os.environ, HOME=self.tmp)
    for v in ("BITS_WORK_DIR", "ALICE_WORK_DIR", "BITS_CHDIR"):
      self.env.pop(v, None)

  def _bits(self, *args, cwd=None):
    return subprocess.run([os.path.join(ROOT, "bits")] + list(args), cwd=cwd or self.tmp,
                          env=self.env, capture_output=True, text=True)

  def test_help_version_architecture_leave_no_work_dir(self):
    for args in (["init", "--help"], ["build", "--help"], ["doctor", "--help"],
                 ["version"], ["architecture"]):
      with self.subTest(args=args):
        self.assertEqual(self._bits(*args).returncode, 0)
        self.assertEqual(os.listdir(self.tmp), [])

  def test_a_command_creates_it_where_it_runs(self):
    os.makedirs(os.path.join(self.tmp, "sub"))
    r = self._bits("clean", "-C", "sub", "-a", "slc9_x86-64")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(sorted(os.listdir(self.tmp)), ["sub"])           # not ./sw
    self.assertTrue(os.path.isdir(os.path.join(self.tmp, "sub", "sw")))


if __name__ == "__main__":
  unittest.main()
