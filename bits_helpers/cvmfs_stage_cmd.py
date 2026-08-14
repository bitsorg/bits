#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""`bits cvmfs-stage` — prepare a package for a staged CVMFS publish.

Runs the canonical publisher against a staging S3 prefix with no gateway, then
names the catalog prepub must graft. Prints the two values a staged submission
carries, and nothing else on stdout, so a caller can eval them:

    eval "$(bits cvmfs-stage --repo software.cern.ch --path x86_64-el9/pkg/1.0 \\
              --tar /tmp/pkg.tar --job-id "$CI_JOB_ID")"
    # -> BITS_STAGING_PREFIX=staging/<host>/<user>/<job>
    #    BITS_CATALOG_HASH=<40 hex>C

--json emits the same as an object. Progress and errors go to stderr.

The credential comes from $HOME/.bits/<repo>.s3.conf if present, else
/etc/cvmfs/keys/<repo>.s3.conf (ADR-0011 D10). It is the STAGING credential,
scoped to write only its own prefix; it is never the CVMFS bucket credential,
and this command never contacts the gateway.
"""

import argparse
import getpass
import json
import os
import socket
import contextlib
import fcntl
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

try:
    from bits_helpers.cvmfs_stage import (
        StageError, fetch_published_root, find_subtree_catalog, http_fetcher,
        parse_manifest, prepare_argv, scratch_dir, staging_prefix,
    )
except ImportError:  # pragma: no cover
    from cvmfs_stage import (  # type: ignore
        StageError, fetch_published_root, find_subtree_catalog, http_fetcher,
        parse_manifest, prepare_argv, scratch_dir, staging_prefix,
    )


def find_s3_conf(repo, explicit=None):
    """$HOME/.bits first, then the system path (ADR-0011 D10)."""
    if explicit:
        if not os.path.exists(explicit):
            raise StageError("no S3 config at %s" % explicit)
        return explicit
    candidates = [
        os.path.join(os.path.expanduser("~"), ".bits", "%s.s3.conf" % repo),
        "/etc/cvmfs/keys/%s.s3.conf" % repo,
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    raise StageError("no staging S3 config found; looked in:\n  %s"
                     % "\n  ".join(candidates))


@contextlib.contextmanager
def prepare_lock(repo, spool=None, timeout=1800):
    """Serialise prepares for one repository on this host.

    `cvmfs_server` holds a per-repository lock around a transaction. Calling
    `cvmfs_swissknife` directly bypasses it, and two concurrent prepares then
    abort: both open the SAME statistics database and the loser dies with
    `sql.cc:21: Assertion 'success' failed` (SIGABRT) while exporting its
    manifest -- verified on the testbed, one of two concurrent runs failing.

    The stats path is CVMFS_STATISTICS_DB in the repository's server.conf, or
    /var/spool/cvmfs/<repo>/stats.db; it is repository configuration, not a
    command-line flag, so no argument this tool can pass makes the two runs
    disjoint. A per-invocation scratch directory is necessary and NOT
    sufficient.

    This costs nothing that matters: the parallelism ADR-0011 buys is across the
    runner fleet, not within one host, and a prepare that waits is still off the
    publisher's critical path.
    """
    spool = spool or "/var/spool/cvmfs/%s" % repo
    lock_dir = os.path.join(spool, "tmp")
    try:
        os.makedirs(lock_dir, exist_ok=True)
    except OSError as exc:
        raise StageError("cannot create %s for the prepare lock: %s" % (lock_dir, exc))
    lock_path = os.path.join(lock_dir, ".bits-cvmfs-stage.lock")

    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("[cvmfs-stage] waiting for another prepare of %s on this host"
                  % repo, file=sys.stderr)
            deadline = time.time() + timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise StageError(
                            "timed out after %ds waiting for another prepare of "
                            "%s on this host (lock: %s)"
                            % (timeout, repo, lock_path))
                    time.sleep(1)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bits cvmfs-stage")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--path", required=True, help="CVMFS path to publish at")
    ap.add_argument("--tar", required=True)
    # No CI_JOB_ID means an interactive run. Falling back to a constant would
    # give every such run by one user on one host the SAME staging prefix, and
    # promotion lists the whole prefix -- so each publish would promote the
    # others' objects, and a future prefix-delete cleanup would drop objects a
    # concurrent run had staged. A random suffix keeps them disjoint.
    ap.add_argument("--job-id",
                    default=os.environ.get("CI_JOB_ID")
                    or "local-" + uuid.uuid4().hex[:12])
    ap.add_argument("--stratum0-url", default=os.environ.get("BITS_STRATUM0_URL", ""))
    ap.add_argument("--s3-conf", default="")
    ap.add_argument("--swissknife", default="cvmfs_swissknife")
    ap.add_argument("--spool", default="",
                    help="spool root; default /var/spool/cvmfs/<repo>")
    ap.add_argument("--host", default=socket.gethostname())
    ap.add_argument("--user", default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--base-root", default="",
                    help="base revision to prepare against; read from the "
                         "repository when not given")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the prepare command and stop; makes no network "
                         "call, so it can be run anywhere")
    a = ap.parse_args(argv)

    try:
        user = a.user or getpass.getuser()
    except Exception:
        user = os.environ.get("USER", "")

    try:
        s3_conf = find_s3_conf(a.repo, a.s3_conf or None)
        stratum0 = a.stratum0_url or "http://cvmfs-stratum-zero.cern.ch/cvmfs/%s" % a.repo
        stage = staging_prefix(a.host, user, a.job_id)

        # The base the prepare computes against, and later the graft's
        # old_root_hash. Read from the repository, not assumed -- but a
        # --dry-run must work on a laptop with no route to stratum0, so the
        # fetch is skipped when the answer cannot matter.
        if a.base_root:
            base = a.base_root
        elif a.dry_run:
            base = "0" * 40
        else:
            try:
                base = fetch_published_root(stratum0)
            except Exception as exc:
                raise StageError("cannot read the current root from %s: %s"
                                 % (stratum0, exc))
        print("[cvmfs-stage] repo=%s base=%s" % (a.repo, base), file=sys.stderr)
        print("[cvmfs-stage] staging prefix: %s" % stage, file=sys.stderr)

        fd, manifest = tempfile.mkstemp(prefix="bits-stage-", suffix=".manifest")
        os.close(fd)
        try:
            # Per-invocation scratch: concurrent prepares on one host would
            # otherwise share <spool>/tmp with no lock, because calling
            # swissknife directly bypasses cvmfs_server's per-repo lock.
            tmp = scratch_dir(a.repo, a.job_id, a.spool or None)
            cmd = prepare_argv(
                repo=a.repo, lease_path=a.path, tar_path=a.tar,
                stage_prefix=stage, s3_conf=s3_conf, stratum0_url=stratum0,
                base_root=base, manifest_out=manifest, swissknife=a.swissknife,
                spool=a.spool or None, tmp=tmp)

            if a.dry_run:
                print(" ".join(cmd), file=sys.stderr)
                return 0

            os.makedirs(tmp, exist_ok=True)
            print("[cvmfs-stage] scratch: %s" % tmp, file=sys.stderr)

            print("[cvmfs-stage] preparing (no gateway)...", file=sys.stderr)
            # swissknife logs progress to STDOUT (kLogStdout throughout
            # swissknife_ingest.cc). This command's stdout is a contract -- the
            # two assignments and nothing else, so `eval "$(bits cvmfs-stage)"`
            # works -- so its child's stdout goes to stderr with the rest of the
            # progress output.
            try:
                with prepare_lock(a.repo, a.spool or None):
                    rc = subprocess.call(cmd, stdout=sys.stderr)
            except OSError as exc:
                # A missing cvmfs_swissknife is a runner prerequisite, not a
                # bug: name it rather than showing a traceback.
                raise StageError("cannot run %s: %s -- the staged publish path "
                                 "needs cvmfs_swissknife on PATH"
                                 % (a.swissknife, exc))
            if rc != 0:
                raise StageError("prepare failed with exit %d" % rc)

            with open(manifest, "rb") as fh:
                m = parse_manifest(fh.read().decode("utf-8", errors="replace"))
        finally:
            try:
                os.unlink(manifest)
            except OSError:
                pass
            # Remove the scratch directory on every exit. Leaving it would fill
            # the spool one prepare at a time, and it is ours alone.
            if not a.dry_run:
                shutil.rmtree(tmp, ignore_errors=True)

        # The manifest names the ROOT catalog. prepub needs the SUBTREE one.
        # rstrip first: a --stratum0-url with a trailing slash otherwise makes
        # this "<repo>/staging/..." , every staging fetch 404s, everything falls
        # back to the repository (which lacks the new objects), and the walk
        # fails claiming the prepare produced no subtree catalog -- a confident
        # wrong diagnosis.
        stage_url = "%s/%s" % (stratum0.rstrip("/").rsplit("/", 1)[0], stage)
        catalog = find_subtree_catalog(m["root"], a.path,
                                       http_fetcher(stage_url, stratum0))
        print("[cvmfs-stage] manifest root %s (not sent)" % m["root"], file=sys.stderr)
        print("[cvmfs-stage] subtree catalog %s" % catalog, file=sys.stderr)

        if a.json:
            print(json.dumps({"staging_prefix": stage,
                              "catalog_hash": catalog + "C",
                              "base_root": base}))
        else:
            print("BITS_STAGING_PREFIX=%s" % stage)
            print("BITS_CATALOG_HASH=%sC" % catalog)
        return 0

    except StageError as exc:
        print("[cvmfs-stage] ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
