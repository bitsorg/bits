# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""download.py runs no shell on names that come from a source URL: they ran on the
host, outside any sandbox, with the whole environment."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers import download as dl


class NoShellTest(unittest.TestCase):
  def setUp(self):
    self.work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.work)
    self.cwd = os.getcwd()
    os.chdir(self.work)                       # where a `touch` would land
    self.addCleanup(os.chdir, self.cwd)

  def test_a_source_name_runs_nothing(self):
    name = "a;touch pwned;b.tar.gz"

    def fetch(source, dest_dir, work_dir):
      with open(os.path.join(dest_dir, name), "w") as f:
        f.write("data")
    dest = os.path.join(self.work, "SOURCES", "pkg", "1", "x")
    with patch.dict(dl.downloadHandlers, {"https": fetch}):
      dl.download("https://example.org/" + name, dest, self.work)
    self.assertFalse(os.path.exists(os.path.join(self.work, "pwned")))
    with open(os.path.join(dest, name)) as f:
      self.assertEqual(f.read(), "data")


if __name__ == "__main__":
  unittest.main()
