# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise reuse through local paths without modifying the deployment."""

from argparse import Namespace
import hashlib
import json
import os
from pathlib import Path
from shlex import quote
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers.build import _BuildLoopCtx
from bits_helpers.build import build_one_package
from bits_helpers.build_config import BuildConfig
from bits_helpers.initdotsh import generate_initdotsh
from bits_helpers.reuse_symlinks import stage_reused_package, find_symlink_package
from bits_helpers.reuse_symlinks import stage_hook_scripts

# What cms.bits' hooks/scram-project does (CMS REFERENCE_PACKAGE_POST).
SCRAM_HOOK = """\
private() {
  [ -L "$1" ] || return 0
  rm "$1"
  cp -RH "$SOURCE_ROOT/$1" "$1"
  chmod -R u+w "$1"
}
if [ -d "$SOURCE_ROOT/etc/scram.d" ]; then private etc/scram.d; fi
if [ -d "$SOURCE_ROOT/tools/selected" ]; then private tools; fi
if [ -d "$SOURCE_ROOT/.SCRAM" ]; then
  private .SCRAM
  private config
  config/SCRAM/projectAreaRename.pl "$REFERENCE_ROOT" "$WORK_DIR" "$ARCHITECTURE" "$PKG_ROOT"
fi
"""


class DeploymentSelectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "deploy"
        self.work = Path(self.tmp.name) / "work"
        self.arch = "el9_amd64_gcc14"
        self.spec = dict(package="data-example", version="V00-03-00", force_revision="",
                         pkg_family="cms", architecture="share", requires=[],
                         hash="expected", remote_revision_hash="expected",
                         local_revision_hash="local", remote_hashes=["expected"],
                         local_hashes=["local"], is_devel_pkg=False)

    def deploy(self, arch="share", family="cms", label="V00-03-00", hash_value="expected"):
        source = self.root / arch / family / self.spec["package"] / label
        (source / "etc/profile.d").mkdir(parents=True)
        (source / "payload").mkdir()
        (source / "payload/data").write_text("unchanged")
        if hash_value is not None:
            (source / ".build-hash").write_text(hash_value + "\n")
        (source / "etc/profile.d/init.sh").write_text(
            'export DATA_EXAMPLE_ROOT="%s"\nexport DATA_EXAMPLE_REVISION=""\n' % source)
        return source

    def test_shared_family_package_without_modulefiles_builds_by_symlinking(self):
        source = self.deploy()
        original = (source / "etc/profile.d/init.sh").read_bytes()
        args = Namespace(defaults=["release"], architecture=self.arch, docker=False,
                         symlinkPackagesFrom=str(self.root), manifest=None)
        cfg = BuildConfig.from_args(args)
        self.assertIsNone(cfg.reuse_overlay)
        spec = dict(self.spec)
        ctx = _BuildLoopCtx(
            args=args, cfg=cfg, specs={spec["package"]: spec}, workDir=str(self.work),
            syncHelper=None, scheduler=None, mainPackage=spec["package"],
            raw_architecture=self.arch, develPackageBranch="", defaultsMeta={},
            buildTargets="", packages=[], prefetch_executor=None, cmake_prefix_env=False,
            specs_for_checksum_phase=[], monitoredDirs={})
        with patch("bits_helpers.build.storeHook"), patch("bits_helpers.build.storeHashes"), \
             patch("bits_helpers.build.log_current_package"):
            build_one_package(spec["package"], ctx)
        local = self.work / "share/cms/data-example/V00-03-00"
        self.assertTrue(spec["reuse_symlinked"])
        self.assertEqual(spec["revision"], "")
        self.assertNotIn("reuse_module_id", spec)
        self.assertTrue((local / "payload").is_symlink())
        self.assertEqual((local / "payload/data").read_text(), "unchanged")
        consumer = generate_initdotsh("App", {"App": dict(package="App", requires=["data-example"]),
                                              "data-example": spec}, self.arch, workDir=str(self.work))
        output = subprocess.check_output(
            ["bash", "-ec", consumer + '\nprintf "%s" "$DATA_EXAMPLE_ROOT"'], text=True)
        self.assertEqual(output, str(local))
        self.assertEqual((source / "etc/profile.d/init.sh").read_bytes(), original)

    def test_manifest_records_the_linked_deployment(self):
        from bits_helpers.manifest import BuildManifest
        source = self.deploy()
        self.work.mkdir()
        manifest = BuildManifest(str(self.work), ["data-example"], self.arch, ["release"],
                                 str(self.work), "abc")
        args = Namespace(defaults=["release"], architecture=self.arch, docker=False,
                         symlinkPackagesFrom=str(self.root), manifest=manifest)
        spec = dict(self.spec)
        ctx = _BuildLoopCtx(
            args=args, cfg=BuildConfig.from_args(args), specs={spec["package"]: spec},
            workDir=str(self.work), syncHelper=None, scheduler=None,
            mainPackage=spec["package"], raw_architecture=self.arch, develPackageBranch="",
            defaultsMeta={}, buildTargets="", packages=[], prefetch_executor=None,
            cmake_prefix_env=False, specs_for_checksum_phase=[], monitoredDirs={})
        with patch("bits_helpers.build.storeHook"), patch("bits_helpers.build.storeHashes"), \
             patch("bits_helpers.build.log_current_package"):
            build_one_package(spec["package"], ctx)
        entry, = manifest.packages()
        self.assertEqual(entry["outcome"], "already_installed")
        self.assertEqual(entry["symlinked_from"], str(source))
        self.assertEqual(entry["symlink_policy"], "strict")

    def test_build_runs_the_recipe_stage_hooks(self):
        self.deploy()
        hooks = Path(self.tmp.name) / "cms.bits/hooks"
        hooks.mkdir(parents=True)
        (hooks / "mark").write_text('echo "$PKGNAME" > "$PKG_ROOT/hooked"\n')
        args = Namespace(defaults=["release"], architecture=self.arch, docker=False,
                         symlinkPackagesFrom=str(self.root), manifest=None,
                         configDir=str(hooks.parent))
        spec = dict(self.spec, hook={"SYMLINK_STAGE": "mark"})
        ctx = _BuildLoopCtx(
            args=args, cfg=BuildConfig.from_args(args), specs={spec["package"]: spec},
            workDir=str(self.work), syncHelper=None, scheduler=None,
            mainPackage=spec["package"], raw_architecture=self.arch, develPackageBranch="",
            defaultsMeta={}, buildTargets="", packages=[], prefetch_executor=None,
            cmake_prefix_env=False, specs_for_checksum_phase=[], monitoredDirs={})
        with patch("bits_helpers.build.storeHook"), patch("bits_helpers.build.storeHashes"), \
             patch("bits_helpers.build.log_current_package"), patch.dict(os.environ, {"BITS_PATH": ""}):
            build_one_package(spec["package"], ctx)
        local = self.work / "share/cms/data-example/V00-03-00"
        self.assertEqual((local / "hooked").read_text(), "data-example\n")

    def test_matching_local_install_is_kept(self):
        from bits_helpers.plan import classify, INSTALLED
        from bits_helpers.reuse_symlinks import select_symlink_package
        source = self.deploy()
        local = self.work / "share/cms/data-example/V00-03-00"
        local.parent.mkdir(parents=True)
        shutil.copytree(source, local)
        cfg = BuildConfig.from_args(Namespace(symlinkPackagesFrom=str(self.root)))
        spec = dict(self.spec)
        self.assertEqual(classify(spec, self.arch, str(self.work), None, False, False,
                                  None, symlink=lambda s: select_symlink_package(s, cfg, self.arch)),
                         (INSTALLED, "expected", ""))
        args = Namespace(defaults=["release"], architecture=self.arch, docker=False,
                         symlinkPackagesFrom=str(self.root), manifest=None)
        ctx = _BuildLoopCtx(
            args=args, cfg=cfg, specs={spec["package"]: spec}, workDir=str(self.work),
            syncHelper=None, scheduler=None, mainPackage=spec["package"],
            raw_architecture=self.arch, develPackageBranch="", defaultsMeta={},
            buildTargets="", packages=[], prefetch_executor=None, cmake_prefix_env=False,
            specs_for_checksum_phase=[], monitoredDirs={})
        with patch("bits_helpers.build.storeHook"), patch("bits_helpers.build.storeHashes"), \
             patch("bits_helpers.build.log_current_package"):
            build_one_package(spec["package"], ctx)
        self.assertFalse(local.is_symlink())
        self.assertNotIn("reuse_symlinked", spec)
        self.assertEqual(spec["hash"], "expected")

    def test_shared_package_sources_arch_specific_dependency(self):
        source = self.deploy()
        (source / "etc/profile.d/init.sh").write_text(
            ': "${BITS_ARCH_PREFIX:=el9_amd64_gcc14}"\n'
            '. "$WORK_DIR/$BITS_ARCH_PREFIX/cms/cms-common/1/etc/profile.d/init.sh"\n'
            'export DATA_EXAMPLE_ROOT="$WORK_DIR/share/cms/data-example/V00-03-00"\n')
        common = self.work / self.arch / "cms/cms-common/1/etc/profile.d"
        common.mkdir(parents=True)
        (common / "init.sh").write_text(
            'export CMS_COMMON_ROOT="$WORK_DIR/$BITS_ARCH_PREFIX/cms/cms-common/1"\n')
        match = find_symlink_package(self.spec, self.root, self.arch)
        local = stage_reused_package(self.spec, match["install_base"], self.work,
                                     self.arch, source_path=match["source"])
        output = subprocess.check_output(
            ["bash", "-e", "-c",
             'BITS_ARCH_PREFIX=caller; . "$1"; '
             'printf "%s\\n%s\\n" "$CMS_COMMON_ROOT" "$BITS_ARCH_PREFIX"',
             "bash", str(Path(local) / "etc/profile.d/init.sh")], text=True)
        self.assertEqual(output.splitlines(),
                         [str(self.work / self.arch / "cms/cms-common/1"), "caller"])

    def test_stale_shared_stage_is_refreshed_before_build(self):
        source = self.deploy()
        (source / "etc/profile.d/init.sh").write_text(
            ': "${BITS_ARCH_PREFIX:=el9_amd64_gcc14}"\n'
            '. "$WORK_DIR/$BITS_ARCH_PREFIX/defaults-release/1/etc/profile.d/init.sh"\n')
        defaults = self.work / self.arch / "defaults-release/1/etc/profile.d"
        defaults.mkdir(parents=True)
        (defaults / "init.sh").write_text('export DEFAULTS_RELEASE_REVISION=loaded\n')
        match = find_symlink_package(self.spec, self.root, self.arch)
        local = Path(stage_reused_package(self.spec, match["install_base"], self.work,
                                          self.arch, source_path=match["source"]))
        local_init = local / "etc/profile.d/init.sh"
        fresh = local_init.read_text()
        stale = fresh.replace("BITS_ARCH_PREFIX=%s\n" % self.arch,
                              "BITS_ARCH_PREFIX=share\n", 1)
        self.assertNotEqual(stale, fresh)
        local_init.write_text(stale)
        marker = local / ".bits-reuse-symlinks.json"
        old_identity = json.loads(marker.read_text())
        old_identity["init_sha256"] = hashlib.sha256(stale.encode()).hexdigest()
        marker.write_text(json.dumps(old_identity, sort_keys=True) + "\n")

        args = Namespace(defaults=["release"], architecture=self.arch, docker=False,
                         symlinkPackagesFrom=str(self.root), manifest=None)
        cfg = BuildConfig.from_args(args)
        spec = dict(self.spec)
        ctx = _BuildLoopCtx(
            args=args, cfg=cfg, specs={spec["package"]: spec}, workDir=str(self.work),
            syncHelper=None, scheduler=None, mainPackage=spec["package"],
            raw_architecture=self.arch, develPackageBranch="", defaultsMeta={},
            buildTargets="", packages=[], prefetch_executor=None, cmake_prefix_env=False,
            specs_for_checksum_phase=[], monitoredDirs={})
        with patch("bits_helpers.build.storeHook"), patch("bits_helpers.build.storeHashes"), \
             patch("bits_helpers.build.log_current_package"):
            build_one_package(spec["package"], ctx)
        self.assertTrue(spec["reuse_symlinked"])
        self.assertEqual(local_init.read_text(), fresh)
        output = subprocess.check_output(
            ["bash", "-e", "-c", '. "$1"; printf "%s" "$DEFAULTS_RELEASE_REVISION"',
             "bash", str(local_init)], text=True)
        self.assertEqual(output, "loaded")

    def test_strict_checks_hash_relaxed_uses_path(self):
        source = self.deploy(hash_value="different")
        self.assertIsNone(find_symlink_package(self.spec, self.root, self.arch))
        self.assertEqual(find_symlink_package(self.spec, self.root, self.arch, "relaxed")["source"], str(source))
        (source / ".build-hash").unlink()
        self.assertIsNone(find_symlink_package(self.spec, self.root, self.arch))
        self.assertIsNotNone(find_symlink_package(self.spec, self.root, self.arch, "relaxed"))
        (source / ".build-hash").write_text("local\n")
        self.assertIsNone(find_symlink_package(self.spec, self.root, self.arch))
        self.assertEqual(find_symlink_package(self.spec, self.root, self.arch, "relaxed")["hash"], "local")

    def test_architecture_and_version_are_required_in_both_policies(self):
        self.deploy(arch="wrong-arch")
        self.deploy(label="V00-03-01")
        for policy in ("strict", "relaxed"):
            self.assertIsNone(find_symlink_package(self.spec, self.root, self.arch, policy))

    def test_arch_specific_and_flat_layouts_and_symlinked_package(self):
        self.spec.pop("architecture")
        self.spec["pkg_family"] = "external"
        source = self.deploy(arch=self.arch, family="")
        moved = source.with_name("payload-directory")
        source.rename(moved)
        source.symlink_to(moved, target_is_directory=True)
        match = find_symlink_package(self.spec, self.root, self.arch)
        self.assertEqual(match["source"], str(source))
        local = Path(stage_reused_package(self.spec, match["install_base"], self.work,
                                         self.arch, source_path=match["source"]))
        self.assertEqual(local, self.work / self.arch / "external/data-example/V00-03-00")
        self.assertEqual((local / "payload/data").read_text(), "unchanged")
        self.spec["pkg_family"] = ""
        self.assertIsNotNone(find_symlink_package(self.spec, self.root, self.arch))

    def test_revision_policy_and_dry_run_match_selection(self):
        from bits_helpers.plan import classify, SYMLINK
        from bits_helpers.reuse_symlinks import select_symlink_package
        source = self.deploy(label="V00-03-00-older", hash_value="different")
        # An explicit empty revision must not accidentally match a suffixed one.
        self.assertIsNone(find_symlink_package(self.spec, self.root, self.arch, "relaxed"))
        self.spec.update(force_revision="expected", _revision_policy_hash_injected=True)
        cfg = BuildConfig.from_args(Namespace(
            symlinkPackagesFrom=str(self.root), symlinkPolicy="relaxed"))
        result = classify(self.spec, self.arch, str(self.work), None, False, False, None,
                          symlink=lambda spec: select_symlink_package(spec, cfg, self.arch))
        self.assertEqual(result, (SYMLINK, "different", "older"))
        self.assertFalse(self.work.exists())
        self.assertEqual(select_symlink_package(self.spec, cfg, self.arch)["source"], str(source))
        blocked = BuildConfig.from_args(Namespace(
            symlinkPackagesFrom=str(self.root), buildLocal=[self.spec["package"]]))
        self.assertIsNone(select_symlink_package(self.spec, blocked, self.arch))
        for changes in ({"is_devel_pkg": True}, {"provides_repository": True},
                        {"package": "defaults-release"}):
            self.assertIsNone(select_symlink_package(dict(self.spec, **changes), cfg, self.arch))

    def test_strict_selection_needs_a_certified_hash(self):
        from types import SimpleNamespace
        from bits_helpers import plan
        from bits_helpers.reuse_symlinks import select_symlink_package
        from bits_helpers.sync import NoRemoteSync
        self.deploy()
        certified = {("share", "expected"): "sha256"}
        cfg = SimpleNamespace(symlink_packages_from=str(self.root), symlink_policy="strict",
                              build_local=[], require_signed_reuse=True)
        self.assertIsNone(select_symlink_package(self.spec, cfg, self.arch, trusted={}))
        self.assertIsNotNone(select_symlink_package(self.spec, cfg, self.arch, trusted=certified))
        relaxed = SimpleNamespace(**dict(vars(cfg), symlink_policy="relaxed"))
        self.assertIsNotNone(select_symlink_package(self.spec, relaxed, self.arch, trusted={}))
        # The plan asks for the signed index and classifies as the build does.
        args = SimpleNamespace(defaults=["release"], architecture=self.arch)
        for trusted, state in (({}, plan.BUILD), (certified, plan.SYMLINK)):
            spec = dict(self.spec)
            with patch("bits_helpers.build.storeHook"), patch("bits_helpers.hashing.storeHashes"), \
                 patch.object(plan, "_overlay_predicate", lambda cfg: None):
                states, _, _ = plan.plan([spec["package"]], {spec["package"]: spec}, args,
                                         str(self.work), NoRemoteSync(), self.arch, cfg,
                                         lambda: trusted)
            self.assertEqual(states[spec["package"]], state)

    def test_plan_does_not_list_symlinked_packages_in_the_store(self):
        from types import SimpleNamespace
        from bits_helpers import plan
        from bits_helpers.sync import RemoteSync
        self.deploy()
        listed = []

        class Store(RemoteSync):
            remoteStore = "b3://store"

            def list_store_tarballs(self, arch, pkg_hash):
                listed.append(pkg_hash)
                return []
        cfg = SimpleNamespace(symlink_packages_from=str(self.root), symlink_policy="strict",
                              build_local=[], require_signed_reuse=False)
        args = SimpleNamespace(defaults=["release"], architecture=self.arch)
        spec = dict(self.spec)
        with patch("bits_helpers.build.storeHook"), patch("bits_helpers.hashing.storeHashes"), \
             patch.object(plan, "_overlay_predicate", lambda cfg: None):
            states, _, _ = plan.plan([spec["package"]], {spec["package"]: spec}, args,
                                     str(self.work), Store(), self.arch, cfg, None)
        self.assertEqual(states[spec["package"]], plan.SYMLINK)
        self.assertEqual(listed, [])

    def test_build_selects_with_the_signed_index(self):
        class Selected(Exception):
            pass
        seen = []

        def select(spec, cfg, architecture, trusted=None):
            seen.append(trusted)
            raise Selected()
        for signed in (True, False):
            args = Namespace(defaults=["release"], architecture=self.arch, docker=False,
                             symlinkPackagesFrom=str(self.root), manifest=None,
                             requireSignedReuse=signed)
            spec = dict(self.spec)
            ctx = _BuildLoopCtx(
                args=args, cfg=BuildConfig.from_args(args), specs={spec["package"]: spec},
                workDir=str(self.work), syncHelper=None, scheduler=None,
                mainPackage=spec["package"], raw_architecture=self.arch, develPackageBranch="",
                defaultsMeta={}, buildTargets="", packages=[], prefetch_executor=None,
                cmake_prefix_env=False, specs_for_checksum_phase=[], monitoredDirs={})
            with patch("bits_helpers.build.storeHook"), patch("bits_helpers.build.storeHashes"), \
                 patch("bits_helpers.build.log_current_package"), \
                 patch("bits_helpers.build.trusted_reuse_index", return_value={"k": "v"}), \
                 patch("bits_helpers.reuse_symlinks.select_symlink_package", select), \
                 self.assertRaises(Selected):
                build_one_package(spec["package"], ctx)
        self.assertEqual(seen, [{"k": "v"}, None])


class ReuseSymlinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "deployment" / "Packages"
        self.work = self.root / "local"
        self.arch = "el9_amd64_gcc14"
        self.spec = dict(package="gcc", version="14", revision="hash", hash="hash",
                         commit_hash="commit", requires=[], pkg_family="external",
                         reuse_module_id="gcc/14-hash", is_devel_pkg=False,
                         remote_revision_hash="hash", remote_hashes=["hash"], local_hashes=[])
        self.source = self.base / "external/gcc/14-hash"
        (self.source / "etc/profile.d").mkdir(parents=True)
        (self.source / "bin").mkdir()
        (self.source / "lib").mkdir()
        (self.source / "lib/libgcc.so").write_text("unchanged library")
        (self.source / "bin/gcc").write_text("#!/bin/sh\nprintf 'compiler works\\n'\n")
        (self.source / "bin/gcc").chmod(0o755)
        self.original = generate_initdotsh(
            "gcc", {"gcc": self.spec}, self.arch, workDir=str(self.root / "old-build"),
            post_build=True)
        # Cover a literal deployment path as well as WORK_DIR-based paths.
        self.original += '\nexport GCC_EXTRA="%s/include"\n' % self.source
        (self.source / "etc/profile.d/init.sh").write_text(self.original)
        (self.source / "etc/profile.d/.bits-pkginfo").write_text(
            'PKG_DIR="%s"\n' % (self.root / "old-build"))

    def stage(self, **kwargs):
        return Path(stage_reused_package(self.spec, str(self.base), str(self.work),
                                         self.arch, source_path=self.source, **kwargs))

    def environment(self, script):
        result = subprocess.run(["bash", "-e", "-c", script + "\nenv -0"],
                                env={"PATH": "/usr/bin:/bin"}, check=True,
                                stdout=subprocess.PIPE)
        return dict(v.split("=", 1) for v in result.stdout.decode().split("\0") if "=" in v)

    def test_local_environment_links_payload_and_preserves_source(self):
        local = self.stage()
        self.spec["reuse_symlinked"] = True
        app = dict(package="App", requires=["gcc"])
        consumer = generate_initdotsh("App", {"App": app, "gcc": self.spec}, self.arch,
                                      workDir=str(self.work), reuse_cvmfs_base=str(self.base))
        env = self.environment(consumer)
        self.assertEqual(env["GCC_ROOT"], str(local))
        self.assertEqual(env["GCC_EXTRA"], str(local / "include"))
        self.assertEqual(env["PATH"].split(":")[0], str(local / "bin"))
        self.assertIn(str(local / "lib"), env["LD_LIBRARY_PATH"].split(":"))
        self.assertNotIn(str(self.base), consumer)
        self.assertTrue((local / "bin").is_symlink())
        self.assertFalse((local / "etc/profile.d/init.sh").is_symlink())
        self.assertEqual((local / "lib/libgcc.so").read_text(), "unchanged library")
        self.assertEqual((self.source / "etc/profile.d/init.sh").read_text(), self.original)
        output = subprocess.check_output([str(local / "bin/gcc")], text=True)
        self.assertEqual(output.strip(), "compiler works")

    def test_new_consumer_paths_are_handled_by_existing_relocator(self):
        local = self.stage()
        env = self.environment('. %s' % quote(str(local / "etc/profile.d/init.sh")))
        suffix = self.arch + "/external/App/1-hash"
        app = self.work / suffix
        (app / "etc/profile.d").mkdir(parents=True)
        (app / "flags.pc").write_text("Libs: -L%s/lib\n" % env["GCC_ROOT"])
        (app / "etc/profile.d/.bits-relocate").write_text("flags.pc\n")
        (app / "etc/profile.d/.bits-pkginfo").write_text(
            'PKG_DIR="%s"\nPP="%s"\nPH=hash\n' % (self.work, suffix))
        shutil.copy(Path(__file__).parents[1] / "bits_helpers/relocate-me.sh", app)
        relocated = self.root / "downloaded" / suffix
        relocated.parent.mkdir(parents=True)
        shutil.move(str(app), relocated)
        subprocess.run(["bash", str(relocated / "relocate-me.sh")], check=True,
                       env={"PATH": "/usr/bin:/bin", "WORK_DIR": str(self.root / "downloaded")})
        self.assertEqual((relocated / "flags.pc").read_text(),
                         "Libs: -L%s/%s/external/gcc/14-hash/lib\n"
                         % (self.root / "downloaded", self.arch))

    def test_idempotent_and_adopts_compatible_existing_layouts(self):
        local = self.stage()
        self.assertEqual(self.stage(), local)
        (local / ".bits-reuse-symlinks.json").unlink()
        # A marker can be lost while the prepared symlink tree remains. Rebuild
        # its private files and adopt its compatible source links.
        self.assertEqual(self.stage(), local)
        self.assertFalse((local / "etc/profile.d/init.sh").is_symlink())
        shutil.rmtree(local)
        local.symlink_to(self.source)
        self.assertEqual(self.stage(), local)
        self.assertFalse(local.is_symlink())
        self.assertTrue((local / "bin").is_symlink())
        self.assertFalse((local / "etc/profile.d/init.sh").is_symlink())
        self.assertEqual((self.source / "etc/profile.d/init.sh").read_text(), self.original)

    def test_compatible_partial_links_are_adopted_but_foreign_links_rejected(self):
        local = self.work / self.arch / "external/gcc/14-hash"
        local.mkdir(parents=True)
        (local / "bin").symlink_to(self.source / "bin")
        (local / "lib").symlink_to(self.source / "lib")
        self.assertEqual(self.stage(), local)
        self.assertTrue((local / "bin").is_symlink())
        self.assertFalse((local / "etc/profile.d/init.sh").is_symlink())

        shutil.rmtree(local)
        local.mkdir(parents=True)
        foreign = self.root / "foreign"
        foreign.mkdir()
        (local / "bin").symlink_to(foreign)
        with self.assertRaisesRegex(ValueError, r"first conflict: .*/bin\)"):
            self.stage()
        self.assertEqual((self.source / "etc/profile.d/init.sh").read_text(), self.original)

    def test_missing_init_fails_without_staging(self):
        (self.source / "etc/profile.d/init.sh").unlink()
        with self.assertRaisesRegex(ValueError, "needs the deployed init.sh"):
            self.stage()
        self.assertFalse(self.work.exists())

    def test_linked_destination_parent_is_rejected(self):
        (self.work / self.arch).mkdir(parents=True)
        (self.work / self.arch / "external").symlink_to(self.base / "external")
        with self.assertRaisesRegex(ValueError, "linked destination parent"):
            self.stage()
        self.assertEqual((self.source / "etc/profile.d/init.sh").read_text(), self.original)

    def test_flat_source_can_stage_under_local_family(self):
        flat = self.base / "gcc/14-hash"
        flat.parent.mkdir(parents=True)
        shutil.move(str(self.source), flat)
        self.source = flat
        (flat / "etc/profile.d/init.sh").write_text(
            'export GCC_ROOT="%s"\nexport GCC_REVISION=hash\n' % flat)
        local = self.stage()
        env = self.environment('. %s' % quote(str(local / "etc/profile.d/init.sh")))
        self.assertEqual(env["GCC_ROOT"], str(local))

    def test_effective_arch_and_caller_arch_restoration(self):
        for effective in ("share", "el9-toolchain"):
            with self.subTest(effective=effective):
                if effective == "share":
                    self.spec["architecture"] = "share"
                else:
                    self.spec.pop("architecture", None)
                    self.spec["_own_hash_arch"] = effective
                # The source init.sh must reflect the effective architecture
                # of the package being staged, as a real deployment does.
                (self.source / "etc/profile.d/init.sh").write_text(generate_initdotsh(
                    "gcc", {"gcc": self.spec}, self.arch,
                    workDir=str(self.root / "old-build"), post_build=True))
                local = self.stage()
                env = self.environment('export BITS_ARCH_PREFIX=consumer\n. %s'
                                       % quote(str(local / "etc/profile.d/init.sh")))
                self.assertEqual(env["GCC_ROOT"], str(local))
                self.assertEqual(env["BITS_ARCH_PREFIX"], "consumer")

    def test_container_init_uses_container_workdir(self):
        local = self.stage(runtime_work_dir="/container/bits/sw")
        init = (local / "etc/profile.d/init.sh").read_text()
        self.assertIn('/container/bits/sw/%s/external/gcc/14-hash/include' % self.arch, init)
        self.assertNotIn(str(self.base), init)
        self.assertNotIn(str(self.root / "old-build"), init)

    def test_transitive_initialization_uses_local_dependencies(self):
        tools = dict(self.spec, package="Tools", pkg_family="", version="1",
                     reuse_module_id="Tools/1-hash", requires=[])
        source = self.base / "Tools/1-hash"
        (source / "etc/profile.d").mkdir(parents=True)
        (source / "bin").mkdir()
        (source / "etc/profile.d/init.sh").write_text(generate_initdotsh(
            "Tools", {"Tools": tools}, self.arch, workDir=str(self.base), post_build=True))
        self.spec["requires"] = ["Tools"]
        (self.source / "etc/profile.d/init.sh").write_text(generate_initdotsh(
            "gcc", {"gcc": self.spec, "Tools": tools}, self.arch,
            workDir=str(self.base), post_build=True))
        tool_root = stage_reused_package(tools, str(self.base), str(self.work), self.arch,
                                         source_path=source)
        local = self.stage()
        env = self.environment('export BITS_ARCH_PREFIX=consumer\n. %s'
                               % quote(str(local / "etc/profile.d/init.sh")))
        self.assertEqual(env["TOOLS_ROOT"], tool_root)
        self.assertEqual(env["GCC_ROOT"], str(local))
        self.assertEqual(env["BITS_ARCH_PREFIX"], "consumer")

    def test_changed_source_is_not_silently_reused(self):
        self.stage()
        (self.source / "etc/profile.d/init.sh").write_text(self.original + "\n# changed\n")
        with self.assertRaisesRegex(ValueError, "incompatible existing content"):
            self.stage()

    def scram_hook(self):
        """A recipe repository's SYMLINK_STAGE hook doing what CMS does for SCRAM."""
        hooks = self.root / "cms.bits/hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        (hooks / "scram-project").write_text(SCRAM_HOOK)
        spec = dict(self.spec, hook={"SYMLINK_STAGE": "scram-project"})
        with patch.dict(os.environ, {"BITS_PATH": ""}):
            return stage_hook_scripts(spec, self.root / "cms.bits")

    def test_stage_hooks_are_found_as_build_hooks_are(self):
        self.assertEqual(stage_hook_scripts(self.spec, "/nowhere"), [])
        self.assertEqual(self.scram_hook(), [str(self.root / "cms.bits/hooks/scram-project")])
        with patch.dict(os.environ, {"BITS_PATH": "other"}):
            with self.assertRaisesRegex(ValueError, "missing is in no hooks/ of other, cms"):
                stage_hook_scripts(dict(self.spec, hook={"SYMLINK_STAGE": "missing"}),
                                   self.root / "cms.bits")

    def test_stage_hook_is_not_part_of_the_hash(self):
        from bits_helpers.hashing import storeHashes
        hashes = []
        for hook in ({}, {"SYMLINK_STAGE": "scram-project"}, {"POST_INSTALL": "x"}):
            specs = {"p": dict(package="p", version="1", recipe="", requires=[],
                               is_devel_pkg=False, commit_hash="0", hook=hook)}
            storeHashes("p", specs, False)
            hashes.append(specs["p"]["remote_revision_hash"])
        self.assertEqual(hashes[0], hashes[1])
        self.assertNotEqual(hashes[0], hashes[2])

    def test_cms_copy_exceptions_and_reference_relocation(self):
        copies = ["etc/profile.d/init.csh", "etc/profile.d/extra.sh",
                  "etc/scram.d/tool.xml", "tools/selected/tool.xml", "tools/other"]
        links = ["etc/other/file", "config/file"]
        for name in copies + links:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(str(self.source) + "/lib\n")
            path.chmod(0o444)
        # rsync -a keeps links inside copied directories as links.
        (self.source / "etc/profile.d/link").symlink_to(self.source / "etc/other/file")
        # bits itself copies only etc/profile.d; the rest is the hook's.
        plain = self.stage()
        self.assertTrue((plain / "etc/scram.d").is_symlink())
        self.assertTrue((plain / "tools").is_symlink())
        self.assertFalse((plain / "etc/profile.d/extra.sh").is_symlink())
        shutil.rmtree(plain)
        local = self.stage(stage_hooks=self.scram_hook())
        for name in copies:
            self.assertFalse((local / name).is_symlink())
            self.assertEqual((local / name).read_text(), str(local) + "/lib\n")
            self.assertTrue(os.stat(local / name).st_mode & 0o200)
            self.assertEqual((self.source / name).read_text(), str(self.source) + "/lib\n")
        self.assertTrue((local / "etc/other").is_symlink())
        self.assertTrue((local / "config").is_symlink())
        self.assertTrue((local / "etc/profile.d/link").is_symlink())
        self.assertEqual(Path(self.stage(stage_hooks=self.scram_hook())), local)

    def test_scram_project_rename_runs_before_text_replacement(self):
        (self.source / ".SCRAM").mkdir()
        (self.source / ".SCRAM/Environment").write_text(str(self.base) + "/payload\n")
        script = self.source / "config/SCRAM/projectAreaRename.pl"
        script.parent.mkdir(parents=True)
        script.write_text('#!/bin/sh\n'
                          'test "$(cat "$4/.SCRAM/Environment")" = "$1/payload" || exit 1\n'
                          'printf "%s\\n" "$1" "$2" "$3" "$4" > "$4/rename-args"\n'
                          'cat "$4/.SCRAM/Environment" > "$4/before-rewrite"\n')
        script.chmod(0o555)
        local = self.stage(stage_hooks=self.scram_hook())
        self.assertFalse((local / ".SCRAM").is_symlink())
        self.assertFalse((local / "config").is_symlink())
        self.assertEqual((local / "before-rewrite").read_text(),
                         str(self.work / self.arch) + "/payload\n")
        self.assertEqual((self.source / ".SCRAM/Environment").read_text(),
                         str(self.base) + "/payload\n")
        self.assertFalse((self.source / "rename-args").exists())
        self.assertIn(str(local), (local / "rename-args").read_text())

    def test_scram_hook_copies_a_linked_directory_not_the_link(self):
        real = self.source / "tools-1"
        (real / "selected").mkdir(parents=True)
        (real / "selected/tool.xml").write_text(str(self.source) + "/lib\n")
        (self.source / "tools").symlink_to("tools-1")
        local = self.stage(stage_hooks=self.scram_hook())
        self.assertFalse((local / "tools").is_symlink())
        self.assertEqual((local / "tools/selected/tool.xml").read_text(), str(local) + "/lib\n")
        self.assertEqual((real / "selected/tool.xml").read_text(), str(self.source) + "/lib\n")

    def test_interrupted_hook_stage_is_made_again(self):
        hooks = self.scram_hook()
        local = self.stage(stage_hooks=hooks)
        marker = local / ".bits-reuse-symlinks.json"
        # As left by a process killed in a hook: unfinished, deployed init.sh.
        marker.write_text(json.dumps(dict(json.loads(marker.read_text()), complete=False)))
        (local / "etc/profile.d/init.sh").write_text(self.original)
        self.assertEqual(self.stage(stage_hooks=hooks), local)
        self.assertNotIn("complete", json.loads(marker.read_text()))
        self.assertNotEqual((local / "etc/profile.d/init.sh").read_text(), self.original)
        # Someone else's content there is still refused.
        (local / "etc/profile.d/init.sh").write_text(self.original)
        with self.assertRaisesRegex(ValueError, "incompatible existing content"):
            self.stage(stage_hooks=hooks)

    def test_failed_stage_hook_leaves_no_stage(self):
        hook = self.root / "fail"
        hook.write_text("exit 3\n")
        with self.assertRaisesRegex(ValueError, "stage hook .* failed for gcc"):
            self.stage(stage_hooks=[str(hook)])
        self.assertFalse(os.path.lexists(self.work / self.arch / "external/gcc/14-hash"))

    def test_module_overlay_does_not_stage_local_symlinks(self):
        spec = dict(self.spec)
        spec.pop("reuse_module_id")
        args = Namespace(defaults=["release"], architecture=self.arch, docker=False,
                         reuseOverlay="overlay", manifest=None,
                         reuseCvmfsBase=str(self.base))
        ctx = _BuildLoopCtx(
            args=args, cfg=BuildConfig.from_args(args), specs={"gcc": spec},
            workDir=str(self.work), syncHelper=None, scheduler=None,
            mainPackage="gcc", raw_architecture=self.arch, develPackageBranch="",
            defaultsMeta={}, buildTargets="", packages=[], prefetch_executor=None,
            cmake_prefix_env=False, specs_for_checksum_phase=[], monitoredDirs={})
        with patch("bits_helpers.build.storeHook"), \
             patch("bits_helpers.build.storeHashes"), \
             patch("bits_helpers.build.log_current_package"), \
             patch("bits_helpers.cvmfs_import.overlay_reuse_module", return_value="gcc/14-hash"):
            build_one_package("gcc", ctx)
        self.assertNotIn("reuse_symlinked", spec)
        self.assertFalse(self.work.exists())
        init = generate_initdotsh("App", {"App": dict(package="App", requires=["gcc"]), "gcc": spec},
                                  self.arch, workDir=str(self.work),
                                  reuse_cvmfs_base=str(self.base))
        self.assertIn(str(self.base), init)
