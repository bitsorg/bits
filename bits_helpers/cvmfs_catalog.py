#!/usr/bin/env python3
"""Fast module listing on CVMFS via the serving catalog (no per-file FUSE walk).

`bits q` / `bits avail` enumerate the installed tree to collect modulefiles. On
CVMFS every directory test is a FUSE lookup (cold: catalog fetch + zlib +
SQLite; warm: still a syscall), so a stack with thousands of packages costs tens
of thousands of FUSE operations per call.

A CVMFS catalog already *is* a SQLite database with one row per path. When the
queried directory is served by a single, dedicated nested catalog rooted exactly
there, we can read that catalog's content hash from the cvmfs client's existing
`user.catalog_counters` magic xattr, fetch the one catalog object over HTTP,
decompress it, and run a single local SQLite query to list every entry — no
per-file FUSE walk. See ADR-001 (Option E).

This helper is self-contained (stdlib only) and imports nothing from bits, so it
runs as a plain script:

    python3 bits_helpers/cvmfs_catalog.py <dir> [--regex RE] [--depth2-dirs]

Exit status is a contract for the shell frontend:
    0  success — the listing was printed; use it.
    3  fast path not applicable (not on CVMFS, no dedicated catalog rooted here,
       deeper nested catalogs, or any fetch/parse error). The caller MUST fall
       back to its POSIX walk; a one-line reason is written to stderr.

The fast path is deliberately conservative: it only ever lists from a single
catalog rooted at the queried path with no deeper nested catalogs, so it can
never silently return a partial or oversized listing — it returns everything or
signals fallback.
"""

import argparse
import os
import re
import sqlite3
import struct
import sys
import tempfile
import urllib.request
import zlib

# Catalog entry flags (cvmfs/cvmfs/catalog_sql.h).
kFlagDir = 1
kFlagFile = 4
kFlagLink = 8


class FastPathUnavailable(Exception):
  """Raised when the catalog fast path cannot or must not be used.

  Always means the caller should fall back to the POSIX walk; the message is the
  human-readable reason.
  """


def read_xattr(path, name):
  """Read an extended attribute, via os.getxattr then the attr/getfattr tools."""
  try:
    return os.getxattr(path, name).decode("utf-8", "replace")
  except (OSError, AttributeError):
    pass
  short = name[len("user."):] if name.startswith("user.") else name
  import subprocess
  for cmd in (["attr", "-q", "-g", short, path],
              ["getfattr", "--only-values", "-n", name, path]):
    try:
      out = subprocess.run(cmd, capture_output=True, check=True)
      return out.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.CalledProcessError):
      continue
  raise FastPathUnavailable("no %s xattr (not a CVMFS path?)" % name)


def parse_catalog_counters(text):
  """Parse the catalog_counters xattr into {hash, mountpoint, counters{}}."""
  info = {"hash": None, "mountpoint": None, "counters": {}}
  for line in text.splitlines():
    line = line.strip()
    if not line:
      continue
    if line.startswith("catalog_hash:"):
      info["hash"] = line.split(":", 1)[1].strip()
    elif line.startswith("catalog_mountpoint:"):
      info["mountpoint"] = line.split(":", 1)[1].strip()
    else:
      parts = re.split(r"[,\s]+", line, maxsplit=1)
      if len(parts) == 2 and re.fullmatch(r"-?\d+", parts[1].strip()):
        info["counters"][parts[0].strip()] = int(parts[1].strip())
  return info


def subtree_nested(counters):
  """Best-effort subtree nested-catalog count (None if unknown)."""
  for key in ("subtree.nested", "subtree_nested", "nested"):
    if key in counters:
      return counters[key]
  return None


def data_url_for_hash(host, cat_hash):
  """Build the catalog data-object URL: <host>/data/<2>/<rest>C."""
  h = cat_hash.strip()
  if len(h) < 3:
    raise FastPathUnavailable("implausible catalog hash %r" % h)
  return "%s/data/%s/%sC" % (host.rstrip("/"), h[:2], h[2:])


def fetch_and_decompress(url, timeout=30):
  """Download a cvmfs data object and zlib-decompress it."""
  req = urllib.request.Request(url, headers={"User-Agent": "bits-q-catalog"})
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      blob = resp.read()
  except Exception as exc:  # noqa: BLE001 - any network error => fall back
    raise FastPathUnavailable("could not fetch catalog: %s" % exc)
  for wbits in (zlib.MAX_WBITS, -zlib.MAX_WBITS):
    try:
      return zlib.decompressobj(wbits).decompress(blob)
    except zlib.error:
      continue
  raise FastPathUnavailable("could not decompress catalog at %s" % url)


def _u64(signed):
  """SQLite stores md5path halves as signed 64-bit; key on the raw bits."""
  return struct.unpack("<Q", struct.pack("<q", signed))[0]


def list_from_catalog_db(db_path):
  """Return [(relative_path, flags)] for every entry in a catalog SQLite.

  Paths are relative to the catalog root (its mountpoint). The catalog table is
  keyed by md5path_1/2 with a parent_1/2 link; the root is the node whose parent
  is not present in this catalog.
  """
  con = sqlite3.connect(db_path)
  try:
    nodes = {}  # (m1, m2) -> [name, (p1, p2), flags]
    for name, m1, m2, p1, p2, flags in con.execute(
        "SELECT name, md5path_1, md5path_2, parent_1, parent_2, flags "
        "FROM catalog"):
      nodes[(_u64(m1), _u64(m2))] = [name, (_u64(p1), _u64(p2)), flags]
  except sqlite3.Error as exc:
    raise FastPathUnavailable("unexpected catalog schema: %s" % exc)
  finally:
    con.close()

  results = []
  for _key, (name, parent, flags) in nodes.items():
    parts = [name]
    cur = parent
    guard = 0
    while cur in nodes and guard < 4096:
      pname, pparent, _pf = nodes[cur]
      if pparent == cur:  # defensive: self-loop
        break
      parts.append(pname)
      cur = pparent
      guard += 1
    parts.reverse()
    # parts[0] is the catalog root directory name; drop it so paths are
    # relative to the mountpoint.
    rel = "/".join(parts[1:]) if len(parts) > 1 else ""
    if rel:
      results.append((rel, flags))
  return results


def list_entries(path):
  """Fast-path listing of *path*. Returns (entries, meta) or raises.

  entries: [(relative_path, flags)] for every node under the serving catalog.
  Raises FastPathUnavailable unless a single dedicated catalog is rooted exactly
  at *path* with no deeper nested catalogs.
  """
  info = parse_catalog_counters(read_xattr(path, "user.catalog_counters"))
  if not info["hash"]:
    raise FastPathUnavailable("catalog_counters had no catalog_hash")

  abs_path = os.path.realpath(path)
  mount = info["mountpoint"]
  if not (mount and os.path.realpath(mount) == abs_path):
    raise FastPathUnavailable(
        "serving catalog is rooted at %r, not at %r (would over-list)"
        % (mount, abs_path))
  nested = subtree_nested(info["counters"])
  if nested:
    raise FastPathUnavailable(
        "%d deeper nested catalog(s) — single-fetch listing would be partial"
        % nested)

  host = read_xattr(path, "user.host").strip()
  blob = fetch_and_decompress(data_url_for_hash(host, info["hash"]))
  with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as tf:
    tf.write(blob)
    db_path = tf.name
  try:
    entries = list_from_catalog_db(db_path)
  finally:
    os.unlink(db_path)
  return entries, {"hash": info["hash"], "mountpoint": mount, "host": host}


def main(argv=None):
  ap = argparse.ArgumentParser(
      description="List modules under a CVMFS directory via its serving catalog.")
  ap.add_argument("path", help="mounted CVMFS directory to list")
  ap.add_argument("--regex", help="filter listing (case-insensitive)")
  ap.add_argument("--depth2-dirs", action="store_true",
                  help="print absolute <path>/<a>/<b> directories (a drop-in "
                       "for `find -mindepth 2 -maxdepth 2`); default prints the "
                       "modulefiles (regular files / symlinks)")
  args = ap.parse_args(argv)

  if not os.path.isdir(args.path):
    sys.stderr.write("cvmfs_catalog: not a directory: %s\n" % args.path)
    return 3
  try:
    entries, _meta = list_entries(args.path)
  except FastPathUnavailable as exc:
    sys.stderr.write("cvmfs_catalog: fast path unavailable: %s\n" % exc)
    return 3
  except Exception as exc:  # noqa: BLE001 - never crash the frontend; fall back
    sys.stderr.write("cvmfs_catalog: unexpected error: %s\n" % exc)
    return 3

  if args.depth2_dirs:
    out = [os.path.join(args.path, rel) for rel, flags in entries
           if (flags & kFlagDir) and rel.count("/") == 1]
  else:
    out = [rel for rel, flags in entries
           if (flags & kFlagFile) or (flags & kFlagLink)]
  out.sort()
  if args.regex:
    rx = re.compile(args.regex, re.IGNORECASE)
    out = [m for m in out if rx.search(m)]
  sys.stdout.write("".join(m + "\n" for m in out))
  return 0


if __name__ == "__main__":
  sys.exit(main())
