# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure rev-index helpers for the content-addressed store (ADR-0005 P2a)."""

import unittest

from bits_helpers import rev_index as ri


class TestRevIndex(unittest.TestCase):

    def test_marker_key_and_prefix(self):
        self.assertEqual(
            ri.marker_prefix("x86_64-el9", "fftw", "3.3.10"),
            "MANIFESTS/rev-index/x86_64-el9/fftw/3.3.10-")
        self.assertEqual(
            ri.marker_key("x86_64-el9", "fftw", "3.3.10", "2"),
            "MANIFESTS/rev-index/x86_64-el9/fftw/3.3.10-2")

    def test_revision_of_handles_hyphenated_version(self):
        # version itself contains '-' — revision must still parse unambiguously.
        A, P, V = "x86_64-el9", "GCC-Toolchain", "v14.2.0-alice2"
        k = ri.marker_key(A, P, V, "3")
        self.assertEqual(ri.revision_of(k, A, P, V), "3")
        self.assertEqual(ri.revision_of(k, A, P, V), "3")
        # localN revisions
        self.assertEqual(
            ri.revision_of(ri.marker_key(A, P, V, "local5"), A, P, V), "local5")

    def test_revision_of_rejects_foreign_or_nested_keys(self):
        A, P, V = "x86_64-el9", "fftw", "3.3.10"
        self.assertIsNone(ri.revision_of("MANIFESTS/rev-index/x86_64-el9/other/3.3.10-1", A, P, V))
        # a key that dives into a sub-path is not a flat marker
        self.assertIsNone(ri.revision_of(ri.marker_prefix(A, P, V) + "2/extra", A, P, V))
        self.assertIsNone(ri.revision_of(ri.marker_prefix(A, P, V), A, P, V))  # empty revision

    def test_revision_of_rejects_hyphenated_sibling_version(self):
        # Listing "v14.2.0" must NOT pick up the marker of the *sibling* version
        # "v14.2.0-alice2", whose key shares the "v14.2.0-" prefix.
        A = "x86_64-el9"
        sibling = ri.marker_key(A, "GCC-Toolchain", "v14.2.0-alice2", "3")
        self.assertIsNone(ri.revision_of(sibling, A, "GCC-Toolchain", "v14.2.0"))
        # ...but the sibling itself still parses correctly under its own version.
        self.assertEqual(
            ri.revision_of(sibling, A, "GCC-Toolchain", "v14.2.0-alice2"), "3")

    def test_manifest_records_filters_by_pkg_version_arch(self):
        entries = [
            {"package": "fftw", "version": "3.3.10", "revision": "1",
             "effective_architecture": "x86_64-el9", "hash": "h1"},
            {"package": "fftw", "version": "3.3.10", "revision": "2",
             "effective_architecture": "x86_64-el9", "hash": "h2"},
            {"package": "fftw", "version": "3.3.10", "revision": "9",
             "effective_architecture": "x86_64-el8", "hash": "hOTHERARCH"},   # wrong arch
            {"package": "GSL",  "version": "2.8", "revision": "1",
             "effective_architecture": "x86_64-el9", "hash": "hGSL"},          # wrong pkg
            {"package": "fftw", "version": "3.3.11", "revision": "1",
             "effective_architecture": "x86_64-el9", "hash": "hVER"},          # wrong version
            {"package": "fftw", "version": "3.3.10",                            # no revision -> skipped
             "effective_architecture": "x86_64-el9", "hash": "hNOREV"},
        ]
        self.assertEqual(
            ri.manifest_records(entries, "fftw", "3.3.10", "x86_64-el9"),
            {"1": "h1", "2": "h2"})

    def test_manifest_records_empty_and_malformed(self):
        self.assertEqual(ri.manifest_records(None, "p", "v", "a"), {})
        self.assertEqual(ri.manifest_records(["notadict"], "p", "v", "a"), {})

    def test_merge_manifest_wins(self):
        manifest = {"1": "hM", "2": "h2"}
        markers = {"1": "hMARKER", "3": "h3"}   # rev 1 conflicts; markers add rev 3
        self.assertEqual(ri.merge_records(manifest, markers),
                         {"1": "hM", "2": "h2", "3": "h3"})
        # empties
        self.assertEqual(ri.merge_records({}, {"5": "h5"}), {"5": "h5"})
        self.assertEqual(ri.merge_records(None, None), {})


if __name__ == "__main__":
    unittest.main()
