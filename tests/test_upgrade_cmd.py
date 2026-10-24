# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits upgrade: version edits, the overlay it writes, trial hashes and copy-back."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

from bits_helpers import upgrade_cmd as up
from bits_helpers.upgrade_cmd import NeedsHand

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]


class EditTest(unittest.TestCase):
  def test_tag_rules(self):
    self.assertEqual(up.new_tag("v1.2.13", "1.2.13", "1.3.1"), "v1.3.1")
    self.assertEqual(up.new_tag("curl-8_18_0", "8.18.0", "8.21.0"), "curl-8_21_0")
    self.assertEqual(up.new_tag("1-2-3", "1.2.3", "1.2.4"), "1-2-4")
    self.assertIsNone(up.new_tag("v%(version)s", "1.0", "2.0"))
    with self.assertRaises(NeedsHand):
      up.new_tag("release-candidate", "1.0", "2.0")

  def test_recipe_version_and_tag(self):
    text = 'package: curl\nversion: "8.18.0"   # pinned\ntag: curl-8_18_0\n---\nversion: 8.18.0\n'
    out, edits = up.edit_recipe(text, "8.18.0", "8.21.0")
    self.assertEqual(out, 'package: curl\nversion: "8.21.0"   # pinned\ntag: curl-8_21_0\n'
                          '---\nversion: 8.18.0\n')          # the body is never touched
    self.assertEqual(edits, [("version", "8.18.0", "8.21.0"), ("tag", "curl-8_18_0", "curl-8_21_0")])

  def test_recipe_derived_tag_and_refusals(self):
    out, edits = up.edit_recipe("package: im\nversion: 7.1\ntag: v%(version)s\n---\n", "7.1", "7.2")
    self.assertIn("tag: v%(version)s", out)
    self.assertEqual(edits, [("version", "7.1", "7.2")])
    with self.assertRaises(NeedsHand):                 # not the version it says it is
      up.edit_recipe("package: a\nversion: 1.1\n---\n", "1.0", "2.0")
    with self.assertRaises(NeedsHand):                 # a tag that does not spell it
      up.edit_recipe("package: a\nversion: 1.0\ntag: master\n---\n", "1.0", "2.0")

  def test_override_with_matchers(self):
    text = ("package: defaults-dev4\nversion: v1\noverrides:\n"
            "  ROOT:\n    version: \"6.30\"\n"
            "  ROOT:osx.*:\n    version: \"6.30\"\n    tag: v6-30\n"
            "  zlib:\n    version: 1.3\n"
            "env:\n  A: b\n---\n")
    out, edits = up.edit_override(text, "root", "6.30", "6.32", active=lambda m: m != "osx.*")
    self.assertEqual(edits, [("version", "6.30", "6.32")])
    self.assertIn('  ROOT:\n    version: "6.32"\n', out)
    self.assertIn('  ROOT:osx.*:\n    version: "6.30"\n    tag: v6-30\n', out)   # not this build's
    out, _ = up.edit_override(text, "root", "6.30", "6.32", active=lambda m: True)
    self.assertIn("tag: v6-32", out)
    with self.assertRaises(NeedsHand):
      up.edit_override(text, "gcc", "13", "14")

  def test_override_list_form_and_pin(self):
    out, _ = up.edit_override("overrides:\n  - zlib = 1.3\n---\n", "zlib", "1.3", "1.3.1")
    self.assertEqual(out, "overrides:\n  - zlib = 1.3.1\n---\n")
    out, edits = up.edit_pin("package: a\nrequires:\n  - \"zlib = 1.3\"\n  - curl\n---\n", "zlib", "1.3", "1.4")
    self.assertIn('  - "zlib = 1.4"\n', out)
    self.assertEqual(edits, [("pin", "1.3", "1.4")])
    with self.assertRaises(NeedsHand):
      up.edit_pin("package: a\nrequires:\n  - zlib\n---\n", "zlib", "1.3", "1.4")


class MoreEditTest(unittest.TestCase):
  def test_a_version_yaml_would_read_as_a_number_is_quoted(self):
    out, _ = up.edit_recipe("package: z\nversion: 1.3\n---\n", "1.3", "1.10")
    self.assertIn('version: "1.10"\n', out)
    out, _ = up.edit_recipe("package: z\nversion: 1.3\n---\n", "1.3", "1.3.1")
    self.assertIn("version: 1.3.1\n", out)

  def test_override_entries_only(self):
    text = ("overrides:\n"
            "  app:\n    version: 2\n    requires:\n      - zlib = 1.3\n"
            "    variables:\n      version: 1.3\n"
            "  zlib:\n    variables:\n      version: 1.3\n    version: 1.3\n---\n")
    out, edits = up.edit_override(text, "zlib", "1.3", "1.4")
    self.assertEqual(edits, [("version", "1.3", "1.4")])
    self.assertIn("      - zlib = 1.3\n", out)                    # app's requires: not an entry
    self.assertEqual(out.count("version: 1.3\n"), 2)              # the variables: stay
    self.assertTrue(out.endswith("    version: \"1.4\"\n---\n"))       # quoted: 1.4 reads as a number

  def test_source_checks_resolve_the_new_tag(self):
    seen = []
    def tag_exists(source, tag):
      seen.append(tag)
      return True, "found"
    def resolve(spec, text):
      return text.replace("%(version)s", spec["version"])
    orig = up._git_tag_exists
    up._git_tag_exists = tag_exists
    try:
      spec = {"package": "a", "version": "1.0", "tag": "v1.0"}
      up.check_sources(spec, "package: a\nversion: 2.0\ntag: v%(version)s\nsource: https://x/a\n---\n",
                       "2.0", resolve)
      up.check_sources(spec, "package: a\nversion: 2.0\nsource: https://x/a\n---\n", "2.0", resolve)
    finally:
      up._git_tag_exists = orig
    self.assertEqual(seen, ["v2.0", "2.0"])


class WantedTest(unittest.TestCase):
  PLAN = {"releases": [{"build_id": "LCG_110-x", "waves": [
      {"wave": 0, "packages": [{"name": "curl", "target": "8.21.0"}]},
      {"wave": 1, "packages": [{"name": "zlib", "target": "1.3.2"}]}]}],
      "converged": [{"name": "curl", "version": "8.22.0", "releases": [
          {"build_id": "LCG_110-x", "action": "upgrade"}]},
          {"name": "java", "version": "21", "releases": [
          {"build_id": "LCG_110-x", "action": "pinned", "pinned_by": "defaults:dev4", "version": "17"}]}]}

  def _args(self, **kw):
    tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, tmp, True)
    path = os.path.join(tmp, "plan.json")
    with open(path, "w") as f:
      json.dump(self.PLAN, f)
    base = dict(to=[], plan=path, line=None, waves=[], converged=False)
    base.update(kw)
    return argparse.Namespace(**base)

  def _parser(self):
    p = argparse.ArgumentParser()
    p.error = lambda msg: (_ for _ in ()).throw(SystemExit(msg))
    return p

  def test_waves_converged_and_set(self):
    self.assertEqual(up._wanted(self._args(waves=[0]), self._parser()), {"curl": "8.21.0"})
    self.assertEqual(up._wanted(self._args(waves=[0, 1]), self._parser()),
                     {"curl": "8.21.0", "zlib": "1.3.2"})
    self.assertEqual(up._wanted(self._args(converged=True), self._parser()), {"curl": "8.22.0"})
    self.assertEqual(up._wanted(self._args(plan=None, to=["go=1.26.9"]), self._parser()),
                     {"go": "1.26.9"})
    with self.assertRaises(SystemExit):
      up._wanted(self._args(), self._parser())          # no wave chosen
    with self.assertRaises(SystemExit):
      up._wanted(self._args(plan=None, to=["go"]), self._parser())


class MergeTest(unittest.TestCase):
  def test_three_way(self):
    base = "a\nversion: 1\nb\nc\n"
    ours = "a\nversion: 2\nb\nc\n"
    self.assertEqual(up._merge(base, base, ours), (ours, 0))
    self.assertEqual(up._merge(None, base, ours), (ours, 0))
    text, n = up._merge("a\nversion: 1\nb\nc\nd\n", base, ours)      # kept, not reverted
    self.assertEqual((text, n), ("a\nversion: 2\nb\nc\nd\n", 0))
    text, n = up._merge("a\nversion: 3\nb\nc\n", base, ours)
    self.assertTrue(n)
    self.assertIn("<<<<<<< target", text)


RECIPES = {
    "defaults-release.sh": 'package: defaults-release\nversion: v1\noverrides:\n  zlib:\n'
                           '    version: "1.3.1"\n---\n',
    "zlib.sh": 'package: zlib\nversion: "1.2.13"\ntag: v1.2.13\nsource: https://example.invalid/zlib\n'
               '---\necho zlib\n',
    "curl.sh": "package: curl\nversion: 8.18.0\ntag: curl-8_18_0\nsource: https://example.invalid/curl\n"
               "requires:\n  - zlib\n---\necho curl\n",
    "app.sh": 'package: app\nversion: "1.0"\nrequires:\n  - curl\n---\necho app\n',
    "other.sh": 'package: other\nversion: "2.0"\n---\necho other\n',
}


class UpgradeFlowTest(unittest.TestCase):
  """From a build to an overlay, its trial hashes, and back into the repository."""

  def setUp(self):
    self.tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.tmp, True)
    t = self.tmp
    self.remote = os.path.join(t, "recipes.git")
    self.cfg = os.path.join(t, "recipes")
    os.makedirs(self.cfg)
    for name, text in RECIPES.items():
      with open(os.path.join(self.cfg, name), "w") as f:
        f.write(text)
    self._git(self.cfg, "init", "-q")
    self._git(self.cfg, "add", ".")
    self._git(self.cfg, "commit", "-qm", "init")
    self._git(t, "clone", "-q", "--bare", self.cfg, self.remote)
    self._git(self.cfg, "remote", "add", "origin", self.remote)
    site = os.path.join(t, "site")
    os.makedirs(site)
    with open(os.path.join(site, "99-test.yaml"), "w") as f:
      f.write("providers:\n  url: null\n")          # no network
    self.env = dict(os.environ, BITS_SITE_DIR=site, XDG_CONFIG_HOME=os.path.join(t, "xdg"),
                    GIT_CONFIG_GLOBAL=os.path.join(t, "gitconfig"))
    for k in ("BITS_RECIPE_OVERLAY", "BITS_PATH", "BITS_PROVIDERS", "BITS_REPO_DIR"):
      self.env.pop(k, None)
    with open(self.env["GIT_CONFIG_GLOBAL"], "w") as f:
      f.write("[user]\n\tname = t\n\temail = t@t\n")
    self.manifest = os.path.join(t, "bits-manifest.json")
    with open(self.manifest, "w") as f:
      json.dump({"requested_packages": ["app", "other"], "architecture": "slc9_x86-64",
                 "defaults": ["release"]}, f)
    self.ov = os.path.join(t, "ov")

  def _git(self, cwd, *argv):
    subprocess.run(GIT + list(argv), cwd=cwd, check=True, capture_output=True)

  def _bits(self, *argv):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bitsBuild")] + list(argv),
                       cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=300)
    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
    return r.stdout

  def _hashes(self, cfg, *extra):
    out = self._bits("status", "app", "other", "-c", cfg, "-w", os.path.join(self.tmp, "sw"),
                     "-a", "slc9_x86-64", "--json", *extra)
    doc = json.loads(out[out.index("{"):])
    return {p["package"]: (p["version"], p["hash"]) for p in doc["packages"]}

  def test_overlay_trial_and_copy_back(self):
    base = self._hashes(self.cfg)
    self._bits("upgrade", "--manifest", self.manifest, "--recipe-overlay", self.ov, "--no-check",
               "--to", "curl=8.21.0", "--to", "zlib=1.3.2", "--session", "s1",
               "-c", self.cfg, "-w", os.path.join(self.tmp, "sw"))
    self.assertEqual(sorted(os.listdir(self.ov)), ["curl.sh", "defaults-release.sh", "overlay.yaml"])
    with open(os.path.join(self.ov, "overlay.yaml")) as f:
      index = yaml.safe_load(f)
    self.assertEqual(index["session"], "s1")
    self.assertEqual(index["files"]["defaults-release.sh"]["edits"],
                     [{"package": "zlib", "from": "1.3.1", "to": "1.3.2"}])
    self.assertEqual(index["files"]["curl.sh"]["origin"]["path"], "curl.sh")

    # Only the edited packages and their dependents change hash; the defaults
    # package does not, though its profile was edited.
    trial = self._hashes(self.cfg, "--recipe-overlay", self.ov)
    changed = {p for p in base if base[p][1] != trial[p][1]}
    self.assertEqual(changed, {"zlib", "curl", "app"})
    self.assertEqual(trial["curl"][0], "8.21.0")
    self.assertEqual(trial["zlib"][0], "1.3.2")

    # Copy-back onto an unchanged target gives the trial's hashes.
    self._bits("upgrade", "--apply", self.ov, "--target", "main", "--repo", "recipes=" + self.cfg,
               "-w", os.path.join(self.tmp, "sw"))
    tree = os.path.join(self.tmp, "sw", "UPGRADE", "s1", "recipes-main")
    log = subprocess.run(["git", "-C", tree, "log", "--format=%s", "main..HEAD"],
                         capture_output=True, text=True, check=True).stdout.split("\n")
    self.assertEqual(log[:2], ["zlib 1.3.2", "curl 8.21.0"])
    self.assertEqual(self._hashes(tree), trial)

  def test_copy_back_keeps_target_changes(self):
    self._bits("upgrade", "--manifest", self.manifest, "--recipe-overlay", self.ov, "--no-check",
               "--to", "curl=8.21.0", "-c", self.cfg, "-w", os.path.join(self.tmp, "sw"))
    other = os.path.join(self.tmp, "other")
    self._git(self.tmp, "clone", "-q", self.remote, other)
    with open(os.path.join(other, "curl.sh"), "a") as f:
      f.write("echo more\n")
    self._git(other, "commit", "-qam", "more")
    self._git(other, "push", "-q", "origin", "main")
    self._bits("upgrade", "--apply", self.ov, "--target", "main", "--repo", "recipes=" + self.cfg,
               "-w", os.path.join(self.tmp, "sw"))
    with open(os.path.join(self.tmp, "sw", "UPGRADE", "ov", "recipes-main", "curl.sh")) as f:
      text = f.read()
    self.assertIn("version: 8.21.0\n", text)
    self.assertTrue(text.endswith("echo curl\necho more\n"))

  def test_upgrading_again_and_unlisted_files(self):
    for v in ("8.21.0", "8.22.0"):
      self._bits("upgrade", "--manifest", self.manifest, "--recipe-overlay", self.ov, "--no-check",
                 "--to", "curl=" + v, "-c", self.cfg, "-w", os.path.join(self.tmp, "sw"))
    with open(os.path.join(self.ov, "curl.sh")) as f:
      self.assertIn("version: 8.22.0\ntag: curl-8_22_0\n", f.read())
    with open(os.path.join(self.ov, "overlay.yaml")) as f:
      self.assertEqual(yaml.safe_load(f)["files"]["curl.sh"]["edits"],
                       [{"package": "curl", "from": "8.18.0", "to": "8.22.0"}])
    with open(os.path.join(self.ov, "extra.sh"), "w") as f:
      f.write("package: extra\nversion: 1\n---\n")
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bitsBuild"), "upgrade", "--apply", self.ov,
                        "--target", "main", "--repo", "recipes=" + self.cfg, "-w", os.path.join(self.tmp, "sw")],
                       cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=120)
    self.assertNotEqual(r.returncode, 0)
    self.assertIn("extra.sh", r.stdout + r.stderr)

  def test_a_base_not_on_the_remote_is_refused(self):
    with open(os.path.join(self.cfg, "curl.sh"), "a") as f:
      f.write("echo local\n")
    self._git(self.cfg, "commit", "-qam", "local, not pushed")
    self._bits("upgrade", "--manifest", self.manifest, "--recipe-overlay", self.ov, "--no-check",
               "--to", "curl=8.21.0", "-c", self.cfg, "-w", os.path.join(self.tmp, "sw"))
    clone = os.path.join(self.tmp, "clone")
    self._git(self.tmp, "clone", "-q", self.remote, clone)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bitsBuild"), "upgrade", "--apply", self.ov,
                        "--target", "main", "--repo", "recipes=" + clone, "-w", os.path.join(self.tmp, "sw")],
                       cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=120)
    self.assertNotEqual(r.returncode, 0)
    self.assertIn("push it first", r.stdout + r.stderr)

  def test_trial_manifest_is_not_upgraded(self):
    with open(self.manifest, "w") as f:
      json.dump({"requested_packages": ["app"], "recipe_overlay": {"dir": "x"}}, f)
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bitsBuild"), "upgrade", "--manifest",
                        self.manifest, "--recipe-overlay", self.ov, "--to", "curl=1"],
                       cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=120)
    self.assertNotEqual(r.returncode, 0)
    self.assertIn("trial build", r.stderr)


if __name__ == "__main__":
  unittest.main()
