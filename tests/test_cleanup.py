# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers/cleanup.py.

Covers:
  - sentinel file creation and mtime touching
  - package inventory listing and mtime-sorting
  - age-based eviction (evicts old, keeps recent)
  - disk-pressure eviction (LRU order, stops at threshold)
  - flock concurrency: packages held by another job are skipped
  - dry-run: reports without deleting
  - graceful handling of missing workDir or empty sentinel tree
  - acquire_build_lock returns a file handle that prevents exclusive lock
"""
import fcntl
import glob
import os
import shutil
import tempfile
import time
import unittest
from argparse import Namespace
from unittest.mock import MagicMock, call, patch

from bits_helpers.cleanup import (
    _leftovers,
    _list_all_sentinels,
    _list_sentinels,
    _pressure_order,
    _try_evict,
    acquire_build_lock,
    mark_in_store,
    doCleanup,
    sentinel_path,
    touch_sentinel,
    _SentinelEntry,
)


ARCH = "x86_64-el9"


class SentinelPathTest(unittest.TestCase):
    """sentinel_path() returns a deterministic, correctly structured path."""

    def test_path_structure(self):
        p = sentinel_path("/data/sw", ARCH, "ROOT", "6.32.0-1")
        self.assertEqual(p, "/data/sw/.packages/x86_64-el9/ROOT/6.32.0-1")

    def test_abspath_normalised(self):
        # Relative paths are made absolute.
        p = sentinel_path("sw", ARCH, "zlib", "1.3-1")
        self.assertTrue(os.path.isabs(p))
        self.assertTrue(p.endswith("/.packages/x86_64-el9/zlib/1.3-1"))


class TouchSentinelTest(unittest.TestCase):
    """touch_sentinel() creates and refreshes sentinel files."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_sentinel_and_parent_dirs(self):
        touch_sentinel(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        spath = sentinel_path(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        self.assertTrue(os.path.isfile(spath), "sentinel file should be created")

    def test_updates_mtime(self):
        touch_sentinel(self.tmpdir, ARCH, "zlib", "1.3-1")
        spath = sentinel_path(self.tmpdir, ARCH, "zlib", "1.3-1")
        mtime_first = os.path.getmtime(spath)
        time.sleep(0.02)
        touch_sentinel(self.tmpdir, ARCH, "zlib", "1.3-1")
        mtime_second = os.path.getmtime(spath)
        self.assertGreater(mtime_second, mtime_first)

    def test_idempotent_no_exception(self):
        for _ in range(3):
            touch_sentinel(self.tmpdir, ARCH, "Geant4", "11.2-1")

    @patch("fcntl.flock", side_effect=BlockingIOError)
    def test_blocked_flock_logs_warning_no_exception(self, _mock_flock):
        # If another process holds an exclusive lock, we log a warning
        # but do NOT raise — the function must be non-fatal.
        with self.assertLogs("bits", level="WARNING"):
            touch_sentinel(self.tmpdir, ARCH, "ROOT", "6.32.0-1")


class ListSentinelsTest(unittest.TestCase):
    """_list_sentinels() enumerates and sorts sentinels correctly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_sentinel(self, package, version, age_days):
        """Create a sentinel with mtime set to `age_days` days ago."""
        touch_sentinel(self.tmpdir, ARCH, package, version)
        spath = sentinel_path(self.tmpdir, ARCH, package, version)
        old_mtime = time.time() - age_days * 86400
        os.utime(spath, (old_mtime, old_mtime))

    def test_empty_sentinel_dir(self):
        result = _list_sentinels(self.tmpdir, ARCH)
        self.assertEqual(result, [])

    def test_missing_packages_dir(self):
        result = _list_sentinels("/nonexistent/path", ARCH)
        self.assertEqual(result, [])

    def test_sorted_oldest_first(self):
        self._make_sentinel("ROOT",   "6.32.0-1", age_days=10)
        self._make_sentinel("Geant4", "11.2-1",   age_days=3)
        self._make_sentinel("zlib",   "1.3-1",    age_days=7)

        entries = _list_sentinels(self.tmpdir, ARCH)
        self.assertEqual(len(entries), 3)
        packages = [os.path.basename(os.path.dirname(e.sentinel_path))
                    for e in entries]
        self.assertEqual(packages, ["ROOT", "zlib", "Geant4"],
                         "entries should be sorted oldest → newest")

    def test_size_bytes_none_when_not_recorded(self):
        # A plain (transitive) touch records no size; _list_sentinels must not
        # walk the tree, so the cached size is None until resolved on demand.
        touch_sentinel(self.tmpdir, ARCH, "orphan", "1.0-1")
        entries = _list_sentinels(self.tmpdir, ARCH)
        self.assertIsNone(entries[0].size_bytes)

    def test_size_recorded_zero_for_missing_pkg_dir(self):
        # Recording a size for a package with no install dir yields 0.
        touch_sentinel(self.tmpdir, ARCH, "orphan", "1.0-1", record_size=True)
        entries = _list_sentinels(self.tmpdir, ARCH)
        self.assertEqual(entries[0].size_bytes, 0)

    def test_pkg_dir_populated(self):
        # Create a fake package directory so the recorded size > 0.
        pkg_dir = os.path.join(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, "libROOT.so"), "wb") as f:
            f.write(b"x" * 1024)
        touch_sentinel(self.tmpdir, ARCH, "ROOT", "6.32.0-1", record_size=True)
        entries = _list_sentinels(self.tmpdir, ARCH)
        self.assertGreater(entries[0].size_bytes, 0)


class TryEvictTest(unittest.TestCase):
    """_try_evict() removes package and sentinel, respects flock."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_entry(self, package="ROOT", version="6.32.0-1", age_days=10):
        touch_sentinel(self.tmpdir, ARCH, package, version)
        spath = sentinel_path(self.tmpdir, ARCH, package, version)
        old_mtime = time.time() - age_days * 86400
        os.utime(spath, (old_mtime, old_mtime))
        pkg_dir = os.path.join(self.tmpdir, ARCH, package, version)
        os.makedirs(pkg_dir, exist_ok=True)
        return _SentinelEntry(
            sentinel_path=spath,
            pkg_dir=pkg_dir,
            mtime=old_mtime,
            size_bytes=0,
        )

    def test_evicts_package_directory(self):
        entry = self._make_entry()
        result = _try_evict(entry, dry_run=False)
        self.assertIsNotNone(result)   # returns freed bytes (may be 0)
        self.assertFalse(os.path.exists(entry.pkg_dir),
                         "package directory should be removed")
        self.assertFalse(os.path.exists(entry.sentinel_path),
                         "sentinel file should be removed")

    def test_dry_run_does_not_delete(self):
        entry = self._make_entry()
        result = _try_evict(entry, dry_run=True)
        self.assertIsNotNone(result)   # returns bytes that would be freed
        # Neither the package dir nor the sentinel should be removed.
        self.assertTrue(os.path.exists(entry.sentinel_path),
                        "sentinel should survive dry-run")

    def test_skips_when_flock_blocked(self):
        """Packages held by an active build job must not be evicted."""
        entry = self._make_entry()
        # Simulate another process holding a shared lock.
        with patch("fcntl.flock",
                   side_effect=[BlockingIOError, None]):  # first call → blocked
            result = _try_evict(entry, dry_run=False)
        self.assertFalse(result, "eviction should be skipped when lock is held")
        self.assertTrue(os.path.exists(entry.sentinel_path),
                        "sentinel must survive when eviction is skipped")

    def test_returns_false_for_missing_sentinel(self):
        entry = self._make_entry()
        os.unlink(entry.sentinel_path)
        result = _try_evict(entry, dry_run=False)
        self.assertFalse(result)

    def test_cleans_up_empty_parent_dirs(self):
        entry = self._make_entry(package="orphan", version="1.0-1")
        _try_evict(entry, dry_run=False)
        pkg_sentinel_dir = os.path.dirname(entry.sentinel_path)
        self.assertFalse(os.path.exists(pkg_sentinel_dir),
                         "empty package sentinel dir should be removed")


class DoCleanupAgeBasedTest(unittest.TestCase):
    """doCleanup() age-based eviction."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_pkg(self, package, version, age_days):
        touch_sentinel(self.tmpdir, ARCH, package, version)
        spath = sentinel_path(self.tmpdir, ARCH, package, version)
        mtime = time.time() - age_days * 86400
        os.utime(spath, (mtime, mtime))
        pkg_dir = os.path.join(self.tmpdir, ARCH, package, version)
        os.makedirs(pkg_dir, exist_ok=True)

    def _args(self, max_age=7.0, min_free_gb=None, dry_run=False, disk_only=False):
        return Namespace(
            workDir=self.tmpdir,
            architecture=ARCH,
            maxAgeDays=max_age,
            minFreeGb=min_free_gb,
            dryRun=dry_run,
            diskPressureOnly=disk_only,
        )

    def test_evicts_stale_keeps_fresh(self):
        self._make_pkg("stale_pkg",  "1.0-1", age_days=10)  # older than 7d
        self._make_pkg("fresh_pkg",  "2.0-1", age_days=3)   # younger than 7d

        doCleanup(self._args(max_age=7.0), parser=None)

        stale_sentinel = sentinel_path(self.tmpdir, ARCH, "stale_pkg", "1.0-1")
        fresh_sentinel = sentinel_path(self.tmpdir, ARCH, "fresh_pkg", "2.0-1")
        self.assertFalse(os.path.exists(stale_sentinel), "stale package should be evicted")
        self.assertTrue(os.path.exists(fresh_sentinel),  "fresh package should be kept")

    def test_dry_run_evicts_nothing(self):
        self._make_pkg("old_pkg", "1.0-1", age_days=30)
        doCleanup(self._args(max_age=7.0, dry_run=True), parser=None)
        spath = sentinel_path(self.tmpdir, ARCH, "old_pkg", "1.0-1")
        self.assertTrue(os.path.exists(spath), "dry-run must not delete anything")

    def test_zero_max_age_skips_age_eviction(self):
        self._make_pkg("very_old", "1.0-1", age_days=365)
        # max_age=0 → no age-based eviction; disk-pressure also disabled.
        doCleanup(self._args(max_age=0.0, min_free_gb=None), parser=None)
        spath = sentinel_path(self.tmpdir, ARCH, "very_old", "1.0-1")
        self.assertTrue(os.path.exists(spath), "max_age=0 should disable age-based eviction")

    def test_missing_workdir_exits_cleanly(self):
        args = self._args()
        args.workDir = "/nonexistent/path/xyzzy"
        # Should not raise; exits via sys.exit(0).
        with self.assertRaises(SystemExit) as cm:
            doCleanup(args, parser=None)
        self.assertEqual(cm.exception.code, 0)

    def test_empty_sentinel_tree_exits_cleanly(self):
        # workDir exists but has no .packages directory.
        with self.assertRaises(SystemExit) as cm:
            doCleanup(self._args(), parser=None)
        self.assertEqual(cm.exception.code, 0)

    def test_locked_package_not_evicted(self):
        """A package whose sentinel flock is held by another job is skipped."""
        self._make_pkg("locked_pkg", "1.0-1", age_days=20)

        original_flock = fcntl.flock

        def flock_that_blocks_exclusive(fd, op):
            if op == fcntl.LOCK_EX | fcntl.LOCK_NB:
                raise BlockingIOError
            return original_flock(fd, op)

        with patch("fcntl.flock", side_effect=flock_that_blocks_exclusive):
            doCleanup(self._args(max_age=7.0), parser=None)

        spath = sentinel_path(self.tmpdir, ARCH, "locked_pkg", "1.0-1")
        self.assertTrue(os.path.exists(spath),
                        "locked package must not be evicted")


class DoCleanupDiskPressureTest(unittest.TestCase):
    """doCleanup() disk-pressure eviction."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_pkg(self, package, version, age_days):
        touch_sentinel(self.tmpdir, ARCH, package, version)
        spath = sentinel_path(self.tmpdir, ARCH, package, version)
        mtime = time.time() - age_days * 86400
        os.utime(spath, (mtime, mtime))
        pkg_dir = os.path.join(self.tmpdir, ARCH, package, version)
        os.makedirs(pkg_dir, exist_ok=True)

    def _args(self, min_free_gb, dry_run=False, max_age=None, disk_only=True):
        return Namespace(
            workDir=self.tmpdir,
            architecture=ARCH,
            maxAgeDays=max_age,
            minFreeGb=min_free_gb,
            dryRun=dry_run,
            diskPressureOnly=disk_only,
        )

    def test_evicts_lru_until_threshold_met(self):
        """Evict oldest packages first until free space exceeds threshold."""
        # Three packages: oldest first.
        self._make_pkg("pkg_old",    "1.0-1", age_days=30)
        self._make_pkg("pkg_medium", "1.0-1", age_days=15)
        self._make_pkg("pkg_new",    "1.0-1", age_days=2)

        # Simulate 40 GiB free (below 50 GiB threshold).
        # After first eviction simulate 60 GiB free (above threshold).
        free_sequence = [
            40 * 1024**3,   # initial check: below threshold → start evicting
            40 * 1024**3,   # check before evicting pkg_old
            60 * 1024**3,   # check after evicting pkg_old → above threshold, stop
        ]
        with patch("bits_helpers.cleanup._free_bytes",
                   side_effect=free_sequence):
            doCleanup(self._args(min_free_gb=50), parser=None)

        # Only the oldest package should be gone.
        self.assertFalse(
            os.path.exists(sentinel_path(self.tmpdir, ARCH, "pkg_old", "1.0-1")),
            "oldest package should be evicted first",
        )
        self.assertTrue(
            os.path.exists(sentinel_path(self.tmpdir, ARCH, "pkg_medium", "1.0-1")),
            "medium package should be kept once threshold is met",
        )
        self.assertTrue(
            os.path.exists(sentinel_path(self.tmpdir, ARCH, "pkg_new", "1.0-1")),
            "newest package should be kept",
        )

    def test_no_eviction_when_above_threshold(self):
        self._make_pkg("big_pkg", "1.0-1", age_days=100)
        # 100 GiB free — well above the 50 GiB threshold.
        with patch("bits_helpers.cleanup._free_bytes",
                   return_value=100 * 1024**3):
            doCleanup(self._args(min_free_gb=50), parser=None)
        spath = sentinel_path(self.tmpdir, ARCH, "big_pkg", "1.0-1")
        self.assertTrue(os.path.exists(spath),
                        "nothing should be evicted when free space is sufficient")

    def test_disk_pressure_only_skips_age_eviction(self):
        """With disk_pressure_only=True, age-based eviction is suppressed."""
        self._make_pkg("ancient", "1.0-1", age_days=365)
        # Well above threshold → no disk-pressure eviction;
        # and disk_only=True → age-based eviction suppressed.
        with patch("bits_helpers.cleanup._free_bytes",
                   return_value=200 * 1024**3):
            doCleanup(self._args(min_free_gb=50, max_age=7.0, disk_only=True),
                      parser=None)
        spath = sentinel_path(self.tmpdir, ARCH, "ancient", "1.0-1")
        self.assertTrue(os.path.exists(spath),
                        "age-based eviction should not run in disk-pressure-only mode")


class AllArchitecturesTest(unittest.TestCase):
    """--all-architectures: one list, oldest day first, biggest first within it,
    nothing used in the last --keep-recent days."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir, True)
        day = 86400
        self.t30 = (time.time() // day - 30) * day + 3600     # one day, 30 days ago
        self._make("x86_64-ubuntu2604-gcc15-dbg", "Rec", time.time() - day, 5000)
        self._make("slc8_x86-64", "small", self.t30, 1)
        self._make("x86_64-el9", "big", self.t30, 900)
        self._make("slc8_x86-64", "mid", time.time() - 10 * day, 50)

    def _make(self, arch, package, mtime, size):
        touch_sentinel(self.tmpdir, arch, package, "1-1")
        spath = sentinel_path(self.tmpdir, arch, package, "1-1")
        with open(spath, "w") as fh:
            fh.write("%d\n" % size)
        os.utime(spath, (mtime, mtime))
        os.makedirs(os.path.join(self.tmpdir, arch, package, "1-1"), exist_ok=True)

    def _left(self):
        return sorted(e.sentinel_path.split("/.packages/")[1] for e in _list_all_sentinels(self.tmpdir))

    def _args(self, keep_recent=2.0, dry_run=False):
        return Namespace(workDir=self.tmpdir, architecture="x86_64-el9", maxAgeDays=None,
                         minFreeGb=50, dryRun=dry_run, diskPressureOnly=True,
                         allArchitectures=True, keepRecentDays=keep_recent)

    def test_the_order(self):
        got = [e.sentinel_path.split("/.packages/")[1]
               for e in _pressure_order(_list_all_sentinels(self.tmpdir), 2.0)]
        self.assertEqual(got, ["x86_64-el9/big/1-1", "slc8_x86-64/small/1-1", "slc8_x86-64/mid/1-1"])

    def test_it_stops_at_the_target(self):
        with patch("bits_helpers.cleanup._free_bytes", side_effect=[0, 0, 60 * 1024**3]):
            doCleanup(self._args(), parser=None)
        self.assertEqual(self._left(), ["slc8_x86-64/mid/1-1", "slc8_x86-64/small/1-1",
                                        "x86_64-ubuntu2604-gcc15-dbg/Rec/1-1"])

    def test_recent_packages_stay_and_it_says_so(self):
        with patch("bits_helpers.cleanup._free_bytes", return_value=0), \
             patch("bits_helpers.cleanup.warning") as warn:
            doCleanup(self._args(), parser=None)
        self.assertEqual(self._left(), ["x86_64-ubuntu2604-gcc15-dbg/Rec/1-1"])
        self.assertIn("--keep-recent", warn.call_args[0][0])

    def test_keep_recent_zero(self):
        with patch("bits_helpers.cleanup._free_bytes", return_value=0):
            doCleanup(self._args(keep_recent=0), parser=None)
        self.assertEqual(self._left(), [])

    def test_a_held_package_stays(self):
        fh = acquire_build_lock(self.tmpdir, "x86_64-el9", "big", "1-1")
        self.addCleanup(fh.close)
        with patch("bits_helpers.cleanup._free_bytes", return_value=0):
            doCleanup(self._args(keep_recent=0), parser=None)
        self.assertEqual(self._left(), ["x86_64-el9/big/1-1"])

    def test_dry_run(self):
        with patch("bits_helpers.cleanup._free_bytes", return_value=0), \
             patch("bits_helpers.cleanup.warning") as warn:
            doCleanup(self._args(dry_run=True), parser=None)
        self.assertEqual(len(self._left()), 4)
        warn.assert_not_called()


class LeftoversTest(unittest.TestCase):
    """--leftovers: a failed build's BUILD/<hash> and INSTALLROOT/<hash>, and jobs'
    tmp/ entries, once not written for --keep-recent days; nothing else."""
    OLD, NEW, BARE = "a" * 40, "b" * 40, "c" * 40

    def setUp(self):
        self.wd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.wd, True)
        self.old = time.time() - 10 * 86400
        self._dir("BUILD/%s/Rec" % self.OLD, self.old, log=self.old)
        self._dir("BUILD/%s/Moore" % self.NEW, self.old, log=time.time())   # running now
        self._dir("INSTALLROOT/%s/x86_64-el9/Rec" % self.OLD, self.old)
        self._dir("INSTALLROOT/%s/x86_64-el9/Moore" % self.NEW, self.old)  # its log is new
        self._dir("INSTALLROOT/%s/x86_64-el9/Gaudi" % self.BARE, self.old)
        self._dir("BUILD/not-a-hash", self.old)
        self._dir("tmp/bits-pub.old", self.old)
        self._dir("tmp/bits-pub.new", time.time())
        self._dir("x86_64-el9/Rec/v1-1", self.old)                          # installed
        os.symlink(self.OLD, os.path.join(self.wd, "BUILD", "Rec-latest"))
        os.symlink(self.NEW, os.path.join(self.wd, "BUILD", "Moore-latest"))

    def _dir(self, rel, mtime, log=None):
        path = os.path.join(self.wd, rel)
        os.makedirs(path)
        if log is not None:
            top = os.path.join(self.wd, *rel.split("/")[:2])
            with open(os.path.join(top, "log"), "w") as fh:
                fh.write("x")
            os.utime(os.path.join(top, "log"), (log, log))
        while path != self.wd:
            os.utime(path, (mtime, mtime))
            path = os.path.dirname(path)

    def _run(self, dry_run=False, keep_recent=2.0):
        args = Namespace(workDir=self.wd, architecture="x86_64-el9", maxAgeDays=None, minFreeGb=None,
                         dryRun=dry_run, diskPressureOnly=True, allArchitectures=True,
                         keepRecentDays=keep_recent, leftovers=True)
        with self.assertRaises(SystemExit):          # no package sentinels: it ends there
            doCleanup(args, parser=None)

    def _there(self):
        return sorted("%s/%s" % (d, n)
                      for d in ("BUILD", "INSTALLROOT", "tmp")
                      for n in os.listdir(os.path.join(self.wd, d)))

    def test_what_goes_and_what_stays(self):
        self._run()
        self.assertEqual(self._there(), [
            "BUILD/Moore-latest", "BUILD/%s" % self.NEW, "BUILD/not-a-hash",
            "INSTALLROOT/%s" % self.NEW, "tmp/bits-pub.new"])
        self.assertTrue(os.path.isdir(os.path.join(self.wd, "x86_64-el9", "Rec", "v1-1")))

    def test_dry_run_removes_nothing(self):
        before = self._there()
        self._run(dry_run=True)
        self.assertEqual(self._there(), before)

    def test_keep_recent_longer_than_their_age(self):
        before = self._there()
        self._run(keep_recent=30)
        self.assertEqual(self._there(), before)

    def test_never_younger_than_a_day(self):
        self._dir("INSTALLROOT/%s/x" % ("e" * 40), time.time() - 3600)
        self._run(keep_recent=0)
        self.assertIn("INSTALLROOT/%s" % ("e" * 40), self._there())

    def test_a_build_started_since_is_left(self):
        real = _leftovers
        def stale(wd, days, now=None, **kw):
            got = real(wd, days, now, **kw)
            os.utime(os.path.join(self.wd, "INSTALLROOT", self.BARE), None)   # a build began
            return got
        with patch("bits_helpers.cleanup._leftovers", side_effect=stale):
            self._run()
        self.assertIn("INSTALLROOT/%s" % self.BARE, self._there())             # given back

    def test_a_removal_cut_short_is_finished(self):
        self._dir("BUILD/%s.bits-prune-123/x" % ("d" * 40), time.time())
        self._run(keep_recent=30)
        self.assertNotIn("BUILD/%s.bits-prune-123" % ("d" * 40), self._there())


class TarballsTest(unittest.TestCase):
    """A package's store tarball goes with it; one of no installed package is a leftover."""
    ARCH, H1, H2, H3 = "x86_64-el9", "1" * 40, "2" * 40, "3" * 40

    def setUp(self):
        self.wd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.wd, True)
        self.old = time.time() - 10 * 86400
        self._package("ROOT", self.H1)
        self._package("Gaudi", self.H2)
        self._tarball("ROOT", self.H1)
        self._tarball("Gaudi", self.H2)
        self._tarball("Moore", self.H3)                  # its package is not installed

    def _package(self, pkg, h):
        touch_sentinel(self.wd, self.ARCH, pkg, "1-1")
        spath = sentinel_path(self.wd, self.ARCH, pkg, "1-1")
        os.utime(spath, (self.old, self.old))
        d = os.path.join(self.wd, self.ARCH, pkg, "1-1")
        os.makedirs(d)
        with open(os.path.join(d, ".build-hash"), "w") as fh:
            fh.write(h + "\n")

    def _tarball(self, pkg, h, mtime=None, stored=True):
        d = os.path.join(self.wd, "TARS", self.ARCH, "store", h[:2], h)
        os.makedirs(d)
        if stored:
            mark_in_store(self.wd, self.ARCH, h)
        name = "%s-1-1.%s.tar.gz" % (pkg, self.ARCH)
        with open(os.path.join(d, name), "w") as fh:
            fh.write("x" * 100)
        links = os.path.join(self.wd, "TARS", self.ARCH, pkg)
        os.makedirs(links)
        os.symlink("../../%s/store/%s/%s/%s" % (self.ARCH, h[:2], h, name), os.path.join(links, name))
        self._dist(pkg, pkg, h)
        for p in [os.path.join(d, n) for n in os.listdir(d)] + [d]:
            os.utime(p, (mtime or self.old,) * 2)

    def _dist(self, pkg, dep, h):
        """TARS/<arch>/dist/<pkg>/<pkg>-1-1/ links to its own and its dependencies' tarballs."""
        d = os.path.join(self.wd, "TARS", self.ARCH, "dist", pkg, "%s-1-1" % pkg)
        os.makedirs(d, exist_ok=True)
        name = "%s-1-1.%s.tar.gz" % (dep, self.ARCH)
        os.symlink("../../../store/%s/%s/%s" % (h[:2], h, name), os.path.join(d, name))

    def _dist_links(self):
        top = os.path.join(self.wd, "TARS", self.ARCH, "dist")
        return sorted(os.path.relpath(os.path.join(r, n), top)
                      for r, ds, fs in os.walk(top) for n in fs + ds
                      if os.path.islink(os.path.join(r, n)))

    def _stored(self):
        return sorted(os.path.basename(d) for d in
                      glob.glob(os.path.join(self.wd, "TARS", "*", "store", "*", "*")))

    def _links(self):
        return sorted(os.path.relpath(p, os.path.join(self.wd, "TARS")) for p in
                      glob.glob(os.path.join(self.wd, "TARS", self.ARCH, "*", "*.tar.gz")))

    def _args(self, **kw):
        a = dict(workDir=self.wd, architecture=self.ARCH, maxAgeDays=None, minFreeGb=None, dryRun=False,
                 diskPressureOnly=True, allArchitectures=True, keepRecentDays=2.0, leftovers=False)
        a.update(kw)
        return Namespace(**a)

    def test_an_evicted_package_takes_its_tarball(self):
        self._dist("ROOT", "Gaudi", self.H2)                         # ROOT's dist names Gaudi
        with patch("bits_helpers.cleanup._free_bytes", side_effect=[0, 0, 50 * 1024**3]):
            doCleanup(self._args(minFreeGb=10), parser=None)          # Gaudi goes (first by name)
        self.assertEqual(len(_list_all_sentinels(self.wd)), 1)
        self.assertEqual(self._stored(), [self.H1, self.H3])
        self.assertEqual(self._links(), ["x86_64-el9/Moore/Moore-1-1.x86_64-el9.tar.gz",
                                         "x86_64-el9/ROOT/ROOT-1-1.x86_64-el9.tar.gz"])
        self.assertEqual(self._dist_links(), ["Moore/Moore-1-1/Moore-1-1.x86_64-el9.tar.gz",
                                              "ROOT/ROOT-1-1/ROOT-1-1.x86_64-el9.tar.gz"])
        self.assertFalse(os.path.exists(os.path.join(self.wd, "TARS", self.ARCH, "dist", "Gaudi")))

    def test_an_empty_directory_a_build_just_made_stays(self):
        fresh = os.path.join(self.wd, "TARS", self.ARCH, "dist", "Allen", "Allen-1-1")
        os.makedirs(fresh)
        doCleanup(self._args(leftovers=True), parser=None)
        self.assertTrue(os.path.isdir(fresh))

    def test_a_tarball_of_no_installed_package_is_a_leftover(self):
        self._tarball("Rec", "4" * 40, mtime=time.time())             # recent: stays
        doCleanup(self._args(leftovers=True), parser=None)
        self.assertEqual(self._stored(), [self.H1, self.H2, "4" * 40])
        self.assertNotIn("x86_64-el9/Moore/Moore-1-1.x86_64-el9.tar.gz", self._links())
        self.assertFalse(os.path.exists(os.path.join(self.wd, "TARS", self.ARCH, "dist", "Moore")))
        self.assertTrue(os.path.isdir(os.path.join(self.wd, "TARS", self.ARCH, "store")))

    def test_a_tarball_only_here_stays(self):
        self._package("Rec", "5" * 40)
        self._tarball("Rec", "5" * 40, stored=False)
        self._tarball("Allen", "6" * 40, stored=False)               # not installed either
        with patch("bits_helpers.cleanup._free_bytes", return_value=0):
            doCleanup(self._args(minFreeGb=10, keepRecentDays=0, leftovers=True), parser=None)
        self.assertEqual(_list_all_sentinels(self.wd), [])           # every package evicted
        self.assertEqual(self._stored(), ["5" * 40, "6" * 40])        # not their unstored tarballs

    def test_assume_stored(self):
        self._tarball("Allen", "6" * 40, stored=False)
        doCleanup(self._args(leftovers=True, assumeStored=True), parser=None)
        self.assertNotIn("6" * 40, self._stored())

    def test_dry_run_counts_the_tarball(self):
        entry = [e for e in _list_all_sentinels(self.wd) if "ROOT" in e.sentinel_path][0]
        self.assertGreaterEqual(_try_evict(entry, dry_run=True), 100)
        self.assertIn(self.H1, self._stored())


class MarkInStoreTest(unittest.TestCase):
    """doFinalSync marks a tarball the write store has, so prune may drop the local copy."""

    def _sync(self, write_store):
        from bits_helpers import build
        wd = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, wd, True)
        h = "ab" + "c" * 38
        d = os.path.join(wd, "TARS", ARCH, "store", h[:2], h)
        os.makedirs(d)
        spec = {"package": "A", "version": "1", "revision": "1", "hash": h, "requires": [],
                "full_requires": [], "full_runtime_requires": []}
        args = Namespace(workDir=wd, architecture=ARCH, manifest=None, aggressiveCleanup=False,
                         storeIntegrity=False)
        helper = MagicMock(writeStore=write_store)
        with patch.object(build, "createDistLinks"), patch.object(build, "package_info"), \
             patch("bits_helpers.cleanup.touch_sentinel"):
            build.doFinalSync(spec, {"A": spec}, args, helper)
        helper.upload_symlinks_and_tarball.assert_called_once()
        return os.path.exists(os.path.join(d, ".in-store"))

    def test_with_a_write_store(self):
        self.assertTrue(self._sync("b3://store"))

    def test_without_one(self):
        self.assertFalse(self._sync(""))


class AcquireBuildLockTest(unittest.TestCase):
    """acquire_build_lock() holds a shared flock and prevents exclusive eviction."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_returns_open_file_handle(self):
        fh = acquire_build_lock(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        try:
            self.assertFalse(fh.closed)
            # Sentinel file should exist after locking.
            spath = sentinel_path(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
            self.assertTrue(os.path.isfile(spath))
        finally:
            fh.close()

    def test_shared_lock_blocks_exclusive(self):
        """While build lock is held, an exclusive (eviction) lock must fail."""
        fh = acquire_build_lock(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        try:
            spath = sentinel_path(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
            with open(spath, "r+") as evict_fh:
                with self.assertRaises((BlockingIOError, OSError)):
                    fcntl.flock(evict_fh,
                                fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            fh.close()

    def test_lock_released_on_close(self):
        """After the build lock file handle is closed, exclusive lock succeeds."""
        fh = acquire_build_lock(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        fh.close()
        spath = sentinel_path(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        with open(spath, "r+") as evict_fh:
            # Must not raise after the shared lock is released.
            fcntl.flock(evict_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(evict_fh, fcntl.LOCK_UN)

    def test_refreshes_mtime(self):
        """acquire_build_lock touches the sentinel mtime."""
        # Create a stale sentinel first.
        touch_sentinel(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        spath = sentinel_path(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        old_t = time.time() - 100
        os.utime(spath, (old_t, old_t))

        time.sleep(0.02)
        fh = acquire_build_lock(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        fh.close()

        self.assertGreater(os.path.getmtime(spath), old_t,
                           "acquire_build_lock should refresh the sentinel mtime")



class PublishRecordRefreshTest(unittest.TestCase):
    """mark_published_from reads the publish record fetched now, not a copy a
    previous run left in MANIFESTS/retention."""

    def test_the_record_is_fetched_afresh(self):
        import json
        from bits_helpers.cleanup import mark_published_from, published_marker_path
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        old = os.path.join(work, "MANIFESTS", "retention")
        os.makedirs(old)
        with open(os.path.join(old, "cvmfs-status.json"), "w") as fh:
            json.dump({"packages": []}, fh)
        rec = {"packages": [{"package": "zlib", "version": "1.3", "platform": "x86_64-el9",
                             "pkg_id": "zlib-1.3-2-x86_64-el9"}]}

        def fake(source, destDir, work_dir, dest_filename=None):
            with open(os.path.join(destDir, dest_filename), "w") as fh:
                json.dump(rec, fh)
            return True
        with patch("bits_helpers.download.downloadUrllib2", side_effect=fake):
            mark_published_from(work, "https://cvmfs.example/cvmfs-status.json")
        self.assertTrue(os.path.exists(published_marker_path(work, "x86_64-el9", "zlib", "1.3-2")))


if __name__ == "__main__":
    unittest.main()
