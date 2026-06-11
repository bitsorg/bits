#!/usr/bin/env python3
"""Tests for the relaxed-reuse frontier-cut in getPackageList (ADR-0001 Stage 1b).

A `performCvmfsMatch` callback that returns a deployed-package descriptor causes
that package to be grafted: kept in `specs` (so consumers still depend on it and
source its deployed init.sh) but with its dependency subtree pruned and tagged
`from_cvmfs`. With the default callback (None) the resolver is unchanged.
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bits_helpers.utilities import getPackageList


RECIPES = {
    "myapp":   "package: myapp\nversion: v1\nrequires:\n  - mydep\n---\n",
    "mydep":   "package: mydep\nversion: v1\nrequires:\n  - mysubdep\n---\n",
    "mysubdep": "package: mysubdep\nversion: v1\n---\n",
    "defaults-release": "package: defaults-release\nversion: v1\n---\n",
}


def _resolve(perform_cvmfs_match):
    specs = {}

    def fake_resolveFilename(taps, pkg, configDir, genPkgs):
        return (pkg + ".sh", "/pkgdir")

    def fake_getRecipeReader(filename, *a, **k):
        pkg = filename.replace(".sh", "")
        content = RECIPES.get(pkg, "package: {p}\nversion: v1\n---\n".format(p=pkg))
        return lambda: content

    with patch("bits_helpers.utilities.resolveFilename", side_effect=fake_resolveFilename), \
         patch("bits_helpers.utilities.getRecipeReader", side_effect=fake_getRecipeReader), \
         patch("bits_helpers.utilities.getGeneratedPackages", return_value={"/pkgdir": {}}), \
         patch("bits_helpers.utilities.load_for_spec", return_value=None), \
         patch("bits_helpers.utilities.merge_into_spec", return_value=None):
        getPackageList(
            packages=["myapp"], specs=specs, configDir="/fake",
            preferSystem=False, noSystem="*", architecture="slc9_x86-64",
            disable=[], defaults=["release"],
            performPreferCheck=lambda pkg, cmd: (1, ""),
            performRequirementCheck=lambda pkg, cmd: (0, ""),
            performValidateDefaults=lambda spec: (True, "", None),
            overrides={"defaults-release": {}}, taps={},
            log=lambda *a, **k: None, defaults_meta=None,
            performCvmfsMatch=perform_cvmfs_match,
        )
    return specs


class TestRelaxedFrontierCut(unittest.TestCase):

    def test_no_callback_is_unchanged(self):
        specs = _resolve(None)
        for p in ("myapp", "mydep", "mysubdep"):
            self.assertIn(p, specs)
            self.assertNotIn("from_cvmfs", specs[p])

    def test_graft_prunes_subtree_but_keeps_consumer_dep(self):
        # Graft 'mydep' from a blessed release: it stays in specs, its subtree
        # ('mysubdep') is pruned, and the consumer ('myapp') still depends on it.
        def match(spec):
            if spec["package"] == "mydep":
                return {"path": "/cvmfs/rel/mydep/v1", "build_id": "LCG_109-x",
                        "hash": "deadbeef", "version": "v1"}
            return None

        specs = _resolve(match)
        self.assertIn("mydep", specs)
        self.assertTrue(specs["mydep"]["from_cvmfs"])
        self.assertEqual(specs["mydep"]["cvmfs_path"], "/cvmfs/rel/mydep/v1")
        self.assertEqual(specs["mydep"]["cvmfs_hash"], "deadbeef")
        self.assertEqual(specs["mydep"]["requires"], [])
        # subtree pruned
        self.assertNotIn("mysubdep", specs)
        # consumer still present and still depends on the grafted package
        self.assertIn("myapp", specs)
        self.assertNotIn("from_cvmfs", specs["myapp"])
        self.assertIn("mydep", specs["myapp"]["requires"])

    def test_no_match_builds_everything(self):
        specs = _resolve(lambda spec: None)
        for p in ("myapp", "mydep", "mysubdep"):
            self.assertIn(p, specs)
            self.assertNotIn("from_cvmfs", specs[p])


if __name__ == "__main__":
    unittest.main()
