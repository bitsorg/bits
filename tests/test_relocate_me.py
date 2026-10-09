# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""relocate-me.sh: paths into the build area become paths into INSTALL_BASE."""

import os
import shutil
import subprocess
import tempfile
import unittest

import bits_helpers

SCRIPT = os.path.join(os.path.dirname(bits_helpers.__file__), "relocate-me.sh")
PH, PP = "abc123", "x86_64/mypkg/1.0-1"


class RelocateMeTest(unittest.TestCase):
  def _relocate(self, pkg_dir, install_base, strip_pp):
    root = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, root, True)
    os.makedirs(os.path.join(root, "etc", "profile.d"))
    shutil.copy(SCRIPT, root)
    with open(os.path.join(root, "etc", "profile.d", ".bits-pkginfo"), "w") as f:
      f.write("OP=%s\nPP=%s\nPH=%s\nPKG_DIR=%s\n" % (PP, PP, PH, pkg_dir))
    with open(os.path.join(root, "etc", "profile.d", ".bits-relocate"), "w") as f:
      f.write("paths.txt\n")
    with open(os.path.join(root, "paths.txt"), "w") as f:
      f.write("%s/INSTALLROOT/%s/%s/lib\n%s/INSTALLROOT/%s/other/lib\n%s/%s/bin\n"
              % (pkg_dir, PH, PP, pkg_dir, PH, pkg_dir, PP))
    env = dict(os.environ, INSTALL_BASE=install_base)
    env.pop("BITS_RELOCATE_STRIP_PP", None)
    if strip_pp:
      env["BITS_RELOCATE_STRIP_PP"] = "1"
    subprocess.run(["bash", os.path.join(root, "relocate-me.sh")], env=env, check=True)
    with open(os.path.join(root, "paths.txt")) as f:
      return f.read().split()

  def test_local_install(self):
    self.assertEqual(self._relocate("/w/sw", "/opt/sw", False),
                     ["/opt/sw/%s/lib" % PP, "/opt/sw/other/lib", "/opt/sw/%s/bin" % PP])

  def test_publish_strips_the_package_path(self):
    self.assertEqual(self._relocate("/w/sw", "/cvmfs/r/pkg/1.0", True),
                     ["/cvmfs/r/pkg/1.0/lib", "/cvmfs/r/pkg/1.0/other/lib",
                      "/cvmfs/r/pkg/1.0/%s/bin" % PP])

  def test_install_base_containing_pkg_dir_is_not_rewritten_again(self):
    self.assertEqual(self._relocate("/w/sw", "/w/sw/stage/pkg/1.0", True),
                     ["/w/sw/stage/pkg/1.0/lib", "/w/sw/stage/pkg/1.0/other/lib",
                      "/w/sw/stage/pkg/1.0/%s/bin" % PP])


if __name__ == "__main__":
  unittest.main()
