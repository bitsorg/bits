# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""`bits publish PACKAGE --cvmfs-target` publishes the package's tarball relocated to the
target in a working copy: the installed tree (already relocated to the work dir) is
left as it is, and a tarball the build removed is fetched from the store."""

import os
import shutil
import tarfile
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bits_helpers import publish

H = "5a" * 20
PP = "el9/zlib/1.3-1"
BUILD = "/build/sw"            # the build's work dir, in the tarball's paths
TARGET = "/cvmfs/r/x"
RELOCATE_ME = os.path.join(os.path.dirname(publish.__file__), "relocate-me.sh")


def write(path, text):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    f.write(text)


def package_tree(root, pkg_dir, prefix):
  """A package as build_template.sh leaves it: relocation records for *pkg_dir*, its
  paths under *prefix*."""
  write(os.path.join(root, ".meta.json"), "{}")
  write(os.path.join(root, "lib", "zlib.pc"), "libdir=%s/lib\n" % prefix)
  write(os.path.join(root, "etc", "profile.d", ".bits-relocate"), "lib/zlib.pc\n")
  write(os.path.join(root, "etc", "profile.d", ".bits-pkginfo"),
        'OP=%s\nPP=${PKGPATH:-%s}\nPH=%s\nPKG_DIR="%s"\n' % (PP, PP, H, pkg_dir))
  shutil.copy(RELOCATE_ME, os.path.join(root, "relocate-me.sh"))


class Parser:
  def error(self, msg):
    raise SystemExit(msg)


class TarballTest(unittest.TestCase):
  def setUp(self):
    self.work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.work)
    src = os.path.join(self.work, "src")
    package_tree(os.path.join(src, PP), BUILD, "%s/INSTALLROOT/%s/%s" % (BUILD, H, PP))
    self.tar = os.path.join(self.work, "zlib-1.3-1.el9.tar.gz")
    with tarfile.open(self.tar, "w:gz") as tf:
      tf.add(src, ".")
    shutil.rmtree(src)
    self.root = os.path.join(self.work, PP)        # installed: relocated to the work dir
    package_tree(self.root, self.work, "%s/%s" % (self.work, PP))
    write(os.path.join(self.root, ".build-hash"), H + "\n")
    self.installed = sorted((dp, f, open(os.path.join(dp, f)).read())
                            for dp, _d, fs in os.walk(self.root) for f in fs)

  def keep_in_work_dir(self):
    d = os.path.join(self.work, "TARS", "el9", "store", H[:2], H)
    os.makedirs(d)
    shutil.copy(self.tar, d)

  def publish(self, store_has=True, no_relocate=False, dry_run=False):
    args = SimpleNamespace(publishView=None, fromManifest=None, package="zlib", version=None,
                           workDir=self.work, architecture="el9", cvmfsTarget=TARGET,
                           scratchDir=None, prepubUrl="https://prepub", prepubToken=None,
                           prepubRepo=None, prepubPath=None, prepubWebhook=None,
                           prepubPollInterval=10, prepubTimeout=60, prepubNoVerifyTls=False,
                           dryRun=dry_run, publishStore="b3://bucket",
                           noRelocate=no_relocate)
    published, self.asked = {}, []

    def fetch(spec, stores, arch, fetch_dir, want=None):
      self.asked.append((spec, stores, arch))
      return self.tar if store_has else None

    def publish_tree(tree, *a, **kw):
      published.update({os.path.relpath(os.path.join(dp, f), tree): open(os.path.join(dp, f)).read()
                        for dp, _d, fs in os.walk(tree) for f in fs})
    with patch.object(publish, "_find_installroot", return_value=self.root), \
         patch("bits_helpers.cvmfs_publish.fetch_store_tarball", fetch), \
         patch.object(publish, "_publish_tree", publish_tree):
      publish.doPublish(args, Parser())
    return published

  def assert_installed_untouched(self):
    self.assertEqual(sorted((dp, f, open(os.path.join(dp, f)).read())
                            for dp, _d, fs in os.walk(self.root) for f in fs), self.installed)
    self.assertEqual(os.listdir(os.path.join(self.work, "TMP")), [])

  def test_the_tarball_relocated_to_the_target(self):
    self.keep_in_work_dir()
    got = self.publish()
    self.assertEqual(got["lib/zlib.pc"], "libdir=%s/lib\n" % TARGET)
    self.assertIn('PKG_DIR="%s"' % TARGET, got["etc/profile.d/.bits-pkginfo"])
    self.assertFalse([f for f in got if f.endswith(".unrelocated")])
    self.assertEqual(got[".build-hash"], H + "\n")          # strict symlink reuse reads it
    self.assertEqual(self.asked, [])
    self.assert_installed_untouched()

  def test_fetched_from_the_store_when_the_build_removed_it(self):
    got = self.publish()
    self.assertEqual(got["lib/zlib.pc"], "libdir=%s/lib\n" % TARGET)
    spec, stores, arch = self.asked[0]
    self.assertEqual((spec["version"], spec["revision"], spec["hash"], stores, arch),
                     ("1.3", "1", H, ["b3://bucket"], "el9"))
    self.assert_installed_untouched()

  def test_nothing_published_without_a_tarball(self):
    with self.assertRaises(SystemExit):
      self.publish(store_has=False)
    self.assert_installed_untouched()

  def test_a_dry_run_sends_nothing(self):
    for local in (True, False):
      if local:
        self.keep_in_work_dir()
      with patch.object(publish, "banner") as said:
        self.assertEqual(self.publish(dry_run=True), {})
      text = said.call_args[0][0] % said.call_args[0][1:]
      self.assertIn("would publish", text)
      self.assertIn(TARGET, text)
      self.assertIn("store" if local else "fetched from the stores", text)
      self.assertEqual(self.asked, [])                       # not fetched either
      shutil.rmtree(os.path.join(self.work, "TARS"), ignore_errors=True)

  def test_no_relocate_publishes_the_installed_tree(self):
    """Built with --cvmfs-prefix, the installed tree has the final paths; the tarball not."""
    got = self.publish(store_has=False, no_relocate=True)
    self.assertEqual(got["lib/zlib.pc"], "libdir=%s/%s/lib\n" % (self.work, PP))
    self.assertEqual(self.asked, [])
    self.assert_installed_untouched()

  def test_a_familys_package_by_its_own_name(self):
    root = os.path.join(self.work, "el9", "externals", "foo", "1.0-1")
    write(os.path.join(root, ".build-hash"), H)
    tar = os.path.join(self.work, "TARS", "el9", "store", H[:2], H, "foo-1.0-1.el9.tar.gz")
    write(tar, "")
    got = publish._package_tarball(SimpleNamespace(), self.work, root, "externals/foo", "1.0-1",
                                   os.path.join(self.work, "fetch"))
    self.assertEqual(got, tar)


if __name__ == "__main__":
  unittest.main()
