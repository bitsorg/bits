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


class RecipeTestsTest(unittest.TestCase):
    def test_mapping_and_string_forms(self):
        spec = {"package": "xrootd", "preload": [
            {"exe": "bin/xrdcp", "args": ["--version"]},
            {"exe": "bin/xrdfs", "args": "--help -v"},   # string args -> shlex
            "bin/xrdmapc plain",                          # bare string entry
            {"args": ["--x"]},                            # no exe -> skipped
        ]}
        self.assertEqual(C.recipe_tests(spec), [
            ("bin/xrdcp", ["--version"]),
            ("bin/xrdfs", ["--help", "-v"]),
            ("bin/xrdmapc", ["plain"]),
        ])

    def test_absent(self):
        self.assertEqual(C.recipe_tests({"package": "x"}), [])
        self.assertEqual(C.recipe_tests(None), [])


class LoadConfigTest(unittest.TestCase):
    def test_bare_list_and_mapping_override(self):
        cfg = C.load_config(
            "arch: x86_64-el9-gcc14-opt\n"
            "docker: true\n"
            "packages:\n"
            "  - xrootd\n"                       # bare -> defer to recipe
            "  - ROOT:\n"
            "      versions: ['6.38.*']\n"
            "      tests:\n"
            "        - { exe: bin/root, args: [-b, -q] }\n")
        self.assertEqual(cfg["arch"], ["x86_64-el9-gcc14-opt"])   # scalar -> list
        self.assertTrue(cfg["docker"])
        self.assertFalse(cfg["update"])
        self.assertEqual(cfg["packages"]["xrootd"], {"tests": [], "versions": []})
        self.assertEqual(cfg["packages"]["ROOT"]["tests"], [("bin/root", ["-b", "-q"])])
        self.assertEqual(cfg["packages"]["ROOT"]["versions"], ["6.38.*"])

    def test_arch_omitted_is_none_for_discovery(self):
        self.assertIsNone(C.load_config("docker: false\n")["arch"])


class ResolveAndSkipTest(unittest.TestCase):
    def test_config_tests_override_else_recipe(self):
        recipe = {"preload": [{"exe": "bin/xrdcp", "args": ["--version"]}]}
        self.assertEqual(C.resolve_tests({"tests": [("bin/x", [])]}, recipe),
                         [("bin/x", [])])                    # config wins
        self.assertEqual(C.resolve_tests({"tests": []}, recipe),
                         [("bin/xrdcp", ["--version"])])     # falls back to recipe
        self.assertEqual(C.resolve_tests(None, recipe),
                         [("bin/xrdcp", ["--version"])])

    def test_bundle_exists(self):
        d = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, d, True)
        self.assertFalse(C.bundle_exists(d, "bin/xrdcp"))
        os.makedirs(os.path.join(d, "bin"))
        open(os.path.join(d, "bin", ".cvmfsbundle-xrdcp"), "w").close()
        self.assertTrue(C.bundle_exists(d, "bin/xrdcp"))


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


class DiscoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.root, True)
        for rel in ("x86_64-el9-gcc14-opt/Packages/xrootd/5.9.1-1",
                    "x86_64-el9-gcc14-opt/Packages/xrootd/5.9.0-2",
                    "x86_64-el9-gcc14-opt/Packages/ROOT/6.38.00-1",
                    "aarch64-el9-gcc14-opt/Packages/xrootd/5.9.1-1"):
            os.makedirs(os.path.join(self.root, rel))

    def test_discover_archs(self):
        self.assertEqual(sorted(C.discover_archs(self.root)),
                         ["aarch64-el9-gcc14-opt", "x86_64-el9-gcc14-opt"])

    def test_package_versions_all_and_filtered(self):
        a = "x86_64-el9-gcc14-opt"
        self.assertEqual(sorted(C.package_versions(self.root, a, "xrootd")),
                         ["5.9.0-2", "5.9.1-1"])
        # bare version glob matches its <version>-<rev> dir
        self.assertEqual(C.package_versions(self.root, a, "xrootd", ["5.9.1"]),
                         ["5.9.1-1"])
        self.assertEqual(C.package_versions(self.root, a, "ROOT", ["6.38.*"]),
                         ["6.38.00-1"])
        self.assertEqual(C.package_versions(self.root, a, "xrootd", ["9.9.9"]), [])

    def test_package_dir(self):
        self.assertEqual(
            C.package_dir(self.root, "x86_64-el9-gcc14-opt", "xrootd", "5.9.1-1"),
            os.path.join(self.root, "x86_64-el9-gcc14-opt/Packages/xrootd/5.9.1-1"))


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
