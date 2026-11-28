# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sqlite3
import hashlib
import io
import tempfile
import unittest
import zlib
from unittest import mock

from bits_helpers.cvmfs_catalog import (
    FastPathUnavailable,
    fetch_and_decompress,
    kFlagDir,
    kFlagFile,
    list_from_catalog_db,
    main,
    parse_catalog_counters,
    subtree_nested,
)


def _make_catalog(rows):
    """Write a minimal cvmfs-style catalog SQLite; rows: (name,m1,m2,p1,p2,flags)."""
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE catalog (name TEXT, md5path_1 INTEGER, "
                "md5path_2 INTEGER, parent_1 INTEGER, parent_2 INTEGER, "
                "flags INTEGER)")
    con.executemany("INSERT INTO catalog VALUES (?,?,?,?,?,?)", rows)
    con.commit()
    con.close()
    return path


# A small install-tree-shaped catalog rooted at "x":
#   x/ROOT, x/ROOT/v6.40 (dir), x/ROOT/v6.40/etc/modulefiles/ROOT (file),
#   x/Boost, x/Boost/1.90 (dir)
ROWS = [
    ("x",            0, 0, 999, 999, kFlagDir),   # catalog root (parent absent)
    ("ROOT",         1, 0, 0, 0, kFlagDir),       # depth 1
    ("v6.40",        2, 0, 1, 0, kFlagDir),       # depth 2 dir
    ("etc",          3, 0, 2, 0, kFlagDir),
    ("modulefiles",  4, 0, 3, 0, kFlagDir),
    ("ROOT",         5, 0, 4, 0, kFlagFile),      # the modulefile
    ("Boost",        6, 0, 0, 0, kFlagDir),       # depth 1
    ("1.90",         7, 0, 6, 0, kFlagDir),       # depth 2 dir
]


class CatalogReconstructionTest(unittest.TestCase):
    def test_paths_relative_to_catalog_root(self):
        db = _make_catalog(ROWS)
        try:
            entries = dict(list_from_catalog_db(db))
        finally:
            os.unlink(db)
        # Root "x" is dropped; everything else is relative to it.
        self.assertEqual(
            set(entries),
            {"ROOT", "ROOT/v6.40", "ROOT/v6.40/etc",
             "ROOT/v6.40/etc/modulefiles", "ROOT/v6.40/etc/modulefiles/ROOT",
             "Boost", "Boost/1.90"})

    def test_depth2_dirs_filter(self):
        db = _make_catalog(ROWS)
        try:
            entries = list_from_catalog_db(db)
        finally:
            os.unlink(db)
        depth2 = sorted(rel for rel, flags in entries
                        if (flags & kFlagDir) and rel.count("/") == 1)
        self.assertEqual(depth2, ["Boost/1.90", "ROOT/v6.40"])

    def test_modulefiles_filter(self):
        db = _make_catalog(ROWS)
        try:
            entries = list_from_catalog_db(db)
        finally:
            os.unlink(db)
        files = sorted(rel for rel, flags in entries if flags & kFlagFile)
        self.assertEqual(files, ["ROOT/v6.40/etc/modulefiles/ROOT"])

    def test_bad_schema_raises_fastpath_unavailable(self):
        fd, db = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE notcatalog (x INTEGER)")
        con.commit()
        con.close()
        try:
            with self.assertRaises(FastPathUnavailable):
                list_from_catalog_db(db)
        finally:
            os.unlink(db)


class CountersParseTest(unittest.TestCase):
    def test_parse_hash_mountpoint_and_counters(self):
        txt = ("catalog_hash: abc123\n"
               "catalog_mountpoint: /cvmfs/x/modules\n"
               "self_regular,5\n"
               "subtree_nested,0\n")
        info = parse_catalog_counters(txt)
        self.assertEqual(info["hash"], "abc123")
        self.assertEqual(info["mountpoint"], "/cvmfs/x/modules")
        self.assertEqual(subtree_nested(info["counters"]), 0)


class MainFallbackContractTest(unittest.TestCase):
    def test_nonexistent_dir_returns_3(self):
        self.assertEqual(main(["/no/such/cvmfs/dir"]), 3)

    def test_non_cvmfs_dir_returns_3(self):
        # A real local dir with no CVMFS xattr -> fast path unavailable -> 3.
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main([d]), 3)


class _Response(io.BytesIO):
    def read(self, n=-1):
        # Before 3.10 HTTPResponse.read(n) allocates n bytes: a read must be small.
        assert 0 < n <= 1 << 20, "read(%d)" % n
        return super().read(n)


def _serving(blob):
    """urlopen stand-in that answers every request with blob."""
    return mock.patch("bits_helpers.cvmfs_catalog.urllib.request.urlopen",
                      lambda req, timeout=None: _Response(blob))


class FetchVerifiesObjectTest(unittest.TestCase):
    """An object is addressed by the digest of its compressed bytes, so a
    fetch that returns anything else must not be believed."""
    URL = "http://store/data/ab/cdC"
    BODY = b"SQLite format 3\0" + b"x" * 1000

    def test_object_matching_its_hash_is_decompressed(self):
        blob = zlib.compress(self.BODY)
        with _serving(blob):
            out = fetch_and_decompress(self.URL,
                                       expected=hashlib.sha1(blob).hexdigest())
        self.assertEqual(out, self.BODY)

    def test_other_bytes_than_the_named_object_are_refused(self):
        # NEGATIVE CONTROL: drop the digest comparison and this decompresses.
        named = zlib.compress(self.BODY)
        forged = zlib.compress(b"forged catalog")
        with _serving(forged):
            with self.assertRaisesRegex(FastPathUnavailable, "is not object"):
                fetch_and_decompress(self.URL,
                                     expected=hashlib.sha1(named).hexdigest())

    def test_suffixed_algorithm_is_checked_in_that_algorithm(self):
        blob = zlib.compress(self.BODY)
        h = hashlib.shake_128(blob).hexdigest(20) + "-shake128"
        with _serving(blob):
            self.assertEqual(fetch_and_decompress(self.URL, expected=h),
                             self.BODY)
        with _serving(blob):
            with self.assertRaises(FastPathUnavailable):
                fetch_and_decompress(self.URL, expected=hashlib.sha1(
                    blob).hexdigest() + "-shake128")

    def test_unverifiable_hash_is_refused(self):
        blob = zlib.compress(self.BODY)
        for bad in ("abc123", hashlib.sha1(blob).hexdigest() + "-md5"):
            with _serving(blob):
                with self.assertRaisesRegex(FastPathUnavailable, "verify"):
                    fetch_and_decompress(self.URL, expected=bad)

    def test_object_expanding_past_the_cap_is_refused(self):
        # NEGATIVE CONTROL: decompress without max_length and this passes.
        blob = zlib.compress(b"\0" * 100000)
        with _serving(blob):
            with self.assertRaisesRegex(FastPathUnavailable, "expands past"):
                fetch_and_decompress(self.URL, max_bytes=10000,
                                     expected=hashlib.sha1(blob).hexdigest())

    def test_download_past_the_cap_is_refused(self):
        blob = zlib.compress(bytes(range(256)) * 200, 0)
        with _serving(blob):
            with self.assertRaisesRegex(FastPathUnavailable, "exceeds"):
                fetch_and_decompress(self.URL, max_bytes=len(blob) - 1,
                                     expected=hashlib.sha1(blob).hexdigest())

    def test_object_larger_than_one_read_is_whole(self):
        body = hashlib.sha256(b"x").digest() * 200000   # ~6 MiB
        blob = zlib.compress(body, 0)
        with _serving(blob):
            self.assertEqual(fetch_and_decompress(
                self.URL, expected=hashlib.sha1(blob).hexdigest()), body)

    def test_truncated_stream_is_refused(self):
        blob = zlib.compress(self.BODY)[:-6]
        with _serving(blob):
            with self.assertRaisesRegex(FastPathUnavailable, "truncated"):
                fetch_and_decompress(self.URL,
                                     expected=hashlib.sha1(blob).hexdigest())


if __name__ == "__main__":
    unittest.main()
