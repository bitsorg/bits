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
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCH = "x86_64-el9-gcc14-opt"


def _write(path, text, mode=0o644):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    f.write(text)
  os.chmod(path, mode)


class RuntimeWithoutPythonTest(unittest.TestCase):
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


  def _host(self):
    import platform
    import sys
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from test_cvmfs_modules import _host_os
    if not _host_os():
      self.skipTest("bits picks the host's arch on el<N> and Ubuntu hosts only")
    return "%s-%s-gcc14" % (platform.machine(), _host_os())

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

if __name__ == "__main__":
  unittest.main()
