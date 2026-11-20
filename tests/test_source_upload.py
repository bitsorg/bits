# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits store add-source: an archive put in the sources store by hand, under the URL
its recipe names, checked against the recipe's checksum first."""

import hashlib
import os
import unittest

from bits_helpers.download import getUrlChecksum
from bits_helpers.source_upload import SourceUploadError, add_source

from test_bits_store_sync import StandIn, Store

URL = "https://download.example.org/jdk/jdk-21_linux-x64_bin.tar.gz"
DATA = b"an archive behind a licence page"


class AddSourceTest(StandIn):
  def setUp(self):
    super().setUp()
    self.base = "http://127.0.0.1:%d/sources/archives" % self.server.server_address[1]
    self.file = os.path.join(self.tmp.name, "downloaded.tar.gz")
    with open(self.file, "wb") as f:
      f.write(DATA)
    h = getUrlChecksum(URL)
    self.key = "sources/archives/%s/%s/jdk-21_linux-x64_bin.tar.gz" % (h[:2], h)

  def add(self, entry=URL, **kw):
    return add_source(entry, self.file, store=self.base, **kw)

  def test_under_the_url_the_recipe_names(self):
    where, stored = self.add(URL + ",sha256:" + hashlib.sha256(DATA).hexdigest())
    self.assertTrue(stored)
    self.assertEqual(where, "%s/%s" % (self.base.rsplit("/sources/archives", 1)[0], self.key))
    self.assertEqual(Store.markers[self.key], DATA.decode())
    posts = [p for m, p, a in Store.asked if m == "POST"]
    self.assertTrue(posts[0].startswith("/" + self.key + "?uploads") and all(
        a == "Bearer good" for m, p, a in Store.asked if m == "POST"), Store.asked)
    # the first upload stays
    self.assertEqual(self.add(), (where, False))

  def test_a_file_not_the_recipes(self):
    with self.assertRaisesRegex(SourceUploadError, "does not match"):
      self.add(URL + ",sha256:" + "0" * 64)
    self.assertEqual([m for m, *_ in Store.asked], [])   # nothing asked of the store

  def test_urls_the_store_cannot_hold(self):
    for bad in ("ftp://example.org/a.tar.gz", "https://u:p@example.org/a.tar.gz",
                "https://example.org/dl?file=a.tar.gz", "https://example.org/"):
      with self.assertRaises(SourceUploadError, msg=bad):
        self.add(bad)


if __name__ == "__main__":
  unittest.main()
