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

  def test_a_staging_dir_under_the_work_dir_is_not_doubled(self):
    # A cached tarball is relocated first into INSTALLROOT/<hash>, under PKG_DIR.
    stage = "/w/sw/INSTALLROOT/%s" % PH
    self.assertEqual(self._relocate("/w/sw", stage, False),
                     ["%s/%s/lib" % (stage, PP), "%s/other/lib" % stage,
                      "%s/%s/bin" % (stage, PP)])

  def test_a_cached_tarball_is_relocated_with_this_bits_script(self):
    # The tarball's relocate-me.sh is from the bits that packed it (older ones
    # double INSTALLROOT/<hash> for a staging dir under the work dir).
    with open(os.path.join(os.path.dirname(SCRIPT), "build_template.sh")) as f:
      t = f.read()
    cached = t[t.index('tar -xzf "$CACHED_TARBALL"'):]
    install = cached.index('install "${BITS_SCRIPT_DIR}/bits_helpers/relocate-me.sh"')
    self.assertLess(install, cached.index('/bin/bash -ex "$INSTALLROOT/relocate-me.sh"'))


if __name__ == "__main__":
  unittest.main()
