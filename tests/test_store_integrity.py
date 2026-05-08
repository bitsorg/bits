"""Tests for bits_helpers.store_integrity — local tarball integrity ledger.

Coverage:
    record_tarball_checksum  — happy path, missing tarball, idempotent, overwrite warning
    verify_tarball_checksum  — match, no ledger (warn), no ledger (strict), mismatch (fatal)
    _ledger_path             — path structure mirrors resolve_store_path
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

# Ensure the repo root is on sys.path so we can import bits_helpers directly.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bits_helpers import store_integrity as si
from bits_helpers.store_integrity import (
    LEDGER_SUBDIR,
    _ledger_path,
    _tarball_name,
    record_tarball_checksum,
    verify_tarball_checksum,
)
from bits_helpers.utilities import resolve_store_path


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_spec(pkg="MyPkg", version="1.0", revision="1", pkg_hash="abcd1234" * 5,
               architecture="slc7_x86-64"):
    return {
        "package":      pkg,
        "version":      version,
        "revision":     revision,
        "hash":         pkg_hash,
        "architecture": architecture,
    }


def _write_file(path: str, content: bytes = b"fake tarball content") -> str:
    """Create *path* with *content*; return path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


def _sha256(content: bytes) -> str:
    h = hashlib.sha256(content)
    return "sha256:" + h.hexdigest()


# ── _ledger_path ──────────────────────────────────────────────────────────────

class TestLedgerPath(unittest.TestCase):
    """_ledger_path must mirror resolve_store_path under STORE_CHECKSUMS/."""

    def test_path_structure(self):
        spec = _make_spec()
        arch = spec["architecture"]
        tarball = _tarball_name(spec, arch)
        ledger = _ledger_path("/work", arch, spec["hash"], tarball)
        store_rel = resolve_store_path(arch, spec["hash"])
        expected = os.path.join("/work", LEDGER_SUBDIR, store_rel, tarball + ".sha256")
        self.assertEqual(ledger, expected)

    def test_different_hashes_give_different_paths(self):
        spec_a = _make_spec(pkg_hash="aaaa" * 10)
        spec_b = _make_spec(pkg_hash="bbbb" * 10)
        arch = spec_a["architecture"]
        tarball = _tarball_name(spec_a, arch)
        path_a = _ledger_path("/work", arch, spec_a["hash"], tarball)
        tarball_b = _tarball_name(spec_b, arch)
        path_b = _ledger_path("/work", arch, spec_b["hash"], tarball_b)
        self.assertNotEqual(path_a, path_b)


# ── record_tarball_checksum ───────────────────────────────────────────────────

class TestRecordTarballChecksum(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.spec = _make_spec()
        self.arch = self.spec["architecture"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _local_tar_path(self):
        store_rel = resolve_store_path(self.arch, self.spec["hash"])
        tarball = _tarball_name(self.spec, self.arch)
        return os.path.join(self.tmp, store_rel, tarball)

    def _ledger(self):
        tarball = _tarball_name(self.spec, self.arch)
        return _ledger_path(self.tmp, self.arch, self.spec["hash"], tarball)

    def test_records_correct_digest(self):
        content = b"build product bytes"
        _write_file(self._local_tar_path(), content)
        record_tarball_checksum(self.spec, self.tmp, self.arch)
        ledger = self._ledger()
        self.assertTrue(os.path.isfile(ledger))
        with open(ledger) as fh:
            recorded = fh.read().strip()
        self.assertEqual(recorded, _sha256(content))

    def test_no_op_when_tarball_missing(self):
        """record should be a no-op and not raise when the local tarball is absent."""
        record_tarball_checksum(self.spec, self.tmp, self.arch)
        self.assertFalse(os.path.isfile(self._ledger()))

    def test_idempotent_same_digest(self):
        """Recording the same tarball twice should not overwrite or warn."""
        content = b"stable content"
        _write_file(self._local_tar_path(), content)
        record_tarball_checksum(self.spec, self.tmp, self.arch)
        mtime_after_first = os.path.getmtime(self._ledger())
        record_tarball_checksum(self.spec, self.tmp, self.arch)
        mtime_after_second = os.path.getmtime(self._ledger())
        # Ledger must not be rewritten on idempotent call.
        self.assertEqual(mtime_after_first, mtime_after_second)

    def test_overwrite_warns_on_different_digest(self):
        """A pre-existing ledger with a different digest must trigger a warning."""
        content_v1 = b"version 1"
        content_v2 = b"version 2"
        _write_file(self._local_tar_path(), content_v1)
        record_tarball_checksum(self.spec, self.tmp, self.arch)

        # Simulate a new tarball written to the same path (e.g. rebuild)
        _write_file(self._local_tar_path(), content_v2)
        with patch("bits_helpers.store_integrity.warning") as mock_warn:
            record_tarball_checksum(self.spec, self.tmp, self.arch)
            mock_warn.assert_called_once()

        # Ledger must now hold the new digest.
        with open(self._ledger()) as fh:
            recorded = fh.read().strip()
        self.assertEqual(recorded, _sha256(content_v2))

    def test_ledger_written_atomically(self):
        """The ledger file must be written via atomic rename (no partial reads)."""
        content = b"atomic write check"
        _write_file(self._local_tar_path(), content)
        # Capture the real os.replace BEFORE the patch replaces it on the
        # os module object (patching is global — it modifies os.replace itself).
        real_replace = os.replace
        with patch("bits_helpers.store_integrity.os.replace") as mock_replace:
            mock_replace.side_effect = real_replace  # delegate to captured real impl
            record_tarball_checksum(self.spec, self.tmp, self.arch)
            mock_replace.assert_called_once()


# ── verify_tarball_checksum ───────────────────────────────────────────────────

class TestVerifyTarballChecksum(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.spec = _make_spec()
        self.arch = self.spec["architecture"]
        content = b"legitimate tarball"
        store_rel = resolve_store_path(self.arch, self.spec["hash"])
        tarball = _tarball_name(self.spec, self.arch)
        self.local_tar = _write_file(
            os.path.join(self.tmp, store_rel, tarball), content
        )
        self.good_digest = _sha256(content)
        self.ledger = _ledger_path(self.tmp, self.arch, self.spec["hash"], tarball)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("BITS_STRICT_STORE_INTEGRITY", None)

    def _write_ledger(self, digest: str):
        os.makedirs(os.path.dirname(self.ledger), exist_ok=True)
        with open(self.ledger, "w") as fh:
            fh.write(digest + "\n")

    # Happy path ──────────────────────────────────────────────────────────────

    def test_match_passes_silently(self):
        """A matching ledger entry must not raise or exit."""
        self._write_ledger(self.good_digest)
        # Should complete without exception.
        verify_tarball_checksum(self.spec, self.tmp, self.arch, self.local_tar)

    # Missing ledger ──────────────────────────────────────────────────────────

    def test_missing_ledger_warns_and_records(self):
        """First recall with no ledger: warn, record digest, do not exit."""
        with patch("bits_helpers.store_integrity.warning") as mock_warn:
            verify_tarball_checksum(self.spec, self.tmp, self.arch, self.local_tar)
            mock_warn.assert_called_once()
        self.assertTrue(os.path.isfile(self.ledger))
        with open(self.ledger) as fh:
            recorded = fh.read().strip()
        self.assertEqual(recorded, self.good_digest)

    def test_missing_ledger_strict_mode_exits(self):
        """BITS_STRICT_STORE_INTEGRITY=1 must make a missing ledger fatal."""
        os.environ["BITS_STRICT_STORE_INTEGRITY"] = "1"
        with self.assertRaises(SystemExit):
            verify_tarball_checksum(self.spec, self.tmp, self.arch, self.local_tar)

    def test_missing_tarball_is_noop(self):
        """verify should be a no-op when the local tarball file is absent."""
        absent = self.local_tar + ".gone"
        # Must not raise even if there is no ledger entry.
        verify_tarball_checksum(self.spec, self.tmp, self.arch, absent)

    # Mismatch ────────────────────────────────────────────────────────────────

    def test_mismatch_exits(self):
        """A digest mismatch must always be fatal (SystemExit)."""
        self._write_ledger("sha256:" + "ff" * 32)
        with self.assertRaises(SystemExit):
            verify_tarball_checksum(self.spec, self.tmp, self.arch, self.local_tar)

    def test_mismatch_logs_both_digests(self):
        """The error message must include both expected and actual digests."""
        bad_digest = "sha256:" + "ee" * 32
        self._write_ledger(bad_digest)
        logged = []
        with patch("bits_helpers.store_integrity.error",
                   side_effect=lambda msg, *a: logged.append(msg % a)):
            with self.assertRaises(SystemExit):
                verify_tarball_checksum(self.spec, self.tmp, self.arch, self.local_tar)
        full_msg = "\n".join(logged)
        self.assertIn(bad_digest, full_msg)
        self.assertIn(self.good_digest, full_msg)

    def test_mismatch_strict_mode_irrelevant(self):
        """Mismatch is fatal regardless of BITS_STRICT_STORE_INTEGRITY."""
        os.environ["BITS_STRICT_STORE_INTEGRITY"] = "0"
        self._write_ledger("sha256:" + "00" * 32)
        with self.assertRaises(SystemExit):
            verify_tarball_checksum(self.spec, self.tmp, self.arch, self.local_tar)


# ── Integration: record then verify ──────────────────────────────────────────

class TestRoundTrip(unittest.TestCase):
    """record followed by verify must pass; tampering must be detected."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.spec = _make_spec()
        self.arch = self.spec["architecture"]
        content = b"genuine build artifact"
        store_rel = resolve_store_path(self.arch, self.spec["hash"])
        tarball = _tarball_name(self.spec, self.arch)
        self.local_tar = _write_file(
            os.path.join(self.tmp, store_rel, tarball), content
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_upload_then_recall_passes(self):
        record_tarball_checksum(self.spec, self.tmp, self.arch)
        verify_tarball_checksum(self.spec, self.tmp, self.arch, self.local_tar)

    def test_tampered_tarball_detected(self):
        record_tarball_checksum(self.spec, self.tmp, self.arch)
        # Simulate backend replacing the tarball.
        with open(self.local_tar, "wb") as fh:
            fh.write(b"trojanised content injected by attacker")
        with self.assertRaises(SystemExit):
            verify_tarball_checksum(self.spec, self.tmp, self.arch, self.local_tar)


if __name__ == "__main__":
    unittest.main()
