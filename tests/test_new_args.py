# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression tests for newly added CLI arguments.

Covers:
  - bits cleanup subparser: defaults and explicit values
  - bits build --cvmfs-prefix: accepted and stored correctly
  - bits publish --no-relocate: accepted and stored correctly
  - Backward compatibility: omitting new flags leaves existing defaults unchanged
"""
import os
import sys
import types
import unittest
from unittest.mock import patch

from bits_helpers.args import doParseArgs, _parse_flavours
from bits_helpers.utilities import filterByArchitectureDefaults

# Shared architecture that passes validation checks.
_ARCH = "slc7_x86-64"


def _parse(argv):
    """Parse *argv* (list of strings, without the program name) and return args."""
    sys.argv = ["bits"] + argv
    args, _ = doParseArgs()
    return args


def _parse_with_docker(argv):
    """Like _parse() but pretends docker is installed (for --docker tests)."""
    sys.argv = ["bits"] + argv
    # subprocess.getstatusoutput("which docker") returns (0, path) when docker
    # is found.  We mock it so tests pass even on machines without docker.
    with patch("bits_helpers.args.commands.getstatusoutput", return_value=(0, "/usr/bin/docker")):
        args, _ = doParseArgs()
    return args


class CleanupSubparserTest(unittest.TestCase):
    """bits cleanup subparser is registered and has correct defaults."""

    def test_action_is_cleanup(self):
        args = _parse(["cleanup"])
        self.assertEqual(args.action, "cleanup")

    def test_defaults(self):
        args = _parse(["cleanup"])
        self.assertEqual(args.maxAgeDays, 7.0)
        self.assertIsNone(args.minFreeGb)
        self.assertFalse(args.diskPressureOnly)
        self.assertFalse(args.dryRun)
        self.assertEqual(args.workDir, "sw")

    def test_max_age(self):
        args = _parse(["cleanup", "--max-age", "14"])
        self.assertEqual(args.maxAgeDays, 14.0)

    def test_min_free(self):
        args = _parse(["cleanup", "--min-free", "100"])
        self.assertEqual(args.minFreeGb, 100.0)

    def test_disk_pressure_only(self):
        args = _parse(["cleanup", "--disk-pressure-only"])
        self.assertTrue(args.diskPressureOnly)

    def test_dry_run_short(self):
        args = _parse(["cleanup", "-n"])
        self.assertTrue(args.dryRun)

    def test_dry_run_long(self):
        args = _parse(["cleanup", "--dry-run"])
        self.assertTrue(args.dryRun)

    def test_work_dir_override(self):
        args = _parse(["cleanup", "--work-dir", "/data/alice/sw"])
        self.assertEqual(args.workDir, "/data/alice/sw")

    def test_architecture_override(self):
        args = _parse(["cleanup", "--architecture", _ARCH])
        self.assertEqual(args.architecture, _ARCH)

    def test_combined_flags(self):
        args = _parse([
            "cleanup",
            "--max-age", "3",
            "--min-free", "75",
            "--dry-run",
            "--work-dir", "/data/lhcb/sw",
        ])
        self.assertEqual(args.maxAgeDays, 3.0)
        self.assertEqual(args.minFreeGb, 75.0)
        self.assertTrue(args.dryRun)
        self.assertEqual(args.workDir, "/data/lhcb/sw")


class BuildCvmfsPrefixTest(unittest.TestCase):
    """bits build --cvmfs-prefix is accepted and defaults to None."""

    def test_default_is_none(self):
        args = _parse(["build", "--force-unknown-architecture",
                       "-a", _ARCH, "ROOT"])
        self.assertIsNone(getattr(args, "cvmfsPrefix", None))

    def test_cvmfs_prefix_stored(self):
        args = _parse(["build", "--force-unknown-architecture",
                       "-a", _ARCH,
                       "--cvmfs-prefix", "/cvmfs/sft.cern.ch/lcg/releases",
                       "ROOT"])
        self.assertEqual(args.cvmfsPrefix, "/cvmfs/sft.cern.ch/lcg/releases")

    def test_cvmfs_prefix_does_not_affect_docker_flag(self):
        # --cvmfs-prefix alone must not implicitly enable --docker.
        args = _parse(["build", "--force-unknown-architecture",
                       "-a", _ARCH,
                       "--cvmfs-prefix", "/cvmfs/sft.cern.ch/lcg/releases",
                       "ROOT"])
        self.assertFalse(args.docker)

    def test_backward_compat_no_cvmfs_prefix(self):
        """Existing invocations without --cvmfs-prefix are unaffected."""
        args = _parse_with_docker(["build", "--force-unknown-architecture",
                                   "-a", _ARCH,
                                   "--docker-image", "registry.cern.ch/alisw/el9-builder",
                                   "ROOT"])
        self.assertIsNone(getattr(args, "cvmfsPrefix", None))
        self.assertTrue(args.docker)
        self.assertEqual(args.dockerImage,
                         "registry.cern.ch/alisw/el9-builder")


class PublishNoRelocateTest(unittest.TestCase):
    """bits publish --no-relocate is accepted and defaults to False."""

    def test_default_is_false(self):
        args = _parse(["publish", "ROOT",
                       "--cvmfs-target", "/cvmfs/sft.cern.ch/lcg/releases/ROOT/6.32.0",
                       "--spool", "user@host:/spool",
                       "-a", _ARCH])
        self.assertFalse(args.noRelocate)

    def test_no_relocate_flag_set(self):
        args = _parse(["publish", "ROOT",
                       "--cvmfs-target", "/cvmfs/sft.cern.ch/lcg/releases/ROOT/6.32.0",
                       "--spool", "user@host:/spool",
                       "-a", _ARCH,
                       "--no-relocate"])
        self.assertTrue(args.noRelocate)

    def test_backward_compat_existing_publish_args(self):
        """Existing publish invocations without --no-relocate are unaffected."""
        args = _parse(["publish", "ROOT", "6.32.0-1",
                       "--cvmfs-target", "/cvmfs/sft.cern.ch/lcg/releases/ROOT/6.32.0",
                       "--spool", "user@host:/spool",
                       "-a", _ARCH,
                       "--rsync-opts", "-e 'ssh -i key'",
                       "--scratch-dir", "/tmp/bits-scratch"])
        self.assertFalse(args.noRelocate)
        self.assertEqual(args.workDir, "sw")
        self.assertEqual(args.scratchDir, "/tmp/bits-scratch")


class AutoResourcesFlagTest(unittest.TestCase):
    """--auto-resources opts in to the measurement-driven --builders scheduler."""

    def test_off_by_default(self):
        args = _parse(["build", "-a", _ARCH, "--builders", "8", "LCG"])
        self.assertFalse(getattr(args, "autoResources", False))

    def test_enabled_with_flag(self):
        args = _parse(["build", "-a", _ARCH, "--builders", "8",
                       "--auto-resources", "LCG"])
        self.assertTrue(args.autoResources)

    def test_independent_of_explicit_resource_flags(self):
        # --resources keeps its own default (None) and is unaffected.
        args = _parse(["build", "-a", _ARCH, "--builders", "8", "LCG"])
        self.assertIsNone(args.resources)

    # finaliseArgs downgrades resourceMonitoring to False when psutil is not
    # importable, so the "enabled" cases stub psutil into sys.modules to stay
    # deterministic on hosts that don't have it installed.
    def test_resource_monitoring_on_by_default_parallel(self):
        # --builders > 1 enables per-package resource monitoring by default.
        with patch.dict(sys.modules, {"psutil": types.ModuleType("psutil")}):
            args = _parse(["build", "-a", _ARCH, "--builders", "8", "LCG"])
        self.assertTrue(args.resourceMonitoring)

    def test_resource_monitoring_off_by_default_serial(self):
        # Serial builds (--builders 1, the default) leave monitoring off.
        args = _parse(["build", "-a", _ARCH, "LCG"])
        self.assertFalse(args.resourceMonitoring)

    def test_resource_monitoring_explicit_opt_out(self):
        # --no-resource-monitoring disables it even in parallel mode.
        args = _parse(["build", "-a", _ARCH, "--builders", "8",
                       "--no-resource-monitoring", "LCG"])
        self.assertFalse(args.resourceMonitoring)

    def test_resource_monitoring_explicit_opt_in_serial(self):
        # --resource-monitoring forces it on even for a serial build.
        with patch.dict(sys.modules, {"psutil": types.ModuleType("psutil")}):
            args = _parse(["build", "-a", _ARCH, "--resource-monitoring", "LCG"])
        self.assertTrue(args.resourceMonitoring)

    def test_resource_monitoring_downgraded_without_psutil(self):
        # Without psutil, even an explicit request is downgraded to False.
        with patch.dict(sys.modules, {"psutil": None}):
            args = _parse(["build", "-a", _ARCH, "--resource-monitoring", "LCG"])
        self.assertFalse(args.resourceMonitoring)


class FlavourFlagTest(unittest.TestCase):
    """--flavour parsing and how flavours gate (?NAME) conditional requires."""

    def test_parse_forms(self):
        # bare -> true, !name -> false, name=value -> value
        self.assertEqual(_parse_flavours(["cuda", "!debug", "onnx=cpu"]),
                         {"cuda": "true", "debug": "false", "onnx": "cpu"})

    def test_parse_comma_and_precedence(self):
        # comma-separated within one token; later entry wins on a repeated name
        self.assertEqual(_parse_flavours(["cuda,onnx=cpu", "onnx=gpu"]),
                         {"cuda": "true", "onnx": "gpu"})

    def test_parse_trims_and_skips_empty(self):
        self.assertEqual(_parse_flavours([" cuda , ", "a = b", ""]),
                         {"cuda": "true", "a": "b"})

    def test_cli_end_to_end(self):
        args = _parse(["build", "-a", _ARCH, "--flavour", "cuda",
                       "--flavour", "onnx=cpu,debug=off", "key4hep"])
        self.assertEqual(args.flavours, {"cuda": "true", "onnx": "cpu", "debug": "off"})

    def test_default_empty(self):
        args = _parse(["build", "-a", _ARCH, "key4hep"])
        self.assertEqual(args.flavours, {})

    def test_flavour_gates_conditional_require(self):
        # A flavour in the vars dict activates "(?cuda)"; absent/falsey excludes it.
        reqs = ["ROOT", "cudnn:(?cuda)"]
        on = list(filterByArchitectureDefaults(_ARCH, ["dev"], reqs, _parse_flavours(["cuda"])))
        self.assertIn("cudnn", on)
        off = list(filterByArchitectureDefaults(_ARCH, ["dev"], reqs, _parse_flavours(["!cuda"])))
        self.assertNotIn("cudnn", off)
        none = list(filterByArchitectureDefaults(_ARCH, ["dev"], reqs, {}))
        self.assertNotIn("cudnn", none)


class BackwardCompatBuildTest(unittest.TestCase):
    """Existing build argument combinations are completely unaffected."""

    def test_plain_build(self):
        args = _parse(["build", "--force-unknown-architecture", "-a", _ARCH, "ROOT"])
        self.assertIsNone(getattr(args, "cvmfsPrefix", None))
        self.assertFalse(args.docker)
        self.assertFalse(args.containerUseWorkDir)

    def test_docker_build(self):
        args = _parse_with_docker(["build", "-a", _ARCH,
                                   "--docker", "ROOT"])
        self.assertTrue(args.docker)
        self.assertIsNone(getattr(args, "cvmfsPrefix", None))

    def test_container_use_workdir(self):
        args = _parse_with_docker(["build", "-a", _ARCH,
                                   "--docker", "--container-use-workdir", "ROOT"])
        self.assertTrue(args.containerUseWorkDir)
        self.assertIsNone(getattr(args, "cvmfsPrefix", None))


class RemoteStoreUnificationTest(unittest.TestCase):
    """--remote-store is the canonical store flag; --store is a deprecated alias."""

    def test_remote_store_sets_dest(self):
        args = _parse(["store-stats", "--remote-store", "b3://mybucket"])
        self.assertEqual(args.storeStatsStore, "b3://mybucket")

    def test_store_alias_sets_dest(self):
        args = _parse(["store-stats", "--store", "b3://mybucket"])
        self.assertEqual(args.storeStatsStore, "b3://mybucket")

    def test_store_alias_warns(self):
        with patch("bits_helpers.log.warning") as w:
            _parse(["store-stats", "--store", "b3://x"])
        self.assertTrue(w.called)

    def test_remote_store_does_not_warn(self):
        with patch("bits_helpers.log.warning") as w:
            _parse(["store-stats", "--remote-store", "b3://x"])
        self.assertFalse(w.called)

    def test_default_is_default_s3_store(self):
        from bits_helpers.args import DEFAULT_S3_STORE
        args = _parse(["store-stats"])
        self.assertEqual(args.storeStatsStore, DEFAULT_S3_STORE)

    def test_cleanup_store_default_stays_none(self):
        args = _parse(["cleanup"])
        self.assertIsNone(args.retainStore)

    def test_cleanup_remote_store_sets_dest(self):
        args = _parse(["cleanup", "--remote-store", "b3://b"])
        self.assertEqual(args.retainStore, "b3://b")


class BuildersParallelTest(unittest.TestCase):
    """--parallel/--builders: no flag => serial (1), bare => 4, explicit => N."""

    def test_no_flag_serial(self):
        args = _parse(["build", "-a", _ARCH, "ROOT"])
        self.assertEqual(args.builders, 1)

    def test_parallel_bare_before_package(self):
        args = _parse(["build", "-a", _ARCH, "--parallel", "ROOT"])
        self.assertEqual(args.builders, 4)
        self.assertEqual(args.pkgname, ["ROOT"])

    def test_parallel_bare_after_package(self):
        args = _parse(["build", "-a", _ARCH, "ROOT", "--parallel"])
        self.assertEqual(args.builders, 4)

    def test_parallel_with_number(self):
        args = _parse(["build", "-a", _ARCH, "--parallel", "8", "ROOT"])
        self.assertEqual(args.builders, 8)
        self.assertEqual(args.pkgname, ["ROOT"])

    def test_builders_alias_with_number(self):
        args = _parse(["build", "-a", _ARCH, "--builders", "2", "ROOT"])
        self.assertEqual(args.builders, 2)

    def test_builders_bare(self):
        args = _parse(["build", "-a", _ARCH, "ROOT", "--builders"])
        self.assertEqual(args.builders, 4)

    def test_parallel_bare_two_packages(self):
        args = _parse(["build", "-a", _ARCH, "--parallel", "ROOT", "GEANT4"])
        self.assertEqual(args.builders, 4)
        self.assertEqual(args.pkgname, ["ROOT", "GEANT4"])


class RenameAliasesTest(unittest.TestCase):
    """Canonical flag names with deprecated aliases that still work and warn."""

    def test_prefer_system_canonical(self):
        args = _parse(["build", "-a", _ARCH, "--prefer-system", "ROOT"])
        self.assertTrue(args.preferSystem)

    def test_prefer_system_canonical_silent(self):
        with patch("bits_helpers.log.warning") as w:
            _parse(["build", "-a", _ARCH, "--prefer-system", "ROOT"])
        self.assertFalse(w.called)

    def test_always_prefer_system_alias_warns(self):
        with patch("bits_helpers.log.warning") as w:
            args = _parse(["build", "-a", _ARCH, "--always-prefer-system", "ROOT"])
        self.assertTrue(args.preferSystem)
        self.assertTrue(w.called)

    def test_force_overwrite_canonical(self):
        args = _parse(["import", "--force-overwrite"])
        self.assertTrue(args.importForce)

    def test_force_alias_warns(self):
        with patch("bits_helpers.log.warning") as w:
            args = _parse(["import", "--force"])
        self.assertTrue(args.importForce)
        self.assertTrue(w.called)

    def test_version_takes_no_architecture(self):
        with self.assertRaises(SystemExit):
            _parse(["version", "-a", _ARCH])

    def test_version_parses_plain(self):
        args = _parse(["version"])
        self.assertEqual(args.action, "version")


class SearchPathTest(unittest.TestCase):
    """--search-path seeds BITS_PATH; explicit $BITS_PATH wins."""

    def setUp(self):
        self._saved = os.environ.pop("BITS_PATH", None)

    def tearDown(self):
        os.environ.pop("BITS_PATH", None)
        if self._saved is not None:
            os.environ["BITS_PATH"] = self._saved

    def test_seeds_bits_path(self):
        _parse(["build", "-a", _ARCH, "--search-path", "lcg", "ROOT"])
        self.assertEqual(os.environ.get("BITS_PATH"), "lcg")

    def test_comma_separated(self):
        _parse(["build", "-a", _ARCH, "--search-path", "lcg,foo", "ROOT"])
        self.assertEqual(os.environ.get("BITS_PATH"), "lcg,foo")

    def test_no_flag_leaves_unset(self):
        _parse(["build", "-a", _ARCH, "ROOT"])
        self.assertIsNone(os.environ.get("BITS_PATH"))

    def test_explicit_env_wins(self):
        os.environ["BITS_PATH"] = "envwins"
        _parse(["build", "-a", _ARCH, "--search-path", "lcg", "ROOT"])
        self.assertEqual(os.environ.get("BITS_PATH"), "envwins")

    def test_dest_recorded_on_deps(self):
        args = _parse(["deps", "-a", _ARCH, "--search-path", "bar", "ROOT"])
        self.assertEqual(args.searchPath, "bar")

    def test_precedence_over_bits_rc(self):
        import tempfile, shutil
        d = tempfile.mkdtemp()
        cwd = os.getcwd()
        try:
            with open(os.path.join(d, "bits.rc"), "w") as fh:
                fh.write("search_path = fromrc\n")
            os.chdir(d)
            _parse(["build", "-a", _ARCH, "ROOT"])
            self.assertEqual(os.environ.get("BITS_PATH"), "fromrc")  # bits.rc seeds
            os.environ.pop("BITS_PATH", None)
            _parse(["build", "-a", _ARCH, "--search-path", "cli", "ROOT"])
            self.assertEqual(os.environ.get("BITS_PATH"), "cli")     # CLI wins
        finally:
            os.chdir(cwd)
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
