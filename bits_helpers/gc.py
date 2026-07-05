# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reachability garbage collection for the shared content-addressed store.

Baseline, correctness-preserving growth control (ADR-0004 §6). The roots are
every content hash in the *verified* signed common manifest — its key set is
exactly the set of live artifact hashes. Any store object whose hash is not a
root is sweepable once it is older than a grace period. Because objects are
shared across builds and keyed by content, this hash-level accounting handles
sharing for free: dropping a build's roots never deletes an object another
certified build still references.

Safety: GC is *fail-closed*. If the common manifest cannot be signature-verified,
or verifies to an empty root set, we refuse to sweep — never turn an
unverifiable manifest into "delete everything".
"""

import datetime
import re

from bits_helpers import trust
from bits_helpers.log import debug, warning

# A deletable store key is EXACTLY one payload file inside the content-addressed
# store tree: TARS/<arch>/store/<shard>/<hash>/<file>. The shard must be the
# hash's first two chars, and no segment may contain whitespace or path-control
# characters. Anything not matching this is never a delete candidate — deletion
# is restricted to this well-defined namespace and done one object at a time.
_STORE_KEY_RE = re.compile(
    r"^TARS/[0-9A-Za-z][0-9A-Za-z._+-]*/store/"
    r"([0-9A-Za-z]{2})/([0-9A-Za-z][0-9A-Za-z._-]*)/[0-9A-Za-z][0-9A-Za-z._+-]*$")


def safe_store_key(key, architecture=None) -> bool:
    """True only if *key* is a well-formed, safe-to-delete store payload key.

    Guards against deleting the wrong object: the key must sit under
    ``TARS/<arch>/store/<shard>/<hash>/<file>``, carry no whitespace or control
    characters, and have ``shard == hash[:2]``. When *architecture* is given the
    key must also be under that arch's store tree.
    """
    if not isinstance(key, str) or key != key.strip():
        return False
    if any((c.isspace() or not c.isprintable()) for c in key):
        return False
    m = _STORE_KEY_RE.match(key)
    if not m:
        return False
    shard, h = m.group(1), m.group(2)
    if not h.startswith(shard):
        return False
    if architecture is not None and not key.startswith("TARS/%s/store/" % architecture):
        return False
    return True


def _epoch(when) -> float:
    """Seconds-since-epoch for a datetime or a numeric timestamp."""
    if isinstance(when, datetime.datetime):
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return when.timestamp()
    return float(when)


def hash_from_store_key(key: str):
    """Extract the content hash from a store key, or None if it isn't one.

    Store layout: ``TARS/<arch>/store/<h2>/<hash>/<file>``. The hash is the path
    segment two levels below ``store``.
    """
    parts = str(key).split("/")
    try:
        i = parts.index("store")
    except ValueError:
        return None
    if len(parts) > i + 2 and parts[i + 1] and parts[i + 2]:
        return parts[i + 2]
    return None


def reachable_hashes(common_manifest) -> set:
    """The root hash set: every ``hash`` in the common manifest's packages."""
    roots = set()
    for e in (common_manifest.get("packages") or []) if isinstance(common_manifest, dict) else []:
        h = e.get("hash") if isinstance(e, dict) else None
        if h:
            roots.add(h)
    return roots


def plan_sweep(objects, roots, now=None, grace_seconds=0, architecture=None) -> dict:
    """Decide which store objects to sweep.

    *objects* is an iterable of ``(key, last_modified)`` where last_modified is a
    datetime or epoch seconds. An object is a delete candidate only when its key
    is a safe, well-formed store payload key (see :func:`safe_store_key`), its
    hash is not in *roots*, AND it is at least *grace_seconds* old (so an artifact
    just uploaded by an in-flight build, not yet in any signed manifest, is not
    raced away). Keys that don't validate are counted as ``unsafe`` and never
    deleted.

    Returns ``{"delete": [keys], "kept": n, "young": n, "unsafe": n}``.
    """
    now = _epoch(now if now is not None else datetime.datetime.now(datetime.timezone.utc))
    delete, kept, young, unsafe = [], 0, 0, 0
    for key, last_modified in objects:
        if not safe_store_key(key, architecture):
            unsafe += 1
            continue
        h = hash_from_store_key(key)
        if h in roots:
            kept += 1
            continue
        if now - _epoch(last_modified) < grace_seconds:
            young += 1
            continue
        delete.append(key)
    return {"delete": delete, "kept": kept, "young": young, "unsafe": unsafe}


def verified_roots(manifest_path, sig_path=None, dirs=None):
    """Verify the signed common manifest and return its root hash set.

    Returns ``None`` when the signature is missing/untrusted — the caller MUST
    treat that as "do not sweep", never as "no roots".
    """
    kid, index = trust.trusted_index(manifest_path, sig_path, dirs)
    if not kid:
        return None
    return set(index.keys())


def collect_garbage(objects, manifest_path, sig_path=None, dirs=None,
                    grace_seconds=0, now=None, delete_fn=None,
                    dry_run=True, allow_empty=False, architecture=None) -> dict:
    """Verify roots, plan the sweep, and (unless dry-run) delete.

    *objects* is an iterable of ``(key, last_modified)`` for the store's payload
    tree. *delete_fn* takes a list of keys and removes them one object at a time.
    Fail-closed: unverifiable manifest -> refuse; empty root set -> refuse unless
    *allow_empty*. Only keys that pass :func:`safe_store_key` (optionally bound to
    *architecture*) are ever deleted. Returns the plan dict augmented with
    ``deleted`` and ``verified`` flags.
    """
    roots = verified_roots(manifest_path, sig_path, dirs)
    if roots is None:
        warning("gc: refusing to sweep — common manifest %s did not verify",
                manifest_path)
        return {"verified": False, "delete": [], "deleted": 0,
                "kept": 0, "young": 0, "unsafe": 0}
    if not roots and not allow_empty:
        warning("gc: refusing to sweep — verified manifest has no roots "
                "(use allow_empty to override)")
        return {"verified": True, "delete": [], "deleted": 0,
                "kept": 0, "young": 0, "unsafe": 0}
    plan = plan_sweep(objects, roots, now=now, grace_seconds=grace_seconds,
                      architecture=architecture)
    plan["verified"] = True
    plan["deleted"] = 0
    if not dry_run and plan["delete"] and delete_fn is not None:
        plan["deleted"] = delete_fn(plan["delete"])
    debug("gc: roots=%d kept=%d young=%d unsafe=%d sweep=%d%s",
          len(roots), plan["kept"], plan["young"], plan["unsafe"],
          len(plan["delete"]), " (dry-run)" if dry_run else "")
    return plan


# ── S3-backed store access ────────────────────────────────────────────────────

def gather_s3_store_objects(s3, bucket, architecture):
    """Yield ``(key, LastModified)`` for every object under the arch's store tree."""
    prefix = "TARS/%s/store/" % architecture
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
    for pg in pages:
        for item in pg.get("Contents", ()):
            yield item["Key"], item["LastModified"]


def make_s3_deleter(s3, bucket, architecture=None):
    """Return a ``delete_fn(keys)`` that removes objects one at a time.

    Deliberately not a bulk/prefix delete: each key is deleted individually with
    ``delete_object`` after re-validating it with :func:`safe_store_key` (defense
    in depth — an unsafe key is skipped, never removed). Returns the count
    actually deleted.
    """
    def delete_fn(keys):
        deleted = 0
        for key in keys:
            if not safe_store_key(key, architecture):
                warning("gc: skipping unsafe key, not deleting: %r", key)
                continue
            s3.delete_object(Bucket=bucket, Key=key)
            deleted += 1
        return deleted
    return delete_fn


def doGc(args, parser):
    """CLI entrypoint for ``bits gc``."""
    from bits_helpers.publish import _normalize_s3_store
    from bits_helpers.sync import remote_from_url
    from bits_helpers.log import banner

    store = _normalize_s3_store(args.gcStore)
    writer = remote_from_url(store, store, args.architecture, args.workDir)
    bucket = getattr(writer, "remoteStore", None) or getattr(writer, "writeStore", None)
    s3 = writer.s3
    objects = gather_s3_store_objects(s3, bucket, args.architecture)
    plan = collect_garbage(
        objects, args.trustManifest,
        grace_seconds=int(args.graceDays * 86400),
        delete_fn=make_s3_deleter(s3, bucket, args.architecture),
        dry_run=args.dryRun, allow_empty=args.allowEmpty,
        architecture=args.architecture)
    if not plan["verified"]:
        parser.error("common manifest %s did not verify; nothing swept" % args.trustManifest)
    verb = "would sweep" if args.dryRun else "swept"
    banner("gc: %s %d object(s); kept %d, %d too young, %d unsafe/non-store",
           verb, len(plan["delete"]), plan["kept"], plan["young"], plan["unsafe"])
