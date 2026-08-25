# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for source archive caching in the remote store.

Covers the Part 1 feature described in REFERENCE.md §25:

* ``_source_remote_path()`` — canonical remote path helper
* ``NoRemoteSync``, ``HttpRemoteSync``, ``RsyncRemoteSync``,
  ``S3RemoteSync``, ``Boto3RemoteSync`` —
  ``fetch_source()`` / ``upload_source()`` methods
* ``download()`` — ``sync_helper`` integration (local-cache hit,
  remote-store hit, upstream download with subsequent archive upload)
"""

import os
import os.path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bits_helpers import sync

try:
    import botocore  # noqa: F401
    _HAVE_BOTOCORE = True
except ImportError:
    _HAVE_BOTOCORE = False
from bits_helpers.sync import _source_remote_path
from bits_helpers.download import download, getUrlChecksum, fixUrl


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

TEST_URL = "https://example.com/releases/libfoo-1.2.tar.gz"
TEST_FILENAME = "libfoo-1.2.tar.gz"
TEST_URL_HASH = getUrlChecksum(TEST_URL)
TEST_REMOTE_PATH = _source_remote_path(TEST_URL_HASH, TEST_FILENAME)

_FAKE_CONTENT = b"fake tarball content"


def _write_fake_file(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(_FAKE_CONTENT)


# ---------------------------------------------------------------------------
# _source_remote_path()
# ---------------------------------------------------------------------------

class SourceRemotePathTest(unittest.TestCase):
    """Unit tests for the _source_remote_path() helper."""

    def test_structure(self):
        h = "abcdef1234567890abcdef1234567890abcdef12"
        fname = "pkg-1.0.tar.gz"
        path = _source_remote_path(h, fname)
        self.assertEqual(
            path,
            "SOURCES/cache/ab/abcdef1234567890abcdef1234567890abcdef12/pkg-1.0.tar.gz",
        )

    def test_prefix_sharding(self):
        """First two chars of the hash are used as a directory shard."""
        h = "deadbeef" * 5
        path = _source_remote_path(h, "f.tar.gz")
        self.assertTrue(path.startswith("SOURCES/cache/de/"), path)

    def test_mirrors_local_cache_structure(self):
        """Remote path segments must match the local SOURCES/cache layout."""
        h = "1234" * 10
        fname = "data.tar.xz"
        parts = _source_remote_path(h, fname).split("/")
        self.assertEqual(parts[0], "SOURCES")
        self.assertEqual(parts[1], "cache")
        self.assertEqual(parts[2], h[:2])
        self.assertEqual(parts[3], h)
        self.assertEqual(parts[4], fname)

    def test_different_hashes_give_different_paths(self):
        h1 = "aabb" + "0" * 36
        h2 = "ccdd" + "0" * 36
        self.assertNotEqual(
            _source_remote_path(h1, "f.tar.gz"),
            _source_remote_path(h2, "f.tar.gz"),
        )


# ---------------------------------------------------------------------------
# NoRemoteSync
# ---------------------------------------------------------------------------

class NoRemoteSyncSourceTest(unittest.TestCase):
    """fetch_source / upload_source on NoRemoteSync are silent no-ops."""

    def setUp(self):
        self.syncer = sync.NoRemoteSync()

    def test_fetch_returns_false(self):
        self.assertFalse(
            self.syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, "/tmp/dest"),
        )

    def test_upload_is_noop(self):
        # Must not raise and must not call any external command.
        self.syncer.upload_source("/tmp/libfoo-1.2.tar.gz", TEST_URL_HASH, TEST_FILENAME)


# ---------------------------------------------------------------------------
# HttpRemoteSync
# ---------------------------------------------------------------------------

class HttpRemoteSyncSourceTest(unittest.TestCase):
    """HttpRemoteSync.fetch_source delegates to getRetry; upload is a no-op."""

    _REMOTE = "https://store.example.com/bits"

    def _make_syncer(self):
        s = sync.HttpRemoteSync(
            remoteStore=self._REMOTE,
            architecture="slc9_x86-64",
            workdir="/sw",
            insecure=False,
        )
        s.httpBackoff = 0  # don't sleep in tests
        return s

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    def test_fetch_success(self, _exists, _makedirs):
        syncer = self._make_syncer()
        syncer.getRetry = MagicMock(return_value=True)

        dest_dir = "/sw/SOURCES/cache/ab/abc123"
        result = syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, dest_dir)

        syncer.getRetry.assert_called_once()
        url_used = syncer.getRetry.call_args[0][0]
        # URL must contain both the shard prefix and the full hash
        self.assertIn(TEST_URL_HASH[:2], url_used)
        self.assertIn(TEST_URL_HASH, url_used)
        self.assertIn(TEST_FILENAME, url_used)
        self.assertTrue(result)

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=False)
    def test_fetch_miss(self, _exists, _makedirs):
        syncer = self._make_syncer()
        syncer.getRetry = MagicMock(return_value=None)

        result = syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, "/sw/SOURCES/cache/ab/abc")
        self.assertFalse(result)

    def test_upload_is_noop(self):
        """HTTP backend is read-only — upload_source must never call getRetry."""
        syncer = self._make_syncer()
        syncer.getRetry = MagicMock()
        syncer.upload_source("/tmp/libfoo-1.2.tar.gz", TEST_URL_HASH, TEST_FILENAME)
        syncer.getRetry.assert_not_called()

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=False)
    def test_fetch_cleans_up_partial_file_on_failure(self, mock_exists, _makedirs):
        """A failed download must not leave a zero/partial file behind."""
        syncer = self._make_syncer()
        syncer.getRetry = MagicMock(return_value=False)

        with patch("os.unlink") as mock_unlink:
            result = syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, "/sw/SOURCES/cache/ab/abc")
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# RsyncRemoteSync
# ---------------------------------------------------------------------------

class RsyncRemoteSyncSourceTest(unittest.TestCase):
    """RsyncRemoteSync fetch/upload invoke execute() with rsync commands."""

    def _make_syncer(self, write_store="rsync://host/store"):
        return sync.RsyncRemoteSync(
            remoteStore="rsync://host/store",
            writeStore=write_store,
            architecture="slc9_x86-64",
            workdir="/sw",
        )

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch("bits_helpers.sync.execute", return_value=0)
    def test_fetch_success(self, mock_exec, _exists, _makedirs):
        syncer = self._make_syncer()
        result = syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, "/sw/SOURCES/cache/ab/abc")

        self.assertTrue(result)
        cmd = mock_exec.call_args[0][0]
        self.assertIn("rsync", cmd)
        self.assertIn(TEST_URL_HASH[:2], cmd)
        self.assertIn(TEST_URL_HASH, cmd)
        self.assertIn(TEST_FILENAME, cmd)

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=False)
    @patch("bits_helpers.sync.execute", return_value=1)   # rsync exit code 1 = failure
    def test_fetch_miss(self, _exec, _exists, _makedirs):
        syncer = self._make_syncer()
        result = syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, "/sw/SOURCES/cache/ab/abc")
        self.assertFalse(result)

    @patch("bits_helpers.sync.execute", return_value=0)
    def test_upload_calls_rsync(self, mock_exec):
        syncer = self._make_syncer()
        syncer.upload_source("/tmp/libfoo-1.2.tar.gz", TEST_URL_HASH, TEST_FILENAME)

        mock_exec.assert_called_once()
        cmd = mock_exec.call_args[0][0]
        self.assertIn("rsync", cmd)
        self.assertIn(TEST_URL_HASH[:2], cmd)
        self.assertIn(TEST_URL_HASH, cmd)
        self.assertIn("/tmp/libfoo-1.2.tar.gz", cmd)

    @patch("bits_helpers.sync.execute")
    def test_upload_skipped_with_no_write_store(self, mock_exec):
        syncer = self._make_syncer(write_store="")
        syncer.upload_source("/tmp/libfoo.tar.gz", TEST_URL_HASH, TEST_FILENAME)
        mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# S3RemoteSync (s3cmd)
# ---------------------------------------------------------------------------

class S3RemoteSyncSourceTest(unittest.TestCase):
    """S3RemoteSync fetch/upload invoke execute() with s3cmd commands."""

    def _make_syncer(self, write_store="s3://bucket"):
        return sync.S3RemoteSync(
            remoteStore="s3://bucket",
            writeStore=write_store,
            architecture="slc9_x86-64",
            workdir="/sw",
        )

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=True)
    @patch("bits_helpers.sync.execute", return_value=0)
    def test_fetch_success(self, mock_exec, _exists, _makedirs):
        syncer = self._make_syncer()
        result = syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, "/sw/SOURCES/cache/ab/abc")

        self.assertTrue(result)
        cmd = mock_exec.call_args[0][0]
        self.assertIn("s3cmd", cmd)
        self.assertIn(TEST_URL_HASH[:2], cmd)
        self.assertIn(TEST_URL_HASH, cmd)
        self.assertIn(TEST_FILENAME, cmd)

    @patch("os.makedirs")
    @patch("os.path.exists", return_value=False)
    @patch("bits_helpers.sync.execute", return_value=1)
    def test_fetch_miss(self, _exec, _exists, _makedirs):
        syncer = self._make_syncer()
        result = syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, "/sw/SOURCES/cache/ab/abc")
        self.assertFalse(result)

    @patch("bits_helpers.sync.execute", return_value=0)
    def test_upload_calls_s3cmd(self, mock_exec):
        syncer = self._make_syncer()
        syncer.upload_source("/tmp/libfoo-1.2.tar.gz", TEST_URL_HASH, TEST_FILENAME)

        mock_exec.assert_called_once()
        cmd = mock_exec.call_args[0][0]
        self.assertIn("s3cmd", cmd)
        self.assertIn("put", cmd)
        self.assertIn(TEST_URL_HASH[:2], cmd)
        self.assertIn(TEST_URL_HASH, cmd)

    @patch("bits_helpers.sync.execute")
    def test_upload_skipped_with_no_write_store(self, mock_exec):
        syncer = self._make_syncer(write_store="")
        syncer.upload_source("/tmp/libfoo.tar.gz", TEST_URL_HASH, TEST_FILENAME)
        mock_exec.assert_not_called()


# ---------------------------------------------------------------------------
# Boto3RemoteSync
# ---------------------------------------------------------------------------

@unittest.skipIf(not _HAVE_BOTOCORE, "botocore not installed")
@patch("bits_helpers.sync.Boto3RemoteSync._s3_init", new=MagicMock())
class Boto3RemoteSyncSourceTest(unittest.TestCase):
    """Boto3RemoteSync fetch/upload use the boto3 S3 client."""

    def _make_syncer(self, write_store="bucket"):
        s = sync.Boto3RemoteSync(
            remoteStore="b3://bucket",
            writeStore="b3://{}".format(write_store) if write_store else "",
            architecture="slc9_x86-64",
            workdir="/sw",
        )
        s.s3 = MagicMock()
        return s

    @patch("os.makedirs")
    def test_fetch_success(self, _makedirs):
        syncer = self._make_syncer()
        syncer.s3.download_file = MagicMock()  # no exception → success

        result = syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, "/sw/SOURCES/cache/ab/abc")

        syncer.s3.download_file.assert_called_once()
        kwargs = syncer.s3.download_file.call_args[1]
        self.assertEqual(kwargs["Bucket"], "bucket")
        self.assertIn(TEST_URL_HASH[:2], kwargs["Key"])
        self.assertIn(TEST_URL_HASH, kwargs["Key"])
        self.assertIn(TEST_FILENAME, kwargs["Key"])
        self.assertTrue(result)

    @patch("os.makedirs")
    def test_fetch_miss_404(self, _makedirs):
        from botocore.exceptions import ClientError
        syncer = self._make_syncer()
        syncer.s3.download_file = MagicMock(
            side_effect=ClientError({"Error": {"Code": "404"}}, "download_file"),
        )

        result = syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, "/sw/SOURCES/cache/ab/abc")
        self.assertFalse(result)

    @patch("os.makedirs")
    def test_fetch_miss_no_such_key(self, _makedirs):
        from botocore.exceptions import ClientError
        syncer = self._make_syncer()
        syncer.s3.download_file = MagicMock(
            side_effect=ClientError({"Error": {"Code": "NoSuchKey"}}, "download_file"),
        )

        result = syncer.fetch_source(TEST_URL_HASH, TEST_FILENAME, "/sw/SOURCES/cache/ab/abc")
        self.assertFalse(result)

    def test_upload_new_file(self):
        syncer = self._make_syncer()
        syncer._s3_key_exists = MagicMock(return_value=False)

        syncer.upload_source("/tmp/libfoo.tar.gz", TEST_URL_HASH, TEST_FILENAME)

        syncer.s3.upload_file.assert_called_once()
        kwargs = syncer.s3.upload_file.call_args[1]
        self.assertEqual(kwargs["Bucket"], "bucket")
        self.assertIn(TEST_URL_HASH, kwargs["Key"])
        self.assertEqual(kwargs["Filename"], "/tmp/libfoo.tar.gz")

    def test_upload_skips_existing(self):
        """upload_source must not overwrite an already-present archive."""
        syncer = self._make_syncer()
        syncer._s3_key_exists = MagicMock(return_value=True)

        syncer.upload_source("/tmp/libfoo.tar.gz", TEST_URL_HASH, TEST_FILENAME)
        syncer.s3.upload_file.assert_not_called()

    def test_upload_skipped_with_no_write_store(self):
        syncer = self._make_syncer(write_store="")
        syncer.upload_source("/tmp/libfoo.tar.gz", TEST_URL_HASH, TEST_FILENAME)
        syncer.s3.upload_file.assert_not_called()


# ---------------------------------------------------------------------------
# download() — sync_helper integration
# ---------------------------------------------------------------------------

class DownloadSyncHelperTest(unittest.TestCase):
    """Integration tests for download() with sync_helper=."""

    # Helpers
    # -------
    def _cache_dir_for(self, work_dir, url_hash):
        return os.path.join(work_dir, "SOURCES", "cache", url_hash[:2], url_hash)

    def _put_file_in_cache(self, work_dir, url_hash, filename):
        cache_dir = self._cache_dir_for(work_dir, url_hash)
        os.makedirs(cache_dir, exist_ok=True)
        fpath = os.path.join(cache_dir, filename)
        _write_fake_file(fpath)
        return cache_dir, fpath

    def _fake_download_handler(self, filename, content=_FAKE_CONTENT):
        """Return a fake downloadHandler that writes *content* to dest_dir/filename."""
        def handler(source, dest_dir, work_dir):
            with open(os.path.join(dest_dir, filename), "wb") as fh:
                fh.write(content)
            return True
        return handler

    # Tests
    # -----
    @patch("bits_helpers.download.check_file", return_value=None)
    @patch("bits_helpers.download.executeWithErrorCheck", return_value=True)
    def test_no_sync_helper_still_works(self, _exec, _check):
        """Without sync_helper, download() behaves exactly as before."""
        with tempfile.TemporaryDirectory() as tmp:
            self._put_file_in_cache(tmp, TEST_URL_HASH, TEST_FILENAME)
            # Should complete without raising.
            download(TEST_URL, os.path.join(tmp, "dest"), tmp, sync_helper=None)
            _check.assert_called_once()

    @patch("bits_helpers.download.check_file", return_value=None)
    @patch("bits_helpers.download.executeWithErrorCheck", return_value=True)
    def test_local_cache_hit_skips_store_interaction(self, _exec, _check):
        """When the local cache already has the file, the sync helper is untouched."""
        with tempfile.TemporaryDirectory() as tmp:
            self._put_file_in_cache(tmp, TEST_URL_HASH, TEST_FILENAME)
            mock_helper = MagicMock()

            download(TEST_URL, os.path.join(tmp, "dest"), tmp, sync_helper=mock_helper)

            mock_helper.fetch_source.assert_not_called()
            mock_helper.upload_source.assert_not_called()

    @patch("bits_helpers.download.check_file", return_value=None)
    @patch("bits_helpers.download.executeWithErrorCheck", return_value=True)
    def test_remote_store_hit_skips_upstream_and_upload(self, _exec, _check):
        """On local cache miss + remote store hit, upstream is not contacted and
        upload_source is not called (file is already in the store)."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = self._cache_dir_for(tmp, TEST_URL_HASH)
            os.makedirs(cache_dir, exist_ok=True)

            mock_helper = MagicMock()

            def fake_fetch(u_hash, fname, dest_dir):
                # Simulate the remote store writing the file to local cache.
                with open(os.path.join(dest_dir, fname), "wb") as fh:
                    fh.write(b"from remote store")
                return True

            mock_helper.fetch_source.side_effect = fake_fetch

            download(TEST_URL, os.path.join(tmp, "dest"), tmp, sync_helper=mock_helper)

            mock_helper.fetch_source.assert_called_once_with(
                TEST_URL_HASH, TEST_FILENAME, cache_dir,
            )
            # Already in the store — must not re-upload.
            mock_helper.upload_source.assert_not_called()

    @patch("bits_helpers.download.check_file", return_value=None)
    @patch("bits_helpers.download.executeWithErrorCheck", return_value=True)
    def test_upstream_download_triggers_upload(self, _exec, _check):
        """After a successful upstream download, upload_source() archives it."""
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = self._cache_dir_for(tmp, TEST_URL_HASH)
            os.makedirs(cache_dir, exist_ok=True)
            expected_cached_file = os.path.join(cache_dir, TEST_FILENAME)

            mock_helper = MagicMock()
            mock_helper.fetch_source.return_value = False  # remote store miss

            with patch.dict("bits_helpers.download.downloadHandlers",
                            {"https": self._fake_download_handler(TEST_FILENAME)}):
                download(TEST_URL, os.path.join(tmp, "dest"), tmp,
                         sync_helper=mock_helper)

            # fetch_source was tried
            mock_helper.fetch_source.assert_called_once()
            # upload_source was called with the locally-cached file
            mock_helper.upload_source.assert_called_once_with(
                expected_cached_file, TEST_URL_HASH, TEST_FILENAME,
            )

    @patch("bits_helpers.download.check_file", return_value=None)
    @patch("bits_helpers.download.executeWithErrorCheck", return_value=True)
    def test_fetch_order_remote_before_upstream(self, _exec, _check):
        """fetch_source must be tried BEFORE the upstream downloadHandler."""
        call_order = []

        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = self._cache_dir_for(tmp, TEST_URL_HASH)
            os.makedirs(cache_dir, exist_ok=True)

            def tracking_fetch(u_hash, fname, dest_dir):
                call_order.append("remote_store")
                # Return False so that the upstream handler is also called.
                return False

            def tracking_upstream(source, dest_dir, work_dir):
                call_order.append("upstream")
                with open(os.path.join(dest_dir, TEST_FILENAME), "wb") as fh:
                    fh.write(b"upstream")
                return True

            mock_helper = MagicMock()
            mock_helper.fetch_source.side_effect = tracking_fetch

            with patch.dict("bits_helpers.download.downloadHandlers",
                            {"https": tracking_upstream}):
                download(TEST_URL, os.path.join(tmp, "dest"), tmp,
                         sync_helper=mock_helper)

        self.assertEqual(call_order, ["remote_store", "upstream"],
                         "remote store must be consulted before the upstream URL")


if __name__ == "__main__":
    unittest.main()
