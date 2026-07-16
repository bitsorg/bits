# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the dual-source (git vs tarball) selector (bits_helpers/build.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers.build import _source_mode, _apply_source_mode


class SourceModeResolveTest(unittest.TestCase):
    def setUp(self):
        os.environ.pop("BITS_SOURCE_MODE", None)

    def tearDown(self):
        os.environ.pop("BITS_SOURCE_MODE", None)

    def test_default_is_tar(self):
        self.assertEqual(_source_mode({}), "tar")
        self.assertEqual(_source_mode(None), "tar")

    def test_system_field(self):
        self.assertEqual(_source_mode({"system": {"source_mode": "git"}}), "git")
        self.assertEqual(_source_mode({"system": {"source_mode": "tar"}}), "tar")

    def test_variable_field(self):
        self.assertEqual(_source_mode({"variables": {"source_mode": "git"}}), "git")

    def test_env_overrides_defaults(self):
        os.environ["BITS_SOURCE_MODE"] = "git"
        self.assertEqual(_source_mode({"system": {"source_mode": "tar"}}), "git")
        os.environ["BITS_SOURCE_MODE"] = "tar"
        self.assertEqual(_source_mode({"system": {"source_mode": "git"}}), "tar")

    def test_unknown_value_falls_back_to_tar(self):
        self.assertEqual(_source_mode({"system": {"source_mode": "svn"}}), "tar")


class ApplySourceModeTest(unittest.TestCase):
    def _dual(self):
        return {"package": "fftw", "version": "3.3.10",
                "source": "https://github.com/FFTW/fftw3.git",
                "tag": "fftw-%(version)s",
                "sources": ["https://www.fftw.org/fftw-%(version)s.tar.gz"]}

    def test_tar_mode_drops_git(self):
        s = self._dual()
        _apply_source_mode(s, "tar")
        self.assertNotIn("source", s)
        self.assertNotIn("tag", s)              # git ref dropped -> defaults to version
        self.assertEqual(s["sources"], ["https://www.fftw.org/fftw-%(version)s.tar.gz"])
        self.assertEqual(s["version"], "3.3.10")  # version untouched

    def test_git_mode_drops_tarballs(self):
        s = self._dual()
        _apply_source_mode(s, "git")
        self.assertNotIn("sources", s)
        self.assertEqual(s["source"], "https://github.com/FFTW/fftw3.git")
        self.assertEqual(s["tag"], "fftw-%(version)s")
        self.assertEqual(s["version"], "3.3.10")

    def test_single_source_recipes_untouched(self):
        tar_only = {"version": "1", "sources": ["u"]}
        git_only = {"version": "1", "source": "g", "tag": "t"}
        for mode in ("tar", "git"):
            a = dict(tar_only); _apply_source_mode(a, mode); self.assertEqual(a, tar_only)
            b = dict(git_only); _apply_source_mode(b, mode); self.assertEqual(b, git_only)


if __name__ == "__main__":
    unittest.main()
