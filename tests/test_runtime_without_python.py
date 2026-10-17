# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""The environment commands (q, enter, load, printenv, setenv) start no Python
unless they need it, e.g. for a grid job using a community's CVMFS modules.

python3 is a stub that records being run (and fails).
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCH = "x86_64-el9-gcc14-opt"


def _write(path, text, mode=0o644):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    f.write(text)
  os.chmod(path, mode)


class _Bits(unittest.TestCase):
  def setUp(self):
    self.tmp = os.path.realpath(tempfile.mkdtemp())
    self.addCleanup(shutil.rmtree, self.tmp, True)
    self.inst = os.path.join(self.tmp, "inst")
    os.makedirs(self.inst)
    for f in ("bits", "bitsBuild"):
      shutil.copy2(os.path.join(ROOT, f), self.inst)
    self.calls = os.path.join(self.tmp, "calls")
    stubs = os.path.join(self.tmp, "stubs")
    _write(os.path.join(stubs, "python3"),
           '#!/bin/sh\necho "python3 $*" >> "%s"\nexit 1\n' % self.calls, 0o755)
    _write(os.path.join(stubs, "modulecmd"), "#!/bin/sh\nexit 0\n", 0o755)
    self.work = os.path.join(self.tmp, "work")
    _write(os.path.join(self.work, "sw", ARCH, "A", "1-1", "etc", "modulefiles", "A"),
           "#%Module1.0\n")
    self.env = {"HOME": self.tmp, "PATH": stubs + ":/usr/bin:/bin", "MODULES_SHELL": "bash"}

  def _bits(self, *args, cwd=None):
    return subprocess.run([os.path.join(self.inst, "bits")] + list(args),
                          cwd=cwd or self.work, env=self.env, capture_output=True, text=True)

  def _python_ran(self):
    return os.path.exists(self.calls)

  def _host(self):
    import platform
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from test_cvmfs_modules import _host_os
    if not _host_os():
      self.skipTest("bits picks the host's arch on el<N> and Ubuntu hosts only")
    return "%s-%s-gcc14" % (platform.machine(), _host_os())

  def _real_modulecmd(self):
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from test_release_view import _modulecmd
    m = _modulecmd()
    if not m:
      self.skipTest("needs modulecmd (Environment Modules)")
    os.unlink(os.path.join(self.tmp, "stubs", "modulecmd"))
    os.symlink(m, os.path.join(self.tmp, "stubs", "modulecmd"))


class RuntimeWithoutPythonTest(_Bits):
  def test_a_profile_of_another_directory_starts_no_python(self):
    _write(os.path.join(self.tmp, ".bits", "use", "0123456789abcdef.use"), "[common]\n")
    r = self._bits("-a", ARCH, "q")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertIn("A/1-1", r.stdout)
    self.assertFalse(self._python_ran())

  def test_this_directorys_profile_is_read(self):
    key = hashlib.sha256(self.work.encode()).hexdigest()[:16]
    _write(os.path.join(self.tmp, ".bits", "use", key + ".use"), "[common]\n")
    self._bits("-a", ARCH, "q")
    self.assertTrue(self._python_ran())
    os.unlink(self.calls)
    _write(os.path.join(self.work, ".bitsuse"), "[common]\n")
    self._bits("-a", ARCH, "q")
    self.assertTrue(self._python_ran())

  def test_the_arch_published_for_this_host_without_python(self):
    host = self._host()
    shutil.rmtree(os.path.join(self.work, "sw", ARCH))   # nothing installed
    cvmfs = os.path.join(self.tmp, "cvmfs", "community")
    for arch in (host + "-opt", "x86_64-el1-gcc9-opt"):
      _write(os.path.join(cvmfs, arch, "Modules", "modulefiles", "K", "1-1"), "#%Module1.0\n")
    self.env["BITS_CVMFS_PREFIX"] = cvmfs
    r = self._bits("q")
    self.assertEqual((r.returncode, r.stdout.split()), (0, ["K/1-1"]), r.stderr)
    self.assertFalse(self._python_ran())

  def test_of_several_installed_the_hosts_without_python(self):
    host = self._host() + "-opt"
    for arch, pkg in ((host, "H"), ("x86_64-el1-gcc9-opt", "G")):
      _write(os.path.join(self.work, "sw", arch, pkg, "1-1", "etc", "modulefiles", pkg),
             "#%Module1.0\n")
      os.makedirs(os.path.join(self.work, "sw", "MODULES", arch))
    shutil.rmtree(os.path.join(self.work, "sw", ARCH))
    r = self._bits("q")
    self.assertEqual((r.returncode, r.stdout.split()), (0, ["H/1-1"]), r.stderr)
    self.assertFalse(self._python_ran())


class WithoutWorkDirTest(_Bits):
  """A grid job: a community's CVMFS modules, no sw directory."""

  def setUp(self):
    super().setUp()
    self.job = os.path.join(self.tmp, "job")
    os.makedirs(self.job)
    self.cvmfs = os.path.join(self.tmp, "cvmfs", "community")
    self._publish(ARCH)
    self.env["BITS_CVMFS_PREFIX"] = self.cvmfs

  def _publish(self, arch):
    mods = os.path.join(self.cvmfs, arch, "Modules", "modulefiles")
    _write(os.path.join(mods, "BASE", "1.0"), "#%Module1.0\n")
    _write(os.path.join(mods, "K", "1-1"),
           "#%Module1.0\nmodule load BASE/1.0\nsetenv K_ROOT $::env(BASEDIR)/K/1-1\n")

  def test_query_needs_no_work_dir(self):
    r = self._bits("-a", ARCH, "q", cwd=self.job)
    self.assertEqual((r.returncode, r.stdout.split()), (0, ["K/1-1"]), r.stderr)
    self.assertFalse(self._python_ran())

  def test_the_trees_of_a_cvmfs_yaml(self):
    del self.env["BITS_CVMFS_PREFIX"]
    _write(os.path.join(self.job, "cvmfs.yaml"), "prefix: %s\n" % self.cvmfs)
    r = self._bits("-a", ARCH, "q", cwd=self.job)
    self.assertEqual((r.returncode, r.stdout.split()), (0, ["K/1-1"]), r.stderr)
    self.assertFalse(self._python_ran())

  def test_the_hosts_arch(self):
    host = self._host()
    shutil.rmtree(os.path.join(self.cvmfs, ARCH))
    self._publish(host + "-opt")
    self._publish("x86_64-el1-gcc9-opt")
    r = self._bits("q", cwd=self.job)
    self.assertEqual((r.returncode, r.stdout.split()), (0, ["K/1-1"]), r.stderr)
    self.assertFalse(self._python_ran())
    # Two compilers for this host: bits cannot choose.
    self._publish(host.replace("gcc14", "gcc13") + "-opt")
    r = self._bits("q", cwd=self.job)
    self.assertEqual(r.returncode, 1)
    self.assertIn("Choose one: bits -a <arch> q", r.stderr)
    self.assertIn(host + "-opt", r.stderr)
    self.assertFalse(self._python_ran())

  def test_other_commands_still_need_one(self):
    def no_work_dir(*args):
      r = self._bits(*args, cwd=self.job)
      self.assertNotEqual(r.returncode, 0, args)
      self.assertIn("No default work dir", r.stdout + r.stderr, args)
    no_work_dir("-a", ARCH, "build", "K")
    self.env["BITS_CVMFS_PREFIX"] = ""     # off
    no_work_dir("-a", ARCH, "q")
    del self.env["BITS_CVMFS_PREFIX"]
    no_work_dir("-a", ARCH, "q")
    # A cvmfs.yaml that gives no module trees (e.g. alice.bits').
    _write(os.path.join(self.job, "cvmfs.yaml"),
           "prefix: %s\ncvmfs_modules_template: \"{install_dir}/etc/modulefiles/{pkg}\"\n" % self.cvmfs)
    no_work_dir("-a", ARCH, "q")
    os.unlink(os.path.join(self.job, "cvmfs.yaml"))
    self.env["BITS_CVMFS_PREFIX"] = self.cvmfs
    r = self._bits("-a", ARCH, "--dev", "load", "K/1-1", cwd=self.job)
    self.assertEqual(r.returncode, 1)
    self.assertIn("--dev needs a work dir", r.stderr)

  def test_printenv_with_modulecmd(self):
    self._real_modulecmd()
    for action in ("printenv", "load"):
      r = self._bits("-a", ARCH, action, "K/1-1", cwd=self.job)
      self.assertEqual(r.returncode, 0, r.stderr)
      self.assertIn(os.path.join(self.cvmfs, ARCH, "Packages", "K", "1-1"), r.stdout)
    self.assertFalse(self._python_ran())

  def test_setenv_with_modulecmd(self):
    self._real_modulecmd()
    r = self._bits("-a", ARCH, "setenv", "K/1-1", "-c", "sh", "-c", 'echo "$K_ROOT"',
                   cwd=self.job)
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(r.stdout.strip(), os.path.join(self.cvmfs, ARCH, "Packages", "K", "1-1"))
    self.assertFalse(self._python_ran())


if __name__ == "__main__":
  unittest.main()
