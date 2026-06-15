# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the pure logic of tools/initdotsh_modules_diff.py."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import initdotsh_modules_diff as d  # noqa: E402


class TestClassify(unittest.TestCase):

    def test_functional_and_root_and_bookkeeping_and_noise(self):
        self.assertEqual(d.classify_var("CMAKE_PREFIX_PATH"), "functional")
        self.assertEqual(d.classify_var("PYTHONPATH"), "functional")
        self.assertEqual(d.classify_var("ROOTSYS"), "functional")     # recipe env:
        self.assertEqual(d.classify_var("ROOT_ROOT"), "root")
        self.assertEqual(d.classify_var("BOOST_INCLUDE_DIR"), "root")
        self.assertEqual(d.classify_var("ROOT_VERSION"), "bookkeeping")
        self.assertEqual(d.classify_var("ROOT_HASH"), "bookkeeping")
        self.assertEqual(d.classify_var("RECC_PREFIX_MAP"), "bookkeeping")
        self.assertEqual(d.classify_var("LOADEDMODULES"), "noise")
        self.assertEqual(d.classify_var("MODULES_RUN_QUARANTINE"), "noise")
        self.assertEqual(d.classify_var("ARCHITECTURE"), "noise")  # capture-harness var


class TestParseEnv0(unittest.TestCase):

    def test_parses_nul_separated_with_newlines(self):
        text = "A=1\0B=line1\nline2\0PATH=/a:/b\0"
        self.assertEqual(d.parse_env0(text),
                         {"A": "1", "B": "line1\nline2", "PATH": "/a:/b"})

    def test_empty(self):
        self.assertEqual(d.parse_env0(""), {})


class TestDiffEnv(unittest.TestCase):

    def test_added_missing_changed(self):
        legacy = {
            "PATH": "/p/root/bin",
            "LD_LIBRARY_PATH": "/p/root/lib",
            "ROOT_VERSION": "6.38",            # bookkeeping → ignored
            "PYTHONHOME": "/p/py",             # legacy-only functional → missing
        }
        modules = {
            "PATH": "/p/root/bin",
            "LD_LIBRARY_PATH": "/p/root/lib:/p/dep/lib",   # path changed (added)
            "CMAKE_PREFIX_PATH": "/p/root",    # modules-only → added
            "PYTHONPATH": "/p/root/lib/python3.13/site-packages",  # added
            "LOADEDMODULES": "ROOT",           # noise → ignored
        }
        r = d.diff_env(legacy, modules)
        self.assertIn("CMAKE_PREFIX_PATH", r["added_functional"])
        self.assertIn("PYTHONPATH", r["added_functional"])
        self.assertIn("PYTHONHOME", r["missing_functional"])
        self.assertEqual(r["changed_path"]["LD_LIBRARY_PATH"], (["/p/dep/lib"], []))
        self.assertNotIn("ROOT_VERSION", r["added_functional"])
        self.assertNotIn("ROOT_VERSION", r["missing_functional"])

    def test_identical_functional_is_clean(self):
        env = {"PATH": "/x/bin", "ROOT_VERSION": "6.38"}
        r = d.diff_env(env, dict(env))
        self.assertEqual(r["added_functional"], {})
        self.assertEqual(r["missing_functional"], {})
        self.assertEqual(r["changed_path"], {})
        self.assertEqual(r["changed_scalar"], {})


class TestSummarize(unittest.TestCase):

    def test_counts(self):
        results = {
            "ROOT/6.38": {"added_functional": {"CMAKE_PREFIX_PATH": "x",
                                               "PYTHONPATH": "y"},
                          "missing_functional": {}, "changed_path": {},
                          "changed_scalar": {}},
            "vecgeom/1.2": {"added_functional": {"CMAKE_PREFIX_PATH": "x"},
                            "missing_functional": {}, "changed_path": {},
                            "changed_scalar": {}},
            # missing only the bits-owned layer → expected, still counts clean
            "expected/1": {"added_functional": {},
                           "missing_functional": {"CXXFLAGS": "-O2",
                                                  "DEFAULTS_RELEASE_ROOT": "/x"},
                           "changed_path": {}, "changed_scalar": {}},
            # missing a real var → unexpected, flagged
            "weird/9": {"added_functional": {},
                        "missing_functional": {"SOME_VAR": "z"},
                        "changed_path": {}, "changed_scalar": {}},
        }
        s = d.summarize(results)
        self.assertEqual(s["packages"], 4)
        self.assertEqual(s["gain_cmake_prefix_path"], 2)
        self.assertEqual(s["gain_pythonpath"], 1)
        self.assertEqual(s["with_unexpected_missing"], 1)        # only weird/9
        self.assertEqual(s["clean"], 3)                          # incl. expected/1
        self.assertEqual(s["missing_expected"], {"CXXFLAGS": 1, "DEFAULTS_RELEASE_ROOT": 1})
        self.assertEqual(s["missing_unexpected"], {"SOME_VAR": 1})


if __name__ == "__main__":
    unittest.main()
