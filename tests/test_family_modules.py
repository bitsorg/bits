# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits q/printenv/enter with package_family: a package installed under
<arch>/<family>/<pkg>/<version> is found, and its modulefile (written for
$BASEDIR/<pkg>/<version>, the layout on CVMFS) points at it.
Needs a real modulecmd (Environment Modules): on PATH, or $BITS_TEST_MODULECMD.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCH = "slc9_x86-64"


def _modulecmd():
  m = os.environ.get("BITS_TEST_MODULECMD") or shutil.which("modulecmd")
  return m if m and os.access(m, os.X_OK) else None


def _package(sw, path, pkg, verrev, deps=()):
  """A package as bits-recipe-tools' ModuleRecipe writes its modulefile."""
  root = os.path.join(sw, ARCH, path)
  os.makedirs(os.path.join(root, "etc", "modulefiles"))
  os.makedirs(os.path.join(root, "etc", "profile.d"))
  with open(os.path.join(root, "etc", "profile.d", "init.sh"), "w") as f:
    f.write("export %s_ROOT=%s\n" % (pkg.upper(), root))
  lines = ["#%Module1.0", "set version " + verrev,
           "if ![ is-loaded 'BASE/1.0' ] {\n module load BASE/1.0\n}"]
  lines += ["if ![ is-loaded \"%s\" ] { module load %s }" % (d, d) for d in deps]
  lines += ["set PKG_ROOT $::env(BASEDIR)/%s/$version" % pkg,
            "setenv %s_ROOT $PKG_ROOT" % pkg.upper()]
  with open(os.path.join(root, "etc", "modulefiles", pkg), "w") as f:
    f.write("\n".join(lines) + "\n")
  return root


@unittest.skipUnless(_modulecmd(), "needs modulecmd (Environment Modules)")
class FamilyModulesTest(unittest.TestCase):
  def setUp(self):
    self.tmp = os.path.realpath(tempfile.mkdtemp())
    self.addCleanup(shutil.rmtree, self.tmp, True)
    self.work = os.path.join(self.tmp, "work")
    self.sw = os.path.join(self.work, "sw")
    self.zlib = _package(self.sw, "zlib/1.3-1", "zlib", "1.3-1")
    self.root = _package(self.sw, "lcg/ROOT/6.36-1", "ROOT", "6.36-1", deps=["zlib/1.3-1"])
    os.symlink("6.36-1", os.path.join(self.sw, ARCH, "lcg", "ROOT", "latest"))
    mc = _modulecmd()
    self.env = dict(os.environ, HOME=self.tmp, PATH=os.path.dirname(mc) + ":/usr/bin:/bin",
                    MODULES_SHELL="bash", BITS_CVMFS_PREFIX="")
    for v in ("BITS_WORK_DIR", "MODULEPATH", "LOADEDMODULES", "_LMFILES_", "BASEDIR"):
      self.env.pop(v, None)

  def _bits(self, *args):
    return subprocess.run([os.path.join(ROOT, "bits"), "-a", ARCH] + list(args),
                          cwd=self.work, env=self.env, capture_output=True, text=True)

  def test_q_lists_family_packages(self):
    r = self._bits("q")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(r.stdout.split(), ["ROOT/6.36-1", "ROOT/latest", "zlib/1.3-1"])

  def test_printenv_finds_the_package_under_its_family(self):
    for _ in range(2):    # the second run uses the refreshed modules cache
      r = self._bits("printenv", "ROOT/6.36-1")
      self.assertEqual(r.returncode, 0, r.stderr)
      show = subprocess.run(["bash", "-c", r.stdout + '\necho "$ROOT_ROOT $ZLIB_ROOT"'],
                            capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
      self.assertEqual(show.stdout.split(), [self.root, self.zlib])

  def test_dev_load_prints_the_family_path(self):
    r = self._bits("load", "--dev", "ROOT/6.36-1")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertIn(". %s/etc/profile.d/init.sh" % self.root, r.stderr)

  def test_cache_follows_new_and_removed_family_versions(self):
    cache = os.path.join(self.sw, "MODULES", ARCH, "ROOT")
    self.assertEqual(self._bits("printenv", "ROOT/6.36-1").returncode, 0)
    self.assertTrue(os.path.exists(os.path.join(cache, "6.36-1")))
    new = _package(self.sw, "lcg/ROOT/6.38-1", "ROOT", "6.38-1")
    shutil.rmtree(self.root)
    r = self._bits("printenv", "ROOT/6.38-1")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertIn(new, r.stdout)
    self.assertEqual(sorted(os.listdir(cache)), ["6.38-1"])   # 6.36-1 pruned (latest dangles)

  def test_names_with_dots(self):
    _package(self.sw, "lcg/py.test/8.0-1", "py.test", "8.0-1")
    _package(self.sw, "lcg/pyXtest/1.0-1", "pyXtest", "1.0-1")
    r = self._bits("printenv", "py.test/8.0-1")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertIn(os.path.join(self.sw, ARCH, "lcg", "py.test", "8.0-1"), r.stdout)


if __name__ == "__main__":
  unittest.main()
