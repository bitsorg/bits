# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The sources store client (bits_helpers.source_store) against a local stand-in
for bits-store: a token may ask for a fetch, anyone may read."""

import hashlib
import os
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from bits_helpers import source_store, sync
from bits_helpers.download import download

SRC = "https://example.org/dl/pkg+1.0.tar.gz"
H = hashlib.md5(SRC.encode()).hexdigest()
FILE = "pkg+1.0.tar.gz"
DATA = b"source archive bytes"


class Store(BaseHTTPRequestHandler):
  """GET /s/<h2>/<h>/<file>[?url=]: 202 `waits` times for a token's fetch, then 302;
  a bad token 401; anyone a 302 to /bucket/..., which has the object once fetched."""
  objects, asked, waits, cut, status, gzip = {}, [], 0, False, None, False

  def log_message(self, *a):
    pass

  def do_GET(self):
    u = urllib.parse.urlsplit(self.path)
    q = urllib.parse.parse_qs(u.query)
    auth = self.headers.get("Authorization")
    if u.path.startswith("/bucket/"):
      if auth:
        return self._answer(400)                  # a presigned URL takes no token
      body = self.objects.get(u.path[len("/bucket/"):])
      if body is None:
        return self._answer(404)
      self.send_response(200)
      self.send_header("Content-Length", str(len(body) + (10 if self.cut else 0)))
      if self.gzip:
        self.send_header("Content-Encoding", "gzip")   # a bucket labelling a .tar.gz
      self.end_headers()
      self.wfile.write(body)
      return
    key = u.path[len("/s/"):]
    Store.asked.append((auth, q.get("url")))
    if Store.status:
      return self._answer(Store.status)
    if auth not in (None, "Bearer good"):
      return self._answer(401)
    if auth and q.get("url") and key not in self.objects:
      if Store.waits > 0:
        Store.waits -= 1
        self.send_response(202)
        self.send_header("Retry-After", "0")
        self.end_headers()
        return
      self.objects[key] = DATA
    self.send_response(302)
    self.send_header("Location", "/bucket/" + key)
    self.end_headers()

  def _answer(self, code):
    self.send_response(code)
    self.send_header("Content-Length", "0")
    self.end_headers()


class SourceStoreTest(unittest.TestCase):
  def setUp(self):
    Store.objects, Store.asked, Store.waits, Store.cut, Store.status, Store.gzip = {}, [], 0, False, None, False
    self.server = ThreadingHTTPServer(("127.0.0.1", 0), Store)
    threading.Thread(target=self.server.serve_forever, daemon=True).start()
    self.addCleanup(self.server.shutdown)
    self.tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self.tmp.cleanup)
    self.token = os.path.join(self.tmp.name, "token")
    self.dest = os.path.join(self.tmp.name, "cache")
    self.base = "http://127.0.0.1:%d/s" % self.server.server_address[1]

  def store(self, token=None, wait=5):
    if token:
      with open(self.token, "w") as f:
        f.write(token + "\n")
    return source_store.SourceStore(self.base, token_file=self.token, wait=wait)

  def fetched(self):
    with open(os.path.join(self.dest, FILE), "rb") as f:
      return f.read()

  @patch("bits_helpers.source_store.time.sleep")
  def test_a_token_asks_for_the_fetch_and_waits(self, sleep):
    Store.waits = 2
    self.assertTrue(self.store("good").fetch(H, FILE, self.dest, SRC))
    self.assertEqual(sleep.call_count, 2)
    self.assertEqual(self.fetched(), DATA)
    self.assertEqual(Store.asked, [("Bearer good", [SRC])] * 3)   # '+' kept, not a space

  def test_without_a_token_a_miss_is_left_to_the_url(self):
    self.assertFalse(self.store().fetch(H, FILE, self.dest, SRC))
    self.assertEqual(Store.asked, [(None, None)])
    self.assertFalse(os.listdir(self.dest) if os.path.isdir(self.dest) else [])
    Store.objects[H[:2] + "/" + H + "/" + FILE] = DATA
    self.assertTrue(self.store().fetch(H, FILE, self.dest, SRC))
    self.assertEqual(self.fetched(), DATA)

  def test_a_token_not_taken_reads_as_anyone(self):
    Store.objects[H[:2] + "/" + H + "/" + FILE] = DATA
    self.assertTrue(self.store("other site's").fetch(H, FILE, self.dest, SRC))
    self.assertEqual(Store.asked, [("Bearer other site's", [SRC]), (None, None)])

  def test_a_fetch_too_slow_is_left_to_the_url(self):
    Store.waits = 1000
    self.assertFalse(self.store("good", wait=0).fetch(H, FILE, self.dest, SRC))

  def test_no_url_for_other_schemes_or_none(self):
    for src in (None, "git+https://example.org/pkg.git"):
      Store.asked = []
      self.store("good").fetch(H, FILE, self.dest, src)
      self.assertEqual(Store.asked, [("Bearer good", None)])

  def test_a_cut_download_leaves_nothing(self):
    Store.objects[H[:2] + "/" + H + "/" + FILE] = DATA
    Store.cut = True
    self.assertFalse(self.store().fetch(H, FILE, self.dest, SRC))
    self.assertEqual(os.listdir(self.dest), [])

  def test_other_answers_are_a_miss(self):
    for code in (400, 404, 429, 500, 502, 503):
      Store.status = code
      self.assertFalse(self.store("good").fetch(H, FILE, self.dest, SRC), code)

  def test_names_the_store_cannot_hold_are_not_asked(self):
    for name in ("a b.tar.gz", "a%20b.tar.gz", "x.tar.gz?v=1", "x#y", "é.tar.gz", "..", "a" * 256, "f.tgz\n"):
      self.assertFalse(self.store("good").fetch(H, name, self.dest, SRC), name)
    self.assertEqual(Store.asked, [])

  def test_no_url_with_credentials(self):
    self.store("good").fetch(H, FILE, self.dest, "https://user:secret@example.org/" + FILE)
    self.assertEqual(Store.asked, [("Bearer good", None)])

  def test_no_token_over_plain_http_to_another_host(self):
    with open(self.token, "w") as f:
      f.write("good")
    s = source_store.SourceStore("http://store.example.org/s", token_file=self.token)
    self.assertIsNone(s._token())
    self.assertEqual(source_store.SourceStore("https://store.example.org/s", token_file=self.token)._token(), "good")

  @patch("bits_helpers.source_store.time.sleep")
  def test_retry_after_is_clamped(self, sleep):
    for value, want in (("0", 1), ("600", 60), ("Wed, 21 Oct 2026 07:28:00 GMT", 5)):
      Store.waits, Store.objects = 1, {}
      with patch.object(Store, "send_header", _retry_after(value)):
        self.store("good").fetch(H, FILE, self.dest, SRC)
      sleep.assert_called_with(want)

  def test_two_downloads_at_once_do_not_collide(self):
    Store.objects[H[:2] + "/" + H + "/" + FILE] = DATA
    results = []
    threads = [threading.Thread(target=lambda: results.append(self.store().fetch(H, FILE, self.dest, SRC)))
               for _ in range(4)]
    for t in threads:
      t.start()
    for t in threads:
      t.join()
    self.assertEqual(results, [True] * 4)
    self.assertEqual(os.listdir(self.dest), [FILE])
    self.assertEqual(self.fetched(), DATA)

  def test_an_unwritable_cache_is_a_miss(self):
    Store.objects[H[:2] + "/" + H + "/" + FILE] = DATA
    with open(self.dest, "w"):
      pass                                  # a file where the directory should be
    self.assertFalse(self.store().fetch(H, FILE, self.dest, SRC))

  def test_without_cryptography(self):
    import subprocess, sys
    code = ("import sys; sys.modules['cryptography'] = None\n"
            "from bits_helpers import sync\n"
            "print(type(sync.source_sync_for({}, None)).__name__)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=dict(os.environ, BITS_SOURCE_STORE=self.base))
    self.assertEqual(out.stdout.strip(), "SourceStoreSync", out.stderr)

  def test_a_short_body_is_a_miss_even_unchecked(self):
    # urllib3 1.x ends a cut body without an error: the length is checked here
    short = MagicMock(status_code=200, headers={"Content-Length": "100"})
    short.raw.stream.return_value = [b"0123456789"]
    short.__enter__.return_value = short
    with patch("bits_helpers.source_store.requests.get", return_value=short):
      self.assertFalse(self.store()._download("http://x/y", self.dest, FILE))
    self.assertEqual(os.listdir(self.dest), [])

  def test_the_bytes_as_stored_even_if_labelled_gzip(self):
    import gzip
    archive = gzip.compress(DATA)
    Store.objects[H[:2] + "/" + H + "/" + FILE] = archive
    Store.gzip = True
    self.assertTrue(self.store().fetch(H, FILE, self.dest, SRC))
    self.assertEqual(self.fetched(), archive)

  def test_an_unreachable_store_is_a_miss(self):
    s = source_store.SourceStore("http://127.0.0.1:9/s", token_file=self.token, timeout=(1, 1))
    self.assertFalse(s.fetch(H, FILE, self.dest, SRC))


def _retry_after(value):
  real = BaseHTTPRequestHandler.send_header
  def send_header(self, name, v):
    real(self, name, value if name == "Retry-After" else v)
  return send_header


class SourceSyncTest(unittest.TestCase):
  def setUp(self):
    self.store = MagicMock()
    patcher = patch("bits_helpers.source_store.configured", return_value=self.store)
    patcher.start()
    self.addCleanup(patcher.stop)

  def test_the_store_is_asked_first_with_the_url_if_redistributable(self):
    inner = MagicMock()
    helper = sync.source_sync_for({"redistributable": "all"}, inner)
    self.store.fetch.return_value = True
    self.assertTrue(helper.fetch_source_from(H, FILE, "/d", SRC))
    self.store.fetch.assert_called_once_with(H, FILE, "/d", SRC)
    inner.fetch_source_from.assert_not_called()
    self.store.fetch.return_value = False
    helper.fetch_source_from(H, FILE, "/d", SRC)
    inner.fetch_source_from.assert_called_once_with(H, FILE, "/d", SRC)

  def test_sources_not_redistributable_are_read_never_fetched(self):
    helper = sync.source_sync_for({"redistributable": "binaries"}, MagicMock())
    self.store.fetch.return_value = False
    helper.fetch_source_from(H, FILE, "/d", SRC)
    self.store.fetch.assert_called_once_with(H, FILE, "/d", None)

  def test_without_a_remote_store_the_sources_store_is_still_asked(self):
    helper = sync.source_sync_for({"redistributable": "all"}, None)
    self.store.fetch.return_value = False
    self.assertFalse(helper.fetch_source_from(H, FILE, "/d", SRC))
    self.store.fetch.assert_called_once()

  def test_other_files_do_not_ask_it(self):
    inner = MagicMock()
    sync.source_sync_for({"redistributable": "all"}, inner).fetch_source("k", "index", "/d")
    self.store.fetch.assert_not_called()      # the fetch cache's files are not sources

  def test_a_forbidden_recipe_sends_no_url_through_download(self):
    self.store.fetch.return_value = False
    upstream = MagicMock(side_effect=lambda src, d, w: open(os.path.join(d, FILE), "wb").write(DATA))
    with tempfile.TemporaryDirectory() as tmp, \
         patch.dict("bits_helpers.download.downloadHandlers", {"https": upstream}):
      download(SRC, None, tmp, sync_helper=sync.source_sync_for({"redistributable": "none"}, None))
    self.assertIsNone(self.store.fetch.call_args[0][3])
    upstream.assert_called_once()

  def test_download_gets_the_archive_from_the_store(self):
    def fetch(h, f, d, src):
      with open(os.path.join(d, f), "wb") as fh:
        fh.write(DATA)
      return True
    self.store.fetch.side_effect = fetch
    upstream = MagicMock()
    with tempfile.TemporaryDirectory() as tmp, \
         patch.dict("bits_helpers.download.downloadHandlers", {"https": upstream}):
      path = download(SRC, None, tmp, sync_helper=sync.source_sync_for({"redistributable": "all"}, None))
      with open(path, "rb") as f:
        self.assertEqual(f.read(), DATA)
    upstream.assert_not_called()
    self.store.fetch.assert_called_once()
    self.assertEqual(self.store.fetch.call_args[0][3], SRC)


class ConfiguredTest(unittest.TestCase):
  def test_from_the_environment(self):
    with patch.dict(os.environ, {"BITS_SOURCE_STORE": "https://s.example.org/sources/archives/"}):
      self.assertEqual(source_store.configured().base, "https://s.example.org/sources/archives")
    with patch.dict(os.environ, {"BITS_SOURCE_STORE": ""}):
      self.assertIsNone(source_store.configured())   # bits' own site configuration has none


if __name__ == "__main__":
  unittest.main()
