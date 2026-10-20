# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits cvmfs release: make a release (its links and merged view) from one
build, at build time or any time later, without the build's work directory.

The build is named by the CI pipeline that made it (--pipeline), by its build
id (--build-id), or by both when a pipeline built several platforms. Its BOMs in the S3 store (one per architecture, plus
"shared" for noarch packages) say what it built, with its CVMFS templates.
Missing packages are fetched from the store by hash (their sha256 checked
against the BOM, and against the signed manifests with --trust-manifest) and
published with `bits cvmfs publish`, which then makes the release links and
the view and waits for every job.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile

from bits_helpers.log import error, info, warning


class ReleaseError(Exception):
    pass


def get_json(s3, bucket, key):
    """The JSON object stored at *key* (boto3 client *s3*)."""
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return json.loads(body)


def list_keys(s3, bucket, prefix):
    """All keys under *prefix*, across listing pages."""
    out, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        out += [o["Key"] for o in page.get("Contents", [])]
        if not page.get("IsTruncated"):
            return out
        token = page.get("NextContinuationToken")


def is_bom(doc):
    """Whether *doc* has the shape of a build manifest (BOM)."""
    return (isinstance(doc, dict) and doc.get("build_id") and doc.get("effective_architecture")
            and isinstance(doc.get("packages"), list)
            and all(isinstance(p, dict) and p.get("package") for p in doc["packages"]))


def find_boms(s3, bucket, pipeline=None, build_id=None):
    """The BOMs of one build, newest per effective architecture: by the
    pipeline that made it (its index under BUILDS/by-pipeline/), by its build
    id (MANIFESTS/<build_id>/), or by both (that build of that pipeline)."""
    keys, indexed = None, False
    if pipeline:
        builds = {}
        for key in list_keys(s3, bucket, "BUILDS/by-pipeline/%s/" % pipeline):
            try:
                index = get_json(s3, bucket, key)
            except Exception as exc:
                raise ReleaseError("unreadable %s: %s" % (key, exc))
            builds[index.get("build_id", "")] = index.get("keys") or []
        if build_id:
            builds = {b: k for b, k in builds.items() if b == build_id}
        if len(builds) > 1:
            raise ReleaseError("pipeline %s published several builds (%s): name one "
                               "with --build-id" % (pipeline, ", ".join(sorted(builds))))
        if builds:
            keys, indexed = next(iter(builds.values())), True
        elif not build_id:
            raise ReleaseError("no BOM index for pipeline %s: a build published before "
                               "bits recorded one is named with --build-id" % pipeline)
    if keys is None:
        if not build_id:
            raise ReleaseError("name the build with --pipeline or --build-id")
        keys = [k for k in list_keys(s3, bucket, "MANIFESTS/%s/" % build_id)
                if k.endswith(".json")]
    newest = {}
    for key in keys:
        try:
            bom = get_json(s3, bucket, key)
        except Exception as exc:
            if indexed:   # a BOM of this build: without it the release is incomplete
                raise ReleaseError("cannot read %s: %s" % (key, exc))
            warning("skipping unreadable %s: %s", key, exc)
            continue
        if not is_bom(bom):
            continue   # NOTICE, SBOMs and other files beside the BOMs
        if pipeline and str(bom.get("source_pipeline_id", "")) != str(pipeline):
            continue
        arch = bom["effective_architecture"]
        if arch not in newest or bom.get("published_at", "") > newest[arch].get("published_at", ""):
            newest[arch] = bom
    if not newest:
        raise ReleaseError("no BOM found for this build")
    return [newest[a] for a in sorted(newest)]


def merge_boms(boms, arch=None):
    """One build manifest for `bits cvmfs publish` from a build's BOMs (one per
    effective architecture: the build's, its own_hash toolchain's, noarch),
    in dependency order, with the build's templates. The build's architecture
    is the one its BOMs record, else its top-level package's, or for a noarch
    top level the one most of its packages have. Returns (manifest, architecture)."""
    from bits_helpers.arch import SHARED_ARCH
    from bits_helpers.cvmfs_publish import dependency_order
    if not boms:
        raise ReleaseError("no BOM found for this build")
    templates = [b.get("cvmfs_templates") for b in boms if b.get("cvmfs_templates")]
    if not templates:
        raise ReleaseError("the BOMs carry no cvmfs_templates: the build was published "
                           "before bits recorded them; publish it again with this bits")
    if any(t != templates[0] for t in templates):
        raise ReleaseError("the BOMs of this build disagree on its CVMFS templates")
    requested = next((b["requested_packages"] for b in boms if b.get("requested_packages")), [])
    packages = []
    for b in boms:
        for p in b["packages"]:
            packages.append(dict(p, effective_architecture=p.get("effective_architecture")
                                 or b["effective_architecture"]))
    top = [p["effective_architecture"] for p in packages if p["package"] in requested]
    arch = (arch or next((b["architecture"] for b in boms if b.get("architecture")), None)
            or (top[0] if top and top[0] != SHARED_ARCH else None))
    if not arch:
        counts = {}
        for p in packages:
            if p["effective_architecture"] != SHARED_ARCH:
                counts[p["effective_architecture"]] = counts.get(p["effective_architecture"], 0) + 1
        if not counts:
            raise ReleaseError("cannot tell the build's architecture from its BOMs")
        arch = max(sorted(counts), key=lambda k: counts[k])
    return ({"packages": dependency_order(packages),
             "cvmfs_templates": templates[0],
             "requested_packages": requested,
             "created_at": max(b.get("published_at", "") for b in boms),
             "build_id": boms[0].get("build_id", ""),
             "source_pipeline_id": next((str(b["source_pipeline_id"]) for b in boms
                                         if b.get("source_pipeline_id")), "")}, arch)


def check_signed(packages, index):
    """Refuse a package the signed manifests do not attest with this sha256
    (*index*: {(effective_architecture, hash): sha256})."""
    from bits_helpers.certify import norm_sha
    def certified(p):
        return index.get((p.get("effective_architecture"), p.get("hash")))
    bad = [p["package"] for p in packages
           if not certified(p)
           or norm_sha(certified(p)) != norm_sha(p.get("tarball_sha256", ""))]
    if bad:
        raise ReleaseError("not in the signed manifests (or another sha256): %s"
                           % ", ".join(bad))


def fetch_missing(reader, packages, tars_root, published):
    """Download from the store the tarballs of the packages *published* says are
    not on CVMFS yet (exactly the file the BOM names), check each against the
    BOM's sha256, and place it where `bits cvmfs publish` looks for it. Returns
    how many were fetched."""
    from bits_helpers.checksum import checksum_file
    from bits_helpers.certify import norm_sha
    from bits_helpers.cvmfs_publish import tar_path
    from bits_helpers.utilities import resolve_store_path
    store = os.path.join(os.path.dirname(tars_root.rstrip("/")), "store")
    n = 0
    for p in packages:
        if published(p):
            continue
        arch, name = p["effective_architecture"], p.get("tarball")
        if not name:
            raise ReleaseError("%s@%s: the BOM names no tarball" % (p["package"], p["hash"]))
        key = "%s/%s" % (resolve_store_path(arch, p["hash"]), name)
        got = os.path.join(store, key)
        os.makedirs(os.path.dirname(got), exist_ok=True)
        try:
            reader.s3.download_file(Bucket=reader.remoteStore, Key=key, Filename=got)
        except Exception as exc:
            raise ReleaseError("%s@%s: cannot fetch %s from the store: %s"
                               % (p["package"], p["hash"], key, exc))
        if norm_sha(checksum_file(got)) != norm_sha(p.get("tarball_sha256", "")):
            raise ReleaseError("%s@%s: the store's tarball does not match the BOM's sha256"
                               % (p["package"], p["hash"]))
        dest = tar_path(p, tars_root, arch)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not os.path.lexists(dest):
            os.symlink(got, dest)
        n += 1
    return n


def _published_check(ctx):
    """A predicate: is this package on CVMFS already, under this hash?"""
    from bits_helpers.cvmfs_publish import package_path, published_state

    def published(p):
        st = published_state(ctx, package_path(p, ctx))
        return bool(st and st.get("exists") and st.get("hash") == p.get("hash"))
    return published


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="bits cvmfs release",
        description="Make a release (its links and merged view) from one build, "
                    "fetching its packages from the store. Other options are "
                    "passed to `bits cvmfs publish`.")
    ap.add_argument("--pipeline", help="the CI pipeline that made the build")
    ap.add_argument("--build-id", dest="bom_build_id",
                    help="the build id (MANIFESTS/<build-id>/ in the store); with "
                         "--pipeline, that pipeline's build of that id")
    # The layout options are publish's too: the check for what is already on
    # CVMFS must place packages exactly as publish will.
    for opt, env in (("--tmpl-prefix", None), ("--prefix-fallback", "CVMFS_PREFIX_FALLBACK"),
                     ("--platform", "PLATFORM"), ("--install-dir", "CVMFS_INSTALL_DIR"),
                     ("--user", None)):
        ap.add_argument(opt, default=os.environ.get(env, "") if env else "")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--arch", default="",
                    help="the build's architecture, when its top-level package is "
                         "noarch and its BOMs leave it ambiguous")
    ap.add_argument("--remote-store", default=os.environ.get("BITS_REMOTE_STORE", ""),
                    help="the S3 store holding the build's BOMs and tarballs (b3://)")
    ap.add_argument("--trust-manifest", default=os.environ.get("BITS_TRUST_MANIFEST", ""),
                    help="signed manifests (comma-separated); every package must be "
                         "attested there. Without it certification is not checked.")
    ap.add_argument("--trust-groups", default="")
    ap.add_argument("--prepub-url", default=os.environ.get("PREPUB_URL", ""))
    ap.add_argument("--token", default=os.environ.get("PREPUB_API_TOKEN", ""))
    ap.add_argument("--bearer-auth", action="store_true")
    ap.add_argument("--work-dir", default="",
                    help="where to put fetched tarballs (default: a temporary directory)")
    a, rest = ap.parse_known_args(argv)
    if not (a.pipeline or a.bom_build_id):
        ap.error("name the build with --pipeline and/or --build-id")

    from bits_helpers.sync import remote_from_url
    work = a.work_dir or tempfile.mkdtemp(prefix="bits-release-")
    try:
        reader = remote_from_url(a.remote_store, "", "", work)
        if not hasattr(reader, "s3"):
            raise ReleaseError("--remote-store must be an S3 store (b3://): %r"
                               % a.remote_store)
        boms = find_boms(reader.s3, reader.remoteStore, pipeline=a.pipeline,
                         build_id=a.bom_build_id)
        manifest, arch = merge_boms(boms, a.arch or None)
        packages = manifest["packages"]
        info("Release from build %s (%s): %d packages, top level %s",
             manifest["build_id"], arch, len(packages),
             ", ".join(manifest["requested_packages"]) or "?")
        if a.trust_manifest:
            from bits_helpers.build import trusted_reuse_index
            ns = argparse.Namespace(trustManifest=a.trust_manifest,
                                    trustGroups=a.trust_groups or None)
            check_signed(packages, trusted_reuse_index(ns, work))
        else:
            warning("no --trust-manifest: the build's certification is not checked")
        tars_root = os.path.join(work, "TARS")
        ctx = {"repo": a.repo, "arch": arch, "tmpl_prefix": a.tmpl_prefix,
               "prefix_fallback": a.prefix_fallback or None,
               "templates": manifest["cvmfs_templates"], "platform": a.platform,
               "install_dir": a.install_dir, "user": a.user, "prepub_url": a.prepub_url,
               "token": a.token, "bearer_auth": a.bearer_auth}
        fetched = fetch_missing(reader, packages, tars_root, _published_check(ctx))
        info("%d package(s) fetched from the store, %d already on CVMFS",
             fetched, len(packages) - fetched)
        path = os.path.join(work, "release-manifest.json")
        with open(path, "w") as fh:
            json.dump(manifest, fh, indent=1)
        from bits_helpers.cvmfs_publish import main as publish_main
        argv2 = ["--manifest", path, "--repo", a.repo, "--tars-root", tars_root,
                 "--arch", arch, "--release-view", "--wait",
                 "--prepub-url", a.prepub_url, "--token", a.token,
                 "--tmpl-prefix", a.tmpl_prefix, "--prefix-fallback", a.prefix_fallback,
                 "--platform", a.platform, "--install-dir", a.install_dir, "--user", a.user,
                 "--build-id", str(a.pipeline or manifest["source_pipeline_id"]
                                   or manifest["build_id"])]
        if a.bearer_auth:
            argv2.append("--bearer-auth")
        return publish_main(argv2 + rest)
    except ReleaseError as exc:
        error("bits cvmfs release: %s", exc)
        return 1
    finally:
        if not a.work_dir:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
