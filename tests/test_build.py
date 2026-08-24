# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

from argparse import Namespace
import os
import os.path
import platform
import re
import sys
import unittest
# Assuming you are using the mock library to ... mock things
from unittest.mock import call, patch, MagicMock, DEFAULT
from io import StringIO
from collections import OrderedDict

from bits_helpers.utilities import parseRecipe, resolve_tag
from bits_helpers.build import doBuild, storeHashes, generate_initdotsh

# Determine architecture based on platform
def get_test_architecture():
    if sys.platform == 'darwin':
        machine = platform.machine()
        if machine == 'arm64':
            return 'osx_arm64'
        else:
            return 'osx_x86-64'
    else:
        return 'slc7_x86-64'

TEST_ARCHITECTURE = os.environ.get('ARCHITECTURE', get_test_architecture())


TEST_DEFAULT_RELEASE = """\
package: defaults-release
version: v1
---
: this line should trigger a warning
"""
TEST_DEFAULT_RELEASE_BUILD_HASH = "27ce49698e818e8efb56b6eff6dd785e503df341"

TEST_ZLIB_RECIPE = """\
package: zlib
version: v1.3.1
source: https://github.com/madler/zlib
tag: master
---
./configure
make
make install
"""
TEST_ZLIB_GIT_REFS = "8822efa61f2a385e0bc83ca5819d608111b2168a\trefs/heads/master"
TEST_ZLIB_BUILD_HASH = "4d6a75f214dc7931a2a7d5ba82ea0568e652cd84"

TEST_ROOT_RECIPE = """\
package: ROOT
version: v6-08-30
source: https://github.com/root-mirror/root
tag: v6-08-00-patches
requires:
  - zlib
env:
  ROOT_TEST_1: "root test 1"
  ROOT_TEST_2: "root test 2"
  ROOT_TEST_3: "root test 3"
  ROOT_TEST_4: "root test 4"
  ROOT_TEST_5: "root test 5"
  ROOT_TEST_6: "root test 6"
prepend_path:
  PREPEND_ROOT_1: "prepend root 1"
  PREPEND_ROOT_2: "prepend root 2"
  PREPEND_ROOT_3: "prepend root 3"
  PREPEND_ROOT_4: "prepend root 4"
  PREPEND_ROOT_5: "prepend root 5"
  PREPEND_ROOT_6: "prepend root 6"
append_path:
  APPEND_ROOT_1: "append root 1"
  APPEND_ROOT_2: "append root 2"
  APPEND_ROOT_3: "append root 3"
  APPEND_ROOT_4: "append root 4"
  APPEND_ROOT_5: "append root 5"
  APPEND_ROOT_6: "append root 6"
---
./configure
make
make install
"""
TEST_ROOT_GIT_REFS = """\
87b87c4322d2a3fad315c919cb2e2dd73f2154dc\trefs/heads/master
f7b336611753f1f4aaa94222b0d620748ae230c0\trefs/heads/v6-08-00-patches
f7b336611753f1f4aaa94222b0d620748ae230c0\trefs/tags/test-tag"""
TEST_ROOT_BUILD_HASH = ("1f3c771080f71b6c0d2e3d7a285698a20035da12")


TEST_EXTRA_RECIPE = """\
package: Extra
version: v1
tag: v1
source: file:///dev/null
requires:
  - ROOT
---
"""
TEST_EXTRA_GIT_REFS = """\
f000\trefs/heads/master
ba22\trefs/tags/v1
ba22\trefs/tags/v2
baad\trefs/tags/v3"""
TEST_EXTRA_BUILD_HASH = ("6e7bc4976abf77b558cf7faf575ec51670f8d0e5")


GIT_CLONE_REF_ZLIB_ARGS = ("clone", "--bare", "https://github.com/madler/zlib",
                           "/sw/MIRROR/zlib", "--filter=blob:none"), ".", False
GIT_CLONE_SRC_ZLIB_ARGS = ("clone", "-n", "https://github.com/madler/zlib",
                           "/sw/SOURCES/zlib/v1.3.1/8822efa61f",
                           "--dissociate", "--reference", "/sw/MIRROR/zlib", "--filter=blob:none"), ".", False
GIT_SET_URL_ZLIB_ARGS = ("remote", "set-url", "--push", "origin", "https://github.com/madler/zlib"), \
    "/sw/SOURCES/zlib/v1.3.1/8822efa61f", False
GIT_CHECKOUT_ZLIB_ARGS = ("checkout", "-f", "master"), \
    "/sw/SOURCES/zlib/v1.3.1/8822efa61f", False

GIT_FETCH_REF_ROOT_ARGS = ("fetch", "-f", "--prune", "--filter=blob:none", "https://github.com/root-mirror/root", "+refs/tags/*:refs/tags/*",
                           "+refs/heads/*:refs/heads/*"), "/sw/MIRROR/root", False
GIT_CLONE_SRC_ROOT_ARGS = ("clone", "-n", "https://github.com/root-mirror/root",
                           "/sw/SOURCES/ROOT/v6-08-30/f7b3366117",
                           "--dissociate", "--reference", "/sw/MIRROR/root", "--filter=blob:none"), ".", False
GIT_SET_URL_ROOT_ARGS = ("remote", "set-url", "--push", "origin", "https://github.com/root-mirror/root"), \
    "/sw/SOURCES/ROOT/v6-08-30/f7b3366117", False
GIT_CHECKOUT_ROOT_ARGS = ("checkout", "-f", "v6-08-00-patches"), \
    "/sw/SOURCES/ROOT/v6-08-30/f7b3366117", False


def dummy_git(args, directory=".", check=True, prompt=True):
    return {
        (("symbolic-ref", "-q", "HEAD"), "/bits", False): (0, "master"),
        (("rev-parse", "HEAD"), "/alidist", True): "6cec7b7b3769826219dfa85e5daa6de6522229a0",
        (("ls-remote", "--heads", "--tags", "/sw/MIRROR/root"), ".", False): (0, TEST_ROOT_GIT_REFS),
        (("ls-remote", "--heads", "--tags", "/sw/MIRROR/zlib"), ".", False): (0, TEST_ZLIB_GIT_REFS),
        GIT_CLONE_REF_ZLIB_ARGS: (0, ""),
        GIT_CLONE_SRC_ZLIB_ARGS: (0, ""),
        GIT_SET_URL_ZLIB_ARGS: (0, ""),
        GIT_CHECKOUT_ZLIB_ARGS: (0, ""),
        GIT_FETCH_REF_ROOT_ARGS: (0, ""),
        GIT_CLONE_SRC_ROOT_ARGS: (0, ""),
        GIT_SET_URL_ROOT_ARGS: (0, ""),
        GIT_CHECKOUT_ROOT_ARGS: (0, ""),
    }[(tuple(args), directory, check)]


TIMES_ASKED = {}


_REAL_OPEN = open        # captured before any @patch replaces module opens


def _mock_write_cm():
    """Return a MagicMock usable as a write-mode context manager."""
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=StringIO())
    cm.__exit__ = MagicMock(return_value=False)
    return cm


def dummy_open(x, mode="r", encoding=None, errors=None):
    if x.endswith("/fetch-log.txt") and mode == "w":
        return _mock_write_cm()
    if x.endswith("/bits_helpers/build_template.sh"):
        # Actually open the real build_template.sh. (Returning mock.DEFAULT
        # from a side_effect does NOT call the real function — it returns the
        # mock's return_value, so cmd_raw used to be a MagicMock that the old
        # un-entered write mock silently absorbed.)
        return _REAL_OPEN(x, mode)

    # Write-mode guard: absorb any write to paths under the mock work directory
    # (/sw/…) or to ledger/manifest files so that store-integrity or manifest
    # code that fires during the test does not attempt real filesystem access.
    if mode in ("w", "a", "wb", "ab"):
        if x.startswith("/sw/") or "STORE_CHECKSUMS" in x or "bits-manifest" in x:
            return _mock_write_cm()

    if mode == "r":
        try:
            threshold, result = {
                "/sw/BUILD/%s/defaults-release/.build_succeeded" % TEST_DEFAULT_RELEASE_BUILD_HASH: (0, StringIO("0")),
                "/sw/BUILD/%s/zlib/.build_succeeded" % TEST_ZLIB_BUILD_HASH: (0, StringIO("0")),
                "/sw/BUILD/%s/ROOT/.build_succeeded" % TEST_ROOT_BUILD_HASH: (0, StringIO("0")),
                f"/sw/{TEST_ARCHITECTURE}/defaults-release/v1-1/.build-hash": (1, StringIO(TEST_DEFAULT_RELEASE_BUILD_HASH)),
                f"/sw/{TEST_ARCHITECTURE}/zlib/v1.3.1-local1/.build-hash": (1, StringIO(TEST_ZLIB_BUILD_HASH)),
                f"/sw/{TEST_ARCHITECTURE}/ROOT/v6-08-30-local1/.build-hash": (1, StringIO(TEST_ROOT_BUILD_HASH))
            }[x]
        except KeyError:
            # Store-integrity ledger reads: return an empty file so that
            # verify_tarball_checksum treats the entry as absent (no ledger).
            if "STORE_CHECKSUMS" in x:
                raise OSError
            return DEFAULT
        if threshold > TIMES_ASKED.get(x, 0):
            result = None
        TIMES_ASKED[x] = TIMES_ASKED.get(x, 0) + 1
        if not result:
            raise OSError
        return result
    return DEFAULT


def dummy_execute(x, **kwds):
    s = " ".join(x) if isinstance(x, list) else x
    if re.match(".*ln -sfn.*TARS", s):
        return 0
    return {
        f"/bin/bash -e -x /sw/SPECS/{TEST_ARCHITECTURE}/defaults-release/v1-1/build.sh 2>&1": 0,
        f'/bin/bash -e -x /sw/SPECS/{TEST_ARCHITECTURE}/zlib/v1.3.1-local1/build.sh 2>&1': 0,
        f'/bin/bash -e -x /sw/SPECS/{TEST_ARCHITECTURE}/ROOT/v6-08-30-local1/build.sh 2>&1': 0,
    }[s]


def dummy_readlink(x):
    return {
        f"/sw/TARS/{TEST_ARCHITECTURE}/defaults-release/defaults-release-v1-1.{TEST_ARCHITECTURE}.tar.gz":
        f"../../{TEST_ARCHITECTURE}/store/{TEST_DEFAULT_RELEASE_BUILD_HASH[:2]}/{TEST_DEFAULT_RELEASE_BUILD_HASH}/defaults-release-v1-1.{TEST_ARCHITECTURE}.tar.gz"
    }[x]


# Paths that os.path.isfile() should report as existing in the mock world.
# These correspond to the symlink entries in dummy_readlink that have valid
# (non-dangling) targets — i.e., every key in the dummy_readlink dict.
_MOCK_EXISTING_FILES = frozenset({
    f"/sw/TARS/{TEST_ARCHITECTURE}/defaults-release/defaults-release-v1-1.{TEST_ARCHITECTURE}.tar.gz",
})

# Save a reference to the real os.path.isfile *before* any test patch
# replaces it.  dummy_isfile must not call os.path.isfile (which becomes
# the mock during the test) or it will recurse infinitely.
_real_isfile = os.path.isfile

def dummy_isfile(x):
    """Mock for os.path.isfile that returns True for paths known to the mock
    world (the symlinks in dummy_readlink whose targets "exist"), and falls
    back to the real os.path.isfile for everything else.  This is needed so
    that the dangling-symlink guard added to the revision-scan loop in
    build.py does not treat mock symlinks as dangling."""
    if x in _MOCK_EXISTING_FILES:
        return True
    return _real_isfile(x)


def dummy_exists(x):
    # Convert Path objects to strings for comparison
    path_str = str(x) if hasattr(x, '__fspath__') else x
    if path_str.endswith("bits_helpers/.git"):
        return False
    # Return False for any sapling-related paths
    if ".sl" in path_str or path_str.endswith("/sl"):
        return False
    known = {
        "/alidist": True,
        "/alidist/.git": True,
        "/sw": True,
        "/sw/SPECS": False,
        "/sw/MIRROR/root": True,
        "/sw/MIRROR/root/.git": True,
        "/sw/MIRROR/zlib": False,
    }
    if path_str in known:
        return known[path_str]
    # In a clean build the per-package source checkout has no pre-existing
    # ".git" (so the clone-guard must not fire) and there is no in-progress
    # download ".downloading" sentinel (so the prefetch wait returns at once).
    # The broad DEFAULT (truthy) fallback would otherwise report both as
    # present and make those guards misbehave.
    if path_str.endswith(".git") or path_str.endswith(".downloading"):
        return False
    return DEFAULT


# A few errors we should handle, together with the expected result
@patch("bits_helpers.git.clone_speedup_options",
       new=MagicMock(return_value=["--filter=blob:none"]))
@patch("bits_helpers.build.BASH", new="/bin/bash")
class BuildTestCase(unittest.TestCase):
    @patch("bits_helpers.analytics", new=MagicMock())
    @patch("requests.Session.get", new=MagicMock())
    @patch("bits_helpers.sync.execute", new=dummy_execute)
    @patch("bits_helpers.git.git")
    @patch("bits_helpers.build.exists", new=MagicMock(side_effect=dummy_exists))
    @patch("bits_helpers.utilities.exists", new=MagicMock(side_effect=dummy_exists))
    @patch("os.path.exists", new=MagicMock(side_effect=dummy_exists))
    @patch("os.path.isfile", new=MagicMock(side_effect=dummy_isfile))
    @patch("bits_helpers.build.dieOnError", new=MagicMock())
    @patch("bits_helpers.utilities.dieOnError", new=MagicMock())
    @patch("bits_helpers.utilities.warning")
    @patch("bits_helpers.build.readDefaults",
           new=MagicMock(return_value=(OrderedDict({"package": "defaults-release", "disable": []}), "")))
    @patch("shutil.rmtree", new=MagicMock(return_value=None))
    @patch("os.makedirs", new=MagicMock(return_value=None))
    @patch("bits_helpers.build.makedirs", new=MagicMock(return_value=None))
    @patch("bits_helpers.build.symlink", new=MagicMock(return_value=None))
    @patch("bits_helpers.workarea.symlink", new=MagicMock(return_value=None))
    @patch("bits_helpers.utilities.open", new=lambda x: {
        "/alidist/root.sh": StringIO(TEST_ROOT_RECIPE),
        "/alidist/zlib.sh": StringIO(TEST_ZLIB_RECIPE),
        "/alidist/defaults-release.sh": StringIO(TEST_DEFAULT_RELEASE)
    }[x])
    @patch("bits_helpers.sync.open", new=MagicMock(side_effect=dummy_open))
    @patch("bits_helpers.build.open", new=MagicMock(side_effect=dummy_open))
    @patch("codecs.open", new=MagicMock(side_effect=dummy_open))
    @patch("bits_helpers.build.shutil", new=MagicMock())
    @patch("os.listdir")
    @patch("bits_helpers.build.glob", new=lambda pattern: {
        "*": ["zlib"],
        # Stale-sentinel cleanup scans these two depth-bounded globs; no stale
        # .downloading sentinels in this fixture.
        "/sw/SOURCES/*/*/*.downloading": [],
        "/sw/TARS/*/store/*/*.downloading": [],
        f"/sw/TARS/{TEST_ARCHITECTURE}/store/{TEST_DEFAULT_RELEASE_BUILD_HASH[:2]}/{TEST_DEFAULT_RELEASE_BUILD_HASH}/*gz": [],
        f"/sw/TARS/{TEST_ARCHITECTURE}/store/{TEST_ZLIB_BUILD_HASH[:2]}/{TEST_ZLIB_BUILD_HASH}/*gz": [],
        f"/sw/TARS/{TEST_ARCHITECTURE}/store/{TEST_ROOT_BUILD_HASH[:2]}/{TEST_ROOT_BUILD_HASH}/*gz": [],
        f"/sw/TARS/{TEST_ARCHITECTURE}/defaults-release/defaults-release-v1-1.{TEST_ARCHITECTURE}.tar.gz":
        [f"../../{TEST_ARCHITECTURE}/store/{TEST_DEFAULT_RELEASE_BUILD_HASH[:2]}/{TEST_DEFAULT_RELEASE_BUILD_HASH}/defaults-release-v1-1.{TEST_ARCHITECTURE}.tar.gz"],
    }[pattern])
    @patch("bits_helpers.build.readlink", new=dummy_readlink)
    @patch("bits_helpers.build.banner", new=MagicMock(return_value=None))
    @patch("bits_helpers.build.debug")
    @patch("bits_helpers.workarea.is_writeable", new=MagicMock(return_value=True))
    @patch("bits_helpers.build.basename", new=MagicMock(return_value="aliBuild"))
    @patch("bits_helpers.build.install_wrapper_script", new=MagicMock())
    # Mock out the build manifest so it does not try to write real files into
    # the mock work directory (/sw/).  The manifest's _save() calls builtins
    # open() and os.replace() which are not intercepted by the bits_helpers.build
    # open mock, so we replace the whole class with a MagicMock.
    @patch("bits_helpers.manifest.BuildManifest", new=MagicMock())
    # Absorb os.replace and os.symlink in the manifest module so that even if
    # BuildManifest is somehow constructed it cannot touch the real filesystem.
    @patch("bits_helpers.manifest.os.replace", new=MagicMock())
    @patch("bits_helpers.manifest.os.symlink", new=MagicMock())
    def test_coverDoBuild(self, mock_debug, mock_listdir, mock_warning, mock_git_git) -> None:
        mock_git_git.side_effect = dummy_git
        mock_debug.side_effect = lambda *args: None
        mock_warning.side_effect = lambda *args: None
        mock_listdir.side_effect = lambda directory: {
            f"/sw/TARS/{TEST_ARCHITECTURE}/defaults-release": [f"defaults-release-v1-1.{TEST_ARCHITECTURE}.tar.gz"],
            f"/sw/TARS/{TEST_ARCHITECTURE}/zlib": [],
            f"/sw/TARS/{TEST_ARCHITECTURE}/ROOT": [],
        }.get(directory, DEFAULT)
        os.environ["BITS_NO_ANALYTICS"] = "1"

        mock_parser = MagicMock()
        args = Namespace(
            remoteStore="",
            writeStore="",
            referenceSources="/sw/MIRROR",
            docker=False,
            dockerImage=None,
            docker_extra_args=["--network=host"],
            containerWorkDir=False,
            architecture=TEST_ARCHITECTURE,
            workDir="/sw",
            pkgname=["root"],
            configDir="/alidist",
            disable=[],
            force_rebuild=[],
            defaults=["release"],
            jobs=2,
            annotate={},
            preferSystem=[],
            noSystem=None,
            debug=True,
            dryRun=False,
            # This fixture pins the legacy (pre-modules) build hashes, so run in
            # legacy mode; the from-modules default is hash-tested separately.
            initdotshFromModules=False,
            aggressiveCleanup=False,
            environment=[],
            autoCleanup=False,
            noDevel=[],
            onlyDeps=False,
            fetchRepos=False,
            forceTracked=False,
            plugin="legacy",
            builders=1,
            resources=None,
            resourceMonitoring=False,
            makeflow=False,
            # Explicitly disable features whose mocking would require additional
            # filesystem or network setup.
            storeIntegrity=False,   # no ledger reads/writes
            provider_policy={},     # no provider position overrides
        )

        def mkcall(args):
            cmd, directory, check = args
            return call(list(cmd), directory=directory, check=check, prompt=False)

        common_calls = [
            call(("rev-parse", "HEAD"), args.configDir),
            mkcall(GIT_CLONE_REF_ZLIB_ARGS),
            call(["ls-remote", "--heads", "--tags", args.referenceSources + "/zlib"],
                 directory=".", check=False, prompt=False),
            call(["ls-remote", "--heads", "--tags", args.referenceSources + "/root"],
                 directory=".", check=False, prompt=False),
        ]

        mock_git_git.reset_mock()
        mock_debug.reset_mock()
        mock_warning.reset_mock()
        doBuild(args, mock_parser)
        mock_warning.assert_called_with("%s.sh contains a recipe, which will be ignored", "defaults-release")
        mock_debug.assert_called_with("Everything done")
        # After this run, .build-hash files will be simulated to exist
        # already, so sw/SOURCES repos must only be checked out on this run.
        mock_git_git.assert_has_calls(common_calls + [
            mkcall(GIT_CLONE_SRC_ZLIB_ARGS),
            mkcall(GIT_SET_URL_ZLIB_ARGS),
            mkcall(GIT_CHECKOUT_ZLIB_ARGS),
            mkcall(GIT_CLONE_SRC_ROOT_ARGS),
            mkcall(GIT_SET_URL_ROOT_ARGS),
            mkcall(GIT_CHECKOUT_ROOT_ARGS),
        ], any_order=True)
        self.assertEqual(mock_git_git.call_count, len(common_calls) + 6)

        # Force fetching repos
        mock_git_git.reset_mock()
        mock_debug.reset_mock()
        mock_warning.reset_mock()
        args.fetchRepos = True
        doBuild(args, mock_parser)
        mock_warning.assert_called_with("%s.sh contains a recipe, which will be ignored", "defaults-release")
        mock_debug.assert_called_with("Everything done")
        # assert_any_call, not assert_called_with: the revision counter's gap-fill
        # (_store_revision_records) legitimately lists the content-object hash dir
        # afterwards, so the version-link scan is no longer the LAST listdir call.
        mock_listdir.assert_any_call(f"/sw/TARS/{TEST_ARCHITECTURE}/ROOT")
        # We can't compare directly against the list of calls here as they
        # might happen in any order.
        mock_git_git.assert_has_calls(common_calls + [
            mkcall(GIT_FETCH_REF_ROOT_ARGS),
        ], any_order=True)
        self.assertEqual(mock_git_git.call_count, len(common_calls) + 1)

    def setup_spec(self, script):
        """Parse the alidist recipe in SCRIPT and return its spec."""
        err, spec, recipe = parseRecipe(lambda: script)
        self.assertIsNone(err)
        spec["recipe"] = "" if spec["package"].startswith("defaults-") else recipe.strip("\n")
        spec.setdefault("tag", spec["version"])
        spec["tag"] = resolve_tag(spec)
        return spec

    def test_hashing(self) -> None:
        """Check that the hashes assigned to packages remain constant."""
        default = self.setup_spec(TEST_DEFAULT_RELEASE)
        zlib = self.setup_spec(TEST_ZLIB_RECIPE)
        root = self.setup_spec(TEST_ROOT_RECIPE)
        extra = self.setup_spec(TEST_EXTRA_RECIPE)
        default["commit_hash"] = "0"
        for spec, refs in ((zlib, TEST_ZLIB_GIT_REFS),
                           (root, TEST_ROOT_GIT_REFS),
                           (extra, TEST_EXTRA_GIT_REFS)):
            spec.setdefault("requires", []).append(default["package"])
            spec["scm_refs"] = {ref: hash for hash, _, ref in (
                line.partition("\t") for line in refs.splitlines()
            )}
            try:
                spec["commit_hash"] = spec["scm_refs"]["refs/tags/" + spec["tag"]]
            except KeyError:
                spec["commit_hash"] = spec["scm_refs"]["refs/heads/" + spec["tag"]]
        specs = {pkg["package"]: pkg for pkg in (default, zlib, root, extra)}
        for spec in specs.values():
            spec["is_devel_pkg"] = False

        storeHashes("defaults-release", specs, considerRelocation=False)
        default["hash"] = default["remote_revision_hash"]
        self.assertEqual(default["hash"], TEST_DEFAULT_RELEASE_BUILD_HASH)
        self.assertEqual(default["remote_hashes"], [TEST_DEFAULT_RELEASE_BUILD_HASH])

        storeHashes("zlib", specs, considerRelocation=False)
        zlib["hash"] = zlib["local_revision_hash"]
        self.assertEqual(zlib["hash"], TEST_ZLIB_BUILD_HASH)
        self.assertEqual(zlib["local_hashes"], [TEST_ZLIB_BUILD_HASH])

        storeHashes("ROOT", specs, considerRelocation=False)
        root["hash"] = root["local_revision_hash"]
        self.assertEqual(root["hash"], TEST_ROOT_BUILD_HASH)
        # Equivalent "commit hashes": "f7b336611753f1f4aaa94222b0d620748ae230c0"
        # (head of v6-08-00-patches and commit of test-tag), and "test-tag".
        self.assertEqual(len(root["local_hashes"]), 2)
        self.assertEqual(root["local_hashes"][0], TEST_ROOT_BUILD_HASH)

        storeHashes("Extra", specs, considerRelocation=False)
        extra["hash"] = extra["local_revision_hash"]
        self.assertEqual(extra["hash"], TEST_EXTRA_BUILD_HASH)
        # Equivalent "commit hashes": "v1", "v2", "ba22".
        self.assertEqual(len(extra["local_hashes"]), 3)
        self.assertEqual(len(extra["remote_hashes"]), 3)
        self.assertEqual(extra["local_hashes"][0], TEST_EXTRA_BUILD_HASH)

    def test_untracked_requires_does_not_affect_consumer_hash(self) -> None:
        """A dependency under untracked_requires is linked (stays in `requires`)
        but excluded from the consumer's identity hash, so editing it does not
        invalidate the consumer. deps_hash still reflects it (incremental signal).
        """
        def consumer_hashes(untracked, dep_hash):
            # Fresh specs each call (storeHashes memoises onto the spec).
            specs = {
                "D": {"package": "D", "hash": dep_hash},
                "E": {"package": "E", "hash": "eeee"},
                "C": {"package": "C", "recipe": "", "version": "1",
                      "commit_hash": "0", "is_devel_pkg": False,
                      "requires": ["D", "E"],
                      "untracked_requires": ["D"] if untracked else []},
            }
            storeHashes("C", specs, considerRelocation=False)
            return specs["C"]["remote_revision_hash"], specs["C"]["deps_hash"]

        # Tracked: changing D's hash changes the consumer's identity hash.
        h_aaaa, _ = consumer_hashes(untracked=False, dep_hash="aaaa")
        h_bbbb, _ = consumer_hashes(untracked=False, dep_hash="bbbb")
        self.assertNotEqual(h_aaaa, h_bbbb)

        # Untracked: D's hash change leaves the consumer's identity UNCHANGED …
        u_aaaa, udh_aaaa = consumer_hashes(untracked=True, dep_hash="aaaa")
        u_bbbb, udh_bbbb = consumer_hashes(untracked=True, dep_hash="bbbb")
        self.assertEqual(u_aaaa, u_bbbb)
        # … while deps_hash still differs, so a devel build rebuilds incrementally.
        self.assertNotEqual(udh_aaaa, udh_bbbb)
        # Marking D untracked is itself a distinct (but stable) identity.
        self.assertNotEqual(h_aaaa, u_aaaa)

    def test_initdotsh_from_modules_is_a_hashed_input(self) -> None:
        """--initdotsh-from-modules must fold into the hash so the mode change is
        reproducible (a distinct identity), while leaving off-state hashes
        byte-identical to today (the aliBuild simple case).

        The flag is published through the defaults-release env
        (BITS_INITDOTSH_FROM_MODULES=1), which every package depends on — so a
        change to it flows into every package's hash. This guards that wiring.
        """
        def _default_hash(mode_on):
            d = self.setup_spec(TEST_DEFAULT_RELEASE)
            d["commit_hash"] = "0"
            d["is_devel_pkg"] = False
            if mode_on:
                d.setdefault("env", OrderedDict())["BITS_INITDOTSH_FROM_MODULES"] = "1"
            specs = {d["package"]: d}
            storeHashes("defaults-release", specs, considerRelocation=False)
            return d["remote_revision_hash"]

        # Off: byte-identical to the pinned golden hash (no rebuild for anyone).
        self.assertEqual(_default_hash(False), TEST_DEFAULT_RELEASE_BUILD_HASH)
        # On: a distinct identity (separate artifact tree), and deterministic.
        on = _default_hash(True)
        self.assertNotEqual(on, TEST_DEFAULT_RELEASE_BUILD_HASH)
        self.assertEqual(on, _default_hash(True))

    def test_initdotsh(self) -> None:
        """Sanity-check the generated init.sh for a few variables."""
        specs = {
            # Add some attributes that are normally set by doBuild(), but
            # required by generate_initdotsh().
            spec["package"]: dict(spec, revision="1", commit_hash="424242", hash="010101")
            for spec in map(self.setup_spec, (
                    TEST_DEFAULT_RELEASE,
                    TEST_ZLIB_RECIPE,
                    TEST_ROOT_RECIPE,
                    TEST_EXTRA_RECIPE,
            ))
        }

        setup_initdotsh = generate_initdotsh("ROOT", specs, "slc7_x86-64", post_build=False)
        complete_initdotsh = generate_initdotsh("ROOT", specs, "slc7_x86-64", post_build=True)

        # We only generate init.sh for ROOT, so Extra should not appear at all.
        self.assertNotIn("Extra", setup_initdotsh)
        self.assertNotIn("Extra", complete_initdotsh)

        # Dependencies must be loaded both for this build and for subsequent ones.
        self.assertIn('. "$WORK_DIR/$BITS_ARCH_PREFIX"/zlib/v1.3.1-1/etc/profile.d/init.sh', setup_initdotsh)
        self.assertIn('. "$WORK_DIR/$BITS_ARCH_PREFIX"/zlib/v1.3.1-1/etc/profile.d/init.sh', complete_initdotsh)

        # ROOT-specific variables must not be set during ROOT's build yet...
        self.assertNotIn("export ROOT_VERSION=", setup_initdotsh)
        self.assertNotIn("export ROOT_TEST_1=", setup_initdotsh)
        self.assertNotIn("export APPEND_ROOT_1=", setup_initdotsh)
        self.assertNotIn("export PREPEND_ROOT_1=", setup_initdotsh)

        # ...but they must be set once ROOT's build has completed.
        self.assertIn("export ROOT_VERSION=v6-08-30", complete_initdotsh)
        self.assertIn('export ROOT_TEST_1="root test 1"', complete_initdotsh)
        self.assertIn("export APPEND_ROOT_1=", complete_initdotsh)
        self.assertIn("export PREPEND_ROOT_1=", complete_initdotsh)

    def test_initdotsh_from_modules(self) -> None:
        """--initdotsh-from-modules adds the modulefile-equivalent dev env
        (<PKG>_INCLUDE_DIR, PYTHONPATH site-packages) to the package's own
        post-build section, guarded on existence, and changes nothing when off."""
        specs = {
            spec["package"]: dict(spec, revision="1", commit_hash="424242", hash="010101")
            for spec in map(self.setup_spec, (
                    TEST_DEFAULT_RELEASE, TEST_ZLIB_RECIPE,
                    TEST_ROOT_RECIPE, TEST_EXTRA_RECIPE))
        }

        # Off-state must be byte-identical to the unflagged generator.
        base = generate_initdotsh("ROOT", specs, "slc7_x86-64", post_build=True)
        off = generate_initdotsh("ROOT", specs, "slc7_x86-64", post_build=True,
                                 from_modules=False)
        self.assertEqual(base, off)
        self.assertNotIn("_INCLUDE_DIR", off)
        self.assertNotIn("CMAKE_PREFIX_PATH", off)   # legacy: owned by CMakeRecipe

        on = generate_initdotsh("ROOT", specs, "slc7_x86-64", post_build=True,
                                from_modules=True)
        # Guarded include dir + site-packages glob, keyed off the package root.
        self.assertIn('export ROOT_INCLUDE_DIR="${ROOT_ROOT}/include"', on)
        # CMAKE_PREFIX_PATH as a ':'-separated env var (CMake reads it natively).
        self.assertIn('export CMAKE_PREFIX_PATH="${ROOT_ROOT}', on)
        self.assertIn('${ROOT_ROOT}"/lib/python*/site-packages', on)
        self.assertIn("PYTHONPATH=", on)
        # Guarded so it is a no-op when the dir is absent.
        self.assertIn('[ ! -d "${ROOT_ROOT}/include" ]', on)
        # The setup (pre-build, deps-only) pass adds nothing self-referential.
        setup_on = generate_initdotsh("ROOT", specs, "slc7_x86-64", post_build=False,
                                      from_modules=True)
        self.assertNotIn("ROOT_INCLUDE_DIR", setup_on)

    def test_initdotsh_from_modules_shared_dependency(self) -> None:
        """A shared (architecture: shared) dependency must be sourced from the
        literal `$WORK_DIR/shared` tree, never `$BITS_ARCH_PREFIX`, including in
        --initdotsh-from-modules mode; and a shared package's own _ROOT (and the
        from-modules INCLUDE_DIR keyed off it) must use that same literal tree."""
        base = {"revision": "1", "hash": "h", "commit_hash": "c"}
        specs = {
            "App":       dict(base, package="App", version="1.0",
                              requires=["libshared", "libarch"]),
            "libshared": dict(base, package="libshared", version="2.3",
                              architecture="shared", requires=[]),
            "libarch":   dict(base, package="libarch", version="4.5", requires=[]),
        }

        out = generate_initdotsh("App", specs, "slc7_x86-64",
                                 post_build=True, from_modules=True)
        # Shared dep: literal `shared/` tree, NOT the relocatable arch variable.
        self.assertIn('. "$WORK_DIR/shared"/libshared/2.3-1/etc/profile.d/init.sh', out)
        self.assertNotIn('$BITS_ARCH_PREFIX"/libshared', out)
        # Arch-specific dep: still the relocatable arch variable, as before.
        self.assertIn('. "$WORK_DIR/$BITS_ARCH_PREFIX"/libarch/4.5-1/etc/profile.d/init.sh', out)

        # A shared package's own _ROOT, and the from-modules include dir keyed off
        # it, use the literal tree — never $BITS_ARCH_PREFIX.
        shared = generate_initdotsh("libshared", specs, "slc7_x86-64",
                                    post_build=True, from_modules=True)
        self.assertIn('export LIBSHARED_ROOT="$WORK_DIR/shared"/libshared/2.3-1', shared)
        self.assertIn('export LIBSHARED_INCLUDE_DIR="${LIBSHARED_ROOT}/include"', shared)
        self.assertNotIn('$BITS_ARCH_PREFIX"/libshared', shared)

    def test_initdotsh_reuse_modules(self) -> None:
        """A dependency satisfied from a reused CVMFS release is set up via its
        overlay modulefile (module use/load); a locally-built dependency keeps its
        init.sh sourcing — so a plain/legacy package can consume a module-reused
        dep. Dormant (byte-identical) when no reuse_modulepath is passed."""
        base = {"revision": "1", "hash": "h", "commit_hash": "c"}
        specs = {
            "App":   dict(base, package="App", version="1.0",
                          requires=["CMake", "Boost"]),
            "CMake": dict(base, package="CMake", version="3.30.6", requires=[]),
            "Boost": dict(base, package="Boost", version="1.90.0", requires=[]),
        }
        # Baseline: no reuse base -> both deps sourced from the local tree.
        baseline = generate_initdotsh("App", specs, "slc7_x86-64", post_build=False)
        self.assertNotIn("/cvmfs", baseline)
        self.assertIn('"$WORK_DIR/$BITS_ARCH_PREFIX"/CMake/3.30.6-1/etc/profile.d/init.sh',
                      baseline)

        # Mark CMake reused-from-CVMFS; Boost stays locally built.
        specs["CMake"]["reuse_module_id"] = "CMake/3.30.6-1"
        out = generate_initdotsh("App", specs, "slc7_x86-64", post_build=False,
                                 reuse_cvmfs_base="/cvmfs/x/Packages")
        # CMake sourced from its DEPLOYED init.sh on CVMFS, under a WORK_DIR
        # override. BITS_ARCH_PREFIX="." (non-null) survives the deployed init.sh's
        # `:=` default, which "" would not.
        self.assertIn('WORK_DIR="/cvmfs/x/Packages"; BITS_ARCH_PREFIX="."', out)
        self.assertIn('. "/cvmfs/x/Packages/CMake/3.30.6-1/etc/profile.d/init.sh"', out)
        # ...and NOT from the local tree; Boost (built) still is.
        self.assertNotIn('"$WORK_DIR/$BITS_ARCH_PREFIX"/CMake/3.30.6-1/etc/profile.d/init.sh',
                         out)
        self.assertIn('"$WORK_DIR/$BITS_ARCH_PREFIX"/Boost/1.90.0-1/etc/profile.d/init.sh',
                      out)

        # Dormant safety: marker set but no reuse base -> identical to baseline.
        dormant = generate_initdotsh("App", specs, "slc7_x86-64", post_build=False)
        self.assertEqual(dormant, baseline)

    def test_initdotsh_reuse_orders_prereq_before_reused(self) -> None:
        """A local prerequisite of a reused dep must be sourced BEFORE it: the
        reused dep's deployed init.sh transitively sources that prereq (guarded on
        its _REVISION) and would look on CVMFS, where a local-only build is absent.
        Emitting in topological order sets the prereq's _REVISION first so the
        guard skips the CVMFS re-source."""
        base = {"revision": "1", "hash": "h", "commit_hash": "c"}
        specs = {
            "App":   dict(base, package="App", version="1.0",
                          requires=["CMake", "Tools"]),
            # CMake (reused) declares Tools as a dependency, so its deployed
            # init.sh sources Tools; Tools itself is built locally.
            "CMake": dict(base, package="CMake", version="3.30.6",
                          requires=["Tools"]),
            "Tools": dict(base, package="Tools", version="0.0.32", requires=[]),
        }
        specs["CMake"]["reuse_module_id"] = "CMake/3.30.6-1"
        out = generate_initdotsh("App", specs, "slc7_x86-64", post_build=False,
                                 reuse_cvmfs_base="/cvmfs/x/Packages")
        tools_line = '"$WORK_DIR/$BITS_ARCH_PREFIX"/Tools/0.0.32-1/etc/profile.d/init.sh'
        cmake_line = '. "/cvmfs/x/Packages/CMake/3.30.6-1/etc/profile.d/init.sh"'
        self.assertIn(tools_line, out)
        self.assertIn(cmake_line, out)
        self.assertLess(out.index(tools_line), out.index(cmake_line),
                        "local prerequisite Tools must be sourced before reused CMake")


if __name__ == '__main__':
    unittest.main()
