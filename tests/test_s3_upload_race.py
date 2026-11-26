# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Two builds of one hash both find no object and both upload; the last one stays.
Each records the sha256 the store holds after its upload, not only its own."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from bits_helpers import sync
from bits_helpers.checksum import tarball_checksum
from bits_helpers.utilities import resolve_store_path

ARCH, H = "el9", "ab" * 20
SPEC = {"package": "zlib", "version": "1", "revision": "1", "hash": H, "redistributable": True}


@patch("bits_helpers.sync.Boto3RemoteSync._s3_init", new=MagicMock())
class UploadRaceTest(unittest.TestCase):
  def upload(self, other):
    work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, work)
    d = os.path.join(work, resolve_store_path(ARCH, H))
    os.makedirs(d)
    with open(os.path.join(d, "zlib-1-1.el9.tar.gz"), "wb") as f:
      f.write(b"ours")
    objects = {}

    def head(Bucket, Key):
      if Key not in objects:
        raise ClientError({"Error": {"Code": "404"}}, "head_object")
      return {"Metadata": {"sha256": objects[Key]}}

    def upload_file(Bucket, Key, Filename, ExtraArgs):
      objects[Key] = other or ExtraArgs["Metadata"]["sha256"]   # *other* overwrote ours

    s = sync.Boto3RemoteSync("b3://w", "b3://w", ARCH, work)
    s.writeStore = "w"
    s.s3 = MagicMock(head_object=head, upload_file=upload_file)
    spec = dict(SPEC)
    with patch.object(s, "write_rev_marker"):
      s.upload_symlinks_and_tarball(spec)
    self.assertEqual(spec.get("store_tarball_by"), "unknown" if other else None)   # not this build's
    return spec["store_tarball_sha256"], tarball_checksum(os.path.join(d, "zlib-1-1.el9.tar.gz"))

  def test_alone(self):
    recorded, ours = self.upload(None)
    self.assertEqual(recorded, ours)

  def test_overwritten_meanwhile(self):
    with self.assertLogs("bits", "WARNING"):
      recorded, ours = self.upload("sha256:" + "f" * 64)
    self.assertEqual(recorded, "sha256:" + "f" * 64)


if __name__ == "__main__":
  unittest.main()
