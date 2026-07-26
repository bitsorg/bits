# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

# Assuming you are using the mock library to ... mock things
from unittest import mock
from unittest.mock import patch

import bits_helpers.args
from bits_helpers.args import doParseArgs, matchValidArch, _host_online_cpus
import sys
import os
import os.path
import re

import unittest
import shlex

# Stable cpuset value injected by the mock in all docker-related tests.
_MOCK_CPUSET = "0-3"
_MOCK_CPUSET_ARG = "--cpuset-cpus=" + _MOCK_CPUSET

BUILD_MISSING_PKG_ERROR = "the following arguments are required: PACKAGE"
ANALYTICS_MISSING_STATE_ERROR = "the following arguments are required: state"

# A few errors we should handle, together with the expected result
ARCHITECTURE_ERROR = "Unknown / unsupported architecture: foo.\n\n.*"
PARSER_ERRORS = {
  "build --force-unknown-architecture": BUILD_MISSING_PKG_ERROR,
  "build --force-unknown-architecture zlib --foo": 'unrecognized arguments: --foo',
  "init --docker-image": 'unrecognized arguments: --docker-image',
  "builda --force-unknown-architecture zlib" : "argument action: invalid choice: 'builda'.*",
  "build --force-unknown-architecture zlib --no-system --always-prefer-system" : 'argument --always-prefer-system: not allowed with argument --no-system',
  "build zlib --architecture foo": ARCHITECTURE_ERROR,
  "build --force-unknown-architecture zlib --remote-store rsync://test1.local/::rw --write-store rsync://test2.local/::rw ": 'cannot specify ::rw and --write-store at the same time',
  "build zlib -a osx_x86-64 --docker-image foo": 'cannot use `-a osx_x86-64` and --docker',
  "build zlib -a slc7_x86-64 --annotate foobar": "--annotate takes arguments of the form PACKAGE=COMMENT",
  # "analytics": ANALYTICS_MISSING_STATE_ERROR
}

# A few valid archs
VALID_ARCHS = ["osx_x86-64", "slc7_x86-64", "slc8_x86-64"]
INVALID_ARCHS = ["osx_ppc64", "sl8_x86-64"]

class FakeExit(Exception):
  pass

CORRECT_BEHAVIOR = [
  ((), "build --force-unknown-architecture zlib"                                       , [("action", "build"), ("workDir", "sw"), ("referenceSources", "sw/MIRROR")]),
  ((), "init"                                                                          , [("action", "init"), ("workDir", "sw"), ("referenceSources", "sw/MIRROR")]),
  ((), "version"                                                                       , [("action", "version")]),
  ((), "clean"                                                                         , [("action", "clean"), ("workDir", "sw")]),
  ((), "build --force-unknown-architecture -j 10 zlib"                                 , [("action", "build"), ("jobs", 10), ("pkgname", ["zlib"])]),
  ((), "build --force-unknown-architecture -j 10 zlib --disable gcc --disable foo"     , [("disable", ["gcc", "foo"])]),
  ((), "build --force-unknown-architecture -j 10 zlib --disable gcc --disable foo,bar" , [("disable", ["gcc", "foo", "bar"])]),
  ((), "init zlib --dist master"                                                       , [("dist", {"repo": "alisw/alidist", "ver": "master"})]),
  ((), "init zlib --dist ktf/alidist@dev"                                              , [("dist", {"repo": "ktf/alidist", "ver": "dev"})]),
  # A remote/write store no longer forces --no-system: reuse is opportunistic and
  # eligible packages are still taken from the system (noSystem stays None).
  ((), "build --force-unknown-architecture zlib --remote-store rsync://test.local/"    , [("noSystem", None), ("remoteStore", "rsync://test.local/")]),
  ((), "build --force-unknown-architecture zlib --remote-store rsync://test.local/::rw", [("noSystem", None), ("remoteStore", "rsync://test.local/"), ("writeStore", "rsync://test.local/")]),
  ((), "build --force-unknown-architecture zlib --no-remote-store --remote-store rsync://test.local/", [("noSystem", None), ("remoteStore", "")]),
  ((), "build zlib --architecture slc7_x86-64"                                         , [("noSystem", None), ("preferSystem", False), ("remoteStore", "https://s3.cern.ch/swift/v1/alibuild-repo")]),
  ((), "build zlib --architecture ubuntu1804_x86-64"                                   , [("noSystem", None), ("preferSystem", False), ("remoteStore", "")]),
  ((), "build zlib -a slc7_x86-64"                                                     , [("docker", False), ("dockerImage", None), ("docker_extra_args", ["--network=host", _MOCK_CPUSET_ARG])]),
  ((), "build zlib -a slc7_x86-64 --docker-image registry.cern.ch/alisw/some-builder"  , [("docker", True), ("dockerImage", "registry.cern.ch/alisw/some-builder")]),
  ((), "build zlib -a slc7_x86-64 --docker"                                            , [("docker", True), ("dockerImage", "registry.cern.ch/alisw/slc7-builder")]),
  ((), "build zlib -a slc7_x86-64 --docker-extra-args=--foo"                           , [("docker", True), ("dockerImage", "registry.cern.ch/alisw/slc7-builder"), ("docker_extra_args", ["--foo", "--network=host", _MOCK_CPUSET_ARG])]),
  ((), "build zlib --devel-prefix -a slc7_x86-64 --docker"                             , [("docker", True), ("dockerImage", "registry.cern.ch/alisw/slc7-builder"), ("develPrefix", "%s-slc7_x86-64" % os.path.basename(os.getcwd()))]),
  ((), "build zlib --devel-prefix -a slc7_x86-64 --docker-image someimage"             , [("docker", True), ("dockerImage", "someimage"), ("develPrefix", "%s-slc7_x86-64" % os.path.basename(os.getcwd()))]),
  ((), "--debug build --force-unknown-architecture --defaults o2 O2"                   , [("debug", True), ("action",  "build"), ("defaults", ["release", "o2"]), ("pkgname", ["O2"])]),
  ((), "build --force-unknown-architecture --debug --defaults o2 O2"                   , [("debug", True), ("action",  "build"), ("force_rebuild", []), ("defaults", ["release", "o2"]), ("pkgname", ["O2"])]),
  ((), "build --force-unknown-architecture --force-rebuild O2 --force-rebuild O2Physics --defaults o2 O2Physics", [("action", "build"), ("force_rebuild", ["O2", "O2Physics"]), ("defaults", ["release", "o2"]), ("pkgname", ["O2Physics"])]),
  ((), "build --force-unknown-architecture --force-rebuild O2,O2Physics --defaults o2 O2Physics", [("action", "build"), ("force_rebuild", ["O2", "O2Physics"]), ("defaults", ["release", "o2"]), ("pkgname", ["O2Physics"])]),
  ((), "init -z test zlib"                                                             , [("configDir", "test/alidist")]),
  ((), "build --force-unknown-architecture -z test zlib"                               , [("configDir", ".")]),
  # ((), "analytics off"                                                                 , [("state", "off")]),
  # ((), "analytics on"                                                                  , [("state", "on")]),

  # With BITS_WORK_DIR and BITS_CHDIR set
  (("sw2", ".")    , "build --force-unknown-architecture zlib"                         , [("action", "build"), ("workDir", "sw2"), ("referenceSources", "sw2/MIRROR"), ("chdir", ".")]),
  (("sw3", "mydir"), "init"                                                            , [("action", "init"), ("workDir", "sw3"), ("referenceSources", "sw3/MIRROR"), ("chdir", "mydir")]),
  (("sw", ".")     , "clean --chdir mydir2 --work-dir sw4"                             , [("action", "clean"), ("workDir", "sw4"), ("chdir", "mydir2")]),
  (()              , "doctor zlib -C mydir -w sw2"                                     , [("action", "doctor"), ("workDir", "sw2"), ("chdir", "mydir")]),
  (()              , "deps zlib --outgraph graph.pdf"                                  , [("action", "deps"), ("outgraph", "graph.pdf")]),
]

GETSTATUSOUTPUT_MOCKS = {
  "which docker": (0, "/usr/local/bin/docker")
}

class ArgsTestCase(unittest.TestCase):
  @mock.patch("bits_helpers.utilities.getoutput", new=lambda cmd: "x86_64")   # for uname -m
  @mock.patch("bits_helpers.args._host_online_cpus", return_value=_MOCK_CPUSET)
  # Neutralise the host-dependent --memory/--memory-swap docker injection so
  # the exact docker_extra_args expectations below hold on any test host
  # (the cap depends on host RAM and is skipped on hosts below the reserve).
  @mock.patch("bits_helpers.args._docker_memory_args", return_value=[])
  @mock.patch('bits_helpers.args.commands')
  def test_actionParsing(self, mock_commands, _mock_mem, _mock_cpus):
    mock_commands.getstatusoutput.side_effect = lambda x : GETSTATUSOUTPUT_MOCKS[x]
    for (env, cmd, effects) in CORRECT_BEHAVIOR:
      (bits_helpers.args.DEFAULT_WORK_DIR,
       bits_helpers.args.DEFAULT_CHDIR) = env or ("sw", ".")
      with patch.object(sys, "argv", ["alibuild"] + shlex.split(cmd)):
        args, parser = doParseArgs()
        args = vars(args)
        for k, v in effects:
          self.assertEqual(args[k], v)

  @mock.patch("bits_helpers.utilities.getoutput", new=lambda cmd: "x86_64")   # for uname -m
  @mock.patch('bits_helpers.args.argparse.ArgumentParser.error')
  def test_failingParsing(self, mock_print):
    mock_print.side_effect = FakeExit("raised")
    for (cmd, pattern) in PARSER_ERRORS.items():
      mock_print.mock_calls = []
      with patch.object(sys, "argv", ["alibuild"] + shlex.split(cmd)):
        self.assertRaises(FakeExit, doParseArgs)
        for mock_call in mock_print.mock_calls:
          args = mock_call[1]
          print(args)
          self.assertTrue(
                re.match(pattern, args[0]),
                f"Expected '{args[0]}' matching '{pattern}' but it's not the case."
            )

  def test_validArchitectures(self) -> None:
    for arch in VALID_ARCHS:
      self.assertTrue(matchValidArch(arch))
    for arch in INVALID_ARCHS:
      self.assertFalse(matchValidArch(arch))


class CpusetInjectionTestCase(unittest.TestCase):
  """Tests for automatic --cpuset-cpus injection into docker_extra_args."""

  def _parse(self, cmd, cpuset_return="0-7"):
    """Helper: parse a build command with a mocked _host_online_cpus."""
    with mock.patch("bits_helpers.utilities.getoutput", return_value="x86_64"), \
         mock.patch("bits_helpers.args._host_online_cpus", return_value=cpuset_return), \
         mock.patch("bits_helpers.args._docker_memory_args", return_value=[]), \
         mock.patch("bits_helpers.args.commands") as mock_cmd, \
         patch.object(sys, "argv", ["alibuild"] + shlex.split(cmd)):
      mock_cmd.getstatusoutput.side_effect = lambda x: GETSTATUSOUTPUT_MOCKS[x]
      args, _ = doParseArgs()
      return vars(args)

  def test_cpuset_injected_by_default(self):
    """--cpuset-cpus is added automatically when not specified by the user."""
    args = self._parse("build zlib -a slc7_x86-64 --docker", cpuset_return="0-7")
    self.assertIn("--cpuset-cpus=0-7", args["docker_extra_args"])

  def test_cpuset_injected_once(self):
    """--cpuset-cpus appears exactly once in docker_extra_args."""
    args = self._parse("build zlib -a slc7_x86-64 --docker", cpuset_return="0-7")
    cpuset_args = [a for a in args["docker_extra_args"] if a.startswith("--cpuset-cpus")]
    self.assertEqual(len(cpuset_args), 1)

  def test_user_cpuset_not_overridden(self):
    """A user-supplied --cpuset-cpus is preserved; no automatic one is added."""
    args = self._parse(
      "build zlib -a slc7_x86-64 --docker --docker-extra-args=--cpuset-cpus=0-1",
      cpuset_return="0-7",
    )
    cpuset_args = [a for a in args["docker_extra_args"] if a.startswith("--cpuset-cpus")]
    self.assertEqual(len(cpuset_args), 1)
    self.assertEqual(cpuset_args[0], "--cpuset-cpus=0-1")

  def test_cpuset_reflects_host_online(self):
    """The injected value matches whatever _host_online_cpus() returns."""
    for cpuset in ("0-3", "0-7", "0-1,4-5"):
      with self.subTest(cpuset=cpuset):
        args = self._parse("build zlib -a slc7_x86-64 --docker", cpuset_return=cpuset)
        self.assertIn(f"--cpuset-cpus={cpuset}", args["docker_extra_args"])

  def test_network_host_still_present(self):
    """--network=host is always present alongside --cpuset-cpus."""
    args = self._parse("build zlib -a slc7_x86-64 --docker", cpuset_return="0-7")
    self.assertIn("--network=host", args["docker_extra_args"])

  def test_host_online_cpus_sysfs(self):
    """_host_online_cpus() reads /sys/devices/system/cpu/online when available."""
    mock_open = mock.mock_open(read_data="0-11\n")
    with mock.patch("builtins.open", mock_open):
      result = _host_online_cpus()
    self.assertEqual(result, "0-11")
    mock_open.assert_called_once_with("/sys/devices/system/cpu/online")

  def test_host_online_cpus_fallback(self):
    """_host_online_cpus() falls back to os.cpu_count() when sysfs is absent."""
    with mock.patch("builtins.open", side_effect=OSError), \
         mock.patch("os.cpu_count", return_value=4):
      result = _host_online_cpus()
    self.assertEqual(result, "0-3")

  def test_host_online_cpus_fallback_single_cpu(self):
    """_host_online_cpus() handles os.cpu_count() returning None gracefully."""
    with mock.patch("builtins.open", side_effect=OSError), \
         mock.patch("os.cpu_count", return_value=None):
      result = _host_online_cpus()
    self.assertEqual(result, "0-0")


class DockerMemoryCapTestCase(unittest.TestCase):
  """--memory/--memory-swap injection: no single build can OOM the host."""

  @staticmethod
  def _flags(meminfo="MemTotal:       67108864 kB\n", system="Linux", env=None):
    from bits_helpers.args import _docker_memory_args
    with mock.patch("bits_helpers.args.platform.system", return_value=system), \
         mock.patch("builtins.open", mock.mock_open(read_data=meminfo)), \
         mock.patch.dict(os.environ, env or {}, clear=False):
      if env is None and "BITS_DOCKER_MEMORY" in os.environ:
        del os.environ["BITS_DOCKER_MEMORY"]  # pragma: no cover
      return _docker_memory_args()

  def test_cap_is_total_minus_reserve(self):
    # 64 GiB host: reserve = max(4096, 6553) = 6553 MiB → cap 58983 MiB.
    flags = self._flags()
    self.assertEqual(flags, ["--memory=58983m", "--memory-swap=58983m"])

  def test_small_host_skips_cap(self):
    # 4 GiB host is below the 4 GiB reserve → no cap rather than a negative one.
    self.assertEqual(self._flags(meminfo="MemTotal:        4194304 kB\n"), [])

  def test_non_linux_skips_cap(self):
    self.assertEqual(self._flags(system="Darwin"), [])

  def test_env_override_value(self):
    flags = self._flags(env={"BITS_DOCKER_MEMORY": "48g"})
    self.assertEqual(flags, ["--memory=48g", "--memory-swap=48g"])

  def test_env_override_off(self):
    for off in ("off", "0", "false", "no"):
      with self.subTest(off=off):
        self.assertEqual(self._flags(env={"BITS_DOCKER_MEMORY": off}), [])

  def test_user_memory_flag_suppresses_injection(self):
    # Parse-level: a user-supplied --memory* in --docker-extra-args wins.
    with mock.patch("bits_helpers.utilities.getoutput", return_value="x86_64"), \
         mock.patch("bits_helpers.args._host_online_cpus", return_value="0-7"), \
         mock.patch("bits_helpers.args._docker_memory_args",
                    return_value=["--memory=59g", "--memory-swap=59g"]), \
         mock.patch("bits_helpers.args.commands") as mock_cmd, \
         patch.object(sys, "argv", ["alibuild"] + shlex.split(
           "build zlib -a slc7_x86-64 --docker --docker-extra-args=--memory=8g")):
      mock_cmd.getstatusoutput.side_effect = lambda x: GETSTATUSOUTPUT_MOCKS[x]
      args, _ = doParseArgs()
    mem_args = [a for a in vars(args)["docker_extra_args"] if a.startswith("--memory")]
    self.assertEqual(mem_args, ["--memory=8g"])

  def test_injected_when_absent(self):
    # Parse-level: the helper's flags land in docker_extra_args by default.
    with mock.patch("bits_helpers.utilities.getoutput", return_value="x86_64"), \
         mock.patch("bits_helpers.args._host_online_cpus", return_value="0-7"), \
         mock.patch("bits_helpers.args._docker_memory_args",
                    return_value=["--memory=59g", "--memory-swap=59g"]), \
         mock.patch("bits_helpers.args.commands") as mock_cmd, \
         patch.object(sys, "argv", ["alibuild"] + shlex.split(
           "build zlib -a slc7_x86-64 --docker")):
      mock_cmd.getstatusoutput.side_effect = lambda x: GETSTATUSOUTPUT_MOCKS[x]
      args, _ = doParseArgs()
    self.assertIn("--memory=59g", vars(args)["docker_extra_args"])
    self.assertIn("--memory-swap=59g", vars(args)["docker_extra_args"])


class ReleaseBaseTestCase(unittest.TestCase):
  """`release` is the implicit base of every defaults chain."""

  def test_release_prepended_when_absent(self):
    from bits_helpers.args import _with_release_base
    self.assertEqual(_with_release_base(["dev4"]), ["release", "dev4"])
    self.assertEqual(_with_release_base(["o2"]), ["release", "o2"])

  def test_release_not_duplicated(self):
    from bits_helpers.args import _with_release_base
    self.assertEqual(_with_release_base(["release"]), ["release"])
    self.assertEqual(_with_release_base(["release", "dev4"]), ["release", "dev4"])
    # An explicitly-positioned release is respected as written.
    self.assertEqual(_with_release_base(["dev4", "release"]), ["dev4", "release"])


class ReusePolicyArgsTestCase(unittest.TestCase):
  """ADR-0001 relaxed-reuse CLI flags parse and default safely."""

  def _parse(self, cmd):
    with mock.patch("bits_helpers.utilities.getoutput", return_value="x86_64"), \
         mock.patch("bits_helpers.args._host_online_cpus", return_value="0-7"), \
         mock.patch("bits_helpers.args.commands") as mock_cmd, \
         patch.object(sys, "argv", ["alibuild"] + shlex.split(cmd)):
      mock_cmd.getstatusoutput.side_effect = lambda x: GETSTATUSOUTPUT_MOCKS[x]
      args, _ = doParseArgs()
      return vars(args)

  def test_defaults_are_inert(self):
    # Simple aliBuild case: flags absent → None/[] at the arg layer (build.py
    # resolves reusePolicy to "strict"); nothing changes.
    a = self._parse("build --force-unknown-architecture zlib")
    self.assertIsNone(a["reusePolicy"])
    self.assertIsNone(a["reuseBase"])
    self.assertEqual(a["buildLocal"], [])

  def test_relaxed_flags_parse(self):
    a = self._parse("build --force-unknown-architecture --reuse-policy relaxed "
                    "--reuse-base LCG_109 --build-local p1,p2 zlib")
    self.assertEqual(a["reusePolicy"], "relaxed")
    self.assertEqual(a["reuseBase"], "LCG_109")
    self.assertEqual(a["buildLocal"], ["p1", "p2"])

  def test_initdotsh_flag_tristate(self):
    # Unset at the arg layer (build.py resolves None -> from-modules, or legacy
    # when BITS_LEGACY_INITDOTSH=1); the two flags force the value explicitly.
    self.assertIsNone(
      self._parse("build --force-unknown-architecture zlib")["initdotshFromModules"])
    self.assertIs(
      self._parse("build --force-unknown-architecture --legacy-initdotsh zlib")
      ["initdotshFromModules"], False)
    self.assertIs(
      self._parse("build --force-unknown-architecture --initdotsh-from-modules zlib")
      ["initdotshFromModules"], True)


class ProviderPathFrontendTestCase(unittest.TestCase):
  """Legacy vs provider path is chosen by the front-end: aliBuild
  (BITS_BRANDING=aliBuild) defaults to NO bits-providers (legacy alidist path);
  native bits defaults to the provider path. Explicit BITS_PROVIDERS wins."""

  def _bits_providers(self, set_env):
    with mock.patch("bits_helpers.utilities.getoutput", return_value="x86_64"), \
         mock.patch("bits_helpers.args._host_online_cpus", return_value="0-7"), \
         mock.patch("bits_helpers.args.commands") as mock_cmd, \
         mock.patch("bits_helpers.args._read_bits_rc", return_value={}), \
         mock.patch.dict(os.environ, set_env, clear=False), \
         patch.object(sys, "argv", ["x", "build", "--force-unknown-architecture", "zlib"]):
      for k in ("BITS_BRANDING", "BITS_PROVIDERS"):
        if k not in set_env:
          os.environ.pop(k, None)
      mock_cmd.getstatusoutput.side_effect = lambda x: GETSTATUSOUTPUT_MOCKS[x]
      args, _ = doParseArgs()
      return vars(args)["bits_providers"]

  def test_alibuild_frontend_defaults_to_no_providers(self):
    self.assertEqual(self._bits_providers({"BITS_BRANDING": "aliBuild"}), "")

  def test_native_bits_defaults_to_providers(self):
    self.assertEqual(self._bits_providers({}),
                     "https://github.com/bitsorg/bits-providers")

  def test_explicit_providers_wins_under_alibuild(self):
    self.assertEqual(
      self._bits_providers({"BITS_BRANDING": "aliBuild",
                            "BITS_PROVIDERS": "https://example.com/p"}),
      "https://example.com/p")


class ReadBitsRcTestCase(unittest.TestCase):
  """_read_bits_rc accepts the simplified flat layout and the [bits] section."""

  def _read(self, content):
    import tempfile, os
    import bits_helpers.args as A
    p = os.path.join(tempfile.mkdtemp(), "bits.rc")
    with open(p, "w") as fh:
      fh.write(content)
    with mock.patch.object(A, "_BITS_RC_SEARCH_PATHS", [p]):
      return A._read_bits_rc()

  def test_flat_headerless_file(self):
    # The simplified format (no [bits] section), incl. a trailing space.
    rc = self._read("organisation = stacks \nconfig_dir=.\n")
    self.assertEqual(rc.get("organisation"), "stacks")
    self.assertEqual(rc.get("config_dir"), ".")

  def test_explicit_bits_section_still_works(self):
    rc = self._read("[bits]\norganisation = stacks\nconfig_dir = .\n")
    self.assertEqual(rc.get("organisation"), "stacks")
    self.assertEqual(rc.get("config_dir"), ".")

  def test_missing_file_returns_empty(self):
    import bits_helpers.args as A
    with mock.patch.object(A, "_BITS_RC_SEARCH_PATHS", ["/no/such/bits.rc"]):
      self.assertEqual(A._read_bits_rc(), {})

  def test_search_path_seeds_bits_path(self):
    # bits.rc search_path must seed BITS_PATH so a single-package build finds
    # recipes in a sub-repo (e.g. ./lcg.bits). An explicit env BITS_PATH wins.
    import os, tempfile
    import bits_helpers.args as A
    p = os.path.join(tempfile.mkdtemp(), "bits.rc")
    with open(p, "w") as fh:
      fh.write("config_dir=.\nsearch_path=lcg\n")
    saved = os.environ.pop("BITS_PATH", None)
    try:
      with mock.patch.object(A, "_BITS_RC_SEARCH_PATHS", [p]), \
           mock.patch("bits_helpers.utilities.getoutput", return_value="x86_64"), \
           mock.patch("bits_helpers.args._host_online_cpus", return_value="0-7"), \
           mock.patch("bits_helpers.args.commands") as mc, \
           patch.object(sys, "argv",
                        ["alibuild", "build", "--force-unknown-architecture", "zlib"]):
        mc.getstatusoutput.side_effect = lambda x: GETSTATUSOUTPUT_MOCKS[x]
        doParseArgs()
      self.assertEqual(os.environ.get("BITS_PATH"), "lcg")
    finally:
      os.environ.pop("BITS_PATH", None)
      if saved is not None:
        os.environ["BITS_PATH"] = saved


if __name__ == '__main__':
  unittest.main()
