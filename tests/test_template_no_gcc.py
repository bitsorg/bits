# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""build_template.sh's preamble runs where no gcc is on PATH (it asked for one,
unused, and `set -e` stopped the build there without a message)."""

import os
import shutil
import subprocess
import tempfile
import unittest

import bits_helpers

TEMPLATE = os.path.join(os.path.dirname(bits_helpers.__file__), "build_template.sh")


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class NoGccTest(unittest.TestCase):
  def test_preamble_without_gcc(self):
    with open(TEMPLATE) as f:
      src = f.read()
    start = src.index("set -e\nset +h\n")
    block = src[start:src.index("function hash()", start)]
    path = tempfile.mkdtemp()                 # bash and which only
    self.addCleanup(shutil.rmtree, path)
    for tool in ("which", "head"):
      if shutil.which(tool):
        os.symlink(shutil.which(tool), os.path.join(path, tool))
    r = subprocess.run([shutil.which("bash"), "-c", block + "echo reached"],
                       env={"PATH": path}, capture_output=True, text=True)
    self.assertEqual(r.stdout, "reached\n")


if __name__ == "__main__":
  unittest.main()
