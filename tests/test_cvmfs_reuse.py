# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tests for bits_helpers/cvmfs_reuse.graftable_match() (ADR-0001 Stage 1b).

The matcher is read-only and not yet wired into the resolver, so these tests
fully cover its behaviour against a faked deployed-store tree.
"""

import json
import os
import tempfile
import unittest

from bits_helpers.cvmfs_reuse import graftable_match, select_build_id


ARCH = "ubuntu2510_x86-64-gcc15-dbg"


def _deploy(root, pkg, version, revision="1", *, build_id=None, architecture=ARCH,
            pkg_hash="h", write_meta=True):
    """Create <root>/<arch>/Packages/<pkg>/<version>-<revision>/ + .meta.json.

    The directory is named version-revision (as bits deploys); version/revision
    are recorded authoritatively in .meta.json's package field.
    """
    d = os.path.join(root, architecture, "Packages", pkg, "%s-%s" % (version, revision))
    os.makedirs(d, exist_ok=True)
    if write_meta:
        meta = {"architecture": architecture,
                "package": {"hash": pkg_hash, "version": version, "revision": revision}}
        if build_id is not None:
            meta["build_id"] = build_id
        with open(os.path.join(d, ".meta.json"), "w") as fh:
            json.dump(meta, fh)
    return d


class TestGraftableMatch(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_match_on_name_arch_build_id(self):
        d = _deploy(self.root, "ROOT", "6.38.00", "1", build_id="LCG_109-abc")
        m = graftable_match("ROOT", ARCH, "LCG_109-abc", self.root)
        self.assertIsNotNone(m)
        # version/revision come from .meta.json, not the dir basename
        self.assertEqual(m["version"], "6.38.00")
        self.assertEqual(m["revision"], "1")
        self.assertEqual(m["path"], d)
        self.assertEqual(m["hash"], "h")
        self.assertEqual(m["build_id"], "LCG_109-abc")

    def test_no_match_wrong_build_id(self):
        _deploy(self.root, "ROOT", "6.38.00-1", build_id="LCG_108-zzz")
        self.assertIsNone(graftable_match("ROOT", ARCH, "LCG_109-abc", self.root))

    def test_no_match_wrong_arch(self):
        _deploy(self.root, "ROOT", "6.38.00-1", build_id="LCG_109-abc")
        self.assertIsNone(
            graftable_match("ROOT", "osx_arm64_gcc15", "LCG_109-abc", self.root))

    def test_no_match_missing_store(self):
        self.assertIsNone(graftable_match("ROOT", ARCH, "LCG_109-abc",
                                          os.path.join(self.root, "nope")))

    def test_legacy_deploy_without_build_id_never_matches(self):
        _deploy(self.root, "ROOT", "6.38.00-1", build_id=None)   # pre-Stage-0
        self.assertIsNone(graftable_match("ROOT", ARCH, "LCG_109-abc", self.root))

    def test_no_meta_json_never_matches(self):
        _deploy(self.root, "ROOT", "6.38.00-1", write_meta=False)
        self.assertIsNone(graftable_match("ROOT", ARCH, "LCG_109-abc", self.root))

    def test_picks_the_matching_version_among_several(self):
        _deploy(self.root, "Boost", "1.88.0", "1", build_id="OTHER")
        _deploy(self.root, "Boost", "1.90.0", "1", build_id="LCG_109-abc")
        m = graftable_match("Boost", ARCH, "LCG_109-abc", self.root)
        self.assertIsNotNone(m)
        self.assertEqual(m["version"], "1.90.0")
        self.assertEqual(m["revision"], "1")

    def test_empty_inputs_are_safe(self):
        self.assertIsNone(graftable_match("", ARCH, "x", self.root))
        self.assertIsNone(graftable_match("ROOT", "", "x", self.root))
        self.assertIsNone(graftable_match("ROOT", ARCH, "", self.root))
        self.assertIsNone(graftable_match("ROOT", ARCH, "x", ""))


class TestSelectBuildId(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()

    def _mtime(self, d, t):
        os.utime(os.path.join(d, ".meta.json"), (t, t))

    def test_latest_anchors_on_build_target(self):
        # ROOT is the target (last requested). Newer build_id must win for it,
        # regardless of what CMake carries.
        old = _deploy(self.root, "ROOT", "6.0", "1", build_id="rel-old")
        new = _deploy(self.root, "ROOT", "6.1", "1", build_id="rel-new")
        self._mtime(old, 1000); self._mtime(new, 2000)
        _deploy(self.root, "CMake", "3.30", "1", build_id="rel-old")
        bid, cov = select_build_id(["CMake", "ROOT"], ARCH, self.root, "latest")
        self.assertEqual(bid, "rel-new")
        self.assertIn("ROOT", cov["rel-new"])

    def test_latest_common_requires_all_packages(self):
        # rel-new is newest but only ROOT has it; rel-old is shared by both.
        r_old = _deploy(self.root, "ROOT", "6.0", "1", build_id="rel-old")
        r_new = _deploy(self.root, "ROOT", "6.1", "1", build_id="rel-new")
        c_old = _deploy(self.root, "CMake", "3.30", "1", build_id="rel-old")
        self._mtime(r_old, 1000); self._mtime(r_new, 2000); self._mtime(c_old, 1000)
        bid, _ = select_build_id(["CMake", "ROOT"], ARCH, self.root, "latest-common")
        self.assertEqual(bid, "rel-old")

    def test_latest_common_tolerates_duplicate_packages(self):
        # A duplicated target must not make the shared-by-all test unsatisfiable.
        _deploy(self.root, "ROOT", "6.1", "1", build_id="rel-x")
        bid, _ = select_build_id(["ROOT", "ROOT"], ARCH, self.root, "latest-common")
        self.assertEqual(bid, "rel-x")

    def test_latest_common_none_when_no_shared_id(self):
        _deploy(self.root, "ROOT", "6.1", "1", build_id="rel-a")
        _deploy(self.root, "CMake", "3.30", "1", build_id="rel-b")
        bid, _ = select_build_id(["CMake", "ROOT"], ARCH, self.root, "latest-common")
        self.assertIsNone(bid)

    def test_none_when_store_empty_or_no_build_id(self):
        self.assertEqual((None, {}), select_build_id(["ROOT"], ARCH, self.root, "latest"))
        _deploy(self.root, "ROOT", "6.1", "1", build_id=None)  # legacy: no build_id
        bid, cov = select_build_id(["ROOT"], ARCH, self.root, "latest")
        self.assertIsNone(bid)
        self.assertEqual(cov, {})


if __name__ == "__main__":
    unittest.main()
