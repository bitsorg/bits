# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers/preload_cmd — the pure steps of `bits preload`."""

import os
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers import preload_cmd as C

REPO = "/cvmfs/sft.cern.ch"
PKGDIR = REPO + "/lcg/releases/x86_64-el9/xrootd/5.9.1"


class HasPreloadTest(unittest.TestCase):
    def test_detects_forms(self):
        self.assertTrue(C.has_preload("function Preload() {\n cvmfs_preload bin/x\n}"))
        self.assertTrue(C.has_preload("Preload () {\n :\n}"))
        self.assertTrue(C.has_preload("  Preload(){ cvmfs_preload bin/root -b -q; }"))

    def test_absent(self):
        self.assertFalse(C.has_preload("function Build() { true; }"))
        self.assertFalse(C.has_preload(""))
        self.assertFalse(C.has_preload("# mentions Preload() in a comment only? no def"))


class LocatePackageTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        for rel in ("lcg/releases/x86_64-el9/xrootd/5.9.1",
                    "lcg/releases/x86_64-el9/xrootd/5.9.0-2",
                    "lcg/releases/x86_64-el9/Boost/1.90.0-1"):
            os.makedirs(os.path.join(self.root, rel))

    def test_newest_version(self):
        got = C.locate_package(self.root, "xrootd")
        self.assertTrue(got.endswith("xrootd/5.9.1"))

    def test_version_prefix_match(self):
        got = C.locate_package(self.root, "xrootd", "5.9.0")
        self.assertTrue(got.endswith("xrootd/5.9.0-2"))

    def test_absent(self):
        self.assertIsNone(C.locate_package(self.root, "ROOT"))


class PreloadTriggersTest(unittest.TestCase):
    def test_extracts_exe_and_args(self):
        body = ("MODULE_OPTIONS=x\n"
                "function Preload() {\n"
                "  cvmfs_preload bin/root -b -q\n"
                "  cvmfs_preload bin/hadd\n"
                "}\n"
                "function Build(){ cvmfs_preload NOT_A_TRIGGER; }\n")
        self.assertEqual(C.preload_triggers(body),
                         [("bin/root", ["-b", "-q"]), ("bin/hadd", [])])

    def test_semicolon_separated_and_quotes(self):
        body = 'Preload() { cvmfs_preload bin/app "a b" -x; }'
        self.assertEqual(C.preload_triggers(body), [("bin/app", ["a b", "-x"])])

    def test_none_when_no_preload(self):
        self.assertEqual(C.preload_triggers("Build(){ true; }"), [])


class ParseStraceTest(unittest.TestCase):
    def test_success_only_abs_dedup(self):
        text = (
            'open("%s/lib/libXrdCl.so", O_RDONLY) = 3\n'
            'openat(AT_FDCWD, "/usr/lib64/libc.so.6", O_RDONLY) = 4\n'
            'openat(AT_FDCWD, "%s/lib/libXrdCl.so", O_RDONLY) = 5\n'   # dup
            'openat(AT_FDCWD, "relative/path", O_RDONLY) = 6\n'        # relative -> dropped
            # failed loader probes must be dropped, not listed:
            'openat(AT_FDCWD, "%s/lib/tls/x86_64/libXrdCl.so", O_RDONLY) = -1 ENOENT (No such file or directory)\n'
            'openat(AT_FDCWD, "%s/lib/glibc-hwcaps/x86-64-v3/libstdc++.so.6", O_RDONLY) = -1 ENOENT (No such file or directory)\n'
        ) % (PKGDIR, PKGDIR, PKGDIR, PKGDIR)
        self.assertEqual(C.parse_strace_opens(text),
                         [PKGDIR + "/lib/libXrdCl.so", "/usr/lib64/libc.so.6"])


class AssembleAndTarTest(unittest.TestCase):
    def setUp(self):
        self.stage = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.stage, True)

    def test_assemble_then_tar(self):
        traces = [(PKGDIR + "/bin/xrdcp",
                   [PKGDIR + "/bin/xrdcp",                       # trigger, excluded
                    PKGDIR + "/lib/libXrdCl.so",
                    REPO + "/lcg/releases/x86_64-el9/Boost/1.90.0/lib/libboost.so",
                    "/usr/lib64/libc.so.6"])]                    # system, dropped
        staged = C.assemble_bundles(traces, REPO, self.stage)
        self.assertEqual(
            staged,
            ["lcg/releases/x86_64-el9/xrootd/5.9.1/bin/.cvmfsbundle-xrdcp"])
        out = os.path.join(self.stage, "..", "b.tar")
        C.make_tar(self.stage, out)
        with tarfile.open(out) as tf:
            names = tf.getnames()
        self.assertEqual(
            names,
            ["lcg/releases/x86_64-el9/xrootd/5.9.1/bin/.cvmfsbundle-xrdcp"])
        os.remove(out)

    def test_assemble_skips_when_no_repo_opens(self):
        traces = [(PKGDIR + "/bin/xrdcp",
                   [PKGDIR + "/bin/xrdcp", "/usr/lib64/libc.so.6"])]
        self.assertEqual(C.assemble_bundles(traces, REPO, self.stage), [])


if __name__ == "__main__":
    unittest.main()
