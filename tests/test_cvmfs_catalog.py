# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import sqlite3
import tempfile
import unittest

from bits_helpers.cvmfs_catalog import (
    FastPathUnavailable,
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


if __name__ == "__main__":
    unittest.main()
