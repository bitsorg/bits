# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import unittest

# Assuming you are using the mock library to ... mock things
from unittest.mock import patch

from bits_helpers.matchers import filterByArchitectureDefaults, disabledByArchitectureDefaults
from bits_helpers.matchers import resolve_variables
from bits_helpers.arch import doDetectArch, predefined_arch_vars
from bits_helpers.utilities import Hasher
from bits_helpers.utilities import asList
from bits_helpers.utilities import prunePaths
from bits_helpers.utilities import resolve_version, resolve_spec_data, resolve_tag
from bits_helpers.utilities import topological_sort
from bits_helpers.matchers import _parse_req_matcher, _collect_version_pins
from bits_helpers.utilities import asDict, merge_dicts
from bits_helpers.matchers import _version_compare, _parse_patch_entry, filterPatches, _matcher_active
from collections import OrderedDict
import bits_helpers
import bits_helpers.log
import os
import string

UBUNTU_1510_OS_RELEASE = """
NAME="Ubuntu"
VERSION="15.10 (Wily Werewolf)"
ID=ubuntu
ID_LIKE=debian
PRETTY_NAME="Ubuntu 15.10"
VERSION_ID="15.10"
HOME_URL="http://www.ubuntu.com/"
SUPPORT_URL="http://help.ubuntu.com/"
BUG_REPORT_URL="http://bugs.launchpad.net/ubuntu/"
"""

LINUX_MINT_OS_RELEASE = """
NAME="Ubuntu"
VERSION="14.04.4 LTS, Trusty Tahr"
ID=ubuntu
ID_LIKE=debian
PRETTY_NAME="Ubuntu 14.04.4 LTS"
VERSION_ID="14.04"
HOME_URL="http://www.ubuntu.com/"
SUPPORT_URL="http://help.ubuntu.com/"
BUG_REPORT_URL="http://bugs.launchpad.net/ubuntu/"
"""

UBUNTU_1404_OS_RELEASE = """
NAME="Ubuntu"
VERSION="14.04.3 LTS, Trusty Tahr"
ID=ubuntu
ID_LIKE=debian
PRETTY_NAME="Ubuntu 14.04.3 LTS"
VERSION_ID="14.04"
HOME_URL="http://www.ubuntu.com/"
SUPPORT_URL="http://help.ubuntu.com/"
BUG_REPORT_URL="http://bugs.launchpad.net/ubuntu/"
"""

UBUNTU_1604_OS_RELEASE = """
NAME="Ubuntu"
VERSION="16.04 LTS (Xenial Xerus)"
ID=ubuntu
ID_LIKE=debian
PRETTY_NAME="Ubuntu 16.04 LTS"
VERSION_ID="16.04"
HOME_URL="http://www.ubuntu.com/"
SUPPORT_URL="http://help.ubuntu.com/"
BUG_REPORT_URL="http://bugs.launchpad.net/ubuntu/"
UBUNTU_CODENAME=xenial
"""

UBUNTU_1804_OS_RELEASE = """
NAME="Ubuntu"
VERSION="18.04.4 LTS (Bionic Beaver)"
ID=ubuntu
ID_LIKE=debian
PRETTY_NAME="Ubuntu 18.04.4 LTS"
VERSION_ID="18.04"
HOME_URL="https://www.ubuntu.com/"
SUPPORT_URL="https://help.ubuntu.com/"
BUG_REPORT_URL="https://bugs.launchpad.net/ubuntu/"
PRIVACY_POLICY_URL="https://www.ubuntu.com/legal/terms-and-policies/privacy-policy"
VERSION_CODENAME=bionic
UBUNTU_CODENAME=bionic
"""

DEBIAN_7_OS_RELEASE = """
PRETTY_NAME="Debian GNU/Linux 7 (wheezy)"
NAME="Debian GNU/Linux"
VERSION_ID="7"
VERSION="7 (wheezy)"
ID=debian
ANSI_COLOR="1;31"
HOME_URL="http://www.debian.org/"
SUPPORT_URL="http://www.debian.org/support/"
BUG_REPORT_URL="http://bugs.debian.org/"
"""

DEBIAN_8_OS_RELEASE = """
PRETTY_NAME="Debian GNU/Linux 8 (jessie)"
NAME="Debian GNU/Linux"
VERSION_ID="8"
VERSION="8 (jessie)"
ID=debian
HOME_URL="http://www.debian.org/"
SUPPORT_URL="http://www.debian.org/support"
BUG_REPORT_URL="https://bugs.debian.org/"
"""

SABAYON2_OS_RELEASE = """
NAME=Sabayon
ID=sabayon
PRETTY_NAME="Sabayon/Linux"
ANSI_COLOR="1;32"
HOME_URL="http://www.sabayon.org/"
SUPPORT_URL="http://forum.sabayon.org/"
BUG_REPORT_URL="https://bugs.sabayon.org/"
"""

ALMA_8_OS_RELEASE = """
NAME="AlmaLinux"
VERSION="8.10 (Cerulean Leopard)"
ID="almalinux"
ID_LIKE="rhel centos fedora"
VERSION_ID="8.10"
PLATFORM_ID="platform:el8"
PRETTY_NAME="AlmaLinux 8.10 (Cerulean Leopard)"
ANSI_COLOR="0;34"
LOGO="fedora-logo-icon"
CPE_NAME="cpe:/o:almalinux:almalinux:8::baseos"
HOME_URL="https://almalinux.org/"
DOCUMENTATION_URL="https://wiki.almalinux.org/"
BUG_REPORT_URL="https://bugs.almalinux.org/"

ALMALINUX_MANTISBT_PROJECT="AlmaLinux-8"
ALMALINUX_MANTISBT_PROJECT_VERSION="8.10"
REDHAT_SUPPORT_PRODUCT="AlmaLinux"
REDHAT_SUPPORT_PRODUCT_VERSION="8.10"
SUPPORT_END=2029-06-01
"""

ALMA_9_OS_RELEASE = """
NAME="AlmaLinux"
VERSION="9.6 (Sage Margay)"
ID="almalinux"
ID_LIKE="rhel centos fedora"
VERSION_ID="9.6"
PLATFORM_ID="platform:el9"
PRETTY_NAME="AlmaLinux 9.6 (Sage Margay)"
ANSI_COLOR="0;34"
LOGO="fedora-logo-icon"
CPE_NAME="cpe:/o:almalinux:almalinux:9::baseos"
HOME_URL="https://almalinux.org/"
DOCUMENTATION_URL="https://wiki.almalinux.org/"
BUG_REPORT_URL="https://bugs.almalinux.org/"

ALMALINUX_MANTISBT_PROJECT="AlmaLinux-9"
ALMALINUX_MANTISBT_PROJECT_VERSION="9.6"
REDHAT_SUPPORT_PRODUCT="AlmaLinux"
REDHAT_SUPPORT_PRODUCT_VERSION="9.6"
SUPPORT_END=2032-06-01
"""

ROCKY_8_OS_RELEASE = """
NAME="Rocky Linux"
VERSION="8.10 (Green Obsidian)"
ID="rocky"
ID_LIKE="rhel centos fedora"
VERSION_ID="8.10"
PLATFORM_ID="platform:el8"
PRETTY_NAME="Rocky Linux 8.10 (Green Obsidian)"
ANSI_COLOR="0;32"
LOGO="fedora-logo-icon"
CPE_NAME="cpe:/o:rocky:rocky:8:GA"
HOME_URL="https://rockylinux.org/"
BUG_REPORT_URL="https://bugs.rockylinux.org/"
SUPPORT_END="2029-05-31"
ROCKY_SUPPORT_PRODUCT="Rocky-Linux-8"
ROCKY_SUPPORT_PRODUCT_VERSION="8.10"
REDHAT_SUPPORT_PRODUCT="Rocky Linux"
REDHAT_SUPPORT_PRODUCT_VERSION="8.10"
"""

ROCKY_9_OS_RELEASE = """
NAME="Rocky Linux"
VERSION="9.6 (Blue Onyx)"
ID="rocky"
ID_LIKE="rhel centos fedora"
VERSION_ID="9.6"
PLATFORM_ID="platform:el9"
PRETTY_NAME="Rocky Linux 9.6 (Blue Onyx)"
ANSI_COLOR="0;32"
LOGO="fedora-logo-icon"
CPE_NAME="cpe:/o:rocky:rocky:9::baseos"
HOME_URL="https://rockylinux.org/"
VENDOR_NAME="RESF"
VENDOR_URL="https://resf.org/"
BUG_REPORT_URL="https://bugs.rockylinux.org/"
SUPPORT_END="2032-05-31"
ROCKY_SUPPORT_PRODUCT="Rocky-Linux-9"
ROCKY_SUPPORT_PRODUCT_VERSION="9.6"
REDHAT_SUPPORT_PRODUCT="Rocky Linux"
REDHAT_SUPPORT_PRODUCT_VERSION="9.6"
"""

architecturePayloads = [
  ['osx_x86-64', False, [], ('','',''), 'Darwin', 'x86-64'],
  ['osx_arm64', False, [], ('','',''), 'Darwin', 'arm64'],
  ['slc5_x86-64', False, [], ('redhat', '5.XX', 'Boron'), 'Linux', 'x86-64'],
  ['slc6_x86-64', False, [], ('centos', '6.X', 'Carbon'), 'Linux', 'x86-64'],
  ['slc7_x86-64', False, [], ('centos', '7.X', 'Ptor'), 'Linux', 'x86-64'],
  ['slc8_x86-64', True, ALMA_8_OS_RELEASE.split("\n"), ('AlmaLinux', '8.10', 'Cerulean Leopard'), 'Linux', 'x86_64'],
  ['slc8_x86-64', True, ROCKY_8_OS_RELEASE.split("\n"), ('Rocky Linux', '8.10', 'Green Obsidian'), 'Linux', 'x86_64'],
  ['slc9_x86-64', True, ALMA_9_OS_RELEASE.split("\n"), ('AlmaLinux', '9.6', 'Sage Margay'), 'Linux', 'x86_64'],
  ['slc9_x86-64', True, ROCKY_9_OS_RELEASE.split("\n"), ('Rocky Linux', '9.6', 'Blue Onyx'), 'Linux', 'x86_64'],
  ['ubuntu1804_x86-64', True, UBUNTU_1804_OS_RELEASE.split("\n"), ('Ubuntu', '18.04', 'bionic'), 'Linux', 'x86-64'],
  ['ubuntu1604_x86-64', True, UBUNTU_1604_OS_RELEASE.split("\n"), ('Ubuntu', '16.04', 'xenial'), 'Linux', 'x86-64'],
  ['ubuntu1510_x86-64', False, [], ('Ubuntu', '15.10', 'wily'), 'Linux', 'x86-64'],
  ['ubuntu1510_x86-64', True, UBUNTU_1510_OS_RELEASE.split("\n"), ('Ubuntu', '15.10', 'wily'), 'Linux', 'x86-64'],
  ['ubuntu1510_x86-64', True, UBUNTU_1510_OS_RELEASE.split("\n"), ('', '', ''), 'Linux', 'x86-64'], # ANACONDA case
  ['ubuntu1404_x86-64', True, UBUNTU_1404_OS_RELEASE.split("\n"), ('Ubuntu', '14.04', 'trusty'), 'Linux', 'x86-64'],
  ['ubuntu1404_x86-64', True, UBUNTU_1404_OS_RELEASE.split("\n"), ('', '', ''), 'Linux', 'x86-64'],
  ['ubuntu1404_x86-64', True, LINUX_MINT_OS_RELEASE.split("\n"), ('LinuxMint', '17.3', 'rosa'), 'Linux', 'x86-64'], # LinuxMint
  ['ubuntu1204_x86-64', True, DEBIAN_7_OS_RELEASE.split("\n"), ('Debian', '7', 'wheezy'), 'Linux', 'x86-64'],
  ['ubuntu1404_x86-64', True, DEBIAN_8_OS_RELEASE.split("\n"), ('Debian', '8', 'jessie'), 'Linux', 'x86-64'],
  ['sabayon2_x86-64', True, SABAYON2_OS_RELEASE.split("\n"), ('gentoo', '2.2', ''), 'Linux', 'x86_64']
]

macOSArchitecturePayloads = [
  ['osx_x86-64', False, [], ('','',''), 'Darwin', 'x86_64'],
  ['osx_arm64', False, [], ('','',''), 'Darwin', 'arm64'],
]

class TestUtilities(unittest.TestCase):
  def test_osx(self) -> None:
    for payload in architecturePayloads:
      result, hasOsRelease, osReleaseLines, platformTuple, platformSystem, platformProcessor = payload
      self.assertEqual(result, doDetectArch(hasOsRelease, osReleaseLines, platformTuple, platformSystem, platformProcessor))
  # Test by mocking platform.processor
  def test_osx_mock(self) -> None:
    for payload in macOSArchitecturePayloads:
      result, hasOsRelease, osReleaseLines, platformTuple, platformSystem, platformProcessor = payload
      with patch('platform.machine', return_value=platformProcessor):
        platformProcessor = None
        self.assertEqual(result, doDetectArch(hasOsRelease, osReleaseLines, platformTuple, platformSystem, None))
  def test_Hasher(self) -> None:
    h = Hasher()
    h("foo")
    self.assertEqual("0beec7b5ea3f0fdbc95d0dd47f3c5bc275da8a33", h.hexdigest())
    h("")
    self.assertEqual("0beec7b5ea3f0fdbc95d0dd47f3c5bc275da8a33", h.hexdigest())
    self.assertRaises(AttributeError, h, 1)
    h("bar")
    self.assertEqual("8843d7f92416211de9ebb963ff4ce28125932878", h.hexdigest())

  def test_UTF8_Hasher(self) -> None:
    h1 = Hasher()
    h2 = Hasher()
    h3 = Hasher()
    h1('\ua000')
    h2('\ua001')
    h3(b'foo')
    self.assertEqual(h1.hexdigest(), "2af8e41129115eb231a0af76ec5465d3a9184fc4")
    self.assertEqual(h2.hexdigest(), "1619bcdbeff6828138ad9b6e43cc17e856457603")
    self.assertEqual(h3.hexdigest(), "0beec7b5ea3f0fdbc95d0dd47f3c5bc275da8a33")
    self.assertNotEqual(h1.hexdigest(), h2.hexdigest())

  def test_asList(self) -> None:
    self.assertEqual(asList("a"), ["a"])
    self.assertEqual(asList(["a"]), ["a"])
    self.assertEqual(asList(None), [None])

  def test_filterByArchitecture(self) -> None:
    self.assertEqual(["AliRoot"], list(filterByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot"])))
    self.assertEqual([], list(filterByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot:(?!osx)"])))
    self.assertEqual(["GCC"], list(filterByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot:(?!osx)", "GCC"])))
    self.assertEqual(["AliRoot", "GCC"], list(filterByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot:(?!slc6)", "GCC"])))
    self.assertEqual(["GCC"], list(filterByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot:slc6", "GCC:osx"])))
    self.assertEqual([], list(filterByArchitectureDefaults("osx_x86-64", "ali", [])))
    self.assertEqual(["GCC"], list(filterByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot:slc6", "GCC:defaults=ali"])))
    self.assertEqual([], list(filterByArchitectureDefaults("osx_x86-64", "o2", ["AliRoot:slc6", "GCC:defaults=ali"])))
    # Version-pinned entries: filter still yields the plain name
    self.assertEqual(["root"], list(filterByArchitectureDefaults("slc8_x86-64", "release", ["root = 6.24.02"])))
    self.assertEqual([], list(filterByArchitectureDefaults("osx_arm64", "release", ["root = 6.24.02:(?!osx)"])))
    self.assertEqual(["root"], list(filterByArchitectureDefaults("slc8_x86-64", "release", ["root = 6.24.02:(?!osx)"])))
    # Version-gated requires keyed on the depending package's own version:
    # the 5th positional arg is the owner version (sort -V comparison).
    reqs = ["CMake", "curl:version>=v6.40.00"]
    self.assertEqual(["CMake", "curl"],
                     list(filterByArchitectureDefaults("osx_arm64", "release", reqs, None, "v6.40.00")))
    self.assertEqual(["CMake"],
                     list(filterByArchitectureDefaults("osx_arm64", "release", reqs, None, "v6.38.00")))
    self.assertEqual(["CMake", "curl"],
                     list(filterByArchitectureDefaults("osx_arm64", "release", reqs, None, "v6.42.00")))
    # Combined arch + version atom: curl only on non-osx AND >= 6.40.
    reqs2 = ["curl:(?!osx) && version>=v6.40.00"]
    self.assertEqual(["curl"], list(filterByArchitectureDefaults("slc9_x86-64", "release", reqs2, None, "v6.40.00")))
    self.assertEqual([], list(filterByArchitectureDefaults("osx_arm64", "release", reqs2, None, "v6.40.00")))
    self.assertEqual([], list(filterByArchitectureDefaults("slc9_x86-64", "release", reqs2, None, "v6.38.00")))

  def test_disabledByArchitecture(self) -> None:
    self.assertEqual([], list(disabledByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot"])))
    self.assertEqual(["AliRoot"], list(disabledByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot:(?!osx)"])))
    self.assertEqual(["AliRoot"], list(disabledByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot:(?!osx)", "GCC"])))
    self.assertEqual([], list(disabledByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot:(?!slc6)", "GCC"])))
    self.assertEqual(["AliRoot"], list(disabledByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot:slc6", "GCC:osx"])))
    self.assertEqual([], list(disabledByArchitectureDefaults("osx_x86-64", "ali", [])))
    self.assertEqual(["AliRoot"], list(disabledByArchitectureDefaults("osx_x86-64", "ali", ["AliRoot:slc6", "GCC:defaults=ali"])))
    self.assertEqual(["AliRoot", "GCC"], list(disabledByArchitectureDefaults("osx_x86-64", "o2", ["AliRoot:slc6", "GCC:defaults=ali"])))
    # Version-pinned entries are disabled according to the matcher, not the pin
    self.assertEqual(["root"], list(disabledByArchitectureDefaults("osx_arm64", "release", ["root = 6.24.02:(?!osx)"])))

  def test_variable_conditional_requires(self) -> None:
    """`dep:(?VAR)` is active iff defaults variable VAR is truthy."""
    arch = "ubuntu2510_x86-64-gcc15-dbg"
    # Active when the variable is truthy ...
    self.assertEqual(["cuda"], list(filterByArchitectureDefaults(
        arch, ["dev4", "cuda"], ["cuda:(?cuda)"], {"cuda": "true"})))
    self.assertEqual([], list(disabledByArchitectureDefaults(
        arch, ["dev4", "cuda"], ["cuda:(?cuda)"], {"cuda": "true"})))
    # ... and disabled when unset or false-ish ...
    for vars_ in (None, {}, {"cuda": "false"}, {"cuda": "off"}, {"cuda": "0"}, {"cuda": ""}):
      self.assertEqual([], list(filterByArchitectureDefaults(
          arch, ["dev4"], ["cuda:(?cuda)"], vars_)), vars_)
      self.assertEqual(["cuda"], list(disabledByArchitectureDefaults(
          arch, ["dev4"], ["cuda:(?cuda)"], vars_)), vars_)
    # A variable matcher must not be confused with a real arch regex like (?!osx)
    self.assertEqual([], list(filterByArchitectureDefaults(
        "osx_arm64", ["dev4"], ["GCC:(?!osx)"], {"cuda": "true"})))
    self.assertEqual(["GCC"], list(filterByArchitectureDefaults(
        arch, ["dev4"], ["GCC:(?!osx)"], {"cuda": "true"})))

  def test_predefined_arch_vars(self) -> None:
    """Only the truthy platform variables are exposed, so (?osx) is false off mac."""
    self.assertEqual({"osx": "true", "arm64": "true", "aarch64": "true"},
                     predefined_arch_vars("osx_arm64"))
    v = predefined_arch_vars("ubuntu2510_x86-64-gcc15")
    self.assertEqual("true", v.get("linux"))
    self.assertEqual("true", v.get("x86_64"))
    self.assertNotIn("osx", v)
    self.assertNotIn("arm64", v)

  def test_resolve_variables(self) -> None:
    """Gated `variables:` entries resolve against flavours / predefined / earlier vars."""
    linux = "ubuntu2510_x86-64-gcc15"
    osx = "osx_arm64"
    # Plain entries pass through; predefined arch vars are seeded.
    r = resolve_variables({"cxxstd": "20"}, {}, linux, ["dev4"])
    self.assertEqual("20", r["cxxstd"])
    self.assertEqual("true", r["linux"])
    self.assertNotIn("osx", r)
    # Gated on a CLI flavour: defined only when the flavour is set.
    vblock = {"heavygen": {"value": "yes", "when": "(?openloops)"}}
    self.assertNotIn("heavygen", resolve_variables(vblock, {}, linux, ["dev4"]))
    self.assertEqual("yes", resolve_variables(
        vblock, {"openloops": "true"}, linux, ["dev4"])["heavygen"])
    # Gated on flavour AND not-osx (arch regex): off on mac even with the flavour.
    g = {"heavygen": {"value": True, "when": "(?openloops) && (?!osx)"}}
    self.assertIn("heavygen", resolve_variables(g, {"openloops": "1"}, linux, ["dev4"]))
    self.assertNotIn("heavygen", resolve_variables(g, {"openloops": "1"}, osx, ["dev4"]))
    # Gated on a predefined arch var directly: (?osx) true on mac only.
    o = {"macflag": {"value": True, "when": "(?osx)"}}
    self.assertIn("macflag", resolve_variables(o, {}, osx, ["dev4"]))
    self.assertNotIn("macflag", resolve_variables(o, {}, linux, ["dev4"]))
    # Chained: a later entry gated on an earlier (previously defined) variable.
    chain = OrderedDict([
        ("base", {"value": True, "when": "(?openloops)"}),
        ("derived", {"value": True, "when": "(?base)"}),
    ])
    self.assertIn("derived", resolve_variables(chain, {"openloops": "1"}, linux, ["dev4"]))
    self.assertNotIn("derived", resolve_variables(chain, {}, linux, ["dev4"]))
    # A CLI flavour overrides a defaults entry of the same name.
    self.assertEqual("cli", resolve_variables(
        {"x": "file"}, {"x": "cli"}, linux, ["dev4"])["x"])
    # The resolved variables then drive package gating end to end.
    rv = resolve_variables({"heavygen": {"value": True, "when": "(?openloops) && (?!osx)"}},
                           {"openloops": "1"}, linux, ["dev4"])
    self.assertEqual(["herwig3"], list(filterByArchitectureDefaults(
        linux, ["dev4"], ["herwig3:(?heavygen)"], rv)))

  def test_parse_req_matcher(self) -> None:
    """_parse_req_matcher should correctly parse all requirement syntax variants."""
    # Plain name
    self.assertEqual(("AliRoot", ".*", None), _parse_req_matcher("AliRoot"))
    # Arch-conditional
    self.assertEqual(("AliRoot", "(?!osx)", None), _parse_req_matcher("AliRoot:(?!osx)"))
    # defaults= conditional
    self.assertEqual(("GCC", "defaults=ali", None), _parse_req_matcher("GCC:defaults=ali"))
    # Version pin, no matcher
    self.assertEqual(("root", ".*", "6.24.02"), _parse_req_matcher("root = 6.24.02"))
    self.assertEqual(("root", ".*", "6.24.02"), _parse_req_matcher("root=6.24.02"))
    self.assertEqual(("root", ".*", "master"), _parse_req_matcher("root = master"))
    # Version pin with spaces around =
    self.assertEqual(("my-provider", ".*", "feature-xyz"), _parse_req_matcher("my-provider = feature-xyz"))
    # Version pin + arch matcher
    self.assertEqual(("root", "(?!osx)", "6.24.02"), _parse_req_matcher("root = 6.24.02:(?!osx)"))
    # Version pin + defaults= matcher
    self.assertEqual(("boost", "defaults=o2", "1.80.0"), _parse_req_matcher("boost = 1.80.0:defaults=o2"))

  def test_collect_version_pins_basic(self) -> None:
    """_collect_version_pins registers active pins and ignores inactive ones."""
    pins = {}
    _collect_version_pins("slc8_x86-64", "release",
                          ["root = 6.24.02", "hepmc3", "boost = 1.82.0:(?!osx)"],
                          "mypackage", pins, {})
    self.assertEqual({"root": "6.24.02", "boost": "1.82.0"}, pins)

  def test_collect_version_pins_arch_inactive(self) -> None:
    """A pin whose matcher does not match the current arch is ignored."""
    pins = {}
    _collect_version_pins("osx_arm64", "release",
                          ["boost = 1.82.0:(?!osx)"],
                          "mypackage", pins, {})
    self.assertEqual({}, pins)

  def test_collect_version_pins_same_version_two_owners(self) -> None:
    """Two packages pinning the same dep to the same version is not an error."""
    pins = {"root": "6.24.02"}
    # Should not raise
    _collect_version_pins("slc8_x86-64", "release",
                          ["root = 6.24.02"],
                          "other-package", pins, {})
    self.assertEqual({"root": "6.24.02"}, pins)

  def test_collect_version_pins_conflict_raises(self) -> None:
    """Two different version pins for the same dep must raise a fatal error."""
    pins = {"root": "6.24.02"}
    with self.assertRaises(SystemExit):
      _collect_version_pins("slc8_x86-64", "release",
                            ["root = 6.32.06"],
                            "other-package", pins, {})

  def test_collect_version_pins_already_resolved_same_version(self) -> None:
    """A pin for an already-resolved dep at the same version is silently accepted."""
    from collections import OrderedDict
    specs = {"root": {"version": "6.24.02"}}
    pins = {}
    _collect_version_pins("slc8_x86-64", "release",
                          ["root = 6.24.02"],
                          "mypackage", pins, specs)
    # No pin registered (already resolved correctly); no error raised
    self.assertEqual({}, pins)

  def test_collect_version_pins_already_resolved_conflict_raises(self) -> None:
    """A pin for a dep resolved at a *different* version must raise a fatal error."""
    specs = {"root": {"version": "6.32.06"}}
    pins = {}
    with self.assertRaises(SystemExit):
      _collect_version_pins("slc8_x86-64", "release",
                            ["root = 6.24.02"],
                            "mypackage", pins, specs)

  def test_prunePaths(self) -> None:
    fake_env = {
      "PATH": "/sw/bin:/usr/local/bin",
      "LD_LIBRARY_PATH": "/sw/lib",
      "DYLD_LIBRARY_PATH": "/sw/lib",
      "BITS_VERSION": "v1.0.0",
      "ROOT_VERSION": "v1.0.0"
    }
    fake_env_copy = {
      "PATH": "/sw/bin:/usr/local/bin",
      "LD_LIBRARY_PATH": "/sw/lib",
      "DYLD_LIBRARY_PATH": "/sw/lib",
      "BITS_VERSION": "v1.0.0",
      "ROOT_VERSION": "v1.0.0"
    }
    with patch.object(os, "environ", fake_env):
      prunePaths("/sw")
      self.assertTrue("ROOT_VERSION" not in fake_env)
      self.assertTrue(fake_env["PATH"] == "/usr/local/bin")
      self.assertTrue(fake_env["LD_LIBRARY_PATH"] == "")
      self.assertTrue(fake_env["DYLD_LIBRARY_PATH"] == "")
      self.assertTrue(fake_env["BITS_VERSION"] == "v1.0.0")

    with patch.object(os, "environ", fake_env_copy):
      prunePaths("/foo")
      self.assertTrue("ROOT_VERSION" not in fake_env_copy)
      self.assertTrue(fake_env_copy["PATH"] == "/sw/bin:/usr/local/bin")
      self.assertTrue(fake_env_copy["LD_LIBRARY_PATH"] == "/sw/lib")
      self.assertTrue(fake_env_copy["DYLD_LIBRARY_PATH"] == "/sw/lib")
      self.assertTrue(fake_env_copy["BITS_VERSION"] == "v1.0.0")

  def test_resolver(self) -> None:
    spec = {"package": "test-pkg",
      "version": "%(tag_basename)s",
      "tag": "foo/bar",
      "commit_hash": "000000000000000000000000000"
    }
    self.assertTrue(resolve_version(spec, "release", "stream/v1", "v1"), "bar")
    spec["version"] = "%(branch_stream)s"
    self.assertTrue(resolve_version(spec, "release", "stream/v1", "v1"), "v1")
    spec["version"] = "%(defaults_upper)s"
    self.assertTrue(resolve_version(spec, "o2", "stream/v1", "v1"), "O2")
    spec["version"] = "NO%(defaults_upper)s"
    self.assertTrue(resolve_version(spec, "release", "stream/v1", "v1"), "NO")


class TestTopologicalSort(unittest.TestCase):
    """Check that various properties of topological sorting hold."""

    def test_resolve_dependency_chain(self) -> None:
        """Test that topological sorting correctly sorts packages in a dependency chain."""
        # Topological sorting only takes "requires" into account, since the
        # build/runtime distinction does not matter for resolving build order.
        self.assertEqual(["c", "b", "a"], list(topological_sort({
            "a": {"package": "a", "requires": ["b"]},
            "b": {"package": "b", "requires": ["c"]},
            "c": {"package": "c", "requires": []},
        })))

    def test_diamond_dependency(self) -> None:
        """Test that a diamond dependency relationship is handled correctly."""
        self.assertEqual(["base", "mid2", "mid1", "top"], list(topological_sort({
            "top": {"package": "top", "requires": ["mid1", "mid2"]},
            # Add a mid1 -> mid2 cross-dependency to make the order deterministic.
            "mid1": {"package": "mid1", "requires": ["base", "mid2"]},
            "mid2": {"package": "mid2", "requires": ["base"]},
            "base": {"package": "base", "requires": []},
        })))

    def test_dont_drop_packages(self) -> None:
        """Check that topological sorting doesn't drop any packages."""
        # For half the packages, depend on the first package, to make this a
        # little more than trivial.
        specs = {pkg: {"package": pkg, "requires": [] if pkg < "m" else ["a"]}
                 for pkg in string.ascii_lowercase}
        self.assertEqual(frozenset(specs.keys()),
                         frozenset(topological_sort(specs)))

    def test_cycle(self) -> None:
        """Test that dependency cycles are detected and reported."""
        specs = {
            "A": {"package": "A", "requires": ["B"]},
            "B": {"package": "B", "requires": ["C"]},
            "C": {"package": "C", "requires": ["D"]},
            "D": {"package": "D", "requires": ["A"]}
        }
        with patch.object(bits_helpers.log, 'error') as mock_error:
          with self.assertRaises(SystemExit) as cm:
            list(topological_sort(specs))
          self.assertEqual(cm.exception.code, 1)
          mock_error.assert_called_once_with("%s", "Dependency cycle detected: A -> B -> C -> D -> A")

    def test_empty_set(self) -> None:
        """Test that an empty set of packages is handled correctly."""
        self.assertEqual([], list(topological_sort({})))
        
    def test_single_package(self) -> None:
        """Test that a single package with no dependencies is handled correctly."""
        self.assertEqual(["A"], list(topological_sort({
            "A": {"package": "A", "requires": []}
        })))
        
    def test_independent_packages(self) -> None:
        """Test that packages with no dependencies between them are handled correctly."""
        result = list(topological_sort({
            "A": {"package": "A", "requires": []},
            "B": {"package": "B", "requires": []},
            "C": {"package": "C", "requires": []}
        }))
        self.assertEqual({"A", "B", "C"}, set(result))
        self.assertEqual(3, len(result))


class TestResolveSpecDataVariables(unittest.TestCase):
    """Defaults-profile variables + soft expansion (PR #97, softer variant)."""

    def _spec(self, variables=None):
        return {"package": "foo", "commit_hash": "abc1234567", "tag": "v1",
                "version": "1.0", "variables": variables or {}}

    def test_default_var_available(self):
        out = resolve_spec_data(self._spec(), "%(base)s/x", ["release"],
                                default_vars={"base": "http://h"}, strict=False)
        self.assertEqual(out, "http://h/x")

    def test_recipe_var_overrides_default(self):
        out = resolve_spec_data(self._spec({"v": "recipe"}), "%(v)s", ["release"],
                                default_vars={"v": "defaults"}, strict=True)
        self.assertEqual(out, "recipe")

    def test_soft_leaves_unknown_and_shell_percent_untouched(self):
        # %(base)s/%(name)s substitute; %(unknown)s, %%, and ${X%suffix} stay put,
        # and nothing raises (the soft path never does raw %-formatting).
        data = "u=%(base)s/%(name)s pct=100%% keep=${X%suffix} miss=%(unknown)s"
        out = resolve_spec_data(self._spec(), data, ["release"],
                                default_vars={"base": "http://h"}, strict=False)
        self.assertEqual(out, "u=http://h/foo pct=100%% keep=${X%suffix} miss=%(unknown)s")

    def test_soft_indirect_nesting(self):
        out = resolve_spec_data(self._spec(), "%(%(v1)s_key)s", ["release"],
                                default_vars={"v1": "foo", "foo_key": "bar"}, strict=False)
        self.assertEqual(out, "bar")

    def test_strict_unknown_is_fatal(self):
        with patch("bits_helpers.utilities.dieOnError") as die:
            resolve_spec_data(self._spec(), "%(nope)s", ["release"], strict=True)
        self.assertTrue(die.called)
        self.assertIs(die.call_args[0][0], True)  # dieOnError(True, ...)

    def test_strict_indirect_double_percent_still_works(self):
        spec = self._spec({"v1": "foo", "foo_key": "bar"})
        out = resolve_spec_data(spec, "%%(%(v1)s_key)s", ["release"], strict=True)
        self.assertEqual(out, "bar")


class AsDictTest(unittest.TestCase):
    """overrides: collapsing, including the 'name = value' pin shorthand."""

    def test_string_pin_shorthand(self):
        # A list of "name = value" strings (same syntax as requires: pins) must
        # produce per-package {version, tag} overrides, not be silently dropped.
        out = asDict(["acts = 44.4.0", "k4actstracking = v00-02"])
        self.assertEqual(dict(out["acts"]), {"version": "44.4.0", "tag": "44.4.0"})
        self.assertEqual(dict(out["k4actstracking"]), {"version": "v00-02", "tag": "v00-02"})

    def test_dict_form_preserved(self):
        out = asDict([{"GCC-Toolchain": {"source": "x", "tag": "v1"}}])
        self.assertEqual(dict(out["GCC-Toolchain"]), {"source": "x", "tag": "v1"})

    def test_mixed_and_malformed(self):
        # bare names (no '=') are ignored; valid pins still applied.
        out = asDict(["justaname", "a = 1"])
        self.assertNotIn("justaname", out)
        self.assertEqual(dict(out["a"]), {"version": "1", "tag": "1"})

    def test_empty_is_empty(self):
        self.assertEqual(asDict([]), {})
        self.assertEqual(asDict(None), {})


class DefaultsChainMergeTest(unittest.TestCase):
    """Chained defaults (a::b::c) deep-merge overrides: union of entries, last
    wins per key, regardless of list-form vs dict-form per profile."""

    @staticmethod
    def _merge_chain(*profiles):
        # Mirror readDefaults: normalise each profile's overrides to dict-form
        # BEFORE merging, so list-form and dict-form blocks interoperate.
        merged = OrderedDict()
        for p in profiles:
            p = OrderedDict(p)
            if "overrides" in p:
                p["overrides"] = asDict(p["overrides"])
            merged = merge_dicts(merged, p)
        return merged["overrides"]

    def test_dict_then_list_both_survive(self):
        # gcc15 (dict-form toolchain pin) :: key4hep (list-form acts pin):
        # both pins must survive -- the regression that silently dropped the
        # GCC-Toolchain pin (falling back to the gcc14 recipe default).
        ov = self._merge_chain(
            {"overrides": [{"GCC-Toolchain": {"source": "x", "tag": "v15.2.0-alice1"}}]},
            {"overrides": ["acts = 44.4.0", "k4actstracking = v00-02"]},
        )
        self.assertEqual(ov["GCC-Toolchain"]["tag"], "v15.2.0-alice1")
        self.assertEqual(dict(ov["acts"]), {"version": "44.4.0", "tag": "44.4.0"})
        self.assertEqual(dict(ov["k4actstracking"]), {"version": "v00-02", "tag": "v00-02"})

    def test_list_then_dict_both_survive(self):
        # Order-independent: the same chain with the forms swapped.
        ov = self._merge_chain(
            {"overrides": ["acts = 44.4.0"]},
            {"overrides": [{"GCC-Toolchain": {"tag": "v15.2.0-alice1"}}]},
        )
        self.assertIn("acts", ov)
        self.assertEqual(ov["GCC-Toolchain"]["tag"], "v15.2.0-alice1")

    def test_same_key_last_wins(self):
        # A later profile overriding the same package replaces its value.
        ov = self._merge_chain(
            {"overrides": ["acts = 26.0.0"]},
            {"overrides": ["acts = 44.4.0"]},
        )
        self.assertEqual(ov["acts"]["version"], "44.4.0")

    def test_same_key_deep_merge_keeps_other_fields(self):
        # Same package across profiles: fields merge key-by-key, last wins per
        # field, other fields preserved.
        ov = self._merge_chain(
            {"overrides": [{"ROOT": {"tag": "v6-36-04", "source": "orig"}}]},
            {"overrides": [{"ROOT": {"tag": "v6-40-00"}}]},
        )
        self.assertEqual(ov["ROOT"]["tag"], "v6-40-00")   # last wins
        self.assertEqual(ov["ROOT"]["source"], "orig")    # untouched field kept


class ArchTemplateTest(unittest.TestCase):
    """Configurable architecture layout: components, templates, and the
    order/separator-independent matching used for validation, docker, S3."""

    UBUNTU = ("ubuntu", "25.10", "")

    def _comp(self, processor="x86_64"):
        from bits_helpers.arch import arch_components
        return arch_components(True, [], self.UBUNTU, "Linux", processor)

    def test_components(self):
        c = self._comp()
        self.assertEqual(c, {"os": "ubuntu2510", "machine": "x86-64", "_machine": "x86_64"})

    def test_default_layout_unchanged(self):
        # The built-in template must reproduce today's string byte-for-byte.
        from bits_helpers.arch import doDetectArch, DEFAULT_ARCH_TEMPLATE, apply_arch_template
        self.assertEqual(DEFAULT_ARCH_TEMPLATE, "%(os)s_%(machine)s")
        self.assertEqual(doDetectArch(True, [], self.UBUNTU, "Linux", "x86_64"), "ubuntu2510_x86-64")
        self.assertEqual(apply_arch_template(DEFAULT_ARCH_TEMPLATE, self._comp()), "ubuntu2510_x86-64")

    def test_three_layouts(self):
        from bits_helpers.arch import apply_arch_template
        c = self._comp()
        self.assertEqual(apply_arch_template("%(os)s_%(machine)s", c), "ubuntu2510_x86-64")
        self.assertEqual(apply_arch_template("%(os)s_%(_machine)s", c), "ubuntu2510_x86_64")
        self.assertEqual(apply_arch_template("%(_machine)s-%(os)s", c), "x86_64-ubuntu2510")

    def test_literal_template_passthrough(self):
        from bits_helpers.arch import apply_arch_template
        self.assertEqual(apply_arch_template("ubuntu2510_x86-64", self._comp()), "ubuntu2510_x86-64")

    def test_bad_template_raises(self):
        from bits_helpers.arch import apply_arch_template
        with self.assertRaises(ValueError):
            apply_arch_template("%(nope)s", self._comp())

    def test_osx_components(self):
        from bits_helpers.arch import arch_components
        c = arch_components(False, [], ("", "", ""), "Darwin", "arm64")
        self.assertEqual(c["os"], "osx")
        self.assertEqual(c["machine"], "arm64")

    def test_tokens(self):
        from bits_helpers.arch import arch_distro_token, arch_machine_token
        self.assertEqual(arch_distro_token("x86_64-ubuntu2510"), "ubuntu2510")
        self.assertEqual(arch_distro_token("slc9_aarch64"), "slc9")
        self.assertEqual(arch_machine_token("ubuntu2510_x86_64"), "x86_64")
        self.assertEqual(arch_machine_token("ubuntu2510_x86-64"), "x86-64")
        self.assertIsNone(arch_distro_token("garbage123"))

    def test_normalise_arch_key_equivalence(self):
        from bits_helpers.arch import normalise_arch_key
        # underscore and dash machine forms collapse to the same key
        self.assertEqual(normalise_arch_key("ubuntu2404_x86_64"),
                         normalise_arch_key("ubuntu2404_x86-64"))
        # order does not matter for the (distro, machine) key
        self.assertEqual(normalise_arch_key("x86_64-ubuntu2404"),
                         normalise_arch_key("ubuntu2404_x86-64"))

    def test_matchValidArch_layouts(self):
        from bits_helpers.args import matchValidArch
        for a in ("ubuntu2510_x86-64", "ubuntu2510_x86_64", "x86_64-ubuntu2510",
                  "slc9_aarch64", "osx_arm64"):
            self.assertTrue(matchValidArch(a), a)
        self.assertFalse(matchValidArch("garbage123"))

    def test_cmdline_detection(self):
        from bits_helpers.args import _architecture_given_on_cmdline as g
        self.assertTrue(g(["bits", "build", "-a", "x", "pkg"]))
        self.assertTrue(g(["bits", "build", "--architecture", "x"]))
        self.assertTrue(g(["bits", "build", "--architecture=x"]))
        self.assertTrue(g(["bits", "build", "-ax86_64-ubuntu2510"]))
        # argparse accepts any unambiguous abbreviation of --architecture; the
        # detection must too, or a caller passing --arch has its architecture
        # silently overwritten by the defaults `architecture:` template.
        self.assertTrue(g(["bits", "build", "--arch", "x86_64-el10", "pkg"]))
        self.assertTrue(g(["bits", "build", "--arch=x86_64-el10"]))
        self.assertTrue(g(["bits", "build", "--archi", "x"]))
        self.assertFalse(g(["bits", "build", "--annotate", "pkg"]))   # not a prefix
        self.assertFalse(g(["bits", "build", "pkg"]))


class VersionMatcherTest(unittest.TestCase):
    """version<op> matchers, &&/|| combinators, and conditional patches."""

    ARCH = "ubuntu2510_x86-64-gcc15-dbg"

    def _m(self, matcher, version=None, default_vars=None):
        return _matcher_active(matcher, self.ARCH, ["dev4"], default_vars, version)

    def test_natural_version_compare(self):
        self.assertEqual(_version_compare("v40r2", "v40r4"), -1)
        self.assertEqual(_version_compare("v40r10", "v40r2"), 1)   # numeric, not lexical
        self.assertEqual(_version_compare("01.07", "01.10"), -1)
        self.assertEqual(_version_compare("v40r2", "v40r2"), 0)

    def test_separator_dash_dot_underscore_equivalent(self):
        # '-', '.', '_' are equivalent separators: dash- and dot-form tags
        # compare equal, and ordering is numeric across separators (the old
        # code ranked v6-40-00 below v6.36.99 because '-' < '.').
        self.assertEqual(_version_compare("v6-40-00", "v6.40.00"), 0)
        self.assertEqual(_version_compare("v6_40_00", "v6.40.00"), 0)
        self.assertEqual(_version_compare("v6-40-00", "v6.36.99"), 1)
        self.assertEqual(_version_compare("v6-38-00", "v6.40.00"), -1)
        # dash-form tag in a version>= matcher now gates correctly
        self.assertTrue(self._m("version>=v6.40.00", "v6-40-00"))
        self.assertFalse(self._m("version>=v6.40.00", "v6-38-00"))

    def test_version_operators(self):
        self.assertTrue(self._m("version=v40r2", "v40r2"))
        self.assertFalse(self._m("version=v40r2", "v40r4"))
        self.assertTrue(self._m("version!=v40r2", "v40r4"))
        self.assertTrue(self._m("version<v40r4", "v40r2"))
        self.assertFalse(self._m("version<v40r4", "v40r4"))
        self.assertTrue(self._m("version<=v40r4", "v40r4"))
        self.assertTrue(self._m("version>=v40r3", "v40r4"))
        self.assertFalse(self._m("version>v40r4", "v40r4"))

    def test_version_no_version_is_inactive(self):
        self.assertFalse(self._m("version=v40r2", None))

    def test_and_or_combinators(self):
        self.assertTrue(self._m("version>=v40r2 && version<v41r0", "v40r4"))
        self.assertFalse(self._m("version>=v40r2 && version<v40r4", "v40r4"))
        self.assertTrue(self._m("version<v40r0 || version>=v40r4", "v40r4"))
        # && binds tighter than ||
        self.assertTrue(self._m("(?!osx) && version>=v40r3 || version<v40r0", "v40r4"))

    def test_single_pipe_is_regex_alternation(self):
        # a lone | inside an arch regex is ordinary alternation, not OR
        self.assertTrue(self._m(".*gcc15.*|.*osx.*"))
        self.assertFalse(self._m(".*osx.*|.*arm64.*"))

    def test_parse_patch_entry(self):
        self.assertEqual(_parse_patch_entry("p.patch"), ("p.patch", None, ""))
        self.assertEqual(_parse_patch_entry("p.patch:version=v40r2"),
                         ("p.patch", "version=v40r2", ""))
        self.assertEqual(_parse_patch_entry("p.patch,sha256:abc"),
                         ("p.patch", None, ",sha256:abc"))
        self.assertEqual(_parse_patch_entry("p.patch:(?cuda),md5:x"),
                         ("p.patch", "(?cuda)", ",md5:x"))

    def test_filter_patches_strips_matcher_and_drops_inactive(self):
        pl = ["a.patch:version=v40r2", "b.patch", "c.patch:version>=v40r4,sha256:zz"]
        self.assertEqual(filterPatches(pl, self.ARCH, ["dev4"], None, "v40r2"),
                         ["a.patch", "b.patch"])
        self.assertEqual(filterPatches(pl, self.ARCH, ["dev4"], None, "v40r4"),
                         ["b.patch", "c.patch,sha256:zz"])


class TestResolveTag(unittest.TestCase):
    """`tag:` must accept the same variables as version:/source:/patches:."""

    def test_date_keywords_still_work(self):
        spec = {"package": "p", "tag": "nightly-%(year)s"}
        self.assertRegex(resolve_tag(spec), r"^nightly-\d{4}$")

    def test_spec_keys_still_win_over_nothing(self):
        spec = {"package": "p", "version": "v1", "tag": "rel-%(version)s"}
        self.assertEqual(resolve_tag(spec), "rel-v1")

    def test_defaults_profile_variables(self):
        """key4hep case: variables: release: main + overrides: tag: %(release)s."""
        spec = {"package": "lcg.bits", "tag": "%(release)s"}
        self.assertEqual(resolve_tag(spec, {"release": "main"}), "main")

    def test_recipe_variables_override_profile(self):
        spec = {"package": "p", "tag": "%(release)s",
                "variables": {"release": "dev4"}}
        self.assertEqual(resolve_tag(spec, {"release": "main"}), "dev4")

    def test_unknown_variable_still_fatal(self):
        spec = {"package": "p", "tag": "%(nope)s"}
        with patch("bits_helpers.utilities.dieOnError") as die:
            resolve_tag(spec, {"release": "main"})
        die.assert_called_once()
        self.assertIn("nope", die.call_args[0][1])
        # the message should help by listing what IS available
        self.assertIn("release", die.call_args[0][1])


if __name__ == '__main__':
    unittest.main()
