from argparse import Namespace
import configparser
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
        rcFile="bits.rc",
        appendRc=False,
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
    """doInitConfig() writes the correct bits.rc content."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._rc = os.path.join(self._tmpdir, "bits.rc")

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _read_rc(self):
        cfg = configparser.ConfigParser()
        cfg.read(self._rc)
        return dict(cfg["bits"]) if "bits" in cfg else {}

    def test_writes_remote_store(self):
        args = _cfg_args(
            initRemoteStore="https://store.example.com",
            rcFile=self._rc,
            _init_explicit={"remote_store"},
        )
        doInitConfig(args)
        self.assertEqual(self._read_rc().get("remote_store"), "https://store.example.com")

    def test_writes_write_store(self):
        args = _cfg_args(
            initWriteStore="b3://mybucket/store",
            rcFile=self._rc,
            _init_explicit={"write_store"},
        )
        doInitConfig(args)
        self.assertEqual(self._read_rc().get("write_store"), "b3://mybucket/store")

    def test_writes_providers(self):
        args = _cfg_args(
            providers="https://github.com/myorg/bits-providers",
            rcFile=self._rc,
            _init_explicit={"providers"},
        )
        doInitConfig(args)
        self.assertEqual(self._read_rc().get("providers"),
                         "https://github.com/myorg/bits-providers")

    def test_writes_organisation(self):
        args = _cfg_args(
            organisation="MYORG",
            rcFile=self._rc,
            _init_explicit={"organisation"},
        )
        doInitConfig(args)
        self.assertEqual(self._read_rc().get("organisation"), "MYORG")

    def test_writes_work_dir_via_short_flag(self):
        args = _cfg_args(
            workDir="/opt/sw",
            rcFile=self._rc,
            _init_explicit={"w"},           # user passed -w
        )
        doInitConfig(args)
        self.assertEqual(self._read_rc().get("work_dir"), "/opt/sw")

    def test_writes_architecture_via_short_flag(self):
        args = _cfg_args(
            architecture="ubuntu2204_x86-64",
            rcFile=self._rc,
            _init_explicit={"a"},
        )
        doInitConfig(args)
        self.assertEqual(self._read_rc().get("architecture"), "ubuntu2204_x86-64")

    def test_writes_defaults_list_as_double_colon(self):
        args = _cfg_args(
            defaults=["release", "myproject"],
            rcFile=self._rc,
            _init_explicit={"defaults"},
        )
        doInitConfig(args)
        self.assertEqual(self._read_rc().get("defaults"), "release::myproject")

    def test_does_not_write_unspecified_keys(self):
        """Only explicitly requested keys must appear in bits.rc."""
        args = _cfg_args(
            initRemoteStore="https://store.example.com",
            workDir="/opt/sw",              # NOT in explicit flags
            rcFile=self._rc,
            _init_explicit={"remote_store"},
        )
        doInitConfig(args)
        rc = self._read_rc()
        self.assertIn("remote_store", rc)
        self.assertNotIn("work_dir", rc)

    def test_multiple_keys_in_one_pass(self):
        args = _cfg_args(
            initRemoteStore="https://store.example.com",
            initWriteStore="b3://mybucket",
            organisation="MYORG",
            rcFile=self._rc,
            _init_explicit={"remote_store", "write_store", "organisation"},
        )
        doInitConfig(args)
        rc = self._read_rc()
        self.assertEqual(rc["remote_store"], "https://store.example.com")
        self.assertEqual(rc["write_store"], "b3://mybucket")
        self.assertEqual(rc["organisation"], "MYORG")

    def test_append_preserves_existing_keys(self):
        """--append must keep existing bits.rc entries that are not overridden."""
        # Write initial file with providers key
        initial = configparser.ConfigParser()
        initial.add_section("bits")
        initial.set("bits", "providers", "https://github.com/org/providers")
        with open(self._rc, "w") as fh:
            initial.write(fh)

        args = _cfg_args(
            initRemoteStore="https://store.example.com",
            rcFile=self._rc,
            appendRc=True,
            _init_explicit={"remote_store"},
        )
        doInitConfig(args)
        rc = self._read_rc()
        # New key written
        self.assertEqual(rc["remote_store"], "https://store.example.com")
        # Existing key preserved
        self.assertEqual(rc["providers"], "https://github.com/org/providers")

    def test_append_overwrites_changed_key(self):
        """--append must update an existing key when the user re-specifies it."""
        initial = configparser.ConfigParser()
        initial.add_section("bits")
        initial.set("bits", "remote_store", "https://old-store.example.com")
        with open(self._rc, "w") as fh:
            initial.write(fh)

        args = _cfg_args(
            initRemoteStore="https://new-store.example.com",
            rcFile=self._rc,
            appendRc=True,
            _init_explicit={"remote_store"},
        )
        doInitConfig(args)
        rc = self._read_rc()
        self.assertEqual(rc["remote_store"], "https://new-store.example.com")

    def test_no_flags_does_not_write_file(self):
        """With no explicit flags doInitConfig must not create bits.rc."""
        args = _cfg_args(rcFile=self._rc, _init_explicit=set())
        doInitConfig(args)
        self.assertFalse(os.path.exists(self._rc))

    def test_dry_run_does_not_write_file(self):
        """--dry-run must print the config without touching the file system."""
        args = _cfg_args(
            initRemoteStore="https://store.example.com",
            rcFile=self._rc,
            dryRun=True,
            _init_explicit={"remote_store"},
        )
        with patch("bits_helpers.init.info") as mock_info:
            doInitConfig(args)
        self.assertFalse(os.path.exists(self._rc))
        # info() should have been called with the INI text
        self.assertTrue(mock_info.called)
        printed = " ".join(str(a) for call in mock_info.call_args_list for a in call[0])
        self.assertIn("remote_store", printed)

    def test_fresh_write_overwrites_existing(self):
        """Without --append, an existing bits.rc is replaced entirely."""
        initial = configparser.ConfigParser()
        initial.add_section("bits")
        initial.set("bits", "providers", "https://old-providers")
        with open(self._rc, "w") as fh:
            initial.write(fh)

        args = _cfg_args(
            initWriteStore="b3://mybucket",
            rcFile=self._rc,
            appendRc=False,
            _init_explicit={"write_store"},
        )
        doInitConfig(args)
        rc = self._read_rc()
        self.assertIn("write_store", rc)
        self.assertNotIn("providers", rc)   # old key gone


class BitsRcDefaultsAppliedTest(unittest.TestCase):
    """Verify that bits.rc values become argparse defaults via set_defaults()."""

    def _parse(self, argv, rc_content=""):
        """Parse argv with a bits.rc in a temp dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            rc_path = os.path.join(tmpdir, "bits.rc")
            if rc_content:
                with open(rc_path, "w") as fh:
                    fh.write(rc_content)
            old_cwd = os.getcwd()
            try:
                os.chdir(tmpdir)
                import sys
                old_argv = sys.argv[:]
                sys.argv = ["bits"] + argv
                try:
                    from bits_helpers.args import doParseArgs
                    args, _ = doParseArgs()
                    return args
                finally:
                    sys.argv = old_argv
            finally:
                os.chdir(old_cwd)

    def test_remote_store_from_rc(self):
        """bits.rc remote_store must set the default for 'bits build'."""
        rc = "[bits]\nremote_store = https://rc-store.example.com\n"
        with patch("bits_helpers.args.cleanup_git_log"):
            args = self._parse(["build", "zlib", "--force-unknown-architecture"], rc)
        self.assertEqual(args.remoteStore, "https://rc-store.example.com")

    def test_cli_overrides_rc(self):
        """An explicit CLI --remote-store must win over the bits.rc value."""
        rc = "[bits]\nremote_store = https://rc-store.example.com\n"
        with patch("bits_helpers.args.cleanup_git_log"):
            args = self._parse(
                ["build", "zlib",
                 "--remote-store", "https://cli-store.example.com",
                 "--force-unknown-architecture"],
                rc,
            )
        self.assertEqual(args.remoteStore, "https://cli-store.example.com")

    def test_no_rc_uses_hardcoded_default(self):
        """Without bits.rc the original hardcoded default must be used."""
        with patch("bits_helpers.args.cleanup_git_log"):
            args = self._parse(["build", "zlib", "--force-unknown-architecture"])
        # Default is "" (empty string, no remote store)
        self.assertEqual(args.remoteStore, "")


if __name__ == '__main__':
    unittest.main()
