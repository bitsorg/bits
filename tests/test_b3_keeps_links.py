# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A b3:// read store's fetch_symlinks keeps this work dir's version links, as the
HTTP backend does. It removed them all, and a hash-only store gives none back: the
revision scan then found no local revision, and a second local variant of a version
took local1 again, replacing the first one's install."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from bits_helpers import sync

ARCH, H1, H2 = "el9_x86-64", "a" * 40, "b" * 40


@patch("bits_helpers.sync.Boto3RemoteSync._s3_init", new=MagicMock())
class KeepsLinksTest(unittest.TestCase):
  def test_local_links_stay_and_the_stores_are_added(self):
    work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, work)
    links = os.path.join(work, "TARS", ARCH, "zlib")
    os.makedirs(links)
    for h, rev in ((H1, "local1"), (H2, "local2")):
      os.symlink("../../%s/store/%s/%s/zlib-1.0-%s.%s.tar.gz" % (ARCH, h[:2], h, rev, ARCH),
                 os.path.join(links, "zlib-1.0-%s.%s.tar.gz" % (rev, ARCH)))
    store = sync.Boto3RemoteSync("b3://bucket", "", ARCH, work)
    legacy = b"zlib-1.0-1.%s.tar.gz\t../../%s/store/cc/%s/zlib-1.0-1.%s.tar.gz\n" % (
      ARCH.encode(), ARCH.encode(), b"c" * 40, ARCH.encode())

    def get_object(Bucket, Key):
      if Key.endswith(".manifest"):
        return {"Body": MagicMock(iter_lines=lambda: [legacy])}
      raise ClientError({"Error": {"Code": "NoSuchKey"}}, "get_object")
    store.s3 = MagicMock(get_object=get_object,
                         get_paginator=lambda m: MagicMock(paginate=lambda **kw: [{}]))
    store.fetch_symlinks({"package": "zlib", "version": "1.0", "hash": H1, "remote_hashes": [H1]})
    self.assertEqual(sorted(os.listdir(links)), ["zlib-1.0-%s.%s.tar.gz" % (r, ARCH)
                                                 for r in ("1", "local1", "local2")])


if __name__ == "__main__":
  unittest.main()
