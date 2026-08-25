# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers/bits_use — the .bitscmd saved-arg-profile."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers import bits_use as U


class BitsUseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = os.path.join(self.tmp, ".bitscmd")
        self._cwd = os.getcwd()

    def tearDown(self):
        import shutil
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── sections round-trip ──────────────────────────────────────────────────
    def test_write_read_sections(self):
        U.write_section("common", ["--architecture", "x86_64-el9-gcc14-opt"], self.p)
        U.write_section("build", ["--docker", "--sandbox", "off"], self.p)
        sec = U.read_all(self.p)
        self.assertEqual(sec["common"], ["--architecture", "x86_64-el9-gcc14-opt"])
        self.assertEqual(sec["build"], ["--docker", "--sandbox", "off"])

    def test_global_aliases_common(self):
        U.write_section("global", ["-a", "X"], self.p)
        self.assertEqual(U.read_all(self.p).get("common"), ["-a", "X"])

    def test_headerless_preamble_is_common(self):
        with open(self.p, "w") as fh:
            fh.write("--architecture X\n[build]\n--docker\n")
        sec = U.read_all(self.p)
        self.assertEqual(sec["common"], ["--architecture", "X"])
        self.assertEqual(sec["build"], ["--docker"])

    def test_quoting_roundtrips(self):
        U.write_section("build", ["--env", "A=b c", "--x", "a::b"], self.p)
        self.assertEqual(U.read_all(self.p)["build"], ["--env", "A=b c", "--x", "a::b"])

    def test_clear_one_and_all(self):
        U.write_section("common", ["-a", "X"], self.p)
        U.write_section("build", ["--docker"], self.p)
        self.assertTrue(U.clear_section("build", self.p))
        self.assertNotIn("build", U.read_all(self.p))
        self.assertIn("common", U.read_all(self.p))
        self.assertTrue(U.clear_section(None, self.p))
        self.assertFalse(os.path.exists(self.p))

    # ── merge / rewrite ──────────────────────────────────────────────────────
    def _profile(self):
        U.write_section("common", ["--architecture", "A", "--defaults", "D"], self.p)
        U.write_section("build", ["--docker", "--reuse-from", "cvmfs::relaxed"], self.p)

    def test_merged_build_gets_common_and_section(self):
        self._profile()
        self.assertEqual(
            U.merged_argv("build", ["xrootd"], self.p),
            ["--architecture", "A", "--defaults", "D",
             "--docker", "--reuse-from", "cvmfs::relaxed", "xrootd"])

    def test_merged_q_gets_common_only(self):
        self._profile()
        self.assertEqual(U.merged_argv("q", ["ROOT"], self.p),
                         ["--architecture", "A", "--defaults", "D", "ROOT"])

    def test_rewrite_injects_after_action(self):
        self._profile()
        self.assertEqual(
            U.rewrite_argv(["build", "xrootd"], self.p),
            ["build", "--architecture", "A", "--defaults", "D",
             "--docker", "--reuse-from", "cvmfs::relaxed", "xrootd"])

    def test_rewrite_skips_top_flags(self):
        self._profile()
        out = U.rewrite_argv(["-d", "build", "xrootd"], self.p)
        self.assertEqual(out[:2], ["-d", "build"])
        self.assertIn("--docker", out)

    def test_rewrite_use_opts_out(self):
        self._profile()
        self.assertEqual(U.rewrite_argv(["use", "build", "--docker"], self.p),
                         ["use", "build", "--docker"])

    def test_rewrite_no_profile_is_noop(self):
        self.assertEqual(U.rewrite_argv(["build", "x"], self.p), ["build", "x"])

    def test_rewrite_user_arg_comes_after_injected(self):
        # A user's explicit --architecture must land AFTER the injected one so
        # argparse last-wins lets it override.
        self._profile()
        out = U.rewrite_argv(["build", "--architecture", "B", "x"], self.p)
        self.assertLess(out.index("A"), out.index("B"))

    def test_rewrite_option_before_action_is_noop(self):
        self._profile()
        self.assertEqual(U.rewrite_argv(["--weird", "x"], self.p),
                         ["--weird", "x"])

    # ── --rewrite0 wire contract (wrapper reads with `while read -d ''`) ──────
    def test_rewrite0_nul_terminated_last_token_survives(self):
        import io, contextlib
        self._profile()
        os.chdir(self.tmp)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            U.main(["--rewrite0", "--", "build", "xrootd"])
        s = buf.getvalue()
        self.assertTrue(s.endswith("\0"))          # trailing NUL present
        # `while read -d ''` keeps only fully-terminated fields; splitting on NUL
        # and dropping the trailing empty must reproduce every token, incl. last.
        toks = s.split("\0")[:-1]
        self.assertEqual(toks[0], "build")
        self.assertEqual(toks[-1], "xrootd")

    def test_malformed_profile_ignored_not_crash(self):
        with open(self.p, "w") as fh:
            fh.write('[build]\n--foo "unbalanced\n')     # bad quoting
        self.assertEqual(U.read_all(self.p), {})          # fail safe, no raise
        self.assertEqual(U.rewrite_argv(["build", "x"], self.p), ["build", "x"])


if __name__ == "__main__":
    unittest.main()
