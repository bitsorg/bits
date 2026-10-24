# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Recipe overlays: searched first, each file keeps its origin, a build with one is a trial."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from bits_helpers import recipe_overlay
from bits_helpers.paths import getConfigPaths, resolveFilename


def _write(path, text=""):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w") as f:
    f.write(text)


class RecipeOverlayTest(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.mkdtemp()
    self.cfg = os.path.join(self.tmp, "cfg")
    self.prov = os.path.join(self.tmp, "prov")
    self.ov = os.path.join(self.tmp, "ov")
    for d in (self.cfg, self.prov, self.ov):
      os.makedirs(d)
    _write(os.path.join(self.ov, recipe_overlay.INDEX), "session: s1\n")
    env = {k: v for k, v in os.environ.items() if k not in (recipe_overlay.ENV, "BITS_PATH")}
    env["BITS_PATH"] = self.prov
    patcher = mock.patch.dict(os.environ, env, clear=True)
    patcher.start()
    self.addCleanup(patcher.stop)
    self.addCleanup(shutil.rmtree, self.tmp, True)

  def test_activate_needs_an_index(self):
    with self.assertRaises(ValueError):
      recipe_overlay.activate(self.cfg)
    self.assertEqual(recipe_overlay.directory(), "")
    self.assertEqual(recipe_overlay.activate(self.ov), self.ov)
    self.assertEqual(os.environ[recipe_overlay.ENV], self.ov)

  def test_searched_first_and_stands_for_its_origin(self):
    _write(os.path.join(self.prov, "curl.sh"), "package: curl\n")
    _write(os.path.join(self.prov, "zlib.sh"), "package: zlib\n")
    _write(os.path.join(self.ov, "curl.sh"), "package: curl\n")
    _write(os.path.join(self.ov, "new.sh"), "package: new\n")
    self.assertEqual(getConfigPaths(self.cfg), [self.cfg, self.prov])
    recipe_overlay.activate(self.ov)
    self.assertEqual(getConfigPaths(self.cfg), [self.ov, self.cfg, self.prov])
    # Read from the overlay, at home in the repository whose file it shadows.
    self.assertEqual(resolveFilename({}, "curl", self.cfg, {}),
                     (os.path.join(self.ov, "curl.sh"), self.prov))
    self.assertEqual(resolveFilename({}, "zlib", self.cfg, {}),
                     (os.path.join(self.prov, "zlib.sh"), self.prov))
    # A file only the overlay has stays at home in the overlay.
    self.assertEqual(resolveFilename({}, "new", self.cfg, {})[1], self.ov)

  def test_home_keeps_the_spelling_of_the_search_dir(self):
    # generated-package tables are keyed by the search dirs as spelt.
    _write(os.path.join(self.cfg, "curl.sh"))
    _write(os.path.join(self.ov, "curl.sh"))
    recipe_overlay.activate(self.ov)
    rel = os.path.relpath(self.cfg)
    self.assertEqual(recipe_overlay.home(os.path.join(self.ov, "curl.sh"), [self.ov, rel]), rel)
    self.assertEqual(recipe_overlay.home(os.path.join(self.cfg, "curl.sh"), [rel]), self.cfg)

  def test_patches_overlay_first_then_origin(self):
    _write(os.path.join(self.ov, "patches", "fix.patch"), "ours")
    spec = {"pkgdir": self.prov, "recipe_overlay": True}
    recipe_overlay.activate(self.ov)
    self.assertEqual(recipe_overlay.patch_file(spec, "fix.patch"),
                     os.path.join(self.ov, "patches", "fix.patch"))
    self.assertEqual(recipe_overlay.patch_file(spec, "other.patch"),
                     os.path.join(self.prov, "patches", "other.patch"))
    # A recipe not read from the overlay never takes the overlay's patches.
    self.assertEqual(recipe_overlay.patch_file({"pkgdir": self.prov}, "fix.patch"),
                     os.path.join(self.prov, "patches", "fix.patch"))

  def test_files_for_the_manifest(self):
    _write(os.path.join(self.ov, "curl.sh"), "x")
    _write(os.path.join(self.ov, ".git", "HEAD"), "ref")
    recipe_overlay.activate(self.ov)
    self.assertEqual(sorted(recipe_overlay.files()), ["curl.sh", "overlay.yaml"])

  def test_refuse_trials(self):
    self.assertIsNone(recipe_overlay.refuse({"packages": [{"package": "a"}]}, "publish it"))
    self.assertIsNone(recipe_overlay.refuse(None, "publish it"))
    self.assertIn("publish it", recipe_overlay.refuse({"recipe_overlay": {"dir": "x"}}, "publish it"))
    self.assertTrue(recipe_overlay.refuse({"packages": [{"recipe_overlay": True}]}, "x"))
    self.assertTrue(recipe_overlay.refuse({"package": "a", "recipe_overlay": True}, "x"))


class ManifestRecordTest(unittest.TestCase):
  def test_overlay_and_raw_architecture_recorded(self):
    from bits_helpers.manifest import BuildManifest
    tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, tmp, True)
    m = BuildManifest(work_dir=tmp, requested_packages=["curl"], architecture="slc9_x86-64-gcc15",
                      defaults=["release", "gcc15"], config_dir=tmp, config_commit="c",
                      raw_architecture="slc9_x86-64")
    m.set_recipe_overlay("/ov", "s1", {"curl.sh": "ab"})
    m.add_package({"package": "curl", "version": "8.21.0", "revision": "1", "hash": "h",
                   "remote_revision_hash": "h", "local_revision_hash": "h"}, "built_from_source")
    with open(m.path) as f:
      doc = json.load(f)
    self.assertEqual(doc["raw_architecture"], "slc9_x86-64")
    self.assertEqual(doc["recipe_overlay"], {"dir": "/ov", "session": "s1", "files": {"curl.sh": "ab"}})
    self.assertTrue(doc["packages"][0]["recipe_overlay"])
    self.assertTrue(recipe_overlay.refuse(doc, "publish it"))


class CertifyTrialTest(unittest.TestCase):
  def test_trial_manifests_are_skipped_never_certified(self):
    from bits_helpers import certify
    normal, trial = {"packages": [{"package": "a"}]}, {"recipe_overlay": {"session": "s"}, "packages": []}
    self.assertEqual(certify._without_trials([normal, trial]), [normal])
    with self.assertRaises(certify.CertifyError):
      certify._without_trials([trial])
    self.assertEqual(certify._without_trials([]), [])


class TrialArgsTest(unittest.TestCase):
  def _parse(self, *argv):
    from bits_helpers.args import doParseArgs
    with mock.patch("bits_helpers.arch.getoutput", return_value="x86_64"), \
         mock.patch.object(sys, "argv", ["bits"] + list(argv)):
      return doParseArgs()[0]

  def test_a_trial_writes_to_no_store(self):
    tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, tmp, True)
    _write(os.path.join(tmp, recipe_overlay.INDEX), "session: s\n")
    with mock.patch.dict(os.environ, {}, clear=False):
      os.environ.pop(recipe_overlay.ENV, None)
      args = self._parse("build", "zlib", "-a", "slc9_x86-64", "--recipe-overlay", tmp,
                         "--remote-store", "s3://store::rw")
      self.assertEqual(args.recipeOverlay, tmp)
      self.assertEqual(args.writeStore, "")
      self.assertEqual(args.remoteStore, "s3://store")
      self.assertTrue(args.recipeOverlayDroppedWrite)
      self.assertEqual(os.environ[recipe_overlay.ENV], tmp)

  def test_other_commands_ignore_it(self):
    with mock.patch.dict(os.environ, {recipe_overlay.ENV: "/somewhere"}, clear=False):
      self._parse("deps", "zlib", "-a", "slc9_x86-64", "--outgraph", "x.pdf")
      self.assertEqual(recipe_overlay.directory(), "")

  def test_status_takes_the_overlay_too(self):
    tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, tmp, True)
    _write(os.path.join(tmp, recipe_overlay.INDEX), "session: s\n")
    with mock.patch.dict(os.environ, {recipe_overlay.ENV: tmp}, clear=False):
      args = self._parse("status", "zlib", "-a", "slc9_x86-64")
      self.assertEqual(args.recipeOverlay, tmp)


if __name__ == "__main__":
  unittest.main()
