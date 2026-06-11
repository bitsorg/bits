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

    def test_replaces_path_vars_keeps_setenvs(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"),
                     ["bin/x", "lib/libx.so", "lib/pkgconfig/x.pc",
                      "lib/python3.13/site-packages/x.py"])
            env = {"X_ROOT": a, "ROOTSYS": a, "XRD_TIMEOUT": "150"}
            out = view_cmd.collapse_exports(env, os.path.join(d, "cache"),
                                            system_path="/usr/bin:/bin")
            view = view_cmd.view_dir_for([a], os.path.join(d, "cache"))
            # path-list vars collapse to single view entries
            self.assertIn('export PATH="%s/bin:/usr/bin:/bin"' % view, out)
            self.assertIn('export CMAKE_PREFIX_PATH="%s"' % view, out)
            self.assertIn('export LD_LIBRARY_PATH="%s/lib"' % view, out)
            self.assertIn('export PKG_CONFIG_PATH="%s/lib/pkgconfig"' % view, out)
            self.assertIn("python3.13/site-packages", out)
            # the collapse helper does NOT touch setenvs (ROOTSYS/XRD_*); the
            # caller keeps those from the module load.
            self.assertNotIn("ROOTSYS", out)
            self.assertNotIn("XRD_TIMEOUT", out)

    def test_no_roots_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(view_cmd.collapse_exports({"PATH": "/usr/bin"},
                                                       os.path.join(d, "cache")), "")

    def test_macos_lib_var(self):
        with tempfile.TemporaryDirectory() as d:
            a = _pkg(os.path.join(d, "a"), ["lib/libx.dylib"])
            out = view_cmd.collapse_exports({"X_ROOT": a}, os.path.join(d, "cache"),
                                            lib_path_var="DYLD_LIBRARY_PATH")
            self.assertIn("export DYLD_LIBRARY_PATH=", out)
            self.assertNotIn("export LD_LIBRARY_PATH=", out)


if __name__ == "__main__":
    unittest.main()
