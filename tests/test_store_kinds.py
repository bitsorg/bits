# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A read store and a write store of different kinds get one helper each: an rsync
or s3:// (s3cmd) read store with a b3:// write store handed the b3 URL to the
rsync or s3cmd backend as its write store."""

import unittest
from unittest.mock import MagicMock, patch

from bits_helpers import sync


@patch("bits_helpers.sync.Boto3RemoteSync._s3_init", new=MagicMock())
@patch("bits_helpers.sync.resolve_and_export_s3_config", new=MagicMock())
class StoreKindsTest(unittest.TestCase):
  def kinds(self, read, write):
    h = sync.remote_from_url(read, write, "el9", "/tmp/sw")
    if isinstance(h, sync.MultiRemoteSync):
      return [type(r).__name__ for r in h.readers], [type(w).__name__ for w in h.writers]
    return type(h).__name__

  def test_mixed_kinds_are_split(self):
    for read, reader in (("rsync://host/store", "RsyncRemoteSync"), ("/srv/store", "RsyncRemoteSync"),
                         ("s3://bucket", "S3RemoteSync")):
      self.assertEqual(self.kinds(read, "b3://w"), ([reader], ["Boto3RemoteSync"]), read)

  def test_one_kind_stays_one_helper(self):
    self.assertEqual(self.kinds("s3://r", "s3://w"), "S3RemoteSync")       # s3cmd reads one, writes the other
    self.assertEqual(self.kinds("/srv/a", "/srv/b"), "RsyncRemoteSync")
    self.assertEqual(self.kinds("b3://r", "b3://r"), "Boto3RemoteSync")


if __name__ == "__main__":
  unittest.main()
