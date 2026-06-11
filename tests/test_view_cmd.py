"""Tests for bits_helpers/view_cmd.py (interactive view-env collapse)."""

import json
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


ARCH = "arch"


def _client_view(work_dir, roots):
    """The client-cache view dir collapse_exports would build for *roots*."""
    cache = os.path.join(work_dir, view_cmd.CLIENT_CACHE_SUBDIR, ARCH)
    return view_cmd.view_dir_for(sorted(roots), cache)


class TestCollapseExports(unittest.TestCase):

    def test_remaps_entries_dedups_keeps_system_and_setenvs(self):
        with tempfile.TemporaryDirectory() as d:
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
            out = view_cmd.collapse_exports(env, d, ARCH)
            view = _client_view(d, [a, b])                     # no build_id → client view
            self.assertIn('export PATH="%s/bin:/usr/bin:/bin"' % view, out)  # system kept
            self.assertIn('export LD_LIBRARY_PATH="%s/lib"' % view, out)     # deduped to one
            self.assertIn('export PYTHONPATH="%s/lib"' % view, out)          # PyROOT findable
            self.assertIn('export CMAKE_PREFIX_PATH="%s"' % view, out)       # roots → one view
            self.assertNotIn("ROOTSYS", out)                                 # setenv untouched
            self.assertTrue(os.path.exists(os.path.join(view, "lib", "ROOT.py")))

    def test_no_roots_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                view_cmd.collapse_exports({"PATH": "/usr/bin"}, d, ARCH), "")

    def test_macos_dyld_var_collapsed(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"), ["lib/libx.dylib"])
            out = view_cmd.collapse_exports(
                {"X_ROOT": a, "DYLD_LIBRARY_PATH": "%s/lib" % a}, d, ARCH)
            self.assertIn('export DYLD_LIBRARY_PATH="%s/lib"' % _client_view(d, [a]), out)

    def test_longest_root_wins_no_false_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "ROOT"), ["bin/r"])
            ab = _pkg(os.path.join(d, "ROOTfoo"), ["bin/rf"])
            env = {"ROOT_ROOT": a, "ROOTFOO_ROOT": ab,
                   "PATH": "%s/bin:%s/bin" % (a, ab)}
            out = view_cmd.collapse_exports(env, d, ARCH)
            self.assertIn('export PATH="%s/bin"' % _client_view(d, [a, ab]), out)


class TestShellSyntax(unittest.TestCase):

    def test_csh_uses_setenv(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"), ["bin/x"])
            env = {"A_ROOT": a, "PATH": "%s/bin:/usr/bin" % a}
            view = _client_view(d, [a])
            sh = view_cmd.collapse_exports(env, d, ARCH, shell="sh")
            csh = view_cmd.collapse_exports(env, d, ARCH, shell="csh")
            self.assertIn('export PATH="%s/bin:/usr/bin"' % view, sh)
            self.assertIn('setenv PATH "%s/bin:/usr/bin";' % view, csh)


class TestPublishedPreference(unittest.TestCase):

    def _pkg_with_meta(self, prefix, files, build_id):
        _pkg(prefix, files)
        with open(os.path.join(prefix, ".meta.json"), "w") as fh:
            json.dump({"build_id": build_id}, fh)
        return prefix

    def test_closure_build_id(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._pkg_with_meta(os.path.join(d, "a"), ["bin/x"], "L-1")
            b = self._pkg_with_meta(os.path.join(d, "b"), ["bin/y"], "L-1")
            c = _pkg(os.path.join(d, "c"), ["bin/z"])            # no meta
            self.assertEqual(view_cmd.closure_build_id([a, b]), "L-1")   # unanimous
            self.assertIsNone(view_cmd.closure_build_id([a, c]))         # mixed/absent

    def test_prefers_published_view_when_build_id_matches(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._pkg_with_meta(os.path.join(d, "a"), ["bin/x", "lib/la.so"], "L-1")
            # a published view named "rel-L-1" exists; the client finds it by build_id
            pub = os.path.join(d, "Views", "rel-L-1", ARCH)
            os.makedirs(os.path.join(pub, "bin"))
            env = {"A_ROOT": a, "PATH": "%s/bin:/usr/bin" % a,
                   "LD_LIBRARY_PATH": "%s/lib" % a}
            out = view_cmd.collapse_exports(env, d, ARCH)
            # entries remapped onto the PUBLISHED view, not a freshly built client one
            self.assertIn('export PATH="%s/bin:/usr/bin"' % pub, out)
            self.assertIn('export LD_LIBRARY_PATH="%s/lib"' % pub, out)
            # no client cache was built
            self.assertFalse(os.path.exists(
                os.path.join(d, view_cmd.CLIENT_CACHE_SUBDIR)))

    def test_falls_back_to_client_when_no_published(self):
        with tempfile.TemporaryDirectory() as d:
            a = self._pkg_with_meta(os.path.join(d, "a"), ["bin/x"], "L-1")  # no pub dir
            env = {"A_ROOT": a, "PATH": "%s/bin" % a}
            out = view_cmd.collapse_exports(env, d, ARCH)
            self.assertIn('export PATH="%s/bin"' % _client_view(d, [a]), out)


class TestPruneViews(unittest.TestCase):

    def _build(self, roots, view_dir):
        os.makedirs(view_dir, exist_ok=True)

    def test_touch_on_use_then_age_based_gc(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "cache")
            fresh = view_cmd.ensure_view([os.path.join(d, "a")], cache, _build=self._build)
            stale = view_cmd.ensure_view([os.path.join(d, "b")], cache, _build=self._build)
            # age the 'stale' view's stamp to 2 days ago
            old = time.time() - 2 * 86400
            os.utime(os.path.join(stale, view_cmd.READY_STAMP), (old, old))

            removed = view_cmd.prune_views(cache, ttl_days=1)
            self.assertEqual(removed, [os.path.basename(stale)])
            self.assertFalse(os.path.exists(stale))
            self.assertTrue(os.path.exists(fresh))      # recently used → kept

    def test_ensure_view_refreshes_stamp(self):
        import time
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "cache")
            v = view_cmd.ensure_view([os.path.join(d, "a")], cache, _build=self._build)
            stamp = os.path.join(v, view_cmd.READY_STAMP)
            old = time.time() - 5 * 86400
            os.utime(stamp, (old, old))
            view_cmd.ensure_view([os.path.join(d, "a")], cache, _build=self._build)  # re-use
            self.assertGreater(os.path.getmtime(stamp), old)   # touched

    def test_gc_disabled_when_ttl_zero(self):
        with tempfile.TemporaryDirectory() as d:
            cache = os.path.join(d, "cache")
            view_cmd.ensure_view([os.path.join(d, "a")], cache, _build=self._build)
            self.assertEqual(view_cmd.prune_views(cache, ttl_days=0), [])


if __name__ == "__main__":
    unittest.main()
