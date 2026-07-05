# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for reachability GC of the shared store (bits_helpers/gc.py)."""

import os
import shutil
import tempfile
import unittest

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bits_helpers import certify, gc

ARCH = "slc7_x86-64"


def _key(arch, shard, h, name="pkg-1.tar.gz"):
    return "TARS/%s/store/%s/%s/%s" % (arch, shard, h, name)


class TestSafeStoreKey(unittest.TestCase):

    def test_valid_store_key(self):
        self.assertTrue(gc.safe_store_key(_key(ARCH, "aa", "aa11beef"), ARCH))

    def test_rejects_whitespace_and_control(self):
        self.assertFalse(gc.safe_store_key(_key(ARCH, "aa", "aa11") + " ", ARCH))
        self.assertFalse(gc.safe_store_key(_key(ARCH, "aa", "aa 11"), ARCH))
        self.assertFalse(gc.safe_store_key(_key(ARCH, "aa", "aa11") + "\n", ARCH))
        self.assertFalse(gc.safe_store_key(" " + _key(ARCH, "aa", "aa11"), ARCH))

    def test_rejects_shard_hash_mismatch(self):
        self.assertFalse(gc.safe_store_key(_key(ARCH, "zz", "aa11"), ARCH))

    def test_rejects_outside_store_tree(self):
        self.assertFalse(gc.safe_store_key("TARS/%s/aa11/x.tar.gz" % ARCH, ARCH))
        self.assertFalse(gc.safe_store_key("MANIFESTS/whatever.json", ARCH))
        self.assertFalse(gc.safe_store_key("TARS/%s/store/aa/aa11" % ARCH, ARCH))  # no file

    def test_rejects_path_traversal(self):
        self.assertFalse(gc.safe_store_key("TARS/%s/store/aa/../../etc" % ARCH, ARCH))

    def test_binds_to_architecture(self):
        k = _key(ARCH, "aa", "aa11")
        self.assertTrue(gc.safe_store_key(k, ARCH))
        self.assertFalse(gc.safe_store_key(k, "other_arch"))


class TestHashExtraction(unittest.TestCase):

    def test_hash_from_store_key(self):
        self.assertEqual(gc.hash_from_store_key(_key(ARCH, "aa", "aa11beef")), "aa11beef")

    def test_non_store_key_returns_none(self):
        self.assertIsNone(gc.hash_from_store_key("MANIFESTS/x.json"))


class TestPlanSweep(unittest.TestCase):

    def test_keeps_roots_sweeps_orphans(self):
        objs = [(_key(ARCH, "aa", "aa11"), 0), (_key(ARCH, "bb", "bb22"), 0)]
        plan = gc.plan_sweep(objs, {"aa11"}, now=10 ** 12, grace_seconds=0, architecture=ARCH)
        self.assertEqual(plan["delete"], [_key(ARCH, "bb", "bb22")])
        self.assertEqual(plan["kept"], 1)

    def test_grace_protects_young_objects(self):
        now = 1_000_000
        objs = [(_key(ARCH, "bb", "bb22"), now - 10)]      # 10s old
        plan = gc.plan_sweep(objs, set(), now=now, grace_seconds=3600, architecture=ARCH)
        self.assertEqual(plan["delete"], [])
        self.assertEqual(plan["young"], 1)

    def test_unsafe_keys_never_deleted(self):
        objs = [(_key(ARCH, "aa", "aa11") + " ", 0),      # trailing space
                ("TARS/%s/store/zz/aa11/x" % ARCH, 0)]     # shard mismatch
        plan = gc.plan_sweep(objs, set(), now=10 ** 12, grace_seconds=0, architecture=ARCH)
        self.assertEqual(plan["delete"], [])
        self.assertEqual(plan["unsafe"], 2)


class TestCollectGarbageFailClosed(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        priv = Ed25519PrivateKey.generate()
        self.key_pem = os.path.join(self.tmp, "signing.pem")
        with open(self.key_pem, "wb") as fh:
            fh.write(priv.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
        self.trust_dir = os.path.join(self.tmp, "keys")
        os.makedirs(self.trust_dir)
        with open(os.path.join(self.trust_dir, "pub.pem"), "wb") as fh:
            fh.write(priv.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo))
        self._old = os.environ.get("BITS_TRUST_KEYS")
        os.environ["BITS_TRUST_KEYS"] = self.trust_dir

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BITS_TRUST_KEYS", None)
        else:
            os.environ["BITS_TRUST_KEYS"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _signed_manifest(self, hashes):
        pkgs = [{"package": "p%d" % i, "version": "1", "revision": "1",
                 "effective_architecture": ARCH, "hash": h,
                 "tarball": "p.tar.gz", "tarball_sha256": "sha256:%s" % h}
                for i, h in enumerate(hashes)]
        out = os.path.join(self.tmp, "common.json")
        certify.certify([{"build_id": "b1", "packages": pkgs}], self.key_pem, out,
                        probe=lambda a, h: "sha256:%s" % h)
        return out

    def test_sweeps_only_unreferenced(self):
        manifest = self._signed_manifest(["aa11"])
        objs = [(_key(ARCH, "aa", "aa11"), 0), (_key(ARCH, "bb", "bb22"), 0)]
        deleted = []
        plan = gc.collect_garbage(
            objs, manifest, grace_seconds=0, now=10 ** 12,
            delete_fn=lambda ks: deleted.extend(ks) or len(ks),
            dry_run=False, architecture=ARCH)
        self.assertTrue(plan["verified"])
        self.assertEqual(deleted, [_key(ARCH, "bb", "bb22")])
        self.assertEqual(plan["deleted"], 1)

    def test_refuses_when_manifest_unverified(self):
        manifest = self._signed_manifest(["aa11"])
        os.remove(manifest + ".sig")            # break the signature
        objs = [(_key(ARCH, "bb", "bb22"), 0)]
        deleted = []
        plan = gc.collect_garbage(objs, manifest, now=10 ** 12,
                                  delete_fn=lambda ks: deleted.extend(ks),
                                  dry_run=False, architecture=ARCH)
        self.assertFalse(plan["verified"])
        self.assertEqual(deleted, [])           # nothing swept on unverifiable roots

    def test_refuses_empty_roots_without_override(self):
        manifest = self._signed_manifest([])    # verified but zero roots
        objs = [(_key(ARCH, "bb", "bb22"), 0)]
        deleted = []
        plan = gc.collect_garbage(objs, manifest, now=10 ** 12,
                                  delete_fn=lambda ks: deleted.extend(ks),
                                  dry_run=False, architecture=ARCH)
        self.assertTrue(plan["verified"])
        self.assertEqual(deleted, [])           # empty roots would wipe store -> refuse


if __name__ == "__main__":
    unittest.main()
