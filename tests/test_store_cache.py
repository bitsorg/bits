"""Tests for the plan's cache of store listings (bits_helpers/store_cache.py)."""
import json
import os
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bits_helpers import plan, store_cache

ARCH = "x86_64-el9-gcc14-opt"


class StoreCacheTest(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)

  def test_found_tarballs_are_kept_between_runs(self):
    c = store_cache.StoreCache(self.tmp.name, "b3://store")
    c.put(ARCH, "h1", ["z-1-1.tar.gz"])
    c.put(ARCH, "h2", [])                                      # nothing found: not kept
    c.save()
    again = store_cache.StoreCache(self.tmp.name, "b3://store")
    self.assertEqual((again.get(ARCH, "h1"), again.get(ARCH, "h2")), (["z-1-1.tar.gz"], None))
    self.assertIsNone(store_cache.StoreCache(self.tmp.name, "b3://other").get(ARCH, "h1"))
    self.assertTrue(again.path.startswith(os.path.join(self.tmp.name, ".store-cache") + os.sep))

  def test_two_runs_merge_and_old_entries_go(self):
    a = store_cache.StoreCache(self.tmp.name, "s")
    b = store_cache.StoreCache(self.tmp.name, "s")
    a.put(ARCH, "h1", ["a"])
    b.put(ARCH, "h2", ["b"])
    a.save()
    b.save()                                                   # merged with a's
    c = store_cache.StoreCache(self.tmp.name, "s")
    self.assertEqual((c.get(ARCH, "h1"), c.get(ARCH, "h2")), (["a"], ["b"]))
    with patch.object(store_cache, "_today", lambda: 10 ** 6):
      c.put(ARCH, "h3", ["c"])
      c.entries["%s/h2" % ARCH]["used"] = 10 ** 6
      c.save()
    with open(c.path) as f:
      self.assertEqual(sorted(json.load(f)["entries"]), ["%s/h2" % ARCH, "%s/h3" % ARCH])

  def test_a_bad_or_unwritable_file_is_only_a_miss(self):
    c = store_cache.StoreCache(self.tmp.name, "s")
    os.makedirs(os.path.dirname(c.path))
    with open(c.path, "w") as f:
      f.write("{not json")
    self.assertIsNone(store_cache.StoreCache(self.tmp.name, "s").get(ARCH, "h"))
    nowhere = store_cache.StoreCache(os.path.join(self.tmp.name, "file"), "s")
    open(os.path.join(self.tmp.name, "file"), "w").close()      # a file, not a directory
    nowhere.put(ARCH, "h", ["x"])
    nowhere.save()                                             # logged, not raised
    self.assertIsNone(store_cache.StoreCache("", "s").path)

  def test_a_tarball_not_there_after_all_is_forgotten(self):
    c = store_cache.StoreCache(self.tmp.name, "s")
    c.put(ARCH, "h1", ["a"])
    c.save()
    d = store_cache.StoreCache(self.tmp.name, "s")
    d.forget(ARCH, ["h1"])
    d.save()                                                   # not merged back
    self.assertIsNone(store_cache.StoreCache(self.tmp.name, "s").get(ARCH, "h1"))

  def test_store_id(self):
    multi = SimpleNamespace(readers=[SimpleNamespace(remoteStore="a"), SimpleNamespace(remoteStore="b")])
    self.assertEqual(store_cache.store_id(multi), "a|b")
    self.assertEqual(store_cache.store_id(SimpleNamespace()), "")


class KeptListingsTest(unittest.TestCase):
  def setUp(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)
    self.kept = store_cache.StoreCache(tmp.name, "s")
    self.calls = []

  def helper(self):
    calls = self.calls

    class Helper:
      def list_store_tarballs(self, arch, h):
        calls.append(h)
        return ["%s.tar.gz" % h] if h.startswith("in") else []
    return Helper()

  def test_a_certified_tarball_is_not_asked_for_again(self):
    trusted = {(ARCH, "in1"): "sha"}
    first = plan._Listings(self.helper(), self.kept, trusted)
    self.assertEqual(first.prefetch([(ARCH, "in1"), (ARCH, "in2"), (ARCH, "out")]), 3)
    self.assertEqual(sorted(self.kept.entries), ["%s/in1" % ARCH])   # certified only
    again = plan._Listings(self.helper(), self.kept, trusted)
    self.assertEqual(again.prefetch([(ARCH, "in1"), (ARCH, "in2"), (ARCH, "out")]), 2)
    self.assertEqual(again.list_store_tarballs(ARCH, "in1"), ["in1.tar.gz"])
    self.assertEqual(self.calls.count("in1"), 1)
    decertified = plan._Listings(self.helper(), self.kept, {})       # no longer signed
    decertified.list_store_tarballs(ARCH, "in1")
    self.assertEqual(self.calls.count("in1"), 2)
    unsigned_reuse = plan._Listings(self.helper(), self.kept, None)  # signed reuse off
    unsigned_reuse.prefetch([(ARCH, "in1")])
    self.assertEqual(self.calls.count("in1"), 3)

  def test_a_failed_listing_is_not_asked_for_again_ahead(self):
    class Failing:
      def list_store_tarballs(s, arch, h):
        self.calls.append(h)
        raise OSError("timeout")
    lister = plan._Listings(Failing())
    self.assertEqual(lister.prefetch([(ARCH, "x")]), 1)
    self.assertEqual(lister.prefetch([(ARCH, "x")]), 0)                # next round
    with self.assertRaises(OSError):
      lister.list_store_tarballs(ARCH, "x")                             # when needed
    self.assertEqual(self.calls, ["x", "x"])


class RoundsTest(unittest.TestCase):
  """A package not in the store is built with its local hash: the hashes of what
  needs it are listed in a second round, in parallel, not one by one."""

  def test_changed_hashes_are_listed_in_another_round(self):
    tmp = tempfile.TemporaryDirectory()
    self.addCleanup(tmp.cleanup)

    def hashes(p, specs, considerRelocation=False):
      s = specs[p]
      if "remote_revision_hash" in s:
        return
      dep = specs["zlib"]["hash"] if p == "curl" else ""
      s["remote_revision_hash"] = s["remote_hashes"][0] if p == "zlib" else "in-curl-" + dep
      s["remote_hashes"] = [s["remote_revision_hash"]]
      s["local_revision_hash"] = "local-" + p
      s["local_hashes"] = [s["local_revision_hash"]]
    specs = {"zlib": {"package": "zlib", "version": "1", "remote_hashes": ["zr"]},
             "curl": {"package": "curl", "version": "1", "remote_hashes": []}}
    calls = []

    class Store:
      def list_store_tarballs(self, arch, h):
        calls.append((h, threading.current_thread() is threading.main_thread()))
        return ["curl-1-1.%s.tar.gz" % arch] if h.startswith("in-curl-") else []
    from bits_helpers.sync import RemoteSync
    store = type("S", (Store, RemoteSync), {})()
    store.remoteStore = "b3://store"
    args = SimpleNamespace(defaults=["release"], architecture=ARCH)
    cfg = SimpleNamespace(require_signed_reuse=False, fetch_build_requires=False)
    with patch("bits_helpers.build.storeHook"), \
         patch("bits_helpers.hashing.storeHashes", hashes), \
         patch.object(plan, "_overlay_predicate", lambda cfg: None):
      states, _, _ = plan.plan(["zlib", "curl"], specs, args, tmp.name, store, "slc9", cfg, None)
    self.assertEqual(states, {"zlib": plan.BUILD, "curl": plan.REMOTE})
    self.assertEqual(sorted(calls), [("in-curl-local-zlib", False), ("in-curl-zr", False),
                                     ("zr", False)])            # each once, all ahead in parallel
    self.assertEqual(specs["curl"]["hash"], "in-curl-local-zlib")
    # Signed: what was found certified is kept, and not asked for in the next run.
    trusted = {(ARCH, "in-curl-local-zlib"): "sha"}
    for s in specs.values():
      for k in ("remote_revision_hash", "hash", "revision"):
        s.pop(k, None)
    specs["zlib"]["remote_hashes"] = ["zr"]
    cfg.require_signed_reuse = True
    for run in (1, 2):
      del calls[:]
      with patch("bits_helpers.build.storeHook"), \
           patch("bits_helpers.hashing.storeHashes", hashes), \
           patch.object(plan, "_overlay_predicate", lambda cfg: None):
        states, _, _ = plan.plan(["zlib", "curl"], specs, args, tmp.name, store, "slc9", cfg,
                                 lambda: trusted)
      for s in specs.values():
        for k in ("remote_revision_hash", "hash", "revision"):
          s.pop(k, None)
      specs["zlib"]["remote_hashes"] = ["zr"]
      self.assertEqual(states["curl"], plan.REMOTE)
      self.assertEqual(sorted(h for h, _ in calls),
                       ["in-curl-local-zlib", "in-curl-zr", "zr"] if run == 1 else ["in-curl-zr", "zr"])


if __name__ == "__main__":
  unittest.main()
