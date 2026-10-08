"""Tests for the --dry-run reuse plan (bits_helpers/plan.py)."""
import os
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers import plan

ARCH = "x86_64-el9-gcc14-opt"
RH, LH = "aa" + "0" * 38, "bb" + "0" * 38


def _spec(**kw):
  s = {"package": "zlib", "version": "1.3", "remote_hashes": [RH], "local_hashes": [LH],
       "remote_revision_hash": RH, "local_revision_hash": LH}
  s.update(kw)
  return s


class _Store:
  def __init__(self, names=()):
    self.names = list(names)
    self.listed = []

  def list_store_tarballs(self, arch, pkg_hash):
    self.listed.append(pkg_hash)
    return self.names if pkg_hash == RH else []


class ClassifyTest(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.mkdtemp()

  def _tarball(self, h, rev):
    d = os.path.join(self.tmp, "TARS", ARCH, "store", h[:2], h)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "zlib-1.3-%s.%s.tar.gz" % (rev, ARCH)), "w").close()

  def _installed(self, rev, h):
    d = os.path.join(self.tmp, ARCH, "zlib", "1.3-" + rev)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, ".build-hash"), "w") as f:
      f.write(h + "\n")

  def _classify(self, store=None, write_store=False, trusted=None, **kw):
    return plan.classify(_spec(**kw), ARCH, self.tmp, store, store is not None,
                         write_store, trusted)

  def test_installed(self):
    self._tarball(RH, "2")
    self._installed("2", RH)
    self.assertEqual(self._classify(), (plan.INSTALLED, RH, "2"))

  def test_local_tarball(self):
    self._tarball(RH, "2")
    self.assertEqual(self._classify(), (plan.LOCAL_TARBALL, RH, "2"))

  def test_local_revision_under_local_hash(self):
    self._tarball(LH, "local1")
    self.assertEqual(self._classify(), (plan.LOCAL_TARBALL, LH, "local1"))

  def test_local_revision_under_remote_hash_ignored(self):
    self._tarball(RH, "local1")
    self.assertEqual(self._classify()[0], plan.BUILD)

  def test_remote(self):
    store = _Store(["zlib-1.3-4.%s.tar.gz" % ARCH])
    self.assertEqual(self._classify(store), (plan.REMOTE, RH, "4"))

  def test_remote_unsigned(self):
    store = _Store(["zlib-1.3-4.%s.tar.gz" % ARCH])
    self.assertEqual(self._classify(store, trusted={})[0], plan.REMOTE_UNSIGNED)
    self.assertEqual(self._classify(store, trusted={RH: "sha"})[0], plan.REMOTE)

  def test_local_preferred_over_remote(self):
    self._tarball(RH, "2")
    store = _Store(["zlib-1.3-4.%s.tar.gz" % ARCH])
    self.assertEqual(self._classify(store)[0], plan.LOCAL_TARBALL)
    self.assertEqual(store.listed, [])

  def test_build_hash_follows_write_store(self):
    self.assertEqual(self._classify(_Store(), write_store=True), (plan.BUILD, RH, ""))
    self.assertEqual(self._classify(_Store(), write_store=False), (plan.BUILD, LH, ""))

  def _hash_policy_local_fallback(self):
    spec = _spec(
        revision_policy="hash", _revision_policy_hash_injected=True,
        force_revision=RH, hash=RH, revision=RH)
    self._tarball(LH, LH)
    return spec

  def test_hash_policy_local_fallback_finds_local_tarball(self):
    spec = self._hash_policy_local_fallback()
    self.assertEqual(self._classify(**spec), (plan.LOCAL_TARBALL, LH, LH))

  def test_hash_policy_local_fallback_finds_installed_local_build(self):
    spec = self._hash_policy_local_fallback()
    self._installed(LH, LH)
    self.assertEqual(self._classify(**spec), (plan.INSTALLED, LH, LH))

  def test_hash_policy_unsigned_remote_falls_back_to_local_hash(self):
    """Build discards an unsigned remote tarball and builds under the local hash."""
    spec = _spec(revision_policy="hash", _revision_policy_hash_injected=True,
                 force_revision=RH, hash=RH, revision=RH)
    store = _Store(["zlib-1.3-%s.%s.tar.gz" % (RH, ARCH)])
    self.assertEqual(self._classify(store, trusted={}, **spec),
                     (plan.REMOTE_UNSIGNED, LH, LH))
    self.assertEqual(self._classify(store, trusted={RH: "sha"}, **spec),
                     (plan.REMOTE, RH, RH))

  def test_hash_policy_fallback_does_not_change_virtual_package_hash(self):
    spec = _spec(
        provides_repository=True, _revision_policy_hash_injected=True,
        force_revision=RH, hash=RH, revision=RH)
    self.assertEqual(self._classify(**spec), (plan.BUILD, RH, RH))
    self.assertEqual(spec["hash"], RH)

  def test_devel(self):
    self.assertEqual(self._classify(is_devel_pkg=True)[0], plan.DEVEL)


class PrintPlanTest(unittest.TestCase):
  def test_summary(self):
    rows = [("a", "1", plan.INSTALLED, ""), ("b", "1", plan.REMOTE, ""),
            ("c", "1", plan.BUILD, ""), ("d", "1", plan.REMOTE_UNSIGNED, "")]
    with patch("bits_helpers.plan.banner") as b:
      plan.print_plan(rows, store_checked=True)
    text = b.call_args[0][0] % b.call_args[0][1:]
    self.assertIn("2 reused (1 installed, 0 local tarball, 1 from remote store), 2 to build", text)
    self.assertNotIn("not checked", text)
    with patch("bits_helpers.plan.banner") as b:
      plan.print_plan(rows, store_checked=False)
    self.assertIn("remote store not checked", b.call_args[0][0] % b.call_args[0][1:])


class StoreListableTest(unittest.TestCase):
  def test_listing_backends(self):
    from bits_helpers.sync import RemoteSync, NoRemoteSync

    class Listing(RemoteSync):
      def list_store_tarballs(self, arch, pkg_hash):
        return []
    self.assertTrue(plan.store_can_list(Listing()))
    self.assertFalse(plan.store_can_list(NoRemoteSync()))
    self.assertFalse(plan.store_can_list(None))


class MoreClassifyTest(ClassifyTest):
  def test_write_store_skips_local_revisions(self):
    self._tarball(LH, "local1")
    self.assertEqual(self._classify(write_store=True), (plan.BUILD, RH, ""))

  def test_lowest_revision_picked(self):
    store = _Store(["zlib-1.3-10.%s.tar.gz" % ARCH, "zlib-1.3-2.%s.tar.gz" % ARCH])
    self.assertEqual(self._classify(store)[2], "2")

  def test_force_revision(self):
    self._tarball(RH, "")
    os.rename(os.path.join(self.tmp, "TARS", ARCH, "store", RH[:2], RH, "zlib-1.3-.%s.tar.gz" % ARCH),
              os.path.join(self.tmp, "TARS", ARCH, "store", RH[:2], RH, "zlib-1.3.%s.tar.gz" % ARCH))
    self.assertEqual(self._classify(force_revision=""), (plan.LOCAL_TARBALL, RH, ""))
    # Nothing found: a forced revision builds under the remote hash, even without a write store.
    self.assertEqual(self._classify(force_revision="X", remote_hashes=["cc" + "0" * 38],
                                    remote_revision_hash="cc" + "0" * 38),
                     (plan.BUILD, "cc" + "0" * 38, "X"))

  def test_overlay(self):
    got = plan.classify(_spec(), ARCH, self.tmp, None, False, False, None,
                        overlay=lambda spec: True)
    self.assertEqual(got, (plan.OVERLAY, RH, ""))


class PlanBuildTest(unittest.TestCase):
  def test_devel_disables_write_store_for_later_packages(self):
    import types
    specs = {"a": _spec(package="a", is_devel_pkg=True), "b": _spec(package="b")}
    args = types.SimpleNamespace(defaults=["release"], architecture=ARCH)
    cfg = types.SimpleNamespace(require_signed_reuse=False, reuse_overlay=None)
    store = types.SimpleNamespace(writeStore="b3://bucket")
    with patch("bits_helpers.build.storeHook"), patch("bits_helpers.hashing.storeHashes"), \
         patch("bits_helpers.plan.banner"):
      rows = plan.plan_build(["a", "b"], specs, args, tempfile.mkdtemp(), store, "x86_64",
                             cfg, lambda: {})
    self.assertEqual([r[2] for r in rows], [plan.DEVEL, plan.BUILD])
    self.assertEqual(specs["b"]["hash"], LH)   # local hash: uploads are off after a devel pkg


if __name__ == "__main__":
  unittest.main()
