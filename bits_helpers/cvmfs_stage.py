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

Reading the staged objects back needs the BUCKET root, not the repository URL
--------------------------------------------------------------------------
The staging prefix is bucket-root relative: `staging/<host>/<user>/<job>` is
handed to the spooler as its alias, so objects land at exactly that key. To
fetch them over HTTP the walk needs the bucket root, and the only thing the
caller has is the repository's URL -- which is the bucket root plus the
repository's own alias.

Deriving one from the other by chopping a fixed number of path segments is
wrong, because the two deployments have different alias depths:

    production   http://cvmfs-bits.s3.cern.ch/cvmfs/bits.cern.ch
                 bucket cvmfs-bits (virtual-host), alias "cvmfs/bits.cern.ch"
    testbed      http://minio:9000/cvmfs/test.cvmfs.io
                 bucket cvmfs (path-style),        alias "test.cvmfs.io"

An earlier version chopped exactly one segment. That is correct for the testbed
and wrong for production, where it produced `.../cvmfs/staging/...` for objects
written at `staging/...`. The failure is not a 404 the caller sees: the fetcher
falls back to the repository, the repository does not have the newly staged
catalogs, and the walk reports "the prepare did not produce a subtree catalog
for this path" -- a confident wrong diagnosis pointing at swissknife. So the
alias is read from the repository configuration that defines it, and the URL is
required to end with it rather than assumed to.
"""

import os
import re
import sqlite3
import tempfile
import time
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


def fetch_repo_catalog(repo_url, cat_hash, timeout=30):
    """Fetch a catalog from the REPOSITORY only, never the staging prefix.

    http_fetcher tries staging first and falls back to the repository, which
    is right for the walk. It is wrong for answering "is this path already
    published?", where a staged copy would produce a false yes.
    """
    return fetch_and_decompress(data_url_for_hash(repo_url, cat_hash),
                                timeout=timeout)


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


def parse_s3_upstream(value):
    """Split a CVMFS_UPSTREAM_STORAGE value into (alias, s3_config_path).

    The value is the one CVMFS itself parses:

        S3,<scratch-dir>,<repo_alias>@<s3.conf>

    The alias is the key prefix every object of the repository lives under --
    `upload_s3.cc` builds "<alias>/data/<hash-path>" -- so it is what turns a
    bucket into a repository URL, and it is not derivable from the repository
    name: production uses "cvmfs/bits.cern.ch" for repository "bits.cern.ch".
    """
    value = (value or "").strip().strip("'\"")
    fields = value.split(",")
    # Exactly three fields, and "S3" case-sensitively: this is what CVMFS
    # itself accepts (upload_spooler_definition.cc refuses upstream.size() != 3
    # and compares the tag literally). Being more permissive here would accept
    # values the publisher rejects, so a malformed upstream would be diagnosed
    # by whatever failed next instead of by this function.
    if len(fields) != 3 or fields[0].strip() != "S3":
        raise StageError(
            "CVMFS_UPSTREAM_STORAGE is not an S3 spooler configuration: %r. "
            "Expected exactly S3,<scratch-dir>,<alias>@<s3.conf>. The staged "
            "publish path stages into S3 and has nothing to read back from "
            "any other upstream." % value[:200])
    alias, sep, conf = fields[2].partition("@")
    alias = alias.strip().strip("/")
    if not alias or not sep or not conf.strip():
        raise StageError(
            "cannot read <alias>@<s3.conf> out of CVMFS_UPSTREAM_STORAGE: %r"
            % value[:200])
    return alias, conf.strip()


def repo_upstream(repo, server_conf=None):
    """(alias, s3_config_path) from the repository's own server.conf.

    Both halves come from ONE line, which is the point: the alias says where
    the repository's objects live inside the bucket, and the path says which
    S3 configuration names that bucket. Taking them from different places is
    how a prepare ends up writing to one store and reading from another.

    Later assignments win, as they would in the shell that sources this file,
    and `export KEY=value` counts as an assignment -- which is the form that
    silently lost to a stale earlier line in the first version of this
    function, producing a "the URL and the alias disagree" error that blamed
    the configuration for a parser bug.

    This is a deliberately shallow reader, not a shell: it does not expand
    variables, follow continuations or evaluate conditionals. Every one of
    those produces a StageError further on rather than a wrong answer, because
    the value either fails to parse or fails to match the repository URL.
    """
    path = server_conf or "/etc/cvmfs/repositories.d/%s/server.conf" % repo
    try:
        # errors="replace": a stray non-UTF-8 byte anywhere in the file must
        # not turn into a UnicodeDecodeError traceback out of main(), which
        # catches StageError only.
        with open(path, "r", errors="replace") as fh:
            text = fh.read()
    except IOError as exc:
        raise StageError(
            "cannot read %s: %s -- the staged publish path needs the "
            "repository's S3 alias, which lives in CVMFS_UPSTREAM_STORAGE "
            "there. Pass --repo-alias to override." % (path, exc))

    value = None
    for line in text.splitlines():
        line = line.strip()
        key, sep, rest = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key == "CVMFS_UPSTREAM_STORAGE":
            value = rest
    if value is None:
        raise StageError("%s sets no CVMFS_UPSTREAM_STORAGE" % path)
    return parse_s3_upstream(value)


def repo_alias(repo, server_conf=None):
    """Just the alias half of repo_upstream()."""
    return repo_upstream(repo, server_conf)[0]


def bucket_root_url(repo_url, alias):
    """Strip the repository's alias off its URL, leaving the bucket root.

    REQUIRES rather than assumes: the repository URL must end with the alias.
    When it does not, the console's `stratum0_url` and the repository's own
    configuration disagree about where the repository lives, and every
    subsequent fetch would silently address the wrong keys. See the module
    docstring for what that looked like the first time.
    """
    url = (repo_url or "").rstrip("/")
    if "://" not in url:
        raise StageError("repository URL %r is not absolute" % repo_url)
    alias = (alias or "").strip("/")
    if not alias:
        raise StageError("empty repository alias: nothing to strip from %r" % repo_url)

    # The leading slash is what makes this a SEGMENT match rather than a
    # substring one: without it, alias "cvmfs.io" would match the tail of
    # ".../test.cvmfs.io" and silently cut a hostname in half.
    suffix = "/" + alias
    if not url.endswith(suffix):
        raise StageError(
            "the repository URL and the repository's S3 alias disagree.\n"
            "  URL   (BITS_STRATUM0_URL): %s\n"
            "  alias (CVMFS_UPSTREAM_STORAGE): %s\n"
            "The URL must be the bucket root followed by the alias, e.g.\n"
            "  http://cvmfs-bits.s3.cern.ch/cvmfs/bits.cern.ch  (alias cvmfs/bits.cern.ch)\n"
            "Refusing to guess: staged objects would be fetched from the wrong "
            "keys, and the walk would then blame the prepare for producing no "
            "subtree catalog." % (url, alias))

    root = url[:-len(suffix)]
    if "://" not in root or not root.split("://", 1)[1]:
        raise StageError(
            "stripping alias %r from %r leaves no bucket root (%r)"
            % (alias, url, root))
    return root


def staging_url(repo_url, alias, stage_prefix):
    """The HTTP base under which this run's staged objects can be read.

    Bucket root + the staging prefix, because the prefix is what the spooler
    was given as its alias and is therefore bucket-root relative.
    """
    stage_prefix = (stage_prefix or "").strip("/")
    if not stage_prefix:
        raise StageError("empty staging prefix")
    return "%s/%s" % (bucket_root_url(repo_url, alias), stage_prefix)


def probe_staged_object(stage_url, cat_hash, timeout=30, retry_delay=2):
    """Confirm one object we KNOW was just staged is readable at stage_url.

    The premise is checked, not assumed: WritableCatalogManager::Commit calls
    root_catalog->SetDirty() unconditionally (catalog_mgr_rw.cc:1320), so the
    root catalog is always in the set SnapshotCatalogs uploads, and
    swissknife_ingest.cc:163 gives the catalog spooler the same staging
    destination as the data spooler. After a successful prepare that object is
    in the staging prefix whether or not the tar changed anything.

    A failure here therefore means the staging URL is wrong -- OR that the
    object store was briefly unreachable. Both are reported, because
    fetch_and_decompress flattens timeouts, 5xx and 403 into one error and
    claiming certainty about which would be the same overconfidence this
    function exists to prevent. One retry, because a publish that already paid
    for the ingest should not die on a single dropped connection.

    This exists because the alternative diagnosis is wrong and convincing.
    Without it, a bad staging URL makes every fetch 404, http_fetcher silently
    falls back to the repository, the repository does not have the newly staged
    catalogs, and find_subtree_catalog reports "the prepare did not produce a
    subtree catalog for this path" -- sending the reader to swissknife when the
    fault is a URL.

    Deliberately empirical. The bucket root could instead be computed from the
    s3.conf, but CVMFS does not parse those files, it EXECUTES them
    (options.cc BashOptionsManager pipes the file through a shell and reads
    values back with `echo $PARAM`), so any reimplementation here would
    disagree with the uploader on `$VAR`, backticks, comments and quoting. One
    HTTP request answers the question that a parser could only estimate.
    """
    # Wrapped: data_url_for_hash raises FastPathUnavailable on a short hash,
    # and main() catches only StageError, so it would escape as a traceback.
    try:
        url = data_url_for_hash(stage_url, cat_hash)
    except Exception as exc:
        raise StageError("cannot address catalog %r under %s: %s"
                         % (cat_hash, stage_url, exc))

    last = None
    for attempt in (1, 2):
        try:
            fetch_and_decompress(url, timeout=timeout)
            return
        except Exception as exc:
            last = exc
            if attempt == 1:
                time.sleep(retry_delay)

    raise StageError(
        "the prepare succeeded, but the object it just staged cannot be read "
        "back (2 attempts):\n"
        "  %s\n"
        "  %s\n"
        "That object was written moments ago, so either the staging URL is "
        "wrong or the object store is unreachable. If the error above is a "
        "404, it is the URL: check the community's stratum0_url in "
        "bits-console -- it must be the bucket root followed by the "
        "repository's S3 alias, and the bucket root must be the host serving "
        "the object store, not a stratum0 web front end."
        % (url, last))


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
                 pubkey=None, swissknife="cvmfs_swissknife", tmp=None,
                 replace=False):
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
    if replace:
        # -D is "entity to delete before to extract the tar". Without it,
        # extracting into a path that already holds this version re-adds
        # entries the catalog has, and the C++ ABORTS:
        #   PANIC: catalog_rw.cc:168 failed to add '<path>/relocate-me.sh'
        #   ... UNIQUE constraint failed: catalog.md5path_1, catalog.md5path_2
        #
        # Opt-in, never a default. It DELETES the published path inside the
        # revision being prepared, so a caller that did not mean to republish
        # gets a loud failure instead of silently discarding whatever was
        # there -- possibly a different build that landed at the same path.
        # ADR-0011: anything that deletes state runs only on an explicit flag,
        # never as a side effect of a missing-file heuristic.
        argv += ["-D", lease_path.strip("/")]
    for forbidden in ("-P", "-H"):
        if forbidden in argv:
            raise StageError(
                "prepare argv contains %s: this would contact the gateway, "
                "which is exactly what a producer-side prepare must not do"
                % forbidden)
    return argv
