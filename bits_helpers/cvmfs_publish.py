# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits cvmfs-publish — producer-side staged publish of a build's packages.

This is the Python home for what cvmfs-prepub-publish.yml did per package in
bash: resolve the CVMFS path from the package's own .meta.json, untar, relocate,
relativise absolute symlinks, sanitize, tar, stage (bits cvmfs-stage) and submit
to prepub. Concentrating it here lets the packages of one build be prepared
CONCURRENTLY and biggest-first — the lever MEASUREMENTS §31 identified — with a
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

# ── pure helpers (unit-testable, no I/O) ─────────────────────────────────────

_TOKENS = ("pkg", "tag", "version", "revision", "platform",
           "install_dir", "commit", "user", "family")


def expand_tmpl(tmpl, pkg="", tag="", version="", revision="", platform="",
                install_dir="", commit="", user="", family=""):
    """Port of the CI `_expand_tmpl`: substitute {token}s into a path template.

    {family} carries its OWN trailing slash when non-empty (templates use the
    adjacent form {family}{pkg}); an empty family collapses to just {pkg}.
    {release} is already baked into the template by the build — untouched here.
    """
    fam_seg = (family + "/") if family else ""
    subst = {"pkg": pkg, "tag": tag, "version": version, "revision": revision,
             "platform": platform, "install_dir": install_dir, "commit": commit,
             "user": user, "family": fam_seg}
    out = tmpl
    for k, v in subst.items():
        out = out.replace("{%s}" % k, v)
    return out


def repo_relative_path(p, repo, meta_root=None, prefix_fallback=None):
    """Port of the CI `_repo_relative_path`: turn an absolute /cvmfs/<repo>/...
    path into a repo-relative lease path, re-rooting a reused artefact whose
    .meta.json root differs from the community prefix. Raises ValueError when the
    result is not under /cvmfs/<repo>/ (a prefix/community mismatch)."""
    if (prefix_fallback and meta_root and meta_root != prefix_fallback
            and p.startswith(meta_root + "/")):
        p = prefix_fallback + p[len(meta_root):]
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
                     prefix_fallback=None):
    """Resolve the repo-relative publish path from the package's own .meta.json
    cvmfs_templates (kind='path' for the package tree, 'modules' for the module
    file), mirroring the CI loop. Returns None when the kind has no template."""
    meta = os.path.join(pkgroot, ".meta.json")
    with open(meta) as fh:
        tm = (json.load(fh).get("cvmfs_templates") or {})
    meta_root = tm.get("prefix") or None
    # Template selection is ARCH-DRIVEN, exactly as the CI loop: a shared (noarch)
    # package uses the shared template (falling back to path); everything else
    # uses the path template. Selecting shared unconditionally would resolve a
    # different repo path and change the relocated bytes → a different hash.
    if kind == "path":
        key = (tm.get("shared") or tm.get("path")) if arch == "shared" else tm.get("path")
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
                    user=user, family=family)
    return repo_relative_path(p, repo, meta_root, prefix_fallback)


# ── staged submit (the CI does this with raw curl; not in prepub.submit_job) ──

def submit_staged(prepub_url, token, repo, path, staging_prefix, catalog_hash,
                  build_id="", bearer_auth=False, no_verify_tls=False):
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


def _locate_pkgroot(work_dir):
    """The dir holding .meta.json inside the extracted tar (mirrors the CI find)."""
    for dp, _dns, fns in os.walk(work_dir):
        if ".meta.json" in fns:
            return dp
    raise SystemExit("cannot locate package root (.meta.json) under %s" % work_dir)


def publish_one(spec, ctx):
    """Full producer pipeline for ONE package, staged path. Mirrors the CI loop
    body: locate tar -> untar -> resolve path -> relocate -> relativise -> tar ->
    cvmfs-stage -> submit; plus the modulefile as a second job. Returns a list of
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

    tar_gz = os.path.join(ctx["tars_root"], arch, pkg,
                          "%s-%s.%s.tar.gz" % (pkg, vdir, arch))
    if not os.path.isfile(tar_gz):
        return []   # system-provided: no tar, nothing to publish

    work_dir = tempfile.mkdtemp(prefix="pub-", dir=ctx.get("tmp_dir") or None)
    jobs = []
    try:
        subprocess.run(["tar", "-xzf", tar_gz, "-C", work_dir], check=True)
        pkgroot = _locate_pkgroot(work_dir)

        path = resolve_pkg_path(
            pkgroot, ctx["repo"], pkg, vdir, ver, rev, ctx.get("platform", ""),
            ctx.get("install_dir", ""), commit, ctx.get("user", ""), family,
            kind="path", tmpl_prefix=ctx["tmpl_prefix"], arch=arch,
            prefix_fallback=ctx.get("prefix_fallback"))
        if not path:
            raise SystemExit("%s@%s has no cvmfs_templates path in .meta.json"
                             % (pkg, vdir))

        reloc = os.path.join(pkgroot, "relocate-me.sh")
        if os.path.isfile(reloc):
            env = dict(os.environ,
                       INSTALL_BASE="/cvmfs/%s/%s" % (ctx["repo"], path),
                       WORK_DIR=work_dir, BITS_RELOCATE_STRIP_PP="1")
            # Run it exactly as the CI does: cwd=work_dir, script named RELATIVE
            # to it (the CI passes ${_pkgpath}/relocate-me.sh), so $0 matches.
            subprocess.run(["bash", "-e", os.path.relpath(reloc, work_dir)],
                           cwd=work_dir, env=env, check=True)
            for dp, _dn, fns in os.walk(pkgroot):
                for f in fns:
                    if f.endswith(".unrelocated"):
                        os.remove(os.path.join(dp, f))

        relativise_symlinks(pkgroot)
        sanitize(pkgroot)
        _fp = tree_fingerprint(pkgroot)   # content of the tree that goes into the tar

        pkg_tar = tempfile.mktemp(suffix=".tar", dir=ctx.get("tmp_dir") or None)
        subprocess.run(["tar", "-cf", pkg_tar, "--hard-dereference",
                        "-C", pkgroot, "."], check=True)
        try:
            prefix, catalog = stage_tar(
                ctx["repo"], pkg_tar, path,
                "%s-%s" % (ctx["job_id_base"], _hash8(path)), ctx["stratum0_url"],
                no_stats_db=ctx.get("no_stats_db", False),
                no_prepare_lock=ctx.get("no_prepare_lock", False),
                swissknife=ctx.get("swissknife"), base_root=ctx.get("base_root"))
        finally:
            _safe_rm(pkg_tar)
        # `catalog` is already the C-suffixed hash (cvmfs-stage prints
        # BITS_CATALOG_HASH=<40hex>C) — do NOT append another C.
        if ctx.get("submit", True):
            jid = submit_staged(ctx["prepub_url"], ctx["token"], ctx["repo"],
                                path, prefix, catalog, build_id=ctx.get("build_id", ""),
                                bearer_auth=ctx.get("bearer_auth", False),
                                no_verify_tls=ctx.get("no_verify_tls", False))
        else:
            jid = catalog   # --dry-run: the catalog hash (already <hash>C), for verify
            # mtime-independent content fingerprint — the reliable equivalence
            # check (the catalog hash embeds relocation-time mtimes).
            print("FINGERPRINT %s %s@%s(pkg)" % (_fp, pkg, vdir))
        jobs.append((jid, "%s@%s(pkg)" % (pkg, vdir)))
        # NOTE: modulefile job (etc/modulefiles/<pkg>) is added next increment.
    finally:
        _safe_rmtree(work_dir)
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
              base_root=None):
    """Run `bits cvmfs-stage` in-process (cvmfs_stage_cmd.main) and return
    (staging_prefix, catalog_hash). Reuses ALL of cvmfs-stage's logic (prepare,
    D16 base retry, subtree-catalog walk, probe) rather than reimplementing it.
    base_root pins the base revision (else cvmfs-stage reads the current root):
    the verify hook uses it to re-prepare against the base BEFORE the package was
    published, avoiding the add-only UNIQUE conflict."""
    import io
    from contextlib import redirect_stdout
    from bits_helpers import cvmfs_stage_cmd
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
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cvmfs_stage_cmd.main(argv)
    if rc != 0:
        raise SystemExit("cvmfs-stage failed for %s (rc=%s)" % (path, rc))
    prefix = catalog = ""
    for line in buf.getvalue().splitlines():
        if line.startswith("BITS_STAGING_PREFIX="):
            prefix = line.split("=", 1)[1]
        elif line.startswith("BITS_CATALOG_HASH="):
            catalog = line.split("=", 1)[1]
    if not prefix or not catalog:
        raise SystemExit("cvmfs-stage returned no prefix/hash for %s" % path)
    return prefix, catalog


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="bits cvmfs-publish")
    ap.add_argument("--fingerprint", default="",
                    help="print the content fingerprint of a directory tree and "
                         "exit (the CI uses this to fingerprint its own relocated "
                         "tree with the identical algorithm); other args ignored")
    ap.add_argument("--manifest")   # required unless --fingerprint (checked below)
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
    ap.add_argument("--dry-run", action="store_true",
                    help="stage but do NOT submit — prints DRYRUN(prefix|hashC), "
                         "so the catalog hash can be checked without a graft")
    a = ap.parse_args(argv)

    if a.fingerprint:
        print(tree_fingerprint(a.fingerprint))
        return 0
    if not a.manifest or not a.repo:
        ap.error("--manifest and --repo are required")

    with open(a.manifest) as fh:
        pkgs = (json.load(fh).get("packages") or [])
    ctx = {"repo": a.repo, "tars_root": a.tars_root, "arch": a.arch,
           "tmpl_prefix": a.tmpl_prefix, "prefix_fallback": a.prefix_fallback or None,
           "stratum0_url": a.stratum0_url, "prepub_url": a.prepub_url, "token": a.token,
           "platform": a.platform, "install_dir": a.install_dir, "user": a.user,
           "job_id_base": a.job_id_base, "swissknife": a.swissknife or None,
           "build_id": a.build_id, "bearer_auth": a.bearer_auth,
           "base_root": a.base_root or None,
           "no_stats_db": a.no_stats_db, "no_prepare_lock": a.no_prepare_lock,
           "submit": not a.dry_run, "tmp_dir": os.path.join(
               os.environ.get("BITS_WORK_DIR", "/tmp"), "tmp")}
    os.makedirs(ctx["tmp_dir"], exist_ok=True)

    rc = 0
    for spec in pkgs:
        if spec.get("provides_repository") or spec.get("package") == "defaults-release":
            continue
        if a.one and spec.get("package") != a.one:
            continue
        try:
            for jid, label in publish_one(spec, ctx):
                print("PUBLISHED %s %s" % (jid, label))
        except SystemExit as exc:
            print("FAILED %s: %s" % (spec.get("package"), exc), file=sys.stderr)
            rc = 1
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
