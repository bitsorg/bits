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


if __name__ == "__main__":
    unittest.main()
