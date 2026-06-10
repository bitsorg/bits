"""Tests for the sentinel-file helpers in bits_helpers.download.

These helpers coordinate concurrent downloads between the prefetch thread pool
and the main build loop (or Makeflow shell rules):

* ``_sentinel_path(path)`` — ``path + ".downloading"``
* ``_acquire_download(path)`` — atomically claim the download slot (O_CREAT|O_EXCL)
* ``_wait_for_sentinel(path)`` — block until the sentinel disappears

The tests use real temporary directories so that the O_CREAT|O_EXCL file-
creation race condition is exercised with actual filesystem semantics.
"""

import os
import tempfile
import threading
import time
import unittest

from bits_helpers.download import (
    _acquire_download,
    _sentinel_path,
    _wait_for_sentinel,
)


class SentinelPathTest(unittest.TestCase):
    """_sentinel_path() — pure string function."""

    def test_appends_suffix(self):
        self.assertEqual(
            _sentinel_path("/sw/TARS/x86-64/store/ab/abc123/pkg.tar.gz"),
            "/sw/TARS/x86-64/store/ab/abc123/pkg.tar.gz.downloading",
        )

    def test_arbitrary_path(self):
        self.assertEqual(_sentinel_path("foo"), "foo.downloading")

    def test_already_has_suffix(self):
        """The function is dumb — it appends regardless."""
        self.assertEqual(
            _sentinel_path("foo.downloading"),
            "foo.downloading.downloading",
        )


class AcquireDownloadTest(unittest.TestCase):
    """_acquire_download(path) — atomic sentinel creation."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_first_caller_wins(self):
        """The first call succeeds and writes the PID."""
        target = os.path.join(self.tmp, "pkg.tar.gz")
        self.assertTrue(_acquire_download(target))
        sentinel = _sentinel_path(target)
        self.assertTrue(os.path.exists(sentinel))
        with open(sentinel) as fh:
            content = fh.read()
        self.assertEqual(content, str(os.getpid()))

    def test_second_caller_fails(self):
        """A concurrent caller sees the sentinel and returns False."""
        target = os.path.join(self.tmp, "pkg.tar.gz")
        _acquire_download(target)          # first caller wins
        self.assertFalse(_acquire_download(target))  # second caller loses

    def test_sentinel_contains_pid(self):
        """The sentinel file stores the creating process's PID."""
        target = os.path.join(self.tmp, "a.tar.gz")
        _acquire_download(target)
        with open(_sentinel_path(target)) as fh:
            sentinel_content = fh.read()
        self.assertEqual(sentinel_content, str(os.getpid()))

    def test_sentinel_removed_before_retry(self):
        """After the sentinel is deleted, a new caller can claim the slot."""
        target = os.path.join(self.tmp, "b.tar.gz")
        _acquire_download(target)
        os.unlink(_sentinel_path(target))
        # Now the slot is free again.
        self.assertTrue(_acquire_download(target))

    def test_concurrent_acquire_only_one_wins(self):
        """Under true concurrency exactly one thread acquires the download slot."""
        target = os.path.join(self.tmp, "concurrent.tar.gz")
        winners = []
        barrier = threading.Barrier(10)

        def try_acquire():
            barrier.wait()          # all threads start at the same moment
            if _acquire_download(target):
                winners.append(1)

        threads = [threading.Thread(target=try_acquire) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(winners), 1, "Exactly one thread must win the sentinel")


class WaitForSentinelTest(unittest.TestCase):
    """_wait_for_sentinel(path) — blocking poll loop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_sentinel_returns_immediately(self):
        """When no sentinel exists, the function returns without sleeping."""
        target = os.path.join(self.tmp, "pkg.tar.gz")
        start = time.monotonic()
        _wait_for_sentinel(target)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 0.5,
                        "_wait_for_sentinel should return immediately when no sentinel")

    def test_waits_until_sentinel_removed(self):
        """When a sentinel exists, the function blocks until it is removed."""
        target = os.path.join(self.tmp, "slow.tar.gz")
        _acquire_download(target)

        remove_after = 0.4  # seconds

        def remove_sentinel():
            time.sleep(remove_after)
            os.unlink(_sentinel_path(target))

        remover = threading.Thread(target=remove_sentinel, daemon=True)
        remover.start()

        start = time.monotonic()
        _wait_for_sentinel(target)
        elapsed = time.monotonic() - start

        # Must have waited at least half the removal delay.
        self.assertGreaterEqual(elapsed, remove_after / 2,
                                "_wait_for_sentinel returned too early")
        # Must not have waited excessively long after the sentinel disappeared.
        self.assertLess(elapsed, remove_after + 1.0,
                        "_wait_for_sentinel did not return after sentinel was removed")
        remover.join(timeout=1.0)

    def test_wait_poll_interval(self):
        """The function polls every ~0.25 s, so should not return in < 0.1 s
        when a sentinel is present and removed quickly.

        We remove the sentinel immediately after creation; the function should
        still return within a few polling intervals.
        """
        target = os.path.join(self.tmp, "fast.tar.gz")
        _acquire_download(target)
        sentinel = _sentinel_path(target)

        def remove_immediately():
            time.sleep(0.05)
            os.unlink(sentinel)

        t = threading.Thread(target=remove_immediately, daemon=True)
        t.start()
        start = time.monotonic()
        _wait_for_sentinel(target)
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.5)
        t.join(timeout=1.0)

    def test_stale_sentinel_from_dead_pid_returns_immediately(self):
        """A sentinel owned by a PID that no longer exists is stale; the wait
        must not block on it (a crashed prefetch worker can never hang us)."""
        target = os.path.join(self.tmp, "orphan.tar.gz")
        sentinel = _sentinel_path(target)
        # Find a PID that is (almost certainly) not running.
        dead_pid = 2 ** 22
        while True:
            try:
                os.kill(dead_pid, 0)
                dead_pid -= 1            # in use; try another
            except OSError:
                break                    # not in use -> good
        with open(sentinel, "w") as fh:
            fh.write(str(dead_pid))
        start = time.monotonic()
        _wait_for_sentinel(target)
        self.assertLess(time.monotonic() - start, 0.5,
                        "stale (dead-PID) sentinel should not be waited on")

    def test_unreadable_sentinel_is_treated_as_stale(self):
        """An empty/garbage sentinel cannot identify a live download and must
        be treated as stale rather than waited on forever."""
        target = os.path.join(self.tmp, "garbage.tar.gz")
        with open(_sentinel_path(target), "w") as fh:
            fh.write("not-a-pid")
        start = time.monotonic()
        _wait_for_sentinel(target)
        self.assertLess(time.monotonic() - start, 0.5,
                        "unparseable sentinel should be treated as stale")

    def test_timeout_bounds_the_wait(self):
        """Even if a sentinel is held by a live PID and never cleared, the wait
        returns after the timeout instead of blocking forever."""
        target = os.path.join(self.tmp, "forever.tar.gz")
        _acquire_download(target)        # owned by THIS (live) process
        start = time.monotonic()
        _wait_for_sentinel(target, timeout=0.5, poll=0.1)
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertLess(elapsed, 2.0)
        os.unlink(_sentinel_path(target))


class SentinelIntegrationTest(unittest.TestCase):
    """End-to-end: one thread acquires, another waits, file is eventually ready."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_acquire_then_wait(self):
        """Simulate the prefetch → main-loop handoff.

        Thread A acquires the download slot, 'downloads' (sleeps briefly),
        removes the sentinel. Thread B calls _wait_for_sentinel and verifies
        it unblocks after A finishes.
        """
        target = os.path.join(self.tmp, "pkg.tar.gz")
        events = []

        def downloader():
            if _acquire_download(target):
                events.append("acquire")
                time.sleep(0.3)
                # Simulate completed download: write the file, remove sentinel.
                with open(target, "w") as fh:
                    fh.write("data")
                # Record "done" before removing the sentinel so that any
                # thread unblocked by the unlink is guaranteed to see "done"
                # already in the list.
                events.append("done")
                os.unlink(_sentinel_path(target))

        def consumer():
            time.sleep(0.05)        # let downloader start first
            _wait_for_sentinel(target)
            events.append("unblocked")

        t_down = threading.Thread(target=downloader, daemon=True)
        t_cons = threading.Thread(target=consumer, daemon=True)
        t_down.start()
        t_cons.start()
        t_down.join(timeout=2.0)
        t_cons.join(timeout=2.0)

        self.assertEqual(events, ["acquire", "done", "unblocked"],
                         "Consumer must unblock only after downloader finishes")
        self.assertTrue(os.path.exists(target), "Downloaded file must exist")


if __name__ == "__main__":
    unittest.main()
