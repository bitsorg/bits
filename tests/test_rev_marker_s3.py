# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""ADR-0005 P2b: S3 read/write of the rev-index markers (Boto3RemoteSync).

Exercises write_rev_marker (idempotent PUT, body=hash) and read_rev_markers
(LIST + GET -> {revision: hash}) against a small fake S3 client, plus the
best-effort / no-op guards.
"""

import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from bits_helpers import sync
from bits_helpers import rev_index as ri

ARCH = "x86_64-el9"


def _spec(rev="2", h="bb22" + "0" * 36):
    return {"package": "fftw", "version": "3.3.10", "revision": rev, "hash": h}


class FakeS3:
    """Minimal stand-in for the boto3 S3 client used by Boto3RemoteSync."""

    def __init__(self, existing=None, objects=None):
        # keys that head_object should report as already present (write bucket)
        self.existing = set(existing or ())
        # key -> body bytes, for list + get (read bucket)
        self.objects = dict(objects or {})
        self.puts = {}  # key -> body bytes actually PUT

    def head_object(self, Bucket, Key):
        if Key in self.existing:
            return {}
        raise ClientError({"Error": {"Code": "404"}}, "head_object")

    def put_object(self, Bucket, Key, Body):
        self.puts[Key] = Body

    def get_object(self, Bucket, Key):
        return {"Body": MagicMock(read=lambda: self.objects[Key])}

    def get_paginator(self, method):
        assert method == "list_objects_v2"
        objects = self.objects

        class _P:
            def paginate(self, Bucket, Prefix):
                contents = [{"Key": k} for k in objects if k.startswith(Prefix)]
                return [{"Contents": contents}] if contents else [{}]
        return _P()


@patch("bits_helpers.sync.Boto3RemoteSync._s3_init", new=MagicMock())
class RevMarkerS3TestCase(unittest.TestCase):

    def _syncer(self, s3, writeStore="wbucket", remoteStore="rbucket"):
        s = sync.Boto3RemoteSync(remoteStore="b3://" + remoteStore,
                                 writeStore="b3://" + writeStore,
                                 architecture=ARCH, workdir="/tmp/x")
        s.s3 = s3
        s.remoteStore = remoteStore
        s.writeStore = writeStore
        return s

    # ---- write ---------------------------------------------------------

    def test_write_marker_puts_hash_when_absent(self):
        s3 = FakeS3()
        self._syncer(s3).write_rev_marker(_spec())
        key = ri.marker_key(ARCH, "fftw", "3.3.10", "2")
        self.assertEqual(s3.puts.get(key), b"bb22" + b"0" * 36)

    def test_write_marker_head_skip_when_present(self):
        key = ri.marker_key(ARCH, "fftw", "3.3.10", "2")
        s3 = FakeS3(existing={key})
        self._syncer(s3).write_rev_marker(_spec())
        self.assertEqual(s3.puts, {}, "must not overwrite an existing marker")

    def test_write_marker_noop_without_write_store(self):
        s3 = FakeS3()
        self._syncer(s3, writeStore="").write_rev_marker(_spec())
        self.assertEqual(s3.puts, {})

    def test_write_marker_noop_for_local_or_empty_revision(self):
        s3 = FakeS3()
        syncer = self._syncer(s3)
        syncer.write_rev_marker(_spec(rev="local3"))
        syncer.write_rev_marker(_spec(rev=""))
        self.assertEqual(s3.puts, {})

    def test_write_marker_swallows_s3_errors(self):
        s3 = FakeS3()
        s3.put_object = MagicMock(side_effect=RuntimeError("boom"))
        # best-effort: must not raise
        self._syncer(s3).write_rev_marker(_spec())

    # ---- read ----------------------------------------------------------

    def test_read_markers_returns_revision_to_hash(self):
        pfx = ri.marker_prefix(ARCH, "fftw", "3.3.10")
        s3 = FakeS3(objects={
            pfx + "1": b"h1",
            pfx + "2": b"h2\n",           # trailing newline stripped
            # a nested key under the same prefix must be ignored
            pfx + "2/extra": b"junk",
            # a different version's marker must not leak in (prefix excludes it)
            "MANIFESTS/rev-index/%s/fftw/3.3.11-1" % ARCH: b"hOTHER",
        })
        got = self._syncer(s3).read_rev_markers("fftw", "3.3.10", ARCH)
        self.assertEqual(got, {"1": "h1", "2": "h2"})

    def test_read_markers_empty_when_none(self):
        got = self._syncer(FakeS3()).read_rev_markers("fftw", "3.3.10", ARCH)
        self.assertEqual(got, {})

    def test_read_markers_swallows_list_errors(self):
        s3 = FakeS3()
        s3.get_paginator = MagicMock(side_effect=RuntimeError("boom"))
        self.assertEqual(
            self._syncer(s3).read_rev_markers("fftw", "3.3.10", ARCH), {})


class HttpListStoreTarballsTestCase(unittest.TestCase):
    """HttpRemoteSync.list_store_tarballs: the object NAME carries the revision.

    The listing is untrusted remote JSON, so it must not raise and must not let a
    path component escape.
    """

    def _syncer(self, listing):
        s = sync.HttpRemoteSync.__new__(sync.HttpRemoteSync)
        s.remoteStore = "https://s3.cern.ch/swift/v1/bucket"
        s.getRetry = MagicMock(return_value=listing)
        return s

    def test_returns_names(self):
        s = self._syncer([{"name": "fftw-3.3.10-1.%s.tar.gz" % ARCH, "type": "file"}])
        self.assertEqual(s.list_store_tarballs(ARCH, "aa" * 20),
                         ["fftw-3.3.10-1.%s.tar.gz" % ARCH])
        self.assertIn("TARS/%s/store/aa/" % ARCH, s.getRetry.call_args[0][0])

    def test_hostile_or_malformed_listing_is_survivable(self):
        for listing in (None, "not-a-list", 42, [None], ["bare-string"],
                        [{"type": "file"}], [{"name": 7}], [{}]):
            self.assertEqual(self._syncer(listing).list_store_tarballs(ARCH, "aa"), [],
                             repr(listing))

    def test_names_are_basenamed(self):
        s = self._syncer([{"name": "../../../etc/passwd"},
                          {"name": "/abs/fftw-3.3.10-2.%s.tar.gz" % ARCH}])
        self.assertEqual(s.list_store_tarballs(ARCH, "aa"),
                         ["passwd", "fftw-3.3.10-2.%s.tar.gz" % ARCH])

    def test_getretry_errors_are_swallowed(self):
        s = self._syncer(None)
        s.getRetry = MagicMock(side_effect=RuntimeError("boom"))
        self.assertEqual(s.list_store_tarballs(ARCH, "aa"), [])


class Boto3ListStoreTarballsTestCase(unittest.TestCase):

    def _syncer(self, keys):
        s = sync.Boto3RemoteSync.__new__(sync.Boto3RemoteSync)
        s._s3_listdir = MagicMock(return_value=iter(keys))
        return s

    def test_returns_basenames(self):
        s = self._syncer(["TARS/%s/store/aa/aabb/fftw-3.3.10-1.%s.tar.gz" % (ARCH, ARCH)])
        self.assertEqual(s.list_store_tarballs(ARCH, "aabb"),
                         ["fftw-3.3.10-1.%s.tar.gz" % ARCH])

    def test_list_errors_are_swallowed(self):
        s = sync.Boto3RemoteSync.__new__(sync.Boto3RemoteSync)
        s._s3_listdir = MagicMock(side_effect=RuntimeError("boom"))
        self.assertEqual(s.list_store_tarballs(ARCH, "aabb"), [])


class _BareReader:
    """A read-only backend with no store-metadata support (CVMFS, rsync, http)."""
    architecture = ARCH
    workdir = "/sw"


class DualDelegationTestCase(unittest.TestCase):
    """DualRemoteSync routes store-metadata reads to reader, then writer (ADR-0005 P2d)."""

    def test_delegates_read_rev_markers_to_reader(self):
        reader = MagicMock()
        reader.read_rev_markers.return_value = {"2": "bb"}
        writer = MagicMock()
        dual = sync.DualRemoteSync(reader=reader, writer=writer)
        self.assertEqual(dual.read_rev_markers("fftw", "3.3.10", ARCH), {"2": "bb"})
        reader.read_rev_markers.assert_called_once_with("fftw", "3.3.10", ARCH)
        writer.read_rev_markers.assert_not_called()

    def test_falls_back_to_writer_when_reader_lacks_method(self):
        # REGRESSION: `--remote-store https://…::rw` builds DualRemoteSync(Http, Boto3).
        # HttpRemoteSync had no read_rev_markers, and delegating to the reader ALONE
        # returned {} — so the counter never saw the markers, treated the revision as
        # free, and a second hash was uploaded under the same revision number. The
        # markers live in the writer's bucket (the same bucket, here); use it.
        writer = MagicMock()
        writer.read_rev_markers.return_value = {"1": "aa"}
        dual = sync.DualRemoteSync(reader=_BareReader(), writer=writer)
        self.assertEqual(dual.read_rev_markers("fftw", "3.3.10", ARCH), {"1": "aa"})
        writer.read_rev_markers.assert_called_once_with("fftw", "3.3.10", ARCH)

    def test_falls_back_to_writer_when_reader_raises(self):
        reader = MagicMock()
        reader.read_rev_markers.side_effect = RuntimeError("network")
        writer = MagicMock()
        writer.read_rev_markers.return_value = {"1": "aa"}
        dual = sync.DualRemoteSync(reader=reader, writer=writer)
        self.assertEqual(dual.read_rev_markers("fftw", "3.3.10", ARCH), {"1": "aa"})

    def test_empty_when_neither_backend_supports_markers(self):
        dual = sync.DualRemoteSync(reader=_BareReader(), writer=_BareReader())
        self.assertEqual(dual.read_rev_markers("fftw", "3.3.10", ARCH), {})

    def test_list_store_tarballs_delegates_and_defaults(self):
        reader = MagicMock()
        reader.list_store_tarballs.return_value = ["fftw-3.3.10-1.%s.tar.gz" % ARCH]
        dual = sync.DualRemoteSync(reader=reader, writer=MagicMock())
        self.assertEqual(dual.list_store_tarballs(ARCH, "aa"),
                         ["fftw-3.3.10-1.%s.tar.gz" % ARCH])
        self.assertEqual(
            sync.DualRemoteSync(reader=_BareReader(),
                                writer=_BareReader()).list_store_tarballs(ARCH, "aa"),
            [])


if __name__ == "__main__":
    unittest.main()
