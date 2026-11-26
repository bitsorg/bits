# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Stamping a legacy object's sha256 (a metadata self-copy, REPLACE) keeps the rest
of its metadata: bits-by, which says who uploaded it, and its content type."""

import hashlib
import unittest
from unittest.mock import MagicMock, patch

from bits_helpers import sync


@patch("bits_helpers.sync.Boto3RemoteSync._s3_init", new=MagicMock())
class StampTest(unittest.TestCase):
  def test_other_metadata_is_kept(self):
    s = sync.Boto3RemoteSync("b3://r", "b3://w", "el9", "/tmp/x")
    s.writeStore = "w"
    data = [b"stored", b""]
    s.s3 = MagicMock(head_object=lambda Bucket, Key: {"Metadata": {"bits-by": "alice", "Sha256": ""},
                                                       "ContentType": "application/gzip"},
                     get_object=lambda Bucket, Key: {"Body": MagicMock(read=lambda n: data.pop(0))})
    sha = s._s3_remote_tarball_sha256("TARS/el9/store/ab/abab/x.tar.gz")
    self.assertEqual(sha, "sha256:" + hashlib.sha256(b"stored").hexdigest())
    extra = s.s3.copy.call_args.kwargs["ExtraArgs"]
    self.assertEqual(extra, {"Metadata": {"bits-by": "alice", "sha256": sha},
                             "MetadataDirective": "REPLACE", "ContentType": "application/gzip"})


if __name__ == "__main__":
  unittest.main()
