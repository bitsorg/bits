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


if __name__ == "__main__":
    unittest.main()
