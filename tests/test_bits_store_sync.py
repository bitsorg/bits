# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The bits-store package store (bits_helpers.bits_store_sync) against a local
stand-in for bits-store's tarballs format: a build by its hash, never a listing;
writes staged, then promoted once."""

import base64
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from bits_helpers import plan, source_store, sync
from bits_helpers import bits_store_sync
from bits_helpers.bits_store_sync import BitsStoreSync, StoreWriteError

ARCH = "x86_64-el9"
H1, H2 = "1" * 40, "2" * 40
NAME = "zlib-v1.3-1.%s.tar.gz" % ARCH
DATA = b"a build"


class Store(BaseHTTPRequestHandler):
  """/store/<arch>/<hash>: 302 to /bucket/<hash> with the name, 404 if absent; a
  token other than "good" 401; /bucket/<hash> the bytes, refusing any token.
  /restricted/<arch>/<hash> likewise from private, for the token "good" only (403)."""
  builds, public, asked, cut, failing, bare, private = {}, False, [], False, 0, False, {}
  group, sigs, unsigned = "lhcb", {}, False     # /restricted's Bits-Object-Group; signatures by hash
  markers, meta, staged, busy, racer, put_fails = {}, {}, {}, 0, None, 0
  refused, failing_completes = "", 0           # refused: a path prefix answered 403
  by = {}                                      # hash -> Bits-Object-By

  def log_message(self, *a):
    pass

  def _route(self, body):
    auth = self.headers.get("Authorization")
    Store.asked.append((self.command, self.path, auth))
    if self.path.startswith(("/MANIFESTS/", "/BUILDS/")):    # objects by key: 302 to /obj/
      if Store.failing > 0:
        Store.failing -= 1
        return self._answer(503)
      if auth not in (None, "Bearer good"):
        return self._answer(401)
      if self.path[1:] not in self.markers:
        return self._answer(404)
      self.send_response(302)
      self.send_header("Location", "/obj/" + self.path[1:])
      self.send_header("Content-Length", "0")
      self.end_headers()
      return
    if self.path.startswith("/obj/"):                        # the bucket: never a token
      key = self.path[len("/obj/"):]
      if auth or key.endswith("forbidden") or key not in self.markers:
        return self._answer(404 if key not in self.markers else 403)
      data = self.markers[key].encode()
      self.send_response(200)
      self.send_header("Content-Length", str(len(data)))
      self.end_headers()
      self.wfile.write(data)
      return
    if self.path.startswith("/bucket/"):
      h = self.path[len("/bucket/"):]
      private = h.startswith("p")                         # /restricted's builds: /bucket/p<hash>
      h = h[1:] if private else h
      build = (self.private if private else self.builds).get(h)
      if auth or build is None:
        return self._answer(400 if auth else 404)
      self.send_response(200)
      if h in self.meta:
        self.send_header("x-amz-meta-sha256", self.meta[h])
      if private and h in self.sigs:
        self.send_header("x-amz-meta-bits-signature", self.sigs[h])
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
    area, _, rest = self.path[1:].partition("/")
    if area == "restricted" and (auth != "Bearer good" or self.refused == "/restricted"):
      return self._answer(403)                 # no capability there
    arch, _, h = rest.partition("/")
    build = (self.private if area == "restricted" else self.builds).get(h) if arch == ARCH else None
    if build is None:
      return self._answer(404)
    self.send_response(302)
    if not self.bare:
      self.send_header("Location", "/bucket/" + ("p" if area == "restricted" else "") + h)
      self.send_header("Bits-Object-Name", build[0])
      if area == "restricted" and self.group:
        self.send_header("Bits-Object-Group", self.group)
      if h in self.by:
        self.send_header("Bits-Object-By", self.by[h])
    self.send_header("Content-Length", "0")
    self.end_headers()

  def do_GET(self):
    if self.path.endswith("?revisions"):
      Store.asked.append(("GET", self.path, self.headers.get("Authorization")))
      if self.refused and self.path.startswith(self.refused):
        return self._json(403, {"error": "forbidden"})
      prefix = self.path[len("/"):-len("?revisions")] + "-"
      return self._json(200, {k[len(prefix):]: v for k, v in self.markers.items() if k.startswith(prefix)})
    self._route(True)

  def do_POST(self):
    """?uploads and ?complete=<id>, for /store/<arch>/<hash> (a "name") and markers."""
    auth = self.headers.get("Authorization")
    Store.asked.append(("POST", self.path, auth))
    body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
    if auth != "Bearer good":
      return self._json(401, {"error": "unauthorized"})
    if self.refused and self.path.startswith(self.refused):
      return self._json(403, {"error": "forbidden"})
    if self.unsigned and self.path.startswith("/restricted/"):
      return self._json(503, {"error": "unsigned"})       # its signer without its key
    path, _, query = self.path.partition("?")
    h = path.rsplit("/", 1)[1]
    kept = self.private if path.startswith("/restricted/") else self.builds
    taken = (lambda: h in kept) if path.startswith(("/store/", "/restricted/")) else (lambda: path[1:] in self.markers)
    if query == "uploads":
      if taken():
        return self._json(409, {"error": "exists"})
      sid, put = str(len(self.staged)), "http://%s/put/" % self.headers["Host"]   # presigned: absolute
      self.staged[sid] = {"path": path, "body": body, "parts": {}}
      if "part_md5" in body:
        return self._json(201, {"id": sid, "parts": [
            {"number": n + 1, "url": "%s%s/%d" % (put, sid, n + 1), "headers": {"Content-MD5": m}}
            for n, m in enumerate(body["part_md5"])]})
      return self._json(201, {"id": sid, "url": "%s%s/1" % (put, sid), "headers": {"Content-MD5": body["md5"]}})
    up = self.staged[query[len("complete="):]]
    if Store.failing_completes:
      Store.failing_completes -= 1
      return self._json(502, {})
    if Store.busy and path.startswith("/store/"):
      Store.busy -= 1
      return self._json(409, {"error": "busy"})
    if Store.racer and path.startswith("/store/"):
      Store.builds[h], Store.racer = Store.racer, None
    if taken():
      return self._json(409, {"error": "exists"})
    data = b"".join(up["parts"][n] for n in sorted(up["parts"]))
    if len(data) != up["body"]["size"] or hashlib.sha256(data).hexdigest() != up["body"]["sha256"]:
      return self._json(422, {"error": "mismatch"})
    if path.startswith(("/store/", "/restricted/")):
      kept[h], Store.meta[h] = (up["body"]["name"], data), "sha256:" + up["body"]["sha256"]
    else:
      Store.markers[path[1:]] = data.decode()
    return self._json(201, {})

  def do_PUT(self):
    Store.asked.append(("PUT", self.path, self.headers.get("Authorization")))
    data = self.rfile.read(int(self.headers["Content-Length"]))
    if Store.put_fails:
      Store.put_fails -= 1
      return self._answer(403)
    _, _, sid, n = self.path.split("/")
    if base64.b64encode(hashlib.md5(data).digest()).decode() != self.headers.get("Content-MD5"):
      return self._answer(400)
    Store.staged[sid]["parts"][int(n)] = data
    self._answer(200)

  def _json(self, code, answer):
    out = json.dumps(answer).encode()
    self.send_response(code)
    self.send_header("Content-Length", str(len(out)))
    self.end_headers()
    self.wfile.write(out)

  def do_HEAD(self):
    self._route(False)

  def _answer(self, code):
    self.send_response(code)
    self.send_header("Content-Length", "0")
    self.end_headers()


class StandIn(unittest.TestCase):
  def setUp(self):
    Store.builds, Store.public, Store.asked, Store.cut, Store.failing, Store.bare = {H2: (NAME, DATA)}, False, [], False, 0, False
    Store.private, Store.group, Store.sigs, Store.unsigned = {}, "lhcb", {}, False
    Store.markers, Store.meta, Store.staged, Store.busy, Store.racer, Store.put_fails = {}, {}, {}, 0, None, 0
    Store.refused, Store.failing_completes, Store.by = "", 0, {}
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
    from bits_helpers import trust                # no restricted keys but a test's own
    p = patch.object(trust, "default_trust_dirs", lambda: [os.path.join(self.tmp.name, "no-keys")])
    p.start()
    self.addCleanup(p.stop)

  def store(self):
    return BitsStoreSync(self.url, ARCH, self.work)

  def spec(self, *hashes):
    return {"package": "zlib", "version": "v1.3", "remote_hashes": list(hashes)}

  def stored(self, h=H2):
    """The build of *h* fetched into the work dir, or None."""
    path = os.path.join(self.work, "TARS", ARCH, "store", h[:2], h, NAME)
    if not os.path.isfile(path):
      return None
    with open(path, "rb") as f:
      return f.read()


class BitsStoreSyncTest(StandIn):
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
    rw = sync.remote_from_url(self.url, self.url, ARCH, self.work)
    self.assertIsInstance(rw, BitsStoreSync)
    self.assertEqual(rw.writeStore, self.url)
    for read in ("rsync://example.org/store", "s3://bucket", self.url.replace("/store", "/other")):
      with tempfile.TemporaryDirectory() as w:
        dual = sync.remote_from_url(read, self.url, ARCH, w)
      self.assertIsInstance(dual, sync.DualRemoteSync, read)
      self.assertIsInstance(dual.writer, BitsStoreSync)
      self.assertEqual(dual.writer.writeStore, self.url)

  def test_writes_need_a_token(self):
    os.unlink(self.token)
    self.assertEqual(sync.remote_from_url(self.url, "", ARCH, self.work).writeStore, "")
    with self.assertRaises(SystemExit):
      sync.remote_from_url(self.url, self.url, ARCH, self.work)


BUILT = b"a new build, " * 5


class BitsStoreObjectsTest(StandIn):
  """Objects by key (BOMs, records, the marker): read through the redirect, the token
  never sent to the bucket; written create-only."""

  def test_put_create_only_and_get(self):
    s = BitsStoreSync(self.url, ARCH, self.work, writable=True)
    key = "MANIFESTS/release_x-ab/p7-NOTICE"
    self.assertTrue(s.put_key(key, b"notice"))
    self.assertFalse(s.put_key(key, b"another"))                 # taken: kept
    self.assertEqual(s.get_key(key), b"notice")
    self.assertIsNone(s.get_key("BUILDS/by-pipeline/7/release_x-ab.json"))
    bucket = [a for m, p, a in Store.asked if p.startswith("/obj/")]
    self.assertEqual(bucket, [None])                             # no token to the bucket

  def test_a_store_that_cannot_say_is_an_error(self):
    s = self.store()
    Store.markers["MANIFESTS/forbidden"] = "x"
    with self.assertRaises(StoreWriteError):
      s.get_key("MANIFESTS/forbidden")                           # 403 at the bucket: final
    self.assertEqual(sum(p == "/obj/MANIFESTS/forbidden" for m, p, a in Store.asked), 1)
    Store.failing = 9
    with self.assertRaises(StoreWriteError):
      s.get_key("MANIFESTS/orchestrator.json")


class BitsStoreWriteTest(StandIn):
  def writer(self):
    return BitsStoreSync(self.url, ARCH, self.work, writable=True)

  def built(self, h=H1, revision="2", data=BUILT, **more):
    spec = {"package": "zlib", "version": "v1.3", "revision": revision, "hash": h, **more}
    d = os.path.join(self.work, "TARS", ARCH, "store", h[:2], h)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "zlib-v1.3-%s.%s.tar.gz" % (revision, ARCH)), "wb") as f:
      f.write(data)
    return spec

  def marker(self, rev="2"):
    return "MANIFESTS/rev-index/%s/zlib/v1.3-%s" % (ARCH, rev)

  def test_the_marker_then_the_build(self):
    spec = self.built()
    self.writer().upload_symlinks_and_tarball(spec)
    self.assertEqual(Store.markers, {self.marker(): H1})
    self.assertEqual(Store.builds[H1], ("zlib-v1.3-2.%s.tar.gz" % ARCH, BUILT))
    self.assertEqual(spec["store_tarball_sha256"], "sha256:" + hashlib.sha256(BUILT).hexdigest())
    posts = [p for m, p, _ in Store.asked if m == "POST"]
    self.assertTrue(posts[0].startswith("/" + self.marker() + "?uploads"), posts)
    self.assertTrue(any(p.startswith("/store/%s/%s?complete=" % (ARCH, H1)) for p in posts), posts)
    self.assertTrue(all(a == "Bearer good" for m, p, a in Store.asked if not p.startswith(("/put/", "/bucket/"))))
    self.assertTrue(all(a is None for m, p, a in Store.asked if p.startswith(("/put/", "/bucket/"))))
    self.assertTrue(self.writer().writestore_has_tarball(spec))
    self.assertFalse(self.store().writestore_has_tarball(spec))     # not a write store

  def test_writes_keep_the_token_reads_dropped(self):
    w = self.writer()
    w.token = None                                    # as when a read was refused
    w.upload_symlinks_and_tarball(self.built())
    self.assertIn(H1, Store.builds)

  def test_a_busy_name_is_waited_for(self):
    Store.busy, Store.failing_completes = 6, 1         # past TRIES, and a 502 first
    spec = self.built()
    self.writer().upload_symlinks_and_tarball(spec)
    self.assertEqual(Store.builds[H1][1], BUILT)
    self.assertEqual(spec["store_tarball_sha256"], "sha256:" + hashlib.sha256(BUILT).hexdigest())

  def test_a_marker_not_written_fails_the_package(self):
    Store.refused = "/MANIFESTS/"
    w = self.writer()
    with self.assertRaises(StoreWriteError):
      w.upload_symlinks_and_tarball(self.built())
    self.assertNotIn(H1, Store.builds)
    self.assertEqual(w.read_rev_markers("zlib", "v1.3", ARCH), {})
    self.assertEqual(w.token, "good")                  # a refused marker read keeps it for packages
    self.assertEqual(w.list_store_tarballs(ARCH, H2), [NAME])

  def test_bucket_commands_refuse_it(self):
    from bits_helpers.publish import _normalize_s3_store
    with self.assertRaises(SystemExit):
      _normalize_s3_store(self.url)
    from bits_helpers.orchestrated import s3_of
    self.assertIsNone(s3_of(self.url))

  def test_in_parts(self):
    spec = self.built(data=bytes(range(256)) * 3)
    shown, said = [], []
    progress = MagicMock(side_effect=lambda txt, pct: shown.append(pct))
    with patch.object(bits_store_sync, "ONE_PUT", 500), patch.object(bits_store_sync, "PART", 300), \
         patch.object(bits_store_sync, "ProgressPrint", return_value=progress) as made, \
         patch.object(bits_store_sync.sys.stdout, "isatty", return_value=False), \
         patch.object(bits_store_sync, "info", lambda fmt, *a: said.append(fmt % a)):
      self.writer().upload_symlinks_and_tarball(spec)
    name = "zlib-v1.3-2.%s.tar.gz" % ARCH
    made.assert_called_once_with("Uploading " + name, min_interval=5.0)   # none for the marker
    self.assertEqual(said, ["%s: part %d of 3 uploaded" % (name, n) for n in (1, 2, 3)]
                     + ["%s: bits-store is storing it (0 MiB)" % name])
    self.assertEqual(shown[-1], 100)                     # the build's bytes, the marker's not
    self.assertEqual(shown, sorted(shown))
    progress.end.assert_called_once_with("done")
    said.clear()                                         # on a terminal, the progress alone
    with patch.object(bits_store_sync, "ONE_PUT", 500), patch.object(bits_store_sync, "PART", 300), \
         patch.object(bits_store_sync.sys.stdout, "isatty", return_value=True), \
         patch.object(bits_store_sync, "info", lambda fmt, *a: said.append(fmt % a)):
      self.writer().upload_symlinks_and_tarball(self.built(h="4" * 40, data=bytes(700)))
    self.assertEqual(said, ["%s: bits-store is storing it (0 MiB)" % name])
    self.assertEqual(Store.builds[H1][1], bytes(range(256)) * 3)
    self.assertEqual(len([p for m, p, _ in Store.asked if p.startswith("/put/1/")]), 3)
    self.assertEqual(Store.staged["1"]["body"]["part_size"], 300)
    self.assertNotIn("md5", Store.staged["1"]["body"])

  def test_a_stored_build_is_kept_and_its_sha256_recorded(self):
    Store.meta[H2] = "sha256:" + "a" * 64
    spec = self.built(h=H2, revision="1")
    self.writer().upload_symlinks_and_tarball(spec)
    self.assertEqual(spec["store_tarball_sha256"], "sha256:" + "a" * 64)
    self.assertEqual(spec["store_tarball_by"], "unknown")             # before bits-store said
    self.assertEqual(Store.builds[H2], (NAME, DATA))
    self.assertFalse(any(p.startswith("/store/") for m, p, _ in Store.asked if m == "POST"))
    del Store.meta[H2]                                 # an old one: its bytes hashed
    Store.by[H2] = "j%C3%BCrgen%0Aevil"               # its uploader, escaped
    spec = self.built(h=H2, revision="1")
    self.writer().upload_symlinks_and_tarball(spec)
    self.assertEqual(spec["store_tarball_sha256"], "sha256:" + hashlib.sha256(DATA).hexdigest())
    self.assertEqual(spec["store_tarball_by"], "jürgenevil")         # printable only
    spec = self.built(h=H1)                            # uploaded by this build: nobody else's
    self.writer().upload_symlinks_and_tarball(spec)
    self.assertNotIn("store_tarball_by", spec)

  def test_a_race_lost_keeps_the_winner(self):
    Store.racer, Store.busy = ("zlib-v1.3-3.%s.tar.gz" % ARCH, b"first"), 1
    spec = self.built()
    with patch("bits_helpers.bits_store_sync.warning") as warned:
      self.writer().upload_symlinks_and_tarball(spec)
    self.assertEqual(Store.builds[H1][1], b"first")
    self.assertEqual(spec["store_tarball_sha256"], "sha256:" + hashlib.sha256(b"first").hexdigest())
    warned.assert_called_once()                       # kept under another name

  def test_a_failed_put_is_never_completed(self):
    Store.put_fails = 1
    spec = self.built(revision="local1")              # no marker for a local revision
    progress = MagicMock()
    with self.assertRaises(StoreWriteError), patch.object(bits_store_sync, "ProgressPrint", return_value=progress):
      self.writer().upload_symlinks_and_tarball(spec)
    progress.end.assert_called_once_with("failed", error=True)
    self.assertNotIn(H1, Store.builds)
    self.assertFalse(any("complete" in p for _, p, _ in Store.asked))
    self.assertEqual(Store.markers, {})

  def test_a_fixed_label_taken_stops(self):
    Store.markers[self.marker("1")] = H2
    self.writer().upload_symlinks_and_tarball(self.built(revision="1"))
    self.assertEqual(Store.markers[self.marker("1")], H2)          # write-once
    with self.assertRaises(SystemExit):
      self.writer().upload_symlinks_and_tarball(self.built(h="3" * 40, revision="1", force_revision="1"))

  def test_revisions(self):
    Store.markers = {self.marker("1"): H1, self.marker("2"): "6" * 64, self.marker("x"): H2,
                     self.marker("3"): "not a hash", "MANIFESTS/rev-index/%s/zlib/v1.3-alpha-1" % ARCH: H2}
    self.assertEqual(self.store().read_rev_markers("zlib", "v1.3", ARCH), {"1": H1, "2": "6" * 64})
    self.assertEqual(self.store().read_rev_markers("zlib", "../v1.3", ARCH), {})
    s = BitsStoreSync("bits-store+http://127.0.0.1:9/store", ARCH, self.work, timeout=(1, 1))
    self.assertEqual(s.read_rev_markers("zlib", "v1.3", ARCH), {})


class RestrictedTest(StandIn):
  """Builds that may not be redistributed: bits-store's /restricted, with a token,
  reused only when bits-store's signature verifies over the bytes downloaded."""
  built = BitsStoreWriteTest.built

  def setUp(self):
    super().setUp()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from bits_helpers import trust
    self.key = Ed25519PrivateKey.generate()
    keys = os.path.join(self.tmp.name, "keys")
    os.makedirs(os.path.join(keys, "restricted"))
    with open(os.path.join(keys, "restricted", "store.pem"), "wb") as f:
      f.write(self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo))
    p = patch.object(trust, "default_trust_dirs", lambda: [keys])
    p.start()
    self.addCleanup(p.stop)
    self.kid = trust.key_id(self.key.public_key())

  def sign(self, h=H2, data=DATA, group="lhcb", name=NAME, key=None):
    """bits-store's signature of the restricted build of *h*."""
    sha = hashlib.sha256(data).hexdigest()
    sig = (key or self.key).sign(bits_store_sync.restricted_statement(ARCH, h, group, name, sha))
    return "%s:%s" % (self.kid, base64.b64encode(sig).decode())

  def test_its_keys_sign_nothing_else(self):
    from bits_helpers import trust
    self.assertIn(self.kid, trust.load_restricted_keys())
    self.assertNotIn(self.kid, trust.load_trusted_keys())          # never a manifest's

  def test_the_statement_is_bits_stores(self):
    # bits-store's TestTheRestrictedStatement: the same bytes
    self.assertEqual(bits_store_sync.restricted_statement(
        "x86_64-el9", "ab12", "a.b_c-d", "Aa0._~+!@=,:-1.0-1.x86_64-el9.tar.gz", "ff"),
        b"bits-restricted-build v1\n" + b'{"arch":"x86_64-el9","group":"a.b_c-d","hash":"ab12",'
        b'"name":"Aa0._~+!@=,:-1.0-1.x86_64-el9.tar.gz","sha256":"ff"}')

  def test_read_after_the_store_with_a_token(self):
    Store.builds, Store.private, Store.sigs = {}, {H2: (NAME, DATA)}, {H2: self.sign()}
    Store.by[H2] = "alice"                             # who uploaded it
    store = self.store()
    store.fetch_tarball(self.spec(H2))
    self.assertEqual(self.stored(), DATA)
    self.assertEqual([p for m, p, _ in Store.asked if m == "GET" and not p.startswith("/bucket/")],
                     ["/store/%s/%s" % (ARCH, H2), "/restricted/%s/%s" % (ARCH, H2)])
    self.assertTrue(store.stored_privately(ARCH, H2))
    self.assertEqual(store.verified_private(ARCH, H2), "sha256:" + hashlib.sha256(DATA).hexdigest())
    m = sync.MultiRemoteSync([sync.RemoteSync(), store], [])
    self.assertEqual(m.verified_private(ARCH, H2), store.verified_private(ARCH, H2))
    self.assertEqual((store.writer_of(ARCH, H2), m.writer_of(ARCH, H2)), ("alice", "alice"))
    self.assertTrue(m.stored_privately(ARCH, H2))

  def test_never_kept_unless_its_signature_verifies(self):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    swapped = b"swapped bytes"
    for why, sig, group, data in (
        ("none", None, "lhcb", DATA),
        ("other bytes", self.sign(), "lhcb", swapped),
        ("another group's", self.sign(group="atlas"), "lhcb", DATA),     # copied into lhcb's folder
        ("no group said", self.sign(group=""), "", DATA),
        ("another key", self.sign(key=Ed25519PrivateKey.generate()), "lhcb", DATA),
        ("not base64", self.kid + ":%%%", "lhcb", DATA)):
      with self.subTest(why):
        Store.builds, Store.private, Store.group = {}, {H2: (NAME, data)}, group
        Store.sigs = {H2: sig} if sig else {}
        store = self.store()
        store.fetch_tarball(self.spec(H2))
        self.assertIsNone(self.stored(), why)
        self.assertIsNone(store.verified_private(ARCH, H2))

  def test_a_bad_signature_is_not_downloaded_again(self):
    Store.builds, Store.private, Store.sigs = {}, {H2: (NAME, DATA)}, {H2: self.sign(data=b"other")}
    self.store().fetch_tarball(self.spec(H2))
    self.assertEqual(len([p for m, p, _ in Store.asked if m == "GET" and p.startswith("/bucket/")]), 1)

  def test_without_its_keys_never_used(self):
    from bits_helpers import trust
    Store.builds, Store.private, Store.sigs = {}, {H2: (NAME, DATA)}, {H2: self.sign()}
    for why, keys in (("no key", lambda: {}), ("no cryptography", lambda: (_ for _ in ()).throw(ImportError("x")))):
      with self.subTest(why), patch.object(trust, "load_restricted_keys", keys):
        Store.asked = []
        store, w = self.store(), BitsStoreSync(self.url, ARCH, self.work, writable=True)
        store.fetch_tarball(self.spec(H2))
        self.assertIsNone(self.stored())
        self.assertFalse([p for _, p, _ in Store.asked if p.startswith("/restricted/")])
        self.assertFalse(w.stores_restricted())
    # a manifest key (a trust dir's own) vouches for nothing restricted
    with patch.object(trust, "load_restricted_keys", lambda: {}), \
         patch.object(trust, "load_trusted_keys", lambda *a: {self.kid: self.key.public_key()}):
      self.store().fetch_tarball(self.spec(H2))
    self.assertIsNone(self.stored())

  def test_one_not_to_be_redistributed_is_looked_for_there_first(self):
    Store.builds, Store.private, Store.sigs = {H2: (NAME, b"public")}, {H2: (NAME, DATA)}, {H2: self.sign()}
    self.store().fetch_tarball(dict(self.spec(H2), redistributable="none"))
    self.assertEqual(self.stored(), DATA)

  def test_a_readers_build_by_its_group(self):
    Store.builds, Store.private, Store.group = {}, {H2: (NAME, DATA)}, "atlas"   # lhcb reads atlas's
    Store.sigs = {H2: self.sign(group="atlas")}
    self.store().fetch_tarball(self.spec(H2))
    self.assertEqual(self.stored(), DATA)

  def test_never_asked_without_a_token(self):
    Store.builds, Store.private, Store.public = {}, {H2: (NAME, DATA)}, True
    os.unlink(self.token)
    self.store().fetch_tarball(self.spec(H2))
    self.assertIsNone(self.stored())
    self.assertFalse([p for _, p, _ in Store.asked if p.startswith("/restricted/")])

  def test_a_token_without_it_asks_once(self):
    Store.builds, Store.private, Store.refused = {H1: (NAME, DATA)}, {H2: (NAME, DATA)}, "/restricted"
    store = self.store()                     # a member's token: no /restricted capability
    self.assertEqual(store.list_store_tarballs(ARCH, H2), [])
    self.assertEqual(store.list_store_tarballs(ARCH, "3" * 40), [])
    asked = [(p, a) for _, p, a in Store.asked if p.startswith("/restricted/")]
    self.assertEqual(asked, [("/restricted/%s/%s" % (ARCH, H2), "Bearer good")])   # refused once
    self.assertEqual(store.list_store_tarballs(ARCH, H1), [NAME])
    self.assertEqual(Store.asked[-1][2], "Bearer good")   # the store still with it

  def test_uploaded_there_and_only_there(self):
    w = BitsStoreSync(self.url, ARCH, self.work, writable=True)
    Store.builds = {}
    spec = self.built(redistributable="none")
    self.assertIsNone(w.upload_symlinks_and_tarball(spec))
    self.assertIn(H1, Store.private)
    self.assertNotIn(H1, Store.builds)
    self.assertEqual(Store.markers, {"MANIFESTS/rev-index/%s/zlib/v1.3-2" % ARCH: H1})
    self.assertEqual(spec["store_tarball_sha256"], Store.meta[H1])
    self.assertTrue(w.writestore_has_tarball(spec))
    public = self.built(h=H2, revision="3")      # one that may be: the store, not /restricted
    self.assertFalse(w.writestore_has_tarball(public))
    w.upload_symlinks_and_tarball(public)
    self.assertIn(H2, Store.builds)
    self.assertNotIn(H2, Store.private)

  def test_a_build_now_public_goes_to_the_store(self):
    # redistributable is not in the hash: a build in /restricted is still uploaded to the store
    w = BitsStoreSync(self.url, ARCH, self.work, writable=True)
    Store.builds, Store.private = {}, {H1: ("zlib-v1.3-2.%s.tar.gz" % ARCH, BUILT)}
    w.upload_symlinks_and_tarball(self.built())
    self.assertIn(H1, Store.builds)

  def test_a_refused_upload_keeps_it_here(self):
    w = BitsStoreSync(self.url, ARCH, self.work, writable=True)
    Store.refused = "/restricted"              # a person's token, or no such binding
    spec = self.built(redistributable="none")
    self.assertIs(w.upload_symlinks_and_tarball(spec), False)
    self.assertNotIn(H1, Store.private)
    self.assertNotIn("store_tarball_sha256", spec)
    m = sync.MultiRemoteSync([w], [w])
    self.assertIs(m.upload_symlinks_and_tarball(spec), False)
    Store.refused = "/nowhere"
    other = BitsStoreSync(self.url, ARCH, self.work, writable=True)
    other.restricted = other.root + "/nowhere"   # one of two write stores without the binding
    m = sync.MultiRemoteSync([w], [w, other])
    self.assertIs(m.upload_symlinks_and_tarball(spec), False)
    self.assertIn(H1, Store.private)             # the other has it
    Store.refused, Store.unsigned = "", True    # bits-store's signer without its key: kept here too
    self.assertIs(w.upload_symlinks_and_tarball(self.built(h="4" * 40, revision="4", redistributable="none")), False)
    self.assertNotIn("4" * 40, Store.private)
    Store.unsigned = False
    Store.refused = "/store"                   # the store's refusal still fails the package
    with self.assertRaises(StoreWriteError):
      w.upload_symlinks_and_tarball(self.built(h="3" * 40, revision="3"))

  def test_the_write_path_keeps_its_token(self):
    w = BitsStoreSync(self.url, ARCH, self.work, writable=True)
    w.token = None                             # reads refused once: the uploads' checks still ask
    Store.private = {H1: ("zlib-v1.3-2.%s.tar.gz" % ARCH, BUILT)}
    Store.meta[H1] = "sha256:" + hashlib.sha256(BUILT).hexdigest()
    spec = self.built(redistributable="none")
    w.upload_symlinks_and_tarball(spec)
    self.assertEqual(spec["store_tarball_sha256"], Store.meta[H1])

  def test_which_stores_may_take_it(self):
    w = BitsStoreSync(self.url, ARCH, self.work, writable=True)
    private, public = {"redistributable": "none"}, {}
    self.assertTrue(sync.may_upload(w, private))
    self.assertFalse(sync.may_upload(self.store(), private))          # read only
    b3 = sync.RemoteSync()                                            # any other store
    self.assertFalse(sync.may_upload(sync.MultiRemoteSync([w], [w, b3]), private))
    self.assertTrue(sync.may_upload(sync.MultiRemoteSync([b3], [w]), private))
    self.assertTrue(sync.may_upload(b3, public))
    self.assertFalse(sync.may_upload(None, private))
    m = sync.MultiRemoteSync([w], [w])
    m.writeStore = ""                                                 # a development package
    self.assertFalse(sync.may_upload(m, private))

  def test_the_plan_takes_it_for_vouched(self):
    Store.builds, Store.private = {}, {H2: (NAME, DATA)}
    spec = dict(self.spec(H2), remote_revision_hash=H2, hash=H2, revision="1",
                local_revision_hash=H2, local_hashes=[], is_devel_pkg=False)
    state = plan.classify(spec, ARCH, self.work, self.store(), True, False, trusted={})[0]
    self.assertEqual(state, plan.REMOTE)
    Store.private = {}
    Store.builds = {H2: (NAME, DATA)}                                 # the store's, unsigned
    state = plan.classify(spec, ARCH, self.work, self.store(), True, False, trusted={})[0]
    self.assertEqual(state, plan.REMOTE_UNSIGNED)


if __name__ == "__main__":
  unittest.main()
