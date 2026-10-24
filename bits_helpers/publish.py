# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits publish — copy, relocate, and hand a built package to cvmfs-prepub.

Pipeline on the build host
---------------------------
1. Locate the package's immutable INSTALLROOT under *workDir*.
2. ``rsync`` it to a temporary CVMFS working copy (scratch directory).
3. Run ``relocate-me.sh`` inside the copy, rewriting all embedded paths to
   the final CVMFS target path.
4. Package the relocated tree as a tar and POST it to the cvmfs-prepub REST
   API (``--prepub-url``), which ingests it into CVMFS; poll until published.
5. Remove the working copy from the scratch directory.

The original INSTALLROOT under *workDir* is never modified.
"""

import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from os.path import abspath, basename, exists, join

from bits_helpers.log import debug, error, info, warning, banner
from bits_helpers.arch import detectArch

_NO_STORE = ("no store: give --remote-store, set $BITS_S3_STORE, or store.operations "
             "in a site configuration file")


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
    single path segment — it can never traverse out of a directory when used as
    a path component (e.g. the prepub tar's per-package subpath).
    """
    # Replace '/' in package just as we do for architecture and version_dir.
    # Without this, a package name like '../../etc' would produce a pkg_id that
    # traverses out of its directory when used as a path component.
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
    from bits_helpers.sync import remote_from_url, binary_redistributable
    e = _load_manifest_spec(work_dir, package, version)
    if not e or not e.get("hash"):
        parser.error("no built manifest entry for %s%s in %s — build it first"
                     % (package, (" " + version) if version else "", work_dir))
    # redistributable: sources|none (QGRAF, CPC, vendor EULAs …): the binary must
    # not land in a potentially world-readable store — uploading IS redistribution.
    # Skip it, matching the build-time and bulk-publish upload gates (fail closed).
    if not binary_redistributable(e):
        banner("NOT uploading %s to the S3 store: its recipe declares "
               "redistributable: %s (the licence forbids public binary "
               "redistribution). Build it locally where it is needed.",
               package, e.get("redistributable"))
        return
    spec = {"package": e["package"], "version": e.get("version"),
            "revision": e.get("revision"), "hash": e["hash"]}
    arch = e.get("effective_architecture") or architecture
    write_store = _normalize_s3_store(write_store)   # https://host/bucket -> b3://bucket
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
    if "," in (url or ""):
        error("this command works on one store, not a list: %s", url)
        sys.exit(1)
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


# The build-manifest fields a BOM entry carries (plus tarball and its sha256).
_BOM_FIELDS = ("package", "version", "revision", "effective_architecture",
               "hash", "commit_hash", "pkg_family", "built_by", "completed_at",
               "license", "redistributable", "view", "requires")

# How many packages are checked/uploaded at once (BITS_PUBLISH_JOBS). Each is a
# few S3 round trips (rev-index marker, HEAD of the content object), so a
# serial pass over a whole stack spends minutes waiting on the network.
_PUBLISH_JOBS = 8


def _publish_jobs():
    try:
        return max(1, min(64, int(os.environ.get("BITS_PUBLISH_JOBS") or _PUBLISH_JOBS)))
    except ValueError:
        return _PUBLISH_JOBS


def _bom_documents(published, manifest_doc, architecture, build_id):
    """``(packages, [(effective_arch, BOM)])`` for the *published* ``(entry,
    store tarball)`` pairs: the minimal BOM entries, and one BOM per architecture."""
    import getpass, socket, datetime
    from bits_helpers.checksum import checksum_file
    # completed_at is kept per package so every hash carries "when built".
    # license/redistributable are the compliance metadata: the BOM (and
    # the signed manifest merged from it) must know what may be laid
    # into a public CVMFS tree and what attribution a NOTICE file needs.
    # view/requires: what a release made later from this BOM alone (bits
    # cvmfs release) needs: the view rules and the dependency order.
    packages = []
    for e, tar in published:
        m = {k: e[k] for k in _BOM_FIELDS if k in e}
        m["tarball"] = e.get("tarball") or basename(tar)
        m["tarball_sha256"] = e.get("tarball_sha256") or checksum_file(tar)
        packages.append(m)
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
        by_arch.setdefault(m.get("effective_architecture") or "shared", []).append(m)
    provenance = {
        "build_id": build_id,
        "published_by": "%s@%s" % (_user, socket.gethostname().split(".")[0]),
        "published_at": datetime.datetime.now(datetime.timezone.utc)
                            .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "architecture": architecture,
    }
    # The CI pipeline that produced this build — the id the console's passkey
    # pre-approval is keyed on. The certify job reads it from this BOM to
    # consume that pre-approval when it signs. Absent for a local publish.
    # FUTURE(build_id-unification 2026-09-20): source_pipeline_id is CI-only
    # (empty off-CI), so CLI/external BOMs are not pre-approvable by it. Unify
    # the pre-approval anchor on the deterministic manifest build_id. KEEP this
    # until that lands. Doc: signing-authz-unified-model-2026-09-20.
    pipeline_id = os.environ.get("CI_PIPELINE_ID", "").strip()
    if pipeline_id:
        provenance["source_pipeline_id"] = pipeline_id
    # The build's CVMFS layout (release name, package paths) and what it
    # was asked to build: a release is made from these later, from the
    # BOM alone, without the build's work directory.
    for fld in ("cvmfs_templates", "requested_packages"):
        if manifest_doc.get(fld):
            provenance[fld] = manifest_doc[fld]
    return packages, [(arch, dict(provenance, effective_architecture=arch, packages=by_arch[arch]))
                      for arch in sorted(by_arch)]


# Who and when: the only BOM fields a reused BOM may differ in.
_BOM_RUN_FIELDS = ("published_by", "published_at")


def _published_bom(writers, build_id, published, manifest_doc, architecture):
    """The BOMs this CI pipeline already published for this build, as
    ``[(arch, bom_dict)]``, or None to publish afresh.

    The publish job's BOM step and `bits certify` in the same job publish the
    same manifest; the second finds the first's BOMs through the pipeline's build
    index (BUILDS/by-pipeline/<pipeline>/<build_id>.json) instead of checking every
    package and uploading the BOMs again. Only the store's sha256 of each tarball
    is taken from them: the documents are made again from the local manifest
    exactly as a publish makes them, and must equal the stored ones but for who
    published them and when. Anything else in the bucket is never certified.
    """
    import json as _json
    pid = os.environ.get("CI_PIPELINE_ID", "").strip()
    w = next(iter(writers.values()), None)
    if not (pid and build_id and w is not None and getattr(w, "s3", None)
            and getattr(w, "writeStore", None)):
        return None

    def _get(key):
        return _json.loads(w.s3.get_object(Bucket=w.writeStore, Key=key)["Body"].read())
    try:
        index = _get("BUILDS/by-pipeline/%s/%s.json" % (pid, build_id))
        keys = index.get("keys") or []
        if index.get("build_id") != build_id or not keys:
            return None
        stored = []
        for key in keys:
            if not str(key).startswith("MANIFESTS/%s/" % build_id):
                return None
            stored.append(_get(key))
        sha = {}
        for doc in stored:
            for p in doc.get("packages") or []:
                if not (isinstance(p, dict) and p.get("hash") and p.get("tarball_sha256")):
                    return None
                sha.setdefault(p["hash"], p["tarball_sha256"])
        if set(sha) != {e.get("hash") for e, _tar in published}:
            raise ValueError("other packages")
        _pkgs, mine = _bom_documents([(dict(e, tarball_sha256=sha[e["hash"]]), tar)
                                      for e, tar in published],
                                     manifest_doc, architecture, build_id)

        def _same(a, b):
            return ({k: v for k, v in a.items() if k not in _BOM_RUN_FIELDS}
                    == {k: v for k, v in b.items() if k not in _BOM_RUN_FIELDS})
        if len(stored) != len(mine) or not all(
                isinstance(d, dict) and _same(d, m) for d, (_a, m) in zip(stored, mine)):
            raise ValueError("not this publish's")
    except Exception as exc:  # pylint: disable=broad-except
        debug("pipeline %s: no BOM to reuse for %s (%s); publishing afresh", pid, build_id, exc)
        return None
    return [(a, d) for (a, _m), d in zip(mine, stored)]


def _publish_from_manifest(architecture, work_dir, store_url, parser, manifest=None, dry_run=False,
                           reuse_pipeline_bom=False):
    """Bulk-upload every built package in a manifest to the S3 store.

    *manifest* is a manifest file path, or 'latest'/None to use the newest under
    WORKDIR/MANIFESTS. With *dry_run*, list what would be uploaded and touch no
    network (no credentials needed). With *reuse_pipeline_bom* (`bits certify`),
    the BOMs this CI pipeline's publish step already wrote for the same packages
    are returned instead of publishing again.
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
    # The build's own architecture, not the publishing host's: it is what the
    # BOMs record and the fallback for entries without an effective one.
    architecture = manifest_doc.get("architecture") or architecture
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
        produced by a single, consistent build, and `bits sign` rejects it
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
    jobs = []           # (n, entry, arch, tarball, spec) to check/upload
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
        if arch not in writers:
            writers[arch] = remote_from_url(write_store, write_store, arch, work_dir)
        jobs.append((n, e, arch, tar, {"package": pkg, "version": e.get("version"),
                                       "revision": e.get("revision"), "hash": h}))

    if jobs and reuse_pipeline_bom:
        from bits_helpers.provenance import build_id_from_manifest
        _bid = build_id_from_manifest(manifest_doc)
        _reused = _published_bom(writers, _bid, [(j[1], j[3]) for j in jobs],
                                 manifest_doc, architecture)
        if _reused is not None:
            banner("Using the BOM this pipeline already published for %s (%d package(s))",
                   _bid, len(jobs))
            return _bid, _reused, _system_from_manifest(manifest_doc)

    def _upload(job):
        _n, _e, _arch, _tar, _spec = job
        writers[_arch].upload_symlinks_and_tarball(_spec)

    # Independent per-package store checks/uploads, a few at a time; the BOM is
    # still assembled in manifest order, so it does not depend on which finished
    # first.
    from concurrent.futures import ThreadPoolExecutor, as_completed
    done_ok = {}
    if jobs:
        pool = ThreadPoolExecutor(max_workers=min(_publish_jobs(), len(jobs)))
        futures = {}
        try:
            futures.update({pool.submit(_upload, j): j for j in jobs})
            for fut in as_completed(futures):
                _n, _e, _arch, _tar, _spec = futures[fut]
                try:
                    fut.result()
                except Exception as exc:  # pylint: disable=broad-except
                    error("  FAILED %s (%s): %s", _spec["package"], _spec["hash"][:12], exc)
                    continue
                # The upload path records the sha256 of the object actually in
                # the store (kept-as-found or freshly uploaded). It overrides
                # whatever the build manifest recorded: the store object is
                # authoritative, and the build's locally-packed bytes may
                # legitimately differ (.tar.gz is not byte-reproducible). The BOM
                # below must describe the stored bytes or `bits sign` rejects it.
                if _spec.get("store_tarball_sha256"):
                    _e = dict(_e, tarball_sha256=_spec["store_tarball_sha256"])
                done_ok[_n] = (_e, _tar)
                info("  [%d/%d] %s %s", len(done_ok), len(jobs), _spec["package"],
                     _spec["hash"][:12])
        except BaseException:
            # Ctrl-C, or a writer giving up (SystemExit): stop at once, not after
            # the rest of the queue.
            for f in futures:
                f.cancel()                    # (cancel_futures needs Python 3.9)
            pool.shutdown(wait=False)
            raise
        pool.shutdown(wait=True)
    ok += len(done_ok)
    published.extend(done_ok[k] for k in sorted(done_ok))

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
            from bits_helpers.provenance import build_id_from_manifest
            import tempfile as _tf
            # Canonical, deterministic build_id (same id the build itself used):
            # <label>-<digest> over the full package set. Two hosts building the
            # same release agree on it, so it names the release in the bucket.
            build_id = build_id_from_manifest(manifest_doc) or "unknown"
            leaf = _run_leaf()
            packages, docs = _bom_documents(published, manifest_doc, architecture, build_id)
            _pipeline_id = os.environ.get("CI_PIPELINE_ID", "").strip()
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
                for _arch, doc in docs:
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
                             len(doc["packages"]), _arch, build_id, w.writeStore, key)
                        bom.append((_arch, doc))
                    except Exception as exc:
                        error("manifest upload failed for %s: %s", _arch, exc)
                        _bom_failed = _arch
                        break
            finally:
                import shutil as _shutil
                _shutil.rmtree(_bomdir, ignore_errors=True)
            # Found again by the pipeline that made it: bits cvmfs release --pipeline.
            # One file per build (a pipeline may build several platforms), outside
            # MANIFESTS/ so the tools that read every BOM there do not see it.
            if _bom_failed is None and _pipeline_id:
                _index = "BUILDS/by-pipeline/%s/%s.json" % (_pipeline_id, build_id)
                try:
                    w.s3.put_object(Bucket=w.writeStore, Key=_index, Body=_json.dumps({
                        "build_id": build_id,
                        "keys": ["MANIFESTS/%s/%s.%s.json" % (build_id, _stem, _a)
                                 for _a, _d in bom]}, indent=1).encode())
                except Exception as exc:
                    warning("could not record %s (bits cvmfs release --pipeline "
                            "will not find this build): %s", _index, exc)
            if _bom_failed is not None:
                error("BOM upload failed for architecture %s — aborting the "
                      "publish (no certification MR will be opened; already-"
                      "uploaded BOMs of this run remain and are harmless: "
                      "certification is scoped per architecture and a re-run "
                      "supersedes them)", _bom_failed)
                sys.exit(1)
            # NOTICE + LICENSE-SOURCE-OFFER.txt (+ SBOMs) next to the release's BOMs:
            # attribution and the GPL source offer are discharged mechanically
            # from the FULL manifest entries (which carry license,
            # redistributable and the source archives' store paths).
            # Best-effort — a compliance-file failure never fails a publish.
            # The SBOMs describe the stored objects: their sha256 is the one the
            # BOM (and so the signed manifest) records, not the local build's.
            # They list what was published plus, marked, what the licence keeps
            # out; a package skipped for having no tarball is not in the release.
            from bits_helpers.sync import redistributable_forms as _forms
            _stored = {m["hash"]: m.get("tarball_sha256") for m in packages if m.get("hash")}
            _sbom_manifest = dict(manifest_doc, build_id=build_id, packages=[
                dict(x, tarball_sha256=_stored[x["hash"]]) if x.get("hash") in _stored else x
                for x in entries if isinstance(x, dict) and (
                    x.get("hash") in _stored
                    or "binaries" not in _forms(x.get("redistributable", "all")))])
            from bits_helpers.notice import upload_release_compliance
            upload_release_compliance(w.s3, w.writeStore, build_id, entries,
                                      store_url=store_url, manifest=_sbom_manifest)

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
        from bits_helpers.defaults import readDefaults
        meta, _ = readDefaults(cfg, defs, lambda _m: None, None)
        sysd = meta.get("system")
        return sysd if isinstance(sysd, dict) else {}
    except Exception:
        return {}


def _preapprove_build(args, parser, build_id, bom, system, for_mr=True):
    """Passkey pre-approval of this build via bits-console, before the MR (or,
    without *for_mr*, as all that certifies it: see orchestrated.py).
    *bom* is the list of (arch, bom_dict) just published; the approval binds their
    packages. Exits (no MR) if it is refused or times out."""
    from bits_helpers import forge, orchestrated
    from bits_helpers.sign_console import preapprove_via_console
    # Check everything the MR needs first: don't ask a human to approve a build
    # whose MR then cannot be opened.
    if not args.certifyGroup:
        parser.error("certify: needs a group (--group or `system: certify_group:`) before "
                     "asking for an approval")
    if for_mr and not (getattr(args, "manifestsRemote", None)
                       and forge.resolve_gitlab_token(getattr(args, "gitlabToken", None))):
        parser.error("certify: needs a group (--group), the manifests repo (--manifests-remote) "
                     "and a GitLab token (~/.bits/gitlab-token) before asking for an approval")
    console = orchestrated.console_url(getattr(args, "console", None), system)
    if not console:
        parser.error("certify: the passkey approval needs the bits-console URL — --console, "
                     "$BITS_CONSOLE_URL or `system: console_url:` (or --approval none)")
    preapprove_via_console(console, build_id, [args.certifyGroup], [d for _a, d in bom],
                           cafile=getattr(args, "consoleCafile", None),
                           insecure=getattr(args, "consoleInsecure", False))


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
        parser.error("certify: no group — pass --group <group> or set `system: certify_group:`")
    remote = getattr(args, "manifestsRemote", None)
    if not remote:
        parser.error("certify: no manifests repo — pass --manifests-remote <git URL> or set "
                     "`system: manifests_remote:`")
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








def _orchestrator_marker(store_url, work_dir, architecture):
    """The store's orchestrator marker (see orchestrated.py), or None. A store that
    cannot say is taken as unmarked: the MR is opened as before, and refused by the
    manifests CI if the orchestrator does own the set."""
    from bits_helpers import orchestrated
    try:
        return orchestrated.for_store(store_url, work_dir, architecture)
    except orchestrated.Unknown as exc:
        warning("certify: %s; certifying through a merge request", exc)
        return None


def doCertify(args, parser):
    """`bits certify`: make a build trusted — upload what is missing, get a passkey
    approval via bits-console (unless --approval none), open the certification MR.
    The manifests-repo CI then signs it (`bits sign`). In a store whose certified
    set bits-orchestrator owns, the approval is the end: it certifies the build."""
    from bits_helpers import forge, orchestrated
    from bits_helpers.args import DEFAULT_S3_STORE
    dry = getattr(args, "dryRun", False)
    passkey = getattr(args, "approval", "passkey") == "passkey"
    architecture = getattr(args, "architecture", None) or detectArch()
    store_url = getattr(args, "publishStore", None) or DEFAULT_S3_STORE
    if not store_url:
        parser.error(_NO_STORE)
    marker = None if dry else _orchestrator_marker(store_url, abspath(args.workDir), architecture)
    if not passkey and not marker and not getattr(args, "certifier", None):
        # Without an approval the MR merges but can never be signed; that is only
        # right for a build approved elsewhere, whose MR a bot opens for a human.
        parser.error("certify: --approval none is for builds already approved elsewhere "
                     "(bits-console) and needs --certifier <user>")
    # Fail before uploading when no MR could be opened anyway.
    if not dry and not marker and not forge.resolve_gitlab_token(getattr(args, "gitlabToken", None)):
        parser.error("certify: no GitLab token for the MR — ~/.bits/gitlab-token, "
                     "$BITS_CERTIFIER_TOKEN or --gitlab-token")
    res = _publish_from_manifest(architecture, abspath(args.workDir), store_url, parser,
                                 manifest=getattr(args, "fromManifest", None) or "latest",
                                 dry_run=dry, reuse_pipeline_bom=True)
    if dry:
        info("[dry-run] would then %sopen the certification MR (no MR when the store "
             "says bits-orchestrator certifies)",
             "ask bits-console for a passkey approval and " if passkey else "")
        return
    if not res:
        parser.error("certify: no build manifest (BOM) was published, so there is nothing to certify")
    build_id, bom, system = res
    system = system or {}
    # CLI > defaults `system:` for the certification target.
    args.certifyGroup = getattr(args, "certifyGroup", None) or system.get("certify_group")
    args.manifestsRemote = getattr(args, "manifestsRemote", None) or system.get("manifests_remote")
    args.certifyRef = getattr(args, "certifyRef", None) or system.get("certify_ref")
    if marker:
        if passkey:
            _preapprove_build(args, parser, build_id, bom, system, for_mr=False)
            banner("Build %s is pre-approved: %s certifies it into %s (no merge request)",
                   build_id, orchestrated.describe(marker), marker["manifests"])
        else:
            banner("Build %s is published: %s certifies it once it is pre-approved "
                   "(no merge request)", build_id, orchestrated.describe(marker))
        return
    if passkey:
        _preapprove_build(args, parser, build_id, bom, system)
    _submit_certification_mr(args, parser, build_id, bom)


# ---------------------------------------------------------------------------
# Main publish entry point
# ---------------------------------------------------------------------------

def doPublish(args, parser):
    """Orchestrate the build-host publishing pipeline.

    cvmfs-prepub path (``--prepub-url``, required for CVMFS publish):
        Relocate the package, package the tree as a tar, POST it to the
        cvmfs-prepub REST API, and poll until the job reaches ``published``.

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
        if not store_url:
            parser.error(_NO_STORE)
        _publish_from_manifest(architecture, abspath(args.workDir), store_url, parser,
                               manifest=_fm, dry_run=getattr(args, "dryRun", False))
        return

    if not getattr(args, "package", None):
        parser.error("publish: PACKAGE is required (or use --release-view NAME to publish a release view).")

    architecture = getattr(args, "architecture", None) or detectArch()
    work_dir     = abspath(args.workDir)
    package      = args.package
    version      = getattr(args, "version", None)
    cvmfs_target = args.cvmfsTarget
    scratch_dir  = getattr(args, "scratchDir", None)

    prepub_url          = getattr(args, "prepubUrl", None)
    prepub_token        = getattr(args, "prepubToken", None)
    prepub_repo         = getattr(args, "prepubRepo", None)
    prepub_path         = getattr(args, "prepubPath", None)
    prepub_webhook      = getattr(args, "prepubWebhook", None)
    prepub_poll_interval = getattr(args, "prepubPollInterval", 10)
    prepub_timeout      = getattr(args, "prepubTimeout", 1800)
    prepub_no_verify_tls = getattr(args, "prepubNoVerifyTls", False)
    prepub_bearer_auth   = getattr(args, "prepubBearerAuth", False)

    # Single-package publish is CVMFS-only via the cvmfs-prepub service. The
    # S3-store write is a separate command now: `bits store upload PACKAGE`.
    if not cvmfs_target:
        parser.error("publish PACKAGE publishes to CVMFS and requires --cvmfs-target; "
                     "to upload a package to the S3 store use `bits store upload`.")
    if not prepub_url:
        parser.error("publishing to CVMFS requires --prepub-url.")

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
        # 3. Skip relocation: package was built with --cvmfs-prefix so all
        #    embedded paths are already correct for CVMFS.
        # ------------------------------------------------------------------
        info("--no-relocate: skipping relocation (package built at final CVMFS path)")
    else:
        # ------------------------------------------------------------------
        # 3. Relocate working copy to final CVMFS target path.
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
            sys.exit(result.returncode)

    # ------------------------------------------------------------------
    # 4. cvmfs-prepub path — each independent directory tree is
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
