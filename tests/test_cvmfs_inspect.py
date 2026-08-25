# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers/cvmfs_inspect — read-only CVMFS tree inspection."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers import cvmfs_inspect as I


def _meta(arch, name, ver, rev, build_id="bid-1", provenance="pure", deps=None):
    return {
        "architecture": arch, "abi_tag": arch, "build_id": build_id,
        "defaults": ["lcg", "release"], "reuse_policy": "strict",
        "provenance": provenance, "bits_version": "0.1", "dist": {"commit": "c" * 40},
        "package": {"name": name, "version": ver, "revision": rev, "hash": "h" + name},
        "dependencies": deps or {"direct": {"build": [], "runtime": []},
                                 "recursive": {"build": [], "runtime": []}},
    }


class CvmfsInspectTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self._pkg("x86_64-el9-gcc14-opt", "Davix", "0.8.10-1", _meta(
            "x86_64-el9-gcc14-opt", "Davix", "0.8.10", "1",
            deps={"direct": {"build": [{"name": "bits-recipe-tools", "version": "0.0.32", "revision": "1"}],
                             "runtime": [{"name": "Boost", "version": "1.90.0", "revision": "1"}]},
                  "recursive": {"build": [], "runtime": []}}))
        self._pkg("x86_64-el9-gcc14-opt", "Boost", "1.90.0-1",
                  _meta("x86_64-el9-gcc14-opt", "Boost", "1.90.0", "1"))
        self._pkg("aarch64-el9-gcc14-opt", "Davix", "0.8.10-1",
                  _meta("aarch64-el9-gcc14-opt", "Davix", "0.8.10", "1", build_id="bid-arm"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def _pkg(self, arch, name, verrev, meta):
        d = os.path.join(self.root, arch, "Packages", name, verrev)
        os.makedirs(d)
        with open(os.path.join(d, ".meta.json"), "w") as fh:
            json.dump(meta, fh)

    def test_list_platforms(self):
        self.assertEqual(I.list_platforms(self.root),
                         ["aarch64-el9-gcc14-opt", "x86_64-el9-gcc14-opt"])

    def test_list_packages_and_resolve(self):
        pk = I.list_packages(self.root, "x86_64-el9-gcc14-opt")
        self.assertEqual(sorted(pk), ["Boost", "Davix"])
        self.assertEqual(I.resolve_verrev(self.root, "x86_64-el9-gcc14-opt", "Davix"),
                         "0.8.10-1")
        self.assertEqual(
            I.resolve_verrev(self.root, "x86_64-el9-gcc14-opt", "Davix", "0.8.10"),
            "0.8.10-1")
        self.assertIsNone(I.resolve_verrev(self.root, "x86_64-el9-gcc14-opt", "Nope"))

    def test_classify_three_states(self):
        host = {"os": "el9", "_machine": "x86_64", "machine": "x86_64"}
        self.assertEqual(I.classify_platform("x86_64-el9-gcc14-opt", host)[0], "native")
        # right machine, wrong OS -> needs a container
        self.assertEqual(I.classify_platform("x86_64-el8-gcc14-opt", host)[0], "container")
        # wrong machine -> incompatible
        self.assertEqual(I.classify_platform("aarch64-el9-gcc14-opt", host)[0], "incompatible")

    def test_format_meta_deps(self):
        m = I.read_meta(self.root, "x86_64-el9-gcc14-opt", "Davix", "0.8.10-1")
        out = I.format_meta(m, deps=True)
        self.assertIn("Davix  0.8.10-1", out)
        self.assertIn("build_id:", out)
        self.assertIn("provenance:   pure", out)
        self.assertIn("bits-recipe-tools 0.0.32-1", out)   # dep tree present
        # provenance_only drops the dep tree
        self.assertNotIn("bits-recipe-tools", I.format_meta(m, provenance_only=True))

    def test_summarize_groups_by_build_id(self):
        pkgs, bids = I.summarize(self.root, "x86_64-el9-gcc14-opt")
        self.assertEqual(sorted(pkgs), ["Boost", "Davix"])
        self.assertEqual(sorted(bids), ["bid-1"])
        self.assertEqual(len(bids["bid-1"]), 2)

    # ── `show` platform selection ────────────────────────────────────────────
    class _Args:
        def __init__(self, root, package, arch=None, deps=False,
                     provenance=False, json=False):
            self.cvmfs, self.package, self.arch = root, package, arch
            self.deps, self.provenance, self.json = deps, provenance, json

    def test_show_no_arch_lists_every_platform(self):
        # Davix exists on both platforms -> compact multi-platform view (JSON).
        import io, contextlib
        a = self._Args(self.root, "Davix", json=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(I._cmd_show(a), 0)
        got = json.loads(buf.getvalue())
        self.assertEqual(sorted(r["arch"] for r in got),
                         ["aarch64-el9-gcc14-opt", "x86_64-el9-gcc14-opt"])
        self.assertTrue(all(r["verrev"] == "0.8.10-1" for r in got))

    def test_show_no_arch_single_hit_is_full_detail(self):
        # Boost is on one platform only -> falls through to full detail.
        import io, contextlib
        a = self._Args(self.root, "Boost")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(I._cmd_show(a), 0)
        out = buf.getvalue()
        self.assertIn("Boost  1.90.0-1", out)
        self.assertIn("build_id:", out)          # detail block, not a table row

    def test_show_with_arch_is_full_detail(self):
        import io, contextlib
        a = self._Args(self.root, "Davix", arch="aarch64-el9-gcc14-opt")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(I._cmd_show(a), 0)
        self.assertIn("bid-arm", buf.getvalue())

    def test_show_not_found_anywhere(self):
        import io, contextlib
        a = self._Args(self.root, "Nope")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(I._cmd_show(a), 1)
        self.assertIn("not found on any platform", buf.getvalue())

    # ── legacy (pre-bits) trees without .meta.json ───────────────────────────
    def _legacy_pkg(self, arch, name, verrev):
        # a package dir with NO .meta.json, like /cvmfs/alice.cern.ch/...
        os.makedirs(os.path.join(self.root, arch, "Packages", name, verrev))

    def test_meta_or_legacy_absent_is_synthesized(self):
        self._legacy_pkg("el5-x86_64", "xrootd", "v3.3.3")
        m = I.meta_or_legacy(self.root, "el5-x86_64", "xrootd", "v3.3.3")
        self.assertTrue(m.get("_legacy"))
        self.assertEqual(m["package"]["name"], "xrootd")
        self.assertEqual(m["package"]["version"], "v3.3.3")
        self.assertEqual(m["architecture"], "el5-x86_64")

    def test_meta_or_legacy_corrupt_still_raises(self):
        d = os.path.join(self.root, "el5-x86_64", "Packages", "bad", "v1")
        os.makedirs(d)
        with open(os.path.join(d, ".meta.json"), "w") as fh:
            fh.write("{ not json")
        with self.assertRaises(ValueError):
            I.meta_or_legacy(self.root, "el5-x86_64", "bad", "v1")

    def test_show_no_arch_json_is_list_even_for_single_hit(self):
        # Stable JSON shape: no-arch --json always returns a per-platform list,
        # so a caller sees the same shape whether 1 or N platforms match.
        import io, contextlib
        a = self._Args(self.root, "Boost", json=True)       # Boost: single platform
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(I._cmd_show(a), 0)
        got = json.loads(buf.getvalue())
        self.assertIsInstance(got, list)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["arch"], "x86_64-el9-gcc14-opt")

    def test_show_across_keeps_unreadable_platform_visible(self):
        # A corrupt .meta.json on one platform must not silently vanish.
        d = os.path.join(self.root, "aarch64-el9-gcc14-opt", "Packages",
                         "Boost", "1.90.0-1")
        os.makedirs(d)
        with open(os.path.join(d, ".meta.json"), "w") as fh:
            fh.write("{ broken")
        import io, contextlib
        a = self._Args(self.root, "Boost", json=True)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(I._cmd_show(a), 0)
        got = {r["arch"]: r for r in json.loads(buf.getvalue())}
        self.assertEqual(len(got), 2)                        # both platforms present
        self.assertEqual(got["aarch64-el9-gcc14-opt"]["provenance"],
                         "unreadable .meta.json")

    def test_meta_or_legacy_dangling_symlink_raises(self):
        d = os.path.join(self.root, "el5-x86_64", "Packages", "xrootd", "v3.3.3")
        os.makedirs(d)
        os.symlink("/no/such/target", os.path.join(d, ".meta.json"))
        with self.assertRaises(OSError):                     # not treated as legacy
            I.meta_or_legacy(self.root, "el5-x86_64", "xrootd", "v3.3.3")

    def test_show_legacy_detail_notes_missing_meta(self):
        import io, contextlib
        self._legacy_pkg("el5-x86_64", "xrootd", "v3.3.3")
        a = self._Args(self.root, "xrootd", arch="el5-x86_64")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(I._cmd_show(a), 0)
        out = buf.getvalue()
        self.assertIn("xrootd  v3.3.3", out)
        self.assertNotIn("v3.3.3-", out)          # no trailing dash when no revision
        self.assertIn("legacy", out)
        self.assertIn("no .meta.json", out)


if __name__ == "__main__":
    unittest.main()
