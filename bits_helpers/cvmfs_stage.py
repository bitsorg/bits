#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Producer-side CVMFS staging: prepare a package, name the catalog to graft.

A build node runs the canonical `cvmfs_swissknife ingest` with an S3 spooler and
NO gateway, writing content-addressed objects into a staging prefix. prepub then
promotes those objects and asks the gateway to graft one catalog. This module
produces the two values prepub needs: the staging prefix, and the hash of the
catalog covering the published path.

Why the second value needs code rather than a `sed` on the manifest
-------------------------------------------------------------------
The manifest the prepare writes names the **root** catalog it computed against
its base revision. The graftable **subtree** catalog for the lease path is a
different object (MEASUREMENTS §21, verified on the testbed).

Sending the root hash does not fail. It is a syntactically valid catalog hash,
and after promotion it really is in the store, so every check prepub makes on
`catalog_hash` passes — and the gateway is then asked to graft a whole revision
as a subtree. The wrong thing gets published, quietly. So the subtree catalog is
found by walking, and the walk is the part worth testing.

The walk must consult the repository as well as the staging prefix
------------------------------------------------------------------
The prepare rewrites only the catalogs it changed. An unchanged nested catalog
is still *referenced* by the tree but is not re-staged: in §21, `/golden/smoke`
returned 404 from the staging prefix and 200 from the repository. A walker that
assumes the staging prefix is self-contained works on a toy repository and dies
on a real one.
"""

import os
import re
import sqlite3
import tempfile
import zlib

try:  # reuse the existing primitives when running inside bits
    from bits_helpers.cvmfs_catalog import data_url_for_hash, fetch_and_decompress
except ImportError:  # pragma: no cover - standalone use
    from cvmfs_catalog import data_url_for_hash, fetch_and_decompress


class StageError(Exception):
    """Anything that should stop a publish with a legible message."""


# A CVMFS content hash as this stack produces them: SHA-1, lower-case hex.
_HASH_RE = re.compile(r"^[0-9a-f]{40}$")


def parse_manifest(text):
    """Pull the fields we need out of a .cvmfspublished / -o manifest.

    Line-oriented, one letter of key per line: C=root catalog hash, N=repository
    name, S=revision, B=root catalog size.
    """
    out = {}
    for line in text.splitlines():
        line = line.strip()
        # STOP at the separator. What follows is the field hash and then a
        # binary RSA signature, and this function is fed a whole .cvmfspublished
        # decoded with errors="replace". Any signature byte 0x43 after a newline
        # looks like a "C" line and silently REPLACES the root hash -- measured
        # at 3.15% of random signatures. The bad value then goes straight to
        # `-b <base>` with nothing validating it.
        if line == "--":
            break
        if not line:
            continue
        key, value = line[0], line[1:].strip()
        if key == "C":
            out["root"] = value
        elif key == "N":
            out["repo"] = value
        elif key == "S":
            out["revision"] = value
    if "root" not in out:
        raise StageError("manifest names no root catalog (no C line)")
    if not _HASH_RE.match(out["root"]):
        raise StageError("manifest root %r is not a CVMFS hash -- the manifest "
                         "is truncated or the signature was parsed as fields"
                         % out["root"][:64])
    return out


def read_catalog(blob):
    """Return (properties, nested) from a decompressed catalog object.

    nested is a list of (path, sha1) as stored: the sha1 carries no suffix.
    """
    fd, path = tempfile.mkstemp(suffix=".cvmfscatalog")
    os.write(fd, blob)
    os.close(fd)
    try:
        db = sqlite3.connect(path)
        try:
            props = dict(db.execute("select key, value from properties").fetchall())
            nested = db.execute("select path, sha1 from nested_catalogs").fetchall()
        finally:
            db.close()
    except sqlite3.DatabaseError as exc:
        raise StageError("not a readable CVMFS catalog: %s" % exc)
    finally:
        os.unlink(path)
    return props, nested


def http_fetcher(stage_url, repo_url, timeout=30):
    """Fetch a catalog by hash: staging prefix first, repository as fallback.

    The fallback is not an optimisation. See the module docstring: the prepare
    does not re-stage catalogs it did not change.
    """
    def fetch(cat_hash):
        try:
            return fetch_and_decompress(data_url_for_hash(stage_url, cat_hash),
                                        timeout=timeout)
        except Exception:
            return fetch_and_decompress(data_url_for_hash(repo_url, cat_hash),
                                        timeout=timeout)
    return fetch


def find_subtree_catalog(root_hash, lease_path, fetch):
    """Walk from root_hash to the catalog whose root_prefix is lease_path.

    Returns the unsuffixed hash. Raises StageError if no catalog covers the
    path — which means the prepare did not produce what this publish needs, and
    guessing at a substitute is exactly the failure this function exists to
    prevent.
    """
    if not _HASH_RE.match(root_hash or ""):
        raise StageError("implausible root catalog hash %r" % root_hash)
    # An empty or "/" path makes want == "/", which matches the root catalog on
    # the first iteration -- so the one input that disables this guard would
    # return the whole revision, silently, which is the failure the walk exists
    # to prevent. Refuse instead: nothing legitimately publishes at the root.
    if not (lease_path or "").strip("/"):
        raise StageError("refusing to look up a catalog for an empty publish "
                         "path: that resolves to the repository root, and "
                         "grafting the root catalog publishes a whole revision")
    want = "/" + lease_path.strip("/")

    seen = set()
    queue = [root_hash]
    visited = []
    while queue:
        h = queue.pop(0)
        if h in seen:
            continue
        seen.add(h)
        try:
            blob = fetch(h)
        except Exception as exc:
            # A referenced catalog we cannot reach is not fatal on its own: it
            # may be an untouched branch of the tree we do not need. Record it
            # so a failure can say what was unreachable.
            visited.append((h, "<unreachable: %s>" % exc))
            continue
        props, nested = read_catalog(blob)
        prefix = props.get("root_prefix", "/")
        visited.append((h, prefix))
        if prefix == want:
            return h
        for npath, sha in nested:
            if not sha or sha in seen:
                continue
            # Descend only where the target could be. npath is the full
            # repository path of the nested catalog, so the target is inside it
            # only if want is npath or below it.
            np = "/" + (npath or "").strip("/")
            if want == np or want.startswith(np.rstrip("/") + "/"):
                queue.append(sha)

    seen_list = "\n".join("  %s  %s" % (h, p) for h, p in visited)
    raise StageError(
        "no catalog covers %r.\nCatalogs reached from %s:\n%s\n"
        "The prepare did not produce a subtree catalog for this path; "
        "publishing the root catalog instead would graft a whole revision."
        % (want, root_hash, seen_list))


def staging_prefix(host, user, job_id):
    """staging/<host>/<user>/<job-id>, sanitised for prepub's validator.

    prepub accepts slash-separated segments of [A-Za-z0-9._-], at most 128
    bytes, with no trailing "data" segment. Host and user names routinely carry
    characters outside that set, so they are folded rather than rejected: a
    publish should not fail because someone's login has an @ in it.
    """
    def clean(part, fallback):
        part = re.sub(r"[^A-Za-z0-9._-]", "-", str(part or "")).strip("-.")
        return part or fallback

    job = clean(job_id, "unknown-job")
    # prepub refuses a prefix whose LAST segment is "data", because promotion
    # appends "/data/" itself and the collision is the likeliest producer
    # mistake. A job id of "data" is improbable but constructible — CI ids are
    # numeric, an interactive one is whatever was passed — and it must not be
    # the thing that fails a publish at the last step.
    if job == "data":
        job = "data-job"

    prefix = "staging/%s/%s/%s" % (clean(host, "unknown-host"),
                                   clean(user, "unknown-user"),
                                   job)
    if len(prefix) > 128:
        raise StageError("staging prefix exceeds prepub's 128-byte limit: %r" % prefix)
    return prefix


def fetch_published_root(repo_url, timeout=30):
    """The repository's current root hash, from .cvmfspublished.

    This is the base the prepare computes against, and later the old_root_hash
    of the graft.
    """
    import urllib.request
    req = urllib.request.Request(repo_url.rstrip("/") + "/.cvmfspublished",
                                 headers={"User-Agent": "bits-cvmfs-stage"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # The manifest is text fields followed by a binary signature.
        text = resp.read().decode("utf-8", errors="replace")
    return parse_manifest(text)["root"]


def scratch_dir(repo, job_id, spool=None):
    """A per-invocation scratch directory: <spool>/tmp/stage-<job-id>.

    `cvmfs_server` serialises transactions with a per-repository lock. Invoking
    `cvmfs_swissknife` directly bypasses that lock entirely, so two concurrent
    prepares on one build host would share <spool>/tmp with nothing arbitrating
    -- and a CI runner runs jobs concurrently by design. The per-submission
    staging prefix keeps the S3 side disjoint; this keeps the local side
    disjoint too.

    Named from the job id rather than randomly so that leftovers are
    attributable: a scratch directory found next week names the submission that
    made it. Verified on the testbed that swissknife accepts a nested -t.

    NOT isolated by this: <spool>/stats.db, which every invocation writes.
    """
    spool = spool or "/var/spool/cvmfs/%s" % repo
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", str(job_id or "")).strip("-.") or "nojob"
    return os.path.join(spool, "tmp", "stage-%s" % safe)


def prepare_argv(repo, lease_path, tar_path, stage_prefix, s3_conf,
                 stratum0_url, base_root, manifest_out, spool=None,
                 pubkey=None, swissknife="cvmfs_swissknife", tmp=None):
    """Build the `cvmfs_swissknife ingest` command line for a prepare.

    Kept as a separate, pure function because the one thing that must never
    regress here is testable only by inspecting the argv: **neither -P (session
    token) nor -H (gateway key) may appear**. Those two flags are what make the
    binary talk to a gateway; omitting them is the entire mechanism by which a
    producer prepares without publishing (MEASUREMENTS §18). A prepare that
    silently acquired a lease would be a publish nobody asked for.

    Flag-for-flag the invocation proven in §18.
    """
    spool = spool or "/var/spool/cvmfs/%s" % repo
    pubkey = pubkey or "/etc/cvmfs/keys/%s.pub" % repo
    # Both -t and the S3 spooler's scratch argument point at the same directory.
    # Callers pass a per-invocation one (see scratch_dir); the shared default
    # remains only so the signature stays usable in isolation.
    tmp = tmp or "%s/tmp" % spool

    argv = [
        swissknife, "ingest",
        "-u", "/cvmfs/%s" % repo,
        "-c", "%s/rdonly" % spool,
        "-t", tmp,
        "-b", base_root,
        # The S3 spooler writes into the staging prefix. The alias IS the key
        # prefix: objects land at <bucket>/<alias>/data/...
        "-r", "S3,%s,%s@%s" % (tmp, stage_prefix, s3_conf),
        "-w", stratum0_url,
        "-o", manifest_out,
        "-K", pubkey,
        "-N", repo,
        "-U", "0", "-G", "0",
        "-T", tar_path,
        "-B", lease_path.strip("/"),
        "-C", "true",
    ]
    for forbidden in ("-P", "-H"):
        if forbidden in argv:
            raise StageError(
                "prepare argv contains %s: this would contact the gateway, "
                "which is exactly what a producer-side prepare must not do"
                % forbidden)
    return argv
