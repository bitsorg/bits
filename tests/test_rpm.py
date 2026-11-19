import argparse
import contextlib
import glob
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import bits_helpers
from bits_helpers import rpm

ARCH = "x86_64-el9"


def make_package(work_dir, name, pkg_hash, deps=()):
  pkg = {"name": name, "version": "1.0", "revision": "1", "hash": pkg_hash,
         "effective_architecture": ARCH}
  pp = rpm.work_path(pkg)
  root = os.path.join(work_dir, pp)
  os.makedirs(os.path.join(root, "etc", "profile.d"))
  shutil.copy(os.path.join(os.path.dirname(bits_helpers.__file__), "relocate-me.sh"), root)
  files = {
    "etc/profile.d/.bits-pkginfo": 'OP=%s\nPP=${PKGPATH:-%s}\nPH=%s\nPKG_DIR="%s"\n'
                                   % (pp, pp, pkg_hash, work_dir),
    "etc/profile.d/.bits-relocate": "etc/profile.d/init.sh\npaths.txt\n",
    "etc/profile.d/init.sh": "WORK_DIR=%s\n" % work_dir + "".join(
      '. "$WORK_DIR/$BITS_ARCH_PREFIX"/%s/1.0-1/etc/profile.d/init.sh\n' % d["name"]
      for d in deps) + 'export %s_ROOT="$WORK_DIR/$BITS_ARCH_PREFIX"/%s/1.0-1\n' % (name, name),
    "paths.txt": "%s/INSTALLROOT/%s/%s/lib\n" % (work_dir, pkg_hash, pp) + "".join(
      "%s/%s/lib\n" % (work_dir, rpm.work_path(d)) for d in deps),
    ".meta.json": json.dumps({"package": pkg, "dependencies": {
      kind: {"runtime": list(deps), "build": []} for kind in ("direct", "recursive")}}),
  }
  for path, text in files.items():
    with open(os.path.join(root, path), "w") as f:
      f.write(text)
  return pkg


class RpmTest(unittest.TestCase):
  def setUp(self):
    self.work_dir = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.work_dir, True)
    self.zlib = make_package(self.work_dir, "zlib", "z" * 40)
    self.hello = make_package(self.work_dir, "hello", "h" * 40, [self.zlib])
    self.root = os.path.join(self.work_dir, ARCH, "hello", "1.0-1")
    with open(os.path.join(self.root, ".meta.json")) as f:
      self.meta = json.load(f)

  def args(self, **kw):
    values = dict(package="hello", version=None, workDir=self.work_dir, architecture=ARCH,
                  rpmPrefix="/opt/bits", rpmView=None, rpmRepo=None, rpmRepoUrl=None,
                  rpmSignKey=None, rpmOutputDir=os.path.join(self.work_dir, "out"))
    values.update(kw)
    return SimpleNamespace(**values)

  def test_names_and_paths(self):
    self.assertEqual(rpm.rpm_name(self.hello), "hello-hhhhhhhhhhhh_1.0_x86_64_el9")
    self.assertEqual(rpm.rpm_path(self.hello), "hello/1.0-hhhhhhhhhhhh/x86_64-el9")
    self.assertEqual(rpm.release_path("LCG_110", self.hello), "LCG_110/hello/1.0/x86_64-el9")
    self.assertEqual(rpm.work_path(dict(self.hello, pkg_family="py", revision="")),
                     "x86_64-el9/py/hello/1.0")

  def test_stage_points_everything_at_the_rpm_paths(self):
    top = os.path.join(self.work_dir, "stage")
    rpm.stage(self.root, self.meta, "/opt/bits", top)
    dest = os.path.join(top, "root", "opt", "bits", "hello", "1.0-" + "h" * 12, ARCH)
    with open(os.path.join(dest, "paths.txt")) as f:
      self.assertEqual(f.read().split(), ["/opt/bits/hello/1.0-hhhhhhhhhhhh/x86_64-el9/lib",
                                          "/opt/bits/zlib/1.0-zzzzzzzzzzzz/x86_64-el9/lib"])
    with open(os.path.join(dest, "etc", "profile.d", "init.sh")) as f:
      init = f.read()
    self.assertIn('. "/opt/bits/zlib/1.0-zzzzzzzzzzzz/x86_64-el9"/etc/profile.d/init.sh', init)
    self.assertIn('export hello_ROOT="/opt/bits/hello/1.0-hhhhhhhhhhhh/x86_64-el9"', init)
    with open(os.path.join(self.root, "paths.txt")) as f:
      self.assertIn(self.work_dir, f.read())

  def test_spec(self):
    lines = rpm.spec(self.meta, "/opt/bits", "/stage").splitlines()
    for line in ("Name: hello-hhhhhhhhhhhh_1.0_x86_64_el9", "Prefix: /opt/bits",
                 "Requires: zlib-zzzzzzzzzzzz_1.0_x86_64_el9",
                 '  rm -rf "hello/1.0-hhhhhhhhhhhh/x86_64-el9"',
                 '  rmdir -p --ignore-fail-on-non-empty "hello/1.0-hhhhhhhhhhhh" || :',
                 '"/opt/bits/hello/1.0-hhhhhhhhhhhh/x86_64-el9"'):
      self.assertIn(line, lines)

  def test_view_links_each_release_path_to_the_hashed_path(self):
    packages = [(self.root, self.meta), (None, {"package": self.zlib})]
    top = os.path.join(self.work_dir, "stage")
    rpm.stage_view("LCG_110", packages, "/opt/bits", top)
    link = os.path.join(top, "root", "opt", "bits", "LCG_110", "hello", "1.0", ARCH)
    self.assertEqual(os.readlink(link), "../../../hello/1.0-hhhhhhhhhhhh/x86_64-el9")
    text = rpm.view_spec("LCG_110", self.meta, packages, "/opt/bits", "/stage")
    self.assertRegex(text, r"\nVersion: \d{14}\n")
    lines = text.splitlines()
    for line in ("Name: bits-view-LCG_110_x86_64_el9", "Release: hhhhhhhhhhhh", "BuildArch: noarch",
                 "Prefix: /opt/bits", "Requires: hello-hhhhhhhhhhhh_1.0_x86_64_el9",
                 "Requires: zlib-zzzzzzzzzzzz_1.0_x86_64_el9", '"/opt/bits/LCG_110"'):
      self.assertIn(line, lines)

  def test_unsafe_names(self):
    for bad in ('1.0"; rm -rf /; "', "$(id)", "..", "-rf"):
      self.meta["package"]["version"] = bad
      with open(os.path.join(self.root, ".meta.json"), "w") as f:
        json.dump(self.meta, f)
      with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
           mock.patch("shutil.which", return_value="/usr/bin/rpmbuild"):
        rpm.doRpm(self.args(), argparse.ArgumentParser())

  def test_unsafe_view(self):
    with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
      rpm.doRpm(self.args(rpmView=".."), argparse.ArgumentParser())

  def test_a_dependency_not_installed_here(self):
    shutil.rmtree(os.path.join(self.work_dir, ARCH, "zlib"))
    with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
         mock.patch("shutil.which", return_value="/usr/bin/rpmbuild"):
      rpm.doRpm(self.args(), argparse.ArgumentParser())

  @unittest.skipUnless(shutil.which("rpmbuild"), "needs rpmbuild")
  def test_rpmbuild(self):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
      rpm.doRpm(self.args(), argparse.ArgumentParser())
    out = os.path.join(self.work_dir, "out")
    made = sorted(os.listdir(out))
    self.assertEqual([m.split("-")[0] for m in made], ["hello", "zlib"])
    files = subprocess.run(["rpm", "-qlp", os.path.join(out, made[0])], capture_output=True,
                           text=True, check=True).stdout.split()
    self.assertIn("/opt/bits/hello/1.0-hhhhhhhhhhhh/x86_64-el9/paths.txt", files)

  @unittest.skipUnless(shutil.which("rpmbuild"), "needs rpmbuild")
  def test_view_rpm(self):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
      rpm.doRpm(self.args(rpmView="dev3"), argparse.ArgumentParser())
    out = os.path.join(self.work_dir, "out")
    view, = [m for m in os.listdir(out) if m.startswith("bits-view-dev3_")]
    requires = subprocess.run(["rpm", "-qp", "--requires", os.path.join(out, view)],
                              capture_output=True, text=True, check=True).stdout.split("\n")
    self.assertIn("hello-hhhhhhhhhhhh_1.0_x86_64_el9", requires)
    self.assertIn("zlib-zzzzzzzzzzzz_1.0_x86_64_el9", requires)
    files = subprocess.run(["rpm", "-qlp", os.path.join(out, view)], capture_output=True,
                           text=True, check=True).stdout.split()
    self.assertIn("/opt/bits/dev3/hello/1.0/x86_64-el9", files)

  def test_repo_file(self):
    self.assertEqual(rpm.repo_file("file:///srv/rpms", False),
                     "[bits]\nname=bits\nbaseurl=file:///srv/rpms\nenabled=1\n"
                     "gpgcheck=0\nrepo_gpgcheck=0\n")
    self.assertIn("gpgkey=https://x/rpms/RPM-GPG-KEY-bits\n", rpm.repo_file("https://x/rpms", True))
    self.assertIn("gpgcheck=1\nrepo_gpgcheck=1\n", rpm.repo_file("https://x/rpms", True))

  def test_repo_url_needs_repo(self):
    with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
      rpm.doRpm(self.args(rpmRepoUrl="https://x/rpms"), argparse.ArgumentParser())

  def test_a_signed_repository_needs_its_key(self):
    repo = os.path.join(self.work_dir, "repo")
    os.makedirs(repo)
    open(os.path.join(repo, "RPM-GPG-KEY-bits"), "w").close()
    with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()), \
         mock.patch("shutil.which", return_value="/usr/bin/tool"):
      rpm.doRpm(self.args(rpmRepo=repo), argparse.ArgumentParser())

  def test_an_unsigned_repository_stays_unsigned(self):
    repo = os.path.join(self.work_dir, "repo")
    os.makedirs(repo)
    open(os.path.join(repo, "old.rpm"), "w").close()
    self.assertIn("is not signed", rpm.signing_problem(repo, "some key"))
    self.assertIsNone(rpm.signing_problem(repo, None))

  @unittest.skipUnless(shutil.which("rpmbuild") and shutil.which("createrepo_c"),
                       "needs rpmbuild and createrepo_c")
  def test_repo(self):
    repo = os.path.join(self.work_dir, "repo")
    published = []
    for url in ("http://example.org/repo", None):
      with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        rpm.doRpm(self.args(rpmRepo=repo, rpmRepoUrl=url), argparse.ArgumentParser())
      rpms = sorted(glob.glob(os.path.join(repo, "*.rpm")))
      published.append([(r, os.stat(r).st_mtime_ns) for r in rpms])
    self.assertTrue(os.path.exists(os.path.join(repo, "repodata", "repomd.xml")))
    self.assertEqual(len(published[1]), 2)
    self.assertEqual(published[0], published[1])
    with open(os.path.join(repo, "bits.repo")) as f:
      self.assertIn("baseurl=http://example.org/repo\n", f.read())

  @unittest.skipUnless(shutil.which("rpmbuild") and shutil.which("createrepo_c"),
                       "needs rpmbuild and createrepo_c")
  def test_a_repository_keeps_its_prefix(self):
    repo = os.path.join(self.work_dir, "repo")
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
      rpm.doRpm(self.args(rpmRepo=repo), argparse.ArgumentParser())
      with self.assertRaises(SystemExit):
        rpm.doRpm(self.args(rpmRepo=repo, rpmPrefix="/opt/other"), argparse.ArgumentParser())

  @unittest.skipUnless(shutil.which("rpmbuild"), "needs rpmbuild")
  def test_default_output_dir(self):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
      rpm.doRpm(self.args(rpmOutputDir=None), argparse.ArgumentParser())
    out = os.path.join(self.work_dir, "RPMS", "hello-hhhhhhhhhhhh_1.0_x86_64_el9")
    self.assertEqual(len(os.listdir(out)), 2)


if __name__ == "__main__":
  unittest.main()
