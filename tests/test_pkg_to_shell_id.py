# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for pkg_to_shell_id() and its integration into generate_initdotsh().

Covers:
 - pkg_to_shell_id(): all character classes (dash, dot, other punctuation)
 - resolve_spec_data(): root_dir template uses sanitised name
 - generate_initdotsh(): guard variable and export names use sanitised name
 - Backward compatibility: plain dash-only names unchanged
"""

import unittest
from bits_helpers.utilities import pkg_to_shell_id
from bits_helpers.initdotsh import generate_initdotsh


# ---------------------------------------------------------------------------
# pkg_to_shell_id()
# ---------------------------------------------------------------------------

class TestPkgToShellId(unittest.TestCase):

    # ── Basic transformations ──────────────────────────────────────────────

    def test_plain_letters_uppercased(self):
        self.assertEqual(pkg_to_shell_id("zlib"), "ZLIB")

    def test_dash_becomes_underscore(self):
        """Dashes are the common case — must work identically to the old code."""
        self.assertEqual(pkg_to_shell_id("GCC-Toolchain"), "GCC_TOOLCHAIN")

    def test_dot_becomes_underscore(self):
        self.assertEqual(pkg_to_shell_id("common.bits"), "COMMON_BITS")

    def test_multiple_dots(self):
        self.assertEqual(pkg_to_shell_id("o2.framework.extra"), "O2_FRAMEWORK_EXTRA")

    def test_dot_and_dash_mixed(self):
        self.assertEqual(pkg_to_shell_id("my-pkg.v2"), "MY_PKG_V2")

    def test_digits_preserved(self):
        self.assertEqual(pkg_to_shell_id("gcc13"), "GCC13")

    def test_underscore_preserved(self):
        """Underscores are already valid — must pass through unchanged."""
        self.assertEqual(pkg_to_shell_id("my_pkg"), "MY_PKG")

    def test_already_upper(self):
        self.assertEqual(pkg_to_shell_id("ZLIB"), "ZLIB")

    def test_at_sign_becomes_underscore(self):
        """Any non-alphanumeric-or-underscore char is sanitised."""
        self.assertEqual(pkg_to_shell_id("pkg@v2"), "PKG_V2")

    def test_plus_becomes_underscore(self):
        self.assertEqual(pkg_to_shell_id("c++"), "C__")

    def test_consecutive_separators(self):
        """Consecutive separators each become their own underscore."""
        self.assertEqual(pkg_to_shell_id("a..b"), "A__B")

    # ── Backward compatibility ─────────────────────────────────────────────

    def test_backwards_compat_dash(self):
        """Must produce the same result as the old upper().replace('-','_')."""
        old_style = lambda n: n.upper().replace("-", "_")
        for name in ["zlib", "GCC-Toolchain", "AliRoot", "defaults-release",
                     "O2Physics", "XRootD"]:
            self.assertEqual(pkg_to_shell_id(name), old_style(name),
                             "Regression for package %r" % name)

    # ── Result is always a valid shell identifier ──────────────────────────

    def test_result_contains_only_valid_chars(self):
        import re
        for name in ["common.bits", "my-pkg.v2", "c++", "a@b", "x.y.z"]:
            result = pkg_to_shell_id(name)
            self.assertRegex(result, r'^[A-Z0-9_]+$',
                             "Result %r contains invalid shell identifier chars" % result)


# ---------------------------------------------------------------------------
# generate_initdotsh(): shell variable names for dotted package names
# ---------------------------------------------------------------------------

def _make_specs(package, dep_package=None):
    """Return a minimal specs dict for generate_initdotsh tests."""
    deps = []
    if dep_package:
        dep_spec = {
            "package": dep_package,
            "version": "1.0",
            "revision": "1",
            "hash": "aabbcc",
            "commit_hash": "deadbeef",
            "pkg_family": "",
            "requires": [],
            "full_requires": [],
            "prepend_path": {},
            "append_path": {},
            "set_env": {},
            "unset_env": [],
            "env": {},
        }
        deps = [dep_package]

    pkg_spec = {
        "package": package,
        "version": "2.0",
        "revision": "1",
        "hash": "112233",
        "commit_hash": "cafebabe",
        "pkg_family": "",
        "requires": deps,
        "full_requires": deps,
        "prepend_path": {},
        "append_path": {},
        "set_env": {},
        "unset_env": [],
        "env": {},
    }
    result = {package: pkg_spec}
    if dep_package:
        result[dep_package] = dep_spec
    return result


class TestGenerateInitdotshDotPackage(unittest.TestCase):
    """Package names with dots produce valid shell variable names in init.sh."""

    def test_export_root_uses_sanitised_name(self):
        specs = _make_specs("common.bits")
        initsh = generate_initdotsh("common.bits", specs, "slc7_x86-64",
                                    workDir="/sw", post_build=True)
        # Should contain COMMON_BITS_ROOT, not COMMON.BITS_ROOT
        self.assertIn("COMMON_BITS_ROOT", initsh)
        self.assertNotIn("COMMON.BITS_ROOT", initsh)

    def test_export_version_uses_sanitised_name(self):
        specs = _make_specs("common.bits")
        initsh = generate_initdotsh("common.bits", specs, "slc7_x86-64",
                                    workDir="/sw", post_build=True)
        self.assertIn("COMMON_BITS_VERSION", initsh)
        self.assertNotIn("COMMON.BITS_VERSION", initsh)

    def test_export_revision_uses_sanitised_name(self):
        specs = _make_specs("common.bits")
        initsh = generate_initdotsh("common.bits", specs, "slc7_x86-64",
                                    workDir="/sw", post_build=True)
        self.assertIn("COMMON_BITS_REVISION", initsh)
        self.assertNotIn("COMMON.BITS_REVISION", initsh)

    def test_export_hash_uses_sanitised_name(self):
        specs = _make_specs("common.bits")
        initsh = generate_initdotsh("common.bits", specs, "slc7_x86-64",
                                    workDir="/sw", post_build=True)
        self.assertIn("COMMON_BITS_HASH", initsh)
        self.assertNotIn("COMMON.BITS_HASH", initsh)

    def test_guard_variable_for_dotted_dep(self):
        """The guard [ -n "${DEP_REVISION}" ] must use a sanitised dep name."""
        specs = _make_specs("my-pkg", dep_package="common.bits")
        initsh = generate_initdotsh("my-pkg", specs, "slc7_x86-64",
                                    workDir="/sw", post_build=False)
        self.assertIn("COMMON_BITS_REVISION", initsh)
        self.assertNotIn("COMMON.BITS_REVISION", initsh)

    def test_path_uses_original_package_name(self):
        """Filesystem path in init.sh still uses the original dotted name."""
        specs = _make_specs("common.bits")
        initsh = generate_initdotsh("common.bits", specs, "slc7_x86-64",
                                    workDir="/sw", post_build=True)
        # The actual install path must contain the literal package name
        self.assertIn("common.bits/2.0-1", initsh)

    def test_dash_package_backward_compat(self):
        """Dash-only names are unaffected — no regression."""
        specs = _make_specs("GCC-Toolchain")
        initsh = generate_initdotsh("GCC-Toolchain", specs, "slc7_x86-64",
                                    workDir="/sw", post_build=True)
        self.assertIn("GCC_TOOLCHAIN_ROOT", initsh)
        self.assertNotIn("GCC-TOOLCHAIN_ROOT", initsh)
        self.assertIn("GCC-Toolchain/2.0-1", initsh)  # path uses original name


if __name__ == "__main__":
    unittest.main()
