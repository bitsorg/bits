# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

from unittest.mock import patch, MagicMock
from io import StringIO
import os.path

from bits_helpers.deps import doDeps
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
        self.assertEqual(makefile(False), ["defaults-release:", "GCC-Toolchain: defaults-release",
                          "ROOT: defaults-release GCC-Toolchain"])
        self.assertEqual(makefile(True), ["defaults-release:", "ROOT: defaults-release"])
        # AliRoot requires ROOT and GCC-Toolchain at runtime; ROOT's own
        # build-only edge to GCC-Toolchain is dropped with --runtime-only.
        self.assertEqual(makefile(False, "AliRoot"),
                         ["defaults-release:", "GCC-Toolchain: defaults-release",
                          "ROOT: defaults-release GCC-Toolchain",
                          "AliRoot: defaults-release GCC-Toolchain ROOT"])
        self.assertEqual(makefile(True, "AliRoot"),
                         ["defaults-release:", "GCC-Toolchain: defaults-release",
                          "ROOT: defaults-release", "AliRoot: defaults-release GCC-Toolchain ROOT"])
        mockExecute.assert_not_called()

if __name__ == '__main__':
    unittest.main()
