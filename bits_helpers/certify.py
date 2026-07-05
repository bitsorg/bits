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
    by_hash = {}
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
            entry = {k: e[k] for k in _PKG_FIELDS if k in e}
            if not entry.get("group") and man_group:
                entry["group"] = man_group
            prev = by_hash.get(h)
            if prev is None:
                by_hash[h] = entry
            elif _norm_sha(prev.get("tarball_sha256")) != _norm_sha(sha):
                raise CertifyConflict(
                    "hash %s has conflicting tarball_sha256: %s vs %s"
                    % (h, prev.get("tarball_sha256"), sha))
    packages = [by_hash[h] for h in sorted(by_hash)]
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


def validate_against_store(common, probe) -> list:
    """Check every package in *common* against the real store.

    *probe* is ``probe(effective_architecture, hash, tarball) -> 'sha256:HEX' |
    HEX | None`` returning the digest computed from the *actual* stored object
    named by *tarball* (None if it is absent or ambiguous). Returns a list of
    human-readable problems; empty means the manifest is fully backed by the
    store and safe to sign.
    """
    problems = []
    for p in common.get("packages") or []:
        h = p.get("hash")
        arch = p.get("effective_architecture") or ""
        claimed = _norm_sha(p.get("tarball_sha256"))
        actual = probe(arch, h, p.get("tarball"))
        if actual is None:
            problems.append("missing from store: %s %s" % (p.get("package", "?"), h))
        elif _norm_sha(actual) != claimed:
            problems.append(
                "sha256 mismatch for %s %s: manifest=%s store=%s"
                % (p.get("package", "?"), h, claimed, _norm_sha(actual)))
    return problems


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
    common = merge_common_manifest(load_build_manifests(manifests), default_group,
                                   valid_days=valid_days, source_commit=source_commit)
    certified_by = None
    if approval_check is not None:
        certified_by = approval_check(common)
    if probe is not None:
        problems = validate_against_store(common, probe)
        if problems:
            raise CertifyError("store validation failed:\n  " + "\n  ".join(problems))
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
    # Write + sign atomically: a failed signing must never leave an *unsigned*
    # manifest sitting at out_path. Sign the temp file, then move both into place.
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
    debug("certify: signed common manifest %s (%d pkgs) -> %s",
          out_abs, len(common["packages"]), sig_path)
    return out_abs, sig_path


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
                    warning("certify: named tarball %s absent under %s (found %d .tar.gz)",
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

    def _needed(common):
        present = {(p.get("group") or "common") for p in common.get("packages", [])}
        # Scope: the groups changed in this MR when known, else every group
        # present. Overall-admin authority satisfies any group.
        return (set(changed) & present) if changed else present

    def _check(common):
        needed = _needed(common)
        # Preferred: the initiator's own PAT identifies them (GET /user) — an
        # authenticated, unforgeable identity — and we require that person to be
        # an authorised admin for the group(s) being certified.
        if cert_token and api_url:
            user = _forge.gitlab_identify(api_url, cert_token)
            if not user:
                parser.error("--require-approval: could not identify the certifier "
                             "from the provided token (GET /user failed)")
            unmet = sorted(g for g in needed
                           if not _forge.approved_for_group([user], policy, g))
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
            ok, approvers, unmet = _forge.verify_group_approval(fg, policy, needed)
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
    try:
        out_path, sig_path = certify(sources, args.key, args.out, probe=probe,
                                     default_group=getattr(args, "group", None),
                                     valid_days=valid_days,
                                     source_commit=source_commit,
                                     approval_check=approval_check)
    except CertifyError as exc:
        parser.error(str(exc))
    from bits_helpers.log import banner
    banner("Certified common manifest -> %s (signature: %s)", out_path, sig_path)
