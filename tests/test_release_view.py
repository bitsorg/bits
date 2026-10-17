# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits setenv / enter / printenv of a release with a complete view on CVMFS:
its setup.sh stands in for loading the modules, with no modulecmd and no Python.

python3 and modulecmd are stubs that record being run, so the tests also show
when the modules path is taken.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

from bits_helpers import cvmfs_publish as cp

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCH = "x86_64-el9-gcc14-opt"

TOP_MF = """#%Module1.0
if ![ is-loaded 'BASE/1.0' ] { module load BASE/1.0 }
if ![ is-loaded "dep/2-1" ] { module load dep/2-1 }
setenv TOP_ROOT $::env(BASEDIR)/top/1-1
setenv BITS_VIEW 1
"""


def _write(path, text, mode=0o644):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    f.write(text)
  os.chmod(path, mode)


class ReleaseViewTest(unittest.TestCase):
  def setUp(self):
    self.tmp = os.path.realpath(tempfile.mkdtemp())
    self.addCleanup(shutil.rmtree, self.tmp, True)
    self.inst = os.path.join(self.tmp, "inst")
    os.makedirs(self.inst)
    for f in ("bits", "bitsBuild"):
      shutil.copy2(os.path.join(ROOT, f), self.inst)
    self.calls = os.path.join(self.tmp, "calls")
    stubs = os.path.join(self.tmp, "stubs")
    for name in ("python3", "modulecmd"):
      _write(os.path.join(stubs, name),
             '#!/bin/sh\necho "%s $*" >> "%s"\nexit 1\n' % (name, self.calls), 0o755)
    self.work = os.path.join(self.tmp, "work")
    os.makedirs(os.path.join(self.work, "sw", ARCH))
    # The community's tree, as bits cvmfs publish leaves it.
    self.cvmfs = os.path.join(self.tmp, "cvmfs", "community")
    self.mods = os.path.join(self.cvmfs, ARCH, "Modules", "modulefiles")
    _write(os.path.join(self.mods, "BASE", "1.0"), "#%Module1.0\n")
    _write(os.path.join(self.mods, "top", "1-1"), TOP_MF)
    _write(os.path.join(self.mods, "dep", "2-1"), "#%Module1.0\n")
    for pkg in ("top/1-1", "dep/2-1"):
      os.makedirs(os.path.join(self.cvmfs, ARCH, "Packages", pkg, "bin"))
    self.view = os.path.join(self.cvmfs, "views", "R", ARCH)
    os.makedirs(os.path.join(self.view, "bin"))
    env = {"paths": {}, "sets": {"TOP_ROOT": "@B@/top/1-1"},
           "base": (os.path.relpath(os.path.join(self.cvmfs, ARCH, "Packages"), self.view),
                    "/cvmfs/community/%s/Packages" % ARCH)}
    cp.write_view_setup(self.view, "/cvmfs/community/views/R/" + ARCH, env)
    self.marker = os.path.join(self.view, cp.setup_marker(
        {"package": "top", "version": "1", "revision": "1"}))
    _write(self.marker, "#\n")
    self.pointer = os.path.join(self.mods, "top", ".1-1.view")
    os.symlink(os.path.relpath(self.marker, os.path.dirname(self.pointer)), self.pointer)
    self.env = {"HOME": self.tmp, "PATH": stubs + ":/usr/bin:/bin",
                "BITS_CVMFS_PREFIX": self.cvmfs, "MODULES_SHELL": "bash"}

  def _bits(self, *args, **kw):
    return subprocess.run([os.path.join(self.inst, "bits"), "-a", ARCH] + list(args),
                          cwd=self.work, env=self.env, capture_output=True, text=True, **kw)

  def _ran(self):
    try:
      with open(self.calls) as f:
        return f.read().split("\n")[:-1]
    except OSError:
      return []

  def _setenv(self, *mods):
    return self._bits("setenv", *mods, "-c", "sh", "-c",
                      'echo "$TOP_ROOT|${PATH%%:*}|${BITS_VIEW-unset}"')

  def test_setenv_sources_the_view(self):
    r = self._setenv("top/1-1")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(r.stdout.strip().split("|"), [
        os.path.join(self.cvmfs, ARCH, "Packages", "top", "1-1"),
        os.path.join(self.view, "bin"), "unset"])
    self.assertEqual(self._ran(), [])

  def test_printenv_prints_sourcing_the_view(self):
    # load / printenv collapse onto a view only with --view, as the modules path.
    r = self._bits("--view", "printenv", "top/1-1")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(r.stdout, '. "%s/setup.sh";\nunset BITS_VIEW;\n' % self.view)
    self.env["MODULES_SHELL"] = "tcsh"
    r = self._bits("--view", "printenv", "top/1-1")
    self.assertEqual(r.stdout, 'source "%s/setup.csh";\nunsetenv BITS_VIEW;\n' % self.view)
    self.assertEqual(self._ran(), [])
    self._bits("printenv", "top/1-1")
    self.assertTrue(self._ran())

  def test_enter_sources_the_view(self):
    r = self._bits("enter", "top/1-1", input='echo "ROOT=$TOP_ROOT"\nexit\n')
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertIn("ROOT=" + os.path.join(self.cvmfs, ARCH, "Packages", "top", "1-1"),
                  r.stdout)
    self.assertIn("Set up from the release view %s" % self.view, r.stderr)
    self.assertEqual(self._ran(), [])

  def test_the_modules_otherwise(self):
    cases = []
    # Two modules, a bare name: modulecmd resolves them.
    cases.append(lambda: self._setenv("top/1-1,dep/2-1"))
    cases.append(lambda: self._setenv("top"))
    # A package without view: true, unless --view.
    def plain():
      _write(os.path.join(self.mods, "top", "1-1"), TOP_MF.replace("setenv BITS_VIEW 1\n", ""))
      return self._setenv("top/1-1")
    cases.append(plain)
    # The view is not complete (no .setup-complete), or not pointed to.
    def incomplete():
      os.unlink(self.marker)
      return self._setenv("top/1-1")
    cases.append(incomplete)
    # The view was made again for another revision: this one's link leads nowhere.
    def remade():
      os.rename(self.marker, self.marker.replace(".1-1", ".1-2"))
      return self._setenv("top/1-1")
    cases.append(remade)
    def no_csh():
      os.unlink(os.path.join(self.view, "setup.csh"))
      return self._setenv("top/1-1")
    cases.append(no_csh)
    def no_pointer():
      os.unlink(self.pointer)
      return self._setenv("top/1-1")
    cases.append(no_pointer)
    # A local build of it wins over CVMFS.
    def local():
      _write(os.path.join(self.work, "sw", ARCH, "top", "1-1", "etc", "modulefiles", "top"),
             TOP_MF)
      return self._setenv("top/1-1")
    cases.append(local)
    for i, case in enumerate(cases):
      self.setUp()
      case()
      self.assertTrue(any(c.startswith("modulecmd ") for c in self._ran()), i)

  def test_view_option_uses_it_for_a_plain_package(self):
    _write(os.path.join(self.mods, "top", "1-1"), TOP_MF.replace("setenv BITS_VIEW 1\n", ""))
    r = self._bits("--view", "setenv", "top/1-1", "-c", "sh", "-c", 'echo "$TOP_ROOT"')
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(r.stdout.strip(), os.path.join(self.cvmfs, ARCH, "Packages", "top", "1-1"))
    self.assertEqual(self._ran(), [])



def _modulecmd():
  m = os.environ.get("BITS_TEST_MODULECMD") or shutil.which("modulecmd")
  return m if m and os.access(m, os.X_OK) else None


@unittest.skipUnless(_modulecmd(), "needs modulecmd (Environment Modules)")
class SameAsTheModulesTest(unittest.TestCase):
  """What setup.sh sets is what loading the modules sets, but for the entries
  the view collapses (those are the view's)."""

  def test_setup_sets_what_modulecmd_sets(self):
    import sys
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from test_modulefile_env import ROOT as ROOT_MF, ZLIB as ZLIB_MF, MEMBERS
    from bits_helpers import modulefile_env as mfe
    tmp = os.path.realpath(tempfile.mkdtemp()); self.addCleanup(shutil.rmtree, tmp, True)
    pkgs = os.path.join(tmp, ARCH, "Packages")
    mods = os.path.join(tmp, ARCH, "Modules", "modulefiles")
    _write(os.path.join(mods, "BASE", "1.0"), "#%%Module1.0\nsetenv BASEDIR %s\n" % pkgs)
    _write(os.path.join(mods, "ROOT", "6.36-2"), ROOT_MF)
    _write(os.path.join(mods, "zlib", "1.3-1"), ZLIB_MF)
    dirs = {("ROOT", "bin"), ("ROOT", "lib"), ("ROOT", "include"), ("ROOT", "etc/plugins"),
            ("zlib", "lib")}
    for pkg, rel in dirs:
      os.makedirs(os.path.join(pkgs, pkg, MEMBERS[pkg], rel))
    texts = {"ROOT": ROOT_MF, "zlib": ZLIB_MF}
    ops, problems = mfe.release_ops(["ROOT"], MEMBERS, texts.get, lambda p, r: (p, r) in dirs)
    linked = ("bin", "lib", "include")
    paths, sets, more = mfe.setup_env(ops, MEMBERS, lambda p, r: r.split("/")[0] in linked)
    self.assertEqual(problems + more, [])
    view = os.path.join(tmp, "views", "R", ARCH)
    for d in linked:
      os.makedirs(os.path.join(view, d))
    cp.write_view_setup(view, "/cvmfs/x", {"paths": paths, "sets": sets,
                                           "base": (os.path.relpath(pkgs, view), pkgs)})
    show = 'env | LC_ALL=C sort | grep -vE "^(_|SHLVL|PWD|OLDPWD)="'
    clean = {"PATH": "/usr/bin:/bin", "HOME": tmp}
    by_modules = subprocess.run(
        ["bash", "-c", 'eval "$(%s bash load ROOT/6.36-2)"; %s' % (_modulecmd(), show)],
        env=dict(clean, MODULEPATH=mods), capture_output=True, text=True).stdout
    by_view = subprocess.run(["bash", "-c", '. "%s/setup.sh"; %s' % (view, show)],
                             env=dict(clean, MODULEPATH=mods), capture_output=True,
                             text=True).stdout

    def parse(out):
      return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    mod, vw = parse(by_modules), parse(by_view)
    collapse = set(mfe.COLLAPSE_VARS)
    ignore = {"BITS_VIEW", "BASEDIR", "LOADEDMODULES", "_LMFILES_", "MODULEPATH",
              "MODULES_LMALTNAME", "MODULES_LMCONFLICT", "MODULES_LMPREREQ"}
    for var in set(mod) | set(vw):
      if var in ignore or var.startswith("__MODULES") or var.endswith("_modshare"):
        continue
      if var in collapse:
        # Each package entry the modules add is the view's entry for it.
        for entry in mod.get(var, "").split(":"):
          if entry.startswith(pkgs + "/"):
            pkg, ver, *rest = os.path.relpath(entry, pkgs).split("/")
            want = os.path.join(view, *rest) if not rest or rest[0] in linked else entry
            self.assertIn(want.rstrip("/"), vw[var].split(":"), (var, entry))
      else:
        self.assertEqual(vw.get(var), mod.get(var), var)

if __name__ == "__main__":
  unittest.main()
