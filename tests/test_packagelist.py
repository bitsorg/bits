# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

from textwrap import dedent
import os
import unittest
from unittest import mock
from unittest.mock import patch
import tempfile

from shlex import quote

from bits_helpers.cmd import getstatusoutput
from bits_helpers.packages import getPackageList


RECIPES = {
    "CONFIG_DIR/defaults-release.sh": dedent("""\
    package: defaults-release
    version: v1
    force_rebuild: false
    ---
    """),
    "CONFIG_DIR/disable.sh": dedent("""\
    package: disable
    version: v1
    prefer_system: '.*'
    prefer_system_check: 'true'
    ---
    """),
    "CONFIG_DIR/with-replacement.sh": dedent("""\
    package: with-replacement
    version: v1
    prefer_system: '.*'
    prefer_system_check: |
        echo 'bits_system_replace: replacement'
    prefer_system_replacement_specs:
        replacement:
            env:
                SENTINEL_VAR: magic
    ---
    """),
    "CONFIG_DIR/with-replacement-recipe.sh": dedent("""\
    package: with-replacement-recipe
    version: v1
    prefer_system: '.*'
    prefer_system_check: |
        echo 'bits_system_replace: replacement'
    prefer_system_replacement_specs:
        replacement:
            recipe: 'true'
    ---
    """),
    "CONFIG_DIR/missing-spec.sh": dedent("""\
    package: missing-spec
    version: v1
    prefer_system: '.*'
    prefer_system_check: |
        echo 'bits_system_replace: missing_tag'
    prefer_system_replacement_specs: {}
    ---
    """),
    "CONFIG_DIR/pin-owner.sh": dedent("""\
    package: pin-owner
    version: "1"
    requires:
      - pin-dep = 2.0
      - pin-ovr
    ---
    """),
    "CONFIG_DIR/pin-dep.sh": dedent("""\
    package: pin-dep
    version: "1.0"
    ---
    """),
    "CONFIG_DIR/pin-ovr.sh": dedent("""\
    package: pin-ovr
    version: "1.0"
    ---
    """),
    "CONFIG_DIR/sys-libxml2.sh": dedent("""\
    package: sys-libxml2
    version: "2.10.4"
    prefer_system: '.*'
    prefer_system_check: |
        echo 2.9.13
    references:
      - cpe:2.3:a:xmlsoft:libxml2:%(version)s
    ---
    """),
    "CONFIG_DIR/sys-marked.sh": dedent("""\
    package: sys-marked
    version: "1"
    sbom: tool
    prefer_system: '.*'
    prefer_system_check: |
        echo "found it, see 3.1 notes"; echo 'bits_system_version: 7.4.0'
    ---
    """),
    "CONFIG_DIR/sentinel-command.sh": dedent("""\
    package: sentinel-command
    version: v1
    prefer_system: '.*'
    prefer_system_check: |
        : magic sentinel command
    ---
    """),
    "CONFIG_DIR/sbom-top.sh": dedent("""\
    package: sbom-top
    version: v1
    requires:
      - sbom-a
      - sbom-b
    ---
    """),
    "CONFIG_DIR/sbom-a.sh": dedent("""\
    package: sbom-a
    version: v1
    requires:
      - disable
      - sbom-c
    ---
    """),
    "CONFIG_DIR/sbom-b.sh": dedent("""\
    package: sbom-b
    version: v1
    build_requires:
      - disable
    ---
    """),
    "CONFIG_DIR/sbom-c.sh": dedent("""\
    package: sbom-c
    version: v1
    requires:
      - disable
    ---
    """),
    "CONFIG_DIR/sbom-sysreq.sh": dedent("""\
    package: sbom-sysreq
    version: v1
    system_requirement: '.*'
    system_requirement_check: 'true'
    ---
    """),
    "CONFIG_DIR/sbom-uses-sysreq.sh": dedent("""\
    package: sbom-uses-sysreq
    version: v1
    requires:
      - sbom-sysreq
    ---
    """),
    "CONFIG_DIR/force-rebuild.sh": dedent("""\
    package: force-rebuild
    version: v1
    force_rebuild: true
    ---
    """),
    "CONFIG_DIR/dirty_prefer_system_check.sh": dedent("""\
    package: dirty_prefer_system_check
    version: v1
    prefer_system: .*
    prefer_system_check: |
      pwd > HEREE
      exit 0
    ---
    """),
}

class MockReader:
    def __init__(self, url, dist=None, genPackages=None):
        self._contents = RECIPES[url]
        self.url = "mock://" + url

    def __call__(self):
        return self._contents


def getPackageListWithDefaults(packages, force_rebuild=(), satisfied=None, system_info=None,
                               overrides=None, defaults_meta=None):
    specs = {}   # getPackageList will mutate this
    def performPreferCheckWithTempDir(pkg, cmd):
      with tempfile.TemporaryDirectory(prefix=f"bits_prefer_check_{pkg['package']}_") as temp_dir:
        return getstatusoutput(cmd, cwd=temp_dir)
    return_values = getPackageList(
        packages=packages,
        specs=specs,
        configDir="CONFIG_DIR",
        # Make sure getPackageList considers prefer_system_check.
        # (Even with preferSystem=False + noSystem=None, it is sufficient
        # if the prefer_system regex matches the architecture.)
        preferSystem=True,
        noSystem=None,
        architecture="ARCH",
        disable=[],
        defaults=["release"],
        # Mock recipes just run "echo" or ":", so this is safe.
        performPreferCheck=performPreferCheckWithTempDir,
        performRequirementCheck=performPreferCheckWithTempDir,
        performValidateDefaults=lambda spec: (True, "", ["release"]),
        overrides=dict({"defaults-release": {}}, **(overrides or {})),
        defaults_meta=defaults_meta,
        taps={},
        log=lambda *_: None,
        force_rebuild=force_rebuild,
        satisfied_requirements=satisfied,
        system_info=system_info,
    )
    return (specs, *return_values)


@mock.patch("bits_helpers.packages.getRecipeReader", new=MockReader)
@mock.patch("bits_helpers.paths.exists", new=lambda f: f in RECIPES)
class ReplacementTestCase(unittest.TestCase):
    """Test that system package replacements are working."""

    def test_disable(self):
        """Check that not specifying any replacement disables the package.

        This is was the only available behaviour in previous bits versions
        and must be preserved for backward compatibility.
        """
        specs, systemPkgs, ownPkgs, failedReqs, validDefaults = \
            getPackageListWithDefaults(["disable"])
        self.assertIn("disable", systemPkgs)
        self.assertNotIn("disable", ownPkgs)
        self.assertNotIn("disable", specs)

    def test_unfiltered_requires_keep_system_edges(self):
        """Every dependant keeps its edge to a system package for the SBOM,
        however late it is read (the filtered requires lose it)."""
        specs, systemPkgs, _, _, _ = getPackageListWithDefaults(["sbom-top"])
        self.assertIn("disable", systemPkgs)
        # sbom-c is read after "disable" was found on the system: its filtered
        # requires have already lost it.
        self.assertNotIn("disable", specs["sbom-c"]["runtime_requires"])
        for pkg in ("sbom-a", "sbom-c"):
            self.assertIn("disable", specs[pkg]["unfiltered_requires"]["runtime"], pkg)
        self.assertIn("disable", specs["sbom-b"]["unfiltered_requires"]["build"])

    def _package_managers(self, rpm="", dpkg=""):
        """Fake rpm and dpkg-query on PATH that answer *rpm* / *dpkg* for any name
        ending in -libs, and log every call."""
        bindir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, bindir, True)
        for tool, answer in (("rpm", rpm), ("dpkg-query", dpkg)):
            with open(os.path.join(bindir, tool), "w") as f:
                f.write('#!/bin/sh\nfor a; do last=$a; done\n'
                        'echo "$last" >> "%s/calls"\n' % bindir +
                        'case "$last" in *-libs) printf "%%s\\n" "%s";; *) echo "package $last is not installed"; exit 1;; esac\n'
                        % answer)
            os.chmod(os.path.join(bindir, tool), 0o755)
        patcher = mock.patch.dict(os.environ, PATH=bindir + os.pathsep + os.environ["PATH"])
        patcher.start()
        self.addCleanup(patcher.stop)
        return os.path.join(bindir, "calls")

    def test_system_versions_come_from_the_checks(self):
        self._package_managers()
        info = {}
        getPackageListWithDefaults(["sys-libxml2", "sys-marked", "disable"], system_info=info)
        self.assertEqual(info["sys-libxml2"], {"version": "2.9.13",
                         "references": ["cpe:2.3:a:xmlsoft:libxml2:2.9.13:*:*:*:*:*:*:*"]})
        self.assertEqual(info["sys-marked"]["version"], "7.4.0")
        self.assertEqual(info["sys-marked"]["sbom"], "tool")                   # sbom: tool
        self.assertEqual(info["disable"], {"version": "", "references": []})   # it said nothing

    def test_the_package_manager_is_asked_first(self):
        calls = self._package_managers(
            rpm="rpm libxml2 (none) 2.9.13 6.el9_4 x86_64 libxml2-2.9.13-6.el9_4.src.rpm")
        info = {}
        getPackageListWithDefaults(["sys-libxml2", "disable"], system_info=info)
        self.assertEqual(info["sys-libxml2"]["version"], "2.9.13-6.el9_4")
        self.assertRegex(info["sys-libxml2"]["references"][0],
                         r"^pkg:rpm/[^/]+/libxml2@2\.9\.13-6\.el9_4\?arch=x86_64(&distro=[^&]+)?"
                         r"&upstream=libxml2-2\.9\.13-6\.el9_4\.src\.rpm$")
        with open(calls) as f:                       # one query, for both packages
            self.assertEqual(sorted(ln.strip() for ln in f), sorted(
                ["sys-libxml2", "sys-libxml2-libs", "libsys-libxml2", "disable", "disable-libs",
                 "libdisable"] * 2))

    def test_dpkg_is_asked_too_and_held_packages_count(self):
        self._package_managers(dpkg="hi deb sys-marked - 1:7.4.0-2 - amd64 marked@1:7.4.0-2")
        info = {}
        getPackageListWithDefaults(["sys-marked"], system_info=info)
        self.assertEqual((info["sys-marked"]["version"], info["sys-marked"]["sbom"]), ("1:7.4.0-2", "tool"))
        self.assertRegex(info["sys-marked"]["references"][0],
                         r"^pkg:deb/[^/]+/sys-marked@1:7\.4\.0-2\?arch=amd64(&distro=[^&]+)?&upstream=marked$")

    def test_who_pinned_a_version_is_recorded(self):
        specs, *_ = getPackageListWithDefaults(
            ["pin-owner"], overrides={"pin-ovr": {"version": "3.0"}},
            defaults_meta={"_version_profiles": {"pin-ovr": "lcg"}})
        self.assertEqual((specs["pin-dep"]["version"], specs["pin-dep"]["pinned_by"]), ("2.0", "requires:pin-owner"))
        self.assertEqual((specs["pin-ovr"]["version"], specs["pin-ovr"]["pinned_by"]), ("3.0", "defaults:lcg"))
        self.assertNotIn("pinned_by", specs["pin-owner"])

    def test_satisfied_system_requirements_are_collected(self):
        satisfied = set()
        specs, systemPkgs, _, failed, _ = getPackageListWithDefaults(["sbom-uses-sysreq"],
                                                                     satisfied=satisfied)
        self.assertEqual(satisfied, {"sbom-sysreq"})
        self.assertNotIn("sbom-sysreq", systemPkgs)      # not reported as prefer_system
        self.assertIn("sbom-sysreq", specs["sbom-uses-sysreq"]["unfiltered_requires"]["runtime"])

    def test_replacement_given(self):
        """Check that specifying a replacement spec means it is used.

        This also checks that if no recipe is given, we report the package as
        a system package to the user.
        """
        specs, systemPkgs, ownPkgs, failedReqs, validDefaults = \
            getPackageListWithDefaults(["with-replacement"])
        self.assertIn("with-replacement", specs)
        self.assertEqual(specs["with-replacement"]["env"]["SENTINEL_VAR"], "magic")
        # Make sure nothing is run by default.
        self.assertEqual(specs["with-replacement"]["recipe"], "")
        # If the replacement spec has no recipe, report to the user that we're
        # taking the package from the system.
        self.assertIn("with-replacement", systemPkgs)
        self.assertNotIn("with-replacement", ownPkgs)

    def test_replacement_recipe_given(self) -> None:
        """Check that specifying a replacement recipe means it is used.

        Also check that we report to the user that a package will be compiled
        when a replacement recipe is given.
        """
        specs, systemPkgs, ownPkgs, failedReqs, validDefaults = \
            getPackageListWithDefaults(["with-replacement-recipe"])
        self.assertIn("with-replacement-recipe", specs)
        self.assertIn("recipe", specs["with-replacement-recipe"])
        self.assertEqual("true", specs["with-replacement-recipe"]["recipe"])
        # The replacement must carry pkgdir from the original spec, otherwise
        # doBuild raises KeyError: 'pkgdir' when building it (e.g. a Homebrew
        # shim selected on macOS).
        self.assertIn("pkgdir", specs["with-replacement-recipe"])
        # If the replacement spec has a recipe, report to the user that we're
        # compiling the package.
        self.assertNotIn("with-replacement-recipe", systemPkgs)
        self.assertIn("with-replacement-recipe", ownPkgs)

    @mock.patch("bits_helpers.packages.warning")
    def test_missing_replacement_spec(self, mock_warning) -> None:
        """Check a warning is displayed when the replacement spec is not found."""
        warning_msg = "falling back to building the package ourselves"
        warning_exists = False
        def side_effect(msg, *args, **kwargs):
            nonlocal warning_exists
            if warning_msg in str(msg):
              warning_exists = True
        mock_warning.side_effect = side_effect
        specs, systemPkgs, ownPkgs, failedReqs, validDefaults = \
            getPackageListWithDefaults(["missing-spec"])
        self.assertTrue(warning_exists)

    def test_dirty_system_check(self) -> None:
        """Check that prefer_system_check runs in isolation and doesn't create files in cwd."""
        def fake_exists(n):
            return n in RECIPES.keys()
        with patch.object(os.path, "exists", fake_exists):
            getPackageListWithDefaults(["dirty_prefer_system_check"])
            # can't use os.path.exists() ourselves, as we just mocked it
            self.assertFalse("HEREE" in os.listdir())


@mock.patch("bits_helpers.packages.getRecipeReader", new=MockReader)
@mock.patch("bits_helpers.paths.exists", new=lambda f: f in RECIPES)
class ForceRebuildTestCase(unittest.TestCase):
    """Test that force_rebuild keys are applied properly."""

    def test_force_rebuild_recipe(self) -> None:
        """If the recipe specifies force_rebuild, it must be applied."""
        specs, _, _, _, _ = getPackageListWithDefaults(["force-rebuild"])
        self.assertTrue(specs["force-rebuild"]["force_rebuild"])
        self.assertFalse(specs["defaults-release"]["force_rebuild"])

    def test_force_rebuild_command_line(self) -> None:
        """The --force-rebuild option must take precedence, if given."""
        specs, _, _, _, _ = getPackageListWithDefaults(
            ["force-rebuild"], force_rebuild=["defaults-release", "force-rebuild"],
        )
        self.assertTrue(specs["force-rebuild"]["force_rebuild"])
        self.assertTrue(specs["defaults-release"]["force_rebuild"])




RECIPES["CONFIG_DIR/pinned.sh"] = dedent("""\
    package: pinned
    version: v1
    tag: v1
    source: https://example.com/pinned.git
    ---
    """)


@mock.patch("bits_helpers.packages.getRecipeReader", new=MockReader)
@mock.patch("bits_helpers.paths.exists", new=lambda f: f in RECIPES)
class ChecksumStoreTestCase(unittest.TestCase):
    """The checksum files are read after the overrides; a legacy pin needs the
    recipe's own tag."""

    STORE = {"tag": "a" * 40, "commits": {"v2": "b" * 40}, "sources": {}, "patches": {}}

    def _specs(self, overrides):
        specs = {}
        with patch("bits_helpers.packages.load_for_spec", return_value=self.STORE) as load:
            getPackageList(packages=["pinned"], specs=specs, configDir="CONFIG_DIR",
                           preferSystem=False, noSystem=None, architecture="ARCH",
                           disable=[], defaults=["release"],
                           performPreferCheck=lambda *_: (1, ""),
                           performRequirementCheck=lambda *_: (1, ""),
                           performValidateDefaults=lambda spec: (True, "", ["release"]),
                           overrides=dict({"defaults-release": {}}, **overrides), taps={},
                           log=lambda *_: None,
                           defaults_meta={"_defaults_dirs": {"release": "STACK_DIR"}})
        extra = [c.args[1] for c in load.call_args_list if c.args[0]["package"] == "pinned"]
        return specs["pinned"], extra

    def test_legacy_pin_kept_for_recipe_tag(self):
        spec, extra = self._specs({})
        self.assertEqual(spec["pin_commit"], "a" * 40)
        self.assertEqual(spec["pin_commits"], {"v2": "b" * 40})
        self.assertEqual(extra, [["STACK_DIR"]])

    def test_legacy_pin_dropped_when_overridden(self):
        spec, _ = self._specs({"pinned": {"tag": "v2"}})
        self.assertEqual(spec["tag"], "v2")
        self.assertIsNone(spec["pin_commit"])
        self.assertEqual(spec["pin_commits"], {"v2": "b" * 40})


@mock.patch("bits_helpers.packages.getRecipeReader", new=MockReader)
@mock.patch("bits_helpers.paths.exists", new=lambda f: f in RECIPES)
class ChecksumsDirTestCase(ChecksumStoreTestCase):
    """A package whose sources a profile override changed records new checksums
    in that profile's repository."""

    def _dir(self, overrides):
        specs = {}
        with patch("bits_helpers.packages.load_for_spec", return_value=self.STORE):
            getPackageList(packages=["pinned"], specs=specs, configDir="CONFIG_DIR",
                           preferSystem=False, noSystem=None, architecture="ARCH",
                           disable=[], defaults=["release"],
                           performPreferCheck=lambda *_: (1, ""),
                           performRequirementCheck=lambda *_: (1, ""),
                           performValidateDefaults=lambda spec: (True, "", ["release"]),
                           overrides=dict({"defaults-release": {}}, **overrides), taps={},
                           log=lambda *_: None,
                           defaults_meta={"_override_dirs": {"pinned": "STACK_DIR"}})
        return specs["pinned"].get("checksums_dir")

    def test_source_override_sets_the_dir(self):
        self.assertEqual(self._dir({"pinned": {"tag": "v2"}}), "STACK_DIR")

    def test_other_override_does_not(self):
        self.assertIsNone(self._dir({"pinned": {"env": {"X": "1"}}}))
        self.assertIsNone(self._dir({}))


class SystemVersionTestCase(unittest.TestCase):
    def test_a_version_line_or_the_marker(self):
        from bits_helpers.packages import system_version
        self.assertEqual(system_version("2.9.13\n"), "2.9.13")
        self.assertEqual(system_version("cmake version 3.26.5\n3.26.5"), "3.26.5")
        self.assertEqual(system_version("bits_system_version: 1.2\n9.9.9"), "1.2")
        self.assertEqual(system_version("OpenSSL 3.0.7 1 Nov 2022"), "")      # not only a version
        self.assertEqual(system_version(""), "")
        self.assertEqual(system_version("bits_system_version:\nv2.1"), "2.1")   # no value: the line after

    def test_the_distribution_packages(self):
        from bits_helpers.packages import distro_packages, _query_names, _system_query
        self.assertEqual(distro_packages(
            "== zlib\npackage zlib is not installed\nrpm zlib 1 1.2.11 41.el9 x86_64 zlib-1.2.11-41.el9.src.rpm\n"
            "rpm zlib-libs 1 1.2.11 41.el9 x86_64 zlib-1.2.11-41.el9.src.rpm\n== none\n"
            "== libxml2\ndeb libxml2 - 1:2.9.14+dfsg-1.3 - amd64 libxml2@1:2.9.14+dfsg-1.3\nos debian 12\n"), {
            "zlib": ("1:1.2.11-41.el9", "pkg:rpm/debian/zlib@1.2.11-41.el9?arch=x86_64&distro=debian-12"
                     "&epoch=1&upstream=zlib-1.2.11-41.el9.src.rpm"),
            "libxml2": ("1:2.9.14+dfsg-1.3", "pkg:deb/debian/libxml2@1:2.9.14+dfsg-1.3?arch=amd64&distro=debian-12")})
        self.assertEqual(distro_packages("== libssl3\ndeb libssl3 - 3.0.13-0u3 - amd64 openssl@3.0.13-0u3\n"),
                         {"libssl3": ("3.0.13-0u3", "pkg:deb/debian/libssl3@3.0.13-0u3?arch=amd64&upstream=openssl")})
        self.assertEqual(distro_packages("== a\ndeb a - 2-1 - all b@1-1\n")["a"][1],
                         "pkg:deb/debian/a@2-1?arch=all&upstream=b%401-1")
        self.assertEqual(distro_packages("os ubuntu 24.04\n"), {})
        self.assertEqual(distro_packages("== a\ndeb a - 1 - all\nos debian \n")["a"][1],
                         "pkg:deb/debian/a@1?arch=all")                       # sid: no VERSION_ID
        self.assertEqual(_query_names({"package": "OpenSSL"}), ["OpenSSL", "openssl", "openssl-libs", "libopenssl"])
        self.assertEqual(_query_names({"package": "zlib", "system_package": "zlib1g"}), ["zlib1g"])
        self.assertIn("echo %s\nq zlib1g\n" % quote("== it's"),
                      _system_query([{"package": "it's", "system_package": "zlib1g"}]))

if __name__ == '__main__':
    unittest.main()
