# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""An HTTP store that does not answer is not taken for one that has nothing: bits
says the tarball or source is not reused from it (it was silently rebuilt)."""

import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from requests.exceptions import ConnectionError as Down

from bits_helpers import sync

ARCH, H = "el9", "ab" * 20
SPEC = {"package": "zlib", "version": "1", "revision": "1", "hash": H, "remote_hashes": [H]}


class Absent:
  status_code = 404


@patch("bits_helpers.sync.ProgressPrint", new=MagicMock())
class UnansweredTest(unittest.TestCase):
  def store(self, answer):
    work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, work)
    s = sync.HttpRemoteSync("https://store.example", ARCH, work, False)
    s.httpBackoff = 0
    return s, patch("requests.Session.get", side_effect=answer), patch("requests.get", side_effect=answer)

  def run_both(self, answer, hashes=(H,)):
    s, a, b = self.store(answer)
    with a, b, patch("bits_helpers.sync.warning") as warned, patch("bits_helpers.sync.error"):
      s.fetch_tarball(dict(SPEC, remote_hashes=list(hashes)))
      s.fetch_source("cafe" * 10, "zlib.tar.gz", s.workdir + "/SOURCES/x")
    return [c.args[1:] for c in warned.call_args_list]

  def test_down_is_said_once(self):
    self.assertEqual(self.run_both(Down("refused")), [("https://store.example", "the tarball of zlib")])

  def test_absent_is_not(self):
    self.assertEqual(self.run_both(lambda *a, **kw: Absent()), [])

  def test_a_listing_that_failed_before_a_good_download_is_not_said(self):
    other = "cd" * 20

    def answer(url, *a, **kw):
      if H in url:
        raise Down("refused")
      if url.endswith("/"):
        return Listing([{"name": "zlib-1-1.el9.tar.gz"}])
      if other in url and "zlib-1-1" in url:
        return Listing(None, b"tarball")
      return Absent()
    said = self.run_both(answer, hashes=(H, other))
    self.assertEqual(said, [])          # the other hash was fetched


class Listing:
  status_code, headers = 200, {}

  def __init__(self, listing, data=b""):
    self.listing, self.data = listing, data
    self.headers = {"content-length": str(len(data))}

  def raise_for_status(self):
    pass

  def json(self):
    return self.listing

  def iter_content(self, chunk_size):
    yield self.data


if __name__ == "__main__":
  unittest.main()
