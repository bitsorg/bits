"""Tests for the qualify_arch / append_arch defaults-file fields.

Covers:
 - compute_combined_arch() helper: all branching paths
   - Legacy qualify_arch behaviour (backward-compatible)
   - New per-default append_arch behaviour
 - Integration with effective_arch(): shared packages are unaffected
 - _pkg_install_path() with a combined architecture string
 - generate_initdotsh() BITS_ARCH_PREFIX default uses combined arch
"""

import unittest
from bits_helpers.utilities import compute_combined_arch, effective_arch, SHARED_ARCH
from bits_helpers.build import _pkg_install_path, generate_initdotsh


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _meta(**kw):
    """Return a minimal defaults-meta dict."""
    return dict(kw)


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


RAW_ARCH = "slc7_x86-64"


# ---------------------------------------------------------------------------
# Tests for compute_combined_arch()
# ---------------------------------------------------------------------------

class TestComputeCombinedArch(unittest.TestCase):

    # -- qualify_arch absent / false -------------------------------------------

    def test_no_flag_returns_raw(self):
        """Without qualify_arch the raw architecture is returned unchanged."""
        self.assertEqual(
            compute_combined_arch({}, ["release"], RAW_ARCH),
            RAW_ARCH,
        )

    def test_false_flag_returns_raw(self):
        self.assertEqual(
            compute_combined_arch({"qualify_arch": False}, ["dev", "gcc13"], RAW_ARCH),
            RAW_ARCH,
        )

    def test_zero_flag_returns_raw(self):
        """Falsy non-boolean values also disable qualification."""
        self.assertEqual(
            compute_combined_arch({"qualify_arch": 0}, ["dev"], RAW_ARCH),
            RAW_ARCH,
        )

    # -- qualify_arch true, release-only ---------------------------------------

    def test_release_only_returns_raw(self):
        """With qualify_arch but only the 'release' default, no suffix is added."""
        self.assertEqual(
            compute_combined_arch({"qualify_arch": True}, ["release"], RAW_ARCH),
            RAW_ARCH,
        )

    def test_empty_defaults_returns_raw(self):
        """Edge-case: empty defaults list → no suffix."""
        self.assertEqual(
            compute_combined_arch({"qualify_arch": True}, [], RAW_ARCH),
            RAW_ARCH,
        )

    # -- qualify_arch true, non-release defaults --------------------------------

    def test_single_non_release_default(self):
        self.assertEqual(
            compute_combined_arch({"qualify_arch": True}, ["dev"], RAW_ARCH),
            "slc7_x86-64-dev",
        )

    def test_two_defaults(self):
        self.assertEqual(
            compute_combined_arch({"qualify_arch": True}, ["dev", "gcc13"], RAW_ARCH),
            "slc7_x86-64-dev-gcc13",
        )

    def test_three_defaults(self):
        self.assertEqual(
            compute_combined_arch({"qualify_arch": True}, ["dev", "gcc13", "cuda"], RAW_ARCH),
            "slc7_x86-64-dev-gcc13-cuda",
        )

    def test_release_filtered_from_multi_defaults(self):
        """'release' is filtered out when mixed with other defaults."""
        self.assertEqual(
            compute_combined_arch({"qualify_arch": True}, ["release", "dev"], RAW_ARCH),
            "slc7_x86-64-dev",
        )

    def test_delimiter_is_hyphen(self):
        """Defaults components are joined with '-', not '_'."""
        result = compute_combined_arch({"qualify_arch": True}, ["aaa", "bbb"], RAW_ARCH)
        self.assertIn("-aaa-bbb", result)
        self.assertNotIn("_aaa", result)
        self.assertNotIn("_bbb", result)

    def test_different_base_arch(self):
        result = compute_combined_arch({"qualify_arch": True}, ["dev"], "osx_arm64")
        self.assertEqual(result, "osx_arm64-dev")

    def test_case_preserved(self):
        """Defaults component case is preserved exactly."""
        result = compute_combined_arch({"qualify_arch": True}, ["Dev", "GCC13"], RAW_ARCH)
        self.assertEqual(result, "slc7_x86-64-Dev-GCC13")

    # -- idempotency / no mutation ---------------------------------------------

    def test_does_not_mutate_defaults_list(self):
        defaults = ["dev", "gcc13"]
        compute_combined_arch({"qualify_arch": True}, defaults, RAW_ARCH)
        self.assertEqual(defaults, ["dev", "gcc13"])

    def test_does_not_mutate_meta(self):
        meta = {"qualify_arch": True}
        compute_combined_arch(meta, ["dev"], RAW_ARCH)
        self.assertEqual(meta, {"qualify_arch": True})


# ---------------------------------------------------------------------------
# Tests for the per-default append_arch mechanism
# ---------------------------------------------------------------------------

class TestAppendArch(unittest.TestCase):
    """Per-default append_arch: only explicit values are added to the arch."""

    # The append_arch value is appended VERBATIM -- no separator is assumed, so
    # the value carries its own ('-gcc13', '_gcc15') or none at all ('dbg').

    # -- single default with append_arch --------------------------------------

    def test_single_append_arch_uses_value_not_name(self):
        """append_arch value is appended, not the default name."""
        meta = {"_append_arch_qualifiers": ["-gcc13"]}
        self.assertEqual(
            compute_combined_arch(meta, ["gcc13-defaults"], RAW_ARCH),
            "slc7_x86-64-gcc13",
        )

    def test_append_arch_value_differs_from_default_name(self):
        """The value in append_arch is used verbatim, even if different from
        the default filename."""
        meta = {"_append_arch_qualifiers": ["-opt-lto"]}
        self.assertEqual(
            compute_combined_arch(meta, ["optimised"], RAW_ARCH),
            "slc7_x86-64-opt-lto",
        )

    # -- chained defaults, selective opt-in -----------------------------------

    def test_only_defaults_with_append_arch_contribute(self):
        """Defaults without append_arch are transparent to the arch suffix."""
        # defaults chain: release::gcc13::cuda
        # only gcc13 and cuda have append_arch; release does not
        meta = {"_append_arch_qualifiers": ["-gcc13", "-cuda"]}
        self.assertEqual(
            compute_combined_arch(meta, ["release", "gcc13", "cuda"], RAW_ARCH),
            "slc7_x86-64-gcc13-cuda",
        )

    def test_single_opt_in_among_many(self):
        """Only one default in a long chain opts in."""
        meta = {"_append_arch_qualifiers": ["-mpi"]}
        self.assertEqual(
            compute_combined_arch(meta, ["release", "gcc13", "mpi"], RAW_ARCH),
            "slc7_x86-64-mpi",
        )

    def test_order_follows_chain_order(self):
        """Qualifiers appear in the same order as the defaults chain."""
        meta = {"_append_arch_qualifiers": ["-aaa", "-bbb", "-ccc"]}
        result = compute_combined_arch(meta, ["a", "b", "c"], RAW_ARCH)
        self.assertEqual(result, "slc7_x86-64-aaa-bbb-ccc")

    # -- separator lives in the value, never assumed --------------------------

    def test_value_with_leading_dash_is_verbatim(self):
        """A value carrying its own '-' is appended as-is."""
        meta = {"_append_arch_qualifiers": ["-gcc15-dbg"]}
        self.assertEqual(
            compute_combined_arch(meta, ["gcc15"], RAW_ARCH),
            "slc7_x86-64-gcc15-dbg",
        )

    def test_value_with_leading_underscore_is_verbatim(self):
        """The joiner is not assumed to be '-': '_gcc15' yields an '_' separator."""
        meta = {"_append_arch_qualifiers": ["_gcc15"]}
        self.assertEqual(
            compute_combined_arch(meta, ["gcc15"], RAW_ARCH),
            "slc7_x86-64_gcc15",
        )

    def test_bare_value_is_glued_without_separator(self):
        """No separator is injected: a bare value is concatenated directly."""
        meta = {"_append_arch_qualifiers": ["dbg"]}
        self.assertEqual(
            compute_combined_arch(meta, ["dbg"], RAW_ARCH),
            "slc7_x86-64dbg",
        )

    def test_mixed_separators_concatenate(self):
        """Each value contributes exactly what it carries -- '-', '_', or none."""
        meta = {"_append_arch_qualifiers": ["-dev", "_gcc15", "cuda"]}
        self.assertEqual(
            compute_combined_arch(meta, ["a", "b", "c"], RAW_ARCH),
            "slc7_x86-64-dev_gcc15cuda",
        )

    # -- no opt-in → raw arch -------------------------------------------------

    def test_empty_qualifiers_list_returns_raw(self):
        """_append_arch_qualifiers present but empty → raw arch (falsy guard)."""
        meta = {"_append_arch_qualifiers": []}
        self.assertEqual(
            compute_combined_arch(meta, ["dev"], RAW_ARCH),
            RAW_ARCH,
        )

    def test_absent_qualifiers_falls_through_to_qualify_arch(self):
        """Without _append_arch_qualifiers the legacy qualify_arch path is used."""
        meta = {"qualify_arch": True}
        self.assertEqual(
            compute_combined_arch(meta, ["dev", "gcc13"], RAW_ARCH),
            "slc7_x86-64-dev-gcc13",
        )

    # -- append_arch takes precedence over qualify_arch -----------------------

    def test_append_arch_takes_precedence_over_qualify_arch(self):
        """When both are present append_arch values are used, not default names."""
        meta = {
            "qualify_arch": True,
            "_append_arch_qualifiers": ["-mpi"],
        }
        # qualify_arch would produce "slc7_x86-64-dev-mpi-defaults"
        # append_arch should produce "slc7_x86-64-mpi" (value only)
        result = compute_combined_arch(meta, ["dev", "mpi-defaults"], RAW_ARCH)
        self.assertEqual(result, "slc7_x86-64-mpi")

    # -- idempotency / no mutation --------------------------------------------

    def test_does_not_mutate_meta(self):
        meta = {"_append_arch_qualifiers": ["gcc13"]}
        compute_combined_arch(meta, ["gcc13"], RAW_ARCH)
        self.assertEqual(meta, {"_append_arch_qualifiers": ["gcc13"]})

    def test_does_not_mutate_qualifiers_list(self):
        qualifiers = ["gcc13", "cuda"]
        meta = {"_append_arch_qualifiers": qualifiers}
        compute_combined_arch(meta, ["gcc13", "cuda"], RAW_ARCH)
        self.assertEqual(qualifiers, ["gcc13", "cuda"])

    # -- interaction with effective_arch() ------------------------------------

    def test_regular_pkg_uses_append_arch_combined(self):
        meta = {"_append_arch_qualifiers": ["-gcc13"]}
        combined = compute_combined_arch(meta, ["gcc13"], RAW_ARCH)
        spec = _spec("MyPkg")
        self.assertEqual(effective_arch(spec, combined), "slc7_x86-64-gcc13")

    def test_shared_pkg_ignores_append_arch_combined(self):
        meta = {"_append_arch_qualifiers": ["-gcc13"]}
        combined = compute_combined_arch(meta, ["gcc13"], RAW_ARCH)
        spec = _spec("SharedPkg", architecture=SHARED_ARCH)
        self.assertEqual(effective_arch(spec, combined), SHARED_ARCH)


# ---------------------------------------------------------------------------
# Interaction with effective_arch()
# ---------------------------------------------------------------------------

class TestEffectiveArchWithCombinedArch(unittest.TestCase):
    """compute_combined_arch + effective_arch compose correctly."""

    def test_regular_pkg_uses_combined_arch(self):
        combined = compute_combined_arch({"qualify_arch": True}, ["dev", "gcc13"], RAW_ARCH)
        spec = _spec("MyPkg")
        self.assertEqual(effective_arch(spec, combined), "slc7_x86-64-dev-gcc13")

    def test_shared_pkg_ignores_combined_arch(self):
        """architecture: shared packages always resolve to 'shared'."""
        combined = compute_combined_arch({"qualify_arch": True}, ["dev", "gcc13"], RAW_ARCH)
        spec = _spec("SharedPkg", architecture=SHARED_ARCH)
        self.assertEqual(effective_arch(spec, combined), SHARED_ARCH)

    def test_without_qualify_arch_effective_arch_unchanged(self):
        combined = compute_combined_arch({}, ["dev", "gcc13"], RAW_ARCH)
        spec = _spec("MyPkg")
        self.assertEqual(effective_arch(spec, combined), RAW_ARCH)


# ---------------------------------------------------------------------------
# _pkg_install_path() with combined arch
# ---------------------------------------------------------------------------

class TestPkgInstallPathWithCombinedArch(unittest.TestCase):

    def test_install_path_contains_combined_arch(self):
        combined = "slc7_x86-64-dev-gcc13"
        spec = _spec("MyPkg", version="2.0", revision="3")
        path = _pkg_install_path("/sw", combined, spec)
        self.assertIn("slc7_x86-64-dev-gcc13", path)
        self.assertIn("MyPkg", path)

    def test_install_path_does_not_contain_raw_arch(self):
        """The raw platform arch should not appear as a top-level dir."""
        combined = "slc7_x86-64-dev"
        spec = _spec("MyPkg")
        path = _pkg_install_path("/sw", combined, spec)
        # path should start with /sw/slc7_x86-64-dev/…, not /sw/slc7_x86-64/…
        parts = path.split("/")
        self.assertEqual(parts[2], "slc7_x86-64-dev")

    def test_shared_pkg_path_unaffected(self):
        combined = "slc7_x86-64-dev"
        spec = _spec("SharedPkg", architecture=SHARED_ARCH)
        eff = effective_arch(spec, combined)
        path = _pkg_install_path("/sw", eff, spec)
        self.assertIn("shared", path)
        self.assertNotIn("dev", path)


# ---------------------------------------------------------------------------
# generate_initdotsh(): BITS_ARCH_PREFIX uses combined arch
# ---------------------------------------------------------------------------

class TestInitdotshWithCombinedArch(unittest.TestCase):

    def _build_specs(self, combined_arch):
        """Return a minimal specs dict for generate_initdotsh."""
        dep = _spec("defaults-release", version="1", revision="1", hash="000000")
        dep["env"] = {}
        dep["full_requires"] = []
        dep["prepend_path"] = {}
        dep["append_path"] = {}
        dep["set_env"] = {}
        dep["unset_env"] = []

        pkg = _spec("MyPkg", version="3.0", revision="1",
                    requires=["defaults-release"])
        pkg["env"] = {"MYPKG_ROOT": "${WORK_DIR}/%s/MyPkg/3.0-1" % combined_arch}
        pkg["full_requires"] = ["defaults-release"]
        pkg["prepend_path"] = {}
        pkg["append_path"] = {}
        pkg["set_env"] = {}
        pkg["unset_env"] = []

        return {"MyPkg": pkg, "defaults-release": dep}

    def test_bits_arch_prefix_default_is_combined_arch(self):
        """BITS_ARCH_PREFIX in init.sh defaults to the combined arch string."""
        combined = "slc7_x86-64-dev-gcc13"
        specs = self._build_specs(combined)
        initsh = generate_initdotsh("MyPkg", specs, combined,
                                    workDir="/sw", post_build=True)
        self.assertIn(': "${BITS_ARCH_PREFIX:=%s}"' % combined, initsh)

    def test_bits_arch_prefix_without_qualify_arch(self):
        """Without qualify_arch the prefix is just the raw arch."""
        raw = "slc7_x86-64"
        specs = self._build_specs(raw)
        initsh = generate_initdotsh("MyPkg", specs, raw,
                                    workDir="/sw", post_build=True)
        self.assertIn(': "${BITS_ARCH_PREFIX:=%s}"' % raw, initsh)
        self.assertNotIn("dev", initsh.split("BITS_ARCH_PREFIX")[1][:30])

    def test_combined_arch_different_from_release_only(self):
        """Sanity: the two arch strings are genuinely distinct."""
        combined = "slc7_x86-64-dev"
        raw = "slc7_x86-64"
        self.assertNotEqual(combined, raw)


if __name__ == "__main__":
    unittest.main()
