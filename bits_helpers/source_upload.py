# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits store add-source: an archive no build can download itself (behind a licence
page, or without a direct link) put in the site's sources store by hand, under the
URL its recipe names. Builds then take it from there (source_store), checked against
the recipe's checksum as any download; the first upload for a URL stays. One that may
not be passed on goes to the restricted store (--restricted): build jobs read it.
"""

import os
import urllib.parse

from bits_helpers import source_store
from bits_helpers.bits_store_sync import SCHEME, BitsStoreSync
from bits_helpers.checksum import parse_entry, verify_file
from bits_helpers.download import fixUrl, getUrlChecksum


class SourceUploadError(Exception):
  pass


def store_base(store=None, restricted=False):
  """The sources store to upload to: *store*, else the site's (its restricted one)."""
  env, key = (("BITS_SOURCE_STORE_RESTRICTED", "store.sources_restricted") if restricted
              else ("BITS_SOURCE_STORE", "store.sources"))
  base = (store if store is not None else source_store.setting(env, key)).strip().rstrip("/")
  if not base:
    raise SourceUploadError("no sources store: %s, $%s or --store" % (key, env))
  return base


def add_source(entry, path, store=None, insecure=False, restricted=False):
  """Upload *path* as the archive of *entry* (a recipe's sources line: URL, and
  ",sha256:<hex>" when it has one), to the restricted store with *restricted*.
  Returns (the store's URL of it, True when stored, False when the store holds one
  for that URL already)."""
  url, checksum = parse_entry(entry.strip())
  url = fixUrl(url)                     # as download() keys it, and names the file
  u = urllib.parse.urlsplit(url)
  if u.scheme not in ("http", "https") or not u.netloc or u.username or u.password:
    raise SourceUploadError("%s: not an http(s) URL without credentials" % url)
  name = url.rsplit("/", 1)[-1]
  if not name or name in (".", "..") or not source_store._SEGMENT.fullmatch(name):
    raise SourceUploadError("%s: its last segment is not a file name the store can hold" % url)
  if not os.path.isfile(path):
    raise SourceUploadError("%s: no such file" % path)
  if checksum and not verify_file(path, checksum):
    raise SourceUploadError("%s does not match the recipe's checksum %s" % (path, checksum))
  base = store_base(store, restricted)
  h = getUrlChecksum(url)
  target = "%s/%s/%s/%s" % (base, h[:2], h, name)
  stored = BitsStoreSync(SCHEME + base, None, None, insecure=insecure, writable=True)._upload(target, path=path)
  return target, stored is not None
