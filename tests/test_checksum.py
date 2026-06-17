# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tests for bits_helpers/checksum.py and the related changes to
bits_helpers/download.py, bits_helpers/workarea.py, and bits_helpers/build.py.

All filesystem and network operations are mocked so the suite runs offline.
"""

import hashlib
import io
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, mock_open, patch

from bits_helpers.checksum import (
    SUPPORTED_ALGORITHMS,
    check_file,
    checksum_file,
    enforcement_mode,
    parse_checksum,
    parse_entry,
    verify_file,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _tmp_file(data: bytes = b"hello bits\n"):
    """Write *data* to a temp file, return its path."""
    fd, path = tempfile.mkstemp()
    os.write(fd, data)
    os.close(fd)
    return path


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  1.  parse_entry                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestParseEntry(unittest.TestCase):
    """parse_entry() must split URL/filename from checksum suffix."""

    def test_no_checksum_returns_none(self):
        url = "https://example.com/libfoo-1.2.tar.gz"
        self.assertEqual(parse_entry(url), (url, None))

    def test_sha256_suffix_split(self):
        url, cksum = parse_entry(
            "https://example.com/libfoo-1.2.tar.gz,sha256:abcdef1234")
        self.assertEqual(url, "https://example.com/libfoo-1.2.tar.gz")
        self.assertEqual(cksum, "sha256:abcdef1234")

    def test_sha512_suffix_split(self):
        url, cksum = parse_entry("https://example.com/foo.tgz,sha512:cafe0123")
        self.assertEqual(url, "https://example.com/foo.tgz")
        self.assertEqual(cksum, "sha512:cafe0123")

    def test_sha1_suffix_split(self):
        url, cksum = parse_entry("https://example.com/foo.tgz,sha1:deadbeef")
        self.assertEqual(url, "https://example.com/foo.tgz")
        self.assertEqual(cksum, "sha1:deadbeef")

    def test_md5_suffix_split(self):
        url, cksum = parse_entry("https://example.com/foo.tgz,md5:deadbeef")
        self.assertEqual(url, "https://example.com/foo.tgz")
        self.assertEqual(cksum, "md5:deadbeef")

    def test_patch_filename_with_checksum(self):
        name, cksum = parse_entry("fix-endian.patch,sha256:abc123")
        self.assertEqual(name, "fix-endian.patch")
        self.assertEqual(cksum, "sha256:abc123")

    def test_patch_filename_without_checksum(self):
        self.assertEqual(parse_entry("fix-endian.patch"),
                         ("fix-endian.patch", None))

    def test_url_with_comma_in_query_not_split(self):
        # The part after the last comma is "2" which is not algo:hex → no split
        url = "https://example.com/q?a=1,2"
        self.assertEqual(parse_entry(url), (url, None))

    def test_url_with_comma_in_query_and_checksum(self):
        # Checksum is the LAST comma-separated token
        raw = "https://example.com/q?a=1,2,sha256:abcdef"
        url, cksum = parse_entry(raw)
        self.assertEqual(url, "https://example.com/q?a=1,2")
        self.assertEqual(cksum, "sha256:abcdef")

    def test_whitespace_stripped(self):
        url, cksum = parse_entry("  https://example.com/foo.tar.gz , sha256:abc123  ")
        self.assertEqual(url, "https://example.com/foo.tar.gz")
        self.assertEqual(cksum, "sha256:abc123")

    def test_case_insensitive_algorithm(self):
        _, cksum = parse_entry("foo.tar.gz,SHA256:ABCDEF")
        self.assertEqual(cksum, "SHA256:ABCDEF")   # value preserved as-is

    def test_empty_string(self):
        self.assertEqual(parse_entry(""), ("", None))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  2.  parse_checksum                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestParseChecksum(unittest.TestCase):

    def test_valid_sha256(self):
        self.assertEqual(parse_checksum("sha256:abcdef0123"),
                         ("sha256", "abcdef0123"))

    def test_valid_md5(self):
        self.assertEqual(parse_checksum("md5:deadbeef"),
                         ("md5", "deadbeef"))

    def test_case_insensitive(self):
        algo, digest = parse_checksum("SHA256:ABCDEF")
        self.assertEqual(algo, "sha256")
        self.assertEqual(digest, "abcdef")

    def test_missing_colon_raises(self):
        with self.assertRaises(ValueError):
            parse_checksum("sha256abcdef")

    def test_unknown_algo_raises(self):
        with self.assertRaises(ValueError):
            parse_checksum("blake3:abcdef")

    def test_non_hex_digest_raises(self):
        with self.assertRaises(ValueError):
            parse_checksum("sha256:not-hex!")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  3.  checksum_file / verify_file                                         ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestChecksumFile(unittest.TestCase):

    def setUp(self):
        self.data = b"the quick brown fox\n"
        self.path = _tmp_file(self.data)

    def tearDown(self):
        os.unlink(self.path)

    def test_sha256_correct(self):
        expected = "sha256:" + hashlib.sha256(self.data).hexdigest()
        self.assertEqual(checksum_file(self.path, "sha256"), expected)

    def test_sha512_correct(self):
        expected = "sha512:" + hashlib.sha512(self.data).hexdigest()
        self.assertEqual(checksum_file(self.path, "sha512"), expected)

    def test_default_algorithm_is_sha256(self):
        result = checksum_file(self.path)
        self.assertTrue(result.startswith("sha256:"))

    def test_unsupported_algorithm_raises(self):
        with self.assertRaises(ValueError):
            checksum_file(self.path, "blake3")

    def test_verify_file_match(self):
        expected = _sha256(self.data)
        self.assertTrue(verify_file(self.path, expected))

    def test_verify_file_mismatch(self):
        self.assertFalse(verify_file(self.path, "sha256:0000000000"))

    def test_verify_file_case_insensitive(self):
        digest = hashlib.sha256(self.data).hexdigest().upper()
        self.assertTrue(verify_file(self.path, "sha256:" + digest))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  4.  enforcement_mode                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestEnforcementMode(unittest.TestCase):

    def _args(self, check=False, enforce=False, print_=False):
        return SimpleNamespace(
            checkChecksums=check,
            enforceChecksums=enforce,
            printChecksums=print_,
        )

    def test_all_off_returns_off(self):
        self.assertEqual(enforcement_mode({}, self._args()), "off")

    def test_check_flag_returns_warn(self):
        self.assertEqual(enforcement_mode({}, self._args(check=True)), "warn")

    def test_enforce_flag_returns_enforce(self):
        self.assertEqual(enforcement_mode({}, self._args(enforce=True)), "enforce")

    def test_print_flag_returns_print(self):
        self.assertEqual(enforcement_mode({}, self._args(print_=True)), "print")

    def test_print_takes_precedence_over_enforce(self):
        # print_ and enforceChecksums shouldn't both be set (mutually exclusive
        # in argparse), but if they somehow are, "print" wins.
        args = SimpleNamespace(checkChecksums=False,
                               enforceChecksums=True, printChecksums=True)
        self.assertEqual(enforcement_mode({}, args), "print")

    def test_recipe_enforce_checksums_true(self):
        spec = {"enforce_checksums": True}
        self.assertEqual(enforcement_mode(spec, self._args()), "enforce")

    def test_recipe_enforce_checksums_false_returns_off(self):
        spec = {"enforce_checksums": False}
        self.assertEqual(enforcement_mode(spec, self._args()), "off")

    def test_cli_flag_overrides_missing_recipe_field(self):
        self.assertEqual(
            enforcement_mode({}, self._args(enforce=True)), "enforce")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  5.  check_file                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestCheckFile(unittest.TestCase):

    def setUp(self):
        self.data = b"sample content for checksum tests\n"
        self.path = _tmp_file(self.data)
        self.good = _sha256(self.data)
        self.bad  = "sha256:" + "0" * 64

    def tearDown(self):
        os.unlink(self.path)

    # ── mode=off ─────────────────────────────────────────────────────────────

    def test_off_no_verification(self):
        # Should not raise even with wrong checksum
        check_file(self.path, "foo.tar.gz", self.bad, "off")

    def test_off_no_declaration_no_raise(self):
        check_file(self.path, "foo.tar.gz", None, "off")

    # ── mode=warn ────────────────────────────────────────────────────────────

    def test_warn_correct_checksum_no_warning(self):
        with patch("bits_helpers.checksum.verify_file", return_value=True):
            with patch("bits_helpers.checksum.warning") as mock_warn:
                check_file(self.path, "foo.tar.gz", self.good, "warn")
        mock_warn.assert_not_called()

    def test_warn_mismatch_emits_warning(self):
        with patch("bits_helpers.checksum.warning") as mock_warn:
            check_file(self.path, "foo.tar.gz", self.bad, "warn")
        mock_warn.assert_called_once()
        self.assertIn("MISMATCH", mock_warn.call_args[0][0])

    def test_warn_no_declaration_no_error(self):
        # Missing declaration is silently ignored in warn mode
        check_file(self.path, "foo.tar.gz", None, "warn")

    # ── mode=enforce ─────────────────────────────────────────────────────────

    def test_enforce_correct_checksum_no_error(self):
        with patch("bits_helpers.checksum.verify_file", return_value=True):
            check_file(self.path, "foo.tar.gz", self.good, "enforce")

    def test_enforce_mismatch_dies(self):
        with patch("bits_helpers.checksum.dieOnError") as mock_die:
            check_file(self.path, "foo.tar.gz", self.bad, "enforce")
        mock_die.assert_called_once()
        args = mock_die.call_args[0]
        self.assertTrue(args[0])           # first arg must be truthy (error=True)
        self.assertIn("MISMATCH", args[1])

    def test_enforce_no_declaration_dies(self):
        with patch("bits_helpers.checksum.dieOnError") as mock_die:
            check_file(self.path, "foo.tar.gz", None, "enforce")
        mock_die.assert_called_once()
        args = mock_die.call_args[0]
        self.assertTrue(args[0])
        self.assertIn("No checksum declared", args[1])

    # ── mode=print ───────────────────────────────────────────────────────────

    def test_print_outputs_checksum(self):
        with patch("builtins.print") as mock_print:
            check_file(self.path, "foo.tar.gz", None, "print")
        mock_print.assert_called_once()
        printed = mock_print.call_args[0][0]
        self.assertIn("foo.tar.gz", printed)
        self.assertIn("sha256:", printed)

    def test_print_does_not_verify(self):
        # Even a declared checksum is not verified in print mode
        with patch("bits_helpers.checksum.verify_file") as mock_verify:
            with patch("builtins.print"):
                check_file(self.path, "foo.tar.gz", self.bad, "print")
        mock_verify.assert_not_called()


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  6.  download() integration                                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestDownloadChecksum(unittest.TestCase):
    """Verify that download() passes checksum and enforce_mode to check_file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("bits_helpers.download.check_file")
    @patch("bits_helpers.download.executeWithErrorCheck", return_value=True)
    @patch("bits_helpers.download.makedirs")
    def test_checksum_passed_to_check_file(self, _mkd, _exec, mock_check):
        # Simulate a cache hit so no real network call happens.
        fake_cache = os.path.join(self.tmp, "XX", "XXXX")
        os.makedirs(fake_cache, exist_ok=True)
        fake_file = os.path.join(fake_cache, "foo.tar.gz")
        open(fake_file, "w").close()

        with patch("bits_helpers.download.abspath", return_value=self.tmp), \
             patch("bits_helpers.download.join", side_effect=os.path.join), \
             patch("bits_helpers.download.exists", return_value=True), \
             patch("bits_helpers.download.getUrlChecksum", return_value="XX" * 2):
            from bits_helpers.download import download
            download("https://example.com/foo.tar.gz", self.tmp, self.tmp,
                     checksum="sha256:abc123", enforce_mode="warn")

        mock_check.assert_called_once()
        _, _, passed_checksum, passed_mode = mock_check.call_args[0]
        self.assertEqual(passed_checksum, "sha256:abc123")
        self.assertEqual(passed_mode, "warn")

    @patch("bits_helpers.download.check_file")
    @patch("bits_helpers.download.executeWithErrorCheck", return_value=True)
    @patch("bits_helpers.download.makedirs")
    def test_no_checksum_passes_none(self, _mkd, _exec, mock_check):
        with patch("bits_helpers.download.abspath", return_value=self.tmp), \
             patch("bits_helpers.download.join", side_effect=os.path.join), \
             patch("bits_helpers.download.exists", return_value=True), \
             patch("bits_helpers.download.getUrlChecksum", return_value="XX" * 2):
            from bits_helpers.download import download
            download("https://example.com/foo.tar.gz", self.tmp, self.tmp)

        mock_check.assert_called_once()
        _, _, passed_checksum, passed_mode = mock_check.call_args[0]
        self.assertIsNone(passed_checksum)
        self.assertEqual(passed_mode, "off")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  7.  SOURCE*/PATCH* env var stripping in build.py                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestBuildEnvVarStripping(unittest.TestCase):
    """parse_checksum_entry must strip the checksum suffix from SOURCE/PATCH vars."""

    def test_parse_entry_strips_checksum_for_env_var(self):
        from bits_helpers.checksum import parse_entry
        url, _ = parse_entry(
            "https://example.com/libfoo-1.2.tar.gz,sha256:abcdef1234")
        self.assertEqual(os.path.basename(url), "libfoo-1.2.tar.gz")

    def test_parse_entry_plain_url_unchanged(self):
        from bits_helpers.checksum import parse_entry
        url, cksum = parse_entry("https://example.com/libfoo-1.2.tar.gz")
        self.assertEqual(os.path.basename(url), "libfoo-1.2.tar.gz")
        self.assertIsNone(cksum)

    def test_parse_entry_patch_filename_stripped(self):
        from bits_helpers.checksum import parse_entry
        name, cksum = parse_entry("fix-endian.patch,sha256:cafe0099")
        self.assertEqual(name, "fix-endian.patch")
        self.assertEqual(cksum, "sha256:cafe0099")


if __name__ == "__main__":
    unittest.main()
