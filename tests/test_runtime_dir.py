# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""The bits script and bits' own runtime/ (bits.bits installs, e.g. on CVMFS).

Without runtime/ beside the scripts, python3 and modulecmd come from PATH as
always; with it, bits runs runtime/bin/python3 and runtime/bin/modulecmd by path
and never the ones on PATH, ignoring the user's PYTHON* variables.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _shim(path, log, real=None):
  """An executable that logs how it was called, then runs *real* (or exits 0)."""
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    f.write('#!/bin/sh\necho "$0 $*" >> "%s"\n' % log)
    f.write('exec "%s" "$@"\n' % real if real else "exit 0\n")
  os.chmod(path, 0o755)


class RuntimeDirTest(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.mkdtemp()
    self.inst = os.path.join(self.tmp, "inst")
    os.makedirs(self.inst)
    for f in ("bits", "bitsBuild", "bitsStore"):
      shutil.copy2(os.path.join(ROOT, f), self.inst)
    shutil.copytree(os.path.join(ROOT, "bits_helpers"), os.path.join(self.inst, "bits_helpers"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    self.work = os.path.join(self.tmp, "work")
    os.makedirs(os.path.join(self.work, "sw", "MODULES", "slc7_x86-64"))
    self.path_log = os.path.join(self.tmp, "path.log")
    self.runtime_log = os.path.join(self.tmp, "runtime.log")
    fakebin = os.path.join(self.tmp, "fakebin")
    _shim(os.path.join(fakebin, "python3"), self.path_log, sys.executable)
    _shim(os.path.join(fakebin, "modulecmd"), self.path_log)
    self.env = dict(os.environ, HOME=self.tmp, PATH=fakebin + ":/usr/bin:/bin")
    self.env.pop("BITS_WORK_DIR", None)

  def tearDown(self):
    shutil.rmtree(self.tmp, ignore_errors=True)

  def _add_runtime(self):
    rt = os.path.join(self.inst, "runtime", "bin")
    _shim(os.path.join(rt, "python3"), self.runtime_log, sys.executable)
    _shim(os.path.join(rt, "modulecmd"), self.runtime_log)

  def _bits(self, *args):
    return subprocess.run([os.path.join(self.inst, "bits")] + list(args), cwd=self.work,
                          env=self.env, capture_output=True, text=True, timeout=120)

  def _log(self, path):
    if not os.path.exists(path):
      return ""
    with open(path) as f:
      return f.read()

  def test_without_runtime_python_and_modulecmd_come_from_path(self):
    self._bits("use")
    self._bits("modulecmd", "--version")
    self._bits("store", "--help")
    log = self._log(self.path_log)
    self.assertIn("fakebin/python3 -c", log)
    self.assertIn("fakebin/modulecmd --version", log)
    self.assertIn("fakebin/python3 - --help", log)

  def test_with_runtime_bits_uses_it_and_not_path(self):
    self._add_runtime()
    self.assertEqual(self._bits("use").returncode, 0)
    r = self._bits("version")
    self.assertEqual(r.returncode, 0, r.stderr)
    self._bits("modulecmd", "--version")
    self._bits("store", "--help")
    log = self._log(self.runtime_log)
    self.assertIn("runtime/bin/python3 -E -s -c", log)
    self.assertIn("runtime/bin/python3 -E -s %s/bitsBuild version" % self.inst, log)
    self.assertIn("runtime/bin/modulecmd --version", log)
    self.assertIn("runtime/bin/python3 -E -s - --help", log)
    self.assertEqual(self._log(self.path_log), "")

  def test_with_runtime_the_users_pythonpath_is_ignored(self):
    # Inside an entered environment PYTHONPATH belongs to the stack's Python.
    poison = os.path.join(self.tmp, "poison")
    os.makedirs(poison)
    with open(os.path.join(poison, "yaml.py"), "w") as f:
      f.write("raise ImportError('user PYTHONPATH reached bits')\n")
    self._add_runtime()
    self.env["PYTHONPATH"] = poison
    r = self._bits("version")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(self._bits("store", "--help").returncode, 0)


if __name__ == "__main__":
  unittest.main()
