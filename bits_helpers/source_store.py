# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-store's sources binding: a source archive by the MD5 of its URL.

The site key store.sources (or $BITS_SOURCE_STORE) names it, e.g.
https://store.example.org/sources/archives. With a store token (bits login,
~/.bits/token) bits may ask the store to fetch a missing archive (?url=): it
answers 202 while it fetches, then 302. Without one, or for a token the store does
not take, the read is anyone's. Any other answer leaves the download to the
recipe's URL (bits-store README: sources).
"""

import os
import re
import time
import urllib.parse

import requests
import urllib3

from bits_helpers import site
from bits_helpers.log import debug, info

TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".bits", "token")
WAIT = 480           # seconds to wait for the store's fetch: under download()'s sentinel wait
_REDIRECTS = (301, 302, 303, 307, 308)
_SEGMENT = re.compile(r"[A-Za-z0-9._~+!@=,:-]{1,255}")   # bits-store's path rules
_told = set()


def configured():
  """The sources store of this site, or None."""
  url = os.environ.get("BITS_SOURCE_STORE") or str(site.get("store.sources") or "")
  return SourceStore(url) if url.strip() else None


def _once(message, *args):
  if message not in _told:
    _told.add(message)
    info(message, *args)


class SourceStore:
  def __init__(self, base, token_file=TOKEN_FILE, wait=WAIT, timeout=(10, 60)):
    self.base = base.strip().rstrip("/")
    self.token_file = token_file
    self.wait = wait
    self.timeout = timeout

  def _token(self):
    """The store token, if any, and only over https or to this host itself."""
    u = urllib.parse.urlsplit(self.base)
    if u.scheme != "https" and u.hostname not in ("localhost", "127.0.0.1", "::1"):
      return None
    try:
      with open(os.path.expanduser(self.token_file)) as f:
        return f.read().strip() or None
    except OSError:
      return None

  def fetch(self, url_checksum, filename, dest_dir, source=None):
    """Put the stored archive in *dest_dir*/*filename*: True if it is there. With
    *source* (an http(s) URL) and a token, the store may fetch a missing one."""
    if filename in (".", "..") or not _SEGMENT.fullmatch(filename):
      return False                              # a name the store cannot hold
    url = "%s/%s/%s/%s" % (self.base, url_checksum[:2], url_checksum, filename)
    token = self._token()
    headers = {"Authorization": "Bearer " + token} if token else {}
    params = {"url": source} if token and _fetchable(source) else None
    deadline = time.monotonic() + self.wait
    try:
      while True:
        r = requests.get(url, params=params, headers=headers, allow_redirects=False, timeout=self.timeout)
        if r.status_code in (401, 403) and headers:
          _once("Source store %s does not take your store token (bits login?): reading as anyone", self.base)
          headers, params = {}, None
          continue
        if r.status_code == 202:
          if time.monotonic() >= deadline:
            info("Source store %s is still fetching %s: downloading it from its URL", self.base, source)
            return False
          debug("Source store %s is fetching %s", self.base, source)
          time.sleep(min(max(_int(r.headers.get("Retry-After"), 5), 1), 60))
          continue
        if r.status_code in _REDIRECTS and r.headers.get("Location"):
          return self._download(urllib.parse.urljoin(url, r.headers["Location"]), dest_dir, filename)
        debug("Source store %s: %s for %s: %s", self.base, r.status_code, filename, r.text[:200])
        return False
    except (requests.RequestException, urllib3.exceptions.HTTPError, OSError) as exc:
      debug("Source store %s: %s", self.base, exc)
      return False

  def _download(self, location, dest_dir, filename):
    """The object a redirect names, written whole or not at all; no token sent."""
    dest = os.path.join(dest_dir, filename)
    tmp = "%s.%d.%s.part" % (dest, os.getpid(), os.urandom(4).hex())
    with requests.get(location, stream=True, timeout=self.timeout) as r:
      if r.status_code != 200:
        debug("Source store: %s for %s", r.status_code, filename)   # a miss: 404
        return False
      os.makedirs(dest_dir, exist_ok=True)
      size = 0
      try:
        with open(tmp, "wb") as f:
          for chunk in r.raw.stream(1 << 20, decode_content=False):   # the bytes as stored
            f.write(chunk)
            size += len(chunk)
        want = r.headers.get("Content-Length")
        if want is not None and size != _int(want, -1):   # urllib3 1.x does not check
          debug("Source store: %d of %s bytes of %s", size, want, filename)
          return False
        os.replace(tmp, dest)
      finally:
        if os.path.exists(tmp):
          os.unlink(tmp)
    return True


def _fetchable(source):
  """May the store be asked to fetch *source*: http(s), without credentials."""
  if not source:
    return False
  u = urllib.parse.urlsplit(source)
  return u.scheme in ("http", "https") and u.username is None and u.password is None


def _int(value, default):
  try:
    return int(value)
  except (TypeError, ValueError):
    return default
