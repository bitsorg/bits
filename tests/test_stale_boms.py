# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for bits_helpers/stale_boms.py (`bits store --stale-boms`)."""

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError

from bits_helpers import stale_boms as sb
from bits_helpers.utilities import resolve_store_path

ARCH = "x86_64-el9-gcc15-opt"


def _pkg(name, h, sha, revision="1", **kw):
    e = {"package": name, "revision": revision, "effective_architecture": ARCH,
         "hash": h, "tarball": "%s-1-%s.%s.tar.gz" % (name, revision, ARCH),
         "tarball_sha256": sha}
    e.update(kw)
    return e


def _key(e):
    return "%s/%s" % (resolve_store_path(e["effective_architecture"], e["hash"]), e["tarball"])


class FakeS3:
    """head_object/get_object over {key: (bytes, recorded_sha or None)}."""

    def __init__(self, objects, fail=(), list_fails=False):
        self.objects, self.fail, self.heads = objects, set(fail), []
        self.list_fails = list_fails

    def list_objects_v2(self, Bucket, Prefix):
        if self.list_fails:
            raise ClientError({"Error": {"Code": "503"}}, "ListObjectsV2")
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}

    def head_object(self, Bucket, Key):
        self.heads.append(Key)
        if Key in self.fail:
            raise ClientError({"Error": {"Code": "503"}}, "HeadObject")
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        _data, sha = self.objects[Key]
        return {"Metadata": {"sha256": sha} if sha else {}}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key][0])}


class TestClassify(unittest.TestCase):

    def _lookup(self, table):
        return lambda e: table.get(e["hash"], sb.MISSING)

    def test_states_counted(self):
        bom = {"packages": [_pkg("A", "h1", "sha256:aa"), _pkg("B", "h2", "sha256:bb"),
                            _pkg("C", "h3", "sha256:cc"), _pkg("D", "h4", "sha256:dd")]}
        c = sb.classify(bom, self._lookup({"h1": "aa", "h2": "sha256:XX", "h4": None}))
        self.assertEqual((c["ok"], c["stale"], c["missing"], c["unknown"], c["judged"]),
                         (1, 1, 1, 1, 4))
        self.assertFalse(c["fully_stale"])

    def test_stale_plus_missing_is_fully_stale(self):
        bom = {"packages": [_pkg("A", "h1", "sha256:aa"), _pkg("B", "h2", "sha256:bb")]}
        c = sb.classify(bom, self._lookup({"h1": "sha256:new"}))
        self.assertTrue(c["fully_stale"])

    def test_all_missing_is_not_stale(self):
        # Absence alone is also what checking against the wrong store looks like.
        bom = {"packages": [_pkg("A", "h1", "sha256:aa"), _pkg("B", "h2", "sha256:bb")]}
        c = sb.classify(bom, self._lookup({}))
        self.assertEqual(c["missing"], 2)
        self.assertFalse(c["fully_stale"])

    def test_entry_without_tarball_or_arch_is_unknown(self):
        # certify still validates such entries (by guessing the object); never
        # let them be deleted unchecked.
        no_tar = _pkg("B", "h2", "sha256:bb"); del no_tar["tarball"]
        no_arch = _pkg("C", "h3", "sha256:cc"); del no_arch["effective_architecture"]
        bom = {"packages": [_pkg("A", "h1", "sha256:aa"), no_tar, no_arch]}
        c = sb.classify(bom, self._lookup({"h1": "sha256:new"}))
        self.assertEqual((c["stale"], c["unknown"]), (1, 2))
        self.assertFalse(c["fully_stale"])

    def test_one_confirmed_entry_keeps_the_bom(self):
        bom = {"packages": [_pkg("A", "h1", "sha256:aa"), _pkg("B", "h2", "sha256:bb")]}
        self.assertFalse(sb.classify(bom, self._lookup({"h1": "sha256:aa"}))["fully_stale"])

    def test_unknown_checksum_keeps_the_bom(self):
        # Fail-safe: never select a BOM we could not fully check.
        bom = {"packages": [_pkg("A", "h1", "sha256:aa"), _pkg("B", "h2", "sha256:bb")]}
        self.assertFalse(sb.classify(bom, self._lookup({"h2": None}))["fully_stale"])

    def test_unjudgeable_entries_ignored_and_empty_bom_not_stale(self):
        bom = {"packages": [_pkg("L", "h1", "sha256:aa", revision="local1"),
                            {"package": "NoSha", "hash": "h2",
                             "effective_architecture": ARCH},
                            "junk"]}
        c = sb.classify(bom, self._lookup({}))
        self.assertEqual(c["judged"], 0)
        self.assertFalse(c["fully_stale"])


class TestStoreLookup(unittest.TestCase):

    def test_missing_recorded_unknown_and_error(self):
        ok, rec, bare, err = (_pkg("A", "h1", "sha256:aa"), _pkg("B", "h2", "x"),
                              _pkg("C", "h3", "x"), _pkg("D", "h4", "x"))
        s3 = FakeS3({_key(rec): (b"b", "sha256:bb"), _key(bare): (b"c", None),
                     _key(err): (b"d", "sha256:dd")}, fail={_key(err)})
        look = sb.store_lookup(s3, "bkt")
        self.assertEqual(look(ok), sb.MISSING)
        self.assertEqual(look(rec), "sha256:bb")
        self.assertIsNone(look(bare))          # no recorded sha256, not --deep
        self.assertIsNone(look(err))           # a non-404 error is unknown, not missing
        look(rec)
        self.assertEqual(s3.heads.count(_key(rec)), 1)   # memoised

    def test_404_with_other_tarball_in_hash_dir_is_stale(self):
        old, new = _pkg("A", "h1", "sha256:aa"), _pkg("A", "h1", "sha256:bb", revision="2")
        s3 = FakeS3({_key(new): (b"n", "sha256:bb")})
        self.assertEqual(sb.store_lookup(s3, "bkt")(old), sb.STALE)
        self.assertEqual(sb.classify({"packages": [old]}, sb.store_lookup(s3, "bkt"))
                         ["fully_stale"], True)

    def test_404_with_failed_listing_is_unknown(self):
        s3 = FakeS3({}, list_fails=True)
        self.assertIsNone(sb.store_lookup(s3, "bkt")(_pkg("A", "h1", "x")))

    def test_deep_hashes_objects_without_recorded_sha(self):
        import hashlib
        bare = _pkg("C", "h3", "x")
        s3 = FakeS3({_key(bare): (b"payload", None)})
        self.assertEqual(sb.store_lookup(s3, "bkt", deep=True)(bare),
                         "sha256:" + hashlib.sha256(b"payload").hexdigest())


class TestLoadLocalBoms(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, doc):
        path = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(doc if isinstance(doc, str) else json.dumps(doc))
        return path

    def test_reads_boms_skips_signed_git_and_junk(self):
        bom = self._write("manifests/testbed/b1.x.json",
                          {"build_id": "b1", "packages": [_pkg("A", "h1", "aa"), 3]})
        self._write("manifests/testbed/common.json", {"kind": "common-manifest", "packages": []})
        self._write("manifests/testbed/bad.json", "{not json")
        self._write("manifests/testbed/list.json", "[1, 2]")
        self._write(".git/objects/x.json", {"build_id": "git", "packages": []})
        self._write("manifests/testbed/notes.txt", "x")
        boms = sb.load_local_boms(self.tmp)
        self.assertEqual([(b["key"], b["build_id"], len(b["packages"])) for b in boms],
                         [(bom, "b1", 1)])



def _load_bitsstore():
    """Exec bitsStore's embedded Python (its heredoc) as a module namespace."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "bitsStore")) as fh:
        text = fh.read()
    code = text.split("<<'PY'\n", 1)[1].rsplit("\nPY\n", 1)[0]
    ns = {"__name__": "bitsstore_under_test"}
    with patch.dict(os.environ, {"BITSSTORE_STORE": "https://s3.invalid/bucket"}):
        exec(compile(code, "bitsStore", "exec"), ns)   # pylint: disable=exec-used
    return ns


class TestBitsStoreStaleBoms(unittest.TestCase):
    """The ls/rm --stale-boms flow of bitsStore, against a fake store."""

    @classmethod
    def setUpClass(cls):
        cls.bs = _load_bitsstore()

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cur = _pkg("A", "h1", "sha256:new")
        self.s3 = FakeS3({_key(self.cur): (b"a", "sha256:new"),
                          _key(_pkg("B", "h2", "x")): (b"b", "sha256:bb")})
        self.old = self._bom("old", [_pkg("A", "h1", "sha256:old"), _pkg("Z", "h9", "zz")])
        self.new = self._bom("new", [self.cur, _pkg("B", "h2", "sha256:bb")])
        self.part = self._bom("part", [_pkg("A", "h1", "sha256:old"),
                                       _pkg("B", "h2", "sha256:bb")])
        self.gone = self._bom("gone", [_pkg("Z", "h9", "zz")])      # absent only: kept

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _bom(self, bid, pkgs):
        path = os.path.join(self.tmp, "manifests", "testbed", bid + ".json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump({"build_id": bid, "packages": pkgs}, fh)
        return path

    def _args(self, **kw):
        from types import SimpleNamespace
        a = dict(manifests_dir=self.tmp, deep=False, arch=[], build=None,
                 certified_by=None, expired=False, dry_run=False, yes=True,
                 force=False)
        a.update(kw)
        return SimpleNamespace(**a)

    def _report(self, args, store=None):
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            keys = self.bs["stale_bom_report"](store, self.s3, "bkt", args)
        return keys, out.getvalue()

    def test_local_report_selects_only_fully_stale(self):
        keys, out = self._report(self._args())
        self.assertEqual(set(keys), {self.old})
        self.assertIn("KEPT", out)
        self.assertIn("1 stale and 2 partly stale of 4", out)

    def test_store_confirming_nothing_refuses_without_force(self):
        # Checked against the wrong store: nothing matches anywhere, so refuse.
        os.remove(self.new)
        os.remove(self.part)
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()) as err:
            keys, _ = self._report(self._args())
        self.assertEqual(keys, {})
        self.assertIn("wrong store", err.getvalue())
        keys, _ = self._report(self._args(force=True))
        self.assertEqual(set(keys), {self.old})

    def test_filters_by_build_and_arch(self):
        self.assertEqual(self._report(self._args(build="new"))[0], {})
        self.assertEqual(self._report(self._args(arch=["other-arch"]))[0], {})

    def test_local_delete_dry_run_then_real(self):
        keys, _ = self._report(self._args())
        from contextlib import redirect_stdout
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.bs["delete_local_files"](keys, self._args(dry_run=True)), 0)
            self.assertTrue(os.path.exists(self.old))
            self.assertEqual(self.bs["delete_local_files"](keys, self._args()), 0)
        self.assertFalse(os.path.exists(self.old))
        self.assertTrue(os.path.exists(self.new) and os.path.exists(self.part))

    def test_s3_mode_reads_store_boms(self):
        store = self.bs["Store"](self.s3, "bkt")
        store._loaded = True
        store.boms = [{"key": "MANIFESTS/old/x.json", "build_id": "old",
                       "packages": [_pkg("A", "h1", "sha256:old")]},
                      {"key": "MANIFESTS/new/x.json", "build_id": "new",
                       "packages": [self.cur]}]
        keys, _ = self._report(self._args(manifests_dir=None), store=store)
        self.assertEqual(set(keys), {"MANIFESTS/old/x.json"})


if __name__ == "__main__":
    unittest.main()
