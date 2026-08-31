# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

from argparse import Namespace
import os
import os.path as path
import tempfile
import unittest
from unittest.mock import call, patch, MagicMock  # In Python 3, mock is built-in
from io import StringIO
from collections import OrderedDict

from bits_helpers.init import (
    doInit,
    doInitConfig,
    parsePackagesDefinition,
    _explicit_rc_keys,
)


def dummy_exists(x):
  return {
      '/sw/MIRROR/aliroot': True,
  }.get(x, False)


CLONE_EVERYTHING = [
    call(["clone", "--origin", "upstream", "https://github.com/alisw/alidist",
          "-b", "master", "/alidist"]),
    call(["clone", "--origin", "upstream", "https://github.com/alisw/AliRoot",
          "--reference", "/sw/MIRROR/aliroot", "-b", "v5-08-00", "./AliRoot"]),
    call(("remote", "set-url", "--push", "upstream",
          "https://github.com/alisw/AliRoot"), directory="./AliRoot"),
]


class InitTestCase(unittest.TestCase):
    def test_packageDefinition(self) -> None:
      self.assertEqual(parsePackagesDefinition("AliRoot@v5-08-16,AliPhysics@v5-08-16-01"),
                       [{'ver': 'v5-08-16', 'name': 'AliRoot'},
                        {'ver': 'v5-08-16-01', 'name': 'AliPhysics'}])
      self.assertEqual(parsePackagesDefinition("AliRoot,AliPhysics@v5-08-16-01"),
                       [{'ver': '', 'name': 'AliRoot'},
                        {'ver': 'v5-08-16-01', 'name': 'AliPhysics'}])

    @patch.dict(os.environ, {"BITS_BRANDING": "aliBuild"})
    @patch("bits_helpers.init.banner")
    @patch("bits_helpers.init.git")
    @patch("bits_helpers.init.path")
    def test_alibuild_init_no_package_checks_out_recipes(self, mock_path, mock_git, _banner) -> None:
        # `aliBuild init` with no PACKAGE clones the alidist recipes and exits.
        mock_path.exists.return_value = False  # configDir not present yet
        args = Namespace(pkgname="", configDir="alidist", dryRun=False,
                         dist={"repo": "alisw/alidist", "ver": "master"})
        doInit(args)
        mock_git.assert_called_once_with(
            ["clone", "--origin", "upstream", "https://github.com/alisw/alidist",
             "-b", "master", "alidist"])

    @patch("bits_helpers.init.banner")
    @patch("bits_helpers.init.git")
    @patch("bits_helpers.init.path")
    @patch("bits_helpers.repo_provider.resolve_registry_repo")
    def test_init_group_bits_checks_out_repo(self, mock_resolve, mock_path, mock_git, _banner) -> None:
        # `bits init alice.bits` resolves the group in the registry and clones it.
        mock_resolve.return_value = {"source": "bitsorg/alice.bits", "tag": "main"}
        mock_path.exists.return_value = False
        args = Namespace(pkgname="alice.bits", develPrefix=".", dryRun=False, workDir="sw")
        doInit(args)
        dest = path.join(".", "alice.bits")
        mock_resolve.assert_called_once()
        mock_git.assert_called_once_with(
            ["clone", "--origin", "upstream",
             "https://github.com/bitsorg/alice.bits", "-b", "main", dest])

    @patch("bits_helpers.init.git")
    @patch("bits_helpers.init.os")
    @patch("bits_helpers.init.path")
    @patch("bits_helpers.init.getPackageList")
    @patch("bits_helpers.init.parseDefaults")
    @patch("bits_helpers.repo_provider.fetch_repo_providers_iteratively")
    @patch("bits_helpers.repo_provider.load_always_on_providers")
    @patch("bits_helpers.repo_provider.resolve_registry_repo")
    def test_package_init_seeds_group_requires(self, mock_resolve, mock_always, mock_fetch,
                                               mock_parsedefaults, mock_getpkglist,
                                               mock_path, _mock_os, _mock_git) -> None:
        # `bits init -c alice.bits ROOT`: provider discovery is seeded with the
        # checked-out group's registry requires so a package in a required
        # provider repo (alidist.bits) can be found.
        mock_path.exists.return_value = True            # configDir exists
        mock_parsedefaults.return_value = (None, {}, {}, {})
        mock_resolve.return_value = {"requires": ["alidist.bits"]}
        mock_getpkglist.side_effect = SystemExit(0)     # stop after the provider phase
        args = Namespace(pkgname="ROOT", configDir="alice.bits",
                         bits_providers="https://example/p",
                         dist={"repo": "alisw/alidist", "ver": "master"},
                         develPrefix=".", referenceSources="/sw/MIRROR",
                         defaults=["release"], architecture="", dryRun=False,
                         workDir="sw", fetchRepos=False)
        with self.assertRaises(SystemExit):
            doInit(args)
        seeded = mock_fetch.call_args.kwargs["packages"]
        self.assertIn("ROOT", seeded)
        self.assertIn("alidist.bits", seeded)

    @patch.dict(os.environ, {"BITS_BRANDING": ""})
    @patch("bits_helpers.init.doInitConfig")
    def test_plain_bits_init_no_package_writes_config(self, mock_cfg) -> None:
        # Plain `bits init` (not aliBuild) keeps the settings-writing behaviour.
        args = Namespace(pkgname="", configDir="alidist", dryRun=False, dist={})
        doInit(args)
        mock_cfg.assert_called_once()

    @patch("bits_helpers.init.info")
    @patch("bits_helpers.init.path")
    @patch("bits_helpers.init.os")
    def test_doDryRunInit(self, mock_os, mock_path,  mock_info) -> None:
      fake_dist = {"repo": "alisw/alidist", "ver": "master"}
      args = Namespace(
        develPrefix = ".",
        configDir = "/alidist",
        pkgname = "zlib,AliRoot@v5-08-00",
        referenceSources = "/sw/MIRROR",
        dist = fake_dist,
        defaults = ["release"],
        dryRun = True,
        fetchRepos = False,
        architecture = "slc7_x86-64",
        environment = {},
      )
      self.assertRaises(SystemExit, doInit, args)
      self.assertEqual(mock_info.mock_calls, [call('This will initialise local checkouts for %s\n--dry-run / -n specified. Doing nothing.', 'zlib,AliRoot')])

    @patch("bits_helpers.init.banner")
    @patch("bits_helpers.init.info")
    @patch("bits_helpers.init.path")
    @patch("bits_helpers.utilities.exists")
    @patch("bits_helpers.init.os")
    @patch("bits_helpers.init.git")
    @patch("bits_helpers.init.updateReferenceRepoSpec")
    @patch("bits_helpers.utilities.open")
    @patch("bits_helpers.init.readDefaults")
    def test_doRealInit(self, mock_read_defaults, mock_open, mock_update_reference, mock_git, mock_os, mock_exists, mock_path,  mock_info, mock_banner) -> None:
      fake_dist = {"repo": "alisw/alidist", "ver": "master"}
      mock_open.side_effect = lambda x: {
        "/alidist/defaults-release.sh": StringIO("package: defaults-release\nversion: v1\n---"),
        "/alidist/aliroot.sh": StringIO("package: AliRoot\nversion: master\nsource: https://github.com/alisw/AliRoot\n---")
      }[x]
      mock_exists.side_effect = lambda x: {
        "/sw/MIRROR/aliroot": True,
        "/alidist/defaults-release.sh": True,
        "/alidist/aliroot.sh": True,
      }.get(x, False)
      mock_git.return_value = ""
      mock_path.exists.side_effect = dummy_exists
      mock_os.mkdir.return_value = None
      mock_path.join.side_effect = path.join
      mock_read_defaults.return_value = (OrderedDict({"package": "defaults-release", "disable": []}), "")
      args = Namespace(
        develPrefix = ".",
        configDir = "/alidist",
        pkgname = "AliRoot@v5-08-00",
        referenceSources = "/sw/MIRROR",
        dist = fake_dist,
        defaults = ["release"],
        dryRun = False,
        fetchRepos = False,
        architecture = "slc7_x86-64",
        environment = {},
      )
      doInit(args)
      self.assertEqual(mock_git.mock_calls, CLONE_EVERYTHING)
      mock_path.exists.assert_has_calls([call('.'), call('/sw/MIRROR'), call('/alidist'), call('./AliRoot')])

      # Force fetch repos
      mock_git.reset_mock()
      mock_path.reset_mock()
      args.fetchRepos = True
      doInit(args)
      self.assertEqual(mock_git.mock_calls, CLONE_EVERYTHING)
      mock_path.exists.assert_has_calls([call('.'), call('/sw/MIRROR'), call('/alidist'), call('./AliRoot')])


def _cfg_args(**kwargs):
    """Build a minimal Namespace for doInitConfig tests."""
    defaults = dict(
        pkgname="",
        dryRun=False,
        providers=None,
        initRemoteStore=None,
        initWriteStore=None,
        organisation=None,
        workDir="sw",
        architecture="slc9_x86-64",
        defaults=["release"],
        configDir="alidist",
        referenceSources="sw/MIRROR",
        _init_explicit=set(),
    )
    defaults.update(kwargs)
    return Namespace(**defaults)


class ExplicitRcKeysTest(unittest.TestCase):
    """Unit tests for the _explicit_rc_keys() helper."""

    def test_long_flag_remote_store(self):
        keys = _explicit_rc_keys({"remote_store"})
        self.assertIn("remote_store", keys)

    def test_long_flag_write_store(self):
        keys = _explicit_rc_keys({"write_store"})
        self.assertIn("write_store", keys)

    def test_long_flag_providers(self):
        keys = _explicit_rc_keys({"providers"})
        self.assertIn("providers", keys)

    def test_short_flag_work_dir(self):
        keys = _explicit_rc_keys({"w"})
        self.assertIn("work_dir", keys)

    def test_short_flag_architecture(self):
        keys = _explicit_rc_keys({"a"})
        self.assertIn("architecture", keys)

    def test_short_flag_config_dir(self):
        keys = _explicit_rc_keys({"c"})
        self.assertIn("config_dir", keys)

    def test_unknown_flag_ignored(self):
        keys = _explicit_rc_keys({"foo_bar", "x"})
        self.assertEqual(keys, set())

    def test_multiple_flags(self):
        keys = _explicit_rc_keys({"remote_store", "write_store", "organisation"})
        self.assertGreaterEqual(keys, {"remote_store", "write_store", "organisation"})


class ConfigModeDispatchTest(unittest.TestCase):
    """doInit() dispatches to doInitConfig when no PACKAGE is given."""

    @patch("bits_helpers.init.doInitConfig")
    def test_no_package_calls_config(self, mock_cfg):
        """doInit with empty pkgname must delegate to doInitConfig."""
        args = _cfg_args(pkgname="", _init_explicit=set())
        doInit(args)
        mock_cfg.assert_called_once_with(args)

    @patch("bits_helpers.init.doInitConfig")
    def test_with_package_does_not_call_config(self, mock_cfg):
        """doInit with a PACKAGE name must NOT call doInitConfig."""
        # We patch everything that the clone path needs so it doesn't blow up.
        args = _cfg_args(
            pkgname="AliRoot",
            dist={"repo": "alisw/alidist", "ver": "master"},
            dryRun=True,
        )
        try:
            doInit(args)
        except SystemExit:
            pass
        mock_cfg.assert_not_called()


class ConfigModeWriteTest(unittest.TestCase):
    """doInitConfig() records a bits use (.bitsuse) profile."""

    def setUp(self):
        from bits_helpers import bits_use
        self._bu = bits_use
        self._cwd0 = os.getcwd()
        self._tmpdir = tempfile.mkdtemp()
        self._home = tempfile.mkdtemp()
        os.chdir(self._tmpdir)
        self._patch = patch.object(bits_use, "HOME_STORE",
                                   os.path.join(self._home, "use"))
        self._patch.start()

    def tearDown(self):
        import shutil
        self._patch.stop()
        os.chdir(self._cwd0)
        shutil.rmtree(self._tmpdir, ignore_errors=True)
        shutil.rmtree(self._home, ignore_errors=True)

    def _profile(self):
        return self._bu.read_all(self._bu._read_path())

    def test_remote_store_goes_to_build(self):
        doInitConfig(_cfg_args(initRemoteStore="https://store.example.com",
                               _init_explicit={"remote_store"}))
        self.assertEqual(self._profile().get("build"),
                         ["--remote-store", "https://store.example.com"])

    def test_write_store_goes_to_build(self):
        doInitConfig(_cfg_args(initWriteStore="b3://mybucket/store",
                               _init_explicit={"write_store"}))
        self.assertEqual(self._profile().get("build"),
                         ["--write-store", "b3://mybucket/store"])

    def test_architecture_goes_to_common(self):
        doInitConfig(_cfg_args(architecture="slc9_x86-64", _init_explicit={"a"}))
        self.assertEqual(self._profile().get("common"),
                         ["--architecture", "slc9_x86-64"])

    def test_work_dir_goes_to_build(self):
        doInitConfig(_cfg_args(workDir="/opt/sw", _init_explicit={"w"}))
        self.assertEqual(self._profile().get("build"), ["--work-dir", "/opt/sw"])

    def test_defaults_list_joined_with_double_colon(self):
        doInitConfig(_cfg_args(defaults=["release", "myproject"],
                               _init_explicit={"defaults"}))
        self.assertEqual(self._profile().get("build"),
                         ["--defaults", "release::myproject"])

    def test_only_explicit_keys_saved(self):
        doInitConfig(_cfg_args(initRemoteStore="https://store.example.com",
                               workDir="/opt/sw",
                               _init_explicit={"remote_store"}))
        build = self._profile().get("build", [])
        self.assertIn("--remote-store", build)
        self.assertNotIn("--work-dir", build)

    def test_common_and_build_together(self):
        doInitConfig(_cfg_args(architecture="slc9_x86-64",
                               initRemoteStore="https://store.example.com",
                               _init_explicit={"a", "remote_store"}))
        prof = self._profile()
        self.assertEqual(prof.get("common"), ["--architecture", "slc9_x86-64"])
        self.assertEqual(prof.get("build"),
                         ["--remote-store", "https://store.example.com"])

    def test_organisation_is_env_only_not_saved(self):
        doInitConfig(_cfg_args(organisation="MYORG", _init_explicit={"organisation"}))
        self.assertEqual(self._profile(), {})

    def test_providers_is_env_only_not_saved(self):
        doInitConfig(_cfg_args(providers="https://x/bits-providers",
                               _init_explicit={"providers"}))
        self.assertEqual(self._profile(), {})

    def test_dry_run_writes_nothing(self):
        doInitConfig(_cfg_args(initRemoteStore="https://x", dryRun=True,
                               _init_explicit={"remote_store"}))
        self.assertIsNone(self._bu._read_path())


if __name__ == '__main__':
    unittest.main()
