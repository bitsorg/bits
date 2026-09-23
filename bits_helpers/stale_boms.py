# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Find build manifests (BOMs) the store no longer backs.

A BOM records the sha256 of every store object it published. When the store is
wiped and the same hashes are rebuilt, or two nodes race to upload one object,
an older BOM ends up describing bytes the store does not hold. certify already
settles such conflicts against the store (the stale claim is ignored); this
module lets ``bits store ls|verify|rm --stale-boms`` find and prune those BOMs,
either the copies under the store's MANIFESTS/ or the files in a local
bits-manifests checkout (``--manifests-dir``).

Only a *fully* stale BOM is a prune candidate: no entry matches the store, and
at least one entry is positively contradicted by it (the store holds different
bytes for that hash). A BOM with an entry the store still confirms keeps vouching
for it; an entry that cannot be checked (checksum unreadable, no tarball or
architecture recorded) counts as unknown and also keeps the BOM; and a BOM whose
objects are merely absent is kept too, since absence alone is also what pointing
at the wrong store looks like (certify drops absent entries anyway). Fail-safe:
never delete what we could not check.
"""

import hashlib
import json
import os

OK, STALE, MISSING, UNKNOWN = "ok", "stale", "missing", "unknown"


def _norm_sha(value) -> str:
    s = str(value or "").strip().lower()
    return s.split(":", 1)[1] if ":" in s else s


def judged_entries(bom):
    """The BOM entries certify would consider: a hash and a checksum, and not a
    ``localN`` revision (never uploaded, so its absence says nothing)."""
    for e in bom.get("packages") or []:
        if not isinstance(e, dict):
            continue
        if not (e.get("hash") and e.get("tarball_sha256")):
            continue
        if str(e.get("revision") or "").startswith("local"):
            continue
        yield e


def entry_state(entry, stored) -> str:
    """Compare *entry* with *stored*, the result of looking its object up:
    MISSING (no object at all), STALE (not there, but the hash directory holds
    another tarball), None (checksum unknown) or the stored sha256."""
    if stored in (MISSING, STALE):
        return stored
    if stored is None:
        return UNKNOWN
    return OK if _norm_sha(stored) == _norm_sha(entry.get("tarball_sha256")) else STALE


def classify(bom, lookup) -> dict:
    """Count *bom*'s entry states. *lookup(entry)* returns MISSING, STALE, None
    or the stored sha256 (callers memoise it). ``fully_stale`` is True only when
    no entry is OK or UNKNOWN and at least one is STALE."""
    counts = {OK: 0, STALE: 0, MISSING: 0, UNKNOWN: 0}
    for e in judged_entries(bom):
        if not (e.get("effective_architecture") and e.get("tarball")):
            counts[UNKNOWN] += 1           # certify would guess the object; we don't
            continue
        counts[entry_state(e, lookup(e))] += 1
    counts["judged"] = sum(counts.values())
    counts["fully_stale"] = bool(counts[STALE]) and not (counts[OK] or counts[UNKNOWN])
    return counts


def _absent(s3, bucket, prefix):
    """State of an entry whose named tarball is not in the store: STALE when its
    hash directory holds another tarball, MISSING when it holds none, None when
    the listing fails."""
    try:
        listing = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents") or []
    except Exception:                      # pylint: disable=broad-except
        return None
    return STALE if any(o.get("Key", "").endswith(".tar.gz") for o in listing) else MISSING


def store_lookup(s3, bucket, deep=False):
    """A memoised ``lookup(entry)`` for :func:`classify` backed by the S3 store.

    Returns the sha256 recorded on the object at upload time
    (``x-amz-meta-sha256``); on a 404, STALE when the hash directory holds another
    tarball (the hash was re-uploaded under a different name) and MISSING when
    it is empty; None (unknown) when it cannot be read: any other error, or no
    recorded sha256 unless *deep* asks to hash the object itself.
    """
    from botocore.exceptions import ClientError
    from bits_helpers.sync import _SHA256_META
    from bits_helpers.utilities import resolve_store_path
    cache = {}

    def lookup(entry):
        prefix = resolve_store_path(entry["effective_architecture"], entry["hash"]) + "/"
        key = prefix + entry["tarball"]
        if key not in cache:
            try:
                head = s3.head_object(Bucket=bucket, Key=key)
                sha = (head.get("Metadata") or {}).get(_SHA256_META) or None
                if sha is None and deep:
                    digest = hashlib.sha256()
                    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
                    try:
                        for chunk in iter(lambda: body.read(1 << 20), b""):
                            digest.update(chunk)
                    finally:
                        body.close()
                    sha = "sha256:" + digest.hexdigest()
                cache[key] = sha
            except ClientError as err:
                code = str((err.response.get("Error") or {}).get("Code", ""))
                cache[key] = _absent(s3, bucket, prefix) \
                    if code in ("404", "NoSuchKey", "NotFound") else None
            except Exception:              # pylint: disable=broad-except
                cache[key] = None
        return cache[key]
    return lookup


def load_local_boms(root):
    """BOMs under *root* (a bits-manifests checkout or its manifests/ dir), as
    ``{"key": path, "build_id": ..., "packages": [...]}``. Signed common manifests
    and unreadable files are skipped; ``.git`` is never entered."""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            if not name.endswith(".json"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path) as fh:
                    doc = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(doc, dict) or doc.get("kind") == "common-manifest":
                continue
            pkgs = [p for p in doc.get("packages") or [] if isinstance(p, dict)]
            out.append({"key": path, "build_id": doc.get("build_id") or name,
                        "packages": pkgs})
    return out
