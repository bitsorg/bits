# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Certification core: turn a set of build manifests into a signed common manifest.

This is the forge-agnostic heart of the group-signed trusted-reuse model
(docs/adr/0004-group-signed-trusted-reuse.md). Given the per-build BOM manifests
that ``bits publish`` uploads (MANIFESTS/<build_id>/<host>-<UTC>.json), it:

  1. merges them into one *common manifest* — the trust unit — deduped by content
     hash, refusing to merge if two builds disagree on a hash's tarball_sha256
     (fail-closed);
  2. validates every hash against the actual store (the object exists and its
     bytes hash to the recorded tarball_sha256), via an injected ``probe`` so the
     core stays testable and forge/-store-agnostic;
  3. signs the merged manifest with the release Ed25519 key (bits_helpers.trust),
     producing exactly what a client's ``trust.trusted_index()`` consumes.

A CI job (GitLab first, GitHub later) is a thin wrapper: it verifies MR approvals,
then calls :func:`certify`. Certification here is deliberately store-verified, so
a signature can never outrun what is actually in the bucket.
"""

import glob
import json
import os
from datetime import datetime, timezone

from bits_helpers import trust
from bits_helpers.log import debug, warning

SCHEMA_VERSION = 1
COMMON_MANIFEST_KIND = "common-manifest"

# Reuse-relevant fields carried per package into the common manifest. hash +
# tarball_sha256 are what trusted_index() keys on; group drives the consumer
# trust policy (own-group + common); the rest aid GC/monitoring.
_PKG_FIELDS = ("package", "version", "revision", "effective_architecture",
               "hash", "tarball", "tarball_sha256", "group")


class CertifyError(Exception):
    """Certification cannot proceed (e.g. store validation failed)."""


class CertifyConflict(CertifyError):
    """Two builds disagree on the tarball_sha256 for one content hash."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_sha(value) -> str:
    """Normalise 'sha256:HEX' / 'HEX' to a bare lowercase hex string."""
    s = str(value or "").strip().lower()
    return s.split(":", 1)[1] if ":" in s else s


def load_build_manifests(source) -> list:
    """Return a list of build-manifest dicts from *source*.

    *source* may be a directory (every ``*.json`` under it, recursively, minus
    ``*.sig``), a single file path, or a list of paths and/or already-loaded
    dicts. Unreadable/!JSON files are skipped with a warning, never fatal.
    """
    items = source if isinstance(source, (list, tuple)) else [source]
    out = []
    for it in items:
        if isinstance(it, dict):
            out.append(it)
            continue
        if isinstance(it, str) and os.path.isdir(it):
            paths = sorted(p for p in glob.glob(os.path.join(it, "**", "*.json"),
                                                recursive=True)
                           if not p.endswith(".sig"))
        else:
            paths = [it]
        for p in paths:
            try:
                with open(p) as fh:
                    out.append(json.load(fh))
            except Exception as exc:
                warning("certify: skipping unreadable manifest %s (%s)", p, exc)
    return out


def merge_common_manifest(manifests, default_group=None) -> dict:
    """Merge build manifests into one common manifest, deduped by content hash.

    Only packages carrying both a ``hash`` and a ``tarball_sha256`` can be
    certified for reuse; others are skipped. Two entries sharing a hash must
    agree on ``tarball_sha256`` — a mismatch means one of them is wrong about
    what those bytes are, so we refuse (fail-closed) rather than sign ambiguity.

    *default_group* stamps a ``group`` on entries that don't already carry one,
    so a per-group certification tags its batch for the consumer trust filter.
    """
    by_hash = {}
    sources = []
    for man in manifests:
        if not isinstance(man, dict):
            continue
        bid = man.get("build_id")
        if bid and bid not in sources:
            sources.append(bid)
        for e in man.get("packages") or []:
            if not isinstance(e, dict):
                continue
            h = e.get("hash")
            sha = e.get("tarball_sha256")
            if not h or not sha:
                continue
            entry = {k: e[k] for k in _PKG_FIELDS if k in e}
            if default_group and not entry.get("group"):
                entry["group"] = default_group
            prev = by_hash.get(h)
            if prev is None:
                by_hash[h] = entry
            elif _norm_sha(prev.get("tarball_sha256")) != _norm_sha(sha):
                raise CertifyConflict(
                    "hash %s has conflicting tarball_sha256: %s vs %s"
                    % (h, prev.get("tarball_sha256"), sha))
    packages = [by_hash[h] for h in sorted(by_hash)]
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": COMMON_MANIFEST_KIND,
        "created_at": _now_iso(),
        "sources": sources,
        "packages": packages,
    }


def validate_against_store(common, probe) -> list:
    """Check every package in *common* against the real store.

    *probe* is ``probe(effective_architecture, hash) -> 'sha256:HEX' | HEX | None``
    returning the digest computed from the *actual* stored object (None if it is
    absent). Returns a list of human-readable problems; empty means the manifest
    is fully backed by the store and safe to sign.
    """
    problems = []
    for p in common.get("packages") or []:
        h = p.get("hash")
        arch = p.get("effective_architecture") or ""
        claimed = _norm_sha(p.get("tarball_sha256"))
        actual = probe(arch, h)
        if actual is None:
            problems.append("missing from store: %s %s" % (p.get("package", "?"), h))
        elif _norm_sha(actual) != claimed:
            problems.append(
                "sha256 mismatch for %s %s: manifest=%s store=%s"
                % (p.get("package", "?"), h, claimed, _norm_sha(actual)))
    return problems


def certify(manifests, key_pem_path, out_path, probe=None, sig_path=None,
            default_group=None) -> tuple:
    """Merge → (store-validate) → sign. Returns ``(out_path, sig_path)``.

    Raises :class:`CertifyConflict` on a hash/sha256 conflict and
    :class:`CertifyError` if store validation finds any problem. When *probe* is
    None the store-validation step is skipped (use only when the caller has
    already validated, e.g. a dry merge). *default_group* tags untagged entries.
    """
    common = merge_common_manifest(load_build_manifests(manifests), default_group)
    if probe is not None:
        problems = validate_against_store(common, probe)
        if problems:
            raise CertifyError("store validation failed:\n  " + "\n  ".join(problems))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(common, fh, indent=1, sort_keys=True)
    sig_path = trust.sign_manifest(out_path, key_pem_path, sig_path)
    debug("certify: signed common manifest %s (%d pkgs) -> %s",
          out_path, len(common["packages"]), sig_path)
    return out_path, sig_path


def make_s3_probe(store_url, work_dir, default_arch):
    """Build a ``probe(effective_architecture, hash)`` backed by the S3 store.

    Streams the content-addressed store object and returns its ``sha256:HEX``
    digest (computed from the *actual* bytes, so certification cannot be tricked
    by a manifest that lies about a tarball), or None when the object is absent.
    """
    import hashlib
    from bits_helpers.publish import _normalize_s3_store
    from bits_helpers.sync import remote_from_url
    from bits_helpers.utilities import resolve_store_path

    store = _normalize_s3_store(store_url)
    writer = remote_from_url(store, store, default_arch, work_dir)
    bucket = getattr(writer, "remoteStore", None) or getattr(writer, "writeStore", None)
    s3 = writer.s3

    def probe(arch, h):
        prefix = resolve_store_path(arch or default_arch, h) + "/"
        try:
            listing = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
        except Exception as exc:
            warning("certify: store list failed for %s (%s)", prefix, exc)
            return None
        tars = [o["Key"] for o in listing if o["Key"].endswith(".tar.gz")]
        if not tars:
            return None
        digest = hashlib.sha256()
        body = s3.get_object(Bucket=bucket, Key=tars[0])["Body"]
        for chunk in iter(lambda: body.read(1 << 20), b""):
            digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    return probe


def doCertify(args, parser):
    """CLI entrypoint for ``bits certify`` (forge-agnostic; CI wraps this)."""
    sources = list(getattr(args, "manifests", None) or [])
    if not sources:
        sources = [os.path.join(args.workDir, "MANIFESTS")]
    probe = None
    if not getattr(args, "noStoreCheck", False):
        probe = make_s3_probe(args.certifyStore, args.workDir, args.architecture)
    try:
        out_path, sig_path = certify(sources, args.key, args.out, probe=probe,
                                     default_group=getattr(args, "group", None))
    except CertifyError as exc:
        parser.error(str(exc))
    from bits_helpers.log import banner
    banner("Certified common manifest -> %s (signature: %s)", out_path, sig_path)
