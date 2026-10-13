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
        # The shipped keys/key-policy.json is strict ("default": []); this harness
        # trusts its own generated key for every group ("default": ["*"] overrides
        # the shipped strict default, most-specific-last).
        with open(os.path.join(self.trust_dir, "key-policy.json"), "w") as fh:
            fh.write('{"default": ["*"]}\n')
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
                        probe=lambda a, h, t=None: "sha256:%s" % h)
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


class TestLocalizeManifest(unittest.TestCase):
    """gc._localize_manifest: a URL --trust-manifest must be downloaded (with its
    .sig) before verification — gc cannot open() a URL like the consumer path."""

    def setUp(self):
        import bits_helpers.download as dl
        dl._SIGNED.clear()
        self.addCleanup(dl._SIGNED.clear)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _serve(self, files, got=None):
        """A download._http_get stand-in: *files* maps a URL to its body (None:
        the fetch fails). Each body has the ETag "<body>"; a matching
        If-None-Match gets a 304."""
        def fake(url, headers, timeout=60):
            if got is not None:
                got.append(url)
            body = files.get(url)
            if body is None:
                raise OSError("unreachable %s" % url)
            tag = '"%s"' % body
            if headers.get("If-None-Match") == tag:
                return 304, None, tag
            return 200, body.encode(), tag
        return fake

    def _cached(self, name, body):
        d = os.path.join(self.tmp, "MANIFESTS", "gc")
        os.makedirs(d, exist_ok=True)
        for n, b in ((name, body), (name + ".sig", body + "-sig")):
            with open(os.path.join(d, n), "w") as fh:
                fh.write(b)
        return os.path.join(d, name)

    def test_a_cached_copy_is_replaced(self):
        from unittest.mock import patch
        url = "https://s3.example/MANIFESTS/common-manifest-x86_64-el9.json"
        import hashlib
        old = self._cached("%s.common-manifest-x86_64-el9.json"
                           % hashlib.sha256(url.encode()).hexdigest()[:12], "old")
        serve = self._serve({url: "new", url + ".sig": "new-sig"})
        with patch("bits_helpers.download._http_get", side_effect=serve):
            local = gc._localize_manifest(url, self.tmp)
        self.assertEqual(local, old)
        self.assertEqual((open(local).read(), open(local + ".sig").read()), ("new", "new-sig"))
        self.assertEqual([n for n in os.listdir(os.path.dirname(local)) if ".part" in n], [])

    def test_a_failed_fetch_never_falls_back_to_the_cached_copy(self):
        from unittest.mock import patch
        url = "https://s3.example/MANIFESTS/common-manifest-x86_64-el9.json"
        for files in ({}, {url: "new"}):                 # the manifest, or only its .sig
            with self.subTest(files=sorted(files)):
                import bits_helpers.download as dl
                dl._SIGNED.clear()
                import hashlib
                old = self._cached("%s.common-manifest-x86_64-el9.json"
                                   % hashlib.sha256(url.encode()).hexdigest()[:12], "old")
                with patch("bits_helpers.download._http_get",
                           side_effect=self._serve(files)):
                    with self.assertRaises(OSError):
                        gc._localize_manifest(url, self.tmp)
                self.assertNotEqual(open(old + ".sig").read(), "new-sig")

    def test_two_stores_one_name(self):
        from unittest.mock import patch
        a = "https://s3.example/one/MANIFESTS/common-manifest-shared.json"
        b = "https://s3.example/two/MANIFESTS/common-manifest-shared.json"
        serve = self._serve({a: "A", a + ".sig": "a", b: "B", b + ".sig": "b"})
        with patch("bits_helpers.download._http_get", side_effect=serve):
            pa, pb = gc._localize_manifest(a, self.tmp), gc._localize_manifest(b, self.tmp)
        self.assertEqual((open(pa).read(), open(pb).read()), ("A", "B"))

    def test_fetched_once_per_run(self):
        from unittest.mock import patch
        url = "https://s3.example/MANIFESTS/common-manifest-x86_64-el9.json"
        got = []
        with patch("bits_helpers.download._http_get",
                   side_effect=self._serve({url: "new", url + ".sig": "s"}, got)):
            a = gc._localize_manifest(url, self.tmp)
            b = gc._localize_manifest(url, self.tmp)
        self.assertEqual((a, got), (b, [url, url + ".sig"]))
        got.clear()
        with patch("bits_helpers.download._http_get", side_effect=self._serve({}, got)):
            with self.assertRaises(OSError):
                gc._localize_manifest(url + "x", self.tmp)
            with self.assertRaises(OSError):                 # the failure, not a retry
                gc._localize_manifest(url + "x", self.tmp)
        self.assertEqual(got, [url + "x"])

    def test_a_build_reuses_nothing_from_a_manifest_it_cannot_fetch(self):
        """The stale-cache case: a build node's MANIFESTS/trust holds an old,
        still valid copy; the store cannot be read. Nothing is trusted from it."""
        from unittest.mock import patch
        from bits_helpers import build
        url = "https://s3.example/MANIFESTS/common-manifest-x86_64-el9.json"
        d = os.path.join(self.tmp, "MANIFESTS", "trust")
        os.makedirs(d)
        for n in ("common-manifest-x86_64-el9.json", "common-manifest-x86_64-el9.json.sig"):
            open(os.path.join(d, n), "w").write("{}")
        with patch("bits_helpers.download._http_get", side_effect=self._serve({})), \
             patch("bits_helpers.trust.trusted_index",
                   side_effect=AssertionError("the cached copy was verified")):
            self.assertEqual(build._load_trusted_index(url, self.tmp, None), (None, {}))

    def test_local_path_passes_through(self):
        p = os.path.join(self.tmp, "common-manifest-shared.json")
        open(p, "w").close()
        self.assertEqual(gc._localize_manifest(p, self.tmp), p)

    def test_url_is_downloaded_with_sig(self):
        from unittest.mock import patch
        url = "https://s3.example/MANIFESTS/common-manifest-osx_arm64.json"
        got = []
        with patch("bits_helpers.download._http_get",
                   side_effect=self._serve({url: "{}", url + ".sig": "{}"}, got)):
            local = gc._localize_manifest(url, self.tmp)
        # Both the manifest and its detached signature are fetched...
        self.assertEqual(got, [url, url + ".sig"])
        # ...to a real local file, with the .sig sitting next to it.
        self.assertTrue(os.path.isfile(local))
        self.assertTrue(os.path.isfile(local + ".sig"))
        self.assertTrue(local.endswith("common-manifest-osx_arm64.json"))

    def test_a_weak_etag_is_never_sent_back(self):
        from unittest.mock import patch
        import bits_helpers.download as dl
        url = "https://s3.example/MANIFESTS/common-manifest-x86_64-el9.json"
        sent = []

        def weak(u, headers, timeout=60):
            sent.append(headers.get("If-None-Match"))
            return 200, b"v", 'W/"v"'
        for _ in range(2):
            dl._SIGNED.clear()
            with patch("bits_helpers.download._http_get", side_effect=weak):
                gc._localize_manifest(url, self.tmp)
        self.assertEqual(sent, [None] * 4)

    def test_a_truncated_body_is_a_failed_fetch(self):
        from http.client import IncompleteRead
        from unittest.mock import MagicMock, patch
        import bits_helpers.download as dl
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.read.side_effect = IncompleteRead(b"par", 10)
        with patch("bits_helpers.download.urlopen", return_value=resp):
            with self.assertRaises(OSError):
                dl._http_get("https://s3.example/x.json", {})

    def test_an_unchanged_manifest_is_not_downloaded_again(self):
        """The next run sends the ETag it kept: 304, and the local copy stays.
        A copy changed on disk, or a store copy that changed, is fetched in full."""
        from unittest.mock import patch
        import bits_helpers.download as dl
        url = "https://s3.example/MANIFESTS/common-manifest-x86_64-el9.json"
        files = {url: "v1", url + ".sig": "s1"}
        sent = []

        def run():
            dl._SIGNED.clear()
            serve = self._serve(files)

            def spy(u, headers, timeout=60):
                r = serve(u, headers, timeout)
                sent.append((u, headers.get("If-None-Match"), r[0]))
                return r
            with patch("bits_helpers.download._http_get", side_effect=spy):
                return gc._localize_manifest(url, self.tmp)
        local = run()
        sent.clear()
        self.assertEqual(run(), local)
        self.assertEqual([s[2] for s in sent], [304, 304])
        self.assertEqual(open(local).read(), "v1")
        files[url] = "v2"                                        # re-signed in the store
        files[url + ".sig"] = "s2"
        sent.clear()
        run()
        self.assertEqual([s[2] for s in sent], [200, 200])
        self.assertEqual((open(local).read(), open(local + ".sig").read()), ("v2", "s2"))
        with open(local, "w") as fh:                             # tampered with on disk
            fh.write("evil")
        sent.clear()
        run()
        self.assertEqual(sent[0][1:], (None, 200))               # no ETag sent: fetched in full
        self.assertEqual(open(local).read(), "v2")

if __name__ == "__main__":
    unittest.main()
