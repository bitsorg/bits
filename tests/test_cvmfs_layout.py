# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the templated CVMFS layout resolver (bits_helpers/cvmfs_layout.py)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers.cvmfs_layout import resolve_cvmfs_layout as R
from bits_helpers.cvmfs_layout import resolve_cvmfs_templates as RT
from bits_helpers.cvmfs_layout import (
    resolve_release, path_release, bake_release, _declared_release,
    resolve_reuse_from, split_reuse_policy, reuse_module_path_from_templates,
    reuse_from_option)

ARCH = "ubuntu2510_x86-64-gcc15-dbg"


class CvmfsLayoutTest(unittest.TestCase):
    def test_none_when_unconfigured(self):
        self.assertIsNone(R({}, ARCH))
        self.assertIsNone(R(None, ARCH))
        self.assertIsNone(R({"variables": {"x": "1"}}, ARCH))

    def test_full_layout_resolves_architecture(self):
        layout = R({
            "cvmfs_dir": "/cvmfs/sft.cern.ch/lcg/releases",
            "install_dir": "%(architecture)s/Packages",
            "module_dir": "%(architecture)s/modules",
        }, ARCH)
        self.assertEqual(layout["install_path"],
                         "/cvmfs/sft.cern.ch/lcg/releases/%s/Packages" % ARCH)
        self.assertEqual(layout["module_path"],
                         "/cvmfs/sft.cern.ch/lcg/releases/%s/modules" % ARCH)

    def test_dirs_default_sensibly(self):
        layout = R({"cvmfs_dir": "/cvmfs/x"}, ARCH)
        self.assertEqual(layout["install_dir"], ARCH)
        self.assertEqual(layout["module_dir"], "%s/modules" % ARCH)
        self.assertEqual(layout["shared_dir"], "noarch")        # default, NOT arch-scoped
        self.assertEqual(layout["views_dir"], "Views")          # default views dir
        self.assertEqual(layout["install_path"], "/cvmfs/x/" + ARCH)
        self.assertEqual(layout["shared_path"], "/cvmfs/x/noarch")
        self.assertEqual(layout["views_path"], "/cvmfs/x/Views")

    def test_shared_dir_override_and_triggers_layout(self):
        # shared_dir alone is enough to opt in, and is overridable; it is NOT
        # arch-scoped by default (noarch packages live in one place).
        layout = R({"cvmfs_dir": "/cvmfs/x", "shared_dir": "any"}, ARCH)
        self.assertEqual(layout["shared_path"], "/cvmfs/x/any")
        self.assertIsNotNone(R({"shared_dir": "noarch"}, ARCH))

    def test_views_dir_override_and_triggers_layout(self):
        # views_dir alone is enough to opt in, and is overridable
        layout = R({"views_dir": "%(architecture)s/views"}, ARCH)
        self.assertIsNotNone(layout)
        self.assertEqual(layout["views_path"], "%s/views" % ARCH)  # relative, no cvmfs_dir

    def test_unknown_placeholder_left_intact(self):
        layout = R({"cvmfs_dir": "/cvmfs/x",
                    "install_dir": "%(nope)s/%(architecture)s"}, ARCH)
        self.assertEqual(layout["install_dir"], "%(nope)s/" + ARCH)

    def test_relative_when_no_cvmfs_dir(self):
        # install_dir/module_dir without cvmfs_dir -> relative paths (local use)
        layout = R({"install_dir": "%(architecture)s/Packages"}, ARCH)
        self.assertEqual(layout["cvmfs_dir"], "")
        self.assertEqual(layout["install_path"], "%s/Packages" % ARCH)


class CvmfsTemplatesTest(unittest.TestCase):
    """resolve_cvmfs_templates: publish-path templates from defaults-release."""

    def test_none_when_no_prefix_and_no_templates(self):
        # A group that opts out entirely gets None (unaffected).
        self.assertIsNone(RT({}))
        self.assertIsNone(RT(None))
        self.assertIsNone(RT({"system": {}}))

    def test_prefix_only_uses_default_layout(self):
        t = RT({"system": {"prefix": "/cvmfs/x.io"}})
        self.assertEqual(t["prefix"], "/cvmfs/x.io")
        self.assertEqual(t["path"], "{prefix}/{platform}/Packages/{pkg}/{tag}")
        self.assertEqual(t["modules"], "{prefix}/{platform}/Modules/modulefiles/{pkg}")
        self.assertEqual(t["shared"], "{prefix}/noarch/{pkg}/{tag}")
        # user_prefix's own {prefix} back-reference is resolved to the base.
        self.assertEqual(t["user_prefix"], "/cvmfs/x.io/user")

    def test_explicit_templates_win(self):
        t = RT({"system": {
            "prefix": "/cvmfs/test.cvmfs.io",
            "cvmfs_releases_template": "{prefix}/releases/{platform}/Packages/{pkg}/{tag}",
            "cvmfs_modules_template": "{prefix}/{platform}/Modules/modulefiles/{pkg}",
            "cvmfs_shared_path_template": "{prefix}/noarch/{pkg}/{tag}",
            "cvmfs_user_prefix": "{prefix}/user",
        }})
        self.assertEqual(t["path"], "{prefix}/releases/{platform}/Packages/{pkg}/{tag}")

    def test_legacy_alias_cvmfs_path_template(self):
        # cvmfs_path_template is the legacy name for cvmfs_releases_template.
        t = RT({"system": {"prefix": "/cvmfs/x.io",
                           "cvmfs_path_template": "{prefix}/LEG/{pkg}"}})
        self.assertEqual(t["path"], "{prefix}/LEG/{pkg}")

    def test_template_without_prefix_aborts(self):
        # Any template but no prefix -> misconfigured -> dieOnError (SystemExit).
        with self.assertRaises(SystemExit):
            RT({"system": {"cvmfs_releases_template": "{prefix}/x"}})

    def test_bare_top_level_key_honoured(self):
        # A bare top-level prefix (not under system:) is still accepted.
        t = RT({"prefix": "/cvmfs/x.io"})
        self.assertEqual(t["prefix"], "/cvmfs/x.io")

    def test_cvmfs_prefix_alias(self):
        t = RT({"system": {"cvmfs_prefix": "/cvmfs/x.io"}})
        self.assertEqual(t["prefix"], "/cvmfs/x.io")

    def test_injected_prefix_used_when_recipe_has_none(self):
        # A recipe set with no system.prefix + an injected prefix → default layout.
        t = RT({"system": {}}, injected_prefix="/cvmfs/y.io/cms/releases")
        self.assertEqual(t["prefix"], "/cvmfs/y.io/cms/releases")
        self.assertEqual(t["path"], "{prefix}/{platform}/Packages/{pkg}/{tag}")

    def test_prefix_mismatch_aborts(self):
        # Security (fail-closed): a declared prefix that disagrees with the injected
        # authoritative one is a misconfiguration/tamper -> refuse to publish.
        with self.assertRaises(SystemExit):
            RT({"system": {"prefix": "/cvmfs/other-group"}},
               injected_prefix="/cvmfs/my-group")

    def test_matching_declared_and_injected_ok(self):
        # The declared prefix must MIRROR the injected authoritative one; when they
        # agree the build proceeds (injected value is used, trailing slash ignored).
        t = RT({"system": {"prefix": "/cvmfs/my-group"}},
               injected_prefix="/cvmfs/my-group/")
        self.assertEqual(t["prefix"], "/cvmfs/my-group/")

    def test_prefix_below_injected_is_used(self):
        # A narrower tree inside the authorized one is fine and wins.
        t = RT({"system": {"prefix": "/cvmfs/t.io/lhcb/releases"}},
               injected_prefix="/cvmfs/t.io")
        self.assertEqual(t["prefix"], "/cvmfs/t.io/lhcb/releases")

    def test_prefix_above_or_beside_injected_aborts(self):
        for rec in ("/cvmfs/t.io", "/cvmfs/t.io-evil/x", "/cvmfs/t.io/../other"):
            with self.assertRaises(SystemExit):
                RT({"system": {"prefix": rec}}, injected_prefix="/cvmfs/t.io/lhcb")

    def test_repository_swap_keeps_the_group_layout(self):
        t = RT({"system": {"prefix": "/cvmfs/bits.cern.ch/lhcb/releases",
                           "cvmfs_user_prefix": "/cvmfs/bits.cern.ch/lhcb/user",
                           "cvmfs_repository": "test.cvmfs.io",
                           "cvmfs_releases_template": "{prefix}/{release}/{pkg}"}},
               injected_prefix="/cvmfs/test.cvmfs.io")
        self.assertEqual(t["prefix"], "/cvmfs/test.cvmfs.io/lhcb/releases")
        self.assertEqual(t["user_prefix"], "/cvmfs/test.cvmfs.io/lhcb/user")
        self.assertEqual(t["path"], "{prefix}/{release}/{pkg}")

    def test_repository_swap_cannot_escape_a_production_community(self):
        # The overlay used in a production community fails closed.
        with self.assertRaises(SystemExit):
            RT({"system": {"prefix": "/cvmfs/bits.cern.ch/lhcb/releases",
                           "cvmfs_repository": "test.cvmfs.io"}},
               injected_prefix="/cvmfs/bits.cern.ch/lhcb/releases")

    def test_packages_template_is_resolved_and_swapped(self):
        t = RT({"system": {"prefix": "/cvmfs/bits.cern.ch/k4h",
                           "cvmfs_repository": "test.cvmfs.io",
                           "cvmfs_packages_template": "{prefix}/{arch}/{pkg}/{tag}"}})
        self.assertEqual(t["packages"], "{prefix}/{arch}/{pkg}/{tag}")
        self.assertEqual(t["prefix"], "/cvmfs/test.cvmfs.io/k4h")
        self.assertNotIn("packages", RT({"system": {"prefix": "/cvmfs/x.io"}}))
        # No releases template given: no view, the releases path is the packages one.
        self.assertEqual(t["path"], t["packages"])
        v = RT({"system": {"prefix": "/cvmfs/x/g",
                           "cvmfs_packages_template": "{prefix}/{arch}/Packages/{pkg}/{tag}",
                           "cvmfs_views_template": "{prefix}/views/{release}/{arch}",
                           "cvmfs_view_exclude": ["cmake", "ninja"]}})
        self.assertEqual((v["views"], v["view_exclude"]),
                         ("{prefix}/views/{release}/{arch}", ["cmake", "ninja"]))
        self.assertNotIn("views", RT({"system": {"prefix": "/cvmfs/x/g",
                                                 "cvmfs_views_template": "{prefix}/v"}}))

    def test_repository_must_be_a_name(self):
        with self.assertRaises(SystemExit):
            RT({"system": {"prefix": "/cvmfs/a.io/g", "cvmfs_repository": "b.io/x"}})

    def test_recipe_prefix_is_local_dev_fallback(self):
        # With no injected prefix (local build), the recipe prefix is honoured.
        t = RT({"system": {"prefix": "/cvmfs/recipe"}}, injected_prefix=None)
        self.assertEqual(t["prefix"], "/cvmfs/recipe")

    def test_no_prefix_at_all_is_none(self):
        self.assertIsNone(RT({"system": {}}, injected_prefix=None))
        self.assertIsNone(RT({"system": {}}, injected_prefix=""))


class ResolveReleaseTest(unittest.TestCase):
    """resolve_release / path_release / bake_release: the {release} slot + the
    lcg.bits branch label (one value drives both)."""

    # ── _declared_release: where an explicit release: is read from ──────────
    def test_declared_from_variables_then_system_then_toplevel(self):
        self.assertEqual(_declared_release({"variables": {"release": "dev3"}}), "dev3")
        self.assertEqual(_declared_release({"system": {"release": "dev4"}}), "dev4")
        self.assertEqual(_declared_release({"release": "LCG_108"}), "LCG_108")
        # variables win over system/top-level
        self.assertEqual(_declared_release(
            {"variables": {"release": "v"}, "system": {"release": "s"}}), "v")
        self.assertEqual(_declared_release({}), "")
        self.assertEqual(_declared_release(None), "")
        self.assertEqual(_declared_release({"variables": {"release": "  x  "}}), "x")

    # ── precedence: explicit non-trunk > branch > main ─────────────────────
    def test_explicit_nontrunk_wins_over_branch(self):
        self.assertEqual(resolve_release({"variables": {"release": "dev3"}}, "feature-x"), "dev3")

    def test_declared_trunk_does_not_block_branch(self):
        # The base declares release: main (trunk sentinel) — a branch still derives.
        self.assertEqual(resolve_release({"variables": {"release": "main"}}, "feature-x"), "feature-x")
        self.assertEqual(resolve_release({"variables": {"release": "main"}}, ""), "main")

    def test_branch_derivation_plain_branch(self):
        # THE regression this guards: a plain (non -patches) branch must derive,
        # not fall through to main. (Passing the emptied branch_stream broke this.)
        self.assertEqual(resolve_release({}, "feature-x"), "feature-x")
        self.assertEqual(resolve_release({}, "LCG_107"), "LCG_107")

    def test_branch_patches_stripped_to_stream(self):
        # LCG release branches are named <release>-patches; the release is the stream.
        self.assertEqual(resolve_release({}, "LCG_107-patches"), "LCG_107")
        # -patches only stripped as a suffix, not mid-name
        self.assertEqual(resolve_release({}, "my-patches-thing"), "my-patches-thing")

    def test_trunk_branches_and_empty_fall_to_main(self):
        for b in ("main", "master", "HEAD", "Main", "", None):
            self.assertEqual(resolve_release({}, b), "main")

    def test_default_is_main(self):
        self.assertEqual(resolve_release({}), "main")
        self.assertEqual(resolve_release(None), "main")

    # ── path_release: trunk collapses to "", else the value ────────────────
    def test_path_release_collapses_trunk(self):
        for trunk in ("main", "master", "HEAD", "", "  main  ", None):
            self.assertEqual(path_release(trunk), "")
        self.assertEqual(path_release("dev3"), "dev3")
        self.assertEqual(path_release("LCG_107"), "LCG_107")

    # ── bake_release: substitute, or drop the whole {release}/ segment ─────
    def test_bake_substitutes_when_release_set(self):
        self.assertEqual(
            bake_release("/p/releases/{release}/{family}{pkg}/{tag}/{platform}", "dev3"),
            "/p/releases/dev3/{family}{pkg}/{tag}/{platform}")

    def test_bake_collapses_cleanly_when_empty(self):
        # No stray double slash — the segment is removed, not blanked.
        self.assertEqual(
            bake_release("/p/releases/{release}/{family}{pkg}/{tag}/{platform}", ""),
            "/p/releases/{family}{pkg}/{tag}/{platform}")
        self.assertEqual(
            bake_release("/p/releases/{release}/noarch/{pkg}/{tag}", ""),
            "/p/releases/noarch/{pkg}/{tag}")
        # trailing {release} (no following slash) also collapses
        self.assertEqual(bake_release("/p/foo/{release}", ""), "/p/foo")
        # leading {release}/ collapses
        self.assertEqual(bake_release("{release}/foo", ""), "foo")

    def test_bake_noop_without_token_or_template(self):
        self.assertEqual(bake_release("/p/{pkg}/{tag}", ""), "/p/{pkg}/{tag}")
        self.assertEqual(bake_release("/p/{pkg}/{tag}", "dev3"), "/p/{pkg}/{tag}")
        self.assertEqual(bake_release("", "dev3"), "")
        self.assertIsNone(bake_release(None, "dev3"))

    # ── end-to-end: the three canonical cases ──────────────────────────────
    def test_end_to_end_default_main_collapses(self):
        # base on main / no branch -> "main" -> collapsed path (pre-release layout)
        rel = path_release(resolve_release({"variables": {"release": "main"}}, "main"))
        self.assertEqual(rel, "")
        self.assertEqual(
            bake_release("/lcg/releases/{release}/{family}{pkg}/{tag}/{platform}", rel),
            "/lcg/releases/{family}{pkg}/{tag}/{platform}")

    def test_end_to_end_branch_appears(self):
        rel = path_release(resolve_release({"variables": {"release": "main"}}, "LCG_107-patches"))
        self.assertEqual(rel, "LCG_107")
        self.assertEqual(
            bake_release("/lcg/releases/{release}/{family}{pkg}/{tag}/{platform}", rel),
            "/lcg/releases/LCG_107/{family}{pkg}/{tag}/{platform}")

    def test_end_to_end_explicit_release(self):
        rel = path_release(resolve_release({"variables": {"release": "dev3"}}, "main"))
        self.assertEqual(rel, "dev3")


class ResolveReuseFromTest(unittest.TestCase):

    def test_none_and_empty(self):
        self.assertIsNone(resolve_reuse_from(None, None))
        self.assertIsNone(resolve_reuse_from("", {"module_path": "/x"}))

    def test_absolute_path_passthrough(self):
        self.assertEqual(resolve_reuse_from("/cvmfs/x/modules", None),
                         "/cvmfs/x/modules")

    def test_relative_path_rejected(self):
        with self.assertRaises(ValueError):
            resolve_reuse_from("modules", None)

    def test_cvmfs_resolves_from_layout(self):
        layout = R({"cvmfs_dir": "/cvmfs/r"}, ARCH)  # module_path defaults to <arch>/modules
        self.assertEqual(resolve_reuse_from("cvmfs", layout),
                         os.path.join("/cvmfs/r", ARCH, "modules"))

    def test_cvmfs_without_layout_fails(self):
        with self.assertRaises(ValueError):
            resolve_reuse_from("cvmfs", None)
        with self.assertRaises(ValueError):
            resolve_reuse_from("cvmfs", {})  # layout present but no module_path


class ReuseFromOptionTest(unittest.TestCase):
    def test_given_value_wins_over_env(self):
        env = {"BITS_REUSE_FROM": "cvmfs"}
        self.assertEqual(reuse_from_option("/cvmfs/x/modules", env), ("/cvmfs/x/modules", False))
        self.assertEqual(reuse_from_option("cvmfs::relaxed", env), ("cvmfs::relaxed", False))

    def test_empty_value_turns_the_env_default_off(self):
        self.assertEqual(reuse_from_option("", {"BITS_REUSE_FROM": "cvmfs"}), ("", False))

    def test_env_is_the_default(self):
        self.assertEqual(reuse_from_option(None, {"BITS_REUSE_FROM": "cvmfs"}), ("cvmfs", True))

    def test_unset_or_empty_env_means_no_reuse(self):
        self.assertEqual(reuse_from_option(None, {}), (None, False))
        self.assertEqual(reuse_from_option(None, {"BITS_REUSE_FROM": ""}), (None, False))


class SplitReusePolicyTest(unittest.TestCase):

    def test_no_suffix(self):
        self.assertEqual(split_reuse_policy("cvmfs"), ("cvmfs", None))
        self.assertEqual(split_reuse_policy("/cvmfs/x/modulefiles"),
                         ("/cvmfs/x/modulefiles", None))

    def test_none_and_empty(self):
        self.assertEqual(split_reuse_policy(None), (None, None))
        self.assertEqual(split_reuse_policy(""), ("", None))

    def test_policy_suffix(self):
        self.assertEqual(split_reuse_policy("cvmfs::relaxed"), ("cvmfs", "relaxed"))
        self.assertEqual(split_reuse_policy("cvmfs::strict"), ("cvmfs", "strict"))
        self.assertEqual(split_reuse_policy("/cvmfs/x/modulefiles::relaxed"),
                         ("/cvmfs/x/modulefiles", "relaxed"))

    def test_case_insensitive(self):
        self.assertEqual(split_reuse_policy("cvmfs::RELAXED"), ("cvmfs", "relaxed"))

    def test_unknown_suffix_is_not_a_policy(self):
        # A non-policy trailing token is left as part of the source, untouched.
        self.assertEqual(split_reuse_policy("cvmfs::loose"), ("cvmfs::loose", None))


class ReuseModulePathFromTemplatesTest(unittest.TestCase):

    def test_derives_base_from_declared_template(self):
        meta = {"system": {
            "prefix": "/cvmfs/sft.cern.ch/lcg/bits",
            "cvmfs_modules_template": "{prefix}/{platform}/Modules/modulefiles/{pkg}",
        }}
        # Uses the DEPLOYED (raw) arch and strips the trailing /{pkg}.
        self.assertEqual(
            reuse_module_path_from_templates(meta, "x86_64-el9-gcc14-opt"),
            "/cvmfs/sft.cern.ch/lcg/bits/x86_64-el9-gcc14-opt/Modules/modulefiles")

    def test_default_template_when_only_prefix(self):
        # No explicit modules template -> resolve_cvmfs_templates supplies the
        # conventional default, which we still reduce to the base.
        meta = {"system": {"prefix": "/cvmfs/r"}}
        self.assertEqual(reuse_module_path_from_templates(meta, "el9"),
                         "/cvmfs/r/el9/Modules/modulefiles")

    def test_injected_prefix_wins(self):
        meta = {"system": {"cvmfs_modules_template":
                           "{prefix}/{platform}/Modules/modulefiles/{pkg}"}}
        self.assertEqual(
            reuse_module_path_from_templates(meta, "el9", injected_prefix="/cvmfs/inj"),
            "/cvmfs/inj/el9/Modules/modulefiles")

    def test_release_segment_is_baked(self):
        # A {release} template (atlas/lhcb/key4hep/ship) must not leak the token.
        meta = {"system": {"prefix": "/cvmfs/g",
                           "cvmfs_modules_template":
                           "{prefix}/{release}/{platform}/Modules/modulefiles/{pkg}"}}
        self.assertEqual(reuse_module_path_from_templates(meta, "el9", release="LCG_110"),
                         "/cvmfs/g/LCG_110/el9/Modules/modulefiles")
        self.assertEqual(reuse_module_path_from_templates(meta, "el9"),
                         "/cvmfs/g/el9/Modules/modulefiles")

    def test_arch_token(self):
        meta = {"system": {"prefix": "/cvmfs/g",
                           "cvmfs_modules_template":
                           "{prefix}/{arch}/Modules/modulefiles/{pkg}"}}
        self.assertEqual(
            reuse_module_path_from_templates(meta, "el9", arch="el9-gcc14-opt"),
            "/cvmfs/g/el9-gcc14-opt/Modules/modulefiles")

    def test_none_when_no_prefix_or_template(self):
        self.assertIsNone(reuse_module_path_from_templates({}, "el9"))
        self.assertIsNone(reuse_module_path_from_templates(None, "el9"))



class LayoutFileTest(unittest.TestCase):
    """cvmfs.yaml: the layout in a file of its own, under the defaults' system: keys."""

    def setUp(self):
        import tempfile, shutil
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _write(self, name, text):
        with open(os.path.join(self.dir, name), "w") as fh:
            fh.write(text)

    LAYOUT = ('prefix: "/cvmfs/r/g"          # the group root\n'
              'cvmfs_packages_template: "{prefix}/{arch}/Packages/{pkg}/{tag}"\n'
              'cvmfs_modules_template:  "{prefix}/{arch}/Modules/modulefiles/{pkg}"\n'
              'cvmfs_releases_template: "{prefix}/releases/{release}/{pkg}/{version}/{arch}"\n'
              'cvmfs_views_template:    "{prefix}/views/{release}/{arch}"\n')

    def _repo(self, name, files):
        d = os.path.join(self.dir, name)
        os.makedirs(d, exist_ok=True)
        for f, text in files.items():
            with open(os.path.join(d, f), "w") as fh:
                fh.write(text)
        return d

    def _read(self, config_dir, chain, path=""):
        from unittest import mock
        from bits_helpers.defaults import readDefaults
        with mock.patch.dict(os.environ, {"BITS_PATH": path}):
            return RT(readDefaults(config_dir, chain, lambda *_: None, "slc9_x86-64")[0])

    def test_no_file_changes_nothing(self):
        base = self._repo("base", {"defaults-release.sh": "package: defaults-release\nversion: v1\n"
                                   "system:\n  prefix: /cvmfs/x\n---\n"})
        self.assertEqual(self._read(base, ["release"])["prefix"], "/cvmfs/x")

    def test_in_chain_order(self):
        # base (stacks-like: its own layout in the defaults or cvmfs.yaml, a
        # nightly profile) and a group overlay (atlas-like) on the search path.
        nightly = ("package: defaults-dev3\nversion: v1\nsystem:\n"
                   "  cvmfs_views_template: \"{prefix}/views/dev3/{arch}\"\n---\n")
        group = self._repo("group", {
            "defaults-grp.sh": "package: defaults-grp\nversion: v1\n---\n",
            "cvmfs.yaml": 'prefix: /cvmfs/r/grp\ncvmfs_releases_template: ""\n'})
        for base_layout in ("defaults", "file"):
            release = "package: defaults-release\nversion: v1\n"
            files = {"defaults-dev3.sh": nightly}
            if base_layout == "defaults":
                release += ("system:\n  prefix: /cvmfs/r/base\n"
                            "  cvmfs_releases_template: \"{prefix}/releases/{pkg}\"\n")
            else:
                files["cvmfs.yaml"] = self.LAYOUT.replace("/cvmfs/r/g", "/cvmfs/r/base")
            files["defaults-release.sh"] = release + "---\n"
            base = self._repo("base-" + base_layout, files)
            with self.subTest(base_layout=base_layout):
                # The base alone: its own layout.
                self.assertEqual(self._read(base, ["release"])["prefix"], "/cvmfs/r/base")
                # With the group: the group's file wins over the base (defaults or
                # file), and a later profile (dev3) wins over the group's file.
                t = self._read(base, ["release", "grp", "dev3"], path=group)
                self.assertEqual(t["prefix"], "/cvmfs/r/grp")
                if base_layout == "file":   # views need the packages template
                    self.assertEqual(t["views"], "{prefix}/views/dev3/{arch}")
                self.assertNotIn("releases", t["path"])   # cleared by the group's ""

    def test_base_file_never_overrides_the_overlay(self):
        # The overlay brings its own release profile; a later base profile
        # (gcc15) must not bring the base's layout back.
        base = self._repo("base2", {
            "defaults-gcc15.sh": "package: defaults-gcc15\nversion: v1\n---\n",
            "cvmfs.yaml": self.LAYOUT.replace("/cvmfs/r/g", "/cvmfs/r/base")})
        ovl = self._repo("ovl", {
            "defaults-release.sh": "package: defaults-release\nversion: v1\n---\n",
            "defaults-ovl.sh": "package: defaults-ovl\nversion: v1\n---\n",
            "cvmfs.yaml": "prefix: /cvmfs/r/ovl\n"})
        t = self._read(ovl, ["release", "ovl", "gcc15"], path=base)
        self.assertEqual(t["prefix"], "/cvmfs/r/ovl")
        self.assertEqual(t["modules"], "{prefix}/{arch}/Modules/modulefiles/{pkg}")   # the base's

    def test_overlay_layout_in_its_defaults_beats_a_base_file(self):
        # An overlay still in the old format (layout in its defaults) over a
        # base that keeps its layout in cvmfs.yaml.
        base = self._repo("base3", {
            "defaults-gcc15.sh": "package: defaults-gcc15\nversion: v1\n---\n",
            "cvmfs.yaml": self.LAYOUT.replace("/cvmfs/r/g", "/cvmfs/r/base")})
        ovl = self._repo("ovl3", {
            "defaults-release.sh": "package: defaults-release\nversion: v1\n"
                                   "system:\n  prefix: /cvmfs/r/ovl\n---\n",
            "defaults-ovl.sh": "package: defaults-ovl\nversion: v1\n---\n"})
        t = self._read(ovl, ["release", "ovl", "gcc15"], path=base)
        self.assertEqual(t["prefix"], "/cvmfs/r/ovl")

    def test_view_exclude_from_the_more_specific_file(self):
        base = self._repo("base4", {
            "defaults-release.sh": "package: defaults-release\nversion: v1\n---\n",
            "defaults-gcc15.sh": "package: defaults-gcc15\nversion: v1\n---\n",
            "cvmfs.yaml": self.LAYOUT + "cvmfs_view_exclude: [b]\n"})
        ovl = self._repo("ovl4", {
            "defaults-ovl.sh": "package: defaults-ovl\nversion: v1\n---\n",
            "cvmfs.yaml": "cvmfs_view_exclude: [a]\n"})
        # The group's (ovl, the first profile after release) replaces the base's.
        self.assertEqual(self._read(ovl, ["release", "ovl", "gcc15"], path=base)["view_exclude"], ["a"])

    def test_recipe_dir_file_beneath_the_profiles(self):
        # A cvmfs.yaml in the recipe directory no profile came from: beneath them.
        prof = self._repo("prof", {"defaults-release.sh": "package: defaults-release\nversion: v1\n"
                                   "system:\n  prefix: /cvmfs/r/prof\n---\n"})
        here = self._repo("here", {"cvmfs.yaml": self.LAYOUT})
        t = self._read(here, ["release"], path=prof)
        self.assertEqual(t["prefix"], "/cvmfs/r/prof")                       # the profile's
        self.assertEqual(t["modules"], "{prefix}/{arch}/Modules/modulefiles/{pkg}")   # the file's

    def test_bad_files_are_errors(self):
        from bits_helpers.cvmfs_layout import read_layout_file
        for text, msg in (("prefx: /cvmfs/x\n", "unknown key(s) prefx"),
                          ("- /cvmfs/x\n", "expected `key: value` lines"),
                          ("prefix: [\n", "cvmfs.yaml"),
                          ("prefix: 5\n", "prefix must be a string"),
                          ("cvmfs_view_exclude: foo\n", "must be a list of strings")):
            self._write("cvmfs.yaml", text)
            with self.assertRaises(ValueError) as cm:
                read_layout_file(self.dir)
            self.assertIn(msg, str(cm.exception))

    def test_read_defaults_takes_it(self):
        from bits_helpers.defaults import readDefaults
        self._write("defaults-release.sh", "package: defaults-release\nversion: v1\n---\n")
        self._write("cvmfs.yaml", self.LAYOUT)
        from unittest import mock
        with mock.patch.dict(os.environ, {"BITS_PATH": ""}):
            meta, _ = readDefaults(self.dir, ["release"], lambda *_: None, "slc9_x86-64")
        self.assertEqual(RT(meta)["modules"], "{prefix}/{arch}/Modules/modulefiles/{pkg}")
        self._write("cvmfs.yaml", "prefx: /cvmfs/x\n")
        with self.assertRaises(SystemExit):
            readDefaults(self.dir, ["release"], lambda *_: None, "slc9_x86-64")


if __name__ == "__main__":
    unittest.main()
