# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A package built here leaves no build trees behind (BUILD/<hash>, INSTALLROOT/<hash>)."""

import os
import shutil
import stat
import tempfile
import threading
import unittest
from argparse import Namespace
from unittest.mock import MagicMock, patch

from bits_helpers.build import build_work_dir, remove_build_dirs, runBuildCommand

ARCH = "slc7_x86-64"
HASH = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
SPEC = {"package": "zlib", "version": "v1.3.1", "revision": "1", "hash": HASH,
        "architecture": "", "is_devel_pkg": False, "requires": []}


class RemoveBuildDirsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.build = os.path.join(self.dir, "BUILD", HASH)
        self.install = os.path.join(self.dir, "INSTALLROOT", HASH)
        os.makedirs(os.path.join(self.build, "zlib"))
        os.makedirs(os.path.join(self.install, ARCH, "zlib"))
        os.symlink(HASH, os.path.join(self.dir, "BUILD", "zlib-latest"))

    def tearDown(self):
        for root, dirs, _ in os.walk(self.dir):
            for d in dirs:
                p = os.path.join(root, d)
                if not os.path.islink(p):
                    os.chmod(p, 0o755)
        shutil.rmtree(self.dir)

    def args(self, **kw):
        return Namespace(**dict(dict(autoCleanup=True, aggressiveCleanup=False), **kw))

    def gone(self):
        return not any(os.path.lexists(p) for p in (
            self.build, self.install, os.path.join(self.dir, "BUILD", "zlib-latest")))

    def test_removes_both_trees_and_the_latest_link(self):
        remove_build_dirs(SPEC, self.args(), self.dir, self.dir)
        self.assertTrue(self.gone())

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions: run as a user")
    def test_read_only_directories_go_too(self):
        ro = os.path.join(self.build, "zlib", "pkg", "mod")
        os.makedirs(ro)
        open(os.path.join(ro, "f"), "w").close()
        os.chmod(ro, stat.S_IRUSR | stat.S_IXUSR)
        os.chmod(os.path.dirname(ro), stat.S_IRUSR | stat.S_IXUSR)
        remove_build_dirs(SPEC, self.args(), self.dir, self.dir)
        self.assertTrue(self.gone())

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions: run as a user")
    def test_a_read_only_top_directory_goes_too(self):
        os.chmod(self.build, stat.S_IRUSR | stat.S_IXUSR)
        remove_build_dirs(SPEC, self.args(), self.dir, self.dir)
        self.assertTrue(self.gone())

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions: run as a user")
    def test_a_symlink_in_the_tree_is_not_followed(self):
        outside = os.path.join(self.dir, "outside")
        os.makedirs(outside)
        os.chmod(outside, 0o500)
        os.symlink(outside, os.path.join(self.build, "zlib", "link"))
        os.chmod(os.path.join(self.build, "zlib"), 0o500)   # so the chmod pass runs
        remove_build_dirs(SPEC, self.args(), self.dir, self.dir)
        self.assertTrue(self.gone())
        self.assertEqual(stat.S_IMODE(os.stat(outside).st_mode), 0o500)

    def test_a_latest_link_to_another_build_is_kept(self):
        link = os.path.join(self.dir, "BUILD", "zlib-latest")
        os.unlink(link)
        os.symlink("0" * 40, link)
        remove_build_dirs(SPEC, self.args(), self.dir, self.dir)
        self.assertEqual(os.readlink(link), "0" * 40)
        self.assertFalse(os.path.exists(self.build))

    def test_sources_only_when_asked(self):
        src = os.path.join(self.dir, "SOURCES", "zlib")
        os.makedirs(src)
        remove_build_dirs(SPEC, self.args(aggressiveCleanup=True), self.dir, self.dir)
        self.assertTrue(os.path.isdir(src))
        remove_build_dirs(SPEC, self.args(aggressiveCleanup=True), self.dir, self.dir, sources=True)
        self.assertFalse(os.path.exists(src))

    def test_kept_for_a_devel_package_or_without_auto_cleanup(self):
        remove_build_dirs(dict(SPEC, is_devel_pkg=True), self.args(), self.dir, self.dir)
        remove_build_dirs(SPEC, self.args(autoCleanup=False), self.dir, self.dir)
        self.assertTrue(os.path.isdir(self.build) and os.path.isdir(self.install))


class BuiltPackageCleanupTest(unittest.TestCase):
    """runBuildCommand removes the trees once the package is built and synced,
    and keeps them when the build fails (they are what one debugs)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.dir, "BUILD", HASH, "zlib"))
        os.makedirs(os.path.join(self.dir, "INSTALLROOT", HASH))
        with open(os.path.join(self.dir, "BUILD", HASH, "log"), "w") as log:
            log.write("error: fixture\n")
        os.symlink(HASH, os.path.join(self.dir, "BUILD", "zlib-latest"))

    def tearDown(self):
        shutil.rmtree(self.dir)

    def run_build(self, rc, builders, aggressiveCleanup=False):
        args = Namespace(builders=builders, workDir=self.dir, debug=False, resourceMonitoring=False,
                         architecture=ARCH, autoCleanup=True, aggressiveCleanup=aggressiveCleanup)
        sched = Namespace(buildFailures=[], errors={}, buildFailuresLock=threading.Lock(),
                          log=MagicMock())
        with patch.dict(os.environ, {"BITS_BUILD_WORK_DIR": self.dir, "BITS_DIST_HASH": "0123456789"}), \
             patch("bits_helpers.build.execute",
                   side_effect=lambda cmd, printer: printer("bits:stage:recipe") or rc), \
             patch("bits_helpers.build.detectArch", return_value=ARCH), \
             patch("bits_helpers.build.doFinalSync") as sync, \
             patch("bits_helpers.monitor.note_build"):
            runBuildCommand(sched, "zlib", {"zlib": dict(SPEC)}, args, "true", None,
                            self.dir, self.dir, None)
        return sync

    def test_success_removes_the_trees(self):
        for builders in (1, 2):
            with self.subTest(builders=builders):
                self.tearDown()
                self.setUp()
                sync = self.run_build(0, builders)
                sync.assert_called_once()
                self.assertFalse(os.path.exists(os.path.join(self.dir, "BUILD", HASH)))
                self.assertFalse(os.path.exists(os.path.join(self.dir, "INSTALLROOT", HASH)))
                self.assertFalse(os.path.lexists(os.path.join(self.dir, "BUILD", "zlib-latest")))

    def test_aggressive_cleanup_keeps_the_sources_for_the_checksum_phase(self):
        os.makedirs(os.path.join(self.dir, "SOURCES", "zlib"))
        self.run_build(0, 2, aggressiveCleanup=True)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "SOURCES", "zlib")))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "BUILD", HASH)))

    def test_a_failed_build_keeps_them(self):
        self.run_build(7, 2)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "BUILD", HASH)))
        self.assertTrue(os.path.isdir(os.path.join(self.dir, "INSTALLROOT", HASH)))


class BuildWorkDirTest(unittest.TestCase):
    """$BITS_BUILD_WORK_DIR moves BUILD/ off the work dir, except where the build
    script runs in a container (it is not passed in) or for a devel package."""

    def test_where(self):
        with patch.dict(os.environ, {"BITS_BUILD_WORK_DIR": "/fast"}):
            self.assertEqual(build_work_dir(SPEC, Namespace(workDir="sw", docker=False)), "/fast")
            self.assertEqual(build_work_dir(SPEC, Namespace(workDir="sw", docker=True)), "sw")
            self.assertEqual(build_work_dir(dict(SPEC, is_devel_pkg=True),
                                            Namespace(workDir="sw", docker=False)), "sw")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(build_work_dir(SPEC, Namespace(workDir="sw")), "sw")


if __name__ == "__main__":
    unittest.main()
