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
    resolve_release, path_release, bake_release, _declared_release)

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

    def test_fallback_prefix_used_when_recipe_has_none(self):
        # A recipe set with no system.prefix + a fallback → default layout.
        t = RT({"system": {}}, fallback_prefix="/cvmfs/y.io/cms/releases")
        self.assertEqual(t["prefix"], "/cvmfs/y.io/cms/releases")
        self.assertEqual(t["path"], "{prefix}/{platform}/Packages/{pkg}/{tag}")

    def test_recipe_prefix_wins_over_fallback(self):
        t = RT({"system": {"prefix": "/cvmfs/recipe"}}, fallback_prefix="/cvmfs/fb")
        self.assertEqual(t["prefix"], "/cvmfs/recipe")

    def test_no_prefix_no_fallback_is_none(self):
        self.assertIsNone(RT({"system": {}}, fallback_prefix=None))
        self.assertIsNone(RT({"system": {}}, fallback_prefix=""))


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


if __name__ == "__main__":
    unittest.main()
