# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""With a write store a counter revision is reserved in the store's markers when it
is picked, before the build: a build of another hash meanwhile gets the next number."""

import unittest
from unittest.mock import MagicMock, patch

from bits_helpers import rev_index, sync
from bits_helpers.build import reserve_revision

ARCH = "el9_x86-64"
MINE, OTHER = "a" * 40, "b" * 40


class Markers:
  """A store's markers: {revision: hash}, write-once; *taken* writes beside us."""
  def __init__(self, taken=None, fail=False):
    self.held, self.fail, self.asked = dict(taken or {}), fail, []

  def claim_rev_marker(self, package, version, revision, arch, pkg_hash):
    self.asked.append(revision)
    if self.fail:
      raise OSError("store down")
    return self.held.setdefault(revision, pkg_hash)


def spec(rev="3"):
  return {"package": "zlib", "version": "v1.3", "revision": rev, "remote_revision_hash": MINE}


class ReserveRevisionTest(unittest.TestCase):
  def test_a_free_number_is_kept(self):
    store = Markers()
    self.assertEqual(reserve_revision(spec(), ARCH, store, {1, 2}), "3")
    self.assertEqual(store.held, {"3": MINE})

  def test_one_taken_meanwhile_is_skipped(self):
    store, busy = Markers({"3": OTHER, "4": OTHER}), {1, 2}
    self.assertEqual(reserve_revision(spec(), ARCH, store, busy), "5")
    self.assertEqual((store.asked, busy), (["3", "4", "5"], {1, 2, 3, 4}))

  def test_ours_already_is_kept(self):                        # a failed build retried
    self.assertEqual(reserve_revision(spec(), ARCH, Markers({"3": MINE}), set()), "3")

  def test_a_store_without_markers_or_down_keeps_it(self):
    self.assertEqual(reserve_revision(spec(), ARCH, sync.NoRemoteSync(), set()), "3")
    with self.assertLogs("bits", "WARNING"):
      self.assertEqual(reserve_revision(spec(), ARCH, Markers(fail=True), set()), "3")

  def test_several_write_stores_must_all_agree(self):
    multi = sync.MultiRemoteSync([], [Markers(), Markers({"3": OTHER})])
    self.assertEqual(reserve_revision(spec(), ARCH, multi, {1, 2}), "4")


@patch("bits_helpers.sync.Boto3RemoteSync._s3_init", new=MagicMock())
class Boto3ClaimTest(unittest.TestCase):
  def store(self, objects, racer=None, conditional=True):
    from botocore.exceptions import ClientError

    def head(Bucket, Key):
      if Key not in objects:
        raise ClientError({"Error": {"Code": "404"}}, "head_object")

    def put(Bucket, Key, Body, IfNoneMatch=None):
      if racer is not None:                      # another build wrote it since our HEAD
        objects.setdefault(Key, racer)
      if IfNoneMatch and not conditional:
        raise ClientError({"Error": {"Code": "NotImplemented"}}, "put_object")
      if IfNoneMatch and Key in objects:
        raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "put_object")
      objects[Key] = Body

    s = sync.Boto3RemoteSync("b3://r", "b3://w", ARCH, "/tmp/x")
    s.writeStore = "w"
    s.s3 = MagicMock(head_object=head, put_object=put,
                     get_object=lambda Bucket, Key: {"Body": MagicMock(read=lambda: objects[Key])})
    return s

  def test_claims(self):
    key = rev_index.marker_key(ARCH, "zlib", "v1.3", "3")
    objects = {}
    self.assertEqual(self.store(objects).claim_rev_marker("zlib", "v1.3", "3", ARCH, MINE), MINE)
    self.assertEqual(objects[key], MINE.encode())
    self.assertEqual(self.store(objects).claim_rev_marker("zlib", "v1.3", "3", ARCH, OTHER), MINE)
    raced = {}                                  # write-once: the other build keeps it
    self.assertEqual(self.store(raced, racer=OTHER.encode()).claim_rev_marker(
      "zlib", "v1.3", "3", ARCH, MINE), OTHER)
    plain = {}                                  # no write-once: written, and read back
    self.assertEqual(self.store(plain, conditional=False).claim_rev_marker(
      "zlib", "v1.3", "3", ARCH, MINE), MINE)

  def test_giving_up_keeps_the_first_number(self):
    taken = Markers({str(n): OTHER for n in range(3, 40)})
    with self.assertLogs("bits", "WARNING"):
      self.assertEqual(reserve_revision(spec(), ARCH, taken, {1, 2}, attempts=3), "3")


if __name__ == "__main__":
  unittest.main()
