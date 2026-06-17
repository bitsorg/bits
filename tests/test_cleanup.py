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
import os
import shutil
import tempfile
import time
import unittest
from argparse import Namespace
from unittest.mock import MagicMock, call, patch

from bits_helpers.cleanup import (
    _list_sentinels,
    _try_evict,
    acquire_build_lock,
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

    def test_size_bytes_zero_for_missing_pkg_dir(self):
        touch_sentinel(self.tmpdir, ARCH, "orphan", "1.0-1")
        entries = _list_sentinels(self.tmpdir, ARCH)
        self.assertEqual(entries[0].size_bytes, 0)

    def test_pkg_dir_populated(self):
        # Create a fake package directory so size_bytes > 0.
        pkg_dir = os.path.join(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
        os.makedirs(pkg_dir)
        with open(os.path.join(pkg_dir, "libROOT.so"), "wb") as f:
            f.write(b"x" * 1024)
        touch_sentinel(self.tmpdir, ARCH, "ROOT", "6.32.0-1")
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
        self.assertTrue(result)
        self.assertFalse(os.path.exists(entry.pkg_dir),
                         "package directory should be removed")
        self.assertFalse(os.path.exists(entry.sentinel_path),
                         "sentinel file should be removed")

    def test_dry_run_does_not_delete(self):
        entry = self._make_entry()
        result = _try_evict(entry, dry_run=True)
        self.assertTrue(result)
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


if __name__ == "__main__":
    unittest.main()
