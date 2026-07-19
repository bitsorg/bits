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
from datetime import datetime, timedelta, timezone

from bits_helpers import trust
from bits_helpers.log import debug, warning

SCHEMA_VERSION = 1
COMMON_MANIFEST_KIND = "common-manifest"

# Reuse-relevant fields carried per package into the common manifest. hash +
# tarball_sha256 are what trusted_index() keys on; group drives the consumer
# trust policy (own-group + common); the rest aid GC/monitoring.
_PKG_FIELDS = ("package", "version", "revision", "effective_architecture",
               # pkg_family is carried so an install layout
               # (<arch>/<family>/<package>/<version>-<revision>) can be
               # reconstructed from the signed manifest alone, without fetching
               # the per-build BOMs.
               "pkg_family",
               "hash", "tarball", "tarball_sha256", "group",
               # Provenance carried through for store lifecycle management: the
               # build timestamp and the builder host of the object. Combined
               # with the bits fingerprint injected from the manifest level
               # (bits_version / bits_dist_hash), these let `bits store` prune by
               # "built with bits version/dist < X" or "built before <date>"
               # rather than only by S3 upload time (LastModified).
               "completed_at", "built_by")


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
                    man = json.load(fh)
            except Exception as exc:
                warning("certify: skipping unreadable manifest %s (%s)", p, exc)
                continue
            # In a manifests repo the group is the directory:
            # manifests/<group>/<build_id>.json. Record it so merge tags entries
            # by their directory rather than defaulting untagged ones to 'common'.
            if isinstance(man, dict):
                parts = os.path.normpath(p).split(os.sep)
                if len(parts) >= 3 and parts[-3] == "manifests":
                    man.setdefault("_source_group", parts[-2])
            out.append(man)
    return out


def bom_architecture(man) -> "str | None":
    """Return the single effective architecture a build manifest (BOM) is for.

    A BOM is per-platform by construction: ``bits publish`` partitions a build's
    entries by ``effective_architecture`` and emits one BOM per architecture
    ("shared" — noarch — is just another architecture). This is what lets
    certification be scoped per platform instead of scanning every manifest:
    a BOM pairs with exactly one ``common-manifest-<arch>.json``.

    Returns the architecture, or None when the BOM has no certifiable entry
    (nothing carrying both ``hash`` and ``tarball_sha256``). A BOM mixing more
    than one architecture violates the invariant and raises :class:`CertifyError`
    — re-publish it with a bits that splits BOMs per architecture.
    """
    if not isinstance(man, dict):
        return None
    archs = {(e.get("effective_architecture") or "shared")
             for e in man.get("packages") or []
             if isinstance(e, dict) and e.get("hash") and e.get("tarball_sha256")}
    if not archs:
        return None
    if len(archs) > 1:
        raise CertifyError(
            "build manifest %s mixes architectures (%s): a BOM must be "
            "per-platform — re-publish with a bits version that emits one BOM "
            "per effective architecture"
            % (man.get("build_id") or "?", ", ".join(sorted(archs))))
    return archs.pop()


def _expiry_iso(valid_days):
    """ISO-8601 UTC timestamp *valid_days* from now, or None to never expire."""
    if not valid_days:
        return None
    when = datetime.now(timezone.utc) + timedelta(days=valid_days)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def merge_common_manifest(manifests, default_group=None, valid_days=None,
                          source_commit=None) -> dict:
    """Merge build manifests into one common manifest, deduped by content hash.

    Only packages carrying both a ``hash`` and a ``tarball_sha256`` can be
    certified for reuse; others are skipped. Two entries sharing a hash must
    agree on ``tarball_sha256`` — a mismatch means one of them is wrong about
    what those bytes are, so we refuse (fail-closed) rather than sign ambiguity.

    *default_group* stamps a ``group`` on entries that don't already carry one,
    so a per-group certification tags its batch for the consumer trust filter.
    """
    # The store is content-addressed *per architecture*: a package's tarball
    # lives at TARS/<effective_architecture>/store/<hash>/... So the identity of
    # a stored object is (effective_architecture, hash), NOT the hash alone. Two
    # builds that share a hash but differ in effective_architecture are two
    # distinct objects in two distinct trees and must NOT be treated as a
    # conflict — that includes the common case where the package's hash does not
    # depend on the architecture (e.g. a package taken partly from the host, or
    # the same recipe/deps on two platforms). We therefore key dedup and conflict
    # detection on (effective_architecture, hash). Within a single tree the
    # mapping hash -> bytes must stay 1:1, so a genuine same-arch/same-hash but
    # different-sha collision (including a noarch "shared" package packaged
    # non-reproducibly on two platforms into the one shared tree) is still fatal.
    by_key = {}
    src_by_key = {}           # (arch, hash) -> build_id that first supplied it
    sources = []
    for man in manifests:
        if not isinstance(man, dict):
            continue
        bid = man.get("build_id")
        if bid and bid not in sources:
            sources.append(bid)
        # Group precedence: explicit entry group > the manifest's directory group
        # (manifests/<group>/) > the run-wide --group. Prevents untagged entries
        # from silently becoming cluster-wide 'common'.
        man_group = man.get("_source_group") or default_group
        for e in man.get("packages") or []:
            if not isinstance(e, dict):
                continue
            h = e.get("hash")
            sha = e.get("tarball_sha256")
            if not h or not sha:
                continue
            arch = e.get("effective_architecture") or ""
            key = (arch, h)
            entry = {k: e[k] for k in _PKG_FIELDS if k in e}
            if not entry.get("group") and man_group:
                entry["group"] = man_group
            # Stamp the bits fingerprint of the build that produced this object.
            # These live at the manifest level (one bits version per build), so
            # copy them onto the per-package entry — the common manifest merges
            # many builds, and each object must carry its own provenance for
            # store lifecycle tooling to prune by bits version/date.
            for _fld in ("bits_version", "bits_dist_hash"):
                if man.get(_fld) and _fld not in entry:
                    entry[_fld] = man[_fld]
            prev = by_key.get(key)
            if prev is None:
                by_key[key] = entry
                src_by_key[key] = bid
            elif _norm_sha(prev.get("tarball_sha256")) != _norm_sha(sha):
                raise CertifyConflict(
                    "package %s (hash %s, architecture %r) has conflicting "
                    "tarball_sha256 between builds %r and %r: %s vs %s. The same "
                    "package hash was built to different bytes within one "
                    "architecture tree — a non-reproducible build, or a differing "
                    "host toolchain/system library on the two build nodes for this "
                    "architecture. Remove one of the two build manifests from "
                    "manifests/ and re-certify."
                    % (e.get("package", "?"), h, arch or "shared",
                       src_by_key.get(key) or "?", bid or "?",
                       prev.get("tarball_sha256"), sha))
    packages = [by_key[k] for k in sorted(by_key)]
    common = {
        "schema_version": SCHEMA_VERSION,
        "kind": COMMON_MANIFEST_KIND,
        "created_at": _now_iso(),
        "sources": sources,
        "packages": packages,
    }
    # Offline freshness (P5): source_commit ties the signature to a point in the
    # manifests-repo history (anti-rollback, git-native); expires bounds how long
    # a signed manifest is trusted so a stale one can't be replayed forever.
    if source_commit:
        common["source_commit"] = source_commit
    expires = _expiry_iso(valid_days)
    if expires:
        common["expires"] = expires
    return common


def validate_against_store(common, probe):
    """Check every package in *common* against the real store.

    *probe* is ``probe(effective_architecture, hash, tarball) -> 'sha256:HEX' |
    HEX | None`` returning the digest computed from the *actual* stored object
    named by *tarball* (None if it is absent or ambiguous).

    Returns ``(fatal, missing)``:

    - *fatal* — human-readable sha256-mismatch problems: the object exists but
      its bytes differ from the manifest's claim. Never sign such a manifest.
    - *missing* — the package dicts whose store object is absent. They cannot be
      vouched for, but a missing object is not the manifest lying about existing
      bytes, so the caller may DROP them and sign the rest (graceful handling of
      a store that has been GC'd, wiped, partially uploaded, or migrated).

    ``([], [])`` means the manifest is fully backed by the store and safe to sign.
    """
    fatal, missing = [], []
    for p in common.get("packages") or []:
        h = p.get("hash")
        arch = p.get("effective_architecture") or ""
        claimed = _norm_sha(p.get("tarball_sha256"))
        actual = probe(arch, h, p.get("tarball"))
        if actual is None:
            # The store has no object for this hash. We cannot vouch for it, but
            # its ABSENCE is not the manifest lying about existing bytes — a
            # store can legitimately lose objects (GC, cleanup/wipe, a partial
            # upload, a layout migration). So this is DROPPABLE, not fatal: the
            # caller removes the entry and signs the rest.
            missing.append(p)
        elif _norm_sha(actual) != claimed:
            # The object exists but its bytes differ from what the manifest
            # claims. This IS the manifest lying about the store — corruption or
            # tampering — and must never be signed. Fatal.
            fatal.append(
                "sha256 mismatch for %s %s: manifest=%s store=%s"
                % (p.get("package", "?"), h, claimed, _norm_sha(actual)))
    return fatal, missing


def certify(manifests, key_pem_path, out_path, probe=None, sig_path=None,
            default_group=None, valid_days=None, source_commit=None,
            approval_check=None) -> tuple:
    """Merge → (approve) → (store-validate) → sign. Returns ``(out_path, sig_path)``.

    Raises :class:`CertifyConflict` on a hash/sha256 conflict and
    :class:`CertifyError` if store validation finds any problem. When *probe* is
    None the store-validation step is skipped (use only when the caller has
    already validated, e.g. a dry merge). *default_group* tags untagged entries;
    *valid_days*/*source_commit* stamp the offline-freshness fields (P5).
    *approval_check(common) -> approvers* gates on group-admin approval and, when
    it returns approver usernames, they are recorded as ``certified_by`` so the
    identity that authorised the certification travels with the signature.
    """
    common = _prepare_common(manifests, key_pem_path, probe, default_group,
                             valid_days, source_commit, approval_check)
    out_abs, sig_path = _write_signed(common, key_pem_path, out_path, sig_path)
    debug("certify: signed common manifest %s (%d pkgs) -> %s",
          out_abs, len(common["packages"]), sig_path)
    return out_abs, sig_path


def _drop_local_revisions(common) -> list:
    """Remove ``local*``-revision packages from *common* (in place); return them.

    bits assigns a ``localN`` revision exactly when there is no write store, and
    ``doFinalSync`` never uploads such a tarball. A local-revision package is
    therefore, by construction, NOT in the shared store and can never be
    certified — it is not drift, it is a category error. Filtering here (before
    the store probe) keeps laptop/no-write-store BOMs from turning every
    certification into hundreds of "absent from store" warnings, and keeps
    unreusable entries out of the signed manifest.
    """
    pkgs = common.get("packages") or []
    local = [p for p in pkgs
             if str(p.get("revision") or "").startswith("local")]
    if not local:
        return []
    common["packages"] = [p for p in pkgs
                          if not str(p.get("revision") or "").startswith("local")]
    warning("certify: skipping %d local-revision package(s) — a 'localN' revision "
            "is only assigned when there is no write store, so the tarball was "
            "never uploaded and can never be certified. First few: %s%s",
            len(local),
            ", ".join("%s@%s-%s" % (p.get("package", "?"), p.get("version", "?"),
                                    p.get("revision")) for p in local[:5]),
            " …" if len(local) > 5 else "")
    for p in local:
        debug("certify: skipping local revision: %s@%s-%s (%s)",
              p.get("package", "?"), p.get("version", "?"), p.get("revision"),
              p.get("effective_architecture") or "shared")
    return local


def _prepare_common(manifests, key_pem_path, probe, default_group, valid_days,
                    source_commit, approval_check) -> dict:
    """Merge → (approve) → (store-validate) → key-policy check. Returns the
    validated common-manifest dict (with certified_by/at stamped), ready to
    write. Raises :class:`CertifyConflict`/:class:`CertifyError` on any problem.
    """
    common = merge_common_manifest(load_build_manifests(manifests), default_group,
                                   valid_days=valid_days, source_commit=source_commit)
    _drop_local_revisions(common)
    certified_by = None
    if approval_check is not None:
        certified_by = approval_check(common)
    if probe is not None:
        fatal, missing = validate_against_store(common, probe)
        if missing:
            # Graceful handling of store/manifest drift (e.g. a wiped or GC'd
            # store): a package whose object is gone cannot be certified, so drop
            # it and sign the rest rather than failing the WHOLE group. Reported as
            # ONE summary (a drifted group can be hundreds of packages), with the
            # per-package detail at debug level. Never fatal.
            drop = {id(p) for p in missing}
            warning("certify: %d package(s) are not in the store — dropping them "
                    "from the signed manifest (they are not reusable) and certifying "
                    "the remaining %d. First few: %s%s",
                    len(missing), len(common["packages"]) - len(missing),
                    ", ".join("%s@%s" % (p.get("package", "?"), p.get("version", "?"))
                              for p in missing[:5]),
                    " …" if len(missing) > 5 else "")
            for p in missing:
                debug("certify: not in store: %s@%s (hash %s, %s)",
                      p.get("package", "?"), p.get("version", "?"), p.get("hash"),
                      p.get("effective_architecture") or "shared")
            common["packages"] = [p for p in common["packages"] if id(p) not in drop]
        if fatal:
            # A byte-level lie about the store — corruption/tampering. Fail closed.
            raise CertifyError("store validation failed:\n  " + "\n  ".join(fatal))
    # Producer-side per-key group binding: refuse to sign groups this key is not
    # authorised for, so an unauthorised signature is never even produced.
    policy = trust.load_key_policy()
    if policy is not None:
        kid = trust.key_id(trust.load_private_key(key_pem_path).public_key())
        bad = sorted({(p.get("group") or "common") for p in common["packages"]
                      if not trust.key_authorized(kid, p.get("group"), policy)})
        if bad:
            raise CertifyError(
                "signing key %s is not authorised to certify group(s): %s"
                % (kid, ", ".join(bad)))
    if certified_by:
        common["certified_by"] = sorted(set(certified_by))
        common["certified_at"] = _now_iso()
    return common


def _write_signed(common, key_pem_path, out_path, sig_path=None) -> tuple:
    """Atomically write *common* as JSON and sign it. A failed signing must never
    leave an *unsigned* manifest at *out_path*: sign the temp file, then move both
    into place. Returns ``(out_path, sig_path)``.
    """
    out_abs = os.path.abspath(out_path)
    os.makedirs(os.path.dirname(out_abs), exist_ok=True)
    sig_path = sig_path or (out_abs + ".sig")
    tmp = out_abs + ".tmp"
    tmp_sig = tmp + ".sig"
    try:
        with open(tmp, "w") as fh:
            json.dump(common, fh, indent=1, sort_keys=True)
        trust.sign_manifest(tmp, key_pem_path, tmp_sig)
        os.replace(tmp, out_abs)
        os.replace(tmp_sig, sig_path)
    except BaseException:
        for stale in (tmp, tmp_sig):
            try:
                os.remove(stale)
            except OSError:
                pass
        raise
    return out_abs, sig_path


def _arch_stem(out_path, arch) -> str:
    """``.../common-manifest.json`` + ``slc7_x86-64`` -> ``.../common-manifest-slc7_x86-64.json``."""
    stem, ext = os.path.splitext(out_path)
    return "%s-%s%s" % (stem, arch, ext or ".json")


def certify_by_arch(manifests, key_pem_path, out_path, probe=None,
                    default_group=None, valid_days=None, source_commit=None,
                    approval_check=None, only_archs=None) -> list:
    """Certify per platform and emit one signed manifest per architecture.

    Certification is scoped by platform: object identity in the store is
    ``(effective_architecture, hash)``, so entries from different architectures
    can never conflict and there is nothing cross-arch to validate. Each BOM is
    per-platform (see :func:`bom_architecture`) and pairs with exactly one
    ``common-manifest-<arch>.json``; ``"shared"`` (noarch) is just another
    architecture.

    *only_archs* — an iterable of architecture strings — limits the run to those
    platforms: only their BOMs are merged and store-validated, only their files
    are written/signed, and other platforms' previously signed manifests are
    untouched (a CI run for a changed platform neither re-validates nor blocks —
    nor is blocked by — the others). An arch in *only_archs* whose BOMs are all
    gone still gets an EMPTY signed manifest, so deleting a platform's BOMs
    revokes its entries. With *only_archs* None, every architecture present is
    certified (full re-derivation).

    Within the scoped set a sha256 mismatch still fails the whole run
    (fail-closed); a package merely absent from the store is dropped, not fatal —
    see ``_prepare_common``. Returns ``[(out_path, sig_path, arch), ...]``
    sorted by arch.
    """
    loaded = load_build_manifests(manifests)
    if only_archs is not None:
        only = {a.strip() for a in only_archs if a and a.strip()}
        kept = []
        for man in loaded:
            arch = bom_architecture(man)   # also enforces the one-arch invariant
            if arch in only:
                kept.append(man)
        debug("certify: scoped to %s — %d of %d manifest(s) kept",
              ", ".join(sorted(only)), len(kept), len(loaded))
        loaded = kept
    common = _prepare_common(loaded, key_pem_path, probe, default_group,
                             valid_days, source_commit, approval_check)
    buckets = {}
    for p in common["packages"]:
        buckets.setdefault(p.get("effective_architecture") or "shared", []).append(p)
    # Every scoped arch gets a file even when its packages are gone: an empty
    # signed manifest is the revocation of that platform's entries.
    if only_archs is not None:
        for arch in only:
            buckets.setdefault(arch, [])
    # Always emit at least the shared file so consumers/GC have a well-formed,
    # signed artifact to read even when a certification is (transiently) empty.
    if not buckets:
        buckets = {"shared": []}
    outputs = []
    for arch in sorted(buckets):
        sub = dict(common)
        sub["architecture"] = arch
        sub["packages"] = buckets[arch]
        op, sp = _write_signed(sub, key_pem_path, _arch_stem(out_path, arch))
        debug("certify: signed %s manifest %s (%d pkgs)", arch, op, len(buckets[arch]))
        outputs.append((op, sp, arch))
    return outputs


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

    def probe(arch, h, tarball=None):
        prefix = resolve_store_path(arch or default_arch, h) + "/"
        key = None
        # Prefer the exact object the manifest names, so we validate *those* bytes
        # and never hash a different tarball that happens to share the hash dir.
        if tarball:
            cand = prefix + tarball
            try:
                s3.head_object(Bucket=bucket, Key=cand)
                key = cand
            except Exception:
                key = None
        if key is None:
            try:
                listing = s3.list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents", [])
            except Exception as exc:
                warning("certify: store list failed for %s (%s)", prefix, exc)
                return None
            tars = [o["Key"] for o in listing if o["Key"].endswith(".tar.gz")]
            # Only fall back when there is exactly one tarball: an ambiguous dir
            # must not be validated against an arbitrarily-chosen object.
            if len(tars) != 1:
                if tarball:
                    # Detail only: _prepare_common reports one summary for all the
                    # packages it drops, so this must not be a per-package warning.
                    debug("certify: named tarball %s absent under %s (found %d .tar.gz)",
                          tarball, prefix, len(tars))
                return None
            key = tars[0]
        digest = hashlib.sha256()
        body = s3.get_object(Bucket=bucket, Key=key)["Body"]
        for chunk in iter(lambda: body.read(1 << 20), b""):
            digest.update(chunk)
        return "sha256:" + digest.hexdigest()

    return probe


def _make_approval_check(args, parser):
    """Build an ``approval_check(common) -> approvers`` gate for certify().

    Verifies, per group being certified, that an overall bits-admin or that
    group's admin approved the merge request (forge identity). *unmet* groups
    abort via *parser*. Returns the approver usernames so certify records them.
    """
    from bits_helpers import forge as _forge
    from bits_helpers.log import info
    if not getattr(args, "admins", None):
        parser.error("--require-approval needs --admins FILE (admin policy)")
    policy = _forge.load_admin_policy(args.admins)
    if not policy:
        parser.error("no admins parsed from %s" % args.admins)
    changed = [g.strip() for g in (getattr(args, "changedGroups", None) or "").split(",")
               if g.strip()]
    cert_token = getattr(args, "certifierToken", None) or os.environ.get("BITS_CERTIFIER_TOKEN")
    api_url = os.environ.get("CI_API_V4_URL") or getattr(args, "apiUrl", None)
    # A pre-authenticated identity: GitLab sets GITLAB_USER_LOGIN to the user who
    # triggered an API pipeline (unforgeable inside CI), so we trust it directly.
    certifier = getattr(args, "certifier", None) or os.environ.get("GITLAB_USER_LOGIN")
    grouprefs = _forge.admin_policy_grouprefs(policy)

    def _needed(common):
        present = {(p.get("group") or "common") for p in common.get("packages", [])}
        # Scope: the groups changed in this MR when known, else every group
        # present. Overall-admin authority satisfies any group.
        return (set(changed) & present) if changed else present

    def _resolved(token):
        # Expand any &group refs in the admin policy to live GitLab members;
        # literal usernames remain as a manual override.
        if not grouprefs:
            return policy
        if not (api_url and token):
            parser.error("--admins references GitLab group(s) %s but no API URL/token "
                         "is available to resolve them" % ", ".join(sorted(grouprefs)))
        return _forge.resolve_admin_policy(policy, _forge.make_group_resolver(api_url, token))

    def _check(common):
        needed = _needed(common)
        # Best: a pre-authenticated identity (GitLab already verified the PAT when
        # it created the pipeline; GITLAB_USER_LOGIN is that user). No API call.
        if certifier:
            resolved = _resolved(cert_token or os.environ.get("BITS_FORGE_TOKEN"))
            unmet = sorted(g for g in needed
                           if not _forge.approved_for_group([certifier], resolved, g))
            if unmet:
                parser.error("refusing to certify: %s is not authorised for "
                             "group(s): %s" % (certifier, ", ".join(unmet)))
            info("Certification authorised by GitLab user %s (pipeline initiator)",
                 certifier)
            return [certifier]
        # Otherwise the initiator's own PAT identifies them (GET /user) — an
        # authenticated, unforgeable identity — and we require that person to be
        # an authorised admin for the group(s) being certified.
        if cert_token and api_url:
            user = _forge.gitlab_identify(api_url, cert_token)
            if not user:
                parser.error("--require-approval: could not identify the certifier "
                             "from the provided token (GET /user failed)")
            resolved = _resolved(cert_token)
            unmet = sorted(g for g in needed
                           if not _forge.approved_for_group([user], resolved, g))
            if unmet:
                parser.error("refusing to certify: %s is not authorised for "
                             "group(s): %s" % (user, ", ".join(unmet)))
            info("Certification authorised by GitLab user %s", user)
            return [user]
        # Fallback: read who approved the merge request via a read-only bot token.
        fg = _forge.forge_from_env()
        if fg is None:
            parser.error("--require-approval: no certifier token and no forge "
                         "merge-request context in the environment")
        try:
            ok, approvers, unmet = _forge.verify_group_approval(
                fg, _resolved(getattr(fg, "token", None)), needed)
        except Exception as exc:
            parser.error("could not read approvals from the forge: %s" % exc)
        if not ok:
            parser.error("refusing to certify %s: no authorised approval for "
                         "group(s) %s (approvers: %s)"
                         % (fg.context(), ", ".join(unmet),
                            ", ".join(sorted(approvers)) or "none"))
        info("Approval verified for %s by %s", fg.context(),
             ", ".join(sorted(approvers)))
        return sorted(approvers)

    return _check


def doCertify(args, parser):
    """CLI entrypoint for ``bits certify`` (forge-agnostic; CI wraps this)."""
    approval_check = None
    if getattr(args, "requireApproval", False):
        approval_check = _make_approval_check(args, parser)
    sources = list(getattr(args, "manifests", None) or [])
    if not sources:
        sources = [os.path.join(args.workDir, "MANIFESTS")]
    probe = None
    if not getattr(args, "noStoreCheck", False):
        probe = make_s3_probe(args.certifyStore, args.workDir, args.architecture)
    valid_days = getattr(args, "validDays", None)
    if valid_days is not None and valid_days < 0:
        parser.error("--valid-days must be >= 0 (0 means no expiry)")
    source_commit = getattr(args, "sourceCommit", None) or os.environ.get("CI_COMMIT_SHA")
    only_archs = None
    if getattr(args, "architectures", None):
        only_archs = [a for a in args.architectures.split(",") if a.strip()]
    try:
        outputs = certify_by_arch(sources, args.key, args.out, probe=probe,
                                  default_group=getattr(args, "group", None),
                                  valid_days=valid_days,
                                  source_commit=source_commit,
                                  approval_check=approval_check,
                                  only_archs=only_archs)
    except CertifyError as exc:
        parser.error(str(exc))
    from bits_helpers.log import banner
    for out_path, sig_path, arch in outputs:
        banner("Certified %s manifest -> %s (signature: %s)", arch, out_path, sig_path)
