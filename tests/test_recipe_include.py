# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the recipe-body `#!include` directive (a narrow text splice resolved
in parseRecipe, before variable substitution and hashing)."""

import os
import shutil
import tempfile
import unittest

from bits_helpers.utilities import resolveIncludes, parseRecipe, resolve_spec_data


class BufferReader:
    def __init__(self, url, recipe):
        self.url = url
        self.buffer = recipe

    def __call__(self):
        return self.buffer


class TestRecipeInclude(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, rel, content):
        p = os.path.join(self.tmp, rel)
        os.makedirs(os.path.dirname(p) or self.tmp, exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
        return p

    def test_quote_include_relative_to_recipe_dir(self):
        self._write("scram-build.sh", "useCompiler=%(compiler)s\n")
        recipe_url = os.path.join(self.tmp, "cms.sh")
        body = 'echo start\n#!include "scram-build.sh"\necho end\n'
        out = resolveIncludes(body, recipe_url)
        self.assertNotIn("#!include", out)
        self.assertEqual(out, "echo start\nuseCompiler=%(compiler)s\n\necho end\n")

    def test_angle_include_resolves_under_repo_root(self):
        self._write("cms.bits/scram-build.sh", "TOOLS=%(enable_tools)s\n")
        # Recipe lives in a different subdirectory; <...> must resolve under repo root.
        recipe_url = os.path.join(self.tmp, "other/cms.sh")
        out = resolveIncludes('#!include <cms.bits/scram-build.sh>\n', recipe_url, repo_dir=self.tmp)
        self.assertIn("TOOLS=%(enable_tools)s", out)

    def test_included_vars_expand_soft_and_shell_percent_survives(self):
        # The snippet carries a bits var AND bare shell `%` (bash subst, printf).
        self._write("snippet.sh", 'cc=%(compiler)s\ntrim=${name//-/_}\nprintf "%d\\n" "$n"\n')
        recipe_url = os.path.join(self.tmp, "pkg.sh")
        spliced = resolveIncludes('#!include "snippet.sh"\n', recipe_url)
        # Soft expansion (the mode used when vars come from --defaults): known
        # %(var)s expand, bare shell % is left untouched.
        out = resolve_spec_data({"package": "pkg"}, spliced, ["release"],
                                default_vars={"compiler": "gcc15"}, strict=False)
        self.assertIn("cc=gcc15", out)
        self.assertIn("${name//-/_}", out)
        self.assertIn('printf "%d\\n"', out)

    def test_editing_included_file_changes_resolved_body(self):
        p = self._write("snippet.sh", "VERSION=1\n")
        recipe_url = os.path.join(self.tmp, "pkg.sh")
        body = '#!include "snippet.sh"\n'
        a = resolveIncludes(body, recipe_url)
        with open(p, "w") as f:
            f.write("VERSION=2\n")
        b = resolveIncludes(body, recipe_url)
        # The resolved body differs → the consumer's recipe hash changes (the
        # "stale snippet doesn't rebuild" class of bug is avoided).
        self.assertNotEqual(a, b)

    def test_nested_include(self):
        self._write("inner.sh", "INNER=1\n")
        self._write("outer.sh", 'OUTER=1\n#!include "inner.sh"\n')
        recipe_url = os.path.join(self.tmp, "pkg.sh")
        out = resolveIncludes('#!include "outer.sh"\n', recipe_url)
        self.assertIn("OUTER=1", out)
        self.assertIn("INNER=1", out)

    def test_missing_file_errors_clearly(self):
        recipe_url = os.path.join(self.tmp, "pkg.sh")
        with self.assertRaises(RuntimeError) as cm:
            resolveIncludes('#!include "nope.sh"\n', recipe_url)
        self.assertIn("cannot open", str(cm.exception))

    def test_cycle_detected(self):
        self._write("a.sh", '#!include "b.sh"\n')
        self._write("b.sh", '#!include "a.sh"\n')
        recipe_url = os.path.join(self.tmp, "a.sh")
        with self.assertRaises(RuntimeError) as cm:
            resolveIncludes('#!include "a.sh"\n', recipe_url)
        self.assertIn("cyclic", str(cm.exception))

    def test_path_traversal_rejected(self):
        os.makedirs(os.path.join(self.tmp, "sub"), exist_ok=True)
        recipe_url = os.path.join(self.tmp, "sub/pkg.sh")
        with self.assertRaises(RuntimeError) as cm:
            resolveIncludes('#!include "../../etc/passwd"\n', recipe_url)
        self.assertIn("unsafe path", str(cm.exception))

    def test_absolute_path_rejected(self):
        recipe_url = os.path.join(self.tmp, "pkg.sh")
        with self.assertRaises(RuntimeError) as cm:
            resolveIncludes('#!include "/etc/passwd"\n', recipe_url)
        self.assertIn("unsafe path", str(cm.exception))

    def test_plain_shell_comments_left_untouched(self):
        # None of these are the strict `#!include <...>`/"..." directive form.
        body = '#!/bin/bash\n# include the headers below\n#includes are great\necho hi\n'
        out = resolveIncludes(body, os.path.join(self.tmp, "pkg.sh"))
        self.assertEqual(out, body)

    def test_literal_c_include_in_body_is_not_touched(self):
        # Regression: recipe bodies embed literal C `#include <header>` lines in
        # heredocs that generate test programs (e.g. lcg.bits/gcc-toolchain.sh).
        # The `#!include` marker must NOT collide with them — they pass through
        # verbatim and are never treated as a recipe include.
        body = (
            'cat > test.c <<EOF\n'
            '#include <string.h>\n'
            '#include <stdio.h>\n'
            '  #include <openssl/bio.h>\n'
            'int main(){ return 0; }\n'
            'EOF\n'
            '$CC test.c\n'
        )
        out = resolveIncludes(body, os.path.join(self.tmp, "gcc-toolchain.sh"),
                              repo_dir=self.tmp)
        self.assertEqual(out, body)  # untouched — no attempt to splice <string.h>

    def test_marker_and_c_include_coexist(self):
        # A real recipe can use the bits directive AND embed C includes; only the
        # `#!include` line is spliced.
        self._write("flags.sh", "CFLAGS=-O2\n")
        recipe_url = os.path.join(self.tmp, "pkg.sh")
        body = '#!include "flags.sh"\ncat > t.c <<EOF\n#include <string.h>\nEOF\n'
        out = resolveIncludes(body, recipe_url)
        self.assertIn("CFLAGS=-O2", out)        # bits directive spliced
        self.assertIn("#include <string.h>", out)  # C include preserved
        self.assertNotIn("#!include", out)

    def test_parseRecipe_splices_body(self):
        self._write("snip.sh", "echo from-include\n")
        recipe_url = os.path.join(self.tmp, "pkg.sh")
        text = 'package: pkg\nversion: 1\n---\n#!include "snip.sh"\n'
        err, spec, body = parseRecipe(BufferReader(recipe_url, text))
        self.assertIsNone(err)
        self.assertEqual(spec["package"], "pkg")
        self.assertIn("echo from-include", body)
        self.assertNotIn("#!include", body)

    def test_parseRecipe_missing_include_becomes_error(self):
        recipe_url = os.path.join(self.tmp, "pkg.sh")
        text = 'package: pkg\nversion: 1\n---\n#!include "missing.sh"\n'
        err, spec, body = parseRecipe(BufferReader(recipe_url, text))
        self.assertIsNotNone(err)
        self.assertIn("#!include", err)


if __name__ == "__main__":
    unittest.main()
