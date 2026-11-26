# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits cvmfs-publish publishes the tarball the manifest certifies: the work dir's
when its sha256 is the manifest's tarball_sha256, else that object fetched from the
store (the build packed its own copy of a hash the store already held), else nothing."""

import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from unittest import mock

import bits_helpers.cvmfs_publish as cp
from bits_helpers.checksum import checksum_file

H = "ab" * 20
NAME = "GCC-14-1.tc.tar.gz"


def make_tar(path, marker):
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with tarfile.open(path, "w:gz") as tf:
    for name, data in (("tc/GCC/14-1/.meta.json", b"{}"), ("tc/GCC/14-1/which", marker)):
      info = tarfile.TarInfo(name)
      info.size = len(data)
      tf.addfile(info, io.BytesIO(data))
  return path


class CertifiedTest(unittest.TestCase):
  def setUp(self):
    self.t = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.t, True)
    self.local = make_tar(os.path.join(self.t, "TARS", NAME), b"local repack")
    self.stored = make_tar(os.path.join(self.t, "store-copy", NAME), b"certified")

  def spec(self, sha):
    s = {"package": "GCC", "version": "14", "revision": "1", "hash": H,
         "effective_architecture": "tc"}
    if sha:
      s["tarball_sha256"] = sha
    return s

  def store(self, copies):
    """Stores by URL: a tarball to hand out, None for nothing, an exception to raise."""
    self.asked = []

    def remote_from_url(url, write, arch, work_dir):
      self.asked.append((url, arch))
      copy = copies[url]

      def fetch_tarball(fspec):
        if isinstance(copy, BaseException):
          raise copy
        if copy:
          d = os.path.join(work_dir, "TARS", "tc", "store", H[:2], H)
          os.makedirs(d)
          shutil.copy(copy, os.path.join(d, "GCC-14-2.tc.tar.gz"))   # any revision name
      return mock.Mock(fetch_tarball=fetch_tarball)
    return mock.patch("bits_helpers.sync.remote_from_url", remote_from_url)

  def certified(self, sha, store="b3://bucket::rw"):
    return cp.certified_tarball(self.spec(sha), {"store": store, "arch": "el9"}, self.local,
                                os.path.join(self.t, "fetch"))

  def test_the_local_one_when_it_is_certified(self):
    self.assertEqual(self.certified(checksum_file(self.local)), self.local)

  def test_the_stores_when_the_local_one_is_another_packing(self):
    with self.store({"b3://bucket": self.stored}):
      got = self.certified(checksum_file(self.stored))
    self.assertEqual(checksum_file(got), checksum_file(self.stored))
    self.assertEqual(self.asked, [("b3://bucket", "tc")])

  def test_the_stores_are_tried_in_order(self):
    stores = {"https://a": None, "b3://b": SystemExit("unreachable"), "b3://c": self.local,
              "b3://d": self.stored, "b3://e": self.stored}
    with self.store(stores):
      got = self.certified(checksum_file(self.stored), store="https://a,b3://b,b3://c,b3://d::rw,b3://e")
    self.assertEqual(checksum_file(got), checksum_file(self.stored))
    self.assertEqual([u for u, _ in self.asked], ["https://a", "b3://b", "b3://c", "b3://d"])

  def test_nothing_when_neither_is(self):
    for copy in (self.local, None, RuntimeError("down")):
      with self.store({"b3://bucket": copy}), self.assertRaisesRegex(SystemExit, "no store"):
        self.certified(checksum_file(self.stored))
    with self.assertRaisesRegex(SystemExit, r"no store \(--store: none\)"):
      self.certified(checksum_file(self.stored), store="")

  def test_without_a_checksum_the_object_named_for_the_revision(self):
    with self.store({"https://a": self.stored}):       # stores it as GCC-14-2
      self.assertIsNone(cp.fetch_store_tarball(self.spec(None), ["https://a"], "tc",
                                               os.path.join(self.t, "f1")))
      spec = dict(self.spec(None), revision="2")
      got = cp.fetch_store_tarball(spec, ["https://a"], "tc", os.path.join(self.t, "f2"))
    self.assertEqual(os.path.basename(got), "GCC-14-2.tc.tar.gz")

  def test_unpack_refuses_climbing_or_absolute_members(self):
    for bad in ("../evil", "/abs/evil", "tc/../../evil"):
      tar = os.path.join(self.t, "bad.tar.gz")
      with tarfile.open(tar, "w:gz") as tf:
        info = tarfile.TarInfo(bad)
        tf.addfile(info, io.BytesIO(b""))
      dest = tempfile.mkdtemp(dir=self.t)
      with self.assertRaisesRegex(SystemExit, "climbing"):
        cp.unpack_package(tar, dest, "bad")
      self.assertEqual(os.listdir(dest), [])
    self.assertTrue(cp.unpack_package(self.stored, tempfile.mkdtemp(dir=self.t), "ok")
                    .endswith(os.path.join("tc", "GCC", "14-1")))

  def signed(self, sha, signed, store="b3://bucket"):
    ctx = {"store": store, "arch": "el9", "signed": {("tc", H): signed}}
    return cp.certified_tarball(self.spec(sha), ctx, self.local, os.path.join(self.t, "fetch"))

  def test_the_signed_manifest_wins_over_the_build_manifest(self):
    with self.store({"b3://bucket": self.stored}), mock.patch("sys.stderr") as err:
      got = self.signed(checksum_file(self.local), checksum_file(self.stored))
    self.assertEqual(checksum_file(got), checksum_file(self.stored))   # not the local one
    self.assertIn("another tarball", "".join(str(c) for c in err.write.call_args_list))

  def test_a_signed_entry_checks_an_old_manifest_too(self):
    self.assertEqual(self.signed(None, checksum_file(self.local)), self.local)
    with self.store({"b3://bucket": None}), self.assertRaisesRegex(SystemExit, "no store"):
      self.signed(None, checksum_file(self.stored))

  def test_the_signed_manifests_are_the_stores_by_default(self):
    seen = {}

    def srcs(store, prefix, endpoint, work_dir, arches):
      seen.setdefault("stores", []).append((store, prefix, endpoint or None))
      return ["https://s/%s-tc.json" % prefix], set()

    def index(args, work_dir):
      seen["trust"] = args.trustManifest
      return {("tc", H): "sha256:x"}
    with mock.patch("bits_helpers.build.trust_manifest_srcs", srcs), \
         mock.patch("bits_helpers.build.trusted_reuse_index", index):
      no_ep = {k: "" for k in ("BITS_S3_ENDPOINT_URL", "S3_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3",
                               "AWS_ENDPOINT_URL")}   # what another test's S3 setup exported
      with mock.patch.dict(os.environ, dict(no_ep, BITS_S3_B_ENDPOINT_URL="https://s3.b")):
        self.assertEqual(cp.signed_index("bits-store+https://s/store::rw,b3://b", self.t, ["tc"],
                                         prefix="STAGING/m"), {("tc", H): "sha256:x"})
      self.assertEqual(cp.signed_index("", self.t, ["tc"], "https://a/m.json, /m2.json"),
                       {("tc", H): "sha256:x"})
    self.assertEqual(seen["stores"], [("bits-store+https://s/store", "STAGING/m", None),
                                      ("b3://b", "STAGING/m", "https://s3.b")])   # every store
    self.assertEqual(seen["trust"], "https://a/m.json,/m2.json")
    self.assertEqual(cp.signed_index("", self.t, ["tc"]), {})            # nothing to ask

  def test_the_package_root_is_the_packages_own(self):
    def tree(*dirs):
      d = tempfile.mkdtemp(dir=self.t)
      for x in dirs:
        os.makedirs(os.path.join(d, x))
        open(os.path.join(d, x, ".meta.json"), "w").close()
      return d
    d = tree("tc/Aaa/1-1", "tc/GCC/14-1", "tc/GCC/14-1/share/bundled/0")
    self.assertEqual(cp._locate_pkgroot(d, "GCC", "14-1"), os.path.join(d, "tc/GCC/14-1"))
    with self.assertRaisesRegex(SystemExit, "several"):        # not the walk's first
      cp._locate_pkgroot(d)
    d = tree("tc/GCC/14-1", "tc/GCC/14-1/share/bundled/0")
    self.assertEqual(cp._locate_pkgroot(d), os.path.join(d, "tc/GCC/14-1"))   # shallowest
    with self.assertRaisesRegex(SystemExit, "none"):
      cp._locate_pkgroot(tree())

  def test_a_relocation_script_from_an_old_bits_is_refused(self):
    script = os.path.join(self.t, "relocate-me.sh")
    with open(script, "w") as f:
      f.write(". ${THISDIR}/etc/profile.d/.bits-pkginfo\n")
    target = "/cvmfs/r/lcg/GCC/14"
    with self.assertRaisesRegex(SystemExit, "GCC@14: .*older than 2026-07-11"):
      cp.refuse_old_relocation(script, "GCC@14", target)
    with open(os.path.join(os.path.dirname(cp.__file__), "relocate-me.sh")) as f:
      current = f.read()
    with open(script, "w") as f:
      f.write(current)
    os.makedirs(os.path.join(self.t, "etc", "profile.d"))
    with open(os.path.join(self.t, "etc", "profile.d", ".bits-pkginfo"), "w") as f:
      f.write('PKG_DIR="/cvmfs/r/lcg"\n')                  # a --cvmfs-prefix build
    cp.refuse_old_relocation(script, "GCC@14", target)        # this bits' own: fine
    with open(script, "w") as f:                              # as between the two dates
      f.write(current.replace("|${PKG_DIR}|g;s|${PKG_DIR}|$INSTALL_BASE|g",
                              "|$INSTALL_BASE|g;s|${PKG_DIR}|$INSTALL_BASE|g"))
    with self.assertRaisesRegex(SystemExit, "older than 2026-10-09"):
      cp.refuse_old_relocation(script, "GCC@14", target)
    cp.refuse_old_relocation(script, "GCC@14", "/cvmfs/other/GCC/14")   # not in its target

  def test_publish_one_reports_an_old_tarball_as_its_failure(self):
    old = os.path.join(self.t, "old")
    os.makedirs(os.path.join(old, "tc", "GCC", "14-1"))
    for name, text in ((".meta.json", "{}"), ("relocate-me.sh", "true\n")):
      with open(os.path.join(old, "tc", "GCC", "14-1", name), "w") as f:
        f.write(text)
    with tarfile.open(self.local, "w:gz") as tf:
      tf.add(old, ".")
    tm = {"prefix": "/cvmfs/r/g", "path": "{prefix}/{pkg}/{version}/{arch}"}
    ctx = {"repo": "r", "tars_root": self.t, "arch": "el9", "tmpl_prefix": "", "templates": tm,
           "prefix_fallback": "/cvmfs/r", "tmp_dir": self.t, "submit": True}
    with mock.patch.object(cp, "tar_path", lambda s, r, a: self.local), \
         mock.patch.object(cp, "published_state", lambda c, p: {"exists": False}), \
         mock.patch.object(cp, "_publish_tar") as sent, \
         self.assertRaisesRegex(SystemExit, "older than 2026-07-11"):
      cp.publish_one(self.spec(checksum_file(self.local)), ctx)
    sent.assert_not_called()

  def test_an_old_manifest_without_the_field(self):
    self.assertEqual(self.certified(None), self.local)

  def test_publish_one_publishes_the_certified_tree(self):
    tm = {"prefix": "/cvmfs/r/g", "path": "{prefix}/{pkg}/{version}/{arch}"}
    ctx = {"repo": "r", "tars_root": self.t, "arch": "el9", "tmpl_prefix": "", "templates": tm,
           "prefix_fallback": "/cvmfs/r", "tmp_dir": self.t, "submit": True, "store": "b3://b"}
    published = []

    def publish_tar(c, p, tar, *a, **k):
      with tarfile.open(tar) as tf:
        published.append(tf.extractfile("./which").read())
        published.append(tf.extractfile("./.build-hash").read())
      return "J"
    with self.store({"b3://b": self.stored}), \
         mock.patch.object(cp, "tar_path", lambda s, r, a: self.local), \
         mock.patch.object(cp, "published_state", lambda c, p: {"exists": False}), \
         mock.patch.object(cp, "_publish_tar", publish_tar):
      cp.publish_one(self.spec(checksum_file(self.stored)), ctx)
    self.assertEqual(published, [b"certified", (H + "\n").encode()])
    left = sorted(f for f in os.listdir(self.t) if not f.endswith(".tar"))   # _publish_tar's own
    self.assertEqual(left, ["TARS", "store-copy"])                            # nothing else left
    with self.store({"b3://b": None}), \
         mock.patch.object(cp, "tar_path", lambda s, r, a: self.local), \
         mock.patch.object(cp, "published_state", lambda c, p: {"exists": False}), \
         self.assertRaisesRegex(SystemExit, "no store"):
      cp.publish_one(self.spec(checksum_file(self.stored)), ctx)
    self.assertEqual(sorted(f for f in os.listdir(self.t) if not f.endswith(".tar")),
                     ["TARS", "store-copy"])                                # refused: none left


if __name__ == "__main__":
  unittest.main()
