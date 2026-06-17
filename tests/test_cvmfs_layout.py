# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the templated CVMFS layout resolver (bits_helpers/cvmfs_layout.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers.cvmfs_layout import resolve_cvmfs_layout as R

ARCH = "ubuntu2510_x86-64-gcc15-dbg"


class CvmfsLayoutTest(unittest.TestCase):
    def test_none_when_unconfigured(self):
        self.assertIsNone(R({}, ARCH))
        self.assertIsNone(R(None, ARCH))
        self.assertIsNone(R({"variables": {"x": "1"}}, ARCH))

    def test_full_layout_resolves_architecture(self):
        layout = R({
            "cvmfs_dir": "/cvmfs/sft.cern.ch/lcg/releases",
            "install_dir": "%(architecture)s/Packages",
            "module_dir": "%(architecture)s/modules",
        }, ARCH)
        self.assertEqual(layout["install_path"],
                         "/cvmfs/sft.cern.ch/lcg/releases/%s/Packages" % ARCH)
        self.assertEqual(layout["module_path"],
                         "/cvmfs/sft.cern.ch/lcg/releases/%s/modules" % ARCH)

    def test_dirs_default_sensibly(self):
        layout = R({"cvmfs_dir": "/cvmfs/x"}, ARCH)
        self.assertEqual(layout["install_dir"], ARCH)
        self.assertEqual(layout["module_dir"], "%s/modules" % ARCH)
        self.assertEqual(layout["views_dir"], "Views")          # default views dir
        self.assertEqual(layout["install_path"], "/cvmfs/x/" + ARCH)
        self.assertEqual(layout["views_path"], "/cvmfs/x/Views")

    def test_views_dir_override_and_triggers_layout(self):
        # views_dir alone is enough to opt in, and is overridable
        layout = R({"views_dir": "%(architecture)s/views"}, ARCH)
        self.assertIsNotNone(layout)
        self.assertEqual(layout["views_path"], "%s/views" % ARCH)  # relative, no cvmfs_dir

    def test_unknown_placeholder_left_intact(self):
        layout = R({"cvmfs_dir": "/cvmfs/x",
                    "install_dir": "%(nope)s/%(architecture)s"}, ARCH)
        self.assertEqual(layout["install_dir"], "%(nope)s/" + ARCH)

    def test_relative_when_no_cvmfs_dir(self):
        # install_dir/module_dir without cvmfs_dir -> relative paths (local use)
        layout = R({"install_dir": "%(architecture)s/Packages"}, ARCH)
        self.assertEqual(layout["cvmfs_dir"], "")
        self.assertEqual(layout["install_path"], "%s/Packages" % ARCH)


if __name__ == "__main__":
    unittest.main()
