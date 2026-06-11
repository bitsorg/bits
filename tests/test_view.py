"""Tests for bits_helpers/view.py (merged symlink-farm view)."""

import os
import tempfile
import unittest

from bits_helpers.view import build_view, view_env, DEFAULT_SUBDIRS


def _pkg(prefix, files):
    """Create a fake install prefix with the given relative files (touch)."""
    for rel in files:
        path = os.path.join(prefix, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(rel)
    return prefix


class TestBuildView(unittest.TestCase):

    def test_merges_two_packages_into_one_tree(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"),
                     ["bin/aa", "lib/liba.so", "include/a.h",
                      "lib/pkgconfig/a.pc", ".meta.json"])
            b = _pkg(os.path.join(d, "b"),
                     ["bin/bb", "lib64/libb.so",
                      "lib/python3.13/site-packages/b.py", ".meta.json"])
            view = os.path.join(d, "view")
            res = build_view([a, b], view)

            # consumable files from both packages are present...
            for rel in ("bin/aa", "lib/liba.so", "include/a.h",
                        "lib/pkgconfig/a.pc", "bin/bb", "lib64/libb.so",
                        "lib/python3.13/site-packages/b.py"):
                self.assertTrue(os.path.islink(os.path.join(view, rel)), rel)
            # ...metadata is NOT merged (would collide on every package)
            self.assertFalse(os.path.exists(os.path.join(view, ".meta.json")))
            self.assertEqual(res["conflicts"], [])

    def test_links_are_relative_and_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"), ["bin/tool"])
            view = os.path.join(d, "view")
            build_view([a], view)
            link = os.path.join(view, "bin", "tool")
            self.assertFalse(os.path.isabs(os.readlink(link)))   # relative
            self.assertTrue(os.path.exists(link))                # resolves
            with open(link) as fh:
                self.assertEqual(fh.read(), "bin/tool")

    def test_conflict_first_writer_wins_and_is_reported(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"), ["bin/python"])
            b = _pkg(os.path.join(d, "b"), ["bin/python"])
            view = os.path.join(d, "view")
            res = build_view([a, b], view)            # a has priority
            self.assertEqual(len(res["conflicts"]), 1)
            relkey, winner, loser = res["conflicts"][0]
            self.assertEqual(relkey, "bin/python")
            self.assertIn(os.path.join("a", "bin", "python"), winner)
            self.assertIn(os.path.join("b", "bin", "python"), loser)
            # the winner (a) is the one actually linked
            with open(os.path.join(view, "bin", "python")) as fh:
                self.assertEqual(fh.read(), "bin/python")

    def test_missing_subdirs_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"), ["bin/x"])   # no lib/include/share
            view = os.path.join(d, "view")
            res = build_view([a], view)
            self.assertEqual(res["linked"], ["bin/x"])
            self.assertFalse(os.path.exists(os.path.join(view, "lib")))


class TestViewEnv(unittest.TestCase):

    def test_one_entry_per_var_for_existing_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"),
                     ["bin/x", "lib/libx.so", "lib/pkgconfig/x.pc",
                      "lib/python3.13/site-packages/x.py"])
            view = os.path.join(d, "view")
            build_view([a], view)
            env = view_env(view, python_mm="3.13")
            self.assertEqual(env["PATH"], os.path.join(view, "bin"))
            self.assertEqual(env["CMAKE_PREFIX_PATH"], view)
            self.assertEqual(env["LD_LIBRARY_PATH"], os.path.join(view, "lib"))  # no lib64
            self.assertEqual(env["PKG_CONFIG_PATH"], os.path.join(view, "lib", "pkgconfig"))
            self.assertEqual(env["PYTHONPATH"],
                             os.path.join(view, "lib", "python3.13", "site-packages"))

    def test_absent_dirs_omitted(self):
        with tempfile.TemporaryDirectory() as d:
            view = os.path.join(d, "view")
            os.makedirs(view)
            env = view_env(view, python_mm="3.13")
            # only CMAKE_PREFIX_PATH (the view root itself) survives
            self.assertEqual(set(env), {"CMAKE_PREFIX_PATH"})

    def test_macos_lib_path_var(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"), ["lib/libx.dylib"])
            view = os.path.join(d, "view")
            build_view([a], view)
            env = view_env(view, lib_path_var="DYLD_LIBRARY_PATH")
            self.assertIn("DYLD_LIBRARY_PATH", env)
            self.assertNotIn("LD_LIBRARY_PATH", env)


if __name__ == "__main__":
    unittest.main()
