# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers.checksum_store."""

import os
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bits_helpers.checksum_store import (
    find_checksum_file,
    parse_checksum_file,
    load_for_spec,
    merge_into_spec,
    format_checksum_file,
    write_checksum_file,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

GOOD_SHA1  = "a" * 40
GOOD_SHA256 = "b" * 64
_SOURCE_URL = "https://example.com/mylib-1.0.tar.gz"
_EXTRA_URL  = "https://example.com/extra.tar.bz2"
_PATCH_NAME = "fix-endian.patch"


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(textwrap.dedent(content))


# ─────────────────────────────────────────────────────────────────────────────
# 1. find_checksum_file
# ─────────────────────────────────────────────────────────────────────────────

class TestFindChecksumFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pkgdir = os.path.join(self.tmp, "myrepo.bits")
        os.makedirs(self.pkgdir)

    def _checksums_dir(self):
        return os.path.join(self.pkgdir, "checksums")

    def test_returns_none_when_no_checksums_dir(self):
        self.assertIsNone(find_checksum_file(self.pkgdir, "mylib"))

    def test_returns_none_when_file_absent(self):
        os.makedirs(self._checksums_dir())
        self.assertIsNone(find_checksum_file(self.pkgdir, "mylib"))

    def test_returns_path_when_file_present(self):
        path = os.path.join(self._checksums_dir(), "mylib.checksum")
        _write(path, "tag: " + GOOD_SHA1 + "\n")
        result = find_checksum_file(self.pkgdir, "mylib")
        self.assertEqual(result, path)

    def test_case_insensitive_lookup(self):
        """Package name is lowercased before constructing the path."""
        path = os.path.join(self._checksums_dir(), "mylib.checksum")
        _write(path, "tag: " + GOOD_SHA1 + "\n")
        self.assertIsNotNone(find_checksum_file(self.pkgdir, "MyLib"))
        self.assertIsNotNone(find_checksum_file(self.pkgdir, "MYLIB"))


# ─────────────────────────────────────────────────────────────────────────────
# 2. parse_checksum_file
# ─────────────────────────────────────────────────────────────────────────────

class TestParseChecksumFile(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _file(self, content):
        path = os.path.join(self.tmp, "test.checksum")
        _write(path, content)
        return path

    def test_empty_file_returns_empty_store(self):
        path = self._file("")
        result = parse_checksum_file(path)
        self.assertIsNone(result["tag"])
        self.assertEqual(result["sources"], {})
        self.assertEqual(result["patches"], {})

    def test_tag_sha1(self):
        path = self._file("tag: " + GOOD_SHA1)
        self.assertEqual(parse_checksum_file(path)["tag"], GOOD_SHA1.lower())

    def test_tag_sha256(self):
        path = self._file("tag: " + GOOD_SHA256)
        self.assertEqual(parse_checksum_file(path)["tag"], GOOD_SHA256.lower())

    def test_invalid_tag_raises(self):
        path = self._file("tag: notahexstring")
        with self.assertRaises(ValueError):
            parse_checksum_file(path)

    def test_sources_parsed(self):
        content = """
            sources:
              https://example.com/foo.tar.gz: sha256:{sha}
        """.format(sha="a" * 64)
        path = self._file(content)
        result = parse_checksum_file(path)
        self.assertEqual(result["sources"]["https://example.com/foo.tar.gz"],
                         "sha256:" + "a" * 64)

    def test_patches_parsed(self):
        content = """
            patches:
              fix.patch: md5:{md5}
        """.format(md5="a" * 32)
        path = self._file(content)
        result = parse_checksum_file(path)
        self.assertEqual(result["patches"]["fix.patch"], "md5:" + "a" * 32)

    def test_full_file(self):
        content = """
            tag: {sha1}
            sources:
              https://example.com/a.tar.gz: sha256:{sha256}
            patches:
              fix.patch: sha512:{sha512}
        """.format(sha1=GOOD_SHA1, sha256="c" * 64, sha512="d" * 128)
        path = self._file(content)
        result = parse_checksum_file(path)
        self.assertEqual(result["tag"], GOOD_SHA1.lower())
        self.assertIn("https://example.com/a.tar.gz", result["sources"])
        self.assertIn("fix.patch", result["patches"])

    def test_unknown_keys_ignored(self):
        """Extra top-level keys must not raise."""
        path = self._file("future_field: some_value\ntag: " + GOOD_SHA1)
        result = parse_checksum_file(path)
        self.assertEqual(result["tag"], GOOD_SHA1.lower())

    def test_malformed_yaml_raises(self):
        path = self._file(": invalid: yaml: [")
        with self.assertRaises(ValueError):
            parse_checksum_file(path)

    def test_non_mapping_raises(self):
        path = self._file("- list item\n")
        with self.assertRaises(ValueError):
            parse_checksum_file(path)


# ─────────────────────────────────────────────────────────────────────────────
# 3. load_for_spec
# ─────────────────────────────────────────────────────────────────────────────

class TestLoadForSpec(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pkgdir = os.path.join(self.tmp, "myrepo.bits")
        os.makedirs(self.pkgdir)

    def _spec(self):
        return {"pkgdir": self.pkgdir, "package": "mylib"}

    def test_no_file_returns_empty_store(self):
        result = load_for_spec(self._spec())
        self.assertIsNone(result["tag"])
        self.assertEqual(result["sources"], {})
        self.assertEqual(result["patches"], {})

    def test_valid_file_loaded(self):
        path = os.path.join(self.pkgdir, "checksums", "mylib.checksum")
        _write(path, "tag: " + GOOD_SHA1 + "\n")
        result = load_for_spec(self._spec())
        self.assertEqual(result["tag"], GOOD_SHA1.lower())

    def test_corrupt_file_returns_empty_store(self):
        """A bad checksum file logs a warning but does not raise."""
        path = os.path.join(self.pkgdir, "checksums", "mylib.checksum")
        _write(path, "tag: notvalid\n")
        with patch("bits_helpers.checksum_store.warning"):
            result = load_for_spec(self._spec())
        self.assertIsNone(result["tag"])


# ─────────────────────────────────────────────────────────────────────────────
# 4. merge_into_spec
# ─────────────────────────────────────────────────────────────────────────────

class TestMergeIntoSpec(unittest.TestCase):

    def _store(self, tag=None, sources=None, patches=None):
        return {
            "tag": tag,
            "sources": sources or {},
            "patches": patches or {},
        }

    def test_sets_source_checksums(self):
        spec = {}
        merge_into_spec(spec, self._store(sources={_SOURCE_URL: "sha256:" + "a" * 64}))
        self.assertIn(_SOURCE_URL, spec["source_checksums"])

    def test_sets_patch_checksums(self):
        spec = {}
        merge_into_spec(spec, self._store(patches={_PATCH_NAME: "sha256:" + "b" * 64}))
        self.assertIn(_PATCH_NAME, spec["patch_checksums"])

    def test_sets_pin_commit(self):
        spec = {}
        merge_into_spec(spec, self._store(tag=GOOD_SHA1))
        self.assertEqual(spec["pin_commit"], GOOD_SHA1)

    def test_empty_store_leaves_empty_dicts(self):
        spec = {}
        merge_into_spec(spec, self._store())
        self.assertEqual(spec["source_checksums"], {})
        self.assertEqual(spec["patch_checksums"], {})
        self.assertIsNone(spec["pin_commit"])

    def test_overwrites_existing_keys(self):
        """merge_into_spec must replace, not merge, any pre-existing keys."""
        spec = {"source_checksums": {"old": "val"}, "pin_commit": "old"}
        merge_into_spec(spec, self._store(sources={_SOURCE_URL: "sha256:" + "c" * 64}))
        self.assertNotIn("old", spec["source_checksums"])
        self.assertIn(_SOURCE_URL, spec["source_checksums"])
        self.assertIsNone(spec["pin_commit"])


# ─────────────────────────────────────────────────────────────────────────────
# 5. format_checksum_file / write_checksum_file
# ─────────────────────────────────────────────────────────────────────────────

class TestFormatAndWrite(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def _store(self):
        return {
            "tag": GOOD_SHA1,
            "sources": {_SOURCE_URL: "sha256:" + "a" * 64},
            "patches": {_PATCH_NAME: "md5:" + "b" * 32},
        }

    def test_format_contains_tag(self):
        text = format_checksum_file("mylib", self._store())
        self.assertIn("tag:", text)
        self.assertIn(GOOD_SHA1, text)

    def test_format_contains_sources(self):
        text = format_checksum_file("mylib", self._store())
        self.assertIn("sources:", text)
        self.assertIn(_SOURCE_URL, text)

    def test_format_contains_patches(self):
        text = format_checksum_file("mylib", self._store())
        self.assertIn("patches:", text)
        self.assertIn(_PATCH_NAME, text)

    def test_format_includes_regen_hint(self):
        text = format_checksum_file("mylib", self._store())
        self.assertIn("--write-checksums", text)

    def test_write_creates_file(self):
        pkgdir = os.path.join(self.tmp, "repo.bits")
        path = write_checksum_file(pkgdir, "mylib", self._store())
        self.assertTrue(os.path.isfile(path))
        self.assertTrue(path.endswith("mylib.checksum"))

    def test_write_creates_checksums_dir(self):
        pkgdir = os.path.join(self.tmp, "repo.bits")
        write_checksum_file(pkgdir, "mylib", self._store())
        self.assertTrue(os.path.isdir(os.path.join(pkgdir, "checksums")))

    def test_write_content_roundtrip(self):
        """Written file must parse back to the same store."""
        pkgdir = os.path.join(self.tmp, "repo.bits")
        path = write_checksum_file(pkgdir, "mylib", self._store())
        parsed = parse_checksum_file(path)
        self.assertEqual(parsed["tag"], GOOD_SHA1.lower())
        self.assertIn(_SOURCE_URL, parsed["sources"])
        self.assertIn(_PATCH_NAME, parsed["patches"])

    def test_empty_sections_omitted(self):
        """If sources/patches are empty, those sections must not appear."""
        store = {"tag": GOOD_SHA1, "sources": {}, "patches": {}}
        text = format_checksum_file("mylib", store)
        self.assertNotIn("sources:", text)
        self.assertNotIn("patches:", text)


# ─────────────────────────────────────────────────────────────────────────────
# 6. _verify_commit_pin (via workarea)
# ─────────────────────────────────────────────────────────────────────────────

class TestVerifyCommitPin(unittest.TestCase):
    """Unit tests for workarea._verify_commit_pin."""

    def setUp(self):
        from bits_helpers.workarea import _verify_commit_pin
        self._fn = _verify_commit_pin

    def _scm(self, sha):
        m = MagicMock()
        m.checkedOutCommitName.return_value = sha
        return m

    def test_off_mode_no_check(self):
        """Pin is ignored in 'off' mode."""
        scm = self._scm("wrong_sha")
        spec = {"package": "pkg", "pin_commit": GOOD_SHA1}
        with patch("bits_helpers.workarea.dieOnError") as mock_die:
            self._fn(scm, spec, "/src", "off")
        mock_die.assert_not_called()

    def test_no_pin_no_check(self):
        """No check when pin_commit is absent."""
        scm = self._scm(GOOD_SHA1)
        spec = {"package": "pkg"}
        with patch("bits_helpers.workarea.dieOnError") as mock_die:
            self._fn(scm, spec, "/src", "enforce")
        mock_die.assert_not_called()

    def test_enforce_match_no_error(self):
        scm = self._scm(GOOD_SHA1)
        spec = {"package": "pkg", "pin_commit": GOOD_SHA1}
        with patch("bits_helpers.workarea.dieOnError") as mock_die:
            self._fn(scm, spec, "/src", "enforce")
        mock_die.assert_not_called()

    def test_enforce_mismatch_dies(self):
        scm = self._scm("0" * 40)
        spec = {"package": "pkg", "pin_commit": GOOD_SHA1}
        with patch("bits_helpers.workarea.dieOnError") as mock_die:
            self._fn(scm, spec, "/src", "enforce")
        mock_die.assert_called_once()
        self.assertTrue(mock_die.call_args[0][0])  # first arg is True (error condition)

    def test_warn_mismatch_warns_not_dies(self):
        scm = self._scm("0" * 40)
        spec = {"package": "pkg", "pin_commit": GOOD_SHA1}
        with patch("bits_helpers.workarea.dieOnError") as mock_die, \
             patch("bits_helpers.workarea.warning") as mock_warn:
            self._fn(scm, spec, "/src", "warn")
        mock_die.assert_not_called()
        mock_warn.assert_called_once()

    def test_print_mode_prints_sha(self):
        scm = self._scm(GOOD_SHA1)
        spec = {"package": "pkg", "pin_commit": GOOD_SHA1}
        with patch("builtins.print") as mock_print:
            self._fn(scm, spec, "/src", "print")
        mock_print.assert_called_once()
        output = mock_print.call_args[0][0]
        self.assertIn("pkg", output)
        self.assertIn(GOOD_SHA1, output)

    def test_scm_exception_warns_no_die(self):
        scm = MagicMock()
        scm.checkedOutCommitName.side_effect = RuntimeError("no git")
        spec = {"package": "pkg", "pin_commit": GOOD_SHA1}
        with patch("bits_helpers.workarea.dieOnError") as mock_die, \
             patch("bits_helpers.workarea.warning"):
            self._fn(scm, spec, "/src", "enforce")
        mock_die.assert_not_called()

    def test_case_insensitive_comparison(self):
        """SHA comparison must be case-insensitive."""
        scm = self._scm(GOOD_SHA1.upper())
        spec = {"package": "pkg", "pin_commit": GOOD_SHA1.lower()}
        with patch("bits_helpers.workarea.dieOnError") as mock_die:
            self._fn(scm, spec, "/src", "enforce")
        mock_die.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 7. External checksum overrides inline checksum (integration)
# ─────────────────────────────────────────────────────────────────────────────

class TestExternalOverridesInline(unittest.TestCase):
    """Verify that the external store wins over the inline comma-suffix."""

    def test_external_wins_over_inline_source(self):
        """When the external store has a checksum for a URL, it takes priority."""
        from bits_helpers.checksum import parse_entry

        external_ck = "sha256:" + "e" * 64
        inline_ck   = "sha256:" + "f" * 64
        url = "https://example.com/foo.tar.gz"
        source_entry = url + "," + inline_ck

        url_parsed, inline = parse_entry(source_entry)
        source_checksums = {url_parsed: external_ck}

        # Simulate what checkout_sources does:
        actual = source_checksums.get(url_parsed) or inline
        self.assertEqual(actual, external_ck)

    def test_inline_used_when_not_in_external_store(self):
        """If the URL is absent from the external store, the inline value is kept."""
        from bits_helpers.checksum import parse_entry

        inline_ck = "sha256:" + "f" * 64
        url = "https://example.com/bar.tar.gz"
        source_entry = url + "," + inline_ck

        url_parsed, inline = parse_entry(source_entry)
        source_checksums = {}  # empty external store

        actual = source_checksums.get(url_parsed) or inline
        self.assertEqual(actual, inline_ck)

    def test_no_checksum_when_both_absent(self):
        from bits_helpers.checksum import parse_entry

        url = "https://example.com/baz.tar.gz"
        url_parsed, inline = parse_entry(url)
        source_checksums = {}

        actual = source_checksums.get(url_parsed) or inline
        self.assertIsNone(actual)


if __name__ == "__main__":
    unittest.main(verbosity=2)
