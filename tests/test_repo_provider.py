# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tests for bits_helpers/repo_provider.py and the related changes to
bits_helpers/utilities.py (getConfigPaths, getPackageList provider_dirs).

All git/network operations are mocked so the tests run offline without any
real repository.
"""

import os
import shutil
import tempfile
import unittest
from collections import OrderedDict
from textwrap import dedent
from unittest import mock
from unittest.mock import MagicMock, call, patch

import bits_helpers.repo_provider as rp
from bits_helpers.repo_provider import (
    MAX_PROVIDER_ITERATIONS,
    REPOS_CACHE_SUBDIR,
    _add_to_bits_path,
    _provider_cache_root,
    _try_read_spec,
    bootstrap_default_config,
    clone_or_update_provider,
    cwd_is_recipe_dir,
    fetch_repo_providers_iteratively,
)
from bits_helpers.utilities import getPackageList
from bits_helpers.paths import getConfigPaths


# ── Recipe text helpers ─────────────────────────────────────────────────────

def _recipe(package, version="v1", extra_yaml="", script=": # no-op"):
    """Return a minimal recipe string for *package*."""
    return dedent("""\
        package: {package}
        version: {version}
        source: https://github.com/test/{package}.git
        tag: {version}
        {extra_yaml}
        ---
        {script}
    """).format(package=package, version=version,
                extra_yaml=extra_yaml.strip(), script=script)


def _provider_recipe(package, version="v1", position="append"):
    return _recipe(
        package, version,
        extra_yaml="provides_repository: true\n"
                   "repository_position: %s" % position,
    )


# ── Fixtures shared across test cases ──────────────────────────────────────

# A simple mock spec (as returned by _try_read_spec)
def _spec(package, provides=False, position="append",
          requires=None, build_requires=None):
    s = OrderedDict({
        "package": package,
        "version": "v1",
        "source": "https://github.com/test/%s.git" % package,
        "tag": "v1",
    })
    if provides:
        s["provides_repository"] = True
        s["repository_position"] = position
    if requires:
        s["requires"] = list(requires)
    if build_requires:
        s["build_requires"] = list(build_requires)
    return s


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  1.  getConfigPaths – absolute-path support                             ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestGetConfigPaths(unittest.TestCase):
    """getConfigPaths must pass absolute BITS_PATH entries through unchanged."""

    def setUp(self):
        self._orig = os.environ.get("BITS_PATH")

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("BITS_PATH", None)
        else:
            os.environ["BITS_PATH"] = self._orig

    @patch("bits_helpers.paths.exists", return_value=True)
    def test_relative_name_gets_bits_suffix(self, _exists):
        os.environ["BITS_PATH"] = "alice,common"
        paths = getConfigPaths("/base")
        self.assertIn("/base/alice.bits", paths)
        self.assertIn("/base/common.bits", paths)

    @patch("bits_helpers.paths.exists", return_value=True)
    def test_absolute_path_used_directly(self, _exists):
        """An absolute entry in BITS_PATH must not get .bits appended."""
        os.environ["BITS_PATH"] = "/abs/path/my-provider"
        paths = getConfigPaths("/base")
        self.assertIn("/abs/path/my-provider", paths)
        self.assertNotIn("/base//abs/path/my-provider.bits", paths)

    @patch("bits_helpers.paths.exists", return_value=True)
    def test_mixed_relative_and_absolute(self, _exists):
        os.environ["BITS_PATH"] = "alice,/abs/provider,common"
        paths = getConfigPaths("/base")
        self.assertIn("/base/alice.bits", paths)
        self.assertIn("/abs/provider", paths)
        self.assertIn("/base/common.bits", paths)

    def test_empty_bits_path_returns_only_configdir(self):
        os.environ.pop("BITS_PATH", None)
        paths = getConfigPaths("/base")
        self.assertEqual(paths, ["/base"])


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  2.  _add_to_bits_path                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestAddToBitsPath(unittest.TestCase):
    def setUp(self):
        self._orig = os.environ.get("BITS_PATH")
        os.environ.pop("BITS_PATH", None)

    def tearDown(self):
        if self._orig is None:
            os.environ.pop("BITS_PATH", None)
        else:
            os.environ["BITS_PATH"] = self._orig

    def test_append_to_empty(self):
        _add_to_bits_path("/new/dir")
        self.assertEqual(os.environ["BITS_PATH"], "/new/dir")

    def test_append_to_existing(self):
        os.environ["BITS_PATH"] = "alice"
        _add_to_bits_path("/new/dir", "append")
        self.assertEqual(os.environ["BITS_PATH"], "alice,/new/dir")

    def test_prepend(self):
        os.environ["BITS_PATH"] = "alice"
        _add_to_bits_path("/new/dir", "prepend")
        self.assertEqual(os.environ["BITS_PATH"], "/new/dir,alice")

    def test_no_duplicate(self):
        os.environ["BITS_PATH"] = "/new/dir,alice"
        _add_to_bits_path("/new/dir")
        self.assertEqual(os.environ["BITS_PATH"], "/new/dir,alice")


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  3.  clone_or_update_provider – caching logic                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestCloneOrUpdateProvider(unittest.TestCase):
    """Test the caching behaviour of clone_or_update_provider without git."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work_dir = os.path.join(self.tmp, "sw")
        self.ref_dir = os.path.join(self.tmp, "mirror")
        os.makedirs(self.work_dir)
        os.makedirs(self.ref_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec(self, pkg="my-provider"):
        return OrderedDict({
            "package": pkg,
            "version": "v1",
            "source": "https://github.com/test/%s.git" % pkg,
            "tag": "v1",
            "provides_repository": True,
            "repository_position": "append",
        })

    def _mock_scm(self, commit="abcdef1234567890"):
        """Return a Git mock that behaves just enough for clone_or_update_provider."""
        scm = MagicMock()
        scm.listRefsCmd.return_value = ["ls-remote", "--heads", "--tags", "origin"]
        scm.parseRefs.return_value = {
            "refs/tags/v1": commit,
        }
        scm.cloneSourceCmd.return_value = ["clone", "-n", "url", "dest"]
        scm.checkoutCmd.return_value = ["checkout", "v1"]
        # exec() succeeds
        scm.exec.return_value = (0, "")
        return scm

    @patch("bits_helpers.repo_provider.updateReferenceRepoSpec")
    @patch("bits_helpers.repo_provider.logged_scm")
    @patch("bits_helpers.repo_provider.Git")
    def test_cache_miss_clones_and_writes_marker(
            self, MockGit, mock_logged_scm, mock_update_ref):
        commit = "abcdef1234567890"
        scm = self._mock_scm(commit)
        MockGit.return_value = scm
        mock_logged_scm.return_value = "abcdef1234567890\trefs/tags/v1"
        scm.parseRefs.return_value = {"refs/tags/v1": commit}

        spec = self._spec()
        checkout_dir, got_hash = clone_or_update_provider(
            spec, self.work_dir, self.ref_dir, fetch_repos=False)

        # Marker file must exist
        marker = os.path.join(checkout_dir, ".bits_provider_ok")
        self.assertTrue(os.path.exists(marker),
                        "Completion marker not written after clone")
        with open(marker) as fh:
            self.assertEqual(fh.read().strip(), commit)

        # Returned hash must match what ls-remote gave us
        self.assertEqual(got_hash, commit)

        # Git clone must have been called exactly once
        scm.exec.assert_any_call(
            scm.cloneSourceCmd.return_value,
            directory=".", check=False,
        )

    @patch("bits_helpers.repo_provider.updateReferenceRepoSpec")
    @patch("bits_helpers.repo_provider.logged_scm")
    @patch("bits_helpers.repo_provider.Git")
    def test_cache_hit_skips_clone(
            self, MockGit, mock_logged_scm, mock_update_ref):
        commit = "abcdef1234567890"
        scm = self._mock_scm(commit)
        MockGit.return_value = scm
        mock_logged_scm.return_value = "abcdef1234567890\trefs/tags/v1"
        scm.parseRefs.return_value = {"refs/tags/v1": commit}

        spec = self._spec()
        # Pre-populate the cache with a marker
        short = commit[:10]
        cache_root = _provider_cache_root(self.work_dir, spec["package"])
        checkout = os.path.join(cache_root, short)
        os.makedirs(checkout, exist_ok=True)
        with open(os.path.join(checkout, ".bits_provider_ok"), "w") as fh:
            fh.write(commit + "\n")

        checkout_dir, got_hash = clone_or_update_provider(
            spec, self.work_dir, self.ref_dir, fetch_repos=False)

        self.assertEqual(checkout_dir, checkout)
        self.assertEqual(got_hash, commit)
        # No clone must have been attempted
        for c in scm.exec.call_args_list:
            args = c[0][0] if c[0] else []
            self.assertNotIn("clone", args,
                             "Git clone was called despite cache hit")

    @patch("bits_helpers.repo_provider.updateReferenceRepoSpec")
    @patch("bits_helpers.repo_provider.logged_scm")
    @patch("bits_helpers.repo_provider.Git")
    def test_cache_dir_layout(self, MockGit, mock_logged_scm, mock_update_ref):
        """Verify the REPOS/<package>/<short_hash>/ directory layout."""
        commit = "deadbeef12345678"
        scm = self._mock_scm(commit)
        MockGit.return_value = scm
        mock_logged_scm.return_value = deadbeef = "%s\trefs/tags/v1" % commit
        scm.parseRefs.return_value = {"refs/tags/v1": commit}

        spec = self._spec("zlib-recipes")
        checkout_dir, _ = clone_or_update_provider(
            spec, self.work_dir, self.ref_dir, fetch_repos=False)

        expected_root = os.path.join(
            os.path.abspath(self.work_dir), REPOS_CACHE_SUBDIR, "zlib-recipes")
        expected_checkout = os.path.join(expected_root, commit[:10])
        self.assertEqual(checkout_dir, expected_checkout)

        # latest symlink must point to the short hash directory name
        latest = os.path.join(expected_root, "latest")
        self.assertTrue(os.path.islink(latest))
        self.assertEqual(os.readlink(latest), commit[:10])


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  4.  fetch_repo_providers_iteratively                                   ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestFetchRepoProvidersIteratively(unittest.TestCase):
    """Unit tests for the iterative provider-discovery algorithm."""

    def setUp(self):
        self._orig_bits_path = os.environ.get("BITS_PATH")
        os.environ.pop("BITS_PATH", None)
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        if self._orig_bits_path is None:
            os.environ.pop("BITS_PATH", None)
        else:
            os.environ["BITS_PATH"] = self._orig_bits_path

    # ── helpers ────────────────────────────────────────────────────────────

    def _call(self, packages, read_spec_side_effect, clone_side_effect=None,
              provider_policy=None):
        """Run fetch_repo_providers_iteratively with mocked internals."""
        if clone_side_effect is None:
            # Default: return a unique tmp dir + dummy hash per provider call
            counter = [0]
            def _clone(spec, *a, **kw):
                counter[0] += 1
                d = os.path.join(self.tmp, "provider_%d" % counter[0])
                os.makedirs(d, exist_ok=True)
                return d, "hash%04d" % counter[0]
            clone_side_effect = _clone

        with patch.object(rp, "_try_read_spec",
                          side_effect=read_spec_side_effect), \
             patch.object(rp, "clone_or_update_provider",
                          side_effect=clone_side_effect):
            return fetch_repo_providers_iteratively(
                packages=packages,
                config_dir="/cfg",
                work_dir=self.tmp,
                reference_sources=os.path.join(self.tmp, "mirror"),
                fetch_repos=False,
                taps={},
                provider_policy=provider_policy or {},
            )

    # ── tests ──────────────────────────────────────────────────────────────

    def test_no_providers(self):
        """When no package has provides_repository, result is empty."""
        specs = {
            "mypkg": _spec("mypkg"),
            "zlib":  _spec("zlib"),
        }

        def read(pkg, *_):
            return specs.get(pkg)

        result = self._call(["mypkg"], read)
        self.assertEqual(result, {})
        self.assertNotIn("BITS_PATH", os.environ)

    def test_single_provider_is_discovered(self):
        """A direct dependency with provides_repository must be cloned."""
        specs = {
            "mypkg":    _spec("mypkg", requires=["my-recipes"]),
            "my-recipes": _spec("my-recipes", provides=True),
        }

        def read(pkg, *_):
            return specs.get(pkg)

        cloned = []
        def clone(spec, *a, **kw):
            cloned.append(spec["package"])
            d = os.path.join(self.tmp, spec["package"])
            os.makedirs(d, exist_ok=True)
            return d, "hash_" + spec["package"]

        result = self._call(["mypkg"], read, clone)
        self.assertIn("my-recipes", cloned)
        self.assertEqual(len(result), 1)
        checkout_dir = list(result.keys())[0]
        self.assertEqual(result[checkout_dir], ("my-recipes", "hash_my-recipes"))

    def test_provider_added_to_bits_path_append(self):
        """Provider with repository_position=append is appended to BITS_PATH."""
        specs = {"p": _spec("p", provides=True, position="append")}

        def read(pkg, *_):
            return specs.get(pkg)

        checkout = os.path.join(self.tmp, "p")
        self._call(["p"], read, lambda *a, **kw: (checkout, "h1"))
        self.assertIn(checkout, os.environ.get("BITS_PATH", ""))
        # Must not be first
        parts = os.environ["BITS_PATH"].split(",")
        if len(parts) > 1:
            self.assertNotEqual(parts[0], checkout)

    def test_provider_added_to_bits_path_prepend_without_policy_falls_back_to_append(self):
        """A recipe declaring prepend is downgraded to append when no policy grants it."""
        specs = {"p": _spec("p", provides=True, position="prepend")}

        def read(pkg, *_):
            return specs.get(pkg)

        os.environ["BITS_PATH"] = "existing"
        checkout = os.path.join(self.tmp, "p")
        self._call(["p"], read, lambda *a, **kw: (checkout, "h1"))
        parts = os.environ["BITS_PATH"].split(",")
        # Without a policy granting prepend the provider must be appended
        self.assertNotEqual(parts[0], checkout,
                            "Provider should be appended, not prepended, without policy")
        self.assertIn(checkout, parts, "Provider checkout must still appear in BITS_PATH")

    def test_provider_added_to_bits_path_prepend_with_policy(self):
        """A recipe declaring prepend IS prepended when the user's policy explicitly grants it."""
        specs = {"p": _spec("p", provides=True, position="prepend")}

        def read(pkg, *_):
            return specs.get(pkg)

        os.environ["BITS_PATH"] = "existing"
        checkout = os.path.join(self.tmp, "p")
        self._call(["p"], read, lambda *a, **kw: (checkout, "h1"),
                   provider_policy={"p": "prepend"})
        parts = os.environ["BITS_PATH"].split(",")
        self.assertEqual(parts[0], checkout,
                         "Provider should be prepended when policy grants it")

    def test_provider_not_cloned_twice(self):
        """The same provider package is cloned at most once."""
        specs = {
            "a": _spec("a", requires=["p"]),
            "b": _spec("b", requires=["p"]),
            "p": _spec("p", provides=True),
        }

        def read(pkg, *_):
            return specs.get(pkg)

        clone_calls = []
        def clone(spec, *a, **kw):
            clone_calls.append(spec["package"])
            d = os.path.join(self.tmp, spec["package"])
            os.makedirs(d, exist_ok=True)
            return d, "hash"

        self._call(["a", "b"], read, clone)
        self.assertEqual(clone_calls.count("p"), 1,
                         "Provider was cloned more than once")

    def test_nested_providers(self):
        """Provider A whose repo contains provider B must both be discovered.

        Walk:
          top → [a, b];  b not yet visible
          iteration 1: top → a (provider) → clone a → dir_a added to BITS_PATH
          iteration 2: top → a (cached, already cloned)
                            → b (now visible in dir_a, provider) → clone b
          iteration 3: stable (no new providers)

        Note: _try_read_spec receives pkg_lower, so spec-dict keys are
        lower-case.  Package 'b' is a dependency of 'top' but its recipe
        only becomes readable once 'a' has been cloned (dir_a in BITS_PATH).
        """
        dir_a = os.path.join(self.tmp, "dir_a")
        dir_b = os.path.join(self.tmp, "dir_b")
        os.makedirs(dir_a, exist_ok=True)
        os.makedirs(dir_b, exist_ok=True)

        # top depends on both a and b.  b is initially not findable; it only
        # becomes visible once a is cloned and dir_a lands in BITS_PATH.
        specs_initial = {
            "top": _spec("top", requires=["a", "b"]),
            "a":   _spec("a", provides=True),
        }
        specs_after_a = dict(specs_initial)
        specs_after_a["b"] = _spec("b", provides=True)

        def read(pkg, *_):
            # Once a's dir is in BITS_PATH, b's recipe becomes visible
            bits_path = os.environ.get("BITS_PATH", "")
            if dir_a in bits_path:
                return specs_after_a.get(pkg)
            return specs_initial.get(pkg)

        cloned = []
        def clone(spec, *a, **kw):
            cloned.append(spec["package"])
            d = dir_a if spec["package"] == "a" else dir_b
            return d, "hash_" + spec["package"]

        result = self._call(["top"], read, clone)

        self.assertIn("a", cloned, "Provider 'a' was not cloned")
        self.assertIn("b", cloned, "Nested provider 'b' was not cloned")
        self.assertEqual(len(result), 2)

        pkg_names = {name for _, (name, _) in result.items()}
        self.assertIn("a", pkg_names)
        self.assertIn("b", pkg_names)

    def test_max_iterations_guard(self):
        """If providers keep appearing, the loop must stop at MAX_PROVIDER_ITERATIONS."""
        # Every package we see claims to be a new provider that requires
        # a further unknown package, so new providers keep being discovered.
        counter = [0]

        def read(pkg, *_):
            return _spec(pkg, provides=True, requires=["pkg_%d" % (counter[0] + 1)])

        def clone(spec, *a, **kw):
            counter[0] += 1
            pkg = spec["package"]
            d = os.path.join(self.tmp, pkg)
            os.makedirs(d, exist_ok=True)
            return d, "hash_%d" % counter[0]

        # Patch `warning` directly to avoid interacting with the custom
        # LogFormatter (which modifies record.msg in-place and breaks the
        # unittest assertLogs handler's secondary formatting pass).
        with patch("bits_helpers.repo_provider.warning") as mock_warn:
            result = self._call(["pkg_0"], read, clone)

        # A warning about reaching the maximum must have been emitted
        self.assertTrue(mock_warn.called,
                        "No warning emitted when max iterations reached")
        # The warning message should mention "maximum"
        warn_msg = mock_warn.call_args[0][0].lower()
        self.assertIn("maximum", warn_msg)
        self.assertLessEqual(len(result), MAX_PROVIDER_ITERATIONS)

    def test_provider_unavailable_packages_retried_after_clone(self):
        """Packages that were missing before a provider is cloned are re-tried.

        Scenario:
          top → [provider-repo, pkg-from-provider]
          pkg-from-provider is NOT found until provider-repo is cloned.
        """
        dir_p = os.path.join(self.tmp, "provider-repo")
        os.makedirs(dir_p, exist_ok=True)

        specs_base = {
            "top": _spec("top", requires=["provider-repo", "pkg-from-provider"]),
            "provider-repo": _spec("provider-repo", provides=True),
        }

        def read(pkg, *_):
            if dir_p in os.environ.get("BITS_PATH", ""):
                # Once provider is cloned, pkg-from-provider becomes visible
                if pkg == "pkg-from-provider":
                    return _spec("pkg-from-provider")
            return specs_base.get(pkg)

        def clone(spec, *a, **kw):
            return dir_p, "hash_provider"

        # fetch_repo_providers_iteratively should not die even though
        # pkg-from-provider is initially missing.
        result = self._call(["top"], read, clone)
        self.assertIn(dir_p, result)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  5.  getPackageList – provider_dirs tracking                            ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# Recipes used by the package-list test
_PKGLIST_RECIPES = {
    "CONFIG_DIR/defaults-release.sh": dedent("""\
        package: defaults-release
        version: v1
        ---
        """),
    "CONFIG_DIR/top.sh": dedent("""\
        package: top
        version: v1
        requires:
          - provider-pkg
        ---
        : build top
        """),
    # provider-pkg recipe lives under CONFIG_DIR directly (the test treats
    # CONFIG_DIR itself as the provider dir to keep things simple)
    "CONFIG_DIR/provider-pkg.sh": dedent("""\
        package: provider-pkg
        version: v1
        source: https://github.com/test/provider-pkg.git
        tag: v1
        ---
        : # provider build
        """),
}


class MockReaderPkgList:
    def __init__(self, url, dist=None, genPackages=None):
        self._contents = _PKGLIST_RECIPES[url]
        self.url = "mock://" + url

    def __call__(self):
        return self._contents


@mock.patch("bits_helpers.utilities.getRecipeReader", new=MockReaderPkgList)
@mock.patch("bits_helpers.paths.exists",
            new=lambda f: f in _PKGLIST_RECIPES)
class TestGetPackageListProviderDirs(unittest.TestCase):
    """Verify that recipe_provider / recipe_provider_hash are populated."""

    def _call(self, packages, provider_dirs):
        specs = {}
        getPackageList(
            packages=packages,
            specs=specs,
            configDir="CONFIG_DIR",
            preferSystem=False,
            noSystem=None,
            architecture="ARCH",
            disable=[],
            defaults=["release"],
            performPreferCheck=lambda pkg, cmd: (0, ""),
            performRequirementCheck=lambda pkg, cmd: (0, ""),
            performValidateDefaults=lambda spec: (True, "", ["release"]),
            overrides={"defaults-release": {}},
            taps={},
            log=lambda *_: None,
            provider_dirs=provider_dirs,
        )
        return specs

    def test_recipe_provider_set_when_pkgdir_matches(self):
        """When pkgdir is in provider_dirs, spec gains recipe_provider keys."""
        # CONFIG_DIR is the pkgdir for provider-pkg.sh in our mock setup
        provider_dirs = {
            "CONFIG_DIR": ("my-repo-provider", "abcdef1234567890"),
        }
        specs = self._call(["top"], provider_dirs)
        self.assertIn("provider-pkg", specs)
        self.assertEqual(specs["provider-pkg"]["recipe_provider"],
                         "my-repo-provider")
        self.assertEqual(specs["provider-pkg"]["recipe_provider_hash"],
                         "abcdef1234567890")

    def test_recipe_provider_not_set_when_no_match(self):
        """When pkgdir is NOT in provider_dirs, spec has no recipe_provider."""
        specs = self._call(["top"], provider_dirs={})
        self.assertIn("provider-pkg", specs)
        self.assertNotIn("recipe_provider", specs["provider-pkg"])
        self.assertNotIn("recipe_provider_hash", specs["provider-pkg"])

    def test_top_level_pkg_not_tagged_as_provider_sourced(self):
        """Packages from the base configDir should never get recipe_provider."""
        # Use a provider_dirs dict whose key does NOT match CONFIG_DIR
        provider_dirs = {"/some/other/dir": ("other-provider", "0000")}
        specs = self._call(["top"], provider_dirs)
        self.assertNotIn("recipe_provider", specs.get("top", {}))


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  6.  cwd_is_recipe_dir                                                  ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestCwdIsRecipeDir(unittest.TestCase):
    """Unit tests for the CWD recipe-directory detector."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._orig_cwd = os.getcwd()
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content=""):
        with open(os.path.join(self.tmp, name), "w") as fh:
            fh.write(content)

    def test_empty_directory_returns_false(self):
        self.assertFalse(cwd_is_recipe_dir())

    def test_only_package_recipes_without_defaults_returns_false(self):
        """Random recipe files are not enough — defaults-release.sh is required."""
        self._write("ROOT.sh", "package: ROOT\nversion: v6\n---\n: build\n")
        self._write("zlib.sh", "package: zlib\nversion: v1\n---\n: build\n")
        self.assertFalse(cwd_is_recipe_dir())

    def test_defaults_release_sh_returns_true(self):
        """Presence of defaults-release.sh is the definitive marker."""
        self._write("defaults-release.sh", "package: defaults-release\nversion: v1\n---\n")
        self.assertTrue(cwd_is_recipe_dir())

    def test_defaults_release_sh_alongside_other_recipes_returns_true(self):
        """A realistic recipe repo with multiple recipes plus defaults-release.sh."""
        self._write("defaults-release.sh", "package: defaults-release\nversion: v1\n---\n")
        self._write("ROOT.sh", "package: ROOT\nversion: v6\n---\n: build\n")
        self._write("zlib.sh", "package: zlib\nversion: v1\n---\n: build\n")
        self.assertTrue(cwd_is_recipe_dir())

    def test_no_sh_files_returns_false(self):
        self._write("README.md", "# hello\n")
        self._write("build.py", "print('hi')\n")
        self.assertFalse(cwd_is_recipe_dir())

    def test_similarly_named_file_does_not_match(self):
        """defaults-release-extra.sh or xdefaults-release.sh must not match."""
        self._write("defaults-release-extra.sh", "package: x\nversion: v1\n---\n")
        self._write("xdefaults-release.sh", "package: y\nversion: v1\n---\n")
        self.assertFalse(cwd_is_recipe_dir())


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  7.  bootstrap_default_config                                           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestBootstrapDefaultConfig(unittest.TestCase):
    """Unit tests for the backward-compat auto-bootstrap in bootstrap_default_config."""

    # Minimal args mock
    @staticmethod
    def _make_args(bits_providers="https://github.com/bitsorg/bits-providers",
                   organisation=None):
        args = MagicMock()
        args.bits_providers = bits_providers
        args.organisation = organisation
        args.referenceSources = ""
        args.fetchRepos = False
        return args

    # A minimal default.bits.sh recipe text
    _DEFAULT_SH = dedent("""\
        package: default.bits
        version: "1"
        tag: main
        provides_repository: true
        source: https://github.com/bitsorg/alice.bits
        ---
    """)

    # A minimal common.bits.sh so that parseRecipe doesn't complain about requires
    _BITS_PROVIDERS_SH = dedent("""\
        package: bits-providers
        version: "1"
        source: https://github.com/bitsorg/bits-providers
        tag: main
        provides_repository: true
        always_load: true
        ---
    """)

    def _make_clone_side_effect(self, providers_checkout, config_checkout):
        """Return a side_effect that alternates checkouts per call."""
        calls = iter([
            (providers_checkout, "abc123"),
            (config_checkout,   "def456"),
        ])
        return lambda *a, **kw: next(calls)

    @patch("bits_helpers.repo_provider.info")      # avoid LogFormatter double-format bug
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    @patch("bits_helpers.repo_provider.parseRecipe")
    @patch("bits_helpers.repo_provider.getRecipeReader")
    @patch("bits_helpers.repo_provider.exists")   # module-level name, not os.path.exists
    def test_returns_config_checkout_when_default_sh_found(
        self, mock_exists, mock_reader, mock_parse, mock_clone, _info,
    ):
        """Happy path: bits-providers has default.bits.sh → returns config checkout."""
        providers_checkout = "/work/REPOS/bits-providers/abc1234567"
        config_checkout    = "/work/REPOS/default.bits/def4567890"

        # exists() returns True only for the default.bits.sh path
        mock_exists.side_effect = lambda p: p.endswith("default.bits.sh")
        mock_reader.return_value = "reader"
        mock_parse.return_value = (
            None,
            OrderedDict({
                "package": "default.bits",
                "version": "1",
                "tag": "main",
                "provides_repository": True,
                "source": "https://github.com/bitsorg/alice.bits",
            }),
            {},
        )
        mock_clone.side_effect = self._make_clone_side_effect(
            providers_checkout, config_checkout,
        )

        result = bootstrap_default_config(self._make_args(), "/work")

        self.assertEqual(result, config_checkout)
        self.assertEqual(mock_clone.call_count, 2)

    @patch("bits_helpers.repo_provider.info")      # avoid LogFormatter double-format bug
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    @patch("bits_helpers.repo_provider.exists", return_value=False)
    def test_returns_none_when_no_default_sh(self, mock_exists, mock_clone, _info):
        """When default.bits.sh is absent, returns None without cloning config."""
        providers_checkout = "/work/REPOS/bits-providers/abc1234567"
        mock_clone.return_value = (providers_checkout, "abc123")

        result = bootstrap_default_config(self._make_args(), "/work")

        self.assertIsNone(result)
        # Only the bits-providers clone should have been attempted
        self.assertEqual(mock_clone.call_count, 1)

    def test_returns_none_when_no_bits_providers_url(self):
        """When args.bits_providers is falsy, returns None immediately."""
        args = self._make_args(bits_providers="")
        result = bootstrap_default_config(args, "/work")
        self.assertIsNone(result)

    @patch("bits_helpers.repo_provider.warning")   # avoid LogFormatter double-format bug
    @patch("bits_helpers.repo_provider.clone_or_update_provider",
           side_effect=SystemExit(1))
    def test_returns_none_when_bits_providers_clone_fails(self, mock_clone, _warn):
        """A clone failure for bits-providers is caught and returns None."""
        result = bootstrap_default_config(self._make_args(), "/work")
        self.assertIsNone(result)

    @patch("bits_helpers.repo_provider.warning")   # avoid LogFormatter double-format bug
    @patch("bits_helpers.repo_provider.info")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    @patch("bits_helpers.repo_provider.parseRecipe")
    @patch("bits_helpers.repo_provider.getRecipeReader")
    @patch("bits_helpers.repo_provider.exists")   # module-level name, not os.path.exists
    def test_returns_none_when_config_clone_fails(
        self, mock_exists, mock_reader, mock_parse, mock_clone, _info, _warn,
    ):
        """A clone failure for the default config repo is caught and returns None."""
        providers_checkout = "/work/REPOS/bits-providers/abc1234567"
        mock_exists.side_effect = lambda p: p.endswith("default.bits.sh")
        mock_reader.return_value = "reader"
        mock_parse.return_value = (
            None,
            OrderedDict({
                "package": "default.bits",
                "version": "1",
                "tag": "main",
                "provides_repository": True,
                "source": "https://github.com/bitsorg/alice.bits",
            }),
            {},
        )
        # First call (bits-providers) succeeds; second (default config) fails
        mock_clone.side_effect = [
            (providers_checkout, "abc123"),
            SystemExit(1),
        ]
        result = bootstrap_default_config(self._make_args(), "/work")
        self.assertIsNone(result)

    # ── Organisation-aware lookup ──────────────────────────────────────────

    @patch("bits_helpers.repo_provider.info")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    @patch("bits_helpers.repo_provider.parseRecipe")
    @patch("bits_helpers.repo_provider.getRecipeReader")
    @patch("bits_helpers.repo_provider.exists")
    def test_organisation_recipe_preferred_over_default(
        self, mock_exists, mock_reader, mock_parse, mock_clone, _info,
    ):
        """Organisation stored uppercase (LHCB) → resolves to lhcb.bits.sh (lowercase)."""
        providers_checkout = "/work/REPOS/bits-providers/abc1234567"
        config_checkout    = "/work/REPOS/lhcb.bits/def4567890"

        # Only lhcb.bits.sh (lowercase) exists — default.bits.sh is absent.
        # args.organisation is uppercase "LHCB" as stored in bits.rc.
        mock_exists.side_effect = lambda p: p.endswith("lhcb.bits.sh")
        mock_reader.return_value = "reader"
        mock_parse.return_value = (
            None,
            OrderedDict({
                "package": "lhcb.bits",
                "version": "1",
                "tag": "main",
                "provides_repository": True,
                "source": "https://github.com/bitsorg/lhcb.bits",
            }),
            {},
        )
        mock_clone.side_effect = self._make_clone_side_effect(
            providers_checkout, config_checkout,
        )

        # Pass uppercase "LHCB" — the same value bits.rc stores
        result = bootstrap_default_config(
            self._make_args(organisation="LHCB"), "/work",
        )

        self.assertEqual(result, config_checkout)
        # Verify the reader was called with the lowercased lhcb path
        reader_path = mock_reader.call_args[0][0]
        self.assertIn("lhcb.bits.sh", reader_path)
        self.assertNotIn("default.bits.sh", reader_path)

    @patch("bits_helpers.repo_provider.info")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    @patch("bits_helpers.repo_provider.parseRecipe")
    @patch("bits_helpers.repo_provider.getRecipeReader")
    @patch("bits_helpers.repo_provider.exists")
    def test_organisation_falls_back_to_default_sh_when_org_sh_absent(
        self, mock_exists, mock_reader, mock_parse, mock_clone, _info,
    ):
        """When <org>.bits.sh is absent, bootstrap falls back to default.bits.sh."""
        providers_checkout = "/work/REPOS/bits-providers/abc1234567"
        config_checkout    = "/work/REPOS/default.bits/def4567890"

        # LHCB.bits.sh / lhcb.bits.sh absent, default.bits.sh present
        mock_exists.side_effect = lambda p: p.endswith("default.bits.sh")
        mock_reader.return_value = "reader"
        mock_parse.return_value = (
            None,
            OrderedDict({
                "package": "default.bits",
                "version": "1",
                "tag": "main",
                "provides_repository": True,
                "source": "https://github.com/bitsorg/alice.bits",
            }),
            {},
        )
        mock_clone.side_effect = self._make_clone_side_effect(
            providers_checkout, config_checkout,
        )

        # Pass uppercase "LHCB" — fallback must still find default.bits.sh
        result = bootstrap_default_config(
            self._make_args(organisation="LHCB"), "/work",
        )

        self.assertEqual(result, config_checkout)
        reader_path = mock_reader.call_args[0][0]
        self.assertIn("default.bits.sh", reader_path)

    @patch("bits_helpers.repo_provider.info")
    @patch("bits_helpers.repo_provider.clone_or_update_provider")
    @patch("bits_helpers.repo_provider.exists", return_value=False)
    def test_organisation_returns_none_when_neither_sh_found(
        self, _exists, mock_clone, _info,
    ):
        """When both <org>.bits.sh and default.bits.sh are absent, returns None."""
        providers_checkout = "/work/REPOS/bits-providers/abc1234567"
        mock_clone.return_value = (providers_checkout, "abc123")

        result = bootstrap_default_config(
            self._make_args(organisation="LHCB"), "/work",
        )
        self.assertIsNone(result)
        self.assertEqual(mock_clone.call_count, 1)  # only bits-providers was cloned


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  7.  storeHashes – provider hash folded in                              ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestStoreHashesProviderHash(unittest.TestCase):
    """The provider repo's commit hash must NOT affect the build hash: a package
    is hashed by its own inputs, so editing one recipe rebuilds only that package
    (+ dependents), never the whole provider repo."""

    # Minimal spec factory for storeHashes
    @staticmethod
    def _make_spec(**overrides):
        spec = OrderedDict({
            "package": "mypkg",
            "version": "v1",
            "recipe": ": build",
            "tag": "v1",
            "commit_hash": "abc123",
            "is_devel_pkg": False,
            "scm_refs": {},
            "requires": [],
        })
        spec.update(overrides)
        return spec

    def _call_store_hashes(self, spec):
        from bits_helpers.build import storeHashes
        specs = {spec["package"]: spec, "defaults-release": self._make_spec(
            package="defaults-release", version="v1", requires=[])}
        storeHashes(spec["package"], specs, considerRelocation=False)
        return spec

    def test_same_recipe_different_provider_hash_gives_same_build_hash(self):
        """A new provider commit hash must NOT change the build hash when the
        recipe's own inputs are unchanged (no more whole-repo rebuilds)."""
        spec_a = self._make_spec(recipe_provider="my-repo",
                                 recipe_provider_hash="hash_old")
        spec_b = self._make_spec(recipe_provider="my-repo",
                                 recipe_provider_hash="hash_new")

        self._call_store_hashes(spec_a)
        self._call_store_hashes(spec_b)

        self.assertEqual(
            spec_a["remote_revision_hash"],
            spec_b["remote_revision_hash"],
            "Provider commit hash leaked into the package build hash",
        )

    def test_same_recipe_same_provider_hash_gives_same_build_hash(self):
        """Identical recipe + identical provider hash → identical build hash."""
        spec_a = self._make_spec(recipe_provider="my-repo",
                                 recipe_provider_hash="stable_hash")
        spec_b = self._make_spec(recipe_provider="my-repo",
                                 recipe_provider_hash="stable_hash")

        self._call_store_hashes(spec_a)
        self._call_store_hashes(spec_b)

        self.assertEqual(
            spec_a["remote_revision_hash"],
            spec_b["remote_revision_hash"],
        )

    def test_no_provider_hash_does_not_break_hashing(self):
        """Packages without recipe_provider_hash must still hash correctly."""
        spec = self._make_spec()
        self._call_store_hashes(spec)
        self.assertIn("remote_revision_hash", spec)
        self.assertIn("local_revision_hash", spec)

    def test_provider_hash_does_not_change_hash_vs_no_provider(self):
        """A package with a provider hash must hash IDENTICALLY to one without —
        the provider hash is provenance only and never enters the build hash."""
        spec_with    = self._make_spec(recipe_provider="r", recipe_provider_hash="x")
        spec_without = self._make_spec()

        self._call_store_hashes(spec_with)
        self._call_store_hashes(spec_without)

        self.assertEqual(
            spec_with["remote_revision_hash"],
            spec_without["remote_revision_hash"],
        )


class TestApplyProviderOverride(unittest.TestCase):
    """Defaults `overrides: <provider>: {source, tag}` reach the provider clone."""

    def test_override_source_and_tag_with_variable(self):
        spec = {"package": "lcg.bits", "version": "1", "tag": "main",
                "source": "https://github.com/bitsorg/lcg.bits",
                "provides_repository": True}
        overrides = {"lcg.bits": {
            "source": "https://gitlab.cern.ch/sft/stacks/bits/lcg.bits",
            "tag": "%(lcgversion)s"}}
        rp._apply_provider_override(spec, "lcg.bits", overrides,
                                    ["release"], {"lcgversion": "v42"})
        self.assertEqual(spec["source"],
                         "https://gitlab.cern.ch/sft/stacks/bits/lcg.bits")
        self.assertEqual(spec["tag"], "v42")           # %(lcgversion)s expanded

    def test_override_matches_case_insensitively(self):
        # override keys are lowercased by parseDefaults; the require may be mixed.
        spec = {"package": "LCG.bits", "tag": "main", "source": "orig"}
        rp._apply_provider_override(spec, "LCG.bits", {"lcg.bits": {"tag": "x"}},
                                    ["release"], {})
        self.assertEqual(spec["tag"], "x")

    def test_noop_without_matching_override(self):
        spec = {"package": "lcg.bits", "tag": "main", "source": "orig"}
        rp._apply_provider_override(spec, "lcg.bits", {"other": {"tag": "z"}},
                                    ["release"], {})
        self.assertEqual((spec["source"], spec["tag"]), ("orig", "main"))
        # also a no-op when overrides is None
        rp._apply_provider_override(spec, "lcg.bits", None, ["release"], {})
        self.assertEqual((spec["source"], spec["tag"]), ("orig", "main"))


if __name__ == "__main__":
    unittest.main()
