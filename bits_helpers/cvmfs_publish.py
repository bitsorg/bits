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
    n = float(n)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return ("%.0f%s" % (n, u)) if u == "B" else ("%.1f%s" % (n, u))
        n /= 1024.0


def payload_size(spec, tars_root, default_arch):
    """Best size for LPT ordering: the install du the build records in the GC
    sentinel (<sw>/.packages/<arch>/<pkg>/<ver-rev>) — the real payload, present
    for EVERY outcome including reused artefacts — else the (uniform ~4 KB)
    publish tar, else 0. Never raises; any miss degrades to the tar/zero
    fallback. The tar must not be the primary key: uniform across packages, it
    collapsed the sort to manifest order and sent the biggest payload last (§32)."""
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
    unit starts first and does not tail the window (MEASUREMENTS §31/§32). Size
    is payload_size (the GC sentinel du). Stable within a size."""
    return sorted(specs, key=lambda s: payload_size(s, tars_root, default_arch),
                  reverse=True)


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

    tar_gz = tar_path(spec, ctx["tars_root"], ctx["arch"])
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

        _tfd, pkg_tar = tempfile.mkstemp(suffix=".tar", dir=ctx.get("tmp_dir") or None)
        os.close(_tfd)
        subprocess.run(["tar", "-cf", pkg_tar, "--hard-dereference",
                        "-C", pkgroot, "."], check=True)
        try:
            prefix, catalog = stage_tar(
                ctx["repo"], pkg_tar, path,
                "%s-%s" % (ctx["job_id_base"], _hash8(path)), ctx["stratum0_url"],
                no_stats_db=ctx.get("no_stats_db", False),
                no_prepare_lock=ctx.get("no_prepare_lock", False),
                swissknife=ctx.get("swissknife"), base_root=ctx.get("base_root"),
                replace_on_conflict=ctx.get("replace_on_conflict", False))
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

        # Modulefile: a package that ships etc/modulefiles/<pkg> publishes it as
        # a SECOND prepub job at the modules path (mirrors the CI loop).
        modfile = os.path.join(pkgroot, "etc", "modulefiles", pkg)
        if os.path.isfile(modfile):
            mod_path = resolve_pkg_path(
                pkgroot, ctx["repo"], pkg, vdir, ver, rev, ctx.get("platform", ""),
                ctx.get("install_dir", ""), commit, ctx.get("user", ""), family,
                kind="modules", tmpl_prefix=ctx["tmpl_prefix"], arch=arch,
                prefix_fallback=ctx.get("prefix_fallback"))
            if mod_path:
                _mfd, mod_tar = tempfile.mkstemp(suffix=".tar", dir=ctx.get("tmp_dir") or None)
                os.close(_mfd)
                # arcname is the bare filename (tar -C the modulefiles dir, add
                # <pkg>) so prepub extracts it as <modules_path>/<pkg>.
                subprocess.run(["tar", "-cf", mod_tar, "--hard-dereference",
                                "-C", os.path.dirname(modfile), pkg], check=True)
                try:
                    mprefix, mcat = stage_tar(
                        ctx["repo"], mod_tar, mod_path,
                        "%s-%s" % (ctx["job_id_base"], _hash8(mod_path)),
                        ctx["stratum0_url"], no_stats_db=ctx.get("no_stats_db", False),
                        no_prepare_lock=ctx.get("no_prepare_lock", False),
                        swissknife=ctx.get("swissknife"), base_root=ctx.get("base_root"),
                        replace_on_conflict=ctx.get("replace_on_conflict", False))
                finally:
                    _safe_rm(mod_tar)
                if ctx.get("submit", True):
                    mjid = submit_staged(
                        ctx["prepub_url"], ctx["token"], ctx["repo"], mod_path,
                        mprefix, mcat, build_id=ctx.get("build_id", ""),
                        bearer_auth=ctx.get("bearer_auth", False),
                        no_verify_tls=ctx.get("no_verify_tls", False))
                else:
                    mjid = mcat
                jobs.append((mjid, "%s@%s(modules)" % (pkg, vdir)))
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
    ap.add_argument("--replace-on-conflict", action="store_true",
                    help="REPUBLISH: if a package's path is already published, "
                         "the add-only prepare fails on a UNIQUE conflict; retry "
                         "it once with `cvmfs-stage --replace`, which deletes the "
                         "existing subtree inside the prepared revision (prior "
                         "revisions keep objects until GC) and re-adds the new "
                         "content. New paths are unaffected. REQUIRES the prepub "
                         "daemon to also run with replace_on_conflict, else the "
                         "graft still refuses (ADR-0011 D17).")
    ap.add_argument("--workers", type=int, default=1,
                    help="prepare up to N packages concurrently, biggest tar "
                         "first (MEASUREMENTS §31). Default 1 = serial, manifest "
                         "order (today's behaviour). N>1 needs --no-stats-db + "
                         "--no-prepare-lock (concurrent prepares).")
    ap.add_argument("--dry-run", action="store_true",
                    help="stage but do NOT submit — prints DRYRUN(prefix|hashC), "
                         "so the catalog hash can be checked without a graft")
    a = ap.parse_args(argv)

    if a.fingerprint:
        print(tree_fingerprint(a.fingerprint))
        return 0
    if not a.manifest or not a.repo:
        ap.error("--manifest and --repo are required")
    if a.dry_run:
        a.workers = 1   # dry-run is single-package verification — keep it serial
                        # so publish_one's FINGERPRINT print cannot interleave.
    if a.workers > 1 and not (a.no_stats_db and a.no_prepare_lock):
        ap.error("--workers > 1 requires --no-stats-db and --no-prepare-lock: "
                 "concurrent prepares otherwise abort on the shared statistics "
                 "database and the per-host prepare lock (MEASUREMENTS §28)")

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
           "replace_on_conflict": a.replace_on_conflict,
           "submit": not a.dry_run, "tmp_dir": os.path.join(
               os.environ.get("BITS_WORK_DIR", "/tmp"), "tmp")}
    os.makedirs(ctx["tmp_dir"], exist_ok=True)

    def _publishable(s):
        # virtual / repository-loader packages produce nothing for CVMFS
        if s.get("provides_repository") or s.get("package") == "defaults-release":
            return False
        # non-redistributable: kept in the store, never in public CVMFS. Exact
        # replica of the CI (jq `.redistributable != false`): only a literal
        # boolean false excludes; the current enum values never do.
        if s.get("redistributable") is False:
            return False
        if a.one and s.get("package") != a.one:
            return False
        return True
    publishable = [s for s in pkgs if _publishable(s)]

    def _run_one(spec):
        # Returns (rc, lines) — never raises, so one bad package fails the batch
        # without tearing down the pool. publish_one's own errors are SystemExit.
        try:
            jobs = publish_one(spec, ctx)
            return 0, ["PUBLISHED %s %s" % (jid, label) for jid, label in jobs]
        except (SystemExit, Exception) as exc:
            return 1, ["FAILED %s: %s" % (spec.get("package"), exc)]

    def _emit(out):
        # Emit one package's lines atomically (whole list at once, from the main
        # thread) so nothing interleaves, but stream per package so a serial run
        # shows progress and does not lose finished work if killed mid-run.
        for ln in out:
            (sys.stderr if ln.startswith("FAILED") else sys.stdout).write(ln + "\n")
        sys.stdout.flush(); sys.stderr.flush()

    rc = 0
    if a.workers > 1:
        # Biggest-first: the longest prepare (chunk/compress/upload to S3) starts
        # first, so it does not land on the tail and gate the window (§31).
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
