# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""fetch_tarball downloads the object named as the package's tarball, not whatever
the store lists first under the hash (HTTP and boto3 backends)."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bits_helpers import sync

ARCH = "el9_x86-64"
HASH = "ab" * 20
SPEC = {"package": "zlib", "version": "v1.3.1", "revision": "2", "hash": HASH,
        "remote_hashes": [HASH]}
TARBALL = "zlib-v1.3.1-2.%s.tar.gz" % ARCH
LISTED = ["README", "other-v1.3.1-2.%s.tar.gz" % ARCH, TARBALL]   # the tarball is not first


@patch("bits_helpers.sync.ProgressPrint", new=MagicMock())
class FetchTarballNameTest(unittest.TestCase):
  def setUp(self):
    self.work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.work)

  def test_http(self):
    store = sync.HttpRemoteSync("https://store.example", ARCH, self.work, False)
    got = []

    def get(url, dest=None, **kw):
      if dest is None:
        return [{"name": n} for n in LISTED] if "/store/" in url else None
      got.append(url)
      return None
    with patch.object(store, "getRetry", side_effect=get):
      store.fetch_tarball(dict(SPEC))
    self.assertEqual([u.rsplit("/", 1)[1] for u in got], [TARBALL])

  def test_boto3(self):
    store = sync.Boto3RemoteSync.__new__(sync.Boto3RemoteSync)
    store.remoteStore, store.architecture, store.workdir = "bucket", ARCH, self.work
    store.s3 = MagicMock()
    store.s3.head_object.return_value = {"ContentLength": 1}
    prefix = "TARS/%s/store/ab/%s/" % (ARCH, HASH)
    with patch.object(store, "_s3_listdir", return_value=[prefix + n for n in LISTED]):
      store.fetch_tarball(dict(SPEC))
    keys = [c.kwargs["Key"] for c in store.s3.download_file.call_args_list]
    self.assertEqual(keys, [prefix + TARBALL])

  def test_names(self):
    want = sync.tarball_name_regex
    spec = dict(SPEC, revision="dev", force_revision="dev")      # a fixed label
    for name, ok in (("zlib-v1.3.1-2.el9_x86-64.tar.gz", True), ("zlib-v1.3.1.el9_x86-64.tar.gz", True),
                     ("zlib-v1.3.1-local3.el9_x86-64.tar.gz", True), ("zlib-v1.3.1-" + HASH + ".el9_x86-64.tar.gz", True),
                     ("zlib-v1.3.1-dev.el9_x86-64.tar.gz", True), ("zlib-v1.3.1-rc1.el9_x86-64.tar.gz", False),
                     ("zlib-v1.3.1-2.el9_x86-64.tar.gz.sha256", False), ("zlibx-v1.3.1-2.el9_x86-64.tar.gz", False)):
      self.assertEqual(bool(want(spec, ARCH).match(name)), ok, name)
    self.assertTrue(want({"package": "zlib", "version": "v1.3.1", "force_revision": "rc1"}, ARCH)
                    .match("zlib-v1.3.1-rc1.el9_x86-64.tar.gz"))                  # in a prefetch


if __name__ == "__main__":
  unittest.main()
