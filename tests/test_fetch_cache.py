# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits_helpers.fetch_cache: the sources a build downloads itself (issue #129)."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from bits_helpers import fetch_cache

ARCH = "slc9_x86-64"
TEMPLATE = os.path.join(os.path.dirname(fetch_cache.__file__), "build_template.sh")


def template_listing():
  """build_template.sh's lines that list the fetch cache, as a bash script."""
  with open(TEMPLATE) as f:
    text = f.read()
  start = text.index("# What the build left in its fetch cache")
  return text[start:text.index("\nfi\n", start) + 4]


class Store:
  """The backends' fetch_source/upload_source in memory. Like rsync, an upload
  keeps the local file's name, so it must be the one asked for."""

  def __init__(self, write=True):
    self.objects, self.writeStore = {}, "bucket" if write else ""

  def fetch_source(self, url_checksum, filename, dest_dir):
    if (url_checksum, filename) not in self.objects:
      return False
    with open(os.path.join(dest_dir, filename), "wb") as f:
      f.write(self.objects[(url_checksum, filename)])
    return True

  def upload_source(self, local_path, url_checksum, filename):
    assert os.path.basename(local_path) == filename, (local_path, filename)
    if self.writeStore:
      with open(local_path, "rb") as f:
        self.objects.setdefault((url_checksum, filename), f.read())   # never overwritten

  def indexes(self):
    return [json.loads(v) for (_, n), v in self.objects.items() if n == fetch_cache.INDEX]


def spec(version="2.16", **kw):
  return dict(package="tensorflow", version=version, fetch_cache=True, **kw)


class FetchCacheTest(unittest.TestCase):
  def setUp(self):
    self.work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.work, True)
    self.store = Store()

  def cache(self, s=None):
    return os.path.join(self.work, fetch_cache.relative_dir(s or spec(), ARCH))

  def _build(self, s, files):
    """Restore, download *files* into the cache, list it with build_template.sh's
    lines, save."""
    state = fetch_cache.restore(s, self.store, self.work, "cms", ARCH)
    for rel, data in files.items():
      os.makedirs(os.path.dirname(os.path.join(self.cache(s), rel)), exist_ok=True)
      with open(os.path.join(self.cache(s), rel), "wb") as f:
        f.write(data)
    buildroot = os.path.join(self.work, "BUILD")
    os.makedirs(buildroot, exist_ok=True)
    subprocess.run(["bash", "-c", "set -eo pipefail\n" + template_listing()], check=True,
                   env=dict(os.environ, CACHED_TARBALL="", BUILDROOT=buildroot,
                            BITS_FETCH_CACHE=self.cache(s)))
    fetch_cache.save(s, self.store, self.work, "cms", ARCH, state,
                     os.path.join(buildroot, fetch_cache.LISTING))
    return state

  def test_a_build_saves_what_it_fetched_and_a_clean_build_gets_it_back(self):
    self._build(spec(), {"content_addressable/sha256/ab/file": b"llvm", "a b": b"x"})
    (index,) = self.store.indexes()
    self.assertEqual([f["path"] for f in index["files"]], ["a b", "content_addressable/sha256/ab/file"])
    shutil.rmtree(os.path.join(self.work, "FETCH_CACHE"))
    state = fetch_cache.restore(spec(), self.store, self.work, "cms", ARCH)
    with open(os.path.join(self.cache(), "content_addressable/sha256/ab/file"), "rb") as f:
      self.assertEqual(f.read(), b"llvm")
    self.assertEqual((len(state.files), len(state.confirmed)), (2, 2))
    self.assertEqual(sorted(os.listdir(os.path.join(self.work, "FETCH_CACHE"))), [ARCH])

  def test_the_list_is_written_once(self):
    self._build(spec(), {"a": b"1"})
    self._build(spec(), {"b": b"2"})                 # not in the list: a warning
    (index,) = self.store.indexes()
    self.assertEqual([f["path"] for f in index["files"]], ["a"])

  def test_versions_and_architectures_share_files_but_have_their_own_lists(self):
    self._build(spec("2.16"), {"a": b"same"})
    self._build(spec("2.17"), {"a": b"same"})
    self.assertEqual(len([k for k in self.store.objects if k[1] == fetch_cache.BLOB]), 1)
    self.assertEqual(len(self.store.indexes()), 2)
    state = fetch_cache.restore(spec("2.16"), self.store, self.work, "cms", "osx_arm64")
    self.assertIsNone(state.files)

  def test_a_file_missing_from_the_store_is_uploaded_again(self):
    self._build(spec(), {"a": b"1"})
    sha = hashlib.sha256(b"1").hexdigest()
    del self.store.objects[(sha, fetch_cache.BLOB)]
    shutil.rmtree(os.path.join(self.work, "FETCH_CACHE"))
    self._build(spec(), {"a": b"1"})                 # the build downloads it again
    self.assertIn((sha, fetch_cache.BLOB), self.store.objects)

  def test_an_empty_cache_writes_no_list(self):
    self._build(spec(), {})
    self.assertEqual(self.store.indexes(), [])
    self._build(spec(), {"a": b"1"})
    self.assertEqual(len(self.store.indexes()), 1)

  def test_one_failed_file_does_not_stop_the_restore(self):
    self._build(spec(), {"a": b"1", "b": b"2"})
    shutil.rmtree(os.path.join(self.work, "FETCH_CACHE"))
    real, bad = self.store.fetch_source, hashlib.sha256(b"1").hexdigest()

    def flaky(key, filename, tmp):
      if key == bad:
        raise OSError("SlowDown")
      return real(key, filename, tmp)
    self.store.fetch_source = flaky
    state = fetch_cache.restore(spec(), self.store, self.work, "cms", ARCH)
    self.assertEqual((sorted(state.files), os.listdir(self.cache())), (["a", "b"], ["b"]))

  def test_a_symlink_out_of_the_cache_is_not_followed(self):
    self._build(spec(), {"d/a": b"1"})
    shutil.rmtree(self.cache())
    outside = os.path.join(self.work, "outside")
    os.makedirs(outside)
    os.makedirs(self.cache())
    os.symlink(outside, os.path.join(self.cache(), "d"))
    fetch_cache.restore(spec(), self.store, self.work, "cms", ARCH)
    self.assertEqual(os.listdir(outside), [])

  def test_a_tampered_file_or_path_is_not_restored(self):
    self._build(spec(), {"a": b"good"})
    self.store.objects[(hashlib.sha256(b"good").hexdigest(), fetch_cache.BLOB)] = b"evil"
    key = next(k for k in self.store.objects if k[1] == fetch_cache.INDEX)
    index = json.loads(self.store.objects[key])
    index["files"].append(dict(index["files"][0], path="../../escape"))
    self.store.objects[key] = json.dumps(index).encode()
    shutil.rmtree(os.path.join(self.work, "FETCH_CACHE"))
    state = fetch_cache.restore(spec(), self.store, self.work, "cms", ARCH)
    self.assertEqual((os.listdir(self.cache()), list(state.files)), ([], ["a"]))
    self.assertFalse(os.path.exists(os.path.join(self.work, "escape")))

  def test_no_write_store_or_no_redistribution_saves_nothing(self):
    self.store = Store(write=False)
    self._build(spec(), {"a": b"1"})
    self.assertEqual(self.store.objects, {})
    self.store = Store()
    self._build(spec(redistributable="binaries"), {"b": b"2"})
    self.assertEqual(self.store.objects, {})

  def test_an_unreadable_file_only_skips_saving(self):
    if os.geteuid() == 0:
      self.skipTest("root reads everything")
    os.makedirs(self.cache())
    with open(os.path.join(self.cache(), "locked"), "w"):
      pass
    os.chmod(os.path.join(self.cache(), "locked"), 0)
    self._build(spec(), {"a": b"1"})                 # the listing fails, the build does not
    self.assertEqual(self.store.objects, {})

  def test_the_defaults_must_name_the_cache(self):
    self.assertEqual([fetch_cache.cache_name(v) for v in (None, "cms", "../x", True)],
                     ["", "cms", "", ""])
    self.assertFalse(fetch_cache.enabled(spec(), ""))
    self.assertFalse(fetch_cache.enabled({"package": "zlib"}, "cms"))
    self.assertTrue(fetch_cache.enabled(spec(), "cms"))

  def test_a_store_error_does_not_fail_the_build(self):
    class Broken(Store):
      def fetch_source(self, *a):
        raise OSError("503")

      def upload_source(self, *a):
        raise SystemExit(1)                    # as dieOnError does
    self.store = Broken()
    self._build(spec(), {"a": b"1"})            # neither raises


if __name__ == "__main__":
  unittest.main()
