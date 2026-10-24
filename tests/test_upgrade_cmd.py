# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits upgrade: version edits, plans, and the recipes edited in place."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest


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
  def _plan(self, text):
    d = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, d, True)
    path = os.path.join(d, "plan.csv")
    with open(path, "w") as f:
      f.write(text)
    return path

  def _args(self, **kw):
    base = dict(to=[], plan=None, waves=[], line=None)
    base.update(kw)
    return argparse.Namespace(**base)

  def _parser(self):
    p = argparse.ArgumentParser()
    p.error = lambda msg: (_ for _ in ()).throw(SystemExit(msg))
    return p

  def test_the_consoles_csv_exports(self):
    per_line = self._plan("wave,package,from,to,fixes,kev,risk,rebuilds,major,pinned_by\r\n"
                          "0,go,1.12.5,1.21.3,CVE-1,CVE-1,80,13,,\r\n1,curl,8.1.0,8.6.0,CVE-2,,9,3,,\r\n"
                          "host,zlib,1.2,1.3,CVE-3,,1,,,\r\nno fix,left,1.0,,CVE-4,,3,,,\r\n")
    self.assertEqual(up.wanted(self._args(plan=per_line, waves=[0]), self._parser()), {"go": "1.21.3"})
    self.assertEqual(up.wanted(self._args(plan=per_line, waves=[0, 1]), self._parser()),
                     {"go": "1.21.3", "curl": "8.6.0"})
    with self.assertRaises(SystemExit):
      up.wanted(self._args(plan=per_line), self._parser())          # no wave chosen
    conv = self._plan("package,version,community,line,platform,from,action,major,pinned_by,own_target,kev,risk\r\n"
                      "curl,8.7.0,lcg,release_gcc15,x86_64-el9,8.1.0,upgrade,,,8.6.0,,9\r\n"
                      "java,21,lcg,release_gcc15,x86_64-el9,17,pinned,,defaults:dev4,21,,2\r\n"
                      "curl,8.7.0,lcg,release_gcc14,x86_64-el9,8.7.0,keep,,,,,0\r\n")
    self.assertEqual(up.wanted(self._args(plan=conv, line="release_gcc15"), self._parser()), {"curl": "8.7.0"})
    with self.assertRaises(SystemExit):
      up.wanted(self._args(plan=conv), self._parser())             # which line
    self.assertEqual(up.wanted(self._args(to=["go=1.26.9"]), self._parser()), {"go": "1.26.9"})
    bom = self._plan("\ufeffwave,package,from,to\r\n0,go,1,2\r\n0,short\r\n")
    self.assertEqual(up.wanted(self._args(plan=bom, waves=[0]), self._parser()), {"go": "2"})
    for bad in (self._args(), self._args(to=["go"]), self._args(plan=self._plan("a,b\r\n1,2\r\n")),
                self._args(plan=per_line, waves=[7])):
      with self.assertRaises(SystemExit):
        up.wanted(bad, self._parser())


RECIPES = {
    "defaults-release.sh": 'package: defaults-release\nversion: v1\noverrides:\n  zlib:\n'
                           '    version: "1.3.1"\n---\n',
    "zlib.sh": 'package: zlib\nversion: "1.2.13"\ntag: v1.2.13\nsource: https://example.invalid/zlib\n'
               '---\necho zlib\n',
    "app.sh": 'package: app\nversion: "1.0"\nrequires:\n  - curl\n---\necho app\n',
    "other.sh": 'package: other\nversion: "2.0"\n---\necho other\n',
}
PROVIDED = {   # in a repository provider's cached checkout
    "curl.sh": "package: curl\nversion: 8.18.0\ntag: curl-8_18_0\nsource: https://example.invalid/curl\n"
               "patches:\n  - curl-fix.patch\n  - curl-osx.patch:osx.* && strip=0\nrequires:\n  - zlib\n"
               "---\necho curl\n",
    "patches/curl-fix.patch": "--- a\n+++ b\n",
    "patches/curl-osx.patch": "--- a\n+++ b\n",
    "checksums/curl.checksum": "sources: {}\n",
    ".bits_provider_ok": "abc\n",
}


class InPlaceTest(unittest.TestCase):
  def setUp(self):
    self.tmp = t = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, t, True)
    self.cfg, self.sw = os.path.join(t, "recipes"), os.path.join(t, "sw")
    self.cache = os.path.join(self.sw, "REPOS", "lcg.bits", "abc")
    for d, files in ((self.cfg, RECIPES), (self.cache, PROVIDED)):
      for name, text in files.items():
        os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
        with open(os.path.join(d, name), "w") as f:
          f.write(text)
      self._git(d, "init", "-q")
      self._git(d, "add", ".")
      self._git(d, "commit", "-qm", "init")
    site = os.path.join(t, "site")
    os.makedirs(site)
    with open(os.path.join(site, "99-test.yaml"), "w") as f:
      f.write("providers:\n  url: null\n")          # no network
    self.env = dict(os.environ, BITS_SITE_DIR=site, XDG_CONFIG_HOME=os.path.join(t, "xdg"),
                    BITS_PATH=self.cache)
    for k in ("BITS_PROVIDERS", "BITS_REPO_DIR"):
      self.env.pop(k, None)

  def _git(self, cwd, *argv):
    subprocess.run(GIT + list(argv), cwd=cwd, check=True, capture_output=True)

  def _bits(self, *argv, code=0):
    r = subprocess.run([sys.executable, os.path.join(ROOT, "bitsBuild")] + list(argv),
                       cwd=self.tmp, env=self.env, capture_output=True, text=True, timeout=300)
    self.assertEqual(r.returncode, code, r.stdout + r.stderr)
    return r.stdout + r.stderr

  def _upgrade(self, *more, code=0):
    return self._bits("upgrade", "app", "other", "-c", self.cfg, "-w", self.sw, "-a", "slc9_x86-64",
                      "--no-check", *more, code=code)

  def _versions(self):
    out = self._bits("status", "app", "other", "-c", self.cfg, "-w", self.sw, "-a", "slc9_x86-64", "--json")
    return {p["package"]: p["version"] for p in json.loads(out[out.index("{"):out.rindex("}") + 1])["packages"]}

  def test_edited_in_place_or_copied_from_a_provider(self):
    self._bits("-n", "upgrade", "app", "other", "-c", self.cfg, "-w", self.sw, "-a", "slc9_x86-64",
               "--no-check", "--to", "curl=8.21.0")
    self.assertFalse(os.path.exists(os.path.join(self.cfg, "curl.sh")))     # a dry run writes nothing
    out = self._upgrade("--to", "curl=8.21.0", "--to", "zlib=1.3.2", "--to", "other=2.0")
    self.assertIn("copied curl.sh, patches/curl-fix.patch, patches/curl-osx.patch,"
                  " checksums/curl.checksum from", out)
    with open(os.path.join(self.cfg, "curl.sh")) as f:
      self.assertIn("version: 8.21.0\ntag: curl-8_21_0\n", f.read())
    self.assertTrue(os.path.isfile(os.path.join(self.cfg, "patches", "curl-fix.patch")))
    with open(os.path.join(self.cache, "curl.sh")) as f:
      self.assertEqual(f.read(), PROVIDED["curl.sh"])                       # the cache untouched
    with open(os.path.join(self.cfg, "defaults-release.sh")) as f:
      self.assertIn('  zlib:\n    version: "1.3.2"\n', f.read())             # the override pins it
    self.assertEqual(self._versions(), {"defaults-release": "v1", "zlib": "1.3.2", "curl": "8.21.0",
                                        "app": "1.0", "other": "2.0"})

  def test_two_edits_of_one_copied_recipe(self):
    with open(os.path.join(self.cache, "curl.sh")) as f:
      text = f.read().replace("  - zlib\n", "  - zlib = 1.3.1\n")     # curl pins zlib
    with open(os.path.join(self.cache, "curl.sh"), "w") as f:
      f.write(text)
    for dry in (True, False):
      out = self._bits(*(["-n"] if dry else []), "upgrade", "app", "other", "-c", self.cfg, "-w", self.sw,
                       "-a", "slc9_x86-64", "--no-check", "--to", "curl=8.21.0", "--to", "zlib=1.3.2")
      self.assertIn("in the copy made above", out)
    with open(os.path.join(self.cfg, "curl.sh")) as f:
      text = f.read()
    self.assertIn("version: 8.21.0", text)
    self.assertIn("  - zlib = 1.3.2\n", text)

  def test_a_copy_never_replaces_a_file(self):
    with open(os.path.join(self.cfg, "curl.sh"), "w") as f:
      f.write("mine\n")
    with self.assertRaises(NeedsHand):
      up._bring_in(os.path.join(self.cache, "curl.sh"), self.cfg, "curl", dry=False)
    with open(os.path.join(self.cfg, "curl.sh")) as f:
      self.assertEqual(f.read(), "mine\n")
    self.assertFalse(os.path.exists(os.path.join(self.cfg, "patches")))

  def test_left_for_a_hand_edit(self):
    out = self._upgrade("--to", "nope=1", "--to", "zlib=1.3.2", code=1)
    self.assertIn("not in the build", out)
    with open(os.path.join(self.cfg, "app.sh"), "w") as f:
      f.write('package: app\nversion: "1.0"\ntag: master\nrequires:\n  - curl\n---\n')
    self.assertIn("edit by hand", self._upgrade("--to", "app=2.0", code=1))


if __name__ == "__main__":
  unittest.main()
