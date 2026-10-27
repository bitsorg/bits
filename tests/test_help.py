# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits -h lists every command bits runs, and each gives its own help."""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Run by the launcher itself, not by bitsBuild.
LAUNCHER = {"store", "use", "cvmfs", "preload", "overlay", "trace", "q", "query", "enter",
            "setenv", "load", "printenv", "unload", "list", "avail", "modulecmd",
            "shell-helper"}
MODULE_COMMANDS = LAUNCHER - {"store", "use", "cvmfs", "preload", "overlay", "trace"}
DEPRECATED = {"cleanup", "lcg-view", "cvmfs-stage", "cvmfs-publish", "store-stats"}


class HelpTest(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    cls.tmp = tempfile.mkdtemp()
    stubs = os.path.join(cls.tmp, "stubs")
    os.makedirs(stubs)
    # A wrapper, not a link: run by a link's path, a venv's Python does not find its venv.
    with open(os.path.join(stubs, "python3"), "w") as f:
      f.write('#!/bin/sh\nexec %s "$@"\n' % shlex.quote(sys.executable))
    os.chmod(os.path.join(stubs, "python3"), 0o755)
    cls.env = {"HOME": cls.tmp, "PATH": stubs + ":/usr/bin:/bin"}

  @classmethod
  def tearDownClass(cls):
    shutil.rmtree(cls.tmp, True)

  def _bits(self, *args):
    return subprocess.run([os.path.join(ROOT, "bits")] + list(args), cwd=self.tmp,
                          env=self.env, capture_output=True, text=True, timeout=120)

  def _listed(self):
    out = self._bits("-h").stdout
    names = set()
    for line in out.splitlines():
      m = re.match(r"^  ([a-z][a-z-]*(?:, [a-z][a-z-]*)*)(?: |$)", line)
      if m:
        names.update(m.group(1).split(", "))
    return out, names

  def test_every_command_is_listed(self):
    choices = subprocess.run([sys.executable, os.path.join(ROOT, "bitsBuild"), "--help"],
                             env=self.env, capture_output=True, text=True).stdout
    python = set(re.search(r"\{([a-z,-]+)\}", choices).group(1).split(","))
    out, listed = self._listed()
    self.assertEqual(listed, (python | LAUNCHER) - DEPRECATED, out)

  def test_each_command_has_its_own_help(self):
    top, listed = self._listed()
    for name in sorted(listed):
      r = self._bits(name, "--help")
      self.assertEqual(r.returncode, 0, (name, r.stderr))
      self.assertNotEqual(r.stdout, top, name)
      if name in MODULE_COMMANDS:
        self.assertTrue(r.stdout.startswith("bits: the module commands"), name)
      else:
        self.assertRegex(r.stdout, r"(?i)usage: bits", name)

  def test_help_command(self):
    self.assertEqual(self._bits("help", "deps").stdout, self._bits("deps", "--help").stdout)
    self.assertEqual(self._bits("help").stdout, self._bits("--help").stdout)
    self.assertEqual(self._bits("--community", "atlas", "-h").stdout, self._bits("-h").stdout)
    self.assertEqual(self._bits("help", "cvmfs", "stage").stdout,
                     self._bits("cvmfs", "stage", "--help").stdout)
    for args in (["help", "nosuch"], ["nosuch", "--help"]):
      r = self._bits(*args)
      self.assertEqual((r.returncode, r.stdout), (1, ""), args)

  def test_shell_helper_prints_help(self):
    # The function evals what load/unload print: their help must not be.
    r = subprocess.run(["bash", "-c", 'eval "$("$BITS_ENV" shell-helper)"; bits load --help'],
                       cwd=self.tmp, env=dict(self.env, BITS_ENV=os.path.join(ROOT, "bits")),
                       capture_output=True, text=True)
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertTrue(r.stdout.startswith("bits: the module commands"), r.stdout)

  def test_mistakes_are_short(self):
    for args in (["nosuch"], ["-a", "x86_64-el9"]):
      r = self._bits(*args)
      self.assertEqual((r.returncode, r.stdout), (1, ""), args)
      self.assertIn("bits help", r.stderr)
      self.assertLess(len(r.stderr.splitlines()), 4, r.stderr)
    r = self._bits()
    self.assertEqual(r.returncode, 1)
    self.assertIn("Usage: bits", r.stderr)

  def test_options_before_a_build_command(self):
    r = self._bits("-a", "x86_64-el9", "build", "zlib")
    self.assertEqual(r.returncode, 1)
    self.assertIn("build takes its options after it", r.stderr)

  def test_debug_before_a_command(self):
    r = self._bits("--debug", "version")
    self.assertEqual(r.returncode, 0, r.stderr)

  def test_fits_80_columns(self):
    for args in (["-h"], ["enter", "--help"]):
      wide = [l for l in self._bits(*args).stdout.splitlines() if len(l) > 80]
      self.assertEqual(wide, [], args)


if __name__ == "__main__":
  unittest.main()
