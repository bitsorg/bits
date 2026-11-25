# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""runBuildCommand gathers a failure's report (host, xcodebuild, log excerpt) only
when the build failed: a successful package costs none of it."""

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


class FailureReportTest(unittest.TestCase):
  def run_build(self, rc):
    work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, work)
    args = Namespace(builders=2, workDir=work, debug=False, resourceMonitoring=False,
                     architecture="osx_arm64", autoCleanup=False, aggressiveCleanup=False)
    sched = Namespace(buildFailures=[], errors={}, buildFailuresLock=threading.Lock(), log=MagicMock())
    with patch.dict(os.environ, {"BITS_DIST_HASH": "0123456789"}), \
         patch("bits_helpers.build.execute", return_value=rc), \
         patch("bits_helpers.build.detectArch", return_value="osx_arm64") as arch, \
         patch("bits_helpers.build.getstatusoutput", return_value=(0, "Xcode 16\nBuild 1")) as xcode, \
         patch("bits_helpers.build.doFinalSync"), patch("bits_helpers.monitor.note_build"):
      message = runBuildCommand(sched, "zlib", {"zlib": dict(SPEC)}, args, "true", None, work, work, None)
    return message, arch.call_count, xcode.call_count, sched.buildFailures

  def test_only_a_failure_is_reported(self):
    self.assertEqual(self.run_build(0), (None, 0, 0, []))
    message, arch, xcode, failures = self.run_build(2)
    self.assertIn("BUILD FAILED: zlib@v1.3.1", message)
    self.assertIn("XCode: Xcode 16 (Build 1)", message)
    self.assertEqual((arch, xcode, len(failures)), (1, 1, 1))     # each gathered once


if __name__ == "__main__":
  unittest.main()
