# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A native build runs its script without the run's credentials (a recipe gets one
only through -e); a --docker build's command keeps them for docker itself, and the
container gets only its -e variables."""

import os
import shutil
import tempfile
import threading
import unittest
from argparse import Namespace
from unittest.mock import MagicMock, patch

from bits_helpers.build import runBuildCommand

SPEC = {"package": "zlib", "version": "v1.3.1", "revision": "1", "hash": "ab" * 20,
        "architecture": "", "is_devel_pkg": False, "requires": []}


class NativeBuildEnvTest(unittest.TestCase):
  def run_build(self, docker, passed=()):
    work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, work)
    out = os.path.join(work, "seen")
    args = Namespace(builders=2, workDir=work, debug=False, resourceMonitoring=False, docker=docker,
                     environment=list(passed),
                     architecture="el9", autoCleanup=False, aggressiveCleanup=False)
    sched = Namespace(buildFailures=[], errors={}, buildFailuresLock=threading.Lock(), log=MagicMock())
    command = 'echo "${CI_JOB_TOKEN:-none} ${BITS_STORE_TOKEN:-none} ${HOME:+home}" > %s; exit 3' % out
    with patch.dict(os.environ, {"BITS_DIST_HASH": "0123456789", "CI_JOB_TOKEN": "t0k",
                                 "BITS_STORE_TOKEN": "s3cr"}), \
         patch("bits_helpers.build.detectArch", return_value="el9"), \
         patch("bits_helpers.monitor.note_build"):
      runBuildCommand(sched, "zlib", {"zlib": dict(SPEC)}, args, command, None, work, work, None)
    with open(out) as f:
      return f.read()

  def test_native_without_credentials(self):
    self.assertEqual(self.run_build(docker=False), "none none home\n")

  def test_native_with_one_passed_by_name(self):
    self.assertEqual(self.run_build(docker=False, passed=["BITS_STORE_TOKEN"]), "none s3cr home\n")

  def test_docker_command_as_before(self):
    self.assertEqual(self.run_build(docker=True), "t0k s3cr home\n")


if __name__ == "__main__":
  unittest.main()
