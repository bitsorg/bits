"""Tests for bits_helpers/view.py (merged symlink-farm view)."""

import json
import os
import tempfile
import unittest

from bits_helpers.view import (build_view, view_env, DEFAULT_SUBDIRS,
                               collect_build_id_roots, build_published_view,
                               find_published_view, published_view_dirname,
                               CATALOG_FILE)


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


class TestPublishedView(unittest.TestCase):

    def _deployed_pkg(self, store, rel, files, build_id, arch="el9"):
        prefix = os.path.join(store, rel)
        _pkg(prefix, files)
        with open(os.path.join(prefix, ".meta.json"), "w") as fh:
            json.dump({"build_id": build_id, "architecture": arch}, fh)
        return prefix

    def test_collect_build_id_roots(self):
        with tempfile.TemporaryDirectory() as store:
            a = self._deployed_pkg(store, "el9/A/1.0", ["bin/a"], "L-1")
            b = self._deployed_pkg(store, "el9/B/2.0", ["bin/b"], "L-1")
            self._deployed_pkg(store, "el9/C/3.0", ["bin/c"], "OTHER")   # different id
            roots = collect_build_id_roots(store, "L-1", architecture="el9")
            self.assertEqual(roots, sorted([a, b]))

    def test_build_published_view_named_layout_and_catalog(self):
        with tempfile.TemporaryDirectory() as store:
            a = self._deployed_pkg(store, "el9/A/1.0", ["bin/a", "lib/liba.so"], "L-1")
            b = self._deployed_pkg(store, "el9/B/2.0", ["bin/b", "include/b.h"], "L-1")
            res = build_published_view([a, b], "myrel", "L-1", "el9", store)
            dirname = published_view_dirname("myrel", "L-1")
            view = os.path.join(store, "Views", dirname, "el9")
            self.assertEqual(res["view_dir"], view)
            self.assertEqual(dirname, "myrel-L-1")
            for rel in ("bin/a", "lib/liba.so", "bin/b", "include/b.h"):
                self.assertTrue(os.path.islink(os.path.join(view, rel)), rel)
            self.assertTrue(os.path.isfile(
                os.path.join(store, "Views", dirname, CATALOG_FILE)))
            # a consumer (knows only the build_id, not the name) finds it by suffix
            self.assertEqual(find_published_view(store, "L-1", "el9"), view)
            self.assertIsNone(find_published_view(store, "OTHER", "el9"))

    def test_published_view_symlinks_are_relative_and_resolve(self):
        with tempfile.TemporaryDirectory() as store:
            a = self._deployed_pkg(store, "el9/A/1.0", ["bin/a"], "L-1")
            build_published_view([a], "myrel", "L-1", "el9", store)
            link = os.path.join(find_published_view(store, "L-1", "el9"), "bin", "a")
            self.assertFalse(os.path.isabs(os.readlink(link)))
            self.assertTrue(os.path.exists(link))

    def test_republish_rebuilds_cleanly(self):
        # re-running publish over an existing view must not skip files as conflicts
        with tempfile.TemporaryDirectory() as store:
            a = self._deployed_pkg(store, "el9/A/1.0", ["bin/a"], "L-1")
            build_published_view([a], "myrel", "L-1", "el9", store)
            b = self._deployed_pkg(store, "el9/B/2.0", ["bin/b"], "L-1")
            res = build_published_view([a, b], "myrel", "L-1", "el9", store)
            view = find_published_view(store, "L-1", "el9")
            self.assertEqual(res["conflicts"], [])                       # clean
            self.assertTrue(os.path.islink(os.path.join(view, "bin", "a")))
            self.assertTrue(os.path.islink(os.path.join(view, "bin", "b")))

    def test_custom_views_dir_built_and_found(self):
        with tempfile.TemporaryDirectory() as store:
            a = self._deployed_pkg(store, "el9/A/1.0", ["bin/a"], "L-1")
            res = build_published_view([a], "myrel", "L-1", "el9", store,
                                       views_dir="release-views")
            self.assertEqual(res["view_dir"],
                             os.path.join(store, "release-views", "myrel-L-1", "el9"))
            # default Views lookup misses; the configured one finds it
            self.assertIsNone(find_published_view(store, "L-1", "el9"))
            self.assertEqual(
                find_published_view(store, "L-1", "el9", views_dir="release-views"),
                res["view_dir"])


if __name__ == "__main__":
    unittest.main()
