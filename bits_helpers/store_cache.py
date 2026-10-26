# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""What the remote store was found to hold, remembered between runs.

The build plan lists the store for every package it might reuse (one request
per architecture and hash). The store's garbage collection keeps every tarball
a signed manifest lists, so the plan keeps the listings that found one of those
in the work directory, one file per store (.store-cache/<store digest>.json),
and uses them while the signed manifest still lists the tarball. Listings that
found nothing are not kept: the package may be uploaded at any time.

The file is a cache: read once, written by replacing it (merged with what
another run wrote meanwhile), entries unused for PRUNE_DAYS dropped. A lost or
unreadable file only means asking the store again.
"""

import hashlib
import json
import os
import tempfile
import time

from bits_helpers.log import debug

PRUNE_DAYS = 60
VERSION = 1


def store_id(helper):
  """A name for the store(s) *helper* reads, or "" when it reads none."""
  readers = getattr(helper, "readers", None)
  if readers is not None:
    return "|".join(filter(None, (store_id(r) for r in readers)))
  return str(getattr(helper, "remoteStore", "") or "")


def _today():
  return int(time.time() // 86400)


class StoreCache:
  """{(arch, hash): [tarball names]} the store was found to hold."""

  def __init__(self, work_dir, store):
    self.path = None
    if work_dir and store:
      digest = hashlib.sha256(store.encode()).hexdigest()[:16]
      # A dot directory: bits cleanup takes the other names there for architectures.
      self.path = os.path.join(work_dir, ".store-cache", digest + ".json")
    self.entries, self.dropped = self._read(), set()

  def _read(self):
    if not self.path:
      return {}
    try:
      with open(self.path) as f:
        doc = json.load(f)
    except (OSError, ValueError):
      return {}
    if not isinstance(doc, dict) or doc.get("version") != VERSION:
      return {}
    out = {}
    for k, v in (doc.get("entries") or {}).items():
      if isinstance(v, dict) and isinstance(v.get("names"), list) and v["names"]:
        out[k] = {"names": [str(n) for n in v["names"]], "used": int(v.get("used") or 0)}
    return out

  def get(self, arch, pkg_hash):
    """The tarball names known to be in the store, or None (ask the store)."""
    e = self.entries.get("%s/%s" % (arch, pkg_hash))
    if e is None:
      return None
    e["used"] = _today()
    return list(e["names"])

  def put(self, arch, pkg_hash, names):
    """Remember a listing that found tarballs; one that found none is not kept."""
    if names:
      self.entries["%s/%s" % (arch, pkg_hash)] = {"names": sorted(str(n) for n in names),
                                                   "used": _today()}

  def forget(self, arch, hashes):
    """Drop what is known of *hashes*: the store did not have them after all."""
    for h in hashes:
      key = "%s/%s" % (arch, h)
      self.entries.pop(key, None)
      self.dropped.add(key)

  def save(self):
    """Write the cache (merged with the file as it is now), best-effort."""
    if not self.path:
      return
    merged = self._read()
    merged.update(self.entries)
    for key in self.dropped:
      merged.pop(key, None)
    oldest = _today() - PRUNE_DAYS
    merged = {k: v for k, v in merged.items() if v["used"] >= oldest}
    try:
      os.makedirs(os.path.dirname(self.path), exist_ok=True)
      fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".tmp-")
      try:
        with os.fdopen(fd, "w") as f:
          json.dump({"version": VERSION, "entries": merged}, f, separators=(",", ":"))
        os.replace(tmp, self.path)
      except BaseException:
        os.unlink(tmp)
        raise
    except OSError as exc:
      debug("could not write the store cache %s: %s", self.path, exc)
