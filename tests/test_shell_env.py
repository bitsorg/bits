# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The build's environment reaches the host's shell quoted, names too: a recipe's
`hook:` name is part of one, and the docker command line runs on the host."""

import os
import shutil
import subprocess
import tempfile
import unittest

from bits_helpers.build import shell_env


class ShellEnvTest(unittest.TestCase):
  def test_a_name_runs_nothing(self):
    work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, work)
    env = [("-Stouch pwned2;Z_HOOKS", "b"), ("PKGNAME", "zlib"), ("X;touch pwned;Y_HOOKS", "a"),
           ("V", "it's $HOME")]   # first: env takes no option after an assignment
    out = subprocess.run("env -i -- %s /usr/bin/env" % shell_env(env), shell=True, cwd=work,
                         capture_output=True, text=True)
    self.assertEqual(os.listdir(work), [])
    self.assertIn("PKGNAME=zlib\n", out.stdout)
    self.assertIn("V=it's $HOME\n", out.stdout)

  def test_docker_options(self):
    self.assertEqual(shell_env([("A", "1"), ("B", "x y")], "-e "), "-e A=1 -e 'B=x y'")


if __name__ == "__main__":
  unittest.main()
