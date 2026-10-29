# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reading the environment of bits modulefiles without Tcl (modulefile_env)."""

import unittest

from bits_helpers import modulefile_env as mfe

# As bits-recipe-tools' ModuleRecipe writes it, plus a recipe's own lines.
ROOT = """#%Module1.0
proc ModulesHelp { } {
  global version
  puts stderr "Modulefile for ROOT 6.36-2"
}
set version 6.36-2
module-whatis "Modulefile for ROOT 6.36-2"
# Dependencies
if ![ is-loaded 'BASE/1.0' ] {
 module load BASE/1.0
}
if ![ is-loaded "zlib/1.3-1" ] { module load zlib/1.3-1 }

set PKG_ROOT $::env(BASEDIR)/ROOT/$version
if {[file isdirectory $PKG_ROOT/bin]} { prepend-path PATH $PKG_ROOT/bin }
if {[file isdirectory $PKG_ROOT/lib]} { prepend-path LD_LIBRARY_PATH $PKG_ROOT/lib }
if {[file isdirectory $PKG_ROOT/lib64]} { prepend-path LD_LIBRARY_PATH $PKG_ROOT/lib64 }
setenv ROOT_INCLUDE_DIR $PKG_ROOT/include
prepend-path CMAKE_PREFIX_PATH $PKG_ROOT
setenv ROOT_ROOT $PKG_ROOT
if {[file isdirectory $PKG_ROOT/include]} { prepend-path ROOT_INCLUDE_PATH $PKG_ROOT/include }
if {[info exists ::env(MANPATH)] && $::env(MANPATH) ne "" && [file isdirectory $PKG_ROOT/share/man]} { prepend-path MANPATH $PKG_ROOT/share/man }
setenv BITS_VIEW 1

setenv ROOTSYS $PKG_ROOT
setenv ZLIB_FROM_ROOT $env(ZLIB_ROOT)/lib
prepend-path ROOT_PLUGIN_PATH $PKG_ROOT/etc/plugins
"""

ZLIB = """#%Module1.0
set version 1.3-1
if ![ is-loaded 'BASE/1.0' ] {
 module load BASE/1.0
}
set PKG_ROOT $::env(BASEDIR)/zlib/$version
if {[file isdirectory $PKG_ROOT/lib]} { prepend-path LD_LIBRARY_PATH $PKG_ROOT/lib }
setenv ZLIB_ROOT $PKG_ROOT
"""

MEMBERS = {"ROOT": "6.36-2", "zlib": "1.3-1"}
DIRS = {("ROOT", "bin"), ("ROOT", "lib"), ("ROOT", "include"), ("zlib", "lib")}


def ops_of(texts, top=("ROOT",), members=MEMBERS, dirs=DIRS):
    return mfe.release_ops(list(top), members, texts.get,
                           lambda pkg, rel: (pkg, rel) in dirs)


class TestReleaseOps(unittest.TestCase):

    def test_a_dependency_is_loaded_where_its_module_is_loaded(self):
        ops, problems = ops_of({"ROOT": ROOT, "zlib": ZLIB})
        self.assertEqual(problems, [])
        r, z = "@B@/ROOT/6.36-2", "@B@/zlib/1.3-1"
        self.assertEqual(ops, [
            ("zlib", "prepend-path", "LD_LIBRARY_PATH", z + "/lib"),
            ("zlib", "setenv", "ZLIB_ROOT", z),
            ("ROOT", "prepend-path", "PATH", r + "/bin"),
            ("ROOT", "prepend-path", "LD_LIBRARY_PATH", r + "/lib"),   # no lib64
            ("ROOT", "setenv", "ROOT_INCLUDE_DIR", r + "/include"),
            ("ROOT", "prepend-path", "CMAKE_PREFIX_PATH", r),
            ("ROOT", "setenv", "ROOT_ROOT", r),
            ("ROOT", "prepend-path", "ROOT_INCLUDE_PATH", r + "/include"),
            ("ROOT", "setenv", "BITS_VIEW", "1"),
            ("ROOT", "setenv", "ROOTSYS", r),
            ("ROOT", "setenv", "ZLIB_FROM_ROOT", z + "/lib"),   # $env() of the dependency
            ("ROOT", "prepend-path", "ROOT_PLUGIN_PATH", r + "/etc/plugins")])

    def test_what_is_not_understood_is_a_problem(self):
        # A module not read leaves what depends on it unset too.
        _ops, problems = ops_of({"ROOT": ROOT, "zlib": ZLIB + "set x [exec uname]\n"})
        self.assertEqual(problems, ["zlib: unknown command: set x [exec uname]",
                                    "ROOT: $env(ZLIB_ROOT) is not set by the release"])
        _ops, problems = ops_of({"ROOT": ROOT, "zlib": ZLIB + "setenv X [exec uname]\n"})
        self.assertEqual(problems[0], "zlib: unknown command: setenv X [exec uname]")
        _ops, problems = ops_of({"ROOT": ROOT, "zlib": ZLIB + 'setenv X "a\\$b"\n'})
        self.assertEqual(problems, ['zlib: command or escape in a\\$b'])
        _ops, problems = ops_of({"ROOT": ROOT.replace("$env(ZLIB_ROOT)", "$env(HOME)"),
                                 "zlib": ZLIB})
        self.assertEqual(problems, ["ROOT: $env(HOME) is not set by the release"])

    def test_a_module_outside_the_release_is_a_problem(self):
        _ops, problems = ops_of({"ROOT": ROOT, "zlib": ZLIB},
                                members={"ROOT": "6.36-2", "zlib": "1.2-1"})
        self.assertEqual(problems[0], "ROOT loads zlib/1.3-1, not a module of this release")
        _ops, problems = ops_of({"ROOT": ROOT})
        self.assertEqual(problems[0], "zlib: no modulefile")

    def test_a_guard_leaving_the_package_is_a_problem(self):
        out = ROOT + ("if {[file isdirectory $PKG_ROOT/../../zlib/1.3-1/lib]} "
                      "{ prepend-path X $PKG_ROOT/../../zlib/1.3-1/lib }\n")
        _ops, problems = ops_of({"ROOT": out, "zlib": ZLIB})
        self.assertEqual(problems, ["ROOT: tests a directory outside the package: "
                                    "@B@/zlib/1.3-1/lib"])

    def test_unbalanced_braces_are_a_problem(self):
        _ops, problems = ops_of({"ROOT": ROOT + "if {1} {\n", "zlib": ZLIB})
        self.assertTrue(problems[0].startswith("ROOT: unbalanced braces"))


class TestLoads(unittest.TestCase):

    def test_a_load_without_a_version_is_a_problem(self):
        # modulecmd takes the tree's default, which may be another release's.
        _ops, problems = ops_of({"ROOT": ROOT.replace("module load zlib/1.3-1", "module load zlib"),
                                 "zlib": ZLIB})
        self.assertEqual(problems[0], "ROOT loads zlib without a version")

    def test_prereq_of_several_modules_is_a_problem(self):
        # modulecmd reads it as any one of them.
        _ops, problems = ops_of({"ROOT": ROOT + "prereq zlib/1.3-1 zstd/1-1\n", "zlib": ZLIB})
        self.assertEqual(problems, ["ROOT: prereq of several modules: prereq zlib/1.3-1 zstd/1-1"])


V = lambda pkg, rel: rel.split("/")[0] in ("bin", "lib", "include")   # the view links these


class TestSetupEnv(unittest.TestCase):

    def test_collapsed_variables_go_to_the_view_others_to_the_package(self):
        ops, _ = ops_of({"ROOT": ROOT, "zlib": ZLIB})
        paths, sets, problems = mfe.setup_env(ops, MEMBERS, V)
        self.assertEqual(problems, [])
        I = mfe.INHERITED
        self.assertEqual(paths, {
            "LD_LIBRARY_PATH": ["@V@/lib", I],
            "PATH": ["@V@/bin", I],
            "CMAKE_PREFIX_PATH": ["@B@/ROOT/6.36-2", I],   # the root: V says not all of it
            "ROOT_INCLUDE_PATH": ["@V@/include", I],
            "ROOT_PLUGIN_PATH": ["@B@/ROOT/6.36-2/etc/plugins", I]})
        # <PKG>_ROOT and the like keep the package: software finds its files there.
        self.assertEqual(sets, {
            "ZLIB_ROOT": "@B@/zlib/1.3-1", "ROOT_INCLUDE_DIR": "@B@/ROOT/6.36-2/include",
            "ROOT_ROOT": "@B@/ROOT/6.36-2", "ROOTSYS": "@B@/ROOT/6.36-2",
            "ZLIB_FROM_ROOT": "@B@/zlib/1.3-1/lib"})
        paths, _sets, _ = mfe.setup_env(ops, MEMBERS, lambda pkg, rel: True)
        self.assertEqual(paths["CMAKE_PREFIX_PATH"], ["@V@", I])

    def test_an_entry_the_view_does_not_link_stays_the_package(self):
        ops, _ = ops_of({"ROOT": ROOT, "zlib": ZLIB})
        paths, _sets, _ = mfe.setup_env(ops, MEMBERS, lambda pkg, rel: pkg != "zlib")
        self.assertEqual(paths["LD_LIBRARY_PATH"], ["@V@/lib", "@B@/zlib/1.3-1/lib", None])

    def test_entries_keep_the_order_modulecmd_gives_them(self):
        r = "@B@/ROOT/6.36-2"
        ops = [("zlib", "prepend-path", "PATH", "@B@/zlib/1.3-1/bin"),
               ("ROOT", "prepend-path", "PATH", r + "/wrappers"),
               ("ROOT", "prepend-path", "PATH", r + "/bin"),
               ("ROOT", "append-path", "PATH", r + "/tools"),
               ("ROOT", "prepend-path", "PATH", r + "/wrappers")]   # there already: stays
        paths, _s, problems = mfe.setup_env(ops, MEMBERS, V)
        # modulecmd: bin:wrappers:zlib/bin:<before>:tools; collapsed, the first stays.
        self.assertEqual(paths["PATH"], ["@V@/bin", r + "/wrappers", None, r + "/tools"])
        self.assertEqual(problems, [])

    def test_values_setup_cannot_reproduce_are_problems(self):
        ops = [("zlib", "setenv", "MSG", "a b"),
               ("zlib", "setenv", "DATA", "/build/sw/INSTALLROOT/x/share"),
               ("zlib", "setenv", "MORE", "@B@/zlib/1.3-1/a:/build/b"),
               ("zlib", "prepend-path", "PATH", "@B@/zlib/1.3-1/a:@B@/zlib/1.3-1/b")]
        paths, sets, problems = mfe.setup_env(ops, MEMBERS, lambda *a: True)
        self.assertEqual((paths, sets), ({}, {}))
        self.assertEqual(problems, [
            "zlib: MSG's value needs quoting: a b",
            "zlib: DATA is an absolute path: /build/sw/INSTALLROOT/x/share",
            "zlib: MORE is an absolute path: @B@/zlib/1.3-1/a:/build/b",
            "zlib: PATH gets several entries at once: @B@/zlib/1.3-1/a:@B@/zlib/1.3-1/b"])


if __name__ == "__main__":
    unittest.main()
