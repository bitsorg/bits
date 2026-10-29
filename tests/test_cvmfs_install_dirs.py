# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits q / enter / setenv on CVMFS trees laid out by install dir, as ALICE's
(/cvmfs/alice.cern.ch/el9-x86_64/Modules/modulefiles): a cvmfs.yaml whose
cvmfs_modules_template has {install_dir} in place of {arch}. The install dir
is the one published with the arch's (or the host's) CPU and OS; retired
modulefiles in Modules/archive are searched only with --archive.
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tests"))

from test_cvmfs_modules import _host_os            # noqa: E402
from test_release_view import _modulecmd           # noqa: E402

# As on /cvmfs/alice.cern.ch, with old names that carry no OS.
DIRS = ("el8-x86_64", "el9-x86_64", "el10-x86_64", "el9-aarch64", "ubuntu2604-riscv64",
        "x86_64-2.6-gnu-4.8.3", "noarch")

MODULE = """#%%Module1.0
set version %s
module load BASE/1.0%s
setenv %s_ROOT $::env(BASEDIR)/%s/$version
"""


def _write(path, text, mode=0o644):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    f.write(text)
  os.chmod(path, mode)


class InstallDirTreesTest(unittest.TestCase):
  def setUp(self):
    self.tmp = os.path.realpath(tempfile.mkdtemp())
    self.addCleanup(shutil.rmtree, self.tmp, True)
    self.inst = os.path.join(self.tmp, "inst")
    os.makedirs(self.inst)
    for f in ("bits", "bitsBuild"):
      shutil.copy2(os.path.join(ROOT, f), self.inst)
    self.calls = os.path.join(self.tmp, "calls")
    self.stubs = os.path.join(self.tmp, "stubs")
    _write(os.path.join(self.stubs, "python3"),
           '#!/bin/sh\necho "python3 $*" >> "%s"\nexit 1\n' % self.calls, 0o755)
    _write(os.path.join(self.stubs, "modulecmd"),
           '#!/bin/sh\necho "modulecmd MODULEPATH=$MODULEPATH BASEDIR=$BASEDIR $*" >> "%s"\n'
           % self.calls, 0o755)
    self.cvmfs = os.path.join(self.tmp, "cvmfs", "alice")
    for d in DIRS:
      self._module(d, "P/%s-1" % d.replace("-", "_"))
    # Retired, and needing a module that is not.
    self._module("el9-x86_64", "Q/1-1")
    self._module("el9-x86_64", "P/v0-1", archive=True, load="Q/1-1")
    self.job = os.path.join(self.tmp, "job")
    _write(os.path.join(self.job, "cvmfs.yaml"),
           'prefix: "%s"\ncvmfs_modules_template: "{prefix}/{install_dir}/Modules/modulefiles/{pkg}"\n'
           % self.cvmfs)
    self.env = {"HOME": self.tmp, "PATH": self.stubs + ":/usr/bin:/bin", "MODULES_SHELL": "bash"}

  def _module(self, d, name, archive=False, load=None):
    mods = os.path.join(self.cvmfs, d, "Modules")
    _write(os.path.join(mods, "modulefiles", "BASE", "1.0"),
           "#%%Module1.0\nsetenv BASEDIR %s\n" % os.path.join(self.cvmfs, d, "Packages"))
    _write(os.path.join(mods, "archive" if archive else "modulefiles", name),
           MODULE % (name.split("/")[1], "\nmodule load %s" % load if load else "",
                     name.split("/")[0], name.split("/")[0]))

  def _bits(self, *args):
    return subprocess.run([os.path.join(self.inst, "bits")] + list(args), cwd=self.job,
                          env=self.env, capture_output=True, text=True)

  def _calls(self):
    try:
      with open(self.calls) as f:
        return f.read().splitlines()
    except OSError:
      return []

  def _q(self, *args):
    r = self._bits(*args, "q")
    self.assertEqual(r.returncode, 0, r.stderr)
    return r.stdout.split()

  def test_the_install_dir_of_the_arch(self):
    el9 = ["P/el9_x86_64-1", "Q/1-1"]
    self.assertEqual(sorted(self._q("-a", "slc9_x86-64")), el9)
    self.assertEqual(sorted(self._q("-a", "slc9-gpu_x86-64")), el9)
    self.assertEqual(sorted(self._q("-a", "x86_64-el9-gcc14-opt")), el9)
    self.assertEqual(self._q("-a", "slc8_x86-64"), ["P/el8_x86_64-1"])
    self.assertEqual(self._q("-a", "slc10_x86-64"), ["P/el10_x86_64-1"])
    self.assertEqual(self._q("-a", "slc9_aarch64"), ["P/el9_aarch64-1"])
    self.assertEqual(self._q("-a", "ubuntu2604_riscv64"), ["P/ubuntu2604_riscv64-1"])
    # -a may name the install dir itself, also one without an OS.
    self.assertEqual(self._q("-a", "el8-x86_64"), ["P/el8_x86_64-1"])
    self.assertEqual(self._q("-a", "x86_64-2.6-gnu-4.8.3"), ["P/x86_64_2.6_gnu_4.8.3-1"])
    self.assertEqual(self._calls(), [])

  def test_an_installed_arch_gives_its_install_dir(self):
    _write(os.path.join(self.job, "sw", "slc9_x86-64", "A", "1-1", "etc", "modulefiles", "A"),
           "#%Module1.0\n")
    self.assertEqual(sorted(self._q()), ["A/1-1", "P/el9_x86_64-1", "Q/1-1"])
    self.assertEqual(self._calls(), [])

  def test_several_for_the_cpu_and_os(self):
    self._module("el9-x86_64-cuda", "P/cuda-1")
    r = self._bits("-a", "slc9_x86-64", "q")
    self.assertEqual((r.returncode, r.stdout), (0, ""))
    self.assertIn("Choose one: bits -a <install_dir> q", r.stderr)
    self.assertIn("/{install_dir}/Modules/modulefiles", r.stderr)
    self.assertEqual(self._q("-a", "el9-x86_64-cuda"), ["P/cuda-1"])

  def test_a_template_without_pkg_gives_no_trees(self):
    _write(os.path.join(self.job, "cvmfs.yaml"),
           'prefix: "%s"\ncvmfs_modules_template: "{prefix}/{install_dir}/Modules/modulefiles"\n'
           % self.cvmfs)
    r = self._bits("-a", "slc9_x86-64", "q")
    self.assertNotEqual(r.returncode, 0)
    self.assertIn("no work dir", r.stdout + r.stderr)

  def test_no_install_dir_for_the_arch(self):
    for arch in ("slc1_x86-64", "slc7_x86-64", "ubuntu2404_x86-64", "x86_64-gnu"):
      r = self._bits("-a", arch, "q")
      self.assertEqual((r.returncode, r.stdout), (0, ""), arch)
      self.assertIn("NOTE: no CVMFS module tree", r.stderr, arch)

  def test_the_hosts_install_dir(self):
    os_ = _host_os()
    if not os_:
      self.skipTest("bits picks the host's on el<N> and Ubuntu hosts only")
    self._module("%s-%s" % (os_, platform.machine()), "P/host-1")
    self.assertEqual(self._q(), ["P/host-1"])
    self.assertEqual(self._calls(), [])

  def test_the_archive_only_with_archive(self):
    for opt in ("--archive", "-archive"):
      self.assertEqual(sorted(self._q("-a", "slc9_x86-64", opt)), ["P/el9_x86_64-1", "P/v0-1", "Q/1-1"])
    r = self._bits("-a", "slc9_x86-64", "printenv", "P/v0-1")
    self.assertEqual(r.returncode, 1)
    self.assertIn("P/v0-1 was not found", r.stderr)

  def test_the_archives_of_arch_trees(self):
    # BITS_CVMFS_PREFIX trees: the archive of the second only.
    prefix = os.path.join(self.tmp, "cvmfs", "bits")
    for arch, name, archive in (("A-opt", "X/1-1", False), ("A", "T/1-1", False),
                                ("A", "T/0-1", True)):
      _write(os.path.join(prefix, arch, "Modules", "archive" if archive else "modulefiles", name),
             "#%Module1.0\n")
    self.env["BITS_CVMFS_PREFIX"] = prefix
    self.assertEqual(sorted(self._q("-a", "A-opt")), ["T/1-1", "X/1-1"])
    self.assertEqual(sorted(self._q("-a", "A-opt", "--archive")), ["T/0-1", "T/1-1", "X/1-1"])
    self._bits("-a", "A-opt", "--archive", "printenv", "T/0-1")
    t = os.path.join(prefix, "A", "Modules")
    self.assertIn("MODULEPATH=%s/modulefiles:%s/archive " % (t, t), self._calls()[0])

  def test_an_archived_module_uses_its_trees_packages(self):
    tree = os.path.join(self.cvmfs, "el9-x86_64", "Modules")
    want = "MODULEPATH=%s/modulefiles:%s/archive BASEDIR=%s" % (
        tree, tree, os.path.join(self.cvmfs, "el9-x86_64", "Packages"))
    self._bits("-a", "slc9_x86-64", "--archive", "printenv", "P/v0-1")
    self.assertTrue(self._calls()[0].startswith("modulecmd " + want), self._calls())
    # unload finds it where it was loaded from, without --archive.
    os.unlink(self.calls)
    self.env.update(LOADEDMODULES="BASE/1.0:P/v0-1",
                    _LMFILES_="%s/modulefiles/BASE/1.0:%s/archive/P/v0-1" % (tree, tree))
    self._bits("-a", "slc9_x86-64", "unload", "P/v0-1")
    self.assertTrue(self._calls()[0].startswith("modulecmd " + want), self._calls())

  @unittest.skipUnless(_modulecmd(), "needs modulecmd (Environment Modules)")
  def test_setenv_with_modulecmd(self):
    os.unlink(os.path.join(self.stubs, "modulecmd"))
    os.symlink(_modulecmd(), os.path.join(self.stubs, "modulecmd"))
    pkgs = os.path.join(self.cvmfs, "el9-x86_64", "Packages")
    for args, want in ((("P/el9_x86_64-1",), ["P/el9_x86_64-1", ""]),
                       (("--archive", "P/v0-1"), ["P/v0-1", "Q/1-1"])):
      r = self._bits("-a", "slc9_x86-64", "setenv", *args, "-c", "sh", "-c",
                     'echo "$P_ROOT|${Q_ROOT-}"')
      self.assertEqual(r.returncode, 0, (args, r.stderr))
      self.assertEqual(r.stdout.strip().split("|"),
                       [os.path.join(pkgs, w) if w else "" for w in want], args)
    self.assertEqual(self._calls(), [])


if __name__ == "__main__":
  unittest.main()
