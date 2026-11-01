# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import subprocess
import unittest
from unittest import mock

from bits_helpers import version as v


class VersionTest(unittest.TestCase):
  def _git(self, describe, date="2026-10-02", dirty=False):
    out = {"describe": describe, "log": date, "status": " M bits" if dirty else ""}
    return mock.patch.object(v, "_git", side_effect=lambda *a: out[a[0]])

  def test_git_describe_forms(self):
    with mock.patch.object(v.os.path, "exists", return_value=True):
      with self._git("0.5-199-g648dfe8"):
        self.assertEqual(v.version_line(v._from_git()),
                         "bits 0.5-199-g648dfe8 (tag 0.5 +199, commit 648dfe8, 2026-10-02)")
      with self._git("v1-2-0-gabc1234"):   # exactly on a hyphenated tag
        self.assertEqual(v.version_line(v._from_git()),
                         "bits v1-2 (tag v1-2, commit abc1234, 2026-10-02)")
      with self._git("0.5-3-g648dfe8", dirty=True):
        info = v._from_git()
        self.assertEqual(info["version"], "0.5-3-g648dfe8-dirty")
        self.assertIn("with local changes", v.version_line(info))
      with self._git("0.5-0-g648dfe8", dirty=True):   # on the tag, but edited
        self.assertEqual(v._from_git()["version"], "0.5-0-g648dfe8-dirty")
      with self._git("648dfe8", dirty=True):   # no tag reachable
        self.assertEqual(v.version_line(v._from_git()),
                         "bits 648dfe8-dirty (commit 648dfe8, 2026-10-02, with local changes)")
      with self._git("648dfe8"):               # no tag, clean
        self.assertEqual(v.version_line(v._from_git()), "bits 648dfe8 (commit 648dfe8, 2026-10-02)")

  def test_git_failure_falls_back(self):
    with mock.patch.object(v.os.path, "exists", return_value=True), \
         mock.patch.object(v, "_git", side_effect=subprocess.CalledProcessError(128, "git")):
      self.assertIsNone(v._from_git())

  def test_package_version_and_unknown(self):
    with mock.patch.dict("sys.modules", {"bits_helpers._version":
                                         mock.Mock(version="0.6.dev199+g648dfe8.d20261002")}):
      self.assertEqual(v.version_line(v._from_package()),
                       "bits 0.6.dev199+g648dfe8.d20261002 "
                       "(commit 648dfe8, 2026-10-02, with local changes)")
    for desc, line in (("0.7-3-g1a2b3c4", "bits 0.7-3-g1a2b3c4 (tag 0.7 +3, commit 1a2b3c4)"),
                       ("0.7-0-g1a2b3c4", "bits 0.7 (tag 0.7, commit 1a2b3c4)")):
      with mock.patch.dict("sys.modules", {"bits_helpers._version": mock.Mock(version=desc)}):
        self.assertEqual(v.version_line(v._from_package()), line)
    self.assertEqual(v.version_line({}),
                     "bits unknown (neither an installed package nor a git checkout)")


if __name__ == "__main__":
  unittest.main()
