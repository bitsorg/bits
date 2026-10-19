# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits cvmfs-publish — producer-side staged publish of a build's packages.

This is the Python home for what cvmfs-prepub-publish.yml did per package in
bash: resolve the CVMFS path from the package's own .meta.json, untar, relocate,
relativise absolute symlinks, sanitize, tar, stage (bits cvmfs-stage) and submit
to prepub. Concentrating it here lets the packages of one build be prepared
CONCURRENTLY and biggest-first — the measured lever for publish time — with a
real thread pool instead of hand-rolled bash fan-out, and with unit tests.

Increment 1 (this file): the SINGLE-package pipeline `publish_one`, proven to
reproduce the CI's output (same staging prefix + catalog hash) for one package.
The concurrent biggest-first driver is added next, on top of this.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

from bits_helpers.arch import SHARED_ARCH

# ── pure helpers (unit-testable, no I/O) ─────────────────────────────────────

_TOKENS = ("pkg", "tag", "version", "revision", "platform", "arch",
           "install_dir", "commit", "user", "family")


def expand_tmpl(tmpl, pkg="", tag="", version="", revision="", platform="",
                install_dir="", commit="", user="", family="", arch=""):
    """Port of the CI `_expand_tmpl`: substitute {token}s into a path template.

    {family} carries its OWN trailing slash when non-empty (templates use the
    adjacent form {family}{pkg}); an empty family collapses to just {pkg}.
    {release} is already baked into the template by the build — untouched here.
    {platform} is the console platform (x86_64-el9); {arch} is the package's
    build arch (x86_64-el9-gcc14-opt), which keeps compiler/build types apart.
    """
    fam_seg = (family + "/") if family else ""
    subst = {"pkg": pkg, "tag": tag, "version": version, "revision": revision,
             "platform": platform, "arch": arch, "install_dir": install_dir,
             "commit": commit, "user": user, "family": fam_seg}
    out = tmpl
    for k, v in subst.items():
        out = out.replace("{%s}" % k, v)
    return out


def repo_relative_path(p, repo, meta_root=None, prefix_fallback=None):
    """Port of the CI `_repo_relative_path`: turn an absolute /cvmfs/<repo>/...
    path into a repo-relative lease path, re-rooting a reused artefact whose
    .meta.json root differs from the community prefix. Raises ValueError when the
    result is not under /cvmfs/<repo>/ (a prefix/community mismatch)."""
    # A root at or below the community prefix is already inside it: keep it.
    inside = bool(prefix_fallback and meta_root) and (
        meta_root + "/").startswith(prefix_fallback.rstrip("/") + "/")
    if (prefix_fallback and meta_root and not inside
            and p.startswith(meta_root + "/")):
        p = prefix_fallback + p[len(meta_root):]
    if any(seg in (".", "..") for seg in p.split("/")):
        raise ValueError("resolved path has . or .. segments: %s" % p)
    lead = "/cvmfs/%s/" % repo
    if p.startswith(lead):
        p = p[len(lead):]
    if p.startswith("/"):
        raise ValueError(
            "resolved path is not under /cvmfs/%s/: %s (meta_root=%s, "
            "community prefix=%s)" % (repo, p, meta_root, prefix_fallback))
    return p


def relativise_symlinks(pkgroot):
    """Port of the CI relativiser: rewrite absolute in-tree symlinks that point
    into a bits INSTALLROOT to relative links, so CVMFS accepts them. Returns the
    count rewritten. System / cross-package absolute links are left untouched."""
    n = 0
    for dirpath, dirnames, filenames in os.walk(pkgroot):
        for name in filenames + dirnames:
            lnk = os.path.join(dirpath, name)
            if not os.path.islink(lnk):
                continue
            tgt = os.readlink(lnk)
            if not (tgt.startswith("/") and "/INSTALLROOT/" in tgt):
                continue
            tail = tgt.lstrip("/")
            while "/" in tail and not os.path.exists(os.path.join(pkgroot, tail)):
                tail = tail.split("/", 1)[1]
            cand = os.path.join(pkgroot, tail)
            if not os.path.exists(cand):
                continue
            rel = os.path.relpath(cand, os.path.dirname(lnk))
            os.remove(lnk)
            os.symlink(rel, lnk)
            n += 1
    return n


def sanitize(pkgroot):
    """Port of the CI sanitize: report hardlinks (materialised later via
    tar --hard-dereference), REMOVE unpublishable special files (block/char/fifo/
    socket), and report any remaining absolute symlinks. Returns a dict summary."""
    hard = specials = abssym = 0
    for dp, dns, fns in os.walk(pkgroot):
        for name in fns:
            fp = os.path.join(dp, name)
            try:
                st = os.lstat(fp)
            except OSError:
                continue
            import stat as _stat
            if _stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                hard += 1
            if (_stat.S_ISBLK(st.st_mode) or _stat.S_ISCHR(st.st_mode)
                    or _stat.S_ISFIFO(st.st_mode) or _stat.S_ISSOCK(st.st_mode)):
                os.remove(fp)
                specials += 1
        for name in fns + dns:
            lp = os.path.join(dp, name)
            if os.path.islink(lp) and os.readlink(lp).startswith("/"):
                abssym += 1
    return {"hardlinks": hard, "specials_removed": specials, "abs_symlinks": abssym}


def tree_fingerprint(root):
    """Deterministic content fingerprint of a directory tree: sha256 over sorted
    per-entry lines carrying structure + mode + size + content-sha256 + symlink
    target. Deliberately EXCLUDES mtime/uid/gid — a CVMFS catalog hash includes
    mtime, which relocate-me.sh stamps at relocation time, so it is not stable
    run-to-run; content is what "reproduces the CI" must mean."""
    import hashlib
    import stat as _stat
    lines = []
    for dp, dns, fns in os.walk(root):
        for name in dns + fns:
            p = os.path.join(dp, name)
            rel = os.path.relpath(p, root)
            st = os.lstat(p)
            mode = oct(_stat.S_IMODE(st.st_mode))
            if _stat.S_ISLNK(st.st_mode):
                lines.append("%s\tL\t%s" % (rel, os.readlink(p)))
            elif _stat.S_ISDIR(st.st_mode):
                lines.append("%s\tD\t%s" % (rel, mode))
            elif _stat.S_ISREG(st.st_mode):
                h = hashlib.sha256()
                with open(p, "rb") as fh:
                    for chunk in iter(lambda: fh.read(1 << 16), b""):
                        h.update(chunk)
                lines.append("%s\tF\t%s\t%d\t%s" % (rel, mode, st.st_size, h.hexdigest()))
            else:
                lines.append("%s\t?\t%s" % (rel, mode))
    lines.sort()
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def resolve_pkg_path(pkgroot, repo, pkg, vdir, ver, rev, platform, install_dir,
                     commit, user, family, kind, tmpl_prefix, arch,
                     prefix_fallback=None, templates=None, token_arch=None):
    """Resolve the repo-relative publish path (kind='path' for the package tree,
    'modules' for the module file, 'view' for its link in a release view: only
    when a packages template puts the tree elsewhere). ``templates`` are the publishing build's
    (from its manifest) and place every package of the closure; without them the
    package's own .meta.json cvmfs_templates are used (older manifests). Returns
    None when the kind has no template."""
    tm = templates
    if not tm:
        with open(os.path.join(pkgroot, ".meta.json")) as fh:
            tm = (json.load(fh).get("cvmfs_templates") or {})
    meta_root = tm.get("prefix") or None
    # Template selection is ARCH-DRIVEN, exactly as the CI loop: a shared (noarch)
    # package uses the shared template (falling back to path); everything else
    # uses the path template. Selecting shared unconditionally would resolve a
    # different repo path and change the relocated bytes → a different hash.
    if kind == "path":
        # Noarch packages carry effective_architecture "share" (SHARED_ARCH);
        # "shared" is still accepted (it is the certify/BOM bucket name).
        noarch = arch in (SHARED_ARCH, "shared")
        # A packages template is the tree's own home; the releases template
        # ("path") is then only the release view (kind="view").
        tree = tm.get("packages") or tm.get("path")
        key = (tm.get("shared") or tree) if noarch else tree
    elif kind == "view":
        # Only a group with both templates has a release view.
        key = tm.get("path") if tm.get("packages") and tm.get("path") != tm["packages"] else None
    elif kind == "modules":
        key = tm.get("modules")
    else:
        key = None
    if not key:
        return None
    # {prefix} resolves to the caller-supplied root; when absent, fall back to
    # the package's own declared prefix (the admin case, IS_ADMIN=1 — a user
    # publish supplies user_prefix/<login> explicitly).
    key = key.replace("{prefix}", tmpl_prefix or meta_root or "")
    p = expand_tmpl(key, pkg=pkg, tag=vdir, version=ver, revision=rev,
                    platform=platform, install_dir=install_dir, commit=commit,
                    user=user, family=family, arch=token_arch or arch)
    left = re.search(r"\{\w+\}", p)
    if left:
        raise ValueError("unresolved %s in CVMFS path %s" % (left.group(0), p))
    return repo_relative_path(p, repo, meta_root, prefix_fallback)


# ── staged submit (the CI does this with raw curl; not in prepub.submit_job) ──

def submit_staged(prepub_url, token, repo, path, staging_prefix, catalog_hash,
                  build_id="", bearer_auth=False, no_verify_tls=False,
                  identity_path="", identity_hash="", replace=False):
    """POST /api/v1/jobs for the STAGED path: no tar, just staging_prefix +
    catalog_hash (what the CI does). Returns the job id. Reuses prepub.py's
    session/auth helpers; mirrors the CI staged submit — including build_id in
    BOTH the body and the signed field set (omitting it breaks the signature),
    and signing by default (bearer puts the token on every request)."""
    from bits_helpers import prepub as _pp
    url = "%s/api/v1/jobs" % prepub_url.rstrip("/")
    session = _pp._make_session(no_verify_tls, signed=not bearer_auth)
    fields = {
        "repo":           (None, repo),
        "path":           (None, path),
        "publish_path":   (None, "staged"),
        "staging_prefix": (None, staging_prefix),
        "catalog_hash":   (None, catalog_hash),
    }
    signed_fields = {"repo": repo, "path": path, "publish_path": "staged",
                     "staging_prefix": staging_prefix, "catalog_hash": catalog_hash}
    if build_id:
        fields["build_id"] = (None, build_id)
        signed_fields["build_id"] = build_id
    for k, v in _identity_fields(identity_path, identity_hash, replace).items():
        fields[k] = (None, v)
        signed_fields[k] = v
    headers = _pp._auth_headers(token, "POST", _pp._signed_uri(url),
                                fields=signed_fields, bearer_auth=bearer_auth,
                                no_verify_tls=no_verify_tls)
    resp = session.post(url, files=fields, headers=headers, timeout=300)
    if resp.status_code not in (200, 201, 202):
        raise SystemExit("prepub staged submit failed: HTTP %s: %s"
                         % (resp.status_code, resp.text[:400]))
    jid = (resp.json() or {}).get("job_id", "")
    if not jid:
        raise SystemExit("prepub staged submit: no job_id in response: %s"
                         % resp.text[:400])
    return jid


_HEALTH = {}   # prepub URL -> its /api/v1/health answer, once it has answered


def _prepub_health(session, prepub_url):
    """prepub's /api/v1/health, cached per URL once the node has answered; {}
    when it cannot be asked right now (the next call asks again)."""
    if prepub_url not in _HEALTH:
        try:
            r = session.get("%s/api/v1/health" % prepub_url.rstrip("/"), timeout=30)
            body = r.json() if getattr(r, "status_code", 200) == 200 else None
        except Exception:                   # best-effort: never blocks a publish
            return {}
        if not isinstance(body, dict):      # e.g. a node restarting: ask again later
            return {}
        _HEALTH[prepub_url] = body
    return _HEALTH[prepub_url]


def _prepub_max_tar(session, prepub_url):
    """prepub's per-package tar limit. None when it does not advertise one
    (older prepub) or cannot be asked: the upload then goes ahead and prepub
    itself decides."""
    return int(_prepub_health(session, prepub_url).get("max_tar_size") or 0) or None


def prepub_replace_allowed(ctx):
    """Whether prepub replaces what another build published (its health says
    replace_allowed). False for an older prepub, or one that cannot be asked."""
    from bits_helpers import prepub as _pp
    session = _pp._make_session(ctx.get("no_verify_tls", False),
                                signed=not ctx.get("bearer_auth", False))
    return bool(_prepub_health(session, ctx["prepub_url"]).get("replace_allowed"))


def _identity_fields(identity_path, identity_hash, replace):
    """The identity fields of a submission: the path whose presence means the
    content is published, the hash it must carry, and whether to replace
    another build's content there. Every one is signed."""
    out = {}
    if identity_path:
        out["identity_path"] = identity_path
        if identity_hash:   # the .meta.json hash that makes it this build's
            out["identity_hash"] = identity_hash
            if replace:
                out["replace"] = "true"
    return out


def submit_ingest(prepub_url, token, repo, path, tar_file, build_id="",
                  direct_s3=False, bearer_auth=False, no_verify_tls=False,
                  identity_path="", identity_hash="", object_list=False,
                  prewarm=False, replace=False):
    """POST /api/v1/jobs for the INGEST path: the raw tar IS the payload (prepub's
    gateway does the chunk/compress/upload). Mirrors the CI's `_post_tar` ingest
    branch — sends the tar plus its sha256, and SIGNS tar_sha256 so prepub can
    reject a corrupted upload. direct_s3=True adds the direct_s3 field so
    cvmfs_server writes objects straight to S3 (bypassing the gateway).
    object_list=True (needs direct_s3) has the publisher report the objects it
    stored; prewarm=True (needs object_list) lets prepub announce them to the
    Stratum 1s. replace=True (needs identity_path == path and identity_hash)
    asks prepub to replace content another build published at path. Signed by
    default; bearer puts the token on the request instead. Returns the job id."""
    import requests
    from bits_helpers import prepub as _pp
    url = "%s/api/v1/jobs" % prepub_url.rstrip("/")
    session = _pp._make_session(no_verify_tls, signed=not bearer_auth)
    # Refuse here what prepub would refuse: it cuts an oversized upload off
    # mid-stream, which reaches us only as a bare connection reset.
    size = os.path.getsize(tar_file)
    limit = _prepub_max_tar(session, prepub_url)
    if limit and size > limit:
        raise SystemExit("tar is %s, over prepub's per-package limit of %s"
                         " (max_tar_size_gib on the prepub node)"
                         % (_human(size), _human(limit)))
    tar_sha256 = _pp.sha256_file(tar_file)
    # The signed set MUST equal the fields prepub parses, or the digest differs
    # and the publish 401s (reads as auth failure). tar itself is not signed —
    # its sha256 is, and prepub re-hashes the upload to bind them.
    signed_fields = {"repo": repo, "path": path, "publish_path": "ingest",
                     "tar_sha256": tar_sha256}
    if build_id:
        signed_fields["build_id"] = build_id
    if direct_s3:
        signed_fields["direct_s3"] = "true"
    if object_list:
        signed_fields["object_list"] = "true"
    if prewarm:
        signed_fields["prewarm"] = "true"
    # identity_path: prepub re-checks it just before committing and finishes a
    # job whose content appeared meanwhile (a rerun queued behind the original).
    identity = _identity_fields(identity_path, identity_hash, replace)
    signed_fields.update(identity)
    # body_hash BINDS the tar to the signature: prepub re-hashes the uploaded
    # tar and the MAC only matches if the same digest was signed. Signing
    # tar_sha256 as a field is NOT enough — the httpsig body-hash component is
    # what makes a wrong/truncated upload 401 (mirrors prepub.submit_job).
    headers = _pp._auth_headers(token, "POST", _pp._signed_uri(url),
                                fields=signed_fields, body_hash=tar_sha256,
                                bearer_auth=bearer_auth, no_verify_tls=no_verify_tls)
    fields = {
        "repo":         (None, repo),
        "path":         (None, path),
        "publish_path": (None, "ingest"),
        "tar_sha256":   (None, tar_sha256),
    }
    if build_id:
        fields["build_id"] = (None, build_id)
    if direct_s3:
        fields["direct_s3"] = (None, "true")
    if object_list:
        fields["object_list"] = (None, "true")
    if prewarm:
        fields["prewarm"] = (None, "true")
    for k, v in identity.items():
        fields[k] = (None, v)
    with open(tar_file, "rb") as tfh:
        fields["tar"] = ("pkg.tar", tfh, "application/octet-stream")
        try:
            resp = session.post(url, files=fields, headers=headers, timeout=1800)
        except requests.ConnectionError as exc:
            raise SystemExit("upload of %s cut off by prepub (%s); prepub refuses"
                             " an upload that is too large or would fill its"
                             " spool, see its log" % (_human(size), exc))
    if resp.status_code not in (200, 201, 202):
        raise SystemExit("prepub ingest submit failed: HTTP %s: %s"
                         % (resp.status_code, resp.text[:400]))
    jid = (resp.json() or {}).get("job_id", "")
    if not jid:
        raise SystemExit("prepub ingest submit: no job_id in response: %s"
                         % resp.text[:400])
    return jid


def tar_path(spec, tars_root, default_arch):
    """Deterministic path of a package's built tarball (mirrors the CI at
    cvmfs-prepub-publish.yml:1386)."""
    arch = spec.get("effective_architecture") or default_arch
    rev = spec.get("revision", "")
    vdir = spec.get("version", "") + ("-" + rev if rev else "")
    return os.path.join(tars_root, arch, spec["package"],
                        "%s-%s.%s.tar.gz" % (spec["package"], vdir, arch))


def _human(n):
    """Bytes as a short human string (1.8G, 212M, 4K, 0B). For log cross-checks."""
    from bits_helpers.utilities import human_bytes
    return human_bytes(n, units=("B", "K", "M", "G", "T"), sep="")


def payload_size(spec, tars_root, default_arch):
    """Best size for LPT ordering: the install du the build records in the GC
    sentinel (<sw>/.packages/<arch>/<pkg>/<ver-rev>) — the real payload, present
    for EVERY outcome including reused artefacts — else the (uniform ~4 KB)
    publish tar, else 0. Never raises; any miss degrades to the tar/zero
    fallback. The tar must not be the primary key: uniform across packages, it
    collapsed the sort to manifest order and sent the biggest payload last."""
    work_dir = os.environ.get("BITS_WORK_DIR") or os.path.dirname(tars_root.rstrip("/"))
    try:
        from bits_helpers.cleanup import sentinel_path
        from bits_helpers.utilities import ver_rev
        with open(sentinel_path(work_dir, default_arch, spec["package"], ver_rev(spec))) as fh:
            return int(fh.readline().strip())
    except Exception:                       # best-effort heuristic: any failure
        pass                                # degrades to the tar/zero fallback
    try:
        return os.path.getsize(tar_path(spec, tars_root, default_arch))
    except OSError:
        return 0


def order_biggest_first(specs, tars_root, default_arch):
    """Sort package specs by PAYLOAD size, largest first (LPT), so the longest
    unit starts first and does not tail the window. Size
    is payload_size (the GC sentinel du). Stable within a size."""
    return sorted(specs, key=lambda s: payload_size(s, tars_root, default_arch),
                  reverse=True)


def _locate_pkgroot(work_dir):
    """The dir holding .meta.json inside the extracted tar (mirrors the CI find)."""
    for dp, _dns, fns in os.walk(work_dir):
        if ".meta.json" in fns:
            return dp
    raise SystemExit("cannot locate package root (.meta.json) under %s" % work_dir)


def _files_under(root):
    """Set of absolute file paths (not dirs) under *root*. Used to snapshot the
    relocate work dir before/after so we can detect what post-relocate.sh wrote."""
    out = set()
    for dp, _dns, fns in os.walk(root):
        for f in fns:
            out.add(os.path.join(dp, f))
    return out


def _writes_outside_pkgroot(before, after, pkgroot):
    """Files present after relocate but not before, that live OUTSIDE *pkgroot* —
    i.e. what post-relocate.sh created outside the package's own tree. publish_one
    tars only pkgroot, so these would otherwise be silently dropped and never reach
    CVMFS. Returned sorted for a stable message."""
    root = pkgroot.rstrip(os.sep)
    return sorted(p for p in (after - before)
                  if p != root and not p.startswith(root + os.sep))


def _publish_tar(ctx, path, tar, label, fp=None, identity="", identity_hash="",
                 replace=False):
    """Publish ONE prepared tar via the configured path; return its job id and
    remove the tar. INGEST (default): POST the tar itself with submit_ingest — the
    gateway chunks it. STAGED: cvmfs-stage the tar to an S3 prefix (always, so
    --dry-run still yields the catalog hash) then submit_staged. --dry-run submits
    nothing. A non-None fp prints the mtime-independent FINGERPRINT (verify hook).
    identity is the path whose presence means this content is published, and
    identity_hash the hash it carries there; replace asks prepub to replace
    another build's content at that path (it then needs both)."""
    ingest = ctx.get("publish_path") == "ingest"
    submit = ctx.get("submit", True)
    try:
        if ingest:
            jid = (submit_ingest(ctx["prepub_url"], ctx["token"], ctx["repo"], path,
                                 tar, build_id=ctx.get("build_id", ""),
                                 direct_s3=ctx.get("direct_s3", False),
                                 object_list=ctx.get("object_list", False),
                                 prewarm=ctx.get("prewarm", False),
                                 bearer_auth=ctx.get("bearer_auth", False),
                                 no_verify_tls=ctx.get("no_verify_tls", False),
                                 identity_path=identity,
                                 identity_hash=identity_hash, replace=replace)
                   if submit else (fp or ""))
        else:
            prefix, catalog = stage_tar(
                ctx["repo"], tar, path,
                "%s-%s" % (ctx["job_id_base"], _hash8(path)), ctx["stratum0_url"],
                no_stats_db=ctx.get("no_stats_db", False),
                no_prepare_lock=ctx.get("no_prepare_lock", False),
                swissknife=ctx.get("swissknife"), base_root=ctx.get("base_root"),
                replace_on_conflict=replace)
            jid = (submit_staged(ctx["prepub_url"], ctx["token"], ctx["repo"], path,
                                 prefix, catalog, build_id=ctx.get("build_id", ""),
                                 bearer_auth=ctx.get("bearer_auth", False),
                                 no_verify_tls=ctx.get("no_verify_tls", False),
                                 identity_path=identity, identity_hash=identity_hash,
                                 replace=replace)
                   if submit else catalog)   # dry-run: catalog hash for the verify
    finally:
        _safe_rm(tar)
    if not submit and fp is not None:
        print("FINGERPRINT %s %s" % (fp, label))
    return jid


def publish_overlay_tars(ctx, tars):
    """Publish each overlay tar at the repo ROOT (path="") via the staged/ingest
    path; return the job ids.

    An overlay tar carries files at their repo-relative locations (e.g. the
    ``.cvmfsbundle-*`` files `bits preload` writes next to their triggers), so a
    single root lease drops each file into place — unlike a package tar staged at
    one subtree. The caller's tar is preserved: a temp copy is published
    (``_publish_tar`` removes what it publishes). Each tar gets a distinct
    ``job_id_base`` so their leases do not collide.
    """
    import shutil
    base = ctx.get("job_id_base", "local")
    jids = []
    for i, tar in enumerate(tars):
        fd, tmp = tempfile.mkstemp(suffix=".tar", dir=ctx.get("tmp_dir") or None)
        os.close(fd)
        shutil.copyfile(tar, tmp)
        jids.append(_publish_tar(dict(ctx, job_id_base="%s-tar%d" % (base, i)),
                                 "", tmp, os.path.basename(tar)))
    return jids


class AlreadyPublished(Exception):
    """The package is already published at this path by the same build."""


def _spec_path(spec, ctx, kind, arch=None):
    """Repo-relative path of *kind* for a manifest spec, from the build's
    templates (no tarball needed)."""
    ver = spec.get("version", "")
    rev = spec.get("revision", "")
    arch = arch or spec.get("effective_architecture") or ctx["arch"]
    return resolve_pkg_path(
        None, ctx["repo"], spec["package"], ver + ("-" + rev if rev else ""), ver,
        rev, ctx.get("platform", ""), ctx.get("install_dir", ""),
        spec.get("commit", ""), ctx.get("user", ""), spec.get("pkg_family", ""),
        kind=kind, tmpl_prefix=ctx["tmpl_prefix"], arch=arch,
        prefix_fallback=ctx.get("prefix_fallback"), templates=ctx["templates"],
        # One view / modules dir per build: {arch} is the build's, not the
        # package's own (own_hash toolchain, noarch).
        token_arch=(ctx["arch"] or arch) if kind in ("view", "modules") else None)


def package_path(spec, ctx):
    return _spec_path(spec, ctx, "path")


def _prepub_post(ctx, endpoint, payload):
    """POST *payload* (JSON) to prepub's *endpoint*, signed like a submission.
    The response, or None when prepub cannot be reached."""
    import hashlib
    from bits_helpers import prepub as _pp
    url = "%s%s" % (ctx["prepub_url"].rstrip("/"), endpoint)
    body = json.dumps(payload).encode()
    headers = _pp._auth_headers(ctx["token"], "POST", _pp._signed_uri(url),
                                body_hash=hashlib.sha256(body).hexdigest(),
                                bearer_auth=ctx.get("bearer_auth", False),
                                no_verify_tls=ctx.get("no_verify_tls", False))
    headers["Content-Type"] = "application/json"
    session = _pp._make_session(ctx.get("no_verify_tls", False),
                                signed=not ctx.get("bearer_auth", False))
    try:
        return session.post(url, data=body, headers=headers, timeout=120)
    except Exception as exc:
        sys.stderr.write("[publish] cannot reach prepub at %s (%s)\n" % (endpoint, exc))
        return None


def published_state(ctx, path):
    """Ask prepub whether *path* is published: {"exists": bool, "hash": str}, or
    None when prepub cannot tell (older prepub, no stratum0) — then publish."""
    resp = _prepub_post(ctx, "/api/v1/published", {"repo": ctx["repo"], "path": path})
    if resp is None:
        sys.stderr.write("[publish] cannot ask prepub whether %s is published; "
                         "publishing it\n" % path)
        return None
    if resp.status_code == 200:
        return resp.json() or {}
    if resp.status_code == 403:
        raise SystemExit("prepub refused %s: outside the authorized namespace" % path)
    if resp.status_code not in (404, 405, 501):   # 404/405: older prepub
        sys.stderr.write("[publish] published check for %s: HTTP %s; publishing it\n"
                         % (path, resp.status_code))
    return None


PUBLISHED_FILES_BATCH = 200   # prepub answers at most 512 paths per request


def published_files(ctx, paths):
    """The published content of .meta.json / .bits-view.json files, read through
    prepub (POST /api/v1/published/files): {path: parsed JSON, or None when
    not published or unreadable}. None when prepub cannot read them (older
    prepub, no stratum0, an error): the caller then cannot rely on them."""
    out, todo = {}, list(dict.fromkeys(paths))
    batch = PUBLISHED_FILES_BATCH
    while todo:
        part = todo[:batch]
        resp = _prepub_post(ctx, "/api/v1/published/files",
                            {"repo": ctx["repo"], "paths": part})
        if resp is not None and resp.status_code == 413 and batch > 1:
            batch = max(1, batch // 2)   # too many bytes at once: smaller batches
            continue
        if resp is None or resp.status_code != 200:
            if resp is not None and resp.status_code not in (404, 405, 501):
                sys.stderr.write("[publish] reading published metadata: HTTP %s\n"
                                 % resp.status_code)
            return None
        try:
            files = resp.json().get("files")
        except (ValueError, AttributeError):
            files = None
        if not isinstance(files, dict):
            sys.stderr.write("[publish] reading published metadata: unexpected answer\n")
            return None
        for p_ in part:
            out[p_] = files.get(p_)
        todo = todo[len(part):]
    return out


def fixed_dir(ctx, key):
    """Repo-relative directory a template fixes for the whole build: the
    template with {prefix}, {arch} (build arch) and {platform} filled, cut at
    the last "/" before its first per-package token. None when that is not a
    directory below the prefix."""
    tm = ctx["templates"]
    prefix = ctx["tmpl_prefix"] or tm.get("prefix") or ""
    t = (tm[key].replace("{prefix}", prefix).replace("{arch}", ctx["arch"])
         .replace("{platform}", ctx.get("platform", "")))
    fixed = t[:t.index("{")] if "{" in t else t + "/"
    root = fixed[:fixed.rfind("/")]
    if not root.startswith(prefix.rstrip("/") + "/"):
        return None
    return repo_relative_path(root, ctx["repo"], tm.get("prefix"), ctx.get("prefix_fallback"))


def view_root(ctx):
    """Repo-relative root of the release view (see fixed_dir)."""
    root = fixed_dir(ctx, "path")
    if not root:
        raise SystemExit("the releases template %r has no fixed release directory "
                         "for the release view" % ctx["templates"]["path"])
    return root


def base_module(ctx):
    """(modules dir, BASE/1.0 text) for a publish-once build, or None. BASE sets
    BASEDIR, which bits modulefiles resolve their package root against
    ($BASEDIR/<pkg>/<ver-rev>); it is written relative to the modulefile's own
    location, so it holds wherever the tree is mounted (e.g. the testbed)."""
    tm = ctx.get("templates") or {}
    if not (tm.get("packages") and tm.get("modules")):
        return None
    mods, pkgs = fixed_dir(ctx, "modules"), fixed_dir(ctx, "packages")
    if not (mods and pkgs):
        return None
    rel = os.path.relpath(pkgs, os.path.join(mods, "BASE"))
    return mods, (
        "#%%Module1.0\n"
        "## BASEDIR: the Packages directory of this tree (%s from here)\n"
        "set base_path [file normalize [file join [file dirname $ModulesCurrentModulefile] %s]]\n"
        "setenv BASEDIR $base_path\n"
        "set osname [uname sysname]\n"
        "set osarchitecture [uname machine]\n" % (rel, rel))


def native_path(spec, ctx):
    """Where BASE-relative modulefiles look for a package: the packages template
    with the BUILD arch, even for noarch / own_hash packages published elsewhere."""
    tm = dict(ctx["templates"], shared=None)
    return _spec_path(spec, dict(ctx, templates=tm), "path", arch=ctx["arch"])


def module_links(ctx, specs):
    """(modules root, [(modulefile path, its file in the package)]) for the
    packages whose modulefile is not published yet, or None without a fixed
    modules directory. The link replaces the separate copy: modulefiles find
    their package through $BASEDIR, not their own location, and module names
    come from the link's path."""
    root = (fixed_dir(ctx, "modules")
            if (ctx.get("templates") or {}).get("modules") else None)
    if not root:
        return None
    links = []
    for s_ in specs:
        mod = _spec_path(s_, ctx, "modules")
        if not mod:
            continue
        rev = s_.get("revision", "")
        link = "%s/%s" % (mod, s_.get("version", "") + ("-" + rev if rev else ""))
        target = "%s/etc/modulefiles/%s" % (package_path(s_, ctx), s_["package"])
        has = s_.get("_modulefile")   # known when this run extracted the package
        if ctx["submit"]:
            st = published_state(ctx, link)
            if st and st.get("exists"):
                continue
            if has is None:           # published before: ask whether it has one
                st = published_state(ctx, target)
                has = bool(st and st.get("exists"))
        if has:
            links.append((link, target))
    return root, links


def publish_links(ctx, root, links, label):
    """Publish relative symlinks (view path, target) rooted at *root* as one job.
    Returns (job id, None) or (None, error). Ingest first and never replace: the
    root is shared (other platforms / earlier publishes), so the links must merge
    into it; a prepub without ingest gets the configured path, which only works
    while *root* is new."""
    import shutil
    tar = build_view_tar(links, root, ctx["tmp_dir"])
    tries = ["ingest"] + ([ctx["publish_path"]] if ctx.get("publish_path") != "ingest" else [])
    errs = []
    try:
        for how in tries:
            vtar = tar if how == tries[-1] else tar + ".copy"
            if vtar != tar:
                shutil.copyfile(tar, vtar)
            try:
                return _publish_tar(dict(ctx, publish_path=how, replace_on_conflict=False),
                                    root, vtar, label), None
            except (SystemExit, Exception) as exc:
                errs.append("%s: %s" % (how, exc))
    finally:
        _safe_rm(tar)   # left over when the first try published a copy
    return None, ("adding to an existing directory needs prepub's ingest path: %s"
                  % "; ".join(errs))


# What a merged release view unions (per-package metadata such as etc/, .meta.json
# and modulefiles stays out: it would collide on every package).
VIEW_SUBDIRS = ("bin", "lib", "lib64", "include", "share", "cmake", "python",
                "libexec", "man")


def _list_tree(entries):
    """{path: (kind, linkname)} from a .bits-view.json entry list, with the
    directories it lists only implicitly."""
    tree = {p: (k, t) for p, k, t in entries}
    for rel in list(tree):
        d = os.path.dirname(rel)
        while d and d not in tree:
            tree[d] = ("dir", "")
            d = os.path.dirname(d)
    return tree


def _vdir(spec):
    rev = spec.get("revision", "")
    return spec.get("version", "") + ("-" + rev if rev else "")


def _local_root(spec, ctx):
    """The package's local install tree when it holds this very build, else None."""
    fam = spec.get("pkg_family", "")
    local = os.path.join(os.path.dirname(ctx["tars_root"].rstrip("/")),
                         spec.get("effective_architecture") or ctx["arch"],
                         *([fam] if fam else []), spec["package"], _vdir(spec))
    try:
        with open(os.path.join(local, ".meta.json")) as fh:
            same = (json.load(fh).get("package") or {}).get("hash") == spec.get("hash")
    except (OSError, ValueError, AttributeError):
        return None
    return local if same else None


def _package_tarball(spec, ctx, what):
    tar = tar_path(spec, ctx["tars_root"], ctx["arch"])
    if not os.path.isfile(tar):
        raise SystemExit("%s@%s: no tarball to %s from (%s)"
                         % (spec["package"], _vdir(spec), what, tar))
    return tar


def _package_tree(spec, ctx):
    """The package's view entries: its .bits-view.json (written by the build),
    from the local install tree when that holds this very build, else from the
    tarball; a tarball without one (built before) is listed in full."""
    local = _local_root(spec, ctx)
    try:
        if local:
            with open(os.path.join(local, ".bits-view.json")) as fh:
                return _list_tree(json.load(fh)["entries"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return _tar_tree(_package_tarball(spec, ctx, "list its files"))


def _package_file(spec, ctx, rel):
    """The text of the package's *rel* (package-relative) as published, or None
    when it has none: from the local install tree when that holds this very
    build, else from the tarball (<arch>/[<family>/]<pkg>/<ver-rev>/...), its
    revision placeholder filled as the install fills it."""
    import tarfile
    local = _local_root(spec, ctx)
    if local:
        try:
            with open(os.path.join(local, rel)) as fh:
                return fh.read()
        except OSError:
            return None
    fam = spec.get("pkg_family", "")
    want = os.path.join(spec.get("effective_architecture") or ctx["arch"],
                        *([fam] if fam else []), spec["package"], _vdir(spec), rel)
    with tarfile.open(_package_tarball(spec, ctx, "read %s" % rel), "r:*") as tf:
        for m in tf:
            if os.path.normpath(m.name) == want and m.isfile():
                text = tf.extractfile(m).read().decode("utf-8", "replace")
                return text.replace("@@PKGREVISION@%s@@" % spec.get("hash", ""),
                                    spec.get("revision", ""))
    return None


def _tar_tree(tar):
    """{package-relative path: (kind, linkname)} of a package tarball, kind in
    "dir" / "file" / "link", as published (without .unrelocated backups). Uses
    the tarball's .bits-view.json when it has one (sorted early in the tar)."""
    import tarfile
    members, lists, meta_depth, meta_dir = {}, {}, None, None
    with tarfile.open(tar, "r:*") as tf:
        for m in tf:
            n = m.name
            while n.startswith("./"):
                n = n[2:]
            n = os.path.normpath(n or ".")
            base, d = os.path.basename(n), os.path.dirname(n)
            if base == ".bits-view.json" and m.isfile():
                try:
                    lists[d] = _list_tree(json.load(tf.extractfile(m))["entries"])
                except (ValueError, KeyError, TypeError):
                    pass   # unreadable: list the tarball instead
            elif base == ".meta.json":
                # The package root's own list: next to the shallowest .meta.json.
                depth = n.count("/")
                if meta_depth is None or depth < meta_depth:
                    meta_depth, meta_dir = depth, d
                    if d in lists:
                        return lists[d]   # bits tarballs are sorted: found early
            members[n] = m
    if meta_dir in lists:
        return lists[meta_dir]
    metas = [n for n in members if os.path.basename(n) == ".meta.json"]
    if not metas:
        raise SystemExit("%s has no .meta.json" % tar)
    top = os.path.dirname(min(metas, key=len))
    tree = {}
    for name, m in members.items():
        rel = os.path.relpath(name, top) if top else name
        if rel.startswith("..") or rel == "." or rel.endswith(".unrelocated"):
            continue
        tree[rel] = ("dir" if m.isdir() else "link" if m.issym() else "file",
                     m.linkname if m.issym() else "")
    for rel in list(tree):   # directories a tar lists only implicitly
        d = os.path.dirname(rel)
        while d and d not in tree:
            tree[d] = ("dir", "")
            d = os.path.dirname(d)
    return tree


def _resolve(tree, path, depth=0):
    """*path* of a package tree from _tar_tree with its symlinks followed
    inside the package, or None when it leaves it or does not exist."""
    kind, target = tree.get(path, (None, ""))
    if kind != "link" or depth > 16:
        return path if kind else None
    t = os.path.normpath(os.path.join(os.path.dirname(path), target))
    return None if target.startswith("/") or t.startswith("..") else _resolve(tree, t, depth + 1)


def _resolve_path(tree, rel, depth=0):
    """*rel* of a package tree from _tar_tree with every symlink on the way
    followed, as the file system would; None when it does not exist. Raises
    ValueError when a link leads out of the package."""
    cur = ""
    for part in rel.split("/"):
        cur = cur + "/" + part if cur else part
        kind, target = tree.get(cur, (None, ""))
        if kind is None:
            return None
        if kind == "link":
            t = os.path.normpath(os.path.join(os.path.dirname(cur), target))
            if depth > 16:
                raise ValueError("%s: too many links" % cur)
            if target.startswith("/") or t == ".." or t.startswith("../"):
                raise ValueError("%s is a link out of the package" % cur)
            cur = _resolve_path(tree, t, depth + 1)
            if cur is None:
                return None
    return cur


def _view_entries(tree, subdirs):
    """(view-relative path, package-relative path) for every file or symlink the
    view links, walking *subdirs* of a package tree from _tar_tree. A symlinked
    directory whose target stays inside the package is walked like a directory
    (under its own name, which the published tree also has); others are linked."""
    children = {}
    for path in tree:
        children.setdefault(os.path.dirname(path), []).append(path)

    out = []

    def walk(logical, real, seen):
        for child in sorted(children.get(real, [])):
            name = os.path.join(logical, os.path.basename(child))
            real_child = _resolve(tree, child)
            if real_child and tree[real_child][0] == "dir" and real_child not in seen:
                walk(name, real_child, seen | {real_child})
            else:
                out.append((name, name))
    for sub in subdirs:
        real = _resolve(tree, sub)
        if real and tree[real][0] == "dir":
            walk(sub, real, {real})
    return out


def _excluded(path, patterns):
    """True if *path* or one of its parent directories matches a pattern."""
    import fnmatch
    parts = path.split("/")
    return any(fnmatch.fnmatchcase("/".join(parts[:i]), pat)
               for pat in patterns for i in range(1, len(parts) + 1))


# Directories setup.sh looks for in the view (see view.view_env): never folded,
# so they stay real directories it can find.
_VIEW_KEEP = ("lib*/pkgconfig", "lib*/python*", "lib*/python*/site-packages",
              "share/man")


def _view_rules(spec, exclude):
    """(directories the view walks, patterns it drops) for a package, or None
    when the view leaves it out."""
    rules = spec.get("view", True)
    if spec["package"] in exclude or rules is False:
        return None
    rules = rules if isinstance(rules, dict) else {}
    return (list(VIEW_SUBDIRS) + [str(p).strip("/") for p in (rules.get("include") or [])],
            [str(p).strip("/") for p in (rules.get("exclude") or [])])


def merged_view(ctx, specs, staging, view_path):
    """Build the release's merged view in *staging*: relative symlinks from
    *view_path* (repo-relative) into each package's published path, listed from
    the package tarballs (what was published). A subdirectory only one package
    fills, with nothing of it excluded, is linked whole (folded, as GNU stow
    does); elsewhere files are linked one by one. *specs* are in dependency
    order; the dependent (later) package wins a collision.
    Returns {"linked": [...], "conflicts": [(path, winner, loser)]}."""
    import fnmatch
    exclude = set(ctx["templates"].get("view_exclude") or [])
    plans, users = [], {}   # users: view path -> packages with an entry at/below it
    for spec in reversed(specs):
        rules = _view_rules(spec, exclude)
        if rules is None:
            continue
        subdirs, drop = rules
        entries = []
        for vrel, prel in _view_entries(_package_tree(spec, ctx), subdirs):
            dropped = _excluded(prel, drop)
            if not dropped:
                entries.append(vrel)
            p = vrel
            while p:   # a dropped entry keeps its directories from folding
                users.setdefault(p, set()).add(None if dropped else spec["package"])
                p = os.path.dirname(p)
        plans.append((spec, entries, set(subdirs)))

    def kept(d):   # segment-wise: fnmatch's "*" would also match "/"
        segs = d.split("/")
        return any(len(k.split("/")) == len(segs) and
                   all(fnmatch.fnmatchcase(a, b) for a, b in zip(segs, k.split("/")))
                   for k in _VIEW_KEEP)

    def fold(vrel, pkg, roots):
        """The shallowest directory above *vrel*, strictly below one of the
        package's walked roots, that *pkg* alone fills."""
        parts = vrel.split("/")
        for i in range(2, len(parts)):
            d = "/".join(parts[:i])
            if (users.get(d) == {pkg} and not kept(d)
                    and any(d.startswith(r + "/") for r in roots)):
                return d
        return vrel

    owner, dirs, refused = {}, set(), set()
    res = {"linked": [], "conflicts": []}
    for spec, entries, roots in plans:
        pkg_path = package_path(spec, ctx)
        for vrel in entries:
            vrel = fold(vrel, spec["package"], roots)
            if owner.get(vrel) == spec["package"] or (vrel, spec["package"]) in refused:
                continue   # a folded directory, already linked or refused
            parents = [os.path.dirname(vrel)]
            while parents[-1]:
                parents.append(os.path.dirname(parents[-1]))
            clash = vrel in owner or vrel in dirs or any(p in owner for p in parents)
            if clash:
                winner = owner.get(vrel) or next(
                    (owner[p] for p in parents if p in owner), "(a directory)")
                res["conflicts"].append((vrel, winner, spec["package"]))
                refused.add((vrel, spec["package"]))
                continue
            dest = os.path.join(staging, vrel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            dirs.update(p for p in parents if p)
            target = os.path.join(pkg_path, vrel)   # view paths mirror package paths
            os.symlink(os.path.relpath(target, os.path.join(view_path, os.path.dirname(vrel))), dest)
            owner[vrel] = spec["package"]
            res["linked"].append(vrel)
    return res


def view_environment(ctx, specs, view_path, top_level):
    """What loading the release's modules sets, for its view's setup.sh: the
    modulefiles of *top_level* (all *specs* without it) and of the modules they
    load, read without Tcl (bits_helpers.modulefile_env). Returns (env, problems),
    env {"paths": {var: [entries]}, "sets": {var: value}, "base": (relative to
    the view, absolute)} with modulefile_env.VIEW / BASE in the values, or None.
    A problem means setup.sh does not set everything the modules do."""
    from bits_helpers import modulefile_env as mfe
    pkgs_root = (fixed_dir(ctx, "packages")
                 if (ctx.get("templates") or {}).get("packages") else None)
    if not pkgs_root:
        return None, ["the group has no packages directory"]
    by = {sp["package"]: sp for sp in specs}
    members = {n: _vdir(sp) for n, sp in by.items()}
    trees = {}

    def unsupported(what, call):
        try:
            return call()
        except (SystemExit, OSError, ValueError, EOFError) as exc:
            raise mfe.Unsupported("cannot %s: %s" % (what, exc))

    def tree(pkg):
        if pkg not in trees:
            trees[pkg] = unsupported("list it", lambda: _package_tree(by[pkg], ctx))
        return trees[pkg]

    def is_dir(pkg, rel):
        try:
            real = _resolve_path(tree(pkg), rel)
        except ValueError as exc:   # modulecmd follows it out of the package; we cannot
            raise mfe.Unsupported(str(exc))
        return real is not None and tree(pkg)[real][0] == "dir"

    def read(pkg):
        return unsupported("read its modulefile", lambda: _package_file(
            by[pkg], ctx, "etc/modulefiles/%s" % pkg))

    exclude = set(ctx["templates"].get("view_exclude") or [])
    # Beside what a view links, a package root holds only its own metadata.
    own = ("etc", "relocate-me.sh")

    def in_view(pkg, rel):
        rules = _view_rules(by[pkg], exclude)
        if not rules:
            return False
        if not rel:   # the root (CMAKE_PREFIX_PATH): only when the view has all of it
            try:
                names = [p for p in tree(pkg) if "/" not in p]
            except mfe.Unsupported:
                return False
            return not rules[1] and all(
                p.startswith(".") or p in own or p in rules[0] for p in names)
        return not _excluded(rel, rules[1]) and any(
            rel == d or rel.startswith(d + "/") for d in rules[0])

    top = list(top_level or []) or [sp["package"] for sp in specs]
    problems = ["the release's %s is not in it" % t for t in top if t not in by]
    ops, more = mfe.release_ops([t for t in top if t in by], members, read, is_dir)
    paths, sets, rest = mfe.setup_env(ops, members, in_view)
    base = (os.path.relpath(pkgs_root, view_path), "/cvmfs/%s/%s" % (ctx["repo"], pkgs_root))
    return {"paths": paths, "sets": sets, "base": base}, problems + more + rest


def write_view_setup(staging, published_at, env=None):
    """setup.sh / setup.csh for a merged view; one entry per variable, prepended.
    setup.sh locates itself under bash and zsh (so the view works wherever the
    repository is mounted); other shells, and csh which cannot, use the path it
    is published at (*published_at*, absolute). With *env* (view_environment),
    also what the release's modules set: their path entries beside the view's,
    and their other variables (each package's <PKG>_ROOT, ...), below the
    packages directory found from the view."""
    import glob
    from bits_helpers.view import view_env
    from bits_helpers.modulefile_env import BASE, INHERITED, VIEW
    def _ver(site):
        mm = os.path.basename(os.path.dirname(site))[len("python"):]
        return tuple(int(x) for x in mm.split(".") if x.isdigit()), mm
    sites = glob.glob(os.path.join(staging, "lib*", "python*", "site-packages"))
    python_mm = max(map(_ver, sites))[1] if sites else None
    # Each variable's entries in order, INHERITED where its value from before goes:
    # the modules' as they set them, then the view's own that they did not add.
    paths = {var: list(entries) for var, entries in (env or {}).get("paths", {}).items()}
    for var, value in view_env(staging, python_mm=python_mm).items():
        have = paths.setdefault(var, [INHERITED])
        for e in value.replace(staging, VIEW).split(":"):
            if e not in have:
                have.insert(have.index(INHERITED), e)
    sets = (env or {}).get("sets", {})
    uses_base = any(BASE in v for v in list(sets.values()) +
                    [e for es in paths.values() for e in es if e is not INHERITED])
    sh = ["# Source this file (bash/zsh/sh) to use this release view.",
          'if [ -n "${BASH_SOURCE:-}" ]; then _s="${BASH_SOURCE[0]}"',
          "elif [ -n \"${ZSH_VERSION:-}\" ]; then eval '_s=${(%):-%x}'",
          "else _s='%s/setup.sh'; fi" % published_at,
          '_v="$(cd "$(dirname "$_s")" 2>/dev/null && pwd)" || _v="$(dirname "$_s")"',
          'export BITS_VIEW="$_v"']
    csh = ["# Source this file (csh/tcsh) to use this release view.",
           'set _v = "%s"' % published_at,
           "setenv BITS_VIEW $_v"]
    if uses_base:
        rel, absolute = env["base"]
        sh.append('_b="$(cd "$_v/%s" 2>/dev/null && pwd)" || _b="$_v/%s"' % (rel, rel))
        csh.append('set _b = "%s"' % absolute)

    def shell(v):   # the same in both: $_v and $_b
        return v.replace(VIEW, "$_v").replace(BASE, "$_b")
    for var in sorted(paths):
        at = paths[var].index(INHERITED)
        before = shell(":".join(paths[var][:at]))
        after = shell(":".join(paths[var][at + 1:]))
        if before:
            value = before + "${%s:+:$%s}" % (var, var) + (":" + after if after else "")
        else:
            value = "${%s:+$%s:}" % (var, var) + after
        sh.append('export %s="%s"' % (var, value))
        csh.append('if ($?%s) then\n  setenv %s "%s"\nelse\n  setenv %s "%s"\nendif'
                   % (var, var, ":".join(p for p in (before, "${%s}" % var, after) if p),
                      var, ":".join(p for p in (before, after) if p)))
    for var in sorted(sets):
        sh.append('export %s="%s"' % (var, shell(sets[var])))
        csh.append('setenv %s "%s"' % (var, shell(sets[var])))
    # Man pages only while MANPATH is set: unset, man derives its search path
    # from PATH (the view's bin -> share/man), and setting it would hide the
    # system's own pages.
    if os.path.isdir(os.path.join(staging, "share", "man")):
        sh.append('if [ -n "${MANPATH:-}" ]; then export MANPATH="$_v/share/man:$MANPATH"; fi')
        # Multi-line: csh expands ${MANPATH} on a one-line if even when unset.
        csh.append('if ($?MANPATH) then\n  setenv MANPATH "$_v/share/man:${MANPATH}"\nendif')
    sh.append("unset _s _v" + (" _b" if uses_base else ""))
    csh.append("unset _v" + (" _b" if uses_base else ""))
    for name, body in (("setup.sh", sh), ("setup.csh", csh)):
        with open(os.path.join(staging, name), "w") as fh:
            fh.write("\n".join(body) + "\n")
        os.chmod(os.path.join(staging, name), 0o755)


def build_view_tar(links, root, tmp_dir=None):
    """Tar of relative symlinks, one per (view path, package path), rooted at
    *root* (all repo-relative). Returns the tar path."""
    import tarfile
    fd, tar = tempfile.mkstemp(suffix=".tar", dir=tmp_dir)
    os.close(fd)
    import time
    now = int(time.time())
    dirs = set()
    with tarfile.open(tar, "w") as tf:
        for view, target in sorted(links):
            rel = os.path.relpath(view, root)
            if rel.startswith(".."):
                raise SystemExit("view link %s is not under the view root %s" % (view, root))
            parts = rel.split("/")
            for i in range(1, len(parts)):
                d = "/".join(parts[:i])
                if d not in dirs:
                    dirs.add(d)
                    ti = tarfile.TarInfo(d)
                    ti.type, ti.mode, ti.mtime = tarfile.DIRTYPE, 0o755, now
                    tf.addfile(ti)
            ti = tarfile.TarInfo(rel)
            ti.type, ti.mtime = tarfile.SYMTYPE, now
            ti.linkname = os.path.relpath(target, os.path.dirname(view))
            tf.addfile(ti)
    return tar


def _publish_mtime(spec, ctx):
    """The time (seconds since the epoch) stamped on a package's published files:
    when the build recorded the package (manifest completed_at), else when the
    build's manifest was created, else now. The store tarball's own file times
    are zero (reproducible archives), which CVMFS would show as 1970."""
    import calendar
    import time
    for value in (spec.get("completed_at"), ctx.get("build_time")):
        if value:
            try:
                return calendar.timegm(time.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                pass
    return int(time.time())


def publish_one(spec, ctx):
    """Full producer pipeline for ONE package, staged OR ingest path. Mirrors the CI loop
    body: locate tar -> untar -> resolve path -> relocate -> relativise -> tar ->
    cvmfs-stage -> submit; plus the modulefile as a second job, unless a fixed
    modules directory gets a link to it instead (module_links). Returns a list of
    (job_id, label). ctx is a dict of shared config (repo, prefix, tars_root, ...).
    An empty list means the package had no tar (system-provided) and was skipped.
    """
    pkg = spec["package"]
    ver = spec.get("version", "")
    rev = spec.get("revision", "")
    vdir = ver + ("-" + rev if rev else "")
    family = spec.get("pkg_family", "")
    commit = spec.get("commit", "")
    arch = spec.get("effective_architecture") or ctx["arch"]

    # Publish once: with a packages template a package's path is its identity,
    # so one that is already there (same build hash) is not sent again.
    skip_pkg = False   # the tree is there: publish only its missing modulefile
    replace = False    # another build's tree is there: replace it
    publish_once = bool((ctx.get("templates") or {}).get("packages"))
    # The package carries its modulefile (etc/modulefiles/<pkg>); with a fixed
    # modules directory it gets a link there, one job for the whole build
    # (module_links), instead of a copy published as a commit of its own.
    link_modules = publish_once and bool(
        (ctx.get("templates") or {}).get("modules") and fixed_dir(ctx, "modules"))
    if publish_once and ctx.get("submit", True):
        path = package_path(spec, ctx)
        state = published_state(ctx, path)
        if state and state.get("exists"):
            if state.get("hash") and state["hash"] == spec.get("hash"):
                if link_modules:
                    raise AlreadyPublished(path)   # its link: module_links
                mod = _spec_path(spec, ctx, "modules")
                mstate = published_state(ctx, "%s/%s" % (mod, vdir)) if mod else None
                if mstate is None or mstate.get("exists"):
                    raise AlreadyPublished(path)
                skip_pkg = True
            elif not ctx.get("replace_on_conflict"):
                raise SystemExit(
                    "%s@%s: %s is already published by another build (hash %s, this "
                    "build %s); not overwriting it (--replace-on-conflict would)" % (
                        pkg, vdir, path, state.get("hash") or "unknown", spec.get("hash")))
            elif not state.get("hash") or not spec.get("hash"):
                raise SystemExit(
                    "%s@%s: %s cannot be replaced: prepub decides on the build hashes, "
                    "and %s has none" % (pkg, vdir, path,
                                         "the published package" if not state.get("hash")
                                         else "this build"))
            elif not prepub_replace_allowed(ctx):
                # Refused here, before the upload, rather than by prepub after it.
                raise SystemExit(
                    "%s@%s: %s is published by another build (hash %s, this build %s); "
                    "--replace-on-conflict asks prepub to replace it, but this prepub "
                    "does not allow replacing (replace_on_conflict on the node)" % (
                        pkg, vdir, path, state["hash"], spec.get("hash")))
            else:
                replace = True

    tar_gz = tar_path(spec, ctx["tars_root"], ctx["arch"])
    if not os.path.isfile(tar_gz):
        if skip_pkg:
            raise AlreadyPublished(path)   # published; no tar here for its modulefile
        return []   # no tar (system-provided): the caller reports it as SKIPPED

    work_dir = tempfile.mkdtemp(prefix="pub-", dir=ctx.get("tmp_dir") or None)
    jobs = []
    try:
        subprocess.run(["tar", "-xzf", tar_gz, "-C", work_dir], check=True)
        pkgroot = _locate_pkgroot(work_dir)
        if skip_pkg and not os.path.isfile(os.path.join(pkgroot, "etc", "modulefiles", pkg)):
            raise AlreadyPublished(path)   # no modulefile to add: skip the relocate

        path = resolve_pkg_path(
            pkgroot, ctx["repo"], pkg, vdir, ver, rev, ctx.get("platform", ""),
            ctx.get("install_dir", ""), commit, ctx.get("user", ""), family,
            kind="path", tmpl_prefix=ctx["tmpl_prefix"], arch=arch,
            prefix_fallback=ctx.get("prefix_fallback"),
            templates=ctx.get("templates"))
        if not path:
            raise SystemExit("%s@%s has no cvmfs_templates path in .meta.json"
                             % (pkg, vdir))

        reloc = os.path.join(pkgroot, "relocate-me.sh")
        if os.path.isfile(reloc):
            env = dict(os.environ,
                       INSTALL_BASE="/cvmfs/%s/%s" % (ctx["repo"], path),
                       WORK_DIR=work_dir, BITS_RELOCATE_STRIP_PP="1")
            # Views (lcg-view) place the packages they reference with these.
            if ctx.get("templates"):
                env["BITS_CVMFS_TEMPLATES"] = json.dumps(ctx["templates"])
            # Run it exactly as the CI does: cwd=work_dir, script named RELATIVE
            # to it (the CI passes ${_pkgpath}/relocate-me.sh), so $0 matches.
            _before = _files_under(work_dir)
            subprocess.run(["bash", "-e", os.path.relpath(reloc, work_dir)],
                           cwd=work_dir, env=env, check=True)
            for dp, _dn, fns in os.walk(pkgroot):
                for f in fns:
                    if f.endswith(".unrelocated"):
                        os.remove(os.path.join(dp, f))
            # Fail loud, never silent: publish only tars `pkgroot`, so any file
            # post-relocate.sh created OUTSIDE the package's own tree would be
            # dropped and never reach CVMFS. Detect it and stop. (Giving such
            # files a real CVMFS channel — MODULES_STAGING / a shared path — is a
            # planned follow-up; until then, surfacing the loss beats hiding it.)
            _leaked = _writes_outside_pkgroot(_before, _files_under(work_dir), pkgroot)
            if _leaked:
                raise SystemExit(
                    "%s@%s: post-relocate.sh wrote %d file(s) OUTSIDE the package "
                    "tree. `bits cvmfs-publish` only publishes the package's own "
                    "directory, so these would be silently dropped and never reach "
                    "CVMFS. Publishing out-of-package files is not yet implemented "
                    "(needs a MODULES_STAGING / shared-path channel). Offending "
                    "files (relative to the relocate work dir):\n  %s"
                    % (pkg, vdir, len(_leaked),
                       "\n  ".join(os.path.relpath(f, work_dir) for f in _leaked)))

        relativise_symlinks(pkgroot)
        sanitize(pkgroot)
        _fp = tree_fingerprint(pkgroot)   # content of the tree that goes into the tar
        mtime = _publish_mtime(spec, ctx)   # the time its published files get

        if not skip_pkg:
            _tfd, pkg_tar = tempfile.mkstemp(suffix=".tar", dir=ctx.get("tmp_dir") or None)
            os.close(_tfd)
            subprocess.run(["tar", "-cf", pkg_tar, "--hard-dereference",
                            "--mtime=@%d" % mtime,
                            "-C", pkgroot, "."], check=True)
            _lbl = "%s@%s(pkg)" % (pkg, vdir)
            jid = _publish_tar(ctx, path, pkg_tar, _lbl, fp=_fp,
                               identity=path if publish_once else "",
                               identity_hash=spec.get("hash", ""), replace=replace)
            jobs.append((jid, _lbl))

        # Modulefile: a package that ships etc/modulefiles/<pkg> publishes it as
        # a SECOND prepub job at the modules path (mirrors the CI loop) -- only
        # without a fixed modules directory, which module_links fills instead.
        modfile = os.path.join(pkgroot, "etc", "modulefiles", pkg)
        spec["_modulefile"] = os.path.isfile(modfile)   # for module_links
        if spec["_modulefile"] and not link_modules:
            mod_path = resolve_pkg_path(
                pkgroot, ctx["repo"], pkg, vdir, ver, rev, ctx.get("platform", ""),
                ctx.get("install_dir", ""), commit, ctx.get("user", ""), family,
                # One modules dir per build: {arch} is the build arch here, not
                # the package's own (own_hash toolchain / noarch).
                kind="modules", tmpl_prefix=ctx["tmpl_prefix"],
                arch=ctx["arch"] or arch,
                prefix_fallback=ctx.get("prefix_fallback"),
                templates=ctx.get("templates"))
            if mod_path:
                _mfd, mod_tar = tempfile.mkstemp(suffix=".tar", dir=ctx.get("tmp_dir") or None)
                os.close(_mfd)
                # The modulefile must be published under the VERSION name (the
                # environment-modules 'name/version' convention), so it lands at
                # <modules_path>/<vdir>, e.g. Modules/modulefiles/ROOT/v6-36-10-alice2-2
                # — NOT .../ROOT/ROOT. The package ships it as etc/modulefiles/<pkg>,
                # so stage a copy named <vdir> and add that as the tar entry.
                import shutil
                _mstage = tempfile.mkdtemp(prefix="mod-", dir=ctx.get("tmp_dir") or None)
                try:
                    shutil.copy2(modfile, os.path.join(_mstage, vdir))
                    subprocess.run(["tar", "-cf", mod_tar, "--hard-dereference",
                                    "--mtime=@%d" % mtime,
                                    "-C", _mstage, vdir], check=True)
                finally:
                    _safe_rmtree(_mstage)
                _mlbl = "%s@%s(modules)" % (pkg, vdir)
                mjid = _publish_tar(ctx, mod_path, mod_tar, _mlbl,
                                    fp=tree_fingerprint(os.path.dirname(modfile)),
                                    identity="%s/%s" % (mod_path, vdir) if publish_once else "")
                jobs.append((mjid, _mlbl))
    finally:
        _safe_rmtree(work_dir)
    if skip_pkg and not jobs:
        raise AlreadyPublished(path)
    return jobs


def _hash8(s):
    import hashlib
    return hashlib.sha1(s.encode()).hexdigest()[:8]


def _safe_rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


def _safe_rmtree(p):
    import shutil
    shutil.rmtree(p, ignore_errors=True)


def stage_tar(repo, tar_path, path, job_id, stratum0_url,
              no_stats_db=False, no_prepare_lock=False, swissknife=None,
              base_root=None, replace_on_conflict=False):
    """Run `bits cvmfs-stage` in-process (cvmfs_stage_cmd.main) and return
    (staging_prefix, catalog_hash). Reuses ALL of cvmfs-stage's logic (prepare,
    D16 base retry, subtree-catalog walk, probe) rather than reimplementing it.
    base_root pins the base revision (else cvmfs-stage reads the current root):
    the verify hook uses it to re-prepare against the base BEFORE the package was
    published, avoiding the add-only UNIQUE conflict.

    Runs cvmfs-stage as a SUBPROCESS, not in-process: cvmfs_stage_cmd.main prints
    to stdout and capturing that via redirect_stdout mutates sys.stdout
    process-wide, which is NOT thread-safe. A subprocess has its own stdout, so
    several publish_one() may run concurrently in a thread pool."""
    argv = ["--repo", repo, "--path", path, "--tar", tar_path,
            "--job-id", job_id, "--stratum0-url", stratum0_url]
    if base_root:
        argv += ["--base-root", base_root]
    if no_stats_db:
        argv.append("--no-stats-db")
    if no_prepare_lock:
        argv.append("--no-prepare-lock")
    if swissknife:
        argv += ["--swissknife", swissknife]
    bits_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = bits_root + (os.pathsep + env["PYTHONPATH"]
                                     if env.get("PYTHONPATH") else "")
    def _run(extra):
        return subprocess.run(
            [sys.executable, "-c",
             "from bits_helpers.cvmfs_stage_cmd import main; import sys; sys.exit(main())",
             *argv, *extra],
            env=env, capture_output=True, text=True)
    p = _run([])
    first_err = p.stderr or ""
    # Republish: retry with --replace ONLY when the prepare failed because the
    # path is ALREADY PUBLISHED. cvmfs-stage's add-only attempt confirms that
    # against the repository ("It IS in the repository") — an in-tar duplicate
    # hits the SAME swissknife UNIQUE (catalog.md5path) but reports "NOT
    # CONFIRMED", i.e. a packaging bug, which must NOT delete anything. --replace
    # makes the prepare delete-then-add; the prepub daemon's own
    # replace_on_conflict then does the repo-level graft (both must be on).
    if (p.returncode != 0 and replace_on_conflict
            and "It IS in the repository" in first_err):
        p = _run(["--replace"])
        if p.returncode != 0:
            # Keep the original add-only failure: it carries the clearer verdict.
            raise SystemExit(
                "cvmfs-stage --replace retry failed for %s (rc=%s): %s\n"
                "  original add-only failure: %s"
                % (path, p.returncode, (p.stderr or "")[-800:], first_err[-800:]))
    if p.returncode != 0:
        raise SystemExit("cvmfs-stage failed for %s (rc=%s): %s"
                         % (path, p.returncode, first_err[-800:]))
    prefix = catalog = ""
    for line in p.stdout.splitlines():
        if line.startswith("BITS_STAGING_PREFIX="):
            prefix = line.split("=", 1)[1]
        elif line.startswith("BITS_CATALOG_HASH="):
            catalog = line.split("=", 1)[1]
    if not prefix or not catalog:
        raise SystemExit("cvmfs-stage returned no prefix/hash for %s" % path)
    return prefix, catalog


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="bits cvmfs publish")
    ap.add_argument("--fingerprint", default="",
                    help="print the content fingerprint of a directory tree and "
                         "exit (the CI uses this to fingerprint its own relocated "
                         "tree with the identical algorithm); other args ignored")
    ap.add_argument("--manifest")   # required unless --fingerprint/--tar (checked below)
    ap.add_argument("--tar", action="append", default=[], metavar="FILE",
                    help="publish overlay tar(s) at the repo root (e.g. the bundle "
                         "tar from 'bits preload') via the staged/ingest path, "
                         "instead of a build manifest. Repeatable.")
    ap.add_argument("--repo")
    ap.add_argument("--one", help="publish only this package (increment-1 test)")
    ap.add_argument("--tars-root", default=os.path.join(
        os.environ.get("BITS_WORK_DIR", ""), "TARS"))
    ap.add_argument("--arch", default=os.environ.get("BITS_ARCH", ""))
    ap.add_argument("--tmpl-prefix", default="")
    ap.add_argument("--prefix-fallback", default=os.environ.get("CVMFS_PREFIX_FALLBACK", ""))
    ap.add_argument("--stratum0-url", default=os.environ.get("BITS_STRATUM0_URL", ""))
    ap.add_argument("--prepub-url", default=os.environ.get("PREPUB_URL", ""))
    ap.add_argument("--token", default=os.environ.get("PREPUB_API_TOKEN", ""))
    ap.add_argument("--platform", default=os.environ.get("PLATFORM", ""))
    ap.add_argument("--install-dir", default=os.environ.get("CVMFS_INSTALL_DIR", ""))
    ap.add_argument("--user", default="")
    ap.add_argument("--job-id-base", default=os.environ.get("CI_JOB_ID", "local"))
    ap.add_argument("--build-id", default=os.environ.get("CI_PIPELINE_ID", ""))
    ap.add_argument("--base-root", default="",
                    help="pin the base revision for the prepare (else the current "
                         "published root is read); the verify hook passes the "
                         "base the package was published against")
    ap.add_argument("--bearer-auth", action="store_true",
                    help="send the token as a Bearer header (default: sign)")
    ap.add_argument("--swissknife", default="")
    ap.add_argument("--no-stats-db", action="store_true")
    ap.add_argument("--no-prepare-lock", action="store_true")
    ap.add_argument("--direct-s3", action="store_true",
                    help="ingest only: add direct_s3 so cvmfs_server writes data "
                         "objects straight to S3, bypassing the gateway. No effect "
                         "on the staged path.")
    ap.add_argument("--object-list", action="store_true",
                    help="ingest with --direct-s3 only: the publisher reports each "
                         "data object it stored to prepub")
    ap.add_argument("--prewarm", action="store_true",
                    help="ingest with --object-list only: prepub announces the "
                         "stored objects so the Stratum 1s pull them right after "
                         "the commit")
    ap.add_argument("--publish-path", choices=("staged", "ingest"), default="ingest",
                    help="ingest (default): POST the tar itself and let prepub's "
                         "gateway chunk it (the tar IS the payload). staged: "
                         "prepare objects here with cvmfs-stage and submit a light "
                         "job. Both order biggest-first and run concurrently at "
                         "N>1; ingest ignores the cvmfs-stage flags below.")
    ap.add_argument("--replace-on-conflict", action="store_true",
                    help="REPLACE a package that another "
                         "build published at its path (a merged view that differs "
                         "is replaced without it): when the published hash "
                         "differs from this build's, prepub deletes the old "
                         "subtree and commits this one (prior revisions keep "
                         "objects until GC). The same hash is skipped as usual. "
                         "REQUIRES prepub with replace_on_conflict; bits checks "
                         "that before uploading. Release views, aliases and "
                         "modulefiles are never replaced. On the staged path the "
                         "add-only prepare also retries with `cvmfs-stage --replace`, "
                         "and prepub needs its ingest path too (it deletes with it).")
    ap.add_argument("--workers", type=int, default=1,
                    help="prepare up to N packages concurrently, biggest tar "
                         "first. Default 1 = serial, manifest "
                         "order (today's behaviour). Staged path only: N>1 needs "
                         "--no-stats-db + --no-prepare-lock (concurrent prepares).")
    ap.add_argument("--release-view", action="store_true",
                    help="also make the release: the release links (one symlink "
                         "per package at its cvmfs_releases_template path) and the "
                         "merged view (cvmfs_views_template), each when the build "
                         "has that template, from this build's packages alone. "
                         "Only when every package published.")
    ap.add_argument("--force-view", action="store_true",
                    help="with --release-view: replace a merged view that was made "
                         "from other top-level packages")
    ap.add_argument("--wait", action="store_true",
                    help="wait for every submitted job to be published (packages "
                         "before the release); a job that fails fails the command")
    ap.add_argument("--wait-timeout", type=int, default=3600,
                    help="seconds to wait for each job with --wait (default 3600)")
    ap.add_argument("--dry-run", action="store_true",
                    help="stage but do NOT submit — prints DRYRUN(prefix|hashC), "
                         "so the catalog hash can be checked without a graft")
    a = ap.parse_args(argv)
    # prepub refuses these combinations with a 400; say so before any upload.
    if a.object_list and not (a.publish_path == "ingest" and a.direct_s3):
        ap.error("--object-list requires --publish-path ingest and --direct-s3")
    if a.prewarm and not a.object_list:
        ap.error("--prewarm requires --object-list")

    if a.fingerprint:
        print(tree_fingerprint(a.fingerprint))
        return 0
    if a.tar:
        if not a.repo:
            ap.error("--tar requires --repo")
        ctx = {"repo": a.repo, "stratum0_url": a.stratum0_url,
               "prepub_url": a.prepub_url, "token": a.token,
               "job_id_base": a.job_id_base, "build_id": a.build_id,
               "swissknife": a.swissknife or None, "base_root": a.base_root or None,
               "bearer_auth": a.bearer_auth, "no_stats_db": a.no_stats_db,
               "no_prepare_lock": a.no_prepare_lock,
               "replace_on_conflict": a.replace_on_conflict,
               "publish_path": a.publish_path, "direct_s3": a.direct_s3,
               "object_list": a.object_list, "prewarm": a.prewarm,
               "submit": not a.dry_run,
               "tmp_dir": os.path.join(os.environ.get("BITS_WORK_DIR", "/tmp"), "tmp")}
        os.makedirs(ctx["tmp_dir"], exist_ok=True)
        for tar, jid in zip(a.tar, publish_overlay_tars(ctx, a.tar)):
            print("published overlay %s -> %s" % (tar, jid))
        return 0
    if not a.manifest or not a.repo:
        ap.error("--manifest and --repo are required")
    if a.dry_run:
        a.workers = 1   # dry-run is single-package verification — keep it serial
                        # so publish_one's FINGERPRINT print cannot interleave.
    # The stats-db / prepare-lock only exist on the staged path (cvmfs-stage);
    # ingest POSTs the tar and does no local prepare, so N>1 needs nothing extra.
    if (a.workers > 1 and a.publish_path == "staged"
            and not (a.no_stats_db and a.no_prepare_lock)):
        ap.error("--workers > 1 on the staged path requires --no-stats-db and "
                 "--no-prepare-lock: concurrent prepares otherwise abort on the "
                 "shared statistics database and the per-host prepare lock")

    with open(a.manifest) as fh:
        _man = json.load(fh)
    pkgs = _man.get("packages") or []
    # The publishing build's layout places the whole closure (reused packages
    # included); a manifest without it falls back to each package's .meta.json.
    templates = _man.get("cvmfs_templates") or None
    sys.stderr.write("[publish] layout: %s\n" % (
        "%s (this build's templates)" % templates.get("path") if templates
        else "each package's own .meta.json (manifest has no cvmfs_templates)"))
    ctx = {"repo": a.repo, "tars_root": a.tars_root, "arch": a.arch,
           "tmpl_prefix": a.tmpl_prefix, "prefix_fallback": a.prefix_fallback or None,
           "templates": templates, "build_time": _man.get("created_at"),
           "stratum0_url": a.stratum0_url, "prepub_url": a.prepub_url, "token": a.token,
           "platform": a.platform, "install_dir": a.install_dir, "user": a.user,
           "job_id_base": a.job_id_base, "swissknife": a.swissknife or None,
           "build_id": a.build_id, "bearer_auth": a.bearer_auth,
           "base_root": a.base_root or None,
           "no_stats_db": a.no_stats_db, "no_prepare_lock": a.no_prepare_lock,
           "replace_on_conflict": a.replace_on_conflict, "publish_path": a.publish_path,
           "direct_s3": a.direct_s3, "object_list": a.object_list,
           "prewarm": a.prewarm,
           "submit": not a.dry_run, "tmp_dir": os.path.join(
               os.environ.get("BITS_WORK_DIR", "/tmp"), "tmp")}
    os.makedirs(ctx["tmp_dir"], exist_ok=True)

    from bits_helpers.utilities import is_virtual_package
    from bits_helpers.sync import binary_redistributable

    def _publishable(s):
        # virtual / repository-loader packages produce nothing for CVMFS
        if is_virtual_package(s):
            return False
        # binaries not redistributable (redistributable: sources|none, legacy
        # false, unknown -> fail closed): never in public CVMFS. Same gate as
        # the store uploads.
        if not binary_redistributable(s):
            sys.stderr.write("[publish] SKIPPED %s@%s: redistributable: %s\n" % (
                s.get("package"), s.get("version"), s.get("redistributable")))
            return False
        if a.one and s.get("package") != a.one:
            return False
        return True
    publishable = [s for s in pkgs if _publishable(s)]

    if a.release_view and a.one:
        ap.error("--release-view publishes a whole release; it cannot be combined with --one")
    # The release: its links and its merged view are independent of each other
    # (the view links into the packages, not through the links), each made when
    # the build has its template.
    rel_links = rel_view = False
    if a.release_view:
        tm = templates or {}
        why = ("the group has no cvmfs_packages_template" if not tm.get("packages")
               else "the build has no release (the main line)" if not tm.get("release")
               else "")
        rel_links = not why and bool(tm.get("path")) and tm["path"] != tm["packages"]
        rel_view = not why and bool(tm.get("views"))
        if not (rel_links or rel_view):
            sys.stderr.write("[publish] no release: %s\n" % (
                why or "the group has neither cvmfs_releases_template nor "
                       "cvmfs_views_template"))
    in_view = []   # specs the release view links to (published now or before)

    def _run_one(spec):
        # Returns (rc, lines) — never raises, so one bad package fails the batch
        # without tearing down the pool. publish_one's own errors are SystemExit.
        try:
            jobs = publish_one(spec, ctx)
            if not jobs:
                return 0, ["SKIPPED %s@%s: no tarball in %s" % (
                    spec.get("package"), spec.get("version"), ctx["tars_root"])]
            in_view.append(spec)
            return 0, ["PUBLISHED %s %s" % (jid, label) for jid, label in jobs]
        except AlreadyPublished as exc:
            in_view.append(spec)
            return 0, ["SKIPPED %s@%s: already published at %s" % (
                spec.get("package"), spec.get("version"), exc)]
        except (SystemExit, Exception) as exc:
            return 1, ["FAILED %s: %s" % (spec.get("package"), exc)]

    submitted = []   # (job id, label) this run sent, for --wait

    def _emit(out):
        # Emit one package's lines atomically (whole list at once, from the main
        # thread) so nothing interleaves, but stream per package so a serial run
        # shows progress and does not lose finished work if killed mid-run.
        for ln in out:
            (sys.stderr if ln.startswith("FAILED") else sys.stdout).write(ln + "\n")
            parts = ln.split(None, 2)
            if len(parts) == 3 and parts[0] == "PUBLISHED" and parts[1]:
                submitted.append((parts[1], parts[2]))
        sys.stdout.flush(); sys.stderr.flush()

    wait = a.wait and ctx["submit"]
    if wait:
        _wait = lambda: _wait_for(dict(ctx, wait_timeout=a.wait_timeout), submitted, _emit)

    rc = 0
    if a.workers > 1:
        # Biggest-first: the longest prepare (chunk/compress/upload to S3) starts
        # first, so it does not land on the tail and gate the window.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        ordered = order_biggest_first(publishable, ctx["tars_root"], a.arch)
        # Cross-check: print the biggest-first order with sizes (to stderr, so it
        # does not disturb the PUBLISHED stdout the CI parses). If the size source
        # is degenerate this shows as manifest order with equal sizes.
        sys.stderr.write("[publish] fan-out: %d workers; biggest-first order, %d packages:\n"
                         % (a.workers, len(ordered)))
        for i, s in enumerate(ordered, 1):
            sys.stderr.write("  %3d. %9s  %s@%s\n" % (
                i, _human(payload_size(s, ctx["tars_root"], a.arch)),
                s.get("package", ""), s.get("version", "")))
        sys.stderr.flush()
        with ThreadPoolExecutor(max_workers=a.workers) as ex:
            for fut in as_completed([ex.submit(_run_one, s) for s in ordered]):
                r, out = fut.result(); rc |= r; _emit(out)
    else:
        for spec in publishable:               # serial, manifest order = today
            r, out = _run_one(spec); rc |= r; _emit(out)

    # BASE/1.0 for the modulefiles, once per arch (a failure here is reported,
    # not fatal: the next publish adds it).
    base = base_module(ctx) if not rc and not a.one else None
    if base:
        mods, text = base
        state = published_state(ctx, mods + "/BASE/1.0") if ctx["submit"] else None
        if state and state.get("exists"):
            _emit(["SKIPPED BASE: already published at %s/BASE" % mods])
        else:
            import shutil
            stage = tempfile.mkdtemp(prefix="base-", dir=ctx["tmp_dir"])
            try:
                with open(os.path.join(stage, "1.0"), "w") as fh:
                    fh.write(text)
                fd, btar = tempfile.mkstemp(suffix=".tar", dir=ctx["tmp_dir"])
                os.close(fd)
                subprocess.run(["tar", "-cf", btar, "-C", stage, "1.0"], check=True)
                label = "BASE@%s(modules)" % mods
                _emit(["PUBLISHED %s %s" % (_publish_tar(ctx, mods + "/BASE", btar, label), label)])
            except (SystemExit, Exception) as exc:
                sys.stderr.write("[publish] WARNING: BASE module not published: %s\n" % exc)
            finally:
                shutil.rmtree(stage, ignore_errors=True)

        # Packages that live outside <build arch>/Packages (noarch, the own_hash
        # toolchain) get a symlink there, so $BASEDIR/<pkg>/<ver-rev> finds them.
        pkgs_root = fixed_dir(ctx, "packages")
        aliases = []
        for s_ in in_view:
            alias, real = native_path(s_, ctx), package_path(s_, ctx)
            if alias != real and os.path.dirname(os.path.dirname(alias)) == pkgs_root:
                st = published_state(ctx, alias) if ctx["submit"] else None
                if not (st and st.get("exists")):
                    aliases.append((alias, real))
        if aliases:
            label = "aliases@%s" % pkgs_root
            jid, err = publish_links(ctx, pkgs_root, aliases, label)
            # err, not jid, decides: a dry run publishes with an empty job id.
            _emit(["PUBLISHED %s %s" % (jid, label)] if err is None else
                  ["FAILED %d package alias(es) in %s: %s" % (len(aliases), pkgs_root, err)])
            rc |= 0 if err is None else 1

    # The modulefiles: one job linking each new package's own modulefile into
    # the modules directory, after the packages. Only packages that published
    # (or were there) get one, so this need not wait for a clean run.
    mlinks = module_links(ctx, in_view) if (templates or {}).get("packages") else None
    if mlinks and mlinks[1]:
        mroot, links = mlinks
        label = "modulefiles@%s" % mroot
        jid, err = publish_links(ctx, mroot, links, label)
        _emit(["PUBLISHED %s %s" % (jid, label)] if err is None else
              ["FAILED %d modulefile link(s) in %s: %s" % (len(links), mroot, err)])
        rc |= 0 if err is None else 1

    # The release goes in only over a complete set of packages, and with --wait
    # only once they are published, so it never links what never landed.
    if (rel_links or rel_view) and not rc and wait:
        rc |= _wait()
    if rel_links or rel_view:
        if rc:
            _emit(["FAILED release: not created, some packages failed"])
        elif in_view:
            ordered_view = dependency_order(in_view)
            if rel_links:
                rc |= _publish_release_links(ctx, ordered_view, _emit)
            if rel_view:
                rc |= _publish_merged_view(ctx, ordered_view, _emit,
                                           top_level=_man.get("requested_packages"),
                                           force=a.force_view)
    if wait:
        rc |= _wait()
    return rc


def dependency_order(packages):
    """*packages* with each one after the packages it requires (by name, within
    the set), ties broken by name: the same set gives the same order whichever
    order it came in (the build's, or its BOMs')."""
    by_name = {p["package"]: p for p in packages}
    done, out = set(), []

    def visit(p, seen):
        if p["package"] in done or p["package"] in seen:
            return
        seen = seen | {p["package"]}
        for dep in sorted(p.get("requires") or []):
            if dep in by_name:
                visit(by_name[dep], seen)
        done.add(p["package"])
        out.append(p)
    for name in sorted(by_name):
        visit(by_name[name], frozenset())
    return out


def _view_member(spec, ctx):
    """What a merged view records of a package it links."""
    return {"package": spec["package"], "version": spec.get("version", ""),
            "revision": spec.get("revision", ""), "hash": spec.get("hash", ""),
            "path": package_path(spec, ctx), "view": spec.get("view", True)}


def _view_owner(ctx, view_path):
    """The top-level packages the published view at *view_path* records, or
    None when it records none or cannot be read."""
    meta_path = view_path + "/.meta.json"
    got = published_files(ctx, [meta_path])
    if got is None:
        raise SystemExit("cannot read %s through prepub to see which build it "
                         "was made from; --force-view replaces it" % meta_path)
    meta = got.get(meta_path)
    owner = meta.get("top_level") if isinstance(meta, dict) else None
    return sorted(owner) if isinstance(owner, list) and owner else None


def _publish_merged_view(ctx, specs, emit, top_level=None, force=False):
    """The release's merged view (cvmfs_views_template), one per release and
    arch, made from this build's packages alone (*specs*, in dependency order:
    the later package wins a collision). A published view that differs is
    replaced: making the view is the request. One recorded for other top-level
    packages is replaced only with *force*. Returns the rc contribution."""
    import hashlib
    import shutil
    view_path = fixed_dir(ctx, "views")
    if not view_path:
        emit(["FAILED merged view: cvmfs_views_template %r is not a fixed directory"
              % ctx["templates"]["views"]])
        return 1
    top_level = sorted(top_level or [])
    state = published_state(ctx, view_path) if ctx["submit"] else None
    exists = bool(state and state.get("exists"))
    if exists and not state.get("hash"):
        emit(["SKIPPED merged view: %s was published without a fingerprint "
              "and cannot be replaced" % view_path])
        return 0
    staging = tempfile.mkdtemp(prefix="mview-", dir=ctx["tmp_dir"])
    try:
        # mkdtemp makes it 0700, and the tar's "." entry gives the published
        # view root that mode: nobody but the owner could enter it.
        os.chmod(staging, 0o755)
        res = merged_view(ctx, specs, staging, view_path)
        for path, winner, loser in res["conflicts"]:
            sys.stderr.write("[publish] merged view: %s from %s, not %s\n"
                             % (path, winner, loser))
        env, problems = view_environment(ctx, specs, view_path, top_level)
        for why in problems:
            sys.stderr.write("[publish] merged view: setup.sh does not set what the "
                             "modules do: %s\n" % why)
        write_view_setup(staging, "/cvmfs/%s/%s" % (ctx["repo"], view_path), env)
        # The one top-level module setup.sh stands in for, when it does.
        top = next((sp for sp in specs if sp["package"] in top_level), None) \
            if not problems and len(top_level) == 1 else None
        if top:
            marker = os.path.join(staging, setup_marker(top))
            with open(marker, "w") as fh:
                fh.write("# setup.sh sets what loading %s/%s sets\n"
                         % (top["package"], _vdir(top)))
            os.chmod(marker, 0o644)
        # The view's identity: what prepub compares to decide to replace it.
        # Modes are hashed, so they must not depend on the publisher's umask.
        for dp, dns, _fns in os.walk(staging):
            for d in dns:
                os.chmod(os.path.join(dp, d), 0o755)
        recorded = sorted((_view_member(m, ctx) for m in specs), key=lambda m: m["package"])
        # The members are part of it: a package that links nothing is still one.
        fp = hashlib.sha256((tree_fingerprint(staging) + json.dumps(
            recorded, sort_keys=True)).encode()).hexdigest()
        replace = False
        if exists:
            if state.get("hash") == fp:
                emit(["SKIPPED merged view: %s is unchanged" % view_path])
                return _publish_view_pointer(ctx, top, view_path, emit) if top else 0
            owner = _view_owner(ctx, view_path) if not force else None
            if owner and top_level and owner != top_level:
                emit(["FAILED merged view: %s was made from %s, not %s; "
                      "--force-view replaces it" % (view_path, ", ".join(owner),
                                                    ", ".join(top_level))])
                return 1
            if not prepub_replace_allowed(ctx):
                emit(["FAILED merged view: %s differs, and this prepub does not allow "
                      "replacing (replace_on_conflict on the node)" % view_path])
                return 1
            replace = True
        with open(os.path.join(staging, ".meta.json"), "w") as f:
            json.dump({"package": {"package": "merged-view", "hash": fp},
                       "top_level": top_level, "build_id": ctx.get("build_id", ""),
                       "members": recorded,
                       "setup": {"modules": not problems, "problems": problems[:50]}},
                      f, indent=1)
        os.chmod(os.path.join(staging, ".meta.json"), 0o644)
        # No .cvmfscatalog here: both publish paths ingest with create-catalog-on-
        # root (-c / -C true), which adds the marker itself; a second one fails.
        fd, tar = tempfile.mkstemp(suffix=".tar", dir=ctx["tmp_dir"])
        os.close(fd)
        subprocess.run(["tar", "-cf", tar, "-C", staging, "."], check=True)
        label = "merged-view@%s" % view_path
        sys.stderr.write("[publish] merged view %s: %d packages, %d links, %d conflicts\n"
                         % (view_path, len(specs), len(res["linked"]), len(res["conflicts"])))
        jid = _publish_tar(ctx, view_path, tar, label, identity=view_path,
                           identity_hash=fp, replace=replace)
        emit(["PUBLISHED %s %s" % (jid, label)])
        return _publish_view_pointer(ctx, top, view_path, emit) if top else 0
    except (SystemExit, Exception) as exc:
        emit(["FAILED merged view: %s" % exc])
        return 1
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def setup_marker(spec):
    """The file in a release's view that says its setup.sh sets what loading
    the release's top-level module *spec* sets: there only while the view is
    one made for that very module."""
    return ".setup-complete.%s.%s" % (spec["package"], _vdir(spec))


def view_pointer(spec, ctx):
    """Where the modules directory points from a release's top-level module to
    its view: <modules dir>/<pkg>/.<ver-rev>.view, a link to the view's
    setup_marker (a file, not a modulefile, so modulecmd passes it over). A view
    replaced by another release's has no such marker: the link then dangles."""
    mod = _spec_path(spec, ctx, "modules")
    return mod and "%s/.%s.view" % (mod, _vdir(spec))


def _publish_view_pointer(ctx, spec, view_path, emit):
    """The link from the release's top-level module *spec* to its view
    (view_pointer), unless it is published already. Returns the rc contribution."""
    root = (fixed_dir(ctx, "modules")
            if (ctx.get("templates") or {}).get("modules") else None)
    link = view_pointer(spec, ctx) if root else None
    if not link:
        return 0
    st = published_state(ctx, link) if ctx["submit"] else None
    if st and st.get("exists"):
        return 0
    label = "view-pointer@%s" % root
    jid, err = publish_links(ctx, root, [(link, "%s/%s" % (view_path, setup_marker(spec)))],
                             label)
    emit(["PUBLISHED %s %s" % (jid, label)] if err is None else
         ["FAILED view pointer %s: %s" % (link, err)])
    return 0 if err is None else 1


def _publish_release_links(ctx, specs, emit):
    """The release links (cvmfs_releases_template): one symlink per package at
    its releases path. The release root is shared (earlier builds, other
    platforms) and ingest only adds, so only the missing links are sent; an
    existing link is kept as it is. Returns the rc contribution."""
    root = view_root(ctx)
    links = []
    for s_ in specs:
        vpath = _spec_path(s_, ctx, "view")
        if not vpath:
            continue
        st = published_state(ctx, vpath) if ctx["submit"] else None
        if not (st and st.get("exists")):
            links.append((vpath, package_path(s_, ctx)))
    label = "release-view@%s" % root
    if not links:
        emit(["SKIPPED %s: every link is already published" % label])
        return 0
    jid, err = publish_links(ctx, root, links, label)
    if err is None:   # (a dry run publishes with an empty job id)
        emit(["PUBLISHED %s %s" % (jid, label)])
        return 0
    emit(["FAILED release view (%s)" % err])
    return 1


def _wait_for(ctx, jobs, emit):
    """Poll each (job id, label) to a final state. Returns the rc contribution."""
    from bits_helpers.prepub import poll_job
    rc = 0
    for jid, label in jobs:
        try:
            state = poll_job(ctx["prepub_url"], ctx["token"], jid,
                             timeout=ctx.get("wait_timeout", 3600),
                             bearer_auth=ctx.get("bearer_auth", False),
                             no_verify_tls=ctx.get("no_verify_tls", False))
        except (SystemExit, Exception) as exc:
            why = str(exc) if str(exc) not in ("", "0", "1") else "see the log above"
            state = "not published (failed, aborted or timed out: %s)" % why
        if state != "published":
            emit(["FAILED %s: job %s %s" % (label, jid, state)])
            rc = 1
    del jobs[:]
    return rc


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        assert expand_tmpl("{family}{pkg}/{version}", pkg="ROOT", version="v6",
                           family="MCGenerators") == "MCGenerators/ROOT/v6"
        assert expand_tmpl("{pkg}/{version}", pkg="O2", version="daily") == "O2/daily"
        assert repo_relative_path("/cvmfs/r/el9/Packages/O2/1.0", "r") == "el9/Packages/O2/1.0"
        assert repo_relative_path("/cvmfs/bits.cern.ch/alice/P/X", "test.cvmfs.io",
                                  meta_root="/cvmfs/bits.cern.ch/alice",
                                  prefix_fallback="/cvmfs/test.cvmfs.io") == "P/X"
        print("cvmfs_publish pure-helper self-check: OK")
    else:
        sys.exit(main())
