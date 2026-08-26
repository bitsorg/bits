# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers/preload_bundle — post-publish filebundle spec emitter."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers import preload_bundle as P

REPO = "/cvmfs/sft.cern.ch"
BASE = REPO + "/lcg/releases/x86_64-el9/xrootd/5.9.1"   # deployed package dir


class RepoRootTest(unittest.TestCase):
    def test_repo_root_of(self):
        self.assertEqual(P.repo_root_of("/cvmfs/sft.cern.ch/lcg/releases"),
                         "/cvmfs/sft.cern.ch")
        self.assertEqual(P.repo_root_of("/cvmfs/alice.cern.ch"), "/cvmfs/alice.cern.ch")
        self.assertIsNone(P.repo_root_of("/home/user/sw"))
        self.assertIsNone(P.repo_root_of(""))

    def test_to_repo_absolute(self):
        self.assertEqual(P.to_repo_absolute(BASE + "/lib/libXrdCl.so", REPO),
                         "/lcg/releases/x86_64-el9/xrootd/5.9.1/lib/libXrdCl.so")
        self.assertIsNone(P.to_repo_absolute("/usr/lib64/libc.so.6", REPO))
        self.assertIsNone(P.to_repo_absolute(REPO, REPO))          # the mount itself


class BuildBundleTest(unittest.TestCase):
    def test_build_bundle(self):
        trigger = BASE + "/bin/xrdcp"
        opened = [
            trigger,                                       # excluded (the trigger)
            BASE + "/lib/libXrdCl.so",                     # own package
            REPO + "/lcg/releases/x86_64-el9/Boost/1.90.0/lib/libboost.so",  # dep
            "/usr/lib64/libc.so.6",                        # system -> dropped
            "/proc/self/maps",                             # dropped
        ]
        tar_rel, spec = P.build_bundle(trigger, opened, REPO)
        self.assertEqual(tar_rel,
                         "lcg/releases/x86_64-el9/xrootd/5.9.1/bin/.cvmfsbundle-xrdcp")
        self.assertEqual(spec["name"], "CVMFS_BUNDLE")
        self.assertEqual(spec["version"], "1.0.0")
        self.assertEqual(spec["encoding"], "UTF-8")
        self.assertEqual(spec["dependencies"], [
            "/lcg/releases/x86_64-el9/Boost/1.90.0/lib/libboost.so",
            "/lcg/releases/x86_64-el9/xrootd/5.9.1/lib/libXrdCl.so",
        ])

    def test_trigger_not_under_repo(self):
        self.assertEqual(P.build_bundle("/home/x/bin/tool", ["/home/x/lib/a"], REPO),
                         (None, None))

    def test_no_in_repo_opens_no_bundle(self):
        trigger = BASE + "/bin/xrdcp"
        self.assertEqual(
            P.build_bundle(trigger, [trigger, "/usr/lib64/libc.so.6"], REPO),
            (None, None))


class RenderAndPathTest(unittest.TestCase):
    def test_render_spec_exact_envelope(self):
        self.assertEqual(P.render_spec(["/a", "/b"]),
                         {"name": "CVMFS_BUNDLE", "version": "1.0.0",
                          "encoding": "UTF-8", "dependencies": ["/a", "/b"]})

    def test_bundle_path_for(self):
        self.assertEqual(P.bundle_path_for("/x/bin/root"), "/x/bin/.cvmfsbundle-root")
        self.assertEqual(P.bundle_path_for("root"), ".cvmfsbundle-root")


class StageBundleTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.d, True)

    def test_writes_json_into_staging_tree(self):
        trigger = BASE + "/bin/xrdcp"
        tar_rel, spec = P.build_bundle(trigger, [trigger, BASE + "/lib/l.so"], REPO)
        dest = P.stage_bundle(self.d, tar_rel, spec)
        self.assertTrue(dest.endswith(
            "lcg/releases/x86_64-el9/xrootd/5.9.1/bin/.cvmfsbundle-xrdcp"))
        with open(dest) as fh:
            self.assertEqual(json.load(fh)["name"], "CVMFS_BUNDLE")

    def test_unsafe_relpath_rejected(self):
        for bad in ("/abs/x", "../escape/x", "a/\x00/b"):
            with self.assertRaises(ValueError):
                P.stage_bundle(self.d, bad, P.render_spec(["/a"]))


if __name__ == "__main__":
    unittest.main()
