# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Revision index for the content-addressed store (ADR-0005).

Once the S3 store keeps only hash-keyed tarballs and no version links, the
revision counter can no longer scan ``TARS/<arch>/<pkg>/`` to learn which
``(version, revision)`` labels already exist and which hash each maps to. This
module provides that ``(version -> {revision -> hash})`` map from two sources:

1. the signed **common manifest** (primary, certified) — bits already fetches it
   for signed reuse, and each entry carries ``package/version/revision/
   effective_architecture/hash``; and
2. a **rev-index of write-once markers** (supplement, for uncertified rebuilds):

       MANIFESTS/rev-index/<arch>/<pkg>/<version>-<revision>    (body = hash)

   One tiny object per revision, PUT once by the producer (HEAD-skip), never
   mutated and never a symlink — so it has neither the pointer-consistency nor
   the read-modify-write race of the old version links. Reads are scoped by a
   ``<version>-`` prefix, so the ``<version>-<revision>`` split is unambiguous
   even though versions may contain ``-``.

This module is pure (no S3, no filesystem). The S3 list/get/put and the revision
counter wiring live in sync.py / build.py respectively.
"""

REV_INDEX_ROOT = "MANIFESTS/rev-index"


def marker_prefix(arch, package, version):
  """S3 key prefix under which all revision markers for (arch, pkg, version) live.

  Ending in ``<version>-`` makes the trailing revision unambiguous on read even
  when *version* itself contains ``-`` (e.g. ``v14.2.0-alice2``).
  """
  return "{root}/{arch}/{pkg}/{ver}-".format(
      root=REV_INDEX_ROOT, arch=arch, pkg=package, ver=version)


def marker_key(arch, package, version, revision):
  """Full S3 key of the marker for one (arch, pkg, version, revision)."""
  return marker_prefix(arch, package, version) + str(revision)


def revision_of(key, arch, package, version):
  """Return the revision encoded in *key*, or None if *key* is not such a marker.

  The revision is exactly the part after the ``<version>-`` prefix; a revision
  never contains ``/`` so a key that dives into a sub-path is rejected.
  """
  prefix = marker_prefix(arch, package, version)
  if not key.startswith(prefix):
    return None
  rev = key[len(prefix):]
  return rev if (rev and "/" not in rev) else None


def manifest_records(entries, package, version, arch):
  """``{revision: hash}`` for (package, version, arch) from common-manifest entries.

  *entries* is the ``packages`` list of a loaded common manifest (dicts). Only
  entries matching the package, version and effective_architecture are kept, and
  only those carrying both a revision and a hash.
  """
  out = {}
  for e in entries or ():
    if not isinstance(e, dict):
      continue
    if (e.get("package") == package and e.get("version") == version
            and (e.get("effective_architecture") or "") == arch):
      rev, h = e.get("revision"), e.get("hash")
      if rev and h:
        out[str(rev)] = h
  return out


def merge_records(manifest_recs, marker_recs):
  """Union of the two ``{revision: hash}`` maps; the manifest is authoritative.

  Markers supplement revisions the (certified) manifest hasn't recorded yet; on a
  same-revision conflict the manifest wins, since it is the signed, deduplicated
  source of truth.
  """
  merged = dict(marker_recs or {})
  merged.update(manifest_recs or {})
  return merged
