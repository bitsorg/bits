# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers/bits_use — the .bitsuse saved-arg profile."""

import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

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

    def test_rewrite_meta_commands_opt_out(self):
        # Meta commands (use/cvmfs/store/version) take a different option set and
        # must NOT receive the profile, or their argparse would reject it.
        self._profile()
        for meta in (["use", "build", "--docker"], ["cvmfs", "platforms"],
                     ["store", "ls"], ["version"],
                     ["verify", "--from-manifest", "m.json"]):   # accepts neither flag
            self.assertEqual(U.rewrite_argv(list(meta), self.p), list(meta))

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


class BitsUseTwoTierTest(unittest.TestCase):
    """Two-tier storage: local ./.bitsuse (owned) or ~/.bits/use/<key>."""

    def setUp(self):
        self.cwd0 = os.getcwd()
        self.work = tempfile.mkdtemp()
        self.home = tempfile.mkdtemp()
        os.chdir(self.work)
        self._patch = patch.object(U, "HOME_STORE", os.path.join(self.home, "use"))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        os.chdir(self.cwd0)
        try:
            os.chmod(self.work, 0o755)
        except OSError:
            pass
        shutil.rmtree(self.work, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    def test_writes_local_bitsuse(self):
        p = U.write_section("build", ["--docker"])
        self.assertEqual(os.path.basename(p), ".bitsuse")
        self.assertTrue(os.path.exists(os.path.join(self.work, ".bitsuse")))
        self.assertEqual(U.read_all(U._read_path())["build"], ["--docker"])

    def test_legacy_bitscmd_read_fallback(self):
        with open(os.path.join(self.work, ".bitscmd"), "w") as fh:
            fh.write("[build]\n--docker\n")
        self.assertEqual(os.path.basename(U._read_path()), ".bitscmd")
        self.assertEqual(U.rewrite_argv(["build", "x"]), ["build", "--docker", "x"])

    def test_bitsuse_preferred_over_bitscmd(self):
        with open(os.path.join(self.work, ".bitscmd"), "w") as fh:
            fh.write("[build]\n--old\n")
        with open(os.path.join(self.work, ".bitsuse"), "w") as fh:
            fh.write("[build]\n--new\n")
        self.assertEqual(os.path.basename(U._read_path()), ".bitsuse")
        out = U.rewrite_argv(["build", "x"])
        self.assertIn("--new", out)
        self.assertNotIn("--old", out)

    @patch("bits_helpers.bits_use.os.access", return_value=False)
    def test_home_fallback_when_cwd_not_writeable(self, _access):
        p = U.write_section("build", ["--docker"])
        self.assertTrue(U._is_home_path(p))
        self.assertTrue(os.path.exists(p))
        self.assertIn("# dir:", open(p).read())          # dir header recorded
        self.assertEqual(os.path.abspath(U._read_path()), os.path.abspath(p))
        self.assertEqual(U.rewrite_argv(["build", "x"]), ["build", "--docker", "x"])

    def test_first_write_migrates_legacy_content(self):
        with open(os.path.join(self.work, ".bitscmd"), "w") as fh:
            fh.write("[common]\n--architecture X\n")
        U.write_section("build", ["--docker"])            # path=None -> resolves
        sec = U.read_all(U._read_path())
        self.assertEqual(sec.get("common"), ["--architecture", "X"])  # carried over
        self.assertEqual(sec.get("build"), ["--docker"])
        self.assertEqual(os.path.basename(U._read_path()), ".bitsuse")

    @patch("bits_helpers.bits_use.os.access")
    def test_updates_local_when_dir_readonly(self, access):
        # A local .bitsuse exists; the dir is not writeable but the file is.
        with open(os.path.join(self.work, ".bitsuse"), "w") as fh:
            fh.write("[common]\n--architecture X\n")
        access.side_effect = lambda p, mode: p.endswith(".bitsuse")
        p = U.write_section("build", ["--docker"])
        self.assertEqual(os.path.basename(p), ".bitsuse")   # updated local, not home
        self.assertFalse(U._is_home_path(p))

    def test_untrusted_local_ignored_falls_to_home(self):
        with open(os.path.join(self.work, ".bitsuse"), "w") as fh:
            fh.write("[build]\n--planted\n")
        home, _ = U._home_paths()
        os.makedirs(os.path.dirname(home), exist_ok=True)
        with open(home, "w") as fh:
            fh.write("[build]\n--trusted\n")
        with patch("bits_helpers.bits_use._owned_by_user", return_value=False):
            path = U._read_path()
            out = U.rewrite_argv(["build", "x"])
        self.assertTrue(U._is_home_path(path))
        self.assertIn("--trusted", out)
        self.assertNotIn("--planted", out)


if __name__ == "__main__":
    unittest.main()
