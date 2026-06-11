"""
Tests for bits_helpers/cvmfs_reuse.graftable_match() (ADR-0001 Stage 1b).

The matcher is read-only and not yet wired into the resolver, so these tests
fully cover its behaviour against a faked deployed-store tree.
"""

import json
import os
import tempfile
import unittest

from bits_helpers.cvmfs_reuse import graftable_match


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


if __name__ == "__main__":
    unittest.main()
