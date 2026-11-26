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


  def git_repo(self):
    import subprocess
    repo = os.path.join(self.work, "up", "repo.git")
    os.makedirs(repo)
    git = ["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    with open(os.path.join(repo, "hello.txt"), "w") as f:
      f.write("hello\n")
    subprocess.run(git + ["add", "hello.txt"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "init"], check=True)
    subprocess.run(git + ["tag", "v1"], check=True)
    return repo

  def test_a_git_source_runs_no_part_of_its_url(self):
    import tarfile
    repo, out = self.git_repo(), os.path.join(self.work, "out")
    os.makedirs(out)
    self.assertTrue(dl.downloadGit("git+file://%s?obj=main/v1" % repo, out, self.work))
    with tarfile.open(os.path.join(out, "repov1.tar.gz")) as tf:
      self.assertEqual(sorted(tf.getnames()), ["repov1", "repov1/hello.txt"])
    for bad in ("main/v1;touch pwned", "main/v1$(touch pwned)", "main/--help"):
      with patch("bits_helpers.download.warning"):
        self.assertFalse(dl.downloadGit("git+file://%s?obj=%s" % (repo, bad), out, self.work))
    self.assertFalse(os.path.exists(os.path.join(self.work, "pwned")))


if __name__ == "__main__":
  unittest.main()
