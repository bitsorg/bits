# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The bits-store package store (bits_helpers.bits_store_sync) against a local
stand-in for bits-store's tarballs format: a build by its hash, never a listing."""

import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from bits_helpers import plan, source_store, sync
from bits_helpers.bits_store_sync import BitsStoreSync

ARCH = "x86_64-el9"
H1, H2 = "1" * 40, "2" * 40
NAME = "zlib-v1.3-1.%s.tar.gz" % ARCH
DATA = b"a build"


class Store(BaseHTTPRequestHandler):
  """/store/<arch>/<hash>: 302 to /bucket/<hash> with the name, 404 if absent; a
  token other than "good" 401; /bucket/<hash> the bytes, refusing any token."""
  builds, public, asked, cut, failing, bare = {}, False, [], False, 0, False

  def log_message(self, *a):
    pass

  def _route(self, body):
    auth = self.headers.get("Authorization")
    Store.asked.append((self.command, self.path, auth))
    if self.path.startswith("/bucket/"):
      build = self.builds.get(self.path[len("/bucket/"):])
      if auth or build is None:
        return self._answer(400 if auth else 404)
      self.send_response(200)
      self.send_header("Content-Length", str(len(build[1]) + (5 if self.cut else 0)))
      self.end_headers()
      if body:
        self.wfile.write(build[1])
      return
    if Store.failing > 0:
      Store.failing -= 1
      return self._answer(503)
    if auth == "Bearer refused":
      return self._answer(403)                 # taken, but not for this path
    if auth not in (None, "Bearer good") or auth is None and not self.public:
      return self._answer(401)
    arch, _, h = self.path[len("/store/"):].partition("/")
    build = self.builds.get(h) if arch == ARCH else None
    if build is None:
      return self._answer(404)
    self.send_response(302)
    if not self.bare:
      self.send_header("Location", "/bucket/" + h)
      self.send_header("Bits-Object-Name", build[0])
    self.send_header("Content-Length", "0")
    self.end_headers()

  def do_GET(self):
    self._route(True)

  def do_HEAD(self):
    self._route(False)

  def _answer(self, code):
    self.send_response(code)
    self.send_header("Content-Length", "0")
    self.end_headers()


class BitsStoreSyncTest(unittest.TestCase):
  def setUp(self):
    Store.builds, Store.public, Store.asked, Store.cut, Store.failing, Store.bare = {H2: (NAME, DATA)}, False, [], False, 0, False
    p = patch("bits_helpers.bits_store_sync.time.sleep")
    p.start()
    self.addCleanup(p.stop)
    self.server = ThreadingHTTPServer(("127.0.0.1", 0), Store)
    threading.Thread(target=self.server.serve_forever, daemon=True).start()
    self.addCleanup(self.server.shutdown)
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.url = "bits-store+http://127.0.0.1:%d/store" % self.server.server_address[1]
    self.token = os.path.join(self.tmp.name, "token")
    with open(self.token, "w") as f:
      f.write("good\n")
    p = patch.object(source_store, "TOKEN_FILE", self.token)
    p.start()
    self.addCleanup(p.stop)
    self.work = os.path.join(self.tmp.name, "sw")

  def store(self):
    return BitsStoreSync(self.url, ARCH, self.work)

  def spec(self, *hashes):
    return {"package": "zlib", "version": "v1.3", "remote_hashes": list(hashes)}

  def stored(self, h=H2):
    path = os.path.join(self.work, "TARS", ARCH, "store", h[:2], h, NAME)
    if not os.path.isfile(path):
      return None
    with open(path, "rb") as f:
      return f.read()

  def test_a_build_by_its_hash(self):
    self.assertEqual(self.store().list_store_tarballs(ARCH, H2), [NAME])
    self.assertEqual(self.store().list_store_tarballs(ARCH, H1), [])
    self.store().fetch_tarball(self.spec(H1, H2))       # the first hash is not stored
    self.assertEqual(self.stored(), DATA)
    self.assertTrue(all(a == "Bearer good" for _, p, a in Store.asked if p.startswith("/store/")))
    self.assertTrue(all(a is None for _, p, a in Store.asked if p.startswith("/bucket/")))
    Store.asked = []
    self.store().fetch_tarball(self.spec(H2))          # here already: no request
    self.assertEqual(Store.asked, [])

  def test_names_that_are_not_the_build_are_refused(self):
    for name in ("../%s" % NAME, "zlib.x86_64-el8.tar.gz", "", "a b.%s.tar.gz" % ARCH,
                 "other-v1.3-1.%s.tar.gz" % ARCH):
      Store.builds = {H2: (name, DATA)}
      if name != "other-v1.3-1.%s.tar.gz" % ARCH:   # another package's: the caller's check
        self.assertEqual(self.store().list_store_tarballs(ARCH, H2), [], name)
      self.store().fetch_tarball(self.spec(H2))
      self.assertFalse(os.path.isdir(os.path.join(self.work, "TARS", ARCH, "store", "22", H2))
                       and os.listdir(os.path.join(self.work, "TARS", ARCH, "store", "22", H2)), name)

  def test_a_token_not_taken_reads_as_anyone(self):
    with open(self.token, "w") as f:
      f.write("other site's")
    Store.public = True
    self.assertEqual(self.store().list_store_tarballs(ARCH, H2), [NAME])
    Store.public = False
    self.assertEqual(self.store().list_store_tarballs(ARCH, H2), [])

  def test_a_cut_download_leaves_nothing(self):
    Store.cut = True
    self.store().fetch_tarball(self.spec(H2))
    self.assertIsNone(self.stored())
    directory = os.path.join(self.work, "TARS", ARCH, "store", "22", H2)
    self.assertEqual(os.listdir(directory), [])

  def test_a_short_body_is_a_miss_even_unchecked(self):
    short = MagicMock(status_code=200, headers={"Content-Length": "100"})
    short.raw.stream.return_value = [b"0123456789"]
    short.__enter__.return_value = short
    store = self.store()
    store._local.session = MagicMock()
    store._local.session.get.return_value = short
    d = os.path.join(self.work, "d")
    with patch("bits_helpers.bits_store_sync.warning") as warned:
      store._download("http://x/y", d, NAME, self.spec(H2))
    self.assertEqual(os.listdir(d), [])
    warned.assert_called_once()

  def test_failures_are_tried_again(self):
    Store.failing = 2
    self.assertEqual(self.store().list_store_tarballs(ARCH, H2), [NAME])
    Store.failing = 10
    self.assertEqual(self.store().list_store_tarballs(ARCH, H2), [])

  def test_a_redirect_without_its_name_is_a_miss(self):
    Store.bare = True
    self.assertEqual(self.store().list_store_tarballs(ARCH, H2), [])

  def test_a_refused_token_is_said_once_and_dropped(self):
    with open(self.token, "w") as f:
      f.write("refused")
    Store.public = True
    store = self.store()
    with patch("bits_helpers.bits_store_sync.warning") as warned:
      self.assertEqual(store.list_store_tarballs(ARCH, H2), [NAME])
      self.assertEqual(store.list_store_tarballs(ARCH, H2), [NAME])
    warned.assert_called_once()
    self.assertEqual([a for _, _, a in Store.asked], ["Bearer refused", None, None])

  def test_the_next_hash_when_a_name_is_another_build(self):
    Store.builds = {H1: ("other-v1.3-1.%s.tar.gz" % ARCH, b"other"), H2: (NAME, DATA)}
    self.store().fetch_tarball(self.spec(H1, H2))
    self.assertEqual(self.stored(), DATA)

  def test_with_another_store_after_it(self):
    other = tempfile.mkdtemp(dir=self.tmp.name)
    helper = sync.remote_from_url(self.url + ",rsync://" + other, "", ARCH, self.work)
    self.assertIsInstance(helper, sync.MultiRemoteSync)
    self.assertEqual(helper.list_store_tarballs(ARCH, H2), [NAME])

  def test_signed_manifests_are_asked_of_the_service(self):
    from bits_helpers.build import _store_container_root
    self.assertEqual(_store_container_root("bits-store+https://store.example.org/store"),
                     "https://store.example.org")
    self.assertIsNone(_store_container_root("bits-store+ftp://x/store"))

  def test_an_unreachable_store_is_a_miss(self):
    s = BitsStoreSync("bits-store+http://127.0.0.1:9/store", ARCH, self.work, timeout=(1, 1))
    self.assertEqual(s.list_store_tarballs(ARCH, H2), [])
    s.fetch_tarball(self.spec(H2))
    self.assertIsNone(self.stored())

  def test_store_urls(self):
    helper = sync.remote_from_url(self.url, "", ARCH, self.work)
    self.assertIsInstance(helper, BitsStoreSync)
    self.assertTrue(plan.store_can_list(helper))
    with tempfile.TemporaryDirectory() as w:
      dual = sync.remote_from_url(self.url, "rsync://example.org/store", ARCH, w)
    self.assertIsInstance(dual, sync.DualRemoteSync)
    self.assertIsInstance(dual.readers[0], BitsStoreSync)
    with self.assertRaises(SystemExit):
      sync.remote_from_url(self.url, self.url, ARCH, self.work)


if __name__ == "__main__":
  unittest.main()
