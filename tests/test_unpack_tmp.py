# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""build_template.sh unpacks a reused tarball into an emptied TMP/<hash>: files an
interrupted unpack left there do not end up in the installed package."""

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest

import bits_helpers

TEMPLATE = os.path.join(os.path.dirname(bits_helpers.__file__), "build_template.sh")


def _unpack_block():
  """The unpack branch, from the TMP reset to the move into INSTALLROOT."""
  with open(TEMPLATE) as f:
    src = f.read()
  start = src.index('  rm -rf "$BUILDROOT/log"\n', src.index('echo "bits:stage:unpack"'))
  end = src.index('"$INSTALLROOT"\n', src.index('  mv "$WORK_DIR/TMP/$PKGHASH/$PKGPATH"')) + 15
  return src[start:end].replace("%%", "%")


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class UnpackTmpTest(unittest.TestCase):
  def test_leftovers_are_not_installed(self):
    work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, work)
    tarball = os.path.join(work, "p.tar.gz")
    with tarfile.open(tarball, "w:gz") as tf:
      info = tarfile.TarInfo("arch/p/1-1/a.txt")
      info.size = 2
      tf.addfile(info, io.BytesIO(b"a\n"))
    os.makedirs(os.path.join(work, "TMP", "abc", "arch", "p", "1-1"))
    with open(os.path.join(work, "TMP", "abc", "arch", "p", "1-1", "stale.txt"), "w") as f:
      f.write("left by a killed unpack\n")
    installroot = os.path.join(work, "INSTALLROOT", "abc", "arch", "p", "1-1")
    env = dict(os.environ, WORK_DIR=work, PKGHASH="abc", PKGPATH="arch/p/1-1",
               CACHED_TARBALL=tarball, INSTALLROOT=installroot, BUILDROOT=work + "/BUILD")
    script = '_bits_reset() { rm -rf "$1"; }\nset -e\n' + _unpack_block()
    subprocess.run(["bash", "-c", script], env=env, check=True, capture_output=True)
    self.assertEqual(sorted(os.listdir(installroot)), ["a.txt"])


if __name__ == "__main__":
  unittest.main()
