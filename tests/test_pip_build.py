# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""pip_build (bits_helpers/build.py): how PythonPipRecipe packages install, and
that only those built otherwise than from wheels change their hash."""

import os
import unittest
from collections import OrderedDict
from types import SimpleNamespace
from unittest import mock

from bits_helpers import build, hashing

PIP = "#!/bin/bash\n. $(bits-include PythonPipRecipe)\n"
MAKE = "#!/bin/bash\nmake install\n"


def _hash(recipe, mode):
  spec = {"package": "six", "version": "1.17.0", "recipe": recipe, "commit_hash": "1.17.0",
          "is_devel_pkg": False, "requires": [], "env": OrderedDict(), "append_path": OrderedDict(),
          "prepend_path": OrderedDict()}
  build._apply_pip_build(spec, mode)
  hashing.storeHashes("six", {"six": spec}, considerRelocation=False)
  return spec["remote_revision_hash"]


class PipBuildTest(unittest.TestCase):
  def setUp(self):
    p = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "BITS_PIP_BUILD"}, clear=True)
    p.start()
    self.addCleanup(p.stop)

  def test_which_mode(self):
    self.assertEqual(build._pip_build(None), "wheel")
    group = {"system": {"pip_build": "source"}}
    self.assertEqual(build._pip_build(group), "source")
    os.environ["BITS_PIP_BUILD"] = "all"                                 # one build, over the group
    self.assertEqual(build._pip_build(group), "all")
    self.assertEqual(build._pip_build(group, SimpleNamespace(pipBuild="wheel")), "wheel")   # --pip-build
    with mock.patch.object(build, "dieOnError", side_effect=SystemExit) as die:
      os.environ["BITS_PIP_BUILD"] = "sdist"
      with self.assertRaises(SystemExit):
        build._pip_build(None)
      die.assert_called_once()

  def test_only_pip_recipes_are_marked(self):
    for recipe, mode, mark in ((PIP, "source", "source"), (PIP, "all", "all"), (PIP, "wheel", None),
                               (MAKE, "source", None)):
      spec = {"recipe": recipe}
      build._apply_pip_build(spec, mode)
      self.assertEqual(spec.get("pip_build"), mark, (recipe, mode))

  def test_hash(self):
    wheel = _hash(PIP, "wheel")
    self.assertEqual(_hash(MAKE, "source"), _hash(MAKE, "wheel"))         # not a pip package: unchanged
    self.assertEqual(len({wheel, _hash(PIP, "source"), _hash(PIP, "all")}), 3)

  # This pip package's hash from before pip_build existed: wheel must not move it.
  GOLDEN = "8afe0b60aa57dff5fc501c06d23221ff2cf26ef3"

  def test_wheel_keeps_the_hash(self):
    with mock.patch.object(hashing, "BUILD_EPOCH", 0):
      self.assertEqual(_hash(PIP, "wheel"), self.GOLDEN)


if __name__ == "__main__":
  unittest.main()
