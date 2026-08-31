# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits publish — copy, relocate, and stream a built package to a CVMFS ingestion spool.

Pipeline on the build host
---------------------------
1. Locate the package's immutable INSTALLROOT under *workDir*.
2. ``rsync`` it to a temporary CVMFS working copy (scratch directory).
3. Run ``relocate-me.sh`` inside the copy, rewriting all embedded paths to
   the final CVMFS target path.
4. Start an ``inotifywait`` watcher on the working copy *before* relocation
   so that every file written by the relocation script is immediately queued
   for transfer; relocation and transfer therefore overlap in time.
5. ``rsync`` each modified file (or the whole tree on systems without
   inotifywait) to the ingestion spool ``incoming/<pkg-id>/`` directory.
6. Write a ``<pkg-id>.done`` sentinel to the spool inbox.  The ingestion
   daemon treats sentinel arrival as the signal that all file content has
   landed and it can begin finalisation for this package.
7. Remove the working copy from the scratch directory.

The original INSTALLROOT under *workDir* is never modified.
"""

import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from os.path import abspath, basename, exists, join

from bits_helpers.log import debug, error, info, warning, banner
from bits_helpers.arch import detectArch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_installroot(work_dir, architecture, package, version=None):
    """Return the path to the installed package tree.

    Prefers the ``latest`` symlink when *version* is not given.  When
    *version* is supplied the function looks for an exact match first, then
    falls back to any directory whose name starts with *version*.

    Raises ``SystemExit`` when nothing is found.
    """
    base = join(abspath(work_dir), architecture)
    # FIX: normalise the joined path and verify it stays inside `base`.
    # A package name like '../../etc' would otherwise make pkg_base point
    # outside the work directory, allowing arbitrary directory traversal.
    pkg_base = os.path.normpath(join(base, package))
    if not pkg_base.startswith(base + os.sep):
        error("Package name %r escapes the work directory — path traversal rejected", package)
        sys.exit(1)
    if not exists(pkg_base):
        error("No installation found for %s under %s", package, base)
        sys.exit(1)

    if version:
        # Exact match first, then prefix match.
        for entry in sorted(os.listdir(pkg_base)):
            if entry == version or entry.startswith(version + "-"):
                candidate = join(pkg_base, entry)
                if os.path.isdir(candidate):
                    return candidate
        error("Version %s of %s not found under %s", version, package, pkg_base)
        sys.exit(1)

    latest = join(pkg_base, "latest")
    if os.path.islink(latest):
        resolved = os.path.join(pkg_base, os.readlink(latest))
        if exists(resolved):
            return resolved

    # Fall back to the lexicographically last directory.
    entries = sorted(
        e for e in os.listdir(pkg_base) if os.path.isdir(join(pkg_base, e))
    )
    if not entries:
        error("No installed version of %s found under %s", package, pkg_base)
        sys.exit(1)
    return join(pkg_base, entries[-1])


def _pkg_id(package, version_dir, architecture):
    """Return a filesystem-safe identifier for this package instance.

    Format: ``<pkg>-<ver_rev>-<arch>`` with slashes replaced by underscores.

    All three components have '/' replaced so that the resulting ID is always a
    single path segment — it can never escape ``spool/incoming/`` via traversal.
    """
    # FIX: replace '/' in package just as we do for architecture and version_dir.
    # Without this, a package name like '../../etc' would produce a pkg_id that
    # traverses out of spool/incoming/ when used as a path component.
    pkg_tag  = package.replace("/", "_")
    arch_tag = architecture.replace("/", "_").replace("-", "_")
    ver_tag  = version_dir.replace("/", "_")
    return f"{pkg_tag}-{ver_tag}-{arch_tag}"


def _load_manifest_spec(work_dir, package, version):
    """Newest build-manifest entry for *package* (optionally pinned to *version*)."""
    import glob as _glob
    import json as _json
    pats = [join(work_dir, "MANIFESTS", "bits-manifest-*.json"),
            join(work_dir, "bits-manifest-*.json")]
    files = sorted({f for p in pats for f in _glob.glob(p) if not f.endswith("latest.json")},
                   key=os.path.getmtime, reverse=True)
    for f in files:
        try:
            with open(f) as _fh:
                data = _json.load(_fh)
        except Exception:
            continue
        for e in data.get("packages", []):
            if e.get("package") != package:
                continue
            if version and e.get("version") not in (version, version.split("-")[0]):
                continue
            return e
    return None


def _publish_s3(package, version, architecture, work_dir, write_store, parser, dry_run=False):
    """Upload an already-built package's tarball to the S3 write store for reuse."""
    from bits_helpers.sync import remote_from_url
    e = _load_manifest_spec(work_dir, package, version)
    if not e or not e.get("hash"):
        parser.error("no built manifest entry for %s%s in %s — build it first"
                     % (package, (" " + version) if version else "", work_dir))
    spec = {"package": e["package"], "version": e.get("version"),
            "revision": e.get("revision"), "hash": e["hash"]}
    arch = e.get("effective_architecture") or architecture
    if dry_run:
        banner("[dry-run] would publish %s-%s (%s) to %s"
               % (spec["package"], spec.get("version"), spec["hash"][:12], write_store))
        return
    banner("Publishing %s-%s to S3 store %s" % (spec["package"], spec.get("version"), write_store))
    writer = remote_from_url(write_store, write_store, arch, work_dir)
    writer.upload_symlinks_and_tarball(spec)
    info("Uploaded %s (%s) to the S3 store.", spec["package"], spec["hash"][:12])


def _newest_manifest_file(work_dir):
    """Path to the newest build manifest (bits-manifest-*.json), or None."""
    import glob as _glob
    pats = [join(work_dir, "MANIFESTS", "bits-manifest-*.json"),
            join(work_dir, "bits-manifest-*.json")]
    files = sorted({f for p in pats for f in _glob.glob(p) if not f.endswith("latest.json")},
                   key=os.path.getmtime, reverse=True)
    return files[0] if files else None


def _normalize_s3_store(url):
    """Return a write-store URL the boto3 backend understands, deriving the S3
    endpoint from an https URL when needed.

      https://<host>/[swift/v1/]<bucket>[/...]  -> b3://<bucket> (+ endpoint, path-style)
      b3://<bucket> / s3://<bucket> / rsync:...  -> passthrough
    """
    if url.startswith(("b3://", "s3://", "rsync:", "cvmfs://")):
        return url
    m = re.match(r"^https?://([^/]+)/(?:swift/v1/)?([^/?#]+)", url)
    if not m:
        return url
    host, bucket = m.group(1), m.group(2)
    os.environ.setdefault("BITS_S3_ENDPOINT_URL", "https://%s" % host)
    os.environ.setdefault("S3_ADDRESSING_STYLE", "path")  # CERN RGW path-style
    return "b3://%s" % bucket


def _run_leaf():
    """A per-run, per-host filename so concurrent publishes never overwrite.

    ``<shorthost>-<UTC>-<rand>.json``. The random suffix makes it collision-proof
    even for two publishes on the same host within the same second; combined with
    the deterministic build_id folder, concurrent publishes land side by side.
    """
    import socket
    import datetime
    import binascii
    host = re.sub(r"[^A-Za-z0-9._-]", "_", socket.gethostname().split(".")[0]) or "host"
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rnd = binascii.hexlify(os.urandom(4)).decode()
    return "%s-%s-%s.json" % (host, ts, rnd)


def _publish_from_manifest(architecture, work_dir, store_url, parser, manifest=None, dry_run=False):
    """Bulk-upload every built package in a manifest to the S3 store.

    *manifest* is a manifest file path, or 'latest'/None to use the newest under
    WORKDIR/MANIFESTS. With *dry_run*, list what would be uploaded and touch no
    network (no credentials needed).
    """
    import json as _json
    from bits_helpers.sync import remote_from_url
    if manifest and manifest != "latest":
        man = os.path.abspath(os.path.expanduser(manifest))
        if not os.path.isfile(man):
            parser.error("manifest file not found: %s" % man)
    else:
        man = _newest_manifest_file(work_dir)
        if not man:
            parser.error("no build manifest under %s — build something first, "
                         "or pass a path to --from-manifest" % work_dir)
    try:
        with open(man) as _fh:
            manifest_doc = _json.load(_fh)
        entries = manifest_doc.get("packages", [])
    except Exception as exc:
        parser.error("could not read manifest %s: %s" % (man, exc))
    if not entries:
        parser.error("no packages listed in manifest %s" % man)
    write_store = _normalize_s3_store(store_url)
    from bits_helpers.utilities import resolve_store_path
    from bits_helpers.checksum import checksum_file
    import glob as _glob

    from bits_helpers.utilities import ver_rev

    def _store_tarball(arch, entry):
        """The local store tarball for *entry*, chosen by NAME — not by glob order.

        A hash directory can legitimately hold more than one tarball: the upload
        HEAD-skip is keyed on hash AND file name, so a hash that was once uploaded
        under two revision labels keeps both (see build.py). Returning glob()[0]
        therefore picked an arbitrary one, while sync.py uploads the object named
        from ver_rev(spec) — so the BOM could record the checksum of one file while
        the store received the other. That is a manifest/store sha256 mismatch
        produced by a single, consistent build, and `bits certify` rejects it
        (fail-closed: it cannot tell that apart from tampering). Two publishes of
        the same build could even disagree with each other, since glob order is not
        guaranteed.

        Select the exact name the upload will send. If it is missing, fall back to
        a SORTED pick so at least two runs agree, and say so.
        """
        h = entry.get("hash") or ""
        sdir = os.path.join(work_dir, resolve_store_path(arch, h))
        if not os.path.isdir(sdir):
            return None
        spec = {"package":  entry.get("package", ""),
                "version":  entry.get("version"),
                "revision": entry.get("revision")}
        want = "{package}-{ver_rev}.{arch}.tar.gz".format(
            package=spec["package"], ver_rev=ver_rev(spec), arch=arch)
        path = os.path.join(sdir, want)
        if os.path.isfile(path):
            others = sorted(os.path.basename(t)
                            for t in _glob.glob(os.path.join(sdir, "*.tar.gz"))
                            if os.path.basename(t) != want)
            if others:
                debug("%s %s: hash dir also holds %s; publishing %s (the name the "
                      "upload writes)", spec["package"], h[:12], ", ".join(others), want)
            return path
        tars = sorted(_glob.glob(os.path.join(sdir, "*.tar.gz")))
        if tars:
            warning("  %s %s: no tarball named %s — falling back to %s. The BOM will "
                    "record that file's checksum, so verify it matches the store.",
                    spec["package"], h[:12], want, os.path.basename(tars[0]))
        return tars[0] if tars else None

    banner("%s from %s to %s",
           "[dry-run] would publish" if dry_run else "Publishing", basename(man), write_store)
    writers = {}
    done = set()
    published = []      # (entry, store-tarball-path) actually/would-be uploaded
    n = ok = 0
    for e in entries:
        h = e.get("hash")
        pkg = e.get("package", "")
        if not h or h in done:
            continue
        done.add(h)
        # defaults-* are config pseudo-packages: not reusable artifacts, and their
        # content/metadata can leak environment specifics. Never publish them.
        if pkg.startswith("defaults"):
            debug("skip config package %s", pkg)
            continue
        # redistributable: sources|none — the binary must not be uploaded to
        # the (possibly world-readable) store, and consequently never enters
        # the BOM either: what is not in the store cannot be certified or
        # reused. Parsed with the gates' own parser, so legacy booleans in old
        # manifests keep working (false == none).
        from bits_helpers.sync import redistributable_forms
        if "redistributable" in e and \
           "binaries" not in redistributable_forms(e.get("redistributable")):
            info("  skip %s — redistributable: %s (licence forbids binary "
                 "redistribution)", pkg, e.get("redistributable"))
            continue
        arch = e.get("effective_architecture") or architecture
        tar = _store_tarball(arch, e)
        # Only packages with a content-addressed store tarball can be uploaded.
        if not tar:
            warning("  skip %s %s — no local store tarball", pkg, h[:12])
            continue
        n += 1
        if dry_run:
            info("  [%d] %s %s %s", n, pkg, h[:12], basename(tar))
            ok += 1
            published.append((e, tar))
            continue
        writer = writers.get(arch) or writers.setdefault(
            arch, remote_from_url(write_store, write_store, arch, work_dir))
        spec = {"package": pkg, "version": e.get("version"),
                "revision": e.get("revision"), "hash": h}
        try:
            writer.upload_symlinks_and_tarball(spec)
            # The upload path records the sha256 of the object actually in the
            # store (kept-as-found or freshly uploaded). It overrides whatever
            # the build manifest recorded: the store object is authoritative,
            # and the build's locally-packed bytes may legitimately differ
            # (.tar.gz is not byte-reproducible). The BOM below must describe
            # the stored bytes or `bits certify` will reject it.
            if spec.get("store_tarball_sha256"):
                e = dict(e, tarball_sha256=spec["store_tarball_sha256"])
            ok += 1
            published.append((e, tar))
            info("  [%d] %s %s", n, pkg, h[:12])
        except Exception as exc:
            error("  FAILED %s (%s): %s", pkg, h[:12], exc)

    # Upload a MINIMAL BOM manifest under MANIFESTS/ (trust-relevant fields only;
    # drop variables/patches/source_checksums, which bloat it and can leak config)
    # so a CI job can fetch and sign it. Fill tarball/tarball_sha256 from the
    # uploaded store tarball when the build manifest did not record them.
    # Only emit the BOM when every candidate uploaded: a partial publish must not
    # produce a manifest that certification would treat as a complete build.
    build_id = bom = None
    if not dry_run and ok != n:
        error("%d of %d package(s) failed to upload — not writing a BOM manifest "
              "for a partial publish", n - ok, n)
    if not dry_run and ok == n:
        w = next(iter(writers.values()), None)
        if w is not None and hasattr(w, "s3") and getattr(w, "writeStore", None):
            # completed_at is kept per package so every hash carries "when built".
            # license/redistributable are the compliance metadata: the BOM (and
            # the signed manifest merged from it) must know what may be laid
            # into a public CVMFS tree and what attribution a NOTICE file needs.
            _keep = ("package", "version", "revision", "effective_architecture",
                     "hash", "commit_hash", "pkg_family", "built_by", "completed_at",
                     "license", "redistributable")
            packages = []
            for e, tar in published:
                m = {k: e[k] for k in _keep if k in e}
                m["tarball"] = e.get("tarball") or basename(tar)
                m["tarball_sha256"] = e.get("tarball_sha256") or checksum_file(tar)
                packages.append(m)
            import tempfile as _tf, getpass, socket, datetime
            from bits_helpers.provenance import build_id_from_manifest
            # Canonical, deterministic build_id (same id the build itself used):
            # <label>-<digest> over the full package set. Two hosts building the
            # same release agree on it, so it names the release in the bucket.
            build_id = build_id_from_manifest(manifest_doc) or "unknown"
            leaf = _run_leaf()
            try:
                _user = getpass.getuser()
            except Exception:
                _user = "unknown"
            # A BOM is per-platform: partition the entries by their effective
            # architecture ("shared" — noarch — is just another platform) and
            # emit one BOM per architecture, with the architecture in the file
            # name. This is the invariant that lets certification be scoped per
            # platform (a BOM pairs with exactly one common-manifest-<arch>) —
            # bits_helpers.certify.bom_architecture() refuses mixed BOMs.
            by_arch = {}
            for m in packages:
                by_arch.setdefault(
                    m.get("effective_architecture") or "shared", []).append(m)
            _provenance = {
                "build_id": build_id,
                "published_by": "%s@%s" % (_user, socket.gethostname().split(".")[0]),
                "published_at": datetime.datetime.now(datetime.timezone.utc)
                                    .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "architecture": architecture,
            }
            bom = []                    # [(effective_arch, bom_dict), ...]
            _stem = leaf[:-len(".json")]
            # One temp dir for ALL per-arch BOMs, removed when done (the
            # previous mkdtemp-per-arch was never cleaned up and leaked one
            # directory per architecture per publish). A BOM upload failure is
            # a PUBLISH failure: continuing would leave S3 with a partial arch
            # set and open a certification MR for a mismatched subset.
            _bomdir = _tf.mkdtemp(prefix="bits-bom-")
            _bom_failed = None
            try:
                for _arch in sorted(by_arch):
                    doc = dict(_provenance,
                               effective_architecture=_arch,
                               packages=by_arch[_arch])
                    arch_leaf = "%s.%s.json" % (_stem, _arch)
                    tmp = os.path.join(_bomdir, arch_leaf)
                    with open(tmp, "w") as fh:
                        _json.dump(doc, fh, indent=1)
                    # Folder per release (identify the build) + unique leaf per
                    # run (concurrent publishes never clobber each other).
                    key = "MANIFESTS/%s/%s" % (build_id, arch_leaf)
                    try:
                        w.s3.upload_file(tmp, w.writeStore, key)
                        info("Manifest (minimal BOM, %d pkgs, %s, build_id=%s) -> %s/%s",
                             len(by_arch[_arch]), _arch, build_id, w.writeStore, key)
                        bom.append((_arch, doc))
                    except Exception as exc:
                        error("manifest upload failed for %s: %s", _arch, exc)
                        _bom_failed = _arch
                        break
            finally:
                import shutil as _shutil
                _shutil.rmtree(_bomdir, ignore_errors=True)
            if _bom_failed is not None:
                error("BOM upload failed for architecture %s — aborting the "
                      "publish (no certification MR will be opened; already-"
                      "uploaded BOMs of this run remain and are harmless: "
                      "certification is scoped per architecture and a re-run "
                      "supersedes them)", _bom_failed)
                sys.exit(1)
            # NOTICE + LICENSE-SOURCE-OFFER.txt next to the release's BOMs:
            # attribution and the GPL source offer are discharged mechanically
            # from the FULL manifest entries (which carry license,
            # redistributable and the source archives' store paths).
            # Best-effort — a compliance-file failure never fails a publish.
            from bits_helpers.notice import upload_release_compliance
            upload_release_compliance(w.s3, w.writeStore, build_id, entries,
                                      store_url=store_url)

    banner("%s %d package(s) to %s",
           "[dry-run] would publish" if dry_run else "Published", ok, write_store)
    if not dry_run and n and ok != n:
        sys.exit(1)
    # Best-effort: refresh the S3 store-usage gauges on VM. Publish (including a
    # re-publish, which never runs `bits build`) is a store mutation, and the
    # Monitoring dashboard's store bar reads these gauges via the snapshot —
    # without this push the bar goes stale/empty between builds.
    if not dry_run and ok:
        _mon = (os.environ.get("METRICS_URL") or "").strip().rstrip("/")
        _w = next(iter(writers.values()), None)
        if _mon and _w is not None and getattr(_w, "s3", None) and getattr(_w, "writeStore", None):
            from bits_helpers import store_stats as _ss
            _pub_ep = (os.environ.get("BITS_S3_ENDPOINT_URL") or os.environ.get("S3_ENDPOINT_URL")
                       or os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL"))
            _ss.push_store_gauges(_w.s3, _w.writeStore, _mon, work_dir=work_dir,
                                  store=getattr(_w, "remoteStore", "") or _w.writeStore,
                                  endpoint=_pub_ep)
    if dry_run or not bom:
        return None
    return build_id, bom, _system_from_manifest(manifest_doc)


def _system_from_manifest(manifest_doc):
    """Return the defaults ``system:`` block for this build (non-hashed policy).

    Prefer a ``system`` snapshot recorded in the manifest; else load it live from
    the build's ``config_dir`` + ``defaults`` (works on the build host without a
    rebuild). Returns {} when unavailable. Used to default publish/certify knobs
    (certify_group, manifests_remote) from the community's defaults.
    """
    if isinstance(manifest_doc.get("system"), dict):
        return manifest_doc["system"]
    cfg = manifest_doc.get("config_dir")
    defs = manifest_doc.get("defaults") or []
    if not cfg or not os.path.isdir(cfg):
        return {}
    try:
        from bits_helpers.utilities import readDefaults
        meta, _ = readDefaults(cfg, defs, lambda _m: None, None)
        sysd = meta.get("system")
        return sysd if isinstance(sysd, dict) else {}
    except Exception:
        return {}


def _submit_certification_mr(args, parser, build_id, bom):
    """Open a merge request adding this build's manifest(s) to the manifests repo.

    *bom* is a list of ``(effective_architecture, bom_dict)`` — one per-platform
    BOM per architecture the build produced ("shared" included) — committed as
    one commit / one MR, each file named with its architecture so certification
    can be scoped per platform straight from the file list.

    Uses the GitLab REST API with the caller's PAT (works with SSH push — only the
    host/path are parsed from the remote). The MR *author* is the PAT owner, whose
    admin status CI validates before signing. Records nothing locally.
    """
    import json as _json
    from bits_helpers import forge
    group = getattr(args, "certifyGroup", None)
    if not group:
        parser.error("--certify needs --certify-group <group> (the manifests/<group>/ to submit to)")
    remote = getattr(args, "manifestsRemote", None)
    if not remote:
        parser.error("--certify needs --manifests-remote <git URL of the bits-manifests project>")
    api_url, project = forge.parse_git_remote(remote)
    if not api_url:
        parser.error("could not parse --manifests-remote: %s" % remote)
    token = forge.resolve_gitlab_token(getattr(args, "gitlabToken", None))
    if not token:
        parser.error("no GitLab token to open the certification MR — set one in "
                     "~/.bits/gitlab-token, $BITS_CERTIFIER_TOKEN, or pass --gitlab-token")
    # Target the repo's actual default branch unless one was given, so this works
    # whether the manifests repo defaults to main or master.
    target = getattr(args, "certifyRef", None)
    if not target:
        try:
            target = forge.gitlab_default_branch(api_url, token, project)
        except Exception:
            target = None
        target = target or "main"
    # Record the human certifier in the committed manifest (audit trail in the
    # manifests-repo git history). Used when a bot opens the MR on behalf of a
    # human whose authority was already verified upstream (e.g. bits-console).
    certifier = getattr(args, "certifier", None) or os.environ.get("GITLAB_USER_LOGIN")
    if certifier:
        bom = [(a, dict(d, certified_by=[certifier])) for a, d in bom]
    leaf = _run_leaf()                                   # host-UTC-rand, unique
    branch = "certify/%s-%s" % (re.sub(r"[^A-Za-z0-9._-]", "_", build_id), leaf[:-5])
    files = [("manifests/%s/%s.%s.%s" % (group, build_id, arch, leaf),
              _json.dumps(doc, indent=1)) for arch, doc in bom]
    title = "Certify %s (%s)" % (build_id, group)
    try:
        forge.gitlab_create_commit(api_url, token, project, branch, target,
                                   files, None, title)
        mr = forge.gitlab_create_merge_request(api_url, token, project, branch, target, title)
    except Exception as exc:
        parser.error("failed to open the certification MR on %s: %s" % (project, exc))
    banner("Certification MR opened on %s: %s", project,
           mr.get("web_url") or ("!%s" % mr.get("iid")))


def _spool_is_remote(spool):
    """Return True when *spool* is a remote ``[user@]host:path`` spec."""
    # A single colon that is not a Windows drive letter indicates remote.
    return bool(re.match(r'^(?:[^/]+@)?[^/:]+:.+', spool))


def _rsync_to_spool(src, spool, pkg_id, extra_opts=None, remove_source=False):
    """rsync *src* (file or directory) to ``<spool>/incoming/<pkg_id>/``.

    *spool* may be a local path or a remote ``[user@]host:path``.
    """
    dest_base = f"{spool}/incoming/{pkg_id}/"
    cmd = ["rsync", "-a", "--mkpath"]
    if remove_source:
        cmd.append("--remove-source-files")
    if extra_opts:
        cmd.extend(shlex.split(extra_opts))
    cmd += [src, dest_base]
    debug("rsync: %s", " ".join(shlex.quote(c) for c in cmd))
    result = subprocess.run(cmd, check=False)
    if result.returncode not in (0, 24):   # 24 = "vanished source files" — benign
        error("rsync failed with exit code %d", result.returncode)
        sys.exit(result.returncode)


def _write_sentinel(spool, pkg_id, cvmfs_target, rsync_opts=None):
    """Write and transfer the ``.done`` sentinel for *pkg_id*.

    The sentinel is a small text file that carries the *cvmfs_target* so the
    ingestion daemon can construct graft paths without additional out-of-band
    configuration.
    """
    # FIX: the sentinel uses a line-oriented key=value format; a newline in
    # either value would inject a spurious field that the ingestion daemon
    # might misinterpret.  Reject before writing.
    for _field, _val in (("pkg_id", pkg_id), ("cvmfs_target", cvmfs_target)):
        if "\n" in _val or "\r" in _val:
            raise ValueError(
                f"Sentinel field '{_field}' contains a newline character which "
                f"would corrupt the sentinel file: {_val!r}"
            )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".done", prefix=pkg_id, delete=False
    ) as fh:
        fh.write(f"pkg_id={pkg_id}\ncvmfs_target={cvmfs_target}\n")
        sentinel_path = fh.name

    dest = f"{spool}/incoming/{pkg_id}.done"
    if _spool_is_remote(spool):
        cmd = ["rsync", "-a"]
        if rsync_opts:
            cmd.extend(shlex.split(rsync_opts))
        cmd += [sentinel_path, dest]
    else:
        os.makedirs(f"{spool}/incoming", exist_ok=True)
        cmd = ["cp", sentinel_path, dest]

    debug("sentinel: %s -> %s", sentinel_path, dest)
    result = subprocess.run(cmd, check=False)
    os.unlink(sentinel_path)
    if result.returncode != 0:
        error("Failed to write sentinel (exit %d)", result.returncode)
        sys.exit(result.returncode)


# ---------------------------------------------------------------------------
# inotifywait-based streaming transfer
# ---------------------------------------------------------------------------

def _stream_with_inotify(copy_dir, spool, pkg_id, rsync_opts=None):
    """Watch *copy_dir* with inotifywait and rsync each closed file immediately.

    Returns a watcher ``Popen`` object.  The caller must call
    ``watcher.terminate()`` after relocation is complete and all queued files
    have been transferred.

    Falls back to ``None`` (silent no-op) when inotifywait is not available;
    in that case the caller performs a single bulk rsync after relocation.
    """
    if shutil.which("inotifywait") is None:
        debug("inotifywait not available — will fall back to bulk rsync after relocation")
        return None

    # inotifywait outputs one line per event: "<dir> <event> <filename>"
    inotify_cmd = [
        "inotifywait",
        "--monitor",
        "--recursive",
        "--format", "%w%f",
        "--event", "close_write",
        copy_dir,
    ]
    debug("starting inotifywait: %s", " ".join(inotify_cmd))
    watcher = subprocess.Popen(
        inotify_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    # Drain the watcher output in a background thread so we don't block.
    import threading

    def _drain():
        for line in watcher.stdout:
            path = line.rstrip("\n")
            if not path or not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, copy_dir)
            dest_dir = f"{spool}/incoming/{pkg_id}/{os.path.dirname(rel)}"
            if not _spool_is_remote(spool):
                os.makedirs(dest_dir, exist_ok=True)
            _rsync_to_spool(path, spool, join(pkg_id, os.path.dirname(rel)).rstrip("/"),
                            extra_opts=rsync_opts)

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    return watcher


# ---------------------------------------------------------------------------
# Main publish entry point
# ---------------------------------------------------------------------------

def doPublish(args, parser):
    """Orchestrate the build-host publishing pipeline.

    Two mutually exclusive delivery paths are supported:

    Legacy spool path (bits-ingest + bits-cvmfs-publisher runners):
        Requires ``--spool``.  Rsyncs the relocated tree to the spool's
        ``incoming/<pkg_id>/`` directory and writes a ``.done`` sentinel.

    cvmfs-prepub direct path:
        Requires ``--prepub-url``.  Packages the relocated tree as a tar,
        POSTs it to the cvmfs-prepub REST API, and polls until the job
        reaches ``published``.

    View mode (``--release-view NAME``):
        Publishes the merged release view rather than a package; delegated to
        :func:`bits_helpers.view_publish_cmd.doPublishView`. Returns its bool.
    """
    if getattr(args, "publishView", None):
        from bits_helpers.view_publish_cmd import doPublishView
        return doPublishView(args, parser)

    # Bulk S3 upload of a whole build manifest. This is the DEFAULT when no
    # PACKAGE is given; --from-manifest [PATH] selects a specific manifest,
    # otherwise the newest under WORKDIR/MANIFESTS is used.
    _fm = getattr(args, "fromManifest", None)
    if _fm is None and not getattr(args, "package", None):
        _fm = "latest"
    if _fm is not None:
        architecture = getattr(args, "architecture", None) or detectArch()
        from bits_helpers.args import DEFAULT_S3_STORE
        store_url = getattr(args, "publishStore", None) or DEFAULT_S3_STORE
        _res = _publish_from_manifest(architecture, abspath(args.workDir), store_url, parser,
                                      manifest=_fm, dry_run=getattr(args, "dryRun", False))
        if _res:
            _build_id, _bom, _system = _res
            # Resolve certify knobs: CLI flag > env > defaults `system:`. Giving
            # --certify-group (or having both group+remote configured in defaults)
            # implies --certify; --no-certify always opts out.
            _group = getattr(args, "certifyGroup", None) or _system.get("certify_group")
            _remote = (getattr(args, "manifestsRemote", None)
                       or _system.get("manifests_remote"))
            _ref = getattr(args, "certifyRef", None) or _system.get("certify_ref")
            _want = (getattr(args, "certify", False)
                     or bool(getattr(args, "certifyGroup", None))
                     or bool(_group and _remote))
            if getattr(args, "noCertify", False):
                _want = False
            if _want:
                args.certifyGroup, args.manifestsRemote, args.certifyRef = _group, _remote, _ref
                _submit_certification_mr(args, parser, _build_id, _bom)
        return

    if not getattr(args, "package", None):
        parser.error("publish: PACKAGE is required (or use --release-view NAME to publish a release view).")

    architecture = getattr(args, "architecture", None) or detectArch()
    work_dir     = abspath(args.workDir)
    package      = args.package
    version      = getattr(args, "version", None)
    cvmfs_target = args.cvmfsTarget
    spool        = getattr(args, "spool", None)
    scratch_dir  = getattr(args, "scratchDir", None)
    rsync_opts   = getattr(args, "rsyncOpts", None)

    prepub_url          = getattr(args, "prepubUrl", None)
    prepub_token        = getattr(args, "prepubToken", None)
    prepub_repo         = getattr(args, "prepubRepo", None)
    prepub_path         = getattr(args, "prepubPath", None)
    prepub_webhook      = getattr(args, "prepubWebhook", None)
    prepub_poll_interval = getattr(args, "prepubPollInterval", 10)
    prepub_timeout      = getattr(args, "prepubTimeout", 1800)
    prepub_no_verify_tls = getattr(args, "prepubNoVerifyTls", False)
    prepub_bearer_auth   = getattr(args, "prepubBearerAuth", False)

    # ------------------------------------------------------------------
    # Validate: exactly one of --spool / --prepub-url must be provided.
    # ------------------------------------------------------------------
    # ── Resolve publish target(s) ─────────────────────────────────────────────
    # Backward-compatible default: 'cvmfs' when --cvmfs-target is given (the
    # existing pipeline call), otherwise 's3'. --to overrides.
    _to = getattr(args, "publishTo", None)
    if _to == "both":
        targets = {"s3", "cvmfs"}
    elif _to:
        targets = {_to}
    else:
        targets = {"cvmfs"} if cvmfs_target else {"s3"}

    if "s3" in targets:
        write_store = (getattr(args, "writeStore", "") or os.environ.get("BITS_WRITE_STORE")
                       or os.environ.get("WRITE_STORE") or "")
        if not write_store:
            parser.error("--to s3 requires a write store (--write-store, or WRITE_STORE / BITS_WRITE_STORE).")
        _publish_s3(package, version, architecture, work_dir, write_store, parser,
                    dry_run=getattr(args, "dryRun", False))
        if "cvmfs" not in targets:
            return

    # CVMFS publish needs a target path and exactly one sink (--spool | --prepub-url).
    if not cvmfs_target:
        parser.error("--to cvmfs requires --cvmfs-target.")
    if prepub_url and spool:
        parser.error("--prepub-url and --spool are mutually exclusive; use one or the other.")
    if not prepub_url and not spool:
        parser.error("one of --spool or --prepub-url is required.")

    # ------------------------------------------------------------------
    # 0. Redistribution policy gate
    # ------------------------------------------------------------------
    # A recipe may restrict redistribution (`redistributable: sources|none`,
    # hash-excluded metadata — e.g. QGRAF, the Oracle client): such a package
    # must never be laid into a public CVMFS tree — CVMFS is worldwide,
    # read-only, unauthenticated redistribution of binaries. This is the
    # enforcement point for the flag. Skipping (rather than failing) keeps
    # whole-stack publish loops running: the desired end state IS this package
    # being absent from CVMFS. Legacy boolean values parse as all/none.
    from bits_helpers.sync import redistributable_forms
    _entry = _load_manifest_spec(work_dir, package, version)
    if _entry is not None and "redistributable" in _entry and \
       "binaries" not in redistributable_forms(_entry.get("redistributable")):
        banner("NOT publishing %s to CVMFS: its recipe declares "
               "redistributable: %s (the licence forbids public binary "
               "redistribution). Users must obtain it upstream.",
               package, _entry.get("redistributable"))
        return

    # ------------------------------------------------------------------
    # 1. Locate immutable INSTALLROOT
    # ------------------------------------------------------------------
    banner(f"Publishing {package} to CVMFS")
    installroot = _find_installroot(work_dir, architecture, package, version)
    version_dir  = basename(installroot)
    pkg_id       = _pkg_id(package, version_dir, architecture)

    info("installroot : %s", installroot)
    info("pkg_id      : %s", pkg_id)
    info("cvmfs target: %s", cvmfs_target)
    if spool:
        info("spool       : %s", spool)
    else:
        info("prepub url  : %s", prepub_url)

    no_relocate = getattr(args, "noRelocate", False)
    relocate_script = join(installroot, "relocate-me.sh")
    if not no_relocate and not exists(relocate_script):
        error("relocate-me.sh not found in %s — was this package built with bits?", installroot)
        sys.exit(1)

    # ------------------------------------------------------------------
    # 2. Copy INSTALLROOT → working copy (INSTALLROOT is never touched)
    # ------------------------------------------------------------------
    if scratch_dir:
        os.makedirs(scratch_dir, exist_ok=True)
        copy_dir = join(scratch_dir, pkg_id)
        if exists(copy_dir):
            shutil.rmtree(copy_dir)
        os.makedirs(copy_dir)
    else:
        # Use a temp dir that auto-cleans on abnormal exit; we remove it
        # explicitly on success.
        _tmpparent = tempfile.mkdtemp(prefix="bits-cvmfs-")
        copy_dir   = join(_tmpparent, pkg_id)
        os.makedirs(copy_dir)

    info("working copy: %s", copy_dir)

    info("Copying installation tree …")
    rsync_copy = ["rsync", "-a", installroot + "/", copy_dir + "/"]
    subprocess.run(rsync_copy, check=True)

    if no_relocate:
        # ------------------------------------------------------------------
        # 3–5. Skip relocation: package was built with --cvmfs-prefix so
        #      all embedded paths are already correct for CVMFS.
        # ------------------------------------------------------------------
        info("--no-relocate: skipping relocation (package built at final CVMFS path)")
        if spool:
            info("Transferring tree to spool …")
            _rsync_to_spool(copy_dir + "/", spool, pkg_id,
                            extra_opts=rsync_opts, remove_source=False)
    else:
        # ------------------------------------------------------------------
        # 3. Relocate working copy to final CVMFS target path.
        #
        # For the spool path, start inotifywait before relocation so that
        # modified files are streamed to the spool concurrently.  For the
        # prepub path we skip inotify — the final tar is built after
        # relocation completes, so there is nothing to stream incrementally.
        # ------------------------------------------------------------------
        watcher = _stream_with_inotify(copy_dir, spool, pkg_id, rsync_opts) if spool else None

        # ------------------------------------------------------------------
        # 4. Relocate working copy to final CVMFS target path
        # ------------------------------------------------------------------
        info("Relocating to %s …", cvmfs_target)
        env = {**os.environ, "INSTALL_BASE": cvmfs_target}
        result = subprocess.run(
            ["bash", "-e", relocate_script],
            cwd=copy_dir,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            error("relocate-me.sh failed (exit %d)", result.returncode)
            if watcher:
                watcher.terminate()
            sys.exit(result.returncode)

        # ------------------------------------------------------------------
        # 5. Stop watcher (spool path) or skip (prepub path)
        # ------------------------------------------------------------------
        if spool:
            if watcher:
                import time
                # Give the drain thread a moment to flush the last events.
                time.sleep(1)
                watcher.terminate()
                watcher.wait()
            else:
                info("Transferring relocated tree to spool …")
                _rsync_to_spool(copy_dir + "/", spool, pkg_id,
                                extra_opts=rsync_opts, remove_source=False)

    # ------------------------------------------------------------------
    # 6a. Legacy spool path — write .done sentinel
    # ------------------------------------------------------------------
    if spool:
        info("Writing sentinel %s.done …", pkg_id)
        _write_sentinel(spool, pkg_id, cvmfs_target, rsync_opts=rsync_opts)

        # ------------------------------------------------------------------
        # 7a. Cleanup working copy (spool path)
        # ------------------------------------------------------------------
        info("Cleaning up working copy …")
        shutil.rmtree(copy_dir, ignore_errors=True)
        if not scratch_dir:
            shutil.rmtree(_tmpparent, ignore_errors=True)

        info("Done — package %s queued for ingestion.", pkg_id)
        return

    # ------------------------------------------------------------------
    # 6b. cvmfs-prepub direct path — each independent directory tree is
    #     tar'd and submitted to *its own* CVMFS path.  The package payload
    #     and the modulefiles live in different trees (install_dir vs
    #     module_dir), so a single tar would land the modulefiles inside the
    #     package tree; they are submitted as a separate job to --module-target.
    # ------------------------------------------------------------------
    from bits_helpers.prepub import resolve_token

    prepub_cfg = _PrepubConfig(
        url=prepub_url, token=resolve_token(prepub_token),
        webhook=prepub_webhook, no_verify_tls=prepub_no_verify_tls,
        bearer_auth=prepub_bearer_auth,
        poll_interval=prepub_poll_interval, timeout=prepub_timeout,
    )

    # Package payload tree.
    repo, subpath = _resolve_repo_subpath(prepub_repo, prepub_path, cvmfs_target, parser)
    debug("prepub repo=%s  path=%s", repo, subpath)

    # Modulefiles tree (independent of relocation): published to module_dir when
    # --module-target is given, so they reach the separate modules tree even when
    # the package itself is published with --no-relocate.
    module_target = getattr(args, "moduleTarget", None)
    module_dir = join(copy_dir, "etc", "modulefiles")
    publish_modules = bool(module_target) and os.path.isdir(module_dir) and os.listdir(module_dir)

    try:
        _publish_tree(copy_dir, repo, subpath, prepub_cfg, "package %s" % pkg_id)
        if publish_modules:
            mrepo, msub = _resolve_repo_subpath(None, None, module_target, parser)
            _publish_tree(module_dir, mrepo, msub, prepub_cfg,
                          "modulefiles for %s" % pkg_id)
        elif module_target:
            info("No modulefiles under %s; nothing to publish to module tree.", module_dir)
    finally:
        info("Cleaning up working copy …")
        shutil.rmtree(copy_dir, ignore_errors=True)
        if not scratch_dir:
            shutil.rmtree(_tmpparent, ignore_errors=True)
    return True


class _PrepubConfig:
    """The cvmfs-prepub connection settings shared by every per-tree submission."""

    def __init__(self, url, token, webhook, no_verify_tls, poll_interval, timeout,
                 bearer_auth=False):
        self.url = url
        self.token = token
        self.webhook = webhook
        self.no_verify_tls = no_verify_tls
        self.poll_interval = poll_interval
        self.timeout = timeout
        # False (the default) signs each request; True sends the legacy bearer
        # for a server still running auth_mode=bearer.
        self.bearer_auth = bearer_auth


def _resolve_repo_subpath(prepub_repo, prepub_path, cvmfs_target, parser):
    """Return ``(repo, subpath)`` for a CVMFS target, from explicit
    ``--prepub-repo/--prepub-path`` or derived from the target path."""
    from bits_helpers.prepub import _cvmfs_repo_and_path
    if prepub_repo and prepub_path:
        return prepub_repo, prepub_path.strip("/")
    if prepub_repo or prepub_path:
        parser.error("--prepub-repo and --prepub-path must both be supplied when "
                     "either is given; omit both to derive them from the target.")
    try:
        return _cvmfs_repo_and_path(cvmfs_target)
    except ValueError as exc:
        parser.error(str(exc))


def _publish_tree(tree_dir, repo, subpath, cfg, what):
    """Tar *tree_dir* and submit it to ``<repo>/<subpath>`` via cvmfs-prepub,
    polling to completion. This is the single per-tree primitive: the package
    payload, the modulefiles tree and a release view are each published with it,
    to their own independent CVMFS path."""
    from bits_helpers.prepub import poll_job, submit_job
    tar_fd, tar_path = tempfile.mkstemp(prefix="bits-prepub-", suffix=".tar.gz")
    os.close(tar_fd)
    try:
        info("Packaging %s as tar → %s/%s …", what, repo, subpath)
        with tarfile.open(tar_path, "w:gz") as tf:
            tf.add(tree_dir, arcname=".")
        job_id = submit_job(prepub_url=cfg.url, token=cfg.token, repo=repo,
                            path=subpath, tar_path=tar_path, webhook_url=cfg.webhook,
                            no_verify_tls=cfg.no_verify_tls,
                            bearer_auth=cfg.bearer_auth)
        poll_job(prepub_url=cfg.url, token=cfg.token, job_id=job_id,
                 poll_interval=cfg.poll_interval, timeout=cfg.timeout,
                 no_verify_tls=cfg.no_verify_tls,
                 bearer_auth=cfg.bearer_auth)
    finally:
        try:
            os.unlink(tar_path)
        except OSError:
            pass
