# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the per-package NOTICE writer (bits_helpers/build.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers.build import _notice_text, _notice_block, _is_copyleft


class IsCopyleftTest(unittest.TestCase):
    def test_plain_ids(self):
        for lic in ("GPL-2.0-only", "GPL-3.0-or-later", "LGPL-2.1-only", "MPL-2.0",
                    "AGPL-3.0-only"):
            self.assertTrue(_is_copyleft(lic), lic)

    def test_with_exception_matches_base(self):
        self.assertTrue(_is_copyleft("GPL-3.0-or-later WITH GCC-exception-3.1"))

    def test_permissive_not_copyleft(self):
        for lic in ("MIT", "BSD-3-Clause", "Apache-2.0", "BSL-1.0", "CFITSIO", "", None):
            self.assertFalse(_is_copyleft(lic), lic)


class NoticeTextTest(unittest.TestCase):
    def test_permissive_without_acknowledgment_is_empty(self):
        self.assertEqual(_notice_text({"package": "boost", "version": "1.87",
                                       "license": "BSL-1.0"}), "")
        self.assertEqual(_notice_block({"package": "boost", "license": "MIT"}), "")

    def test_acknowledgment_only(self):
        t = _notice_text({"package": "cfitsio", "version": "4.6.2", "license": "CFITSIO",
                          "acknowledgment": "Developed by NASA/HEASARC."})
        self.assertIn("cfitsio 4.6.2", t)
        self.assertIn("License: CFITSIO", t)
        self.assertIn("Developed by NASA/HEASARC.", t)
        self.assertNotIn("copyleft", t)                 # not copyleft -> no source block

    def test_copyleft_tarball_source(self):
        t = _notice_text({"package": "fftw", "version": "3.3.10",
                          "license": "GPL-2.0-or-later",
                          "sources": ["https://www.fftw.org/fftw-3.3.10.tar.gz"]})
        self.assertIn("copyleft", t)
        self.assertIn("https://www.fftw.org/fftw-3.3.10.tar.gz", t)

    def test_copyleft_git_source(self):
        t = _notice_text({"package": "ROOT", "version": "v6.38.00", "license": "LGPL-2.1-only",
                          "source": "https://github.com/root-project/root.git", "tag": "v6-38-00"})
        self.assertIn("git: https://github.com/root-project/root.git @ v6-38-00", t)

    def test_copyleft_without_resolvable_source(self):
        t = _notice_text({"package": "x", "version": "1", "license": "GPL-3.0-only"})
        self.assertIn("see the source: field", t)

    def test_block_is_quoted_heredoc_and_defuses_terminator(self):
        blk = _notice_block({"package": "x", "version": "1", "license": "GPL-3.0-only",
                             "acknowledgment": "line with BITS_NOTICE_EOF inside"})
        self.assertTrue(blk.startswith('cat > "$INSTALLROOT/NOTICE" <<\\BITS_NOTICE_EOF\n'))
        self.assertTrue(blk.rstrip().endswith("BITS_NOTICE_EOF"))
        # the terminator appearing in the body is defused so it can't end the heredoc
        self.assertIn("BITS_NOTICE_EOF_ inside", blk)


if __name__ == "__main__":
    unittest.main()
