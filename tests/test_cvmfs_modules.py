# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits q/printenv/setenv with a community's CVMFS module trees ($BITS_CVMFS_PREFIX).

The trees of the arch that match up to -opt/-dbg are listed and loaded after the
local ones; each module is loaded against its own tree, with that tree's BASEDIR.
Needs a real modulecmd (Environment Modules): on PATH, or $BITS_TEST_MODULECMD.
"""

import os
import platform
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCH = "x86_64-el9-gcc14-opt"


def _host_os():
  """This host's OS token as bits' hostCvmfsArch reads it (el<N>, ubuntu<NNNN>)."""
  try:
    with open("/etc/os-release") as f:
      kv = dict(l.rstrip("\n").split("=", 1) for l in f if "=" in l)
  except OSError:
    return ""
  kv = {k: v.strip('"') for k, v in kv.items()}
  m = re.match(r"^platform:(el[0-9]+)$", kv.get("PLATFORM_ID", ""))
  if m:
    return m.group(1)
  return "ubuntu" + kv.get("VERSION_ID", "").replace(".", "") if kv.get("ID") == "ubuntu" else ""


def _modulecmd():
  m = os.environ.get("BITS_TEST_MODULECMD") or shutil.which("modulecmd")
  return m if m and os.access(m, os.X_OK) else None


def _write(path, text):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    f.write(text)


def _modulefile(pkg, verrev, deps=()):
  """A bits modulefile: its package from $BASEDIR (set by BASE/1.0)."""
  var = pkg.upper()
  lines = ["#%Module1.0", "if ![ is-loaded BASE/1.0 ] { module load BASE/1.0 }"]
  lines += ["module load %s" % d for d in deps]
  lines += ["setenv %s_ROOT $::env(BASEDIR)/%s/%s" % (var, pkg, verrev),
            "prepend-path PATH $::env(BASEDIR)/%s/%s/bin" % (pkg, verrev)]
  return "\n".join(lines) + "\n"


# BASE/1.0 as `bits cvmfs publish` writes it: BASEDIR relative to itself.
_CVMFS_BASE = ("#%Module1.0\n"
               "set base_path [file normalize [file join [file dirname "
               "$ModulesCurrentModulefile] ../../../Packages]]\n"
               "setenv BASEDIR $base_path\n")


@unittest.skipUnless(_modulecmd(), "needs modulecmd (Environment Modules)")
class CvmfsModulesTest(unittest.TestCase):
  def setUp(self):
    self.tmp = os.path.realpath(tempfile.mkdtemp())   # macOS: /var -> /private/var
    self.inst = os.path.join(self.tmp, "inst")
    os.makedirs(self.inst)
    for f in ("bits", "bitsBuild", "bitsModules"):
      shutil.copy2(os.path.join(ROOT, f), self.inst)
    shutil.copytree(os.path.join(ROOT, "bits_helpers"), os.path.join(self.inst, "bits_helpers"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    self.work = os.path.join(self.tmp, "work")
    self.sw = os.path.join(self.work, "sw")
    # Local: A, built here.
    _write(os.path.join(self.sw, ARCH, "A", "1-1", "etc", "modulefiles", "A"), _modulefile("A", "1-1"))
    # CVMFS: A again and B (needs E) for -opt, gcc in the neutral tree, C for
    # -dbg, D for another compiler (never matches).
    self.cvmfs = os.path.join(self.tmp, "cvmfs", "community")
    trees = {ARCH: {"A/1-1": (), "B/2-1": ("E/5-1",), "E/5-1": ()},
             "x86_64-el9-gcc14": {"gcc/14-1": ()},
             "x86_64-el9-gcc14-dbg": {"C/3-1": ()},
             "x86_64-el9-gcc15-opt": {"D/1-1": ()}}
    for arch, mods in trees.items():
      mroot = os.path.join(self.cvmfs, arch, "Modules", "modulefiles")
      _write(os.path.join(mroot, "BASE", "1.0"), _CVMFS_BASE)
      _write(os.path.join(mroot, ".cvmfscatalog"), "")
      for mod, deps in mods.items():
        pkg, verrev = mod.split("/")
        _write(os.path.join(mroot, pkg, verrev), _modulefile(pkg, verrev, deps))
        os.makedirs(os.path.join(self.cvmfs, arch, "Packages", pkg, verrev, "bin"))
    mc = _modulecmd()
    self.env = dict(os.environ, HOME=self.tmp,
                    PATH=os.path.dirname(mc) + ":/usr/bin:/bin",
                    BITS_CVMFS_PREFIX=self.cvmfs, MODULES_SHELL="bash")
    for v in ("BITS_WORK_DIR", "MODULEPATH", "LOADEDMODULES", "_LMFILES_", "BASEDIR",
              "BITS_CATALOG_LISTING"):
      self.env.pop(v, None)

  def tearDown(self):
    shutil.rmtree(self.tmp, ignore_errors=True)

  def _bits(self, *args, **env):
    return subprocess.run([os.path.join(self.inst, "bits"), "-a", ARCH] + list(args),
                          cwd=self.work, env=dict(self.env, **env),
                          capture_output=True, text=True)

  def _printenv(self, *mods, **env):
    """The roots that `eval $(bits printenv …)` sets."""
    r = self._bits("printenv", ",".join(mods), **env)
    self.assertEqual(r.returncode, 0, r.stderr)
    show = subprocess.run(
        ["bash", "-c", r.stdout + '\nfor v in A B C E GCC; do eval "echo $v=\\${${v}_ROOT:-}"; done'],
        capture_output=True, text=True, env={"PATH": "/usr/bin:/bin"})
    return dict(l.split("=", 1) for l in show.stdout.split())

  def test_q_lists_local_then_matching_trees_once(self):
    r = self._bits("q")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(r.stdout.split(), ["A/1-1", "B/2-1", "C/3-1", "E/5-1", "gcc/14-1"])

  def test_options_after_the_command(self):
    # bits q -a ARCH: -a after the command, and an argument after it.
    r = subprocess.run([os.path.join(self.inst, "bits"), "q", "-a", ARCH, "^[BE]/"],
                       cwd=self.work, env=self.env, capture_output=True, text=True)
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(r.stdout.split(), ["B/2-1", "E/5-1"])

  def test_catalog_listing_only_on_request(self):
    # bitsModules (the catalog listing) runs only with BITS_CATALOG_LISTING=1.
    mark = os.path.join(self.tmp, "bitsModules-ran")
    with open(os.path.join(self.inst, "bitsModules"), "w") as f:
      f.write("#!/bin/sh\ntouch %s\nexit 3\n" % mark)
    os.chmod(os.path.join(self.inst, "bitsModules"), 0o755)
    self.assertEqual(self._bits("q").stdout.split(), ["A/1-1", "B/2-1", "C/3-1", "E/5-1", "gcc/14-1"])
    self.assertFalse(os.path.exists(mark))
    self.assertEqual(self._bits("q", BITS_CATALOG_LISTING="1",
                                PATH=self.inst + ":" + self.env["PATH"]).stdout.split(),
                     ["A/1-1", "B/2-1", "C/3-1", "E/5-1", "gcc/14-1"])
    self.assertTrue(os.path.exists(mark))

  def test_q_without_prefix_is_local_only(self):
    r = self._bits("q", BITS_CVMFS_PREFIX="")
    self.assertEqual(r.stdout.split(), ["A/1-1"])

  def test_each_module_from_its_own_tree(self):
    roots = self._printenv("A/1-1", "B/2-1", "gcc/14-1", "C/3-1")
    self.assertEqual(roots["A"], os.path.join(self.sw, ARCH, "A", "1-1"))   # local wins
    opt = os.path.join(self.cvmfs, ARCH, "Packages")
    self.assertEqual(roots["B"], os.path.join(opt, "B", "2-1"))
    self.assertEqual(roots["E"], os.path.join(opt, "E", "5-1"))             # B's dep, same tree
    self.assertEqual(roots["GCC"],
                     os.path.join(self.cvmfs, "x86_64-el9-gcc14", "Packages", "gcc", "14-1"))
    self.assertEqual(roots["C"],
                     os.path.join(self.cvmfs, "x86_64-el9-gcc14-dbg", "Packages", "C", "3-1"))

  def test_cvmfs_only(self):
    roots = self._printenv("B")
    self.assertEqual(roots["B"], os.path.join(self.cvmfs, ARCH, "Packages", "B", "2-1"))
    self.assertEqual(roots["A"], "")

  def test_setenv(self):
    r = self._bits("setenv", "A,B", "-c", "sh", "-c", 'echo "$A_ROOT $B_ROOT"')
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(r.stdout.split(), [os.path.join(self.sw, ARCH, "A", "1-1"),
                                        os.path.join(self.cvmfs, ARCH, "Packages", "B", "2-1")])

  def test_setenv_leaves_local_basedir(self):
    # A local module loaded by hand later (BASE/1.0 loaded already) needs it.
    r = self._bits("setenv", "B", "-c", "sh", "-c", 'echo "$BASEDIR"')
    self.assertEqual(r.stdout.strip(), os.path.join(self.sw, ARCH))

  def test_unload_from_the_tree_it_was_loaded_from(self):
    # B loaded from CVMFS, then built locally too: unload still removes the CVMFS paths.
    load = self._bits("printenv", "B")
    _write(os.path.join(self.sw, ARCH, "B", "2-1", "etc", "modulefiles", "B"), _modulefile("B", "2-1"))
    self._bits("printenv", "A")                    # refreshes the local module cache
    script = (load.stdout + "\n" + 'eval "$(%s -a %s unload B)"\n' % (os.path.join(self.inst, "bits"), ARCH)
              + 'echo "$PATH"\n')
    r = subprocess.run(["bash", "-c", script], cwd=self.work, env=self.env, capture_output=True, text=True)
    self.assertNotIn(os.path.join(self.cvmfs, ARCH, "Packages", "B"), r.stdout)

  def test_tree_through_a_symlink(self):
    # BASE/1.0 resolves BASEDIR physically; unload (BASE loaded already) must too.
    os.rename(os.path.join(self.cvmfs, ARCH), os.path.join(self.cvmfs, "real-opt"))
    os.symlink("real-opt", os.path.join(self.cvmfs, ARCH))
    self.assertEqual(self._printenv("B")["B"],
                     os.path.join(self.cvmfs, "real-opt", "Packages", "B", "2-1"))
    load = self._bits("printenv", "B")
    script = (load.stdout + "\n" + 'eval "$(%s -a %s unload B)"\n' % (os.path.join(self.inst, "bits"), ARCH)
              + 'echo "$PATH"\n')
    r = subprocess.run(["bash", "-c", script], cwd=self.work, env=self.env, capture_output=True, text=True)
    self.assertNotIn("real-opt/Packages/B", r.stdout)

  def test_dev_refuses_cvmfs_modules(self):
    r = self._bits("load", "--dev", "B")
    self.assertNotEqual(r.returncode, 0)
    self.assertIn("--dev works with local modules only", r.stderr)

  def test_other_compiler_and_unset_prefix_not_found(self):
    self.assertNotEqual(self._bits("printenv", "D/1-1").returncode, 0)
    r = self._bits("printenv", "B/2-1", BITS_CVMFS_PREFIX="")
    self.assertNotEqual(r.returncode, 0)
    self.assertIn("B/2-1 was not found", r.stderr)


  # cvmfs.yaml in the recipe repository: the trees without $BITS_CVMFS_PREFIX.
  _LAYOUT = ('# CVMFS layout\nprefix:  "%s"   # the group root\n'
             "cvmfs_modules_template: '{prefix}/{arch}/Modules/modulefiles/{pkg}'\n")

  def _layout(self, text=None, where=None):
    _write(os.path.join(where or self.work, "cvmfs.yaml"), text or self._LAYOUT % self.cvmfs)
    self.env.pop("BITS_CVMFS_PREFIX")

  def test_layout_file(self):
    self._layout()
    r = self._bits("q")
    self.assertEqual(r.returncode, 0, r.stderr)
    self.assertEqual(r.stdout.split(), ["A/1-1", "B/2-1", "C/3-1", "E/5-1", "gcc/14-1"])
    self.assertEqual(self._printenv("B")["B"], os.path.join(self.cvmfs, ARCH, "Packages", "B", "2-1"))

  def test_layout_file_beside_the_work_dir(self):
    # bits run from a subdirectory finds ../sw, and cvmfs.yaml beside it.
    self._layout()
    sub = os.path.join(self.work, "sub")
    os.makedirs(sub)
    r = subprocess.run([os.path.join(self.inst, "bits"), "-a", ARCH, "q", "^B/"], cwd=sub,
                       env=self.env, capture_output=True, text=True)
    self.assertEqual(r.stdout.split(), ["B/2-1"], r.stderr)

  def test_prefix_variable_wins_over_layout_file(self):
    self._layout()
    r = self._bits("q", BITS_CVMFS_PREFIX="")         # empty: CVMFS modules off
    self.assertEqual(r.stdout.split(), ["A/1-1"])

  def test_layout_file_unusable_template(self):
    self._layout('prefix: %s\ncvmfs_modules_template: "{prefix}/{release}/{arch}/M/{pkg}"\n'
                 % self.cvmfs)
    r = self._bits("q")
    self.assertEqual(r.stdout.split(), ["A/1-1"])
    self.assertIn("no CVMFS modules", r.stderr)

  def test_layout_file_without_a_uses_the_local_arch(self):
    os.makedirs(os.path.join(self.sw, "MODULES", ARCH))   # bits picks ARCH as the local arch
    self._layout()
    r = subprocess.run([os.path.join(self.inst, "bits"), "q"], cwd=self.work,
                       env=self.env, capture_output=True, text=True)
    self.assertEqual(r.stdout.split(), ["A/1-1", "B/2-1", "C/3-1", "E/5-1", "gcc/14-1"], r.stderr)

  def test_layout_file_picks_this_hosts_arch(self):
    # No -a and no tree for the local arch: the trees published for this
    # host's CPU and OS, if for one compiler. A CVMFS root of its own.
    cpu, osname = platform.machine(), _host_os()
    if not osname:
      self.skipTest("bits picks a published arch on el<N> and Ubuntu hosts only")
    root = os.path.join(self.tmp, "cvmfs2")
    self._layout(self._LAYOUT % root)
    host = "%s-%s-gcc14" % (cpu, osname)
    for arch, mod in ((host + "-opt", "B/2-1"), (host, "gcc/14-1"), ("x86_64-el1-gcc9-opt", "D/1-1")):
      mroot = os.path.join(root, arch, "Modules", "modulefiles")
      _write(os.path.join(mroot, "BASE", "1.0"), _CVMFS_BASE)
      _write(os.path.join(mroot, mod), _modulefile(*mod.split("/")))
    q = lambda: subprocess.run([os.path.join(self.inst, "bits"), "q"], cwd=self.work,
                               env=self.env, capture_output=True, text=True)
    r = q()
    self.assertEqual(r.stdout.split(), ["B/2-1", "gcc/14-1"], r.stderr)
    # Two compilers for this host: none is picked, the note lists them.
    _write(os.path.join(root, "%s-%s-gcc15-opt" % (cpu, osname), "Modules", "modulefiles",
                        "BASE", "1.0"), _CVMFS_BASE)
    r = q()
    self.assertEqual(r.stdout.split(), [])
    self.assertIn("%s-%s-gcc15-opt" % (cpu, osname), r.stderr)
    self.assertIn("bits -a <arch> q", r.stderr)

  def test_layout_file_quiet_without_published_trees(self):
    # No /cvmfs here (e.g. a laptop): no CVMFS modules and no note.
    self._layout(self._LAYOUT % os.path.join(self.tmp, "nothing"))
    r = self._bits("q")
    self.assertEqual((r.stdout.split(), r.stderr), (["A/1-1"], ""))

if __name__ == "__main__":
  unittest.main()
