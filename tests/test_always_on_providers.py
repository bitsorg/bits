# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the always-on provider loading machinery.

Covers:
 - _read_bits_rc(): searches bits.rc search paths; returns [bits] section
 - _parse_provider_url(): splits url@tag; defaults tag to "main"
 - _make_bits_providers_spec(): correct spec shape and constant fields
 - load_always_on_providers():
     * BITS_PROVIDERS path (step 1)
     * always_load config-dir scan (step 2)
     * double-clone prevention for the reserved package name
     * failure isolation (bad clone → warning, not fatal)
"""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from collections import OrderedDict
from unittest.mock import MagicMock, patch, call

# ── import helpers ────────────────────────────────────────────────────────────

from bits_helpers.repo_provider import (
    BITS_PROVIDERS_PACKAGE,
    _parse_provider_url,
    _make_bits_providers_spec,
    load_always_on_providers,
)


# ---------------------------------------------------------------------------
# _parse_provider_url
# ---------------------------------------------------------------------------

class TestParseProviderUrl(unittest.TestCase):

    def test_url_without_tag_defaults_to_main(self):
        url, tag = _parse_provider_url("https://github.com/org/repo.git")
        self.assertEqual(url, "https://github.com/org/repo.git")
        self.assertEqual(tag, "main")

    def test_url_with_tag(self):
        url, tag = _parse_provider_url("https://github.com/org/repo.git@stable")
        self.assertEqual(url, "https://github.com/org/repo.git")
        self.assertEqual(tag, "stable")

    def test_url_with_semver_tag(self):
        url, tag = _parse_provider_url("https://github.com/org/repo.git@v1.2.3")
        self.assertEqual(url, "https://github.com/org/repo.git")
        self.assertEqual(tag, "v1.2.3")

    def test_url_with_surrounding_whitespace(self):
        url, tag = _parse_provider_url("  https://github.com/org/repo.git@dev  ")
        self.assertEqual(url, "https://github.com/org/repo.git")
        self.assertEqual(tag, "dev")

    def test_url_only_whitespace_tag_falls_back_to_main(self):
        """An @ with no tag text after it defaults to 'main'."""
        url, tag = _parse_provider_url("https://github.com/org/repo.git@")
        self.assertEqual(url, "https://github.com/org/repo.git")
        self.assertEqual(tag, "main")

    def test_ssh_url_without_tag(self):
        url, tag = _parse_provider_url("git@github.com:org/repo.git")
        # partition('@') will split at the first '@', so ssh-style URLs
        # are handled: url = "git", tag = "github.com:org/repo.git"
        # This is the defined behaviour for ssh-style URLs that contain @.
        # The test documents actual (not ideal) behaviour so regressions are caught.
        self.assertIn("github.com", tag)


# ---------------------------------------------------------------------------
# _make_bits_providers_spec
# ---------------------------------------------------------------------------

class TestMakeBitsProvidersSpec(unittest.TestCase):

    def setUp(self):
        self.spec = _make_bits_providers_spec(
            "https://github.com/org/recipes.git", "stable"
        )

    def test_package_is_bits_providers_constant(self):
        self.assertEqual(self.spec["package"], BITS_PROVIDERS_PACKAGE)

    def test_version_is_one(self):
        self.assertEqual(self.spec["version"], "1")

    def test_source_matches_url(self):
        self.assertEqual(self.spec["source"], "https://github.com/org/recipes.git")

    def test_tag_matches_argument(self):
        self.assertEqual(self.spec["tag"], "stable")

    def test_provides_repository_true(self):
        self.assertTrue(self.spec["provides_repository"])

    def test_always_load_true(self):
        self.assertTrue(self.spec["always_load"])

    def test_repository_position_append(self):
        # Default is now "append" — providers cannot self-elevate to prepend.
        self.assertEqual(self.spec["repository_position"], "append")

    def test_returns_ordered_dict(self):
        self.assertIsInstance(self.spec, OrderedDict)

    def test_default_tag_main(self):
        spec = _make_bits_providers_spec("https://example.com/repo.git", "main")
        self.assertEqual(spec["tag"], "main")


# ---------------------------------------------------------------------------
# _read_bits_rc
# ---------------------------------------------------------------------------

class TestReadBitsRc(unittest.TestCase):
    """Tests for args._read_bits_rc() and its search-path logic."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_rc(self, filename, content):
        path = os.path.join(self.tmp, filename)
        with open(path, "w") as fh:
            fh.write(textwrap.dedent(content))
        return path

    def _read_bits_rc(self):
        # Import fresh each time so _BITS_RC_SEARCH_PATHS is re-evaluated
        # with the current working directory.
        from bits_helpers.args import _read_bits_rc
        return _read_bits_rc()

    def test_returns_empty_dict_when_no_rc_file(self):
        result = self._read_bits_rc()
        # May include user's ~/.bitsrc if present; we only assert type.
        self.assertIsInstance(result, dict)

    def test_reads_bits_section(self):
        self._write_rc("bits.rc", """
            [bits]
            providers = https://github.com/org/recipes.git
            sw_dir = /opt/sw
        """)
        result = self._read_bits_rc()
        self.assertEqual(result.get("providers"), "https://github.com/org/recipes.git")
        self.assertEqual(result.get("sw_dir"), "/opt/sw")

    def test_ignores_other_sections(self):
        self._write_rc("bits.rc", """
            [other]
            key = value
        """)
        result = self._read_bits_rc()
        self.assertNotIn("key", result)

    def test_bits_rc_takes_priority_over_bitsrc(self):
        self._write_rc("bits.rc", """
            [bits]
            providers = from-bits-rc
        """)
        self._write_rc(".bitsrc", """
            [bits]
            providers = from-bitsrc
        """)
        result = self._read_bits_rc()
        self.assertEqual(result.get("providers"), "from-bits-rc")

    def test_falls_back_to_bitsrc_when_bits_rc_absent(self):
        self._write_rc(".bitsrc", """
            [bits]
            providers = from-bitsrc
        """)
        result = self._read_bits_rc()
        self.assertEqual(result.get("providers"), "from-bitsrc")

    def test_keys_are_lowercase(self):
        self._write_rc("bits.rc", """
            [bits]
            Providers = https://example.com/repo.git
        """)
        result = self._read_bits_rc()
        # configparser lower-cases keys by default
        self.assertIn("providers", result)
        self.assertNotIn("Providers", result)


# ---------------------------------------------------------------------------
# load_always_on_providers
# ---------------------------------------------------------------------------

def _make_provider_sh(directory, package, source, tag="v1",
                      always_load=True, provides_repository=True,
                      position="append"):
    """Write a minimal recipe .sh file into *directory*.

    The file follows the bits recipe format: YAML header terminated by ``---``,
    followed by an (empty) shell body.
    """
    content_lines = [
        'package: "%s"' % package,
        'version: "1"',
        'source: "%s"' % source,
        'tag: "%s"' % tag,
        "provides_repository: %s" % ("true" if provides_repository else "false"),
        "always_load: %s" % ("true" if always_load else "false"),
        'repository_position: "%s"' % position,
        "---",
        "",  # empty shell body
    ]
    content = "\n".join(content_lines)
    path = os.path.join(directory, package + ".sh")
    with open(path, "w") as fh:
        fh.write(content)
    return path


class TestLoadAlwaysOnProviders(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config_dir = os.path.join(self.tmp, "cfg")
        self.work_dir = os.path.join(self.tmp, "sw")
        self.ref_dir = os.path.join(self.tmp, "mirror")
        os.makedirs(self.config_dir)
        os.makedirs(self.work_dir)
        os.makedirs(self.ref_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── BITS_PROVIDERS path ────────────────────────────────────────────────

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_bits_providers_cloned_first(self, mock_clone, mock_add):
        """When bits_providers is set, the synthesised package is cloned."""
        checkout_dir = os.path.join(self.work_dir, "bits-providers")
        os.makedirs(checkout_dir)
        mock_clone.return_value = (checkout_dir, "abc123")

        result = load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = "https://github.com/org/recipes.git@stable",
        )

        mock_clone.assert_called_once()
        spec_arg = mock_clone.call_args[0][0]
        self.assertEqual(spec_arg["package"], BITS_PROVIDERS_PACKAGE)
        self.assertEqual(spec_arg["source"], "https://github.com/org/recipes.git")
        self.assertEqual(spec_arg["tag"], "stable")

        self.assertIn(checkout_dir, result)
        self.assertEqual(result[checkout_dir][0], BITS_PROVIDERS_PACKAGE)

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_bits_providers_uses_main_tag_by_default(self, mock_clone, mock_add):
        checkout_dir = os.path.join(self.work_dir, "bits-providers")
        os.makedirs(checkout_dir)
        mock_clone.return_value = (checkout_dir, "deadbeef")

        load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = "https://github.com/org/recipes.git",
        )

        spec_arg = mock_clone.call_args[0][0]
        self.assertEqual(spec_arg["tag"], "main")

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider",
           side_effect=SystemExit(1))
    def test_bits_providers_clone_failure_is_non_fatal(self, mock_clone, mock_add):
        """A failing BITS_PROVIDERS clone logs a warning but does not abort."""
        result = load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = "https://github.com/org/bad.git",
        )
        # Should return empty dict (or only config-dir results), not raise
        self.assertNotIn(BITS_PROVIDERS_PACKAGE,
                         [v[0] for v in result.values()])

    # ── config-dir always_load scan ────────────────────────────────────────

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_always_load_recipe_in_config_dir_is_cloned(self, mock_clone, mock_add):
        checkout_dir = os.path.join(self.work_dir, "my-recipes")
        os.makedirs(checkout_dir)
        mock_clone.return_value = (checkout_dir, "feed1234")

        _make_provider_sh(self.config_dir, "my-recipes",
                          "https://github.com/org/my-recipes.git")

        result = load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = None,
        )

        mock_clone.assert_called_once()
        self.assertIn(checkout_dir, result)
        self.assertEqual(result[checkout_dir][0], "my-recipes")

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_recipe_without_always_load_not_cloned(self, mock_clone, mock_add):
        """Recipes that only have provides_repository but not always_load are skipped."""
        _make_provider_sh(self.config_dir, "optional-recipes",
                          "https://github.com/org/optional.git",
                          always_load=False)

        load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = None,
        )

        mock_clone.assert_not_called()

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_recipe_without_provides_repository_not_cloned(self, mock_clone, mock_add):
        """always_load alone (no provides_repository) does not trigger a clone."""
        _make_provider_sh(self.config_dir, "data-pkg",
                          "https://github.com/org/data.git",
                          provides_repository=False, always_load=True)

        load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = None,
        )

        mock_clone.assert_not_called()

    # ── double-clone prevention ────────────────────────────────────────────

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_config_dir_bits_providers_skipped_when_bits_providers_env_set(
            self, mock_clone, mock_add):
        """A ``bits-providers.sh`` in the config dir is skipped when
        BITS_PROVIDERS already handled the reserved package name."""
        bp_checkout = os.path.join(self.work_dir, "bits-providers-env")
        os.makedirs(bp_checkout)

        # clone called twice — once for env, once for the config-dir .sh
        # but the config-dir one should be skipped → only one real call.
        mock_clone.return_value = (bp_checkout, "env_commit")

        _make_provider_sh(self.config_dir, BITS_PROVIDERS_PACKAGE,
                          "https://github.com/org/different-recipes.git")

        result = load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = "https://github.com/org/env-recipes.git",
        )

        # clone called exactly once (for the env-based provider)
        self.assertEqual(mock_clone.call_count, 1)
        spec_arg = mock_clone.call_args[0][0]
        self.assertEqual(spec_arg["source"], "https://github.com/org/env-recipes.git")

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_config_dir_bits_providers_cloned_when_no_env(
            self, mock_clone, mock_add):
        """A ``bits-providers.sh`` recipe IS cloned when bits_providers is None."""
        bp_checkout = os.path.join(self.work_dir, "bits-providers-cfg")
        os.makedirs(bp_checkout)
        mock_clone.return_value = (bp_checkout, "cfg_commit")

        _make_provider_sh(self.config_dir, BITS_PROVIDERS_PACKAGE,
                          "https://github.com/org/cfg-recipes.git",
                          always_load=True)

        result = load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = None,
        )

        mock_clone.assert_called_once()
        self.assertIn(bp_checkout, result)

    # ── multiple providers ─────────────────────────────────────────────────

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_multiple_always_load_recipes_all_cloned(self, mock_clone, mock_add):
        """All always_load recipes in the config dir are cloned."""
        c1 = os.path.join(self.work_dir, "r1")
        c2 = os.path.join(self.work_dir, "r2")
        os.makedirs(c1); os.makedirs(c2)
        mock_clone.side_effect = [(c1, "aaa"), (c2, "bbb")]

        _make_provider_sh(self.config_dir, "recipes-a",
                          "https://github.com/org/a.git")
        _make_provider_sh(self.config_dir, "recipes-b",
                          "https://github.com/org/b.git")

        result = load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = None,
        )

        self.assertEqual(mock_clone.call_count, 2)
        self.assertIn(c1, result)
        self.assertIn(c2, result)

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_config_dir_clone_failure_is_non_fatal(self, mock_clone, mock_add):
        """A failing always_load clone logs a warning but other providers proceed."""
        c2 = os.path.join(self.work_dir, "r2")
        os.makedirs(c2)
        mock_clone.side_effect = [SystemExit(1), (c2, "bbb")]

        _make_provider_sh(self.config_dir, "bad-recipes",
                          "https://github.com/org/bad.git")
        _make_provider_sh(self.config_dir, "good-recipes",
                          "https://github.com/org/good.git")

        result = load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = None,
        )

        self.assertIn(c2, result)
        self.assertEqual(result[c2][0], "good-recipes")

    # ── empty config dir ───────────────────────────────────────────────────

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_empty_config_dir_returns_empty_dict(self, mock_clone, mock_add):
        result = load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = None,
        )
        self.assertEqual(result, {})
        mock_clone.assert_not_called()

    # ── repository_position forwarded ─────────────────────────────────────

    @patch("bits_helpers.repo_provider._add_to_bits_path")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_repository_position_forwarded_to_bits_path(self, mock_clone, mock_add):
        """The ``repository_position`` from the recipe is forwarded to _add_to_bits_path
        as the *recipe_position* keyword argument alongside the provider name and policy."""
        checkout_dir = os.path.join(self.work_dir, "prepend-recipes")
        os.makedirs(checkout_dir)
        mock_clone.return_value = (checkout_dir, "deadbeef")

        _make_provider_sh(self.config_dir, "prepend-recipes",
                          "https://github.com/org/prepend.git",
                          position="prepend")

        load_always_on_providers(
            config_dir        = self.config_dir,
            work_dir          = self.work_dir,
            reference_sources = self.ref_dir,
            fetch_repos       = False,
            bits_providers    = None,
        )

        mock_add.assert_called_once_with(
            checkout_dir,
            recipe_position="prepend",
            provider_name="prepend-recipes",
            policy={},
        )


class TestLocalProviderShadowing(unittest.TestCase):
    """A declared provider that is also checked out locally in config_dir is
    used from that checkout instead of being cloned."""

    @staticmethod
    def _git(d, *args):
        subprocess.run(["git", "-C", d, *args], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.config_dir = os.path.join(self.tmp, "cfg")
        self.work_dir = os.path.join(self.tmp, "sw")
        self.ref_dir = os.path.join(self.tmp, "mirror")
        for d in (self.config_dir, self.work_dir, self.ref_dir):
            os.makedirs(d)
        # A declared provider recipe, plus a local checkout named after it.
        _make_provider_sh(self.config_dir, "myprov.bits",
                          "https://invalid.example/nope.git")
        self.local = os.path.join(self.config_dir, "myprov.bits")
        os.makedirs(self.local)
        with open(os.path.join(self.local, "foo.sh"), "w") as fh:
            fh.write('package: "foo"\nversion: "1"\n---\n')
        self._git(self.local, "init", "-q")
        self._git(self.local, "config", "user.email", "t@t")
        self._git(self.local, "config", "user.name", "t")
        self._git(self.local, "add", "-A")
        self._git(self.local, "commit", "-qm", "init")
        self._saved_path = os.environ.pop("BITS_PATH", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("BITS_PATH", None)
        if self._saved_path is not None:
            os.environ["BITS_PATH"] = self._saved_path

    def _load(self, **kw):
        return load_always_on_providers(
            config_dir=self.config_dir, work_dir=self.work_dir,
            reference_sources=self.ref_dir, fetch_repos=False, **kw)

    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_local_checkout_used_instead_of_clone(self, mock_clone):
        result = self._load()
        mock_clone.assert_not_called()
        self.assertIn(self.local, result)
        pkg, commit = result[self.local]
        self.assertEqual(pkg, "myprov.bits")
        self.assertRegex(commit, r"^[0-9a-f]{7,40}$")
        self.assertIn(self.local, os.environ.get("BITS_PATH", "").split(","))

    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_force_tracked_falls_back_to_clone(self, mock_clone):
        clone_dir = os.path.join(self.work_dir, "clone")
        os.makedirs(clone_dir, exist_ok=True)
        mock_clone.return_value = (clone_dir, "deadbeef")
        result = self._load(force_tracked=True)
        mock_clone.assert_called_once()
        self.assertNotIn(self.local, result)

    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    def test_dirty_checkout_marked(self, mock_clone):
        with open(os.path.join(self.local, "untracked.sh"), "w") as fh:
            fh.write("x")
        _, commit = self._load()[self.local]
        mock_clone.assert_not_called()
        self.assertTrue(commit.endswith("-dirty"))

    def test_local_provider_dir_helper(self):
        from bits_helpers.repo_provider import _local_provider_dir
        self.assertEqual(_local_provider_dir(self.config_dir, "myprov.bits"),
                         self.local)
        self.assertIsNone(_local_provider_dir(self.config_dir, "nonexistent.bits"))

    def test_non_git_dir_hashes_to_local(self):
        from bits_helpers.repo_provider import _local_provider_hash
        plain = os.path.join(self.tmp, "plaindir")
        os.makedirs(plain)
        self.assertEqual(_local_provider_hash(plain), "local")


if __name__ == "__main__":
    unittest.main()
