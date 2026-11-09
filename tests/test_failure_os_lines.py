# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The OS lines of a build failure report (runBuildCommand)."""
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bits_helpers.build import failure_os_lines


class FailureOsLinesTest(unittest.TestCase):
  def test_docker_build_names_host_and_container(self):
    args = SimpleNamespace(docker=True, dockerImage="reg/containers/x86_64-ubuntu2604")
    self.assertEqual(failure_os_lines(args, "x86_64-ubuntu2510"),
                     ["Host OS: x86_64-ubuntu2510", "Container: reg/containers/x86_64-ubuntu2604"])

  def test_local_build_is_one_os_line(self):
    for args in (SimpleNamespace(docker=False, dockerImage="reg/img"),
                 SimpleNamespace(docker=True, dockerImage=None), SimpleNamespace()):
      self.assertEqual(failure_os_lines(args, "x86_64-el9"), ["OS: x86_64-el9"])


if __name__ == "__main__":
  unittest.main()
