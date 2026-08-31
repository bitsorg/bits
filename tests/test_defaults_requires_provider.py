# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the defaults-file ``requires`` → provider-scan seeding feature.

A defaults file (defaults-*.sh) can declare a top-level ``requires`` (and/or
``build_requires``) field to pull in repository-provider packages.  Before
this feature, the provider-scan only walked the user-specified packages list;
the defaults spec's own requires were processed later inside ``getPackageList``
and therefore too late to trigger provider cloning.

The fix extracts ``defaultsMeta.get("requires")`` and
``defaultsMeta.get("build_requires")`` after ``parseDefaults()`` and adds
them to the seed list passed to ``fetch_repo_providers_iteratively``.

Because every non-defaults package automatically has ``defaults-release``
appended to its ``build_requires`` inside ``getPackageList``, any package
listed in the defaults' own ``requires`` would create an unresolvable cycle:

    defaults-release → provider-pkg → defaults-release

To prevent this, ``getPackageList`` strips ``requires`` and ``build_requires``
from the ``defaults-release`` spec before the dependency-following step.  The
provider repos have already been loaded by Phase 2 at this point.

These tests verify:
 1. ``parseDefaults`` propagates a top-level ``requires`` field untouched.
 2. The seed list is built correctly from both ``requires`` and
    ``build_requires``, and is empty when neither field is present.
 3. A provider declared in a defaults ``requires`` is discovered and cloned by
    ``fetch_repo_providers_iteratively`` even when the user-specified packages
    list does not mention it.
 4. Normal (non-provider) packages in defaults ``requires`` don't cause errors
    in the provider-scan phase.
 5. Backward compatibility: defaults files without ``requires`` produce an
    empty seed, leaving the existing behaviour unchanged.
"""

import os
import shutil
import sys
import tempfile
import textwrap
import unittest
from collections import OrderedDict
from unittest.mock import MagicMock, patch, call

# ── path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bits_helpers.utilities import parseDefaults
from bits_helpers.recipe import parseRecipe, getRecipeReader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_sh(directory: str, name: str, yaml_header: str, body: str = "") -> str:
    """Write a recipe .sh into *directory* and return its path."""
    path = os.path.join(directory, name + ".sh")
    with open(path, "w") as fh:
        fh.write(yaml_header.rstrip() + "\n---\n" + body)
    return path


def _noop_log(*args, **kwargs):
    pass


# ---------------------------------------------------------------------------
# 1.  parseDefaults propagates a top-level requires field
# ---------------------------------------------------------------------------

class TestParseDefaultsPropagatesRequires(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _parse(self, yaml_header):
        path = _write_sh(self.tmp, "defaults-release", yaml_header)
        def getter():
            err, meta, body = parseRecipe(getRecipeReader(path))
            return (meta or {}, body or "")
        err, overrides, taps, meta = parseDefaults(
            disable       = [],
            defaultsGetter= getter,
            log           = _noop_log,
        )
        self.assertIsNone(err)
        return meta

    def test_requires_field_is_present_in_meta(self):
        meta = self._parse(textwrap.dedent("""\
            package: defaults-release
            version: "1"
            requires:
              - my-provider
        """))
        self.assertIn("requires", meta)
        self.assertIn("my-provider", meta["requires"])

    def test_build_requires_field_is_present_in_meta(self):
        meta = self._parse(textwrap.dedent("""\
            package: defaults-release
            version: "1"
            build_requires:
              - build-tool-provider
        """))
        self.assertIn("build_requires", meta)
        self.assertIn("build-tool-provider", meta["build_requires"])

    def test_both_requires_and_build_requires_preserved(self):
        meta = self._parse(textwrap.dedent("""\
            package: defaults-release
            version: "1"
            requires:
              - runtime-provider
            build_requires:
              - build-provider
        """))
        self.assertIn("runtime-provider", meta.get("requires", []))
        self.assertIn("build-provider", meta.get("build_requires", []))

    def test_no_requires_means_absent_key(self):
        meta = self._parse(textwrap.dedent("""\
            package: defaults-release
            version: "1"
        """))
        self.assertEqual(meta.get("requires", []), [])
        self.assertEqual(meta.get("build_requires", []), [])

    def test_multiple_requires_all_present(self):
        meta = self._parse(textwrap.dedent("""\
            package: defaults-release
            version: "1"
            requires:
              - provider-a
              - provider-b
              - provider-c
        """))
        for name in ("provider-a", "provider-b", "provider-c"):
            self.assertIn(name, meta["requires"])


# ---------------------------------------------------------------------------
# 2.  defaults_provider_seed construction
# ---------------------------------------------------------------------------

class TestDefaultsProviderSeed(unittest.TestCase):
    """Unit-level test of the seed-list logic (without invoking doBuild)."""

    def _seed(self, meta: dict) -> list:
        """Replicate the seed-extraction logic from doBuild."""
        return (
            list(meta.get("requires", []))
            + list(meta.get("build_requires", []))
        )

    def test_empty_meta_gives_empty_seed(self):
        self.assertEqual(self._seed({}), [])

    def test_only_requires_gives_seed(self):
        meta = {"requires": ["prov-a", "prov-b"]}
        self.assertEqual(self._seed(meta), ["prov-a", "prov-b"])

    def test_only_build_requires_gives_seed(self):
        meta = {"build_requires": ["bprov"]}
        self.assertEqual(self._seed(meta), ["bprov"])

    def test_both_fields_concatenated(self):
        meta = {"requires": ["r1", "r2"], "build_requires": ["b1"]}
        seed = self._seed(meta)
        self.assertEqual(seed, ["r1", "r2", "b1"])

    def test_seed_does_not_modify_original_meta(self):
        meta = {"requires": ["prov-a"]}
        seed = self._seed(meta)
        seed.append("injected")
        self.assertEqual(meta["requires"], ["prov-a"])

    def test_backward_compat_no_requires_no_seed(self):
        """Existing defaults files without requires produce an empty seed."""
        meta = {
            "package": "defaults-release",
            "version": "1",
            "disable": ["alien"],
            "overrides": {"zlib": {"version": "1.3"}},
        }
        self.assertEqual(self._seed(meta), [])


# ---------------------------------------------------------------------------
# 3.  Provider declared in defaults requires is discovered by the scanner
# ---------------------------------------------------------------------------

class TestProviderDiscoveryFromDefaultsRequires(unittest.TestCase):
    """Integration test: a provider listed in defaults requires is cloned.

    We test ``fetch_repo_providers_iteratively`` directly with a seed that
    includes the provider name (simulating what doBuild now passes after
    reading defaults_provider_seed from defaultsMeta).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config_dir = os.path.join(self.tmp, "cfg")
        self.work_dir = os.path.join(self.tmp, "sw")
        os.makedirs(self.config_dir)
        os.makedirs(self.work_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_recipe(self, name: str, yaml_header: str) -> str:
        return _write_sh(self.config_dir, name, yaml_header)

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_provider_in_defaults_requires_is_cloned_when_seeded(
            self, mock_clone, mock_add):
        """When doBuild seeds the scan with defaults_provider_seed,
        a provider declared in defaults requires is cloned."""
        checkout = os.path.join(self.work_dir, "myorg-recipes")
        os.makedirs(checkout)
        mock_clone.return_value = (checkout, "abc1234")

        # Recipe in config dir: a provider that the defaults file requires
        self._write_recipe("myorg-recipes", textwrap.dedent("""\
            package: myorg-recipes
            version: "1"
            source: https://github.com/myorg/recipes.git
            tag: main
            provides_repository: true
        """))

        from bits_helpers.repo_provider import fetch_repo_providers_iteratively

        # The user only builds "zlib"; defaults requires ["myorg-recipes"]
        # doBuild adds "myorg-recipes" to the seed → packages + seed below
        user_packages = ["zlib"]
        defaults_seed = ["myorg-recipes"]

        result = fetch_repo_providers_iteratively(
            packages          = user_packages + defaults_seed,
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = os.path.join(self.tmp, "mirror"),
            fetch_repos       = False,
            taps              = {},
        )

        mock_clone.assert_called_once()
        spec_arg = mock_clone.call_args[0][0]
        self.assertEqual(spec_arg["package"], "myorg-recipes")
        self.assertIn(checkout, result)

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_provider_not_cloned_without_seed(self, mock_clone, mock_add):
        """Without the seed, a provider only in defaults requires is NOT found."""
        self._write_recipe("myorg-recipes", textwrap.dedent("""\
            package: myorg-recipes
            version: "1"
            source: https://github.com/myorg/recipes.git
            tag: main
            provides_repository: true
        """))

        from bits_helpers.repo_provider import fetch_repo_providers_iteratively

        # User only builds "zlib"; no seed → myorg-recipes never visited
        result = fetch_repo_providers_iteratively(
            packages          = ["zlib"],
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = os.path.join(self.tmp, "mirror"),
            fetch_repos       = False,
            taps              = {},
        )

        mock_clone.assert_not_called()
        self.assertEqual(result, {})

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_non_provider_in_defaults_requires_no_error(self, mock_clone, mock_add):
        """A normal (non-provider) package in defaults requires is skipped
        silently by the provider scanner — no exception is raised."""
        # "cmake" is a regular package — no provides_repository
        self._write_recipe("cmake", textwrap.dedent("""\
            package: cmake
            version: "3.28"
            source: https://cmake.org/cmake.git
            tag: v3.28
        """))

        from bits_helpers.repo_provider import fetch_repo_providers_iteratively

        # Should complete without error; cmake is visited but not cloned
        result = fetch_repo_providers_iteratively(
            packages          = ["zlib", "cmake"],  # seeded with cmake from defaults
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = os.path.join(self.tmp, "mirror"),
            fetch_repos       = False,
            taps              = {},
        )

        mock_clone.assert_not_called()
        self.assertEqual(result, {})

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_backward_compat_empty_seed_unchanged_behaviour(
            self, mock_clone, mock_add):
        """When defaults has no requires, the seed is empty and behaviour is
        identical to the pre-feature code (only user packages are walked)."""
        from bits_helpers.repo_provider import fetch_repo_providers_iteratively

        defaults_seed = []  # no requires in defaults

        result = fetch_repo_providers_iteratively(
            packages          = ["zlib"] + defaults_seed,
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = os.path.join(self.tmp, "mirror"),
            fetch_repos       = False,
            taps              = {},
        )

        mock_clone.assert_not_called()
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# 4.  End-to-end: parseDefaults → seed → provider discovery
# ---------------------------------------------------------------------------

class TestDefaultsRequiresEndToEnd(unittest.TestCase):
    """Combine parseDefaults and fetch_repo_providers_iteratively to verify
    the full pipeline that doBuild now exercises."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config_dir = os.path.join(self.tmp, "cfg")
        self.work_dir = os.path.join(self.tmp, "sw")
        os.makedirs(self.config_dir)
        os.makedirs(self.work_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_full_pipeline_provider_in_defaults_requires(
            self, mock_clone, mock_add):
        """A provider listed in defaults requires is cloned when the seed from
        defaultsMeta is forwarded to fetch_repo_providers_iteratively."""
        checkout = os.path.join(self.work_dir, "org-recipes")
        os.makedirs(checkout)
        mock_clone.return_value = (checkout, "deadbeef")

        # Write defaults file with requires
        defaults_yaml = textwrap.dedent("""\
            package: defaults-release
            version: "1"
            requires:
              - org-recipes
        """)
        defaults_path = _write_sh(self.config_dir, "defaults-release", defaults_yaml)

        # Write provider recipe
        _write_sh(self.config_dir, "org-recipes", textwrap.dedent("""\
            package: org-recipes
            version: "1"
            source: https://github.com/org/recipes.git
            tag: stable
            provides_repository: true
        """))

        # Simulate doBuild's sequence
        def getter():
            err, meta, body = parseRecipe(getRecipeReader(defaults_path))
            return (meta or {}, body or "")
        err, overrides, taps, defaultsMeta = parseDefaults(
            disable        = [],
            defaultsGetter = getter,
            log            = _noop_log,
        )
        self.assertIsNone(err)

        defaults_provider_seed = (
            list(defaultsMeta.get("requires", []))
            + list(defaultsMeta.get("build_requires", []))
        )

        from bits_helpers.repo_provider import fetch_repo_providers_iteratively

        result = fetch_repo_providers_iteratively(
            packages          = ["zlib"] + defaults_provider_seed,
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = os.path.join(self.tmp, "mirror"),
            fetch_repos       = False,
            taps              = taps,
        )

        # org-recipes should have been cloned
        mock_clone.assert_called_once()
        self.assertIn(checkout, result)
        self.assertEqual(result[checkout][0], "org-recipes")

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_full_pipeline_no_requires_no_extra_clone(self, mock_clone, mock_add):
        """Existing defaults files without requires: backward-compat check."""
        defaults_yaml = textwrap.dedent("""\
            package: defaults-release
            version: "1"
        """)
        defaults_path = _write_sh(self.config_dir, "defaults-release", defaults_yaml)

        def getter():
            err, meta, body = parseRecipe(getRecipeReader(defaults_path))
            return (meta or {}, body or "")
        err, overrides, taps, defaultsMeta = parseDefaults(
            disable        = [],
            defaultsGetter = getter,
            log            = _noop_log,
        )
        self.assertIsNone(err)

        defaults_provider_seed = (
            list(defaultsMeta.get("requires", []))
            + list(defaultsMeta.get("build_requires", []))
        )
        self.assertEqual(defaults_provider_seed, [])  # seed is empty

        from bits_helpers.repo_provider import fetch_repo_providers_iteratively

        result = fetch_repo_providers_iteratively(
            packages          = ["zlib"] + defaults_provider_seed,
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = os.path.join(self.tmp, "mirror"),
            fetch_repos       = False,
            taps              = taps,
        )

        mock_clone.assert_not_called()
        self.assertEqual(result, {})


# ---------------------------------------------------------------------------
# 5.  getPackageList does NOT propagate defaults requires into the build graph
#     (cycle prevention)
# ---------------------------------------------------------------------------

class TestDefaultsRequiresNoCycle(unittest.TestCase):
    """Verify that a top-level ``requires`` in a defaults file does NOT create
    a dependency cycle inside ``getPackageList``.

    The cycle would be:
        defaults-release → provider-pkg → defaults-release

    because every non-defaults package gets ``defaults-release`` appended to
    its ``build_requires`` automatically (line 1037 in utilities.py).

    The fix strips ``requires`` / ``build_requires`` from the defaults-release
    spec inside ``getPackageList`` before the dependency-following step.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config_dir = os.path.join(self.tmp, "cfg")
        os.makedirs(self.config_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_recipe(self, name: str, yaml_header: str) -> str:
        return _write_sh(self.config_dir, name, yaml_header)

    def _call_getPackageList(self, packages, overrides=None, architecture="slc7_x86-64"):
        """Thin wrapper around getPackageList using the test config dir."""
        from bits_helpers.packages import getPackageList
        from bits_helpers.cmd import getstatusoutput

        specs = {}
        result = getPackageList(
            packages              = packages,
            specs                 = specs,
            configDir             = self.config_dir,
            preferSystem          = False,
            noSystem              = None,
            architecture          = architecture,
            disable               = [],
            defaults              = ["release"],
            performPreferCheck    = lambda pkg, cmd: (1, ""),
            performRequirementCheck = lambda pkg, cmd: (1, ""),
            performValidateDefaults = lambda spec: (True, "", ["release"]),
            overrides             = overrides or {"defaults-release": {}},
            taps                  = {},
            log                   = lambda *_: None,
        )
        return specs, result

    def test_defaults_requires_does_not_cause_cycle(self):
        """defaults-release.requires is stripped inside getPackageList so that
        the provider package does not auto-depend on defaults-release."""
        # defaults file declares a provider in its requires
        self._write_recipe("defaults-release", textwrap.dedent("""\
            package: defaults-release
            version: "1"
            requires:
              - my-provider
        """))
        # my-provider is a provides_repository recipe (already cloned by Phase 2)
        self._write_recipe("my-provider", textwrap.dedent("""\
            package: my-provider
            version: "1"
            source: https://github.com/org/recipes.git
            tag: main
            provides_repository: true
        """))
        # A regular package that the user wants to build
        self._write_recipe("zlib", textwrap.dedent("""\
            package: zlib
            version: "1.3"
        """))

        # This should NOT raise a SystemExit (cycle detected) or any exception
        try:
            specs, _ = self._call_getPackageList(["zlib"])
        except SystemExit as e:
            self.fail(
                "getPackageList raised SystemExit (likely a dependency cycle): %s" % e
            )

        # defaults-release should be in specs but must have empty requires
        self.assertIn("defaults-release", specs)
        dr = specs["defaults-release"]
        self.assertEqual(dr.get("requires", []), [],
                         "defaults-release.requires must be empty inside getPackageList")

        # my-provider must NOT appear in specs — it's only loaded as a provider,
        # not built as a regular package
        self.assertNotIn("my-provider", specs)

    def test_arch_gated_override(self):
        """An override key may carry a ':matcher' suffix; it applies only when
        the matcher is active for the architecture (e.g. 'root:osx' => macOS)."""
        self._write_recipe("defaults-release", "package: defaults-release\nversion: '1'\n")
        self._write_recipe("root", "package: root\nversion: v6.38.00\ntag: v6-38-00\n")
        # parseDefaults lowercases override keys, so pass the lowercased form.
        ovr = {"defaults-release": {},
               "root:osx": {"version": "v6.40.00", "tag": "v6-40-00"}}

        specs_osx, _ = self._call_getPackageList(
            ["root"], overrides=ovr, architecture="osx_arm64")
        self.assertEqual(specs_osx["root"]["version"], "v6.40.00",
                         "osx: ':osx' override should apply")
        self.assertEqual(specs_osx["root"]["tag"], "v6-40-00")

        specs_lin, _ = self._call_getPackageList(
            ["root"], overrides=ovr, architecture="slc7_x86-64")
        self.assertEqual(specs_lin["root"]["version"], "v6.38.00",
                         "linux: ':osx' override must be skipped, recipe default kept")

    def test_defaults_build_requires_does_not_cause_cycle(self):
        """Same as above but using build_requires in the defaults file."""
        self._write_recipe("defaults-release", textwrap.dedent("""\
            package: defaults-release
            version: "1"
            build_requires:
              - my-build-provider
        """))
        self._write_recipe("my-build-provider", textwrap.dedent("""\
            package: my-build-provider
            version: "1"
            source: https://github.com/org/build-recipes.git
            tag: v1
            provides_repository: true
        """))
        self._write_recipe("zlib", textwrap.dedent("""\
            package: zlib
            version: "1.3"
        """))

        try:
            specs, _ = self._call_getPackageList(["zlib"])
        except SystemExit as e:
            self.fail(
                "getPackageList raised SystemExit (likely a dependency cycle): %s" % e
            )

        self.assertIn("defaults-release", specs)
        dr = specs["defaults-release"]
        self.assertEqual(dr.get("build_requires", []), [],
                         "defaults-release.build_requires must be empty inside getPackageList")
        self.assertNotIn("my-build-provider", specs)

    def test_defaults_without_requires_still_works(self):
        """Backward compat: defaults without requires continues to work."""
        self._write_recipe("defaults-release", textwrap.dedent("""\
            package: defaults-release
            version: "1"
        """))
        self._write_recipe("zlib", textwrap.dedent("""\
            package: zlib
            version: "1.3"
        """))

        try:
            specs, _ = self._call_getPackageList(["zlib"])
        except SystemExit as e:
            self.fail("getPackageList raised SystemExit unexpectedly: %s" % e)

        self.assertIn("defaults-release", specs)
        self.assertIn("zlib", specs)
        # zlib should still auto-depend on defaults-release
        self.assertIn("defaults-release", specs["zlib"].get("requires", []))


class TestBootstrapStashesProviderRequires(unittest.TestCase):
    """The bootstrap org-pointer recipe's own ``requires`` (e.g. alice.bits.sh
    ``requires: [alidist.bits]``) are stashed on args so doBuild can seed
    provider discovery with them. Without this, a base provider repo that
    supplies needed recipes but is not a build-graph dependency of the target
    (alidist.bits → gsl, needed by ROOT in alice.bits) is never loaded."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.providers = os.path.join(self.tmp, "bits-providers")
        self.recipe_repo = os.path.join(self.tmp, "alice.bits")
        os.makedirs(self.providers)
        os.makedirs(self.recipe_repo)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_org_pointer_requires_are_stashed(self, mock_clone, mock_add):
        from argparse import Namespace
        from bits_helpers.repo_provider import bootstrap_default_config

        # alice.bits.sh in the bits-providers checkout: the org-pointer provider
        # recipe, which requires the alidist.bits provider repo.
        _write_sh(self.providers, "alice.bits", textwrap.dedent("""\
            package: alice.bits
            version: "1"
            tag: main
            provides_repository: true
            source: https://github.com/bitsorg/alice.bits
            requires:
              - alidist.bits
        """))
        # 1st clone = bits-providers checkout; 2nd = the alice.bits recipe repo.
        mock_clone.side_effect = [(self.providers, "aaa"), (self.recipe_repo, "bbb")]

        args = Namespace(bits_providers="https://github.com/bitsorg/bits-providers",
                         organisation="alice", referenceSources="", fetchRepos=False)
        checkout = bootstrap_default_config(args, self.tmp)

        self.assertEqual(checkout, self.recipe_repo)
        self.assertEqual(getattr(args, "_bootstrap_provider_requires", None),
                         ["alidist.bits"])


class TestProviderVersionConflictWarning(unittest.TestCase):
    """A package requesting a *specific* version of a provider repo that is
    already loaded at a different one must warn (the request is silently
    ignored — one version per provider per build)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config_dir = os.path.join(self.tmp, "cfg")
        self.work_dir = os.path.join(self.tmp, "sw")
        os.makedirs(self.config_dir)
        os.makedirs(self.work_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, consumer_requires):
        checkout = os.path.join(self.work_dir, "myprov")
        os.makedirs(checkout, exist_ok=True)
        _write_sh(self.config_dir, "myprov", textwrap.dedent("""\
            package: myprov
            version: "1"
            source: https://github.com/org/myprov.git
            tag: master
            provides_repository: true
        """))
        _write_sh(self.config_dir, "consumer",
                  "package: consumer\nversion: '1'\nrequires:\n  - %s\n" % consumer_requires)
        from bits_helpers.repo_provider import fetch_repo_providers_iteratively
        with patch("bits_helpers.repo_provider.clone_or_update_provider",
                   return_value=(checkout, "abc1234")), \
             patch("bits_helpers.repo_provider._add_to_bits_path"), \
             patch("bits_helpers.repo_provider.warning") as mock_warn:
            fetch_repo_providers_iteratively(
                packages=["myprov", "consumer"], config_dir=self.config_dir,
                work_dir=self.work_dir,
                reference_sources=os.path.join(self.tmp, "mirror"),
                fetch_repos=False, taps={})
        return " ".join(str(c) for c in mock_warn.call_args_list)

    def test_warns_on_conflicting_version(self):
        warned = self._run("myprov = LCG_109")
        self.assertIn("myprov", warned)
        self.assertIn("LCG_109", warned)
        self.assertIn("already loaded", warned)

    def test_no_warning_when_version_matches(self):
        # Pin equals the tag the provider was cloned at → no conflict.
        self.assertNotIn("already loaded", self._run("myprov = master"))

    def test_no_warning_for_plain_reference(self):
        # No version pin at all → no conflict.
        self.assertNotIn("already loaded", self._run("myprov"))


if __name__ == "__main__":
    unittest.main()
