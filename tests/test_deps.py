# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import patch, MagicMock
from io import StringIO
import os.path
import json
import subprocess
import sys

from bits_helpers.deps import doDeps, deps_graph, deps_makefile
from argparse import Namespace
import unittest

RECIPES = {
    "/dist/defaults-release.sh": """\
package: defaults-release
version: v1
---
""",
    "/dist/gcc-toolchain.sh": """\
package: GCC-Toolchain
version: v1
---
""",
    "/dist/aliroot.sh": """\
package: AliRoot
version: v1
requires:
  - ROOT
  - GCC-Toolchain
---
""",
    "/dist/root.sh": """\
package: ROOT
version: v1
build_requires:
  - GCC-Toolchain
---
""",
}


class DepsTestCase(unittest.TestCase):

    def test_graph_deterministic_across_processes(self):
        code = '''
import json
from bits_helpers.deps import deps_graph
edges = {"base": [], "alpha": ["base"], "beta": ["base"],
         "app": ["beta", "alpha"]}
specs = {p: {"package": p, "requires": edges[p], "runtime_requires": edges[p]}
         for p in set(edges)}
print(json.dumps(deps_graph(specs, "app", runtime_only=True)))
'''
        expected = json.dumps({"alpha": ["base"], "app": ["alpha", "beta"],
                               "base": [], "beta": ["base"]})
        for seed in ("1", "2", "3", "4"):
            with self.subTest(seed=seed):
                output = subprocess.check_output(
                    [sys.executable, "-c", code], text=True,
                    env=dict(os.environ, PYTHONHASHSEED=seed))
                self.assertEqual(output.strip(), expected)

    def test_runtime_graph_ignores_build_only_and_unrelated_packages(self):
        def spec(p, runtime=(), build=()):
            return {"package": p, "requires": list(runtime) + list(build),
                    "runtime_requires": list(runtime)}
        specs = {"app": spec("app", ["beta", "alpha"]),
                 "alpha": spec("alpha"), "beta": spec("beta")}
        expected = json.dumps(deps_graph(specs, "app", runtime_only=True))
        specs["alpha"] = spec("alpha", build=["tool"])
        specs["tool"] = spec("tool")
        specs["unrelated"] = spec("unrelated", ["beta"])
        specs = dict(reversed(list(specs.items())))
        self.assertEqual(json.dumps(deps_graph(specs, "app", runtime_only=True)), expected)
        full = deps_graph(specs, "app")
        self.assertEqual(full["alpha"], ["tool"])
        self.assertEqual(list(full), ["alpha", "app", "beta", "tool"])

    def test_metadata_graph_reads_only_reachable_specs_once(self):
        class CountingSpecs(dict):
            def __getitem__(self, key):
                reads.append(key)
                return super().__getitem__(key)

        reads = []
        specs = CountingSpecs({
            str(i): {"runtime_requires": [str(j) for j in range(max(0, i - 6), i)]}
            for i in range(900)
        })
        specs["unrelated"] = {"runtime_requires": ["missing"]}
        graph = deps_graph(specs, "899", runtime_only=True)
        self.assertEqual(len(graph), 900)
        self.assertEqual(len(reads), 900)
        self.assertEqual(len(set(reads)), 900)

    def test_graph_skips_names_without_a_spec(self):
        # An untracked dep satisfied by the system is disabled during resolution
        # and has no spec; it must not crash metadata generation.
        specs = {"app": {"runtime_requires": ["lib"], "untracked_requires": ["Python"]},
                 "lib": {"runtime_requires": []}}
        self.assertEqual(deps_graph(specs, "app", runtime_only=True),
                         {"app": ["lib"], "lib": []})

    def test_makefile_rejects_cycle(self):
        specs = {"a": {"requires": ["b"]}, "b": {"requires": ["a"]}}
        with self.assertRaises(SystemExit):
            deps_makefile(specs, "a")

    @patch("bits_helpers.deps.open")
    @patch("bits_helpers.deps.execute", new=lambda cmd: True)
    @patch("bits_helpers.recipe.open", new=lambda f: StringIO(RECIPES[f]))
    @patch("bits_helpers.paths.exists", new=lambda f: f in RECIPES)
    @patch("bits_helpers.defaults.exists", new=lambda f: f in RECIPES)
    def test_deps(self, mockDepsOpen):
        """Check doDeps doesn't raise an exception."""
        dot = StringIO()
        dot.name = ""

        def depsOpen(fn, mode):
            dot.name = fn
            return dot
        mockDepsOpen.side_effect = depsOpen

        args = Namespace(workDir="/work",
                         configDir="/dist",
                         debug=False,
                         docker=False,
                         dockerImage=None,
                         docker_extra_args=["--network=host"],
                         preferSystem=[],
                         noSystem="*",
                         architecture="slc7_x86-64",
                         disable=[],
                         neat=True,
                         outdot="/tmp/out.dot",
                         outgraph="/tmp/outgraph.pdf",
                         package="AliRoot",
                         defaults=["release"],
                         environment=[])

        def fake_exists(n):
            return True if n in RECIPES else False

        with patch.object(os.path, "exists", fake_exists):
          doDeps(args, MagicMock())

          # Same check without explicit intermediate dotfile
          args.outdot = None
          doDeps(args, MagicMock())

    @patch("bits_helpers.deps.execute")
    @patch("bits_helpers.recipe.open", new=lambda f: StringIO(RECIPES[f]))
    @patch("bits_helpers.paths.exists", new=lambda f: f in RECIPES)
    @patch("bits_helpers.defaults.exists", new=lambda f: f in RECIPES)
    def test_deps_makefile(self, mockExecute):
        """--outmake writes dependency rules in build order and skips Graphviz."""
        def makefile(runtime_only, package="ROOT"):
            out = StringIO()
            out.close = lambda: None
            args = Namespace(workDir="/work", configDir="/dist", debug=False,
                             docker=False, dockerImage=None, docker_extra_args=[],
                             preferSystem=False, noSystem="*", architecture="slc7_x86-64",
                             disable=[], neat=False, outdot=None, outgraph=None,
                             outmake="/tmp/Makefile", runtimeOnly=runtime_only,
                             package=package, defaults=["release"], environment=[])
            with patch.object(os.path, "exists", lambda n: n in RECIPES), \
                 patch("bits_helpers.deps.open", return_value=out) as mockOpen:
              doDeps(args, MagicMock())
            mockOpen.assert_called_once_with("/tmp/Makefile", "w")
            return out.getvalue().splitlines()[1:]   # drop the header comment

        # ROOT build_requires GCC-Toolchain.
        self.assertEqual(makefile(False), ["GCC-Toolchain:", "ROOT: GCC-Toolchain"])
        self.assertEqual(makefile(True), ["ROOT:"])
        # AliRoot requires ROOT and GCC-Toolchain at runtime; ROOT's own
        # build-only edge to GCC-Toolchain is dropped with --runtime-only.
        self.assertEqual(makefile(False, "AliRoot"),
                         ["GCC-Toolchain:", "ROOT: GCC-Toolchain",
                          "AliRoot: GCC-Toolchain ROOT"])
        self.assertEqual(makefile(True, "AliRoot"),
                         ["GCC-Toolchain:", "ROOT:", "AliRoot: GCC-Toolchain ROOT"])
        mockExecute.assert_not_called()

if __name__ == '__main__':
    unittest.main()
