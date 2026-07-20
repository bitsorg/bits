# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import os.path
import sys
import unittest
from io import BytesIO

from unittest.mock import patch, MagicMock

from bits_helpers import sync
from bits_helpers.utilities import resolve_links_path, resolve_store_path


ARCHITECTURE = "slc7_x86-64"
PACKAGE = "zlib"
GOOD_HASH = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
BAD_HASH = "baadf00dbaadf00dbaadf00dbaadf00dbaadf00d"
NONEXISTENT_HASH = "TRIGGERS_A_404"
GOOD_SPEC = {    # fully present on the remote store
    "package": PACKAGE, "version": "v1.3.1", "revision": "1",
    "hash": GOOD_HASH,
    "remote_revision_hash": GOOD_HASH,
    "remote_hashes": [GOOD_HASH],
}
BAD_SPEC = {     # partially present on the remote store
    "package": PACKAGE, "version": "v1.3.1", "revision": "2",
    "hash": BAD_HASH,
    "remote_revision_hash": BAD_HASH,
    "remote_hashes": [BAD_HASH],
}
MISSING_SPEC = {    # completely absent from the remote store
    "package": PACKAGE, "version": "v1.3.1", "revision": "3",
    "hash": NONEXISTENT_HASH,
    "remote_revision_hash": NONEXISTENT_HASH,
    "remote_hashes": [NONEXISTENT_HASH],
}


def tarball_name(spec):
    return ("{package}-{version}-{revision}.{arch}.tar.gz"
            .format(arch=ARCHITECTURE, **spec))


TAR_NAMES = tarball_name(GOOD_SPEC), tarball_name(BAD_SPEC), tarball_name(MISSING_SPEC)


class MockRequest:
    def __init__(self, j, simulate_err=False) -> None:
        self.j = j
        self.simulate_err = simulate_err
        self.status_code = 200 if j else 404
        self._bytes_left = 123456
        self.headers = {"content-length": str(self._bytes_left)}

    def raise_for_status(self):
        return True

    def json(self):
        return self.j

    def iter_content(self, chunk_size=10):
        if not self.simulate_err:
            while self._bytes_left > 0:
                toread = min(chunk_size, self._bytes_left)
                yield b"x" * toread
                self._bytes_left -= toread


@patch("bits_helpers.sync.ProgressPrint", new=MagicMock())
class SyncTestCase(unittest.TestCase):
    def mock_get(self, url, *args, **kw):
        if NONEXISTENT_HASH in url:
            return MockRequest(None)
        if "/store/" in url:
            if GOOD_HASH in url:
                return MockRequest([{"name": tarball_name(GOOD_SPEC)}])
            elif BAD_HASH in url:
                return MockRequest([{"name": tarball_name(BAD_SPEC)}],
                                   simulate_err=True)
        elif url.endswith(".manifest"):
            return MockRequest("")
        elif ("/%s/" % PACKAGE) in url:
            return MockRequest([{"name": tarball_name(GOOD_SPEC)},
                                {"name": tarball_name(BAD_SPEC)}])
        raise NotImplementedError(url)

    @patch("bits_helpers.sync.open", new=lambda fn, mode: BytesIO())
    @patch("os.path.isfile", new=MagicMock(return_value=False))
    @patch("os.rename", new=MagicMock(return_value=None))
    @patch("os.makedirs", new=MagicMock(return_value=None))
    @patch("os.listdir", new=MagicMock(return_value=[]))
    @patch("bits_helpers.sync.symlink", new=MagicMock(return_value=None))
    @patch("bits_helpers.sync.execute", new=MagicMock(return_value=None))
    @patch("bits_helpers.sync.debug")
    @patch("bits_helpers.sync.error")
    @patch("requests.Session.get")
    def test_http_remote(self, mock_get, mock_error, mock_debug):
        """Test HTTPS remote store."""
        mock_get.side_effect = self.mock_get
        syncer = sync.HttpRemoteSync(remoteStore="https://localhost/test",
                                     architecture=ARCHITECTURE,
                                     workdir="/sw", insecure=False)
        syncer.httpBackoff = 0  # speed up tests

        # Try good spec
        mock_error.reset_mock()

        syncer.fetch_symlinks(GOOD_SPEC)
        syncer.fetch_tarball(GOOD_SPEC)
        mock_error.assert_not_called()
        syncer.upload_symlinks_and_tarball(GOOD_SPEC)

        # Try bad spec
        mock_error.reset_mock()

        syncer.fetch_symlinks(BAD_SPEC)
        syncer.fetch_tarball(BAD_SPEC)

        # We can't use mock_error.assert_called_once_with because two
        # PartialDownloadError instances don't compare equal.
        self.assertEqual(len(mock_error.call_args_list), 1)
        self.assertEqual(mock_error.call_args_list[0][0][0],
                         "GET %s failed: %s")
        self.assertEqual(mock_error.call_args_list[0][0][1],
                         "https://localhost/test/TARS/%s/store/%s/%s/%s" %
                         (ARCHITECTURE, BAD_SPEC["remote_revision_hash"][:2],
                          BAD_SPEC["remote_revision_hash"],
                          tarball_name(BAD_SPEC)))
        self.assertIsInstance(mock_error.call_args_list[0][0][2],
                              sync.PartialDownloadError)

        syncer.upload_symlinks_and_tarball(BAD_SPEC)

        # Try missing spec
        mock_debug.reset_mock()
        syncer.fetch_symlinks(MISSING_SPEC)
        syncer.fetch_tarball(MISSING_SPEC)
        mock_debug.assert_called_with("Nothing fetched for %s (%s)",
                                      MISSING_SPEC["package"], NONEXISTENT_HASH)

    @patch("bits_helpers.sync.execute", new=lambda cmd, printer=None: 0)
    @patch("bits_helpers.sync.os")
    def test_sync(self, mock_os):
        """Check NoRemoteSync, rsync:// and s3:// remote stores."""
        # file does not exist locally: force download
        mock_os.path.exists.side_effect = lambda path: False
        mock_os.path.islink.side_effect = lambda path: False
        mock_os.path.isfile.side_effect = lambda path: False

        syncers = [
            sync.NoRemoteSync(),
            sync.RsyncRemoteSync(remoteStore="ssh://localhost/test",
                                 writeStore="ssh://localhost/test",
                                 architecture=ARCHITECTURE,
                                 workdir="/sw"),
            sync.S3RemoteSync(remoteStore="s3://localhost",
                              writeStore="s3://localhost",
                              architecture=ARCHITECTURE,
                              workdir="/sw"),
        ]

        for spec in (GOOD_SPEC, BAD_SPEC):
            for syncer in syncers:
                syncer.fetch_symlinks(spec)
                syncer.fetch_tarball(spec)
                syncer.upload_symlinks_and_tarball(spec)

        for syncer in syncers:
            syncer.fetch_symlinks(MISSING_SPEC)
            syncer.fetch_tarball(MISSING_SPEC)


class RedistributableFormsTestCase(unittest.TestCase):
    """redistributable: all | binaries | sources | none (+ legacy booleans)."""

    def test_enum_and_legacy_values(self):
        f = sync.redistributable_forms
        both = {"binaries", "sources"}
        self.assertEqual(f(None), both)          # absent -> all
        self.assertEqual(f("all"), both)
        self.assertEqual(f(True), both)          # legacy
        self.assertEqual(f("binaries"), {"binaries"})
        self.assertEqual(f("sources"), {"sources"})
        self.assertEqual(f("none"), set())
        self.assertEqual(f(False), set())        # legacy
        self.assertEqual(f("NONE"), set())       # case-insensitive
        self.assertEqual(f("typo"), set())       # unknown -> fail closed

    def test_spec_gates(self):
        self.assertTrue(sync.binary_redistributable({}))
        self.assertTrue(sync.sources_redistributable({}))
        spec = {"redistributable": "binaries"}
        self.assertTrue(sync.binary_redistributable(spec))
        self.assertFalse(sync.sources_redistributable(spec))
        spec = {"redistributable": "sources"}
        self.assertFalse(sync.binary_redistributable(spec))
        self.assertTrue(sync.sources_redistributable(spec))
        spec = {"redistributable": "none"}
        self.assertFalse(sync.binary_redistributable(spec))
        self.assertFalse(sync.sources_redistributable(spec))

    def test_source_upload_gate_wraps_only_when_needed(self):
        helper = MagicMock()
        self.assertIs(sync.source_sync_for({"redistributable": "all"}, helper),
                      helper)
        gated = sync.source_sync_for({"redistributable": "none"}, helper)
        gated.upload_source("/tmp/x", "abc", "x.tar.gz")
        helper.upload_source.assert_not_called()   # dropped
        gated.fetch_source("abc", "x.tar.gz", "/tmp")
        helper.fetch_source.assert_called_once()   # reads still delegate


@unittest.skipIf(sys.version_info < (3, 6), "python >= 3.6 is required for boto3")
@patch("os.makedirs", new=MagicMock(return_value=None))
@patch("bits_helpers.sync.symlink", new=MagicMock(return_value=None))
@patch("bits_helpers.sync.ProgressPrint", new=MagicMock())
@patch("bits_helpers.log.error", new=MagicMock())
@patch("bits_helpers.sync.Boto3RemoteSync._s3_init", new=MagicMock())
class Boto3TestCase(unittest.TestCase):
    """Check the b3:// remote is working properly."""

    def setUp(self):
        """Create a real workdir holding a content tarball per spec.

        Done in setUp because it runs BEFORE the per-test @patch decorators mock
        out os.makedirs/os.path.*. The upload path checksums the file it sends (a
        .tar.gz is not byte-reproducible, so the store dedupes on the BYTES, not
        on the key alone), so the local tarball has to actually exist.
        Populates ``self.workdir`` and ``self.shas`` ({hash: sha256}).
        """
        import shutil
        import tempfile
        from bits_helpers.checksum import checksum_file
        self.workdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.workdir, True)
        self.shas = {}
        for spec in (MISSING_SPEC, GOOD_SPEC, BAD_SPEC):
            d = os.path.join(self.workdir,
                             resolve_store_path(ARCHITECTURE, spec["hash"]))
            os.makedirs(d, exist_ok=True)
            tar = os.path.join(d, tarball_name(spec))
            with open(tar, "wb") as fh:
                fh.write(b"tarball-" + spec["hash"].encode("utf-8"))
            # Same helper the upload path uses, so the two can never drift.
            self.shas[spec["hash"]] = checksum_file(tar)

    def mock_s3(self):
        """Create a mock object imitating an S3 client.

        Which spec we are listing contents for controls the simulated contents
        of the store under dist*/:

        - MISSING_SPEC: Simulate a case where the store is empty; we can safely
          upload objects to the remote.
        - GOOD_SPEC: Simulate a case where we can fetch tarballs from the store;
          we mustn't upload as that would overwrite existing packages.
        - BAD_SPEC: Simulate a case where we must abort our upload.

        This currently only affects the simulated contents of dist*
        directories.
        """
        from botocore.exceptions import ClientError

        def paginate_listdir(Bucket, Delimiter, Prefix):
            dir = Prefix.rstrip(Delimiter)
            if dir in (resolve_store_path(ARCHITECTURE, NONEXISTENT_HASH),
                       resolve_store_path(ARCHITECTURE, BAD_HASH)):
                return [{}]
            elif dir in (resolve_store_path(ARCHITECTURE, GOOD_HASH),
                         resolve_links_path(ARCHITECTURE, PACKAGE)):
                return [{"Contents": [
                    {"Key": dir + Delimiter + tarball_name(GOOD_SPEC)},
                ]}]
            elif "/dist" not in Prefix:
                raise NotImplementedError("unknown prefix " + Prefix)
            elif dir.endswith("-" + GOOD_SPEC["revision"]):
                # The expected dist symlinks already exist on S3. As our
                # test package has no dependencies, the prefix should only
                # contain a link to the package itself.
                return [{"Contents": [
                    {"Key": dir + Delimiter + "%s.%s.tar.gz" %
                     (os.path.basename(dir), ARCHITECTURE)},
                ]}]
            elif dir.endswith("-" + BAD_SPEC["revision"]):
                # Simulate partially complete upload of symlinks, e.g. by
                # another bits running in parallel.
                return [{"Contents": [
                    {"Key": dir + Delimiter + "somepackage-v1-1.%s.tar.gz" % ARCHITECTURE},
                ]}]
            elif dir.endswith("-" + MISSING_SPEC["revision"]):
                # No pre-existing symlinks under dist*.
                return [{"Contents": []}]
            else:
                raise NotImplementedError("unknown dist prefix " + Prefix)

        def head_object(Bucket, Key):
            if NONEXISTENT_HASH in Key or BAD_HASH in Key or \
               os.path.basename(Key) == tarball_name(MISSING_SPEC):
                raise ClientError({"Error": {"Code": "404"}}, "head_object")
            return {}

        def download_file(Bucket, Key, Filename, Callback=None):
            self.assertNotIn(NONEXISTENT_HASH, Key, "tried to fetch missing tarball")
            self.assertNotIn(BAD_HASH, Key, "tried to follow bad symlink")

        def get_object(Bucket, Key):
            if Key.endswith(".manifest"):
                return {"Body": MagicMock(iter_lines=lambda: [
                    tarball_name(GOOD_SPEC).encode("utf-8") + b"\t...from manifest\n",
                ])}
            return {"Body": MagicMock(read=lambda: b"...fetched individually")}

        def get_paginator(method):
            if method == "list_objects_v2":
                return MagicMock(paginate=paginate_listdir)
            raise NotImplementedError(method)

        return MagicMock(
            get_paginator=get_paginator,
            head_object=head_object,
            download_file=MagicMock(side_effect=download_file),
            get_object=get_object,
            put_object=MagicMock(return_value=None),
            upload_file=MagicMock(return_value=None),
        )

    @patch("glob.glob", new=MagicMock(return_value=[]))
    @patch("os.listdir", new=MagicMock(return_value=[]))
    @patch("os.makedirs", new=MagicMock())
    # Pretend file does not exist locally to force download.
    @patch("os.path.exists", new=MagicMock(return_value=False))
    @patch("os.path.isfile", new=MagicMock(return_value=False))
    @patch("os.path.islink", new=MagicMock(return_value=False))
    @patch("bits_helpers.sync.execute", new=MagicMock(return_value=0))
    def test_tarball_download(self) -> None:
        """Test boto3 behaviour when downloading tarballs from the remote."""
        b3sync = sync.Boto3RemoteSync(
            remoteStore="b3://localhost", writeStore="b3://localhost",
            architecture=ARCHITECTURE, workdir="/sw")
        b3sync.s3 = self.mock_s3()

        b3sync.s3.download_file.reset_mock()
        b3sync.fetch_symlinks(GOOD_SPEC)
        b3sync.fetch_tarball(GOOD_SPEC)
        b3sync.s3.download_file.assert_called()

        b3sync.s3.download_file.reset_mock()
        b3sync.fetch_symlinks(BAD_SPEC)
        b3sync.fetch_tarball(BAD_SPEC)
        b3sync.s3.download_file.assert_not_called()

        b3sync.s3.download_file.reset_mock()
        b3sync.fetch_symlinks(MISSING_SPEC)
        b3sync.fetch_tarball(MISSING_SPEC)
        b3sync.s3.download_file.assert_not_called()

    @patch("os.listdir", new=lambda path: (
        [tarball_name(GOOD_SPEC)] if path.endswith("-" + GOOD_SPEC["revision"]) else
        [tarball_name(BAD_SPEC)] if path.endswith("-" + BAD_SPEC["revision"]) else
        [] if path.endswith("-" + MISSING_SPEC["revision"]) else
        NotImplemented
    ))
    @patch("os.readlink", new=MagicMock(return_value="dummy path"))
    @patch("os.path.islink", new=MagicMock(return_value=True))
    @patch("bits_helpers.sync.Boto3RemoteSync.write_rev_marker", new=MagicMock())
    def test_tarball_upload(self) -> None:
        """Hash-only upload (ADR-0005): the STORE object is authoritative.

        If an object already exists at the designated path it is KEPT — never
        overwritten — because a .tar.gz is not byte-reproducible and overwriting
        would invalidate the checksum every previously-certified manifest
        recorded for it. The stored object's sha256 (metadata, or streamed once
        for a legacy object) is recorded on the spec so the build manifest
        describes the store's bytes, which is what `bits certify` validates.
        NO version-link or dist-symlink objects are written (no put_object).
        """
        import hashlib
        shas = self.shas
        b3sync = sync.Boto3RemoteSync(
            remoteStore="b3://localhost", writeStore="b3://localhost",
            architecture=ARCHITECTURE, workdir=self.workdir)
        b3sync.s3 = self.mock_s3()

        # Fresh build: the content object is absent -> upload it, with the sha256
        # recorded in the object metadata AND on the spec (store == local bytes).
        # The store keeps only the hash-keyed tarball, so no put_object is issued.
        missing_spec = dict(MISSING_SPEC)
        b3sync.s3.put_object.reset_mock()
        b3sync.s3.upload_file.reset_mock()
        b3sync.upload_symlinks_and_tarball(missing_spec)
        b3sync.s3.upload_file.assert_called()
        b3sync.s3.put_object.assert_not_called()
        self.assertEqual(
            b3sync.s3.upload_file.call_args.kwargs["ExtraArgs"]["Metadata"]["sha256"],
            shas[MISSING_SPEC["hash"]])
        self.assertEqual(missing_spec["store_tarball_sha256"],
                         shas[MISSING_SPEC["hash"]])

        # Object already present (metadata sha differs from the local bytes —
        # e.g. an earlier build's packing of the same hash): KEEP the stored
        # object, upload nothing, and record ITS sha256 on the spec so the
        # manifest describes the store, not this build's local repack.
        remote_sha = "sha256:" + "ab" * 32
        good_spec = dict(GOOD_SPEC)
        b3sync.s3.head_object = lambda Bucket, Key: {
            "Metadata": {"sha256": remote_sha}}
        b3sync.s3.put_object.reset_mock()
        b3sync.s3.upload_file.reset_mock()
        b3sync.upload_symlinks_and_tarball(good_spec)
        b3sync.s3.upload_file.assert_not_called()
        b3sync.s3.put_object.assert_not_called()
        self.assertEqual(good_spec["store_tarball_sha256"], remote_sha)

        # Legacy object with no recorded checksum: stream it once, record the
        # digest of the STORED bytes, stamp it back via a metadata self-copy,
        # and still never re-upload.
        stored_bytes = b"legacy-stored-bytes"
        legacy_sha = "sha256:" + hashlib.sha256(stored_bytes).hexdigest()
        chunks = [stored_bytes, b""]
        good_spec = dict(GOOD_SPEC)
        b3sync.s3.head_object = lambda Bucket, Key: {}
        b3sync.s3.get_object = lambda Bucket, Key: {
            "Body": MagicMock(read=lambda n=None: chunks.pop(0))}
        b3sync.s3.copy_object = MagicMock()
        b3sync.s3.upload_file.reset_mock()
        b3sync.upload_symlinks_and_tarball(good_spec)
        b3sync.s3.upload_file.assert_not_called()
        self.assertEqual(good_spec["store_tarball_sha256"], legacy_sha)
        self.assertEqual(
            b3sync.s3.copy_object.call_args.kwargs["Metadata"]["sha256"],
            legacy_sha)

    @patch("os.listdir", new=lambda path: (
        [] if path.endswith("-" + MISSING_SPEC["revision"]) else NotImplemented))
    @patch("os.readlink", new=MagicMock(return_value="dummy path"))
    @patch("os.path.islink", new=MagicMock(return_value=True))
    @patch("bits_helpers.sync.Boto3RemoteSync.write_rev_marker")
    def test_upload_writes_rev_marker(self, mock_marker) -> None:
        """Every upload records the build's rev-index marker (ADR-0005 P2d)."""
        b3sync = sync.Boto3RemoteSync(
            remoteStore="b3://localhost", writeStore="b3://localhost",
            architecture=ARCHITECTURE, workdir=self.workdir)
        b3sync.s3 = self.mock_s3()
        b3sync.upload_symlinks_and_tarball(MISSING_SPEC)
        mock_marker.assert_called_once_with(MISSING_SPEC)

    @patch("os.listdir", new=lambda path: (
        [] if path.endswith("-" + MISSING_SPEC["revision"]) else NotImplemented
    ))
    @patch("os.readlink", new=MagicMock(return_value="dummy path"))
    @patch("os.path.islink", new=MagicMock(return_value=True))
    def test_tarball_upload_spec_with_architecture_key(self) -> None:
        """Regression: a spec carrying an 'architecture' key (shared packages, or
        any recipe that sets the field) must not crash tarball naming.

        The previous code did ``"...".format(architecture=arch, **spec)``; when
        spec has an 'architecture' key, **spec re-passes it and Python raises
        'TypeError: got multiple values for keyword argument architecture'. The
        tarball must be named with effective_arch (== build arch here), matching
        the store key and the symlink target.
        """
        b3sync = sync.Boto3RemoteSync(
            remoteStore="b3://localhost", writeStore="b3://localhost",
            architecture=ARCHITECTURE, workdir=self.workdir)
        b3sync.s3 = self.mock_s3()
        # MISSING_SPEC routing (fresh upload) + an explicit architecture key.
        spec = dict(MISSING_SPEC, architecture=ARCHITECTURE)
        b3sync.s3.put_object.reset_mock()
        b3sync.s3.upload_file.reset_mock()
        b3sync.upload_symlinks_and_tarball(spec)   # must not raise TypeError
        b3sync.s3.upload_file.assert_called()
        # The tarball is uploaded under the effective-arch name (== ARCHITECTURE).
        key = b3sync.s3.upload_file.call_args.kwargs["Key"]
        self.assertTrue(key.endswith(tarball_name(MISSING_SPEC)),
                        "tarball key %r not named with effective arch" % key)


@patch("bits_helpers.sync.Boto3RemoteSync._s3_init", new=MagicMock())
class DualRemoteSyncTestCase(unittest.TestCase):
    """Cross-backend stores: recall from CVMFS, upload freshly-built to S3."""

    READ = "cvmfs:///cvmfs/sft-nightlies-test.cern.ch/lcg/bits/"
    WRITE = "b3://bucketofpieces"

    # ── remote_from_url dispatch ─────────────────────────────────────────────
    def test_cvmfs_read_plus_write_returns_dual(self):
        helper = sync.remote_from_url(self.READ, self.WRITE, ARCHITECTURE, "/work")
        self.assertIsInstance(helper, sync.DualRemoteSync)
        self.assertIsInstance(helper.reader, sync.CVMFSRemoteSync)
        self.assertIsInstance(helper.writer, sync.Boto3RemoteSync)
        # Writer targets the write store for both its read-back and its uploads.
        self.assertEqual(helper.writer.writeStore, "bucketofpieces")
        self.assertEqual(helper.writer.remoteStore, "bucketofpieces")

    def test_cvmfs_read_without_write_is_unchanged(self):
        # No write store -> the old read-only CVMFS helper, not a Dual.
        helper = sync.remote_from_url(self.READ, "", ARCHITECTURE, "/work")
        self.assertIsInstance(helper, sync.CVMFSRemoteSync)

    def test_same_backend_is_unchanged(self):
        helper = sync.remote_from_url("b3://bucket", "b3://bucket", ARCHITECTURE, "/work")
        self.assertIsInstance(helper, sync.Boto3RemoteSync)

    @patch("bits_helpers.sync.error", new=MagicMock())
    def test_cvmfs_write_target_is_rejected(self):
        with self.assertRaises(SystemExit):
            sync.remote_from_url(self.READ, "cvmfs:///somewhere", ARCHITECTURE, "/work")

    # ── routing + freshly-built gate ─────────────────────────────────────────
    def _dual(self):
        return sync.DualRemoteSync(reader=MagicMock(), writer=MagicMock())

    def test_reads_go_to_reader(self):
        dual = self._dual()
        spec = {"package": PACKAGE}
        dual.fetch_tarball(spec)
        dual.fetch_symlinks(spec)
        dual.reader.fetch_tarball.assert_called_once_with(spec)
        dual.reader.fetch_symlinks.assert_called_once_with(spec)
        dual.writer.fetch_tarball.assert_not_called()

    def test_freshly_built_package_is_uploaded(self):
        dual = self._dual()
        spec = {"package": PACKAGE, "cachedTarball": ""}     # built from source
        dual.upload_symlinks_and_tarball(spec)
        dual.writer.upload_symlinks_and_tarball.assert_called_once_with(spec)

    def test_recalled_package_is_not_uploaded(self):
        dual = self._dual()
        spec = {"package": PACKAGE, "cachedTarball": "/work/TARS/.../zlib.tar.gz"}
        dual.upload_symlinks_and_tarball(spec)
        dual.writer.upload_symlinks_and_tarball.assert_not_called()
        self.assertIsNone(dual.upload_shell_command(spec))
        dual.writer.upload_shell_command.assert_not_called()

    def test_writeStore_disable_propagates_to_writer(self):
        # build.py sets syncHelper.writeStore = "" to disable uploads for devel pkgs.
        dual = sync.DualRemoteSync(reader=MagicMock(), writer=MagicMock(writeStore="bucket"))
        self.assertEqual(dual.writeStore, "bucket")
        dual.writeStore = ""
        self.assertEqual(dual.writer.writeStore, "")


if __name__ == '__main__':
    unittest.main()
