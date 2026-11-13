# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A package store behind bits-store: --remote-store bits-store+https://host/store.

bits-store's tarballs format answers GET or HEAD <base>/<arch>/<hash> with a redirect
to the build of that hash, its file name in Bits-Object-Name; bits never lists. The
store token (bits login, ~/.bits/token) goes with every request to bits-store, never
to the redirect; a token the store does not take reads as anyone. Read only for now.
"""

import os
import re
import threading
import time

import requests
import urllib3

from bits_helpers.arch import effective_arch
from bits_helpers.log import debug, warning, ProgressPrint
from bits_helpers.rev_index import REVISION_TOKEN_PATTERN
from bits_helpers.source_store import store_token
from bits_helpers.sync import RemoteSync
from bits_helpers.utilities import resolve_store_path

SCHEME = "bits-store+"
TRIES = 3                  # a network failure, 5xx or 429 is tried again; a 404 never
_NAME = re.compile(r"[A-Za-z0-9._~+!@=,:-]{1,255}")   # bits-store's path rules
_REDIRECTS = (301, 302, 303, 307, 308)
_FAILED = (requests.RequestException, urllib3.exceptions.HTTPError, OSError, ValueError)


class BitsStoreSync(RemoteSync):
  def __init__(self, remoteStore, architecture, workdir, insecure=False, timeout=(10, 60)) -> None:
    self.remoteStore = remoteStore
    self.base = remoteStore[len(SCHEME):].rstrip("/")
    self.writeStore = ""
    self.architecture = architecture
    self.workdir = workdir
    self.verify = not insecure
    self.timeout = timeout
    self.token = store_token(self.base)
    self._local = threading.local()      # a session per thread: the plan asks from 16
    self._lock = threading.Lock()

  def probe_url(self):
    return self.base

  def _session(self):
    if getattr(self._local, "session", None) is None:
      self._local.session = requests.Session()
    return self._local.session

  def _ask(self, method, url):
    """bits-store's answer to *method* on *url*, tried again on failures; the token
    dropped, once said, when bits-store does not take it."""
    for attempt in range(TRIES):
      if attempt:
        time.sleep(0.5 * 2 ** attempt)
      headers = {"Authorization": "Bearer " + self.token} if self.token else {}
      try:
        # GET for a GET-signed redirect (presigned URLs sign their method), streamed so
        # an answer with a body is never read
        with self._session().request(method, url, headers=headers, allow_redirects=False, stream=True,
                                     timeout=self.timeout, verify=self.verify) as r:
          if r.status_code in (401, 403) and headers:
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

  def upload_symlinks_and_tarball(self, spec) -> None:
    pass

  def upload_shell_command(self, spec):
    return None

  def fetch_source(self, url_checksum, filename, dest_dir) -> bool:
    return False             # sources: the site's sources store (store.sources)

  def upload_source(self, local_path, url_checksum, filename) -> None:
    pass
