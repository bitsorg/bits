# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Cache of the sources a build downloads itself (issue #129).

A recipe with ``fetch_cache: true``, built with defaults that name a cache
(``system: fetch_cache: <name>``), gets ``$BITS_FETCH_CACHE``: a directory filled
from the store before its build and saved to it after a successful one. The
recipe points its tool there (Bazel ``--repository_cache``, a process directory,
...). The cache is not part of the package hash.

In the store each file is kept once, by its sha256, with the source mirror's
layout and upload rules (``SOURCES/cache/<sha[:2]>/<sha>/fetched``), so versions
share it. The list of files of a package version on an architecture is written
once, by the first build that saves it, and never changed: later builds restore
exactly those files. build_template.sh lists what the build left in the
directory (``$BUILDROOT/fetched-sources.sha256``); that is what is saved.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile

from bits_helpers.log import debug, info, warning
from bits_helpers.sync import source_sync_for, sources_redistributable

LISTING = "fetched-sources.sha256"
BLOB = "fetched"
INDEX = "index.json"
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-f]{64}$")


def cache_name(value):
  """The cache's name from the defaults' ``system: fetch_cache:``, or "" (off)."""
  if value is None or value == "":
    return ""
  name = str(value).strip()
  if isinstance(value, bool) or not _NAME.match(name):
    warning("Ignoring system: fetch_cache: %r: it takes the cache's name ([A-Za-z0-9._-])",
            value)
    return ""
  return name


def enabled(spec, name):
  """Does *spec* use the cache *name*? Only if the recipe asks and the defaults allow."""
  if not spec.get("fetch_cache"):
    return False
  if not name:
    debug("%s asks for fetch_cache, but the defaults name no cache (system: fetch_cache:)",
          spec["package"])
  return bool(name)


def relative_dir(spec, arch):
  """$BITS_FETCH_CACHE, relative to the work directory."""
  return os.path.join("FETCH_CACHE", arch, spec["package"], spec["version"])


def _index_key(name, spec, arch):
  return hashlib.sha256(("bits-fetch-cache\0%s\0%s\0%s\0%s" % (
      name, arch, spec["package"], spec["version"])).encode()).hexdigest()


def _sha256(path):
  h = hashlib.sha256()
  with open(path, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
      h.update(chunk)
  return h.hexdigest()


def _safe_path(rel):
  """*rel* if it is a plain relative path inside the cache, else None."""
  parts = rel.replace("\\", "/").split("/")
  if rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
    return None
  return os.path.join(*parts)


def _inside(directory, path):
  """Is *path* inside *directory* once symlinks are resolved?"""
  top = os.path.realpath(directory)
  return os.path.realpath(path).startswith(top + os.sep)


def _scratch(work_dir):
  """A temporary directory next to the caches (same filesystem, not listed)."""
  top = os.path.join(work_dir, "FETCH_CACHE")
  os.makedirs(top, exist_ok=True)
  return tempfile.TemporaryDirectory(prefix=".tmp-", dir=top)


class State:
  """What restore() found: the stored list ({path: sha256}, None if there is
  none or it could not be read) and the files it got from the store."""

  def __init__(self, files=None, confirmed=()):
    self.files, self.confirmed = files, set(confirmed)


def _fetch(sync, key, filename, tmp):
  """The stored file *key*/*filename* in *tmp*, or None."""
  path = os.path.join(tmp, filename)
  if os.path.exists(path):
    os.unlink(path)                      # some backends refuse to overwrite
  return path if sync.fetch_source(key, filename, tmp) and os.path.isfile(path) else None


def restore(spec, sync_helper, work_dir, name, arch):
  """Fill the package's cache directory with its stored files. Never raises: on a
  problem the build downloads what it misses itself."""
  directory = os.path.join(work_dir, relative_dir(spec, arch))
  sync = source_sync_for(spec, sync_helper)
  state = State()
  try:
    os.makedirs(directory, exist_ok=True)
    with _scratch(work_dir) as tmp:
      index = _fetch(sync, _index_key(name, spec, arch), INDEX, tmp)
      if index is None:
        return state
      with open(index) as f:
        entries = {str(e["path"]): (str(e["sha256"]), int(e["size"])) for e in json.load(f)["files"]}
      state.files = {}
      for path, (sha, size) in sorted(entries.items()):
        rel = _safe_path(path)
        dest = os.path.join(directory, rel or "")
        if rel is None or not _SHA.match(sha) or not _inside(directory, dest):
          warning("Fetch cache of %s: ignoring the entry %r", spec["package"], path)
          continue
        state.files[rel] = sha
        if os.path.isfile(dest) and os.path.getsize(dest) == size:
          continue                       # left by an earlier build in this (trusted) work dir
        try:
          blob = _fetch(sync, sha, BLOB, tmp)
          if blob is None or _sha256(blob) != sha:
            warning("Fetch cache of %s: %s is %s", spec["package"], rel,
                    "not in the store" if blob is None else "corrupt in the store; not used")
            continue
          os.makedirs(os.path.dirname(dest), exist_ok=True)
          os.replace(blob, dest)
          state.confirmed.add(sha)
        except Exception as exc:        # pylint: disable=broad-except
          warning("Fetch cache of %s: could not restore %s (%s)", spec["package"], rel, exc)
    info("Fetch cache of %s %s: %d files (%d from the store)", spec["package"],
         spec["version"], len(state.files), len(state.confirmed))
  except Exception as exc:          # pylint: disable=broad-except
    warning("Fetch cache of %s: could not restore it (%s); the build downloads what it needs",
            spec["package"], exc)
  return state


def _listing(path):
  """{path: sha256} from build_template.sh's sha256sum listing."""
  files = {}
  with open(path) as f:
    for line in f:
      sha, _, rel = line.rstrip("\n").partition("  ")
      rel = _safe_path(rel[2:] if rel.startswith("./") else rel)
      if rel and _SHA.match(sha):
        files[rel] = sha
  return files


def _upload(sync, path, key, filename, tmp):
  """Upload *path* as *key*/*filename* (rsync keeps the local name: link it so)."""
  staged = os.path.join(tmp, filename)
  if os.path.lexists(staged):
    os.unlink(staged)
  try:
    os.link(path, staged)
  except OSError:
    shutil.copyfile(path, staged)
  sync.upload_source(staged, key, filename)


def save(spec, sync_helper, work_dir, name, arch, state, listing):
  """Store the files the build left in its cache directory (*listing*): the
  list, if none is stored yet, and the files it names that the store may lack.
  Never raises."""
  sync = source_sync_for(spec, sync_helper)
  if not getattr(sync, "writeStore", "") or not os.path.isfile(listing):
    return
  if not sources_redistributable(spec):
    debug("Fetch cache of %s not saved: redistributable forbids its sources", spec["package"])
    return
  directory = os.path.join(work_dir, relative_dir(spec, arch))
  try:
    current = _listing(listing)
    files = current if state.files is None else state.files
    extra = sorted(p for p, s in current.items() if state.files is not None
                   and state.files.get(p) != s)
    if extra:
      warning("Fetch cache of %s %s: %d files the build downloaded are not in it (its list "
              "is written once per version and architecture): %s", spec["package"],
              spec["version"], len(extra), ", ".join(extra[:5]) + (", ..." if len(extra) > 5 else ""))
    with _scratch(work_dir) as tmp:
      done = set(state.confirmed)
      for rel, sha in sorted(files.items()):
        path = os.path.join(directory, rel)
        if sha in done or current.get(rel) != sha:
          continue                       # in the store, or not here to upload
        _upload(sync, path, sha, BLOB, tmp)
        done.add(sha)
      if state.files is not None or not files:
        return                           # stored once, and never empty
      index = os.path.join(tmp, "new-" + INDEX)
      with open(index, "w") as f:
        json.dump({"cache": name, "architecture": arch, "package": spec["package"],
                   "version": spec["version"], "hash": spec.get("hash", ""),
                   "files": [{"path": p.replace(os.sep, "/"), "sha256": s,
                              "size": os.path.getsize(os.path.join(directory, p))}
                             for p, s in sorted(files.items())]}, f, indent=1)
      _upload(sync, index, _index_key(name, spec, arch), INDEX, tmp)
    info("Fetch cache of %s %s: saved %d files", spec["package"], spec["version"], len(files))
  except (Exception, SystemExit) as exc:   # upload_source may dieOnError
    warning("Fetch cache of %s: could not save it (%s)", spec["package"], exc)
