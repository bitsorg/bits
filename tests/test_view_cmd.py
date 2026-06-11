"""Tests for bits_helpers/view_cmd.py (interactive view-env collapse)."""

import os
import tempfile
import unittest

from bits_helpers import view_cmd


def _pkg(prefix, files):
    for rel in files:
        p = os.path.join(prefix, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
    return prefix


class TestCollectRoots(unittest.TestCase):

    def test_collects_existing_root_vars(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "a"), os.path.join(d, "b")
            os.makedirs(a); os.makedirs(b)
            env = {"ROOT_ROOT": a, "BOOST_ROOT": b,
                   "MISSING_ROOT": os.path.join(d, "nope"),  # absent → dropped
                   "NOTAROOT": a, "PATH": "/usr/bin"}
            self.assertEqual(view_cmd.collect_roots(env), sorted([a, b]))


class TestEnsureViewCaching(unittest.TestCase):

    def test_builds_once_then_reuses(self):
        calls = []

        def fake_build(roots, view_dir):
            calls.append(view_dir)
            os.makedirs(view_dir, exist_ok=True)

        with tempfile.TemporaryDirectory() as d:
            roots = [os.path.join(d, "a")]
            v1 = view_cmd.ensure_view(roots, os.path.join(d, "cache"), _build=fake_build)
            v2 = view_cmd.ensure_view(roots, os.path.join(d, "cache"), _build=fake_build)
            self.assertEqual(v1, v2)
            self.assertEqual(len(calls), 1)            # second call hit the cache
            self.assertTrue(os.path.exists(os.path.join(v1, view_cmd.READY_STAMP)))


class TestCollapseExports(unittest.TestCase):

    def test_remaps_entries_dedups_keeps_system_and_setenvs(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "cache")
            a = _pkg(os.path.join(d, "a"), ["bin/x", "lib/liba.so"])
            # ROOT-style: PyROOT modules live directly in lib (the --pylib case)
            b = _pkg(os.path.join(d, "b"), ["bin/y", "lib/libb.so", "lib/ROOT.py"])
            env = {
                "A_ROOT": a, "B_ROOT": b,
                "ROOTSYS": b,                                  # setenv → untouched
                "PATH": "%s/bin:%s/bin:/usr/bin:/bin" % (a, b),
                "LD_LIBRARY_PATH": "%s/lib:%s/lib" % (a, b),   # both deps → one view/lib
                "PYTHONPATH": "%s/lib" % b,                    # --pylib preserved
                "CMAKE_PREFIX_PATH": "%s:%s" % (a, b),         # roots → view, deduped
            }
            out = view_cmd.collapse_exports(env, cache)
            view = view_cmd.view_dir_for(sorted([a, b]), cache)
            self.assertIn('export PATH="%s/bin:/usr/bin:/bin"' % view, out)  # system kept
            self.assertIn('export LD_LIBRARY_PATH="%s/lib"' % view, out)     # deduped to one
            self.assertIn('export PYTHONPATH="%s/lib"' % view, out)          # PyROOT findable
            self.assertIn('export CMAKE_PREFIX_PATH="%s"' % view, out)       # roots → one view
            # setenvs are not collapsed
            self.assertNotIn("ROOTSYS", out)
            # and the remapped PyROOT module really exists in the built view
            self.assertTrue(os.path.exists(os.path.join(view, "lib", "ROOT.py")))

    def test_no_roots_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(view_cmd.collapse_exports({"PATH": "/usr/bin"},
                                                       os.path.join(d, "cache")), "")

    def test_macos_dyld_var_collapsed(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"), ["lib/libx.dylib"])
            out = view_cmd.collapse_exports(
                {"X_ROOT": a, "DYLD_LIBRARY_PATH": "%s/lib" % a},
                os.path.join(d, "cache"))
            view = view_cmd.view_dir_for([a], os.path.join(d, "cache"))
            self.assertIn('export DYLD_LIBRARY_PATH="%s/lib"' % view, out)

    def test_longest_root_wins_no_false_prefix(self):
        # a root must not prefix-shadow a sibling whose path starts the same way
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "ROOT"), ["bin/r"])
            ab = _pkg(os.path.join(d, "ROOTfoo"), ["bin/rf"])
            env = {"ROOT_ROOT": a, "ROOTFOO_ROOT": ab,
                   "PATH": "%s/bin:%s/bin" % (a, ab)}
            out = view_cmd.collapse_exports(env, os.path.join(d, "cache"))
            view = view_cmd.view_dir_for(sorted([a, ab]), os.path.join(d, "cache"))
            # both map under the same view/bin (merged), deduped to one entry
            self.assertIn('export PATH="%s/bin"' % view, out)


if __name__ == "__main__":
    unittest.main()
