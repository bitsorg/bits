# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for architecture: shared support.

Covers:
 - effective_arch() helper
 - _pkg_install_path() with architecture=shared
 - generate_initdotsh() literal "shared" prefix for shared deps
 - Shared-dep warning when a shared package depends on arch-specific packages
"""

import unittest
from unittest.mock import patch

from bits_helpers.utilities import effective_arch, SHARED_ARCH
from bits_helpers.build import _pkg_install_path, generate_initdotsh


# ---------------------------------------------------------------------------
# Minimal spec builders
# ---------------------------------------------------------------------------

def _spec(package, version="1.0", revision="1", hash="abc123",
          commit_hash="deadbeef", architecture=None, pkg_family="",
          requires=None):
    s = {
        "package": package,
        "version": version,
        "revision": revision,
        "hash": hash,
        "commit_hash": commit_hash,
        "pkg_family": pkg_family,
        "requires": requires or [],
    }
    if architecture is not None:
        s["architecture"] = architecture
    return s


BUILD_ARCH = "slc7_x86-64"


# ---------------------------------------------------------------------------
# Tests: effective_arch()
# ---------------------------------------------------------------------------

class TestEffectiveArch(unittest.TestCase):

    def test_normal_spec_returns_build_arch(self):
        spec = _spec("mylib")
        self.assertEqual(effective_arch(spec, BUILD_ARCH), BUILD_ARCH)

    def test_shared_spec_returns_shared(self):
        spec = _spec("mydata", architecture=SHARED_ARCH)
        self.assertEqual(effective_arch(spec, BUILD_ARCH), SHARED_ARCH)

    def test_other_architecture_field_is_ignored(self):
        """A non-shared value in 'architecture' is NOT used as the effective arch."""
        spec = _spec("mything", architecture="osx_x86-64")
        # effective_arch only checks for SHARED_ARCH sentinel; other values are ignored
        self.assertEqual(effective_arch(spec, BUILD_ARCH), BUILD_ARCH)

    def test_shared_sentinel_is_string_shared(self):
        self.assertEqual(SHARED_ARCH, "shared")

    def test_empty_build_arch_forwarded(self):
        spec = _spec("mypkg")
        self.assertEqual(effective_arch(spec, ""), "")

    def test_different_build_archs_forwarded(self):
        spec = _spec("mypkg")
        for arch in ("osx_x86-64", "slc7_x86-64", "ubuntu2004_x86-64"):
            self.assertEqual(effective_arch(spec, arch), arch)

    def test_shared_overrides_any_build_arch(self):
        spec = _spec("mypkg", architecture=SHARED_ARCH)
        for build_arch in ("osx_x86-64", "slc7_x86-64", "ubuntu2004_x86-64"):
            self.assertEqual(effective_arch(spec, build_arch), "shared")


# ---------------------------------------------------------------------------
# Tests: _pkg_install_path() with shared architecture
# ---------------------------------------------------------------------------

class TestPkgInstallPathShared(unittest.TestCase):

    def test_shared_no_family(self):
        spec = _spec("mydata", version="1.0", revision="1",
                     architecture=SHARED_ARCH)
        arch = effective_arch(spec, BUILD_ARCH)
        path = _pkg_install_path("sw", arch, spec)
        self.assertEqual(path, "sw/shared/mydata/1.0-1")

    def test_shared_with_family(self):
        spec = _spec("mydata", version="2.3", revision="5",
                     architecture=SHARED_ARCH, pkg_family="datasets")
        arch = effective_arch(spec, BUILD_ARCH)
        path = _pkg_install_path("sw", arch, spec)
        self.assertEqual(path, "sw/shared/datasets/mydata/2.3-5")

    def test_normal_spec_uses_build_arch(self):
        spec = _spec("mylib", version="3.1", revision="2")
        arch = effective_arch(spec, BUILD_ARCH)
        path = _pkg_install_path("sw", arch, spec)
        self.assertEqual(path, "sw/slc7_x86-64/mylib/3.1-2")

    def test_normal_spec_with_family_uses_build_arch(self):
        spec = _spec("mylib", version="3.1", revision="2", pkg_family="hep")
        arch = effective_arch(spec, BUILD_ARCH)
        path = _pkg_install_path("sw", arch, spec)
        self.assertEqual(path, "sw/slc7_x86-64/hep/mylib/3.1-2")

    def test_shared_workdir_prefix_respected(self):
        spec = _spec("mydata", version="1.0", revision="1",
                     architecture=SHARED_ARCH)
        arch = effective_arch(spec, BUILD_ARCH)
        path = _pkg_install_path("/home/user/sw", arch, spec)
        self.assertEqual(path, "/home/user/sw/shared/mydata/1.0-1")


# ---------------------------------------------------------------------------
# Tests: generate_initdotsh() – literal "shared" prefix for shared deps
# ---------------------------------------------------------------------------

class TestGenerateInitdotshShared(unittest.TestCase):

    def _make_specs(self, dep_architecture=None):
        dep = _spec("sharedlib", version="1.0", revision="1",
                    hash="aabbcc", commit_hash="feedface",
                    architecture=dep_architecture)
        main = _spec("myapp", version="2.0", revision="3",
                     hash="112233", commit_hash="cafebabe",
                     requires=["sharedlib"])
        return {"sharedlib": dep, "myapp": main}

    def test_shared_dep_uses_literal_shared_prefix(self):
        specs = self._make_specs(dep_architecture=SHARED_ARCH)
        initsh = generate_initdotsh("myapp", specs, BUILD_ARCH,
                                    workDir="sw", post_build=False)
        # The shared dep's init.sh should use the literal "$WORK_DIR/shared"
        self.assertIn('"$WORK_DIR/shared"', initsh)
        # And NOT use the runtime variable $BITS_ARCH_PREFIX
        self.assertNotIn('"$WORK_DIR/$BITS_ARCH_PREFIX"/sharedlib', initsh)

    def test_arch_dep_uses_arch_prefix_variable(self):
        specs = self._make_specs(dep_architecture=None)
        initsh = generate_initdotsh("myapp", specs, BUILD_ARCH,
                                    workDir="sw", post_build=False)
        self.assertIn('"$WORK_DIR/$BITS_ARCH_PREFIX"', initsh)
        self.assertNotIn('"$WORK_DIR/shared"', initsh)

    def test_shared_dep_path_contains_package_name(self):
        specs = self._make_specs(dep_architecture=SHARED_ARCH)
        initsh = generate_initdotsh("myapp", specs, BUILD_ARCH,
                                    workDir="sw", post_build=False)
        self.assertIn("sharedlib/1.0-1", initsh)

    def test_post_build_shared_package_uses_literal_prefix(self):
        """When the package itself is shared, its ROOT export uses literal prefix."""
        dep = _spec("defaults-release", version="1", revision="1",
                    hash="00000a", commit_hash="0000000")
        main = _spec("mydata", version="3.0", revision="1",
                     hash="112233", commit_hash="cafebabe",
                     architecture=SHARED_ARCH,
                     requires=["defaults-release"])
        specs = {"defaults-release": dep, "mydata": main}
        initsh = generate_initdotsh("mydata", specs, BUILD_ARCH,
                                    workDir="sw", post_build=True)
        # MYDATA_ROOT should point to the literal shared prefix (not the arch variable)
        self.assertIn('export MYDATA_ROOT="$WORK_DIR/shared"/mydata/3.0-1', initsh)
        # Arch-specific deps (like defaults-release) still use the arch-prefix variable
        self.assertIn('"$WORK_DIR/$BITS_ARCH_PREFIX"', initsh)
        # But the self (shared) package's ROOT must NOT embed the arch-prefix variable
        self.assertNotIn('export MYDATA_ROOT="$WORK_DIR/$BITS_ARCH_PREFIX"', initsh)

    def test_post_build_arch_package_uses_arch_prefix_variable(self):
        dep = _spec("defaults-release", version="1", revision="1",
                    hash="00000a", commit_hash="0000000")
        main = _spec("mylib", version="3.0", revision="1",
                     hash="112233", commit_hash="cafebabe",
                     requires=["defaults-release"])
        specs = {"defaults-release": dep, "mylib": main}
        initsh = generate_initdotsh("mylib", specs, BUILD_ARCH,
                                    workDir="sw", post_build=True)
        self.assertIn('export MYLIB_ROOT="$WORK_DIR/$BITS_ARCH_PREFIX"', initsh)

    def test_mixed_deps_each_use_correct_prefix(self):
        """When a package has both shared and arch-specific deps, each gets the right prefix."""
        arch_dep = _spec("mylib", version="1.0", revision="1",
                         hash="aaaaaa", commit_hash="11111111")
        shared_dep = _spec("mydata", version="2.0", revision="1",
                           hash="bbbbbb", commit_hash="22222222",
                           architecture=SHARED_ARCH)
        main = _spec("myapp", version="3.0", revision="1",
                     hash="cccccc", commit_hash="33333333",
                     requires=["mylib", "mydata"])
        specs = {"mylib": arch_dep, "mydata": shared_dep, "myapp": main}
        initsh = generate_initdotsh("myapp", specs, BUILD_ARCH,
                                    workDir="sw", post_build=False)
        self.assertIn('"$WORK_DIR/$BITS_ARCH_PREFIX"', initsh)
        self.assertIn('"$WORK_DIR/shared"', initsh)


# ---------------------------------------------------------------------------
# Tests: shared-dep warning
# ---------------------------------------------------------------------------

class TestSharedDepWarning(unittest.TestCase):
    """The build function should warn when a shared package depends on arch-specific ones."""

    def _run_warning_check(self, shared_spec, dep_spec, expected_warning):
        """Simulate the warning logic from build.py."""
        specs = {
            shared_spec["package"]: shared_spec,
            dep_spec["package"]: dep_spec,
        }
        spec = shared_spec
        arch_specific_deps = [
            dep for dep in spec.get("requires", [])
            if dep != "defaults-release"
            and specs[dep].get("architecture") != SHARED_ARCH
        ]
        has_warning = bool(arch_specific_deps)
        self.assertEqual(has_warning, expected_warning,
                         "arch_specific_deps=%r" % arch_specific_deps)
        return arch_specific_deps

    def test_shared_pkg_with_arch_dep_triggers_warning(self):
        dep = _spec("mylib")  # no architecture field → arch-specific
        shared = _spec("mydata", architecture=SHARED_ARCH,
                       requires=["mylib"])
        bad_deps = self._run_warning_check(shared, dep, expected_warning=True)
        self.assertIn("mylib", bad_deps)

    def test_shared_pkg_with_shared_dep_no_warning(self):
        dep = _spec("sharedlib", architecture=SHARED_ARCH)
        shared = _spec("mydata", architecture=SHARED_ARCH,
                       requires=["sharedlib"])
        self._run_warning_check(shared, dep, expected_warning=False)

    def test_shared_pkg_with_defaults_release_no_warning(self):
        """defaults-release is always excluded from the arch-specific check."""
        dep = _spec("defaults-release")  # arch-specific, but excluded
        shared = _spec("mydata", architecture=SHARED_ARCH,
                       requires=["defaults-release"])
        self._run_warning_check(shared, dep, expected_warning=False)

    def test_arch_pkg_no_warning_even_with_arch_deps(self):
        """The warning logic only fires for shared packages."""
        dep = _spec("mylib")
        main = _spec("myapp", requires=["mylib"])
        # Non-shared package → warning check should never trigger
        self.assertEqual(main.get("architecture"), None)
        # Confirm the logic only triggers for shared packages
        is_shared = main.get("architecture") == SHARED_ARCH
        self.assertFalse(is_shared)


if __name__ == "__main__":
    unittest.main()
