# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""ADR-0005 P2c: the revision counter folds in (version, revision, hash) records
from the certified common manifest + S3 rev-index markers, additively.

Two layers are checked:
- trust.trusted_records verifies + returns the accepted package entries;
- build._fold_revision_records mirrors the version-link scan (hash match ->
  reuse candidate; mismatch -> reserved revision) so the reuse/assign decision
  is identical whether it is fed from links or from records.
"""

import os
import shutil
import tempfile
import types
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bits_helpers import build, certify, trust


def _pkg(package, h, sha, rev, arch="x86_64-el9", version="3.3.10"):
    return {"package": package, "version": version, "revision": rev,
            "effective_architecture": arch, "hash": h, "tarball_sha256": sha,
            "tarball": "%s.tar.gz" % package}


def _spec(remote=("bb", "aa"), local=()):
    # remote_hashes / local_hashes are ordered preference lists (better_tarball
    # indexes into them), earliest = most preferred.
    return {"package": "fftw", "version": "3.3.10", "is_devel_pkg": False,
            "remote_hashes": list(remote), "local_hashes": list(local)}


class TestTrustedRecords(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        priv = Ed25519PrivateKey.generate()
        self.key_pem = os.path.join(self.tmp, "signing.pem")
        with open(self.key_pem, "wb") as fh:
            fh.write(priv.private_bytes(serialization.Encoding.PEM,
                     serialization.PrivateFormat.PKCS8,
                     serialization.NoEncryption()))
        trust_dir = os.path.join(self.tmp, "keys")
        os.makedirs(trust_dir)
        with open(os.path.join(trust_dir, "pub.pem"), "wb") as fh:
            fh.write(priv.public_key().public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo))
        self._old = os.environ.get("BITS_TRUST_KEYS")
        os.environ["BITS_TRUST_KEYS"] = trust_dir

    def tearDown(self):
        if self._old is None:
            os.environ.pop("BITS_TRUST_KEYS", None)
        else:
            os.environ["BITS_TRUST_KEYS"] = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_trusted_records_returns_verified_entries(self):
        m = {"build_id": "b1", "packages": [
            _pkg("fftw", "h1", "sha256:aa", "1"),
            _pkg("fftw", "h2", "sha256:bb", "2")]}
        store = {("x86_64-el9", "h1"): "sha256:aa", ("x86_64-el9", "h2"): "sha256:bb"}
        out = os.path.join(self.tmp, "out", "common.json")
        out_path, _ = certify.certify([m], self.key_pem, out,
                                      probe=lambda a, h, t=None: store.get((a, h)))
        kid, entries = trust.trusted_records(out_path)
        self.assertIsNotNone(kid)
        # every entry carries the fields the rev-index needs
        self.assertEqual({(e["revision"], e["hash"]) for e in entries},
                         {("1", "h1"), ("2", "h2")})

    def test_trusted_records_fail_closed_on_tamper(self):
        import json
        m = {"build_id": "b1", "packages": [_pkg("fftw", "h1", "sha256:aa", "1")]}
        out = os.path.join(self.tmp, "out", "common.json")
        out_path, _ = certify.certify(
            [m], self.key_pem, out,
            probe=lambda a, h, t=None: "sha256:aa")
        with open(out_path) as fh:
            data = json.load(fh)
        data["packages"][0]["revision"] = "99"      # tamper after signing
        with open(out_path, "w") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        kid, entries = trust.trusted_records(out_path)
        self.assertIsNone(kid)
        self.assertEqual(entries, [])


class TestTrustUnavailable(unittest.TestCase):
    """When the signing library (cryptography) can't be imported, the reuse paths
    must fail CLOSED, not crash the build (regression for the ModuleNotFoundError
    that P2c's early trust import surfaced on runners without cryptography)."""

    def _no_trust(self):
        import builtins
        real = builtins.__import__

        def fake(name, *a, **k):
            if name == "bits_helpers.trust" or name == "cryptography" \
                    or name.startswith("cryptography."):
                raise ModuleNotFoundError("No module named 'cryptography'")
            return real(name, *a, **k)
        return patch.object(builtins, "__import__", side_effect=fake)

    def test_trusted_reuse_records_empty_and_cached(self):
        build._TRUST_IMPORT_WARNED = True  # silence the one-time warning in tests
        args = types.SimpleNamespace()
        with self._no_trust():
            self.assertEqual(build.trusted_reuse_records(args, "/tmp"), [])
        # cached miss -> a second call must not re-import (and stays empty)
        self.assertEqual(build.trusted_reuse_records(args, "/tmp"), [])

    def test_load_trusted_index_fails_closed(self):
        build._TRUST_IMPORT_WARNED = True
        with self._no_trust():
            self.assertEqual(build._load_trusted_index("s", "/tmp", None), (None, {}))


class TestFoldRevisionRecords(unittest.TestCase):
    """build._fold_revision_records — invoked by the counter only as a gap-fill,
    i.e. with candidate=None (the local version-link scan found nothing)."""

    def test_gap_fill_reuse_when_no_symlink_candidate(self):
        # rev 2 -> hash "bb", which this build wants (remote_hashes) and which the
        # local scan did not surface -> the record supplies the reuse candidate.
        cand, busy = build._fold_revision_records(
            [("2", "bb")], _spec(), None, set(), "")
        self.assertEqual(cand, ("2", "bb", None))
        self.assertEqual(busy, set())

    def test_same_revision_two_hashes_reuses_the_matching_one(self):
        # REGRESSION: revision 1 recorded twice (rebuilt after a recipe change).
        # The stale hash must NOT mask the usable one: reuse rev 1, do not assign
        # a fresh revision (which would unpack rev 1's tarball into rev 2's dir).
        cand, busy = build._fold_revision_records(
            [("1", "STALE"), ("1", "bb")], _spec(), None, set(), "")
        self.assertEqual(cand, ("1", "bb", None))
        # busy may hold 1 from the stale pair, but a candidate means we reuse,
        # never assign — so the revision stays 1.
        self.assertEqual(cand[0], "1")

    def test_picks_best_hash_among_records(self):
        # Multiple matching records: better_tarball prefers the hash earliest in
        # remote_hashes ("bb" before "aa"), regardless of revision number.
        cand, _ = build._fold_revision_records(
            [("9", "aa"), ("5", "bb")], _spec(remote=("bb", "aa")), None, set(), "")
        self.assertEqual(cand, ("5", "bb", None))

    def test_mismatch_reserves_revision_number(self):
        # rev 3 -> some other hash -> not reusable, but its number is taken.
        cand, busy = build._fold_revision_records(
            [("3", "ffff")], _spec(), None, set(), "")
        self.assertIsNone(cand)
        self.assertEqual(busy, {3})

    def test_local_mode_does_not_reserve_remote_numbers(self):
        # Read-only store: we assign localN, so plain remote ints are NOT busy
        # (revision_prefix="local"), matching the symlink loop's guard exactly.
        cand, busy = build._fold_revision_records(
            [("3", "ffff")], _spec(), None, set(), "local")
        self.assertIsNone(cand)
        self.assertEqual(busy, set())

    def test_assign_skips_busy_number_from_records(self):
        # End-to-end of the counter's assignment arithmetic: rev 1 taken by an
        # unrelated hash -> next free revision is 2.
        _, busy = build._fold_revision_records(
            [("1", "ffff")], _spec(), None, set(), "")
        nxt = min(set(range(1, max(busy) + 2)) - busy) if busy else 1
        self.assertEqual(nxt, 2)


class TestStoreRevisionRecords(unittest.TestCase):
    """build._store_revision_records — the store's own content-object NAMES.

    The name of TARS/<arch>/store/<h2>/<hash>/<pkg>-<ver>-<rev>.<arch>.tar.gz is
    the authoritative hash -> revision mapping, and the only source that can say
    what revision the store already gave to OUR hash.
    """

    ARCH = "x86_64-el10"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec(self, hashes=("a888a899",)):
        return {"package": "bzip2", "version": "1.0.6", "is_devel_pkg": False,
                "remote_hashes": list(hashes), "local_hashes": []}

    def _seed_local(self, pkg_hash, name):
        from bits_helpers.utilities import resolve_store_path
        d = os.path.join(self.tmp, resolve_store_path(self.ARCH, pkg_hash))
        os.makedirs(d, exist_ok=True)
        if name:
            open(os.path.join(d, name), "w").close()
        return d

    def test_reads_revision_from_local_object_name(self):
        self._seed_local("a888a899", "bzip2-1.0.6-1.%s.tar.gz" % self.ARCH)
        recs = build._store_revision_records(
            self._spec(), self.ARCH, self.tmp, types.SimpleNamespace())
        self.assertEqual(recs, [("1", "a888a899")])

    def test_empty_local_dir_does_not_suppress_remote_lookup(self):
        # REGRESSION: _prefetch_package makes the hash dir BEFORE downloading into
        # it. An empty local listing means "unknown", not "absent" -- if it short-
        # circuited the remote lookup, the counter would fall back to the stale
        # manifest pair and assign a fresh revision.
        self._seed_local("a888a899", None)          # dir exists, is empty
        helper = types.SimpleNamespace(
            list_store_tarballs=lambda a, h: ["bzip2-1.0.6-1.%s.tar.gz" % a])
        self.assertEqual(
            build._store_revision_records(self._spec(), self.ARCH, self.tmp, helper),
            [("1", "a888a899")])

    def test_missing_local_dir_falls_back_to_remote(self):
        helper = types.SimpleNamespace(
            list_store_tarballs=lambda a, h: ["bzip2-1.0.6-2.%s.tar.gz" % a])
        self.assertEqual(
            build._store_revision_records(self._spec(), self.ARCH, self.tmp, helper),
            [("2", "a888a899")])

    def test_stops_at_first_hash_in_preference_order(self):
        seen = []

        def _list(a, h):
            seen.append(h)
            return ["bzip2-1.0.6-1.%s.tar.gz" % a] if h == "PRIMARY" else []
        helper = types.SimpleNamespace(list_store_tarballs=_list)
        recs = build._store_revision_records(
            self._spec(("PRIMARY", "ALT")), self.ARCH, self.tmp, helper)
        self.assertEqual(recs, [("1", "PRIMARY")])
        self.assertEqual(seen, ["PRIMARY"])          # ALT never probed

    def test_two_revisions_under_one_hash_picks_lowest(self):
        # The upload HEAD-skip is keyed on the FULL key (hash + file name), so one
        # hash dir can hold two revision labels. Pick deterministically: the lowest
        # is the label that landed first, and the one earlier builds installed.
        self._seed_local("a888a899", "bzip2-1.0.6-3.%s.tar.gz" % self.ARCH)
        self._seed_local("a888a899", "bzip2-1.0.6-1.%s.tar.gz" % self.ARCH)
        recs = build._store_revision_records(
            self._spec(), self.ARCH, self.tmp, types.SimpleNamespace())
        self.assertEqual(recs, [("1", "a888a899")])

    def test_local_revision_objects_are_ignored(self):
        self._seed_local("a888a899", "bzip2-1.0.6-local2.%s.tar.gz" % self.ARCH)
        self.assertEqual(
            build._store_revision_records(self._spec(), self.ARCH, self.tmp,
                                          types.SimpleNamespace(list_store_tarballs=lambda a, h: [])), [])

    def test_no_lister_and_no_local_yields_nothing(self):
        self.assertEqual(
            build._store_revision_records(self._spec(), self.ARCH, self.tmp,
                                          types.SimpleNamespace(list_store_tarballs=lambda a, h: [])), [])

    def test_revisionless_object_is_not_a_reuse_record(self):
        self._seed_local("a888a899", "bzip2-1.0.6.%s.tar.gz" % self.ARCH)
        self.assertEqual(
            build._store_revision_records(self._spec(), self.ARCH, self.tmp,
                                          types.SimpleNamespace(list_store_tarballs=lambda a, h: [])), [])

    def test_store_name_beats_stale_marker_for_same_revision(self):
        # The bzip2 failure, end to end. The store holds bzip2-1.0.6-1.tar.gz under
        # hash a888a899; the write-once rev-index marker for revision 1 still holds
        # an older hash 07cbfa40 (a different dep closure). Without the store record
        # the fold sees only ("1", 07cbfa40) -> busy={1} -> revision 2 -> and then
        # fetch_tarball, which matches by HASH, unpacks the -1 tarball into -2.
        self._seed_local("a888a899", "bzip2-1.0.6-1.%s.tar.gz" % self.ARCH)
        spec = self._spec()
        store_recs = build._store_revision_records(
            spec, self.ARCH, self.tmp, types.SimpleNamespace())
        stale = [("1", "07cbfa40")]                  # from marker/manifest
        cand, _busy = build._fold_revision_records(
            store_recs + stale, spec, None, set(), "")
        self.assertEqual(cand, ("1", "a888a899", None))   # reuse rev 1, never assign 2

    def test_same_hash_stale_pair_cannot_outrank_the_object_name(self):
        # REGRESSION, and the reason ORDER alone is not enough: better_tarball
        # tie-breaks on the position of the hash in remote_hashes, so for two
        # records carrying the SAME hash the one folded LAST wins. Putting store
        # records first therefore makes them LOSE. _revision_index_records must
        # instead DROP manifest/marker pairs for any hash the store covers.
        spec = self._spec()
        store = [("1", "a888a899")]
        stale = [("2", "a888a899")]                  # same hash, wrong revision
        cand, _ = build._fold_revision_records(store + stale, spec, None, set(), "")
        self.assertEqual(cand[0], "2", "precondition: naive ordering loses the tie")

        with patch.object(build, "_store_revision_records", return_value=store), \
             patch.object(build, "trusted_reuse_records", return_value=[
                 {"package": "bzip2", "version": "1.0.6", "revision": "2",
                  "effective_architecture": self.ARCH, "hash": "a888a899"}]):
            helper = types.SimpleNamespace(
                read_rev_markers=lambda p, v, a: {"2": "a888a899",
                                                  "5": "OTHERHASH"})
            recs = build._revision_index_records(
                spec, self.ARCH, types.SimpleNamespace(), self.tmp, helper)
        # every pair mentioning our hash but the store's own name is gone...
        self.assertEqual([r for r in recs if r[1] == "a888a899"], [("1", "a888a899")])
        # ...while other hashes still reserve their revision numbers.
        self.assertIn(("5", "OTHERHASH"), recs)

        cand, _ = build._fold_revision_records(recs, spec, None, set(), "")
        self.assertEqual(cand, ("1", "a888a899", None))


class TestPrefetchPackage(unittest.TestCase):
    """build._prefetch_package runs in a pool whose results are never collected,
    so it must not raise on state the main loop has not produced yet, and it must
    record what it pulled from the REMOTE store so --require-signed-reuse still
    gates it (a prefetched tarball predates doBuild's own fetch_tarball call and
    would otherwise be mistaken for a trusted local artifact)."""

    ARCH = "x86_64-el10"

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_hash_is_a_clean_skip_not_a_keyerror(self):
        # The pool is submitted BEFORE the loop that assigns spec["hash"].
        spec = {"package": "bzip2", "is_devel_pkg": False}   # no "hash"
        helper = types.SimpleNamespace(
            fetch_tarball=lambda s: self.fail("must not fetch without a hash"))
        build._prefetch_package(spec, helper, self.tmp, self.ARCH)   # no raise
        self.assertNotIn("prefetched_tarballs", spec)

    def test_records_remotely_fetched_tarball(self):
        from bits_helpers.utilities import resolve_store_path
        h = "a888a899"
        spec = {"package": "bzip2", "version": "1.0.6", "hash": h,
                "is_devel_pkg": False}
        name = "bzip2-1.0.6-1.%s.tar.gz" % self.ARCH
        d = os.path.join(self.tmp, resolve_store_path(self.ARCH, h))
        os.makedirs(os.path.dirname(d), exist_ok=True)  # h2 prefix exists in real stores

        def _fetch(s):
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, name), "w").close()   # "download"
        helper = types.SimpleNamespace(fetch_tarball=_fetch)
        build._prefetch_package(spec, helper, self.tmp, self.ARCH)
        self.assertEqual([os.path.basename(t) for t in spec["prefetched_tarballs"]],
                         [name])


class TestSelectCachedTarball(unittest.TestCase):
    """build._select_cached_tarball — the unpacked tarball must be the one whose
    NAME matches the revision the counter assigned. fetch_tarball matches purely
    by hash, so without this guard a `-1` tarball is unpacked into a `-2` tree and
    build_template.sh's `mv TMP/<hash>/<pkg>/<ver>-<rev>` finds nothing."""

    ARCH = "x86_64-el10"

    def _spec(self, rev):
        return {"package": "bzip2", "version": "1.0.6", "revision": rev}

    def _t(self, name):
        return "/sw/TARS/%s/store/a8/a888a899/%s" % (self.ARCH, name)

    def test_picks_the_matching_revision(self):
        tars = [self._t("bzip2-1.0.6-1.%s.tar.gz" % self.ARCH),
                self._t("bzip2-1.0.6-2.%s.tar.gz" % self.ARCH)]
        self.assertEqual(
            build._select_cached_tarball(tars, self._spec("2"), self.ARCH), tars[1])

    def test_rejects_a_mismatched_revision_rather_than_unpacking_it(self):
        tars = [self._t("bzip2-1.0.6-1.%s.tar.gz" % self.ARCH)]
        with patch.object(build, "warning") as warn:
            self.assertEqual(
                build._select_cached_tarball(tars, self._spec("2"), self.ARCH), "")
        self.assertTrue(warn.called)

    def test_accepts_revisionless_tarball(self):
        tars = [self._t("bzip2-1.0.6.%s.tar.gz" % self.ARCH)]
        self.assertEqual(
            build._select_cached_tarball(tars, self._spec("2"), self.ARCH), tars[0])

    def test_exact_revision_preferred_over_revisionless(self):
        tars = [self._t("bzip2-1.0.6.%s.tar.gz" % self.ARCH),
                self._t("bzip2-1.0.6-2.%s.tar.gz" % self.ARCH)]
        self.assertEqual(
            build._select_cached_tarball(tars, self._spec("2"), self.ARCH), tars[1])

    def test_local_revision_matches(self):
        tars = [self._t("bzip2-1.0.6-local1.%s.tar.gz" % self.ARCH)]
        self.assertEqual(
            build._select_cached_tarball(tars, self._spec("local1"), self.ARCH), tars[0])

    def test_empty(self):
        self.assertEqual(build._select_cached_tarball([], self._spec("1"), self.ARCH), "")


if __name__ == "__main__":
    unittest.main()
