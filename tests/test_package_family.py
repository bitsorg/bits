#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the package_family feature (Option C).

Covers:
  - resolve_pkg_family(): glob matching, default fallback, no-config fallback
  - spec["pkg_family"] set correctly by getPackageList()
  - _pkg_install_path(): path construction with and without family
  - generate_initdotsh(): init.sh paths use the dep's family segment
  - storeHashes(): pkg_family is included in the hash
"""
import os
import sys
import unittest
from collections import OrderedDict
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bits_helpers.utilities import resolve_tag
from bits_helpers.defaults import resolve_pkg_family
from bits_helpers.packages import getPackageList
from bits_helpers.recipe import parseRecipe
from bits_helpers.build import _pkg_install_path
from bits_helpers.hashing import storeHashes
from bits_helpers.initdotsh import generate_initdotsh


# ---------------------------------------------------------------------------
# resolve_pkg_family
# ---------------------------------------------------------------------------

class TestResolvePkgFamily(unittest.TestCase):

    FAMILY_CFG = {
        "package_family": {
            "default": "cms",
            "lcg": ["ROOT", "SCRAMV1", "demo2"],
            "externals": ["boost", "zlib", "xz-*"],
        }
    }

    def test_exact_match(self):
        self.assertEqual(resolve_pkg_family(self.FAMILY_CFG, "ROOT"), "lcg")

    def test_exact_match_second_family(self):
        self.assertEqual(resolve_pkg_family(self.FAMILY_CFG, "boost"), "externals")

    def test_glob_wildcard(self):
        self.assertEqual(resolve_pkg_family(self.FAMILY_CFG, "xz-utils"), "externals")

    def test_glob_no_match_returns_default(self):
        self.assertEqual(resolve_pkg_family(self.FAMILY_CFG, "coral"), "cms")

    def test_no_package_family_key_returns_empty(self):
        self.assertEqual(resolve_pkg_family({}, "ROOT"), "")

    def test_package_family_not_dict_returns_empty(self):
        self.assertEqual(resolve_pkg_family({"package_family": None}, "ROOT"), "")
        self.assertEqual(resolve_pkg_family({"package_family": "bad"}, "ROOT"), "")

    def test_no_default_key_returns_empty_on_no_match(self):
        cfg = {"package_family": {"lcg": ["ROOT"]}}
        self.assertEqual(resolve_pkg_family(cfg, "coral"), "")

    def test_default_key_alone(self):
        cfg = {"package_family": {"default": "common"}}
        self.assertEqual(resolve_pkg_family(cfg, "anything"), "common")

    def test_case_sensitive(self):
        """Pattern matching is case-sensitive."""
        self.assertEqual(resolve_pkg_family(self.FAMILY_CFG, "root"), "cms")  # not lcg

    def test_question_mark_wildcard(self):
        cfg = {"package_family": {"ml": ["py?hon"]}}
        self.assertEqual(resolve_pkg_family(cfg, "python"), "ml")
        self.assertEqual(resolve_pkg_family(cfg, "pyython"), "")

    def test_data_glob(self):
        cfg = {"package_family": {"default": "cms", "cms": ["data-*"]}}
        self.assertEqual(resolve_pkg_family(cfg, "data-Geometry"), "cms")
        self.assertEqual(resolve_pkg_family(cfg, "data-"), "cms")
        # "data" without dash should fall to default (which is also cms here, but matched differently)
        self.assertEqual(resolve_pkg_family(cfg, "notdata"), "cms")

    def test_patterns_not_a_list_are_skipped(self):
        """If a family has a non-list value it is skipped gracefully."""
        cfg = {"package_family": {"default": "cms", "bad": "ROOT"}}
        self.assertEqual(resolve_pkg_family(cfg, "ROOT"), "cms")

    def test_defaults_release_gets_empty_family(self):
        """The defaults package itself should get an empty family (no install dir)."""
        self.assertEqual(resolve_pkg_family(self.FAMILY_CFG, "defaults-release"), "")


# ---------------------------------------------------------------------------
# _pkg_install_path
# ---------------------------------------------------------------------------

class TestPkgInstallPath(unittest.TestCase):

    def _spec(self, pkg_family=""):
        return {
            "package": "ROOT",
            "version": "v6-30-06",
            "revision": "1",
            "pkg_family": pkg_family,
        }

    def test_no_family_legacy_layout(self):
        path = _pkg_install_path("sw", "slc9_x86-64", self._spec(""))
        self.assertEqual(path, "sw/slc9_x86-64/ROOT/v6-30-06-1")

    def test_with_family(self):
        path = _pkg_install_path("sw", "slc9_x86-64", self._spec("lcg"))
        self.assertEqual(path, "sw/slc9_x86-64/lcg/ROOT/v6-30-06-1")

    def test_missing_pkg_family_key(self):
        spec = {"package": "ROOT", "version": "v6", "revision": "2"}
        path = _pkg_install_path("sw", "osx_x86-64", spec)
        self.assertEqual(path, "sw/osx_x86-64/ROOT/v6-2")

    def test_nested_workdir(self):
        path = _pkg_install_path("/opt/sw", "slc9_x86-64", self._spec("cms"))
        self.assertEqual(path, "/opt/sw/slc9_x86-64/cms/ROOT/v6-30-06-1")


# ---------------------------------------------------------------------------
# generate_initdotsh — family path segments
# ---------------------------------------------------------------------------

class TestGenerateInitdotshFamily(unittest.TestCase):
    """Verify that dep sourcing paths and _ROOT exports use the family segment."""

    def _make_specs(self, dep_family="", self_family=""):
        return {
            "DepPkg": {
                "package": "DepPkg",
                "version": "v1",
                "revision": "1",
                "pkg_family": dep_family,
                "requires": [],
                "hash": "abc123",
                "commit_hash": "deadbeef",
                "is_devel_pkg": False,
            },
            "MyPkg": {
                "package": "MyPkg",
                "version": "v2",
                "revision": "3",
                "pkg_family": self_family,
                "requires": ["DepPkg"],
                "hash": "cafe42",
                "commit_hash": "feedface",
                "is_devel_pkg": False,
                "env": {},
                "append_path": {},
                "prepend_path": {},
            },
        }

    def test_dep_sourcing_no_families(self):
        specs = self._make_specs()
        script = generate_initdotsh("MyPkg", specs, "slc9_x86-64", workDir="sw")
        self.assertIn('"$WORK_DIR/$BITS_ARCH_PREFIX"/DepPkg/v1-1/etc/profile.d/init.sh', script)

    def test_dep_sourcing_with_dep_family(self):
        specs = self._make_specs(dep_family="lcg")
        script = generate_initdotsh("MyPkg", specs, "slc9_x86-64", workDir="sw")
        self.assertIn('"$WORK_DIR/$BITS_ARCH_PREFIX"/lcg/DepPkg/v1-1/etc/profile.d/init.sh', script)

    def test_self_root_no_family(self):
        specs = self._make_specs()
        script = generate_initdotsh("MyPkg", specs, "slc9_x86-64", workDir="sw", post_build=True)
        self.assertIn('export MYPKG_ROOT="$WORK_DIR/$BITS_ARCH_PREFIX"/MyPkg/v2-3', script)

    def test_self_root_with_family(self):
        specs = self._make_specs(self_family="cms")
        script = generate_initdotsh("MyPkg", specs, "slc9_x86-64", workDir="sw", post_build=True)
        self.assertIn('export MYPKG_ROOT="$WORK_DIR/$BITS_ARCH_PREFIX"/cms/MyPkg/v2-3', script)

    def test_dep_family_does_not_bleed_into_self_root(self):
        """Even if dep has a family, the self ROOT export uses self's own family."""
        specs = self._make_specs(dep_family="lcg", self_family="cms")
        script = generate_initdotsh("MyPkg", specs, "slc9_x86-64", workDir="sw", post_build=True)
        self.assertIn('"$WORK_DIR/$BITS_ARCH_PREFIX"/lcg/DepPkg/', script)
        self.assertIn('"$WORK_DIR/$BITS_ARCH_PREFIX"/cms/MyPkg/', script)


# ---------------------------------------------------------------------------
# getPackageList integration — pkg_family is assigned from defaults_meta
# ---------------------------------------------------------------------------

class TestGetPackageListPkgFamily(unittest.TestCase):
    """Check that getPackageList assigns pkg_family from the defaults metadata."""

    # Minimal recipe YAML bodies for getPackageList
    RECIPES = {
        "myapp": "package: myapp\nversion: v1\n---\n",
        "defaults-release": "package: defaults-release\nversion: v1\n---\n",
    }

    def _call_getPackageList(self, defaults_meta):
        specs = {}

        def fake_prefer_check(pkg, cmd):
            return (1, "")

        def fake_req_check(pkg, cmd):
            return (0, "")

        def fake_validate(spec):
            return (True, "", None)

        def fake_resolveFilename(taps, pkg, configDir, genPkgs):
            return (pkg + ".sh", "/pkgdir")

        def fake_getRecipeReader(filename, *args, **kwargs):
            pkg = filename.replace(".sh", "")
            content = self.RECIPES.get(pkg, "package: {p}\nversion: v1\n---\n".format(p=pkg))
            return lambda: content

        with patch("bits_helpers.packages.resolveFilename",
                   side_effect=fake_resolveFilename), \
             patch("bits_helpers.packages.getRecipeReader",
                   side_effect=fake_getRecipeReader), \
             patch("bits_helpers.packages.getGeneratedPackages",
                   return_value={"/pkgdir": {}}), \
             patch("bits_helpers.packages.load_for_spec",
                   return_value=None), \
             patch("bits_helpers.packages.merge_into_spec",
                   return_value=None):
            getPackageList(
                packages=["myapp"],
                specs=specs,
                configDir="/fake",
                preferSystem=False,
                noSystem="*",
                architecture="slc9_x86-64",
                disable=[],
                defaults=["release"],
                performPreferCheck=fake_prefer_check,
                performRequirementCheck=fake_req_check,
                performValidateDefaults=fake_validate,
                overrides={"defaults-release": {}},
                taps={},
                log=lambda *a, **k: None,
                defaults_meta=defaults_meta,
            )
        return specs

    def test_pkg_family_assigned_when_matched(self):
        meta = {"package_family": {"default": "cms", "lcg": ["myapp"]}}
        specs = self._call_getPackageList(meta)
        self.assertIn("myapp", specs)
        self.assertEqual(specs["myapp"]["pkg_family"], "lcg")

    def test_pkg_family_uses_default_when_no_match(self):
        meta = {"package_family": {"default": "cms", "lcg": ["other"]}}
        specs = self._call_getPackageList(meta)
        self.assertEqual(specs["myapp"]["pkg_family"], "cms")

    def test_pkg_family_empty_when_no_config(self):
        specs = self._call_getPackageList({})
        self.assertEqual(specs["myapp"]["pkg_family"], "")

    def test_pkg_family_empty_when_defaults_meta_none(self):
        specs = self._call_getPackageList(None)
        self.assertEqual(specs["myapp"]["pkg_family"], "")


# ---------------------------------------------------------------------------
# storeHashes — pkg_family must contribute to the hash
# ---------------------------------------------------------------------------

_DEFAULTS_RECIPE = """\
package: defaults-release
version: v1
---
"""

_PKG_RECIPE = """\
package: mypkg
version: v1.0
source: https://example.com/mypkg
tag: master
requires:
  - defaults-release
---
make install
"""

def _make_spec(recipe_text):
    err, spec, recipe = parseRecipe(lambda: recipe_text)
    assert err is None, err
    spec["recipe"] = "" if spec["package"].startswith("defaults-") else recipe.strip("\n")
    spec.setdefault("tag", spec["version"])
    spec["tag"] = resolve_tag(spec)
    spec["is_devel_pkg"] = False
    return spec


class TestPkgFamilyAffectsHash(unittest.TestCase):
    """pkg_family must be part of the build hash.

    A tarball built without a family uses a path of the form
    ARCH/PKG/VER-REV, while one built with a family uses
    ARCH/FAMILY/PKG/VER-REV.  They are not interchangeable, so their
    hashes must differ to prevent bits from silently fetching a
    wrong tarball that would fail relocation.
    """

    def _compute_hashes(self, pkg_family):
        defaults = _make_spec(_DEFAULTS_RECIPE)
        defaults["commit_hash"] = "0"
        pkg = _make_spec(_PKG_RECIPE)
        pkg["commit_hash"] = "aaaa1111"
        pkg["scm_refs"] = {"refs/heads/master": "aaaa1111"}
        pkg["pkg_family"] = pkg_family
        specs = {
            defaults["package"]: defaults,
            pkg["package"]: pkg,
        }
        storeHashes("defaults-release", specs, considerRelocation=False)
        defaults["hash"] = defaults["remote_revision_hash"]
        storeHashes("mypkg", specs, considerRelocation=False)
        return pkg["remote_revision_hash"], pkg["local_revision_hash"]

    def test_no_family_vs_with_family_differ(self):
        """A package with pkg_family must hash differently from the same
        package without one, so they are never confused in the store."""
        remote_no_family, local_no_family = self._compute_hashes("")
        remote_with_family, local_with_family = self._compute_hashes("o2")
        self.assertNotEqual(remote_no_family, remote_with_family,
                            "remote hash must differ when pkg_family changes")
        self.assertNotEqual(local_no_family, local_with_family,
                            "local hash must differ when pkg_family changes")

    def test_different_families_differ(self):
        """Two different pkg_family values must produce distinct hashes."""
        remote_o2, _ = self._compute_hashes("o2")
        remote_cms, _ = self._compute_hashes("cms")
        self.assertNotEqual(remote_o2, remote_cms,
                            "different pkg_family values must produce different hashes")

    def test_same_family_stable(self):
        """Same pkg_family value must always produce the same hash."""
        remote_a, local_a = self._compute_hashes("o2")
        remote_b, local_b = self._compute_hashes("o2")
        self.assertEqual(remote_a, remote_b, "remote hash must be stable for same pkg_family")
        self.assertEqual(local_a, local_b, "local hash must be stable for same pkg_family")


if __name__ == "__main__":
    unittest.main()
