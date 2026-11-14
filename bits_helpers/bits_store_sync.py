# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A package store behind bits-store: --remote-store bits-store+https://host/store.

bits-store's tarballs format answers GET or HEAD <base>/<arch>/<hash> with a redirect
to the build of that hash, its file name in Bits-Object-Name; bits never lists. The
store token (bits login, ~/.bits/token) goes with every request to bits-store, never
to the redirect; a token the store does not take reads as anyone.

As a write store it uploads through bits-store: the revision marker under the
service's /MANIFESTS, then the build, staged and promoted once (first writer wins).
"""

import base64
import hashlib
import io
import os
import re
import threading
import time

import requests
import urllib3

from bits_helpers import rev_index
from bits_helpers.arch import effective_arch
from bits_helpers.log import debug, warning, dieOnError, ProgressPrint
from bits_helpers.rev_index import REVISION_TOKEN_PATTERN
from bits_helpers.source_store import store_token
from bits_helpers.sync import RemoteSync
from bits_helpers.utilities import resolve_store_path, ver_rev

SCHEME = "bits-store+"
TRIES = 3                  # a network failure, 5xx or 429 is tried again; a 404 never
_NAME = re.compile(r"[A-Za-z0-9._~+!@=,:-]{1,255}")   # bits-store's path rules
_REDIRECTS = (301, 302, 303, 307, 308)
_FAILED = (requests.RequestException, urllib3.exceptions.HTTPError, OSError, ValueError)
_REVISION = re.compile(REVISION_TOKEN_PATTERN)
_HASH = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
ONE_PUT = 5 << 30          # bits-store's most for one PUT; larger in parts of PART
PART = 1 << 30
BUSY_WAIT = 10             # seconds between completes while another promote runs


class StoreWriteError(Exception):
  pass


class BitsStoreSync(RemoteSync):
  def __init__(self, remoteStore, architecture, workdir, insecure=False, timeout=(10, 60),
               writable=False) -> None:
    self.remoteStore = remoteStore
    self.base = remoteStore[len(SCHEME):].rstrip("/")
    u = requests.utils.urlparse(self.base)
    self.root = "%s://%s" % (u.scheme, u.netloc)     # the service's, for its /MANIFESTS
    self.writeStore = remoteStore if writable else ""
    self.architecture = architecture
    self.workdir = workdir
    self.verify = not insecure
    self.timeout = timeout
    self.token = self.write_token = store_token(self.base)   # reads may drop theirs
    dieOnError(writable and not self.write_token,
               "%s takes uploads with a store token only (bits login; over https)" % self.base)
    self._local = threading.local()      # a session per thread: the plan asks from 16
    self._lock = threading.Lock()

  def probe_url(self):
    return self.base

  def _session(self):
    if getattr(self._local, "session", None) is None:
      self._local.session = requests.Session()
    return self._local.session

  def _ask(self, method, url, body=None, content=False, timeout=None):
    """bits-store's answer to *method* on *url*, tried again on failures; the token
    dropped, once said, when bits-store does not take it for a package read. A write
    (*body*, JSON) and *content* read the answer's body."""
    for attempt in range(TRIES):
      if attempt:
        time.sleep(0.5 * 2 ** attempt)
      token = self.write_token if body is not None else self.token
      headers = {"Authorization": "Bearer " + token} if token else {}
      try:
        # GET for a GET-signed redirect (presigned URLs sign their method); a read
        # streamed so an answer with a body is never read
        with self._session().request(method, url, headers=headers, json=body, allow_redirects=False,
                                     stream=body is None and not content, timeout=timeout or self.timeout,
                                     verify=self.verify) as r:
          if r.status_code in (401, 403) and headers and body is None and url.startswith(self.base + "/"):
            with self._lock:                 # said once, whatever thread asks first
              if self.token:
                warning("%s does not take your store token (bits login?): reading as anyone", self.base)
                self.token = None
            continue
          if r.status_code < 500 and r.status_code != 429:
            return r
          debug("%s: %s", url, r.status_code)
      except _FAILED as exc:
        debug("%s: %s", url, exc)
    return None

  def _find(self, method, arch, pkg_hash):
    """(file name, redirect) of the build of *pkg_hash*, or (None, None)."""
    url = "%s/%s/%s" % (self.base, arch, pkg_hash)
    r = self._ask(method, url)
    if r is None or r.status_code not in _REDIRECTS or not r.headers.get("Location"):
      debug("%s: %s", url, r.status_code if r is not None else "no answer")   # 404: not stored
      return None, None
    name = r.headers.get("Bits-Object-Name", "")
    if not _NAME.fullmatch(name) or name in (".", "..") or not name.endswith("." + arch + ".tar.gz"):
      debug("%s: ignoring the unsafe or foreign name %r", url, name)   # never a path outside
      return None, None
    return name, requests.compat.urljoin(url, r.headers["Location"])

  def list_store_tarballs(self, arch, pkg_hash):
    """The name of the build of *pkg_hash*, as a one-item list, or []."""
    name, _ = self._find("HEAD", arch, pkg_hash)
    return [name] if name else []

  def fetch_tarball(self, spec) -> None:
    arch = effective_arch(spec, self.architecture)
    pattern = re.compile(r"^{}-{}(?:-{})?\.{}\.tar\.gz$".format(
        re.escape(spec["package"]), re.escape(spec["version"]), REVISION_TOKEN_PATTERN, re.escape(arch)))
    for pkg_hash in spec["remote_hashes"]:            # one already here is used as it is
      directory = os.path.join(self.workdir, resolve_store_path(arch, pkg_hash))
      if os.path.isdir(directory) and any(pattern.match(f) and os.path.isfile(os.path.join(directory, f))
                                          for f in os.listdir(directory)):
        debug("Previously downloaded tarball for %s with hash %s, reusing", spec["package"], pkg_hash)
        return
    for pkg_hash in spec["remote_hashes"]:
      name, location = self._find("GET", arch, pkg_hash)
      if name and pattern.match(name):
        self._download(location, os.path.join(self.workdir, resolve_store_path(arch, pkg_hash)), name, spec)
        return
    debug("Nothing fetched for %s (%s)", spec["package"], ", ".join(spec["remote_hashes"]))

  def _download(self, location, directory, name, spec):
    """The build at *location* into *directory*/*name*, whole or not at all."""
    os.makedirs(directory, exist_ok=True)
    dest = os.path.join(directory, name)
    progress = ProgressPrint("Downloading tarball for %s@%s" % (spec["package"], spec["version"]),
                             min_interval=5.0)
    why = ""
    for attempt in range(TRIES):
      if attempt:
        time.sleep(0.5 * 2 ** attempt)
      why = self._get(location, dest, progress)
      if not why:
        progress.end("done")
        return
      if why.startswith("HTTP 4"):
        break                                  # the redirect expired or was refused
    progress.end("failed", error=True)
    warning("%s: download failed (%s); it will be built", name, why)

  def _get(self, location, dest, progress):
    """"" once *location* is in *dest*, else why not; never a partial file."""
    tmp = "%s.%d.%s.part" % (dest, os.getpid(), os.urandom(4).hex())
    try:
      with self._session().get(location, stream=True, timeout=self.timeout, verify=self.verify) as r:
        if r.status_code != 200:
          return "HTTP %d" % r.status_code
        size, want = 0, r.headers.get("Content-Length")
        with open(tmp, "wb") as f:
          for chunk in r.raw.stream(1 << 20, decode_content=False):   # the bytes as stored
            f.write(chunk)
            size += len(chunk)
            if want:
              progress("[%.0f%%] downloaded...", 100 * size / max(int(want), 1))
        if want is not None and size != int(want):
          return "%d of %s bytes" % (size, want)
        os.replace(tmp, dest)
        return ""
    except _FAILED as exc:
      return str(exc) or type(exc).__name__
    finally:
      if os.path.exists(tmp):
        os.unlink(tmp)

  def fetch_symlinks(self, spec) -> None:
    pass                     # a hash-only store: no dist links to read

  def read_rev_markers(self, package, version, arch):
    """{revision: hash} of the version's revision markers, from ?revisions; {} when
    there are none or bits-store does not answer."""
    if not all(_NAME.fullmatch(x) for x in (arch, package, version)):
      return {}
    url = "%s/%s?revisions" % (self.root, rev_index.marker_prefix(arch, package, version)[:-1])
    r = self._ask("GET", url, content=True)
    try:
      if r is None or r.status_code != 200:
        raise ValueError(r.status_code if r is not None else "no answer")
      return {rev: h for rev, h in r.json().items()
              if isinstance(h, str) and _REVISION.fullmatch(rev) and _HASH.fullmatch(h)}
    except (ValueError, AttributeError) as exc:
      debug("rev-index: %s: %s", url, exc)
      return {}

  def writestore_has_tarball(self, spec, architecture=None):
    if not self.writeStore:
      return False
    return bool(self.list_store_tarballs(effective_arch(spec, architecture or self.architecture), spec["hash"]))

  def upload_symlinks_and_tarball(self, spec) -> None:
    """The revision marker, then the build; a build already stored is kept, and
    the sha256 of what the store holds recorded in store_tarball_sha256."""
    if not self.writeStore:
      return
    arch = effective_arch(spec, self.architecture)
    name = "{}-{}.{}.tar.gz".format(spec["package"], ver_rev(spec), arch)
    if not (_NAME.fullmatch(name) and _NAME.fullmatch(spec["version"]) and _HASH.fullmatch(spec["hash"])):
      raise StoreWriteError("%s (%s): not a name bits-store takes" % (name, spec["hash"]))
    self.write_rev_marker(spec, arch)
    path = os.path.join(self.workdir, resolve_store_path(arch, spec["hash"]), name)
    url = "%s/%s/%s" % (self.base, arch, spec["hash"])
    sha = None if self.list_store_tarballs(arch, spec["hash"]) else self._upload(url, path, name=name)
    spec["store_tarball_sha256"] = sha or self._stored_sha256(arch, spec, name)

  def write_rev_marker(self, spec, arch):
    """This build's write-once revision marker; one taken by another hash stops a
    fixed revision label, as with the other stores."""
    rev = str(spec.get("revision") or "")
    if not rev or rev.startswith("local"):
      return
    key = rev_index.marker_key(arch, spec["package"], spec["version"], rev)
    recorded = None                # unwritten, the revision could be handed out again: an error
    if not self._upload("%s/%s" % (self.root, key), data=spec["hash"].encode()):
      recorded = self.read_rev_markers(spec["package"], spec["version"], arch).get(rev)
    fixed = spec.get("force_revision") and not spec.get("_revision_policy_hash_injected")
    dieOnError(bool(fixed and recorded) and recorded != spec["hash"],
               rev_index.fixed_label_taken(spec["package"], spec["version"], rev, recorded, spec["hash"]))

  def _upload(self, url, path=None, data=None, name=None):
    """*path*'s bytes (or *data*) to *url* through bits-store: their "sha256:<hex>"
    once stored, None when the name or hash holds a file already."""
    size = len(data) if data is not None else os.path.getsize(path)
    parts = size > ONE_PUT
    sha, md5, part_md5 = _checksums(path, data, PART if parts else 0)
    start = {"size": size, "sha256": sha[len("sha256:"):]}
    start.update({"part_size": PART, "part_md5": part_md5} if parts else {"md5": md5})
    if name:
      start["name"] = name
    r = self._ask("POST", url + "?uploads", body=start)
    if r is not None and r.status_code == 409:
      return None
    answer = _json(r, 201, url)
    for i, part in enumerate(answer.get("parts") or [answer]):
      self._put(part, path, data, i * PART, min(PART, size - i * PART) if parts else size)
    wait = 2 * (600 + (size >> 25)) + 300            # bits-store's own time for a complete
    deadline = time.monotonic() + wait
    while True:                                      # "busy": a promote of the name is running
      r = self._ask("POST", "%s?complete=%s" % (url, answer.get("id")), body={}, timeout=(10, wait))
      if r is None or r.status_code != 409 or _json(r, 409, url).get("error") != "busy" \
         or time.monotonic() > deadline:
        break
      time.sleep(BUSY_WAIT)
    if r is not None and r.status_code == 409 and _json(r, 409, url).get("error") == "exists":
      return None
    _json(r, 201, url)
    return sha

  def _put(self, part, path, data, offset, size):
    """One presigned PUT of *size* bytes at *offset*, tried again on failures."""
    why = ""
    for attempt in range(TRIES):
      if attempt:
        time.sleep(0.5 * 2 ** attempt)
      try:
        with (open(path, "rb") if path else io.BytesIO(data)) as f:
          with self._session().put(part["url"], data=_Slice(f, offset, size), headers=part.get("headers") or {},
                                   timeout=self.timeout, verify=self.verify) as r:
            if r.status_code == 200:
              return
            why = "HTTP %d" % r.status_code
            if r.status_code < 500:
              break
      except _FAILED as exc:
        why = str(exc) or type(exc).__name__
    raise StoreWriteError("upload: %s" % why)

  def _stored_sha256(self, arch, spec, name):
    """The sha256 of the build bits-store holds for the hash: the one its writer
    recorded, else of its bytes."""
    stored, location = self._find("GET", arch, spec["hash"])
    if not location:
      raise StoreWriteError("%s/%s: stored, but bits-store does not return it" % (arch, spec["hash"]))
    if stored != name:
      warning("%s: the store keeps %s for hash %s", name, stored, spec["hash"])
    digest = hashlib.sha256()
    try:
      with self._session().get(location, stream=True, timeout=self.timeout, verify=self.verify) as r:
        if r.status_code != 200:
          raise StoreWriteError("%s: HTTP %d" % (stored, r.status_code))
        recorded = r.headers.get("x-amz-meta-sha256", "")
        if _SHA256.fullmatch(recorded):
          return recorded
        for chunk in r.raw.stream(1 << 20, decode_content=False):
          digest.update(chunk)
    except _FAILED as exc:
      raise StoreWriteError("%s: %s" % (stored, exc)) from exc
    return "sha256:" + digest.hexdigest()

  def upload_shell_command(self, spec):
    return None

  def fetch_source(self, url_checksum, filename, dest_dir) -> bool:
    return False             # sources: the site's sources store (store.sources)

  def upload_source(self, local_path, url_checksum, filename) -> None:
    pass


def _checksums(path, data, part):
  """("sha256:<hex>", MD5 in base64, with *part* each part's MD5 in base64) in one pass."""
  sha, md5, parts, this, left = hashlib.sha256(), hashlib.md5(), [], hashlib.md5(), part
  with (open(path, "rb") if path else io.BytesIO(data)) as f:
    for chunk in iter(lambda: f.read(min(1 << 20, left) if part else 1 << 20), b""):
      sha.update(chunk)
      md5.update(chunk)
      if part:
        this.update(chunk)
        left -= len(chunk)
        if not left:
          parts.append(base64.b64encode(this.digest()).decode())
          this, left = hashlib.md5(), part
  if part and left != part:
    parts.append(base64.b64encode(this.digest()).decode())
  return "sha256:" + sha.hexdigest(), base64.b64encode(md5.digest()).decode(), parts


def _json(r, want, url):
  """The JSON answer *r* when its status is *want*, else StoreWriteError."""
  if r is None:
    raise StoreWriteError("%s: no answer" % url)
  try:
    answer = r.json() if r.content else {}
  except ValueError:
    answer = {}
  if r.status_code != want or not isinstance(answer, dict):
    raise StoreWriteError("%s: HTTP %d %s" % (url.split("?")[0], r.status_code, answer.get("error", "")
                                               if isinstance(answer, dict) else ""))
  return answer


class _Slice:
  """*size* bytes of *f* from *offset*, read as requests streams a body."""
  def __init__(self, f, offset, size):
    f.seek(offset)
    self.f, self.left, self.size = f, size, size

  def __len__(self):
    return self.size

  def read(self, n=-1):
    n = self.left if n is None or n < 0 else min(n, self.left)
    chunk = self.f.read(n)
    self.left -= len(chunk)
    return chunk
