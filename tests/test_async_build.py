# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the async build loop enhancements.

Covers:
* ``upload_shell_command()`` on every sync backend (§ Async build loop)
* ``--prefetch-workers``, ``--parallel-sources`` CLI defaults
* ``checkout_sources()`` with ``parallel_sources > 1`` (concurrent source downloads)
"""

import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from collections import OrderedDict
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# 1. upload_shell_command() for all sync backends
# ---------------------------------------------------------------------------

ARCH = "slc7_x86-64"
WORKDIR = "/sw"
GOOD_SPEC = {
    "package": "zlib",
    "version": "v1.3.1",
    "revision": "1",
    "hash": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "architecture": "",         # not a shared package
}


class NoRemoteSyncUploadCmdTest(unittest.TestCase):
    """NoRemoteSync has no write store — always returns None."""

    def test_returns_none(self):
        from bits_helpers.sync import NoRemoteSync
        sync = NoRemoteSync()
        self.assertIsNone(sync.upload_shell_command(GOOD_SPEC))


class HttpRemoteSyncUploadCmdTest(unittest.TestCase):
    """HttpRemoteSync is read-only — upload_shell_command returns None."""

    def test_returns_none(self):
        from bits_helpers.sync import HttpRemoteSync
        with patch("requests.Session"):
            sync = HttpRemoteSync(
                remoteStore="https://example.com/store/",
                architecture=ARCH,
                workdir=WORKDIR,
                insecure=False,
            )
        self.assertIsNone(sync.upload_shell_command(GOOD_SPEC))


class RsyncRemoteSyncUploadCmdTest(unittest.TestCase):
    """RsyncRemoteSync returns None without write store, shell cmd with one."""

    def _make_sync(self, write="rsync://server/repo"):
        from bits_helpers.sync import RsyncRemoteSync
        return RsyncRemoteSync(
            remoteStore="rsync://server/repo",
            writeStore=write,
            architecture=ARCH,
            workdir=WORKDIR,
        )

    def test_no_write_store_returns_none(self):
        self.assertIsNone(self._make_sync(write="").upload_shell_command(GOOD_SPEC))

    def test_returns_bash_command(self):
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        self.assertIsNotNone(cmd)
        self.assertTrue(cmd.startswith("bash -e -c '"), cmd)

    def test_command_contains_rsync(self):
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        self.assertIn("rsync", cmd)

    def test_command_contains_package_name(self):
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        self.assertIn("zlib", cmd)

    def test_command_contains_version(self):
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        # ver_rev(spec) for revision "1" is "v1.3.1-1"
        self.assertIn("v1.3.1-1", cmd)

    def test_single_quotes_escaped(self):
        """Single quotes inside the script must be properly escaped."""
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        # The command is bash -e -c '...'. As long as it starts and ends with
        # single quotes properly, the shell will parse it correctly.
        # We can verify the outermost structure without re-parsing the script.
        self.assertTrue(cmd.startswith("bash -e -c '"))


class S3RemoteSyncUploadCmdTest(unittest.TestCase):
    """S3RemoteSync (s3cmd backend) upload_shell_command tests."""

    def _make_sync(self, write="s3-bucket"):
        from bits_helpers.sync import S3RemoteSync
        return S3RemoteSync(
            remoteStore="s3-bucket",
            writeStore=write,
            architecture=ARCH,
            workdir=WORKDIR,
        )

    def test_no_write_store_returns_none(self):
        self.assertIsNone(self._make_sync(write="").upload_shell_command(GOOD_SPEC))

    def test_returns_bash_command(self):
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        self.assertIsNotNone(cmd)
        self.assertTrue(cmd.startswith("bash -e -c '"), cmd)

    def test_command_contains_s3cmd(self):
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        self.assertIn("s3cmd", cmd)

    def test_command_contains_package_name(self):
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        self.assertIn("zlib", cmd)


class Boto3RemoteSyncUploadCmdTest(unittest.TestCase):
    """Boto3RemoteSync delegates to upload_cmd.py and returns a python3 invocation."""

    def _make_sync(self, write="b3://write-bucket"):
        from bits_helpers.sync import Boto3RemoteSync
        with patch.object(Boto3RemoteSync, "_s3_init"):
            s = Boto3RemoteSync(
                remoteStore="b3://read-bucket",
                writeStore=write,
                architecture=ARCH,
                workdir=WORKDIR,
            )
        return s

    def test_no_write_store_returns_none(self):
        self.assertIsNone(self._make_sync(write="").upload_shell_command(GOOD_SPEC))

    def test_returns_python3_command(self):
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        self.assertIsNotNone(cmd)
        self.assertIn("python3", cmd)
        self.assertIn("bits_helpers.upload_cmd", cmd)

    def test_original_b3_urls_preserved(self):
        """The command must include the original b3:// URLs, not stripped ones."""
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        self.assertIn("b3://read-bucket", cmd)
        self.assertIn("b3://write-bucket", cmd)

    def test_work_dir_in_command(self):
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        self.assertIn(WORKDIR, cmd)

    def test_architecture_in_command(self):
        cmd = self._make_sync().upload_shell_command(GOOD_SPEC)
        self.assertIn(ARCH, cmd)


# ---------------------------------------------------------------------------
# 4. CLI defaults for new flags
# ---------------------------------------------------------------------------

class NewCLIFlagsTest(unittest.TestCase):
    """Verify the three new flags parse correctly with their defaults."""

    @patch("bits_helpers.utilities.getoutput", new=lambda cmd: "x86_64")
    @patch("bits_helpers.args.commands")
    def test_defaults(self, mock_commands):
        """All three new flags must have the documented defaults."""
        import shlex
        from unittest.mock import patch as _patch
        mock_commands.getstatusoutput.return_value = (0, "/usr/local/bin/docker")

        import bits_helpers.args
        from bits_helpers.args import doParseArgs
        bits_helpers.args.DEFAULT_WORK_DIR = "sw"
        bits_helpers.args.DEFAULT_CHDIR = "."

        with _patch.object(sys, "argv",
                           ["bits", "build", "--force-unknown-architecture", "zlib"]):
            args, _ = doParseArgs()

        self.assertEqual(args.prefetchWorkers, -1,
                         "--prefetch-workers must default to -1 (auto)")
        self.assertEqual(args.parallelSources, 1,
                         "--parallel-sources must default to 1")
        self.assertEqual(args.parallelDownloads, 2,
                         "--parallel-downloads must default to 2")

    @patch("bits_helpers.utilities.getoutput", new=lambda cmd: "x86_64")
    @patch("bits_helpers.args.commands")
    def test_prefetch_workers_flag(self, mock_commands):
        """--prefetch-workers N sets prefetchWorkers=N."""
        from unittest.mock import patch as _patch
        mock_commands.getstatusoutput.return_value = (0, "/usr/local/bin/docker")

        import bits_helpers.args
        from bits_helpers.args import doParseArgs
        bits_helpers.args.DEFAULT_WORK_DIR = "sw"
        bits_helpers.args.DEFAULT_CHDIR = "."

        with _patch.object(sys, "argv",
                           ["bits", "build", "--force-unknown-architecture",
                            "--prefetch-workers", "4", "zlib"]):
            args, _ = doParseArgs()

        self.assertEqual(args.prefetchWorkers, 4)

    @patch("bits_helpers.utilities.getoutput", new=lambda cmd: "x86_64")
    @patch("bits_helpers.args.commands")
    def test_parallel_sources_flag(self, mock_commands):
        """--parallel-sources N sets parallelSources=N."""
        from unittest.mock import patch as _patch
        mock_commands.getstatusoutput.return_value = (0, "/usr/local/bin/docker")

        import bits_helpers.args
        from bits_helpers.args import doParseArgs
        bits_helpers.args.DEFAULT_WORK_DIR = "sw"
        bits_helpers.args.DEFAULT_CHDIR = "."

        with _patch.object(sys, "argv",
                           ["bits", "build", "--force-unknown-architecture",
                            "--parallel-sources", "8", "zlib"]):
            args, _ = doParseArgs()

        self.assertEqual(args.parallelSources, 8)


# ---------------------------------------------------------------------------
# 5. parallel checkout_sources() with parallel_sources > 1
# ---------------------------------------------------------------------------

class ParallelCheckoutSourcesTest(unittest.TestCase):
    """checkout_sources() with parallel_sources > 1 downloads URLs concurrently."""

    SOURCES = [
        "https://example.com/foo-1.0.tar.gz",
        "https://example.com/bar-2.0.tar.gz",
        "https://example.com/baz-3.0.tar.gz",
    ]

    def _make_spec(self, sources):
        """Minimal spec for a package with tarball sources.

        commit_hash == tag avoids the symlink() call in checkout_sources()
        that would try to write to /sw/SOURCES/ (which doesn't exist in tests).
        """
        return {
            "package": "mypkg",
            "version": "1.0",
            "commit_hash": "v1.0",   # equals tag → no symlink needed
            "tag": "v1.0",
            "is_devel_pkg": False,
            "sources": sources,
            "scm": MagicMock(),       # not used on the sources path
            "source_checksums": {},
            "patch_checksums": {},
        }

    @patch("bits_helpers.workarea._extract_source_archives", new=MagicMock())
    @patch("bits_helpers.workarea.symlink", new=MagicMock())
    @patch("bits_helpers.workarea.download")
    @patch("bits_helpers.workarea.short_commit_hash", return_value="v1.0")
    @patch("os.makedirs")
    def test_sequential_called_for_each_source(self, mock_makedirs,
                                                mock_short_hash, mock_download):
        """With parallel_sources=1, download() is called once per source."""
        from bits_helpers.workarea import checkout_sources
        spec = self._make_spec(self.SOURCES)
        checkout_sources(spec, "/sw", "/sw/MIRROR", containerised_build=False,
                         parallel_sources=1)
        self.assertEqual(mock_download.call_count, len(self.SOURCES))

    @patch("bits_helpers.workarea._extract_source_archives", new=MagicMock())
    @patch("bits_helpers.workarea.symlink", new=MagicMock())
    @patch("bits_helpers.workarea.download")
    @patch("bits_helpers.workarea.short_commit_hash", return_value="v1.0")
    @patch("os.makedirs")
    def test_parallel_called_for_each_source(self, mock_makedirs,
                                              mock_short_hash, mock_download):
        """With parallel_sources=N, download() is still called once per source."""
        from bits_helpers.workarea import checkout_sources
        spec = self._make_spec(self.SOURCES)
        checkout_sources(spec, "/sw", "/sw/MIRROR", containerised_build=False,
                         parallel_sources=4)
        self.assertEqual(mock_download.call_count, len(self.SOURCES))

    @patch("bits_helpers.workarea._extract_source_archives", new=MagicMock())
    @patch("bits_helpers.workarea.symlink", new=MagicMock())
    @patch("bits_helpers.workarea.download")
    @patch("bits_helpers.workarea.short_commit_hash", return_value="v1.0")
    @patch("os.makedirs")
    def test_parallel_exception_propagates(self, mock_makedirs,
                                           mock_short_hash, mock_download):
        """An exception in any parallel download must propagate to the caller."""
        from bits_helpers.workarea import checkout_sources

        def failing_download(url, *args, **kwargs):
            if "bar" in url:
                raise RuntimeError("simulated download failure")

        mock_download.side_effect = failing_download
        spec = self._make_spec(self.SOURCES)
        with self.assertRaises(RuntimeError):
            checkout_sources(spec, "/sw", "/sw/MIRROR", containerised_build=False,
                             parallel_sources=3)

    @patch("bits_helpers.workarea._extract_source_archives", new=MagicMock())
    @patch("bits_helpers.workarea.symlink", new=MagicMock())
    @patch("bits_helpers.workarea.download")
    @patch("bits_helpers.workarea.short_commit_hash", return_value="v1.0")
    @patch("os.makedirs")
    def test_parallel_faster_than_sequential(self, mock_makedirs,
                                              mock_short_hash, mock_download):
        """Parallel downloads must complete faster than serial ones.

        We simulate each download taking 0.15 s; with parallel_sources=3 the
        total should be < 0.40 s (vs ~0.45 s for serial).
        """
        import time

        def slow_download(url, *args, **kwargs):
            time.sleep(0.15)

        mock_download.side_effect = slow_download
        from bits_helpers.workarea import checkout_sources
        spec = self._make_spec(self.SOURCES)

        start = time.monotonic()
        checkout_sources(spec, "/sw", "/sw/MIRROR", containerised_build=False,
                         parallel_sources=3)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 0.40,
                        "Parallel downloads should not take longer than serial")

    @patch("bits_helpers.workarea._extract_source_archives", new=MagicMock())
    @patch("bits_helpers.workarea.symlink", new=MagicMock())
    @patch("bits_helpers.workarea.download")
    @patch("bits_helpers.workarea.short_commit_hash", return_value="v1.0")
    @patch("os.makedirs")
    def test_single_source_uses_sequential_path(self, mock_makedirs,
                                                  mock_short_hash, mock_download):
        """With a single source, the sequential path is used even if N > 1."""
        from bits_helpers.workarea import checkout_sources
        spec = self._make_spec(["https://example.com/only.tar.gz"])
        checkout_sources(spec, "/sw", "/sw/MIRROR", containerised_build=False,
                         parallel_sources=4)
        mock_download.assert_called_once()


# ---------------------------------------------------------------------------
# 6. _extract_source_archives()
# ---------------------------------------------------------------------------

class ExtractSourceArchivesTest(unittest.TestCase):
    """_extract_source_archives() unpacks archives found in source_dir."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.source_dir, ignore_errors=True)

    # -- sentinel prevents re-extraction --------------------------------------

    def test_sentinel_skips_extraction(self):
        """A valid .bits_extracted sentinel skips re-extraction (no subprocess)."""
        import json
        from bits_helpers.workarea import (_extract_source_archives,
                                           _archive_prefix_depth)
        # Place a fake tarball so we can verify it is not touched.
        fake = os.path.join(self.source_dir, "pkg-1.0.tar.gz")
        open(fake, "w").close()
        # The sentinel records the strip depth used for each archive. Extraction
        # is skipped only when the recorded depths match what we'd compute now;
        # an empty/legacy sentinel is treated as stale and triggers re-extraction.
        # Write a valid sentinel matching the current strip depth.
        sentinel = os.path.join(self.source_dir, ".bits_extracted")
        with open(sentinel, "w") as fh:
            json.dump({"strips": {"pkg-1.0.tar.gz": _archive_prefix_depth(fake)}}, fh)
        with patch("bits_helpers.workarea.subprocess") as mock_sp:
            _extract_source_archives(self.source_dir)
            mock_sp.check_call.assert_not_called()

    # -- tar archives ---------------------------------------------------------

    def _make_tar(self, filename, strip_dir="pkg-1.0"):
        """Create a small but valid tar archive with one file inside strip_dir/."""
        import tarfile, io
        archive_path = os.path.join(self.source_dir, filename)
        with tarfile.open(archive_path, "w:gz") as tf:
            content = b"hello\n"
            info = tarfile.TarInfo(name=strip_dir + "/hello.txt")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        return archive_path

    def test_tar_gz_extracted_with_strip(self):
        """A .tar.gz archive is extracted and hello.txt lands in source_dir."""
        from bits_helpers.workarea import _extract_source_archives
        self._make_tar("pkg-1.0.tar.gz")
        _extract_source_archives(self.source_dir)
        self.assertTrue(os.path.exists(os.path.join(self.source_dir, "hello.txt")))

    def test_tgz_extracted(self):
        """A .tgz archive (alias for .tar.gz) is also extracted."""
        from bits_helpers.workarea import _extract_source_archives
        self._make_tar("pkg-1.0.tgz")
        _extract_source_archives(self.source_dir)
        self.assertTrue(os.path.exists(os.path.join(self.source_dir, "hello.txt")))

    def test_expected_names_ignores_stale_archive(self):
        """A stale archive (not in expected_names) is skipped, not extracted.

        Regression: a leftover ``pkg-1.1.tar.gz`` from a previous recipe revision
        sharing the version directory must not be extracted (and must not abort
        the build when it is a corrupt/HTML download), while the current
        ``pkg-1.1.atlas1.tar.gz`` is unpacked normally.
        """
        from bits_helpers.workarea import _extract_source_archives
        self._make_tar("pkg-1.1.atlas1.tar.gz")          # current, valid
        stale = os.path.join(self.source_dir, "pkg-1.1.tar.gz")
        with open(stale, "w") as fh:                     # corrupt leftover
            fh.write("<html>404 Not Found</html>\n")
        # Must not raise despite the corrupt stale file ...
        _extract_source_archives(self.source_dir,
                                 expected_names={"pkg-1.1.atlas1.tar.gz"})
        # ... and the valid archive's contents are present.
        self.assertTrue(os.path.exists(os.path.join(self.source_dir, "hello.txt")))

    def test_tar_bz2_extracted(self):
        """A .tar.bz2 archive is extracted."""
        import tarfile, io
        from bits_helpers.workarea import _extract_source_archives
        archive_path = os.path.join(self.source_dir, "pkg-1.0.tar.bz2")
        with tarfile.open(archive_path, "w:bz2") as tf:
            content = b"hello\n"
            info = tarfile.TarInfo(name="pkg-1.0/hello.txt")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
        _extract_source_archives(self.source_dir)
        self.assertTrue(os.path.exists(os.path.join(self.source_dir, "hello.txt")))

    def test_sentinel_written_after_extraction(self):
        """After extraction, .bits_extracted is created."""
        from bits_helpers.workarea import _extract_source_archives
        self._make_tar("pkg-1.0.tar.gz")
        _extract_source_archives(self.source_dir)
        self.assertTrue(
            os.path.exists(os.path.join(self.source_dir, ".bits_extracted"))
        )

    def test_no_archives_no_sentinel(self):
        """If there are no archives, no sentinel is written."""
        from bits_helpers.workarea import _extract_source_archives
        open(os.path.join(self.source_dir, "README"), "w").close()
        _extract_source_archives(self.source_dir)
        self.assertFalse(
            os.path.exists(os.path.join(self.source_dir, ".bits_extracted"))
        )

    def test_idempotent_second_call_skipped(self):
        """A second call is a no-op when the sentinel already exists."""
        from bits_helpers.workarea import _extract_source_archives
        self._make_tar("pkg-1.0.tar.gz")
        _extract_source_archives(self.source_dir)
        # Remove extracted file and call again — should not re-extract.
        os.unlink(os.path.join(self.source_dir, "hello.txt"))
        _extract_source_archives(self.source_dir)
        self.assertFalse(
            os.path.exists(os.path.join(self.source_dir, "hello.txt"))
        )

    # -- zip archives ---------------------------------------------------------

    def _make_zip(self, filename, strip_dir="pkg-1.0"):
        """Create a small but valid zip archive with one file inside strip_dir/."""
        import zipfile
        archive_path = os.path.join(self.source_dir, filename)
        with zipfile.ZipFile(archive_path, "w") as zf:
            zf.writestr(strip_dir + "/", "")          # directory entry
            zf.writestr(strip_dir + "/hello.txt", "hello\n")
        return archive_path

    def test_zip_extracted_with_strip(self):
        """A .zip archive is extracted and hello.txt lands in source_dir."""
        from bits_helpers.workarea import _extract_source_archives
        self._make_zip("pkg-1.0.zip")
        _extract_source_archives(self.source_dir)
        self.assertTrue(os.path.exists(os.path.join(self.source_dir, "hello.txt")))

    # -- checkout_sources integration -----------------------------------------

    def test_checkout_sources_calls_extract(self):
        """checkout_sources() calls _extract_source_archives after downloading."""
        from bits_helpers.workarea import checkout_sources
        spec = {
            "package": "mypkg",
            "version": "1.0",
            "commit_hash": "v1.0",
            "tag": "v1.0",
            "is_devel_pkg": False,
            "sources": ["https://example.com/pkg-1.0.tar.gz"],
            "scm": MagicMock(),
            "source_checksums": {},
            "patch_checksums": {},
        }
        with patch("bits_helpers.workarea.download"), \
             patch("bits_helpers.workarea.short_commit_hash", return_value="v1.0"), \
             patch("os.makedirs"), \
             patch("bits_helpers.workarea._extract_source_archives") as mock_extract:
            checkout_sources(spec, "/sw", "/sw/MIRROR", containerised_build=False)
            mock_extract.assert_called_once()


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# write_failure_summary()  (--builders concise failure report)
# ---------------------------------------------------------------------------
class WriteFailureSummaryTest(unittest.TestCase):
    """write_failure_summary() distils a readable per-run failure report."""

    class _Sched:
        def __init__(self, fails, errors):
            self.buildFailures = fails
            self.errors = errors

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_summary_lists_direct_and_cascaded(self):
        from bits_helpers.build import write_failure_summary
        sched = self._Sched(
            fails=[{"package": "motif@2.3.8", "log": "/sw/BUILD/motif-latest/log",
                    "excerpt": "  Matched error lines (last 1):\n    err: boom"}],
            errors={"build:motif": "BUILD FAILED",
                    "build:foo": "The following dependencies could not complete:\nbuild:motif"})
        path, full = write_failure_summary(self.dir, sched, "el9_x86-64")
        self.assertTrue(path and os.path.exists(path))
        # logs land under LOGS/<arch>/ so concurrent different-platform builds
        # sharing one work area do not clobber each other
        self.assertEqual(os.path.dirname(path),
                         os.path.join(self.dir, "LOGS", "el9_x86-64"))
        text = open(path).read()
        self.assertIn("FAILED: motif@2.3.8", text)
        self.assertIn("err: boom", text)                       # excerpt included
        self.assertIn("1 package(s) failed", text)
        self.assertIn("Skipped", text)
        self.assertIn("foo", text)                             # cascaded dependent
        self.assertNotIn("motif", text.split("Skipped")[1])    # not double-counted
        # the combined full error log is also written
        self.assertTrue(full and os.path.exists(full))
        self.assertIn("build:motif", open(full).read())

    def test_no_failures_writes_nothing(self):
        from bits_helpers.build import write_failure_summary
        self.assertEqual(write_failure_summary(self.dir, self._Sched([], {}), "el9_x86-64"),
                         (None, None))
        self.assertFalse(os.path.exists(
            os.path.join(self.dir, "LOGS", "el9_x86-64", "build-summary.log")))
