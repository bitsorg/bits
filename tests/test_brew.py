# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for `bits brew` Brewfile generation (bits_helpers.brew)."""

import os
import tempfile
import unittest

from bits_helpers.brew import collect_homebrew, render_brewfile, _as_list


RECIPES = {
    # macOS Homebrew-sourced, formula == package name.
    "readline.sh": (
        "package: readline\n"
        "version: system\n"
        'prefer_system: ".*"\n'
        "homebrew_formula: readline\n"
        "---\n"
    ),
    # Formula name differs from the package name; carries a tap.
    "png.sh": (
        "package: png\n"
        "version: system\n"
        'prefer_system: "osx.*"\n'
        "homebrew_formula: libpng\n"
        "homebrew_taps:\n"
        "  - example/tap\n"
        "---\n"
    ),
    # Declares a formula but gated to Linux only -> excluded on osx.
    "linonly.sh": (
        "package: linonly\n"
        "version: system\n"
        'prefer_system: "(?!osx).*"\n'
        "homebrew_formula: linonly\n"
        "---\n"
    ),
    # A normal built package: no homebrew_formula -> never contributes.
    "zlib.sh": (
        "package: zlib\n"
        "version: 1.3\n"
        "---\n"
        "#!/bin/bash\n"
        "true\n"
    ),
    # A defaults helper that is not a valid recipe -> skipped quietly.
    "defaults-release.sh": (
        "#!/bin/bash\n"
        "echo not a recipe\n"
    ),
}


class TestBrew(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bits_brew_test_")
        for name, body in RECIPES.items():
            with open(os.path.join(self.tmp, name), "w") as fh:
                fh.write(body)

    def test_as_list(self):
        self.assertEqual(_as_list(None), [])
        self.assertEqual(_as_list("a"), ["a"])
        self.assertEqual(_as_list(["a", " b ", ""]), ["a", "b"])

    def test_collect_osx(self):
        formulae, taps = collect_homebrew(self.tmp, "osx_arm64")
        # recipe formulae + the build-system base (gnu-tar).
        self.assertEqual(formulae, {"readline", "libpng", "gnu-tar"})
        self.assertEqual(taps, {"example/tap"})

    def test_base_formula_gnu_tar_always_on_osx(self):
        # gnu-tar (gtar) is required by build_template.sh for reproducible
        # tarballs, so it must be emitted even with no recipes declaring it.
        empty = tempfile.mkdtemp(prefix="bits_brew_empty_")
        formulae, _ = collect_homebrew(empty, "osx_arm64")
        self.assertIn("gnu-tar", formulae)

    def test_collect_excludes_linux_only_on_osx(self):
        formulae, _ = collect_homebrew(self.tmp, "osx_arm64")
        self.assertNotIn("linonly", formulae)

    def test_collect_linux_is_empty(self):
        # The Brewfile is a macOS artifact; a non-osx target yields nothing,
        # even for recipes whose prefer_system is ".*".
        formulae, taps = collect_homebrew(self.tmp, "slc9_x86-64")
        self.assertEqual(formulae, set())
        self.assertEqual(taps, set())

    def test_render_is_sorted_and_deterministic(self):
        out = render_brewfile({"readline", "libpng"}, {"example/tap"}, "osx_arm64")
        self.assertIn('tap "example/tap"', out)
        # brew lines sorted: libpng before readline
        self.assertLess(out.index('brew "libpng"'), out.index('brew "readline"'))
        self.assertEqual(out, render_brewfile({"libpng", "readline"}, {"example/tap"}, "osx_arm64"))
        self.assertTrue(out.endswith("\n"))


if __name__ == "__main__":
    unittest.main()
