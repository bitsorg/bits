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

The S3 configuration is the one the repository itself uses: the path after the
"@" in CVMFS_UPSTREAM_STORAGE in /etc/cvmfs/repositories.d/<repo>/server.conf,
which is also where the repository's key prefix (its "alias") comes from.
Taking both from one line is what guarantees the objects are written to the
store the repository is served from.

When that file is absent or unreadable -- an s3.conf is conventionally 0600
owned by the publishing user -- the search order from ADR-0011 D10 applies
instead: $HOME/.bits/<repo>.s3.conf, then /etc/cvmfs/keys/<repo>.s3.conf, then
/etc/cvmfs/s3/<repo>.s3.conf. That is a fine way to find a CREDENTIAL and a
poor way to decide which BUCKET, so it warns when it is used.

This command never contacts the gateway and never holds a gateway or prepub
credential: publishing goes through prepub, which holds both.

The S3 credential is NOT scoped, and saying otherwise would misstate the
security posture. ADR-0011 D2 wants a credential able to write only its own
staging prefix; RGW at CERN cannot express that without an administrator
granting `user-policy` capability (`radosgw-admin caps add --uid=... --caps=
"user-policy=*"`), which has not happened. So today this is the repository's
S3 credential, and a build host that holds it can write anywhere in the
bucket. That is a real limitation of the deployment, not of the design, and
it bounds how far the "producer needs nothing privileged" argument reaches.

On a TESTBED the credentials come from .env.s3, which init.sh creates ONCE
with a generated password and thereafter preserves, regenerating
<repo>.s3.conf from it on every init. They therefore SURVIVE a rebuild. An
earlier version of this docstring said they were disposable and warned that a
copy would go stale; that is true only if .env.s3 is deleted, and it
overstated the risk.

What does not survive is a copy taken somewhere else. Prefer the path the
repository's own server.conf names -- init.sh keeps that one current, and a
second copy is the thing that silently ages out.
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
        parse_manifest, prepare_argv, probe_staged_object, repo_upstream,
        scratch_dir, staging_prefix, staging_url,
    )
except ImportError:  # pragma: no cover
    from cvmfs_stage import (  # type: ignore
        StageError, fetch_published_root, find_subtree_catalog, http_fetcher,
        parse_manifest, prepare_argv, probe_staged_object, repo_upstream,
        scratch_dir, staging_prefix, staging_url,
    )


def path_problem(path):
    """Why `path` is unusable, distinguishing ABSENT from UNREACHABLE.

    os.path.exists() answers False for both "no such file" and "an ancestor
    directory is not searchable by this user", because it swallows the OSError
    that stat() raises. Those have nothing in common as far as the fix goes:

      absent      -- the testbed never wrote it, or wrote it somewhere else;
                     look on the machine that generates it.
      unreachable -- the file is there and correct; a home directory is 0700
                     and the service account cannot traverse it. chmod o+x.

    Reporting the first when it is the second sends the reader to the wrong
    machine to look for a file that is sitting right where it should be. This
    happened on the first staged run against the testbed.

    Returns "" when the path is readable.
    """
    if os.access(path, os.R_OK):
        return ""
    # Walk the ancestors top-down: the FIRST unsearchable one is the cause,
    # and anything below it would report a misleading "does not exist".
    walked = os.sep
    for part in os.path.abspath(path).split(os.sep)[1:-1]:
        walked = os.path.join(walked, part)
        if not os.access(walked, os.X_OK):
            return ("is unreachable: %s is not searchable by uid %d. The file "
                    "may well exist and be correct -- run `chmod o+x %s` (or "
                    "use a path outside a private home directory)"
                    % (walked, os.getuid(), walked))
    if not os.path.exists(path):
        return "does not exist"
    return "exists but is not readable by uid %d" % os.getuid()


def find_s3_conf(repo, explicit=None):
    """$HOME/.bits first, then the system path (ADR-0011 D10).

    Readability, not mere existence: an s3.conf is conventionally 0600 owned
    by the publishing user, so a path that exists but cannot be read by THIS
    process is not a usable answer -- selecting it only moves the failure into
    swissknife, which reports it as a shell error while sourcing the file.
    """
    if explicit:
        why = path_problem(explicit)
        if why:
            raise StageError("S3 config %s %s" % (explicit, why))
        return explicit
    candidates = [
        os.path.join(os.path.expanduser("~"), ".bits", "%s.s3.conf" % repo),
        # The canonical CVMFS location, alongside the signing material.
        "/etc/cvmfs/keys/%s.s3.conf" % repo,
        # The testbed's CONTAINER layout, kept only as a fallback so a config
        # copied from a container image is still found. Not canonical: on a
        # build host this directory does not exist. (An earlier version of this
        # comment claimed /etc/cvmfs/keys holds no S3 config, generalising from
        # one host where none had been provisioned there yet. It is the
        # canonical home; the absence was the anomaly.)
        "/etc/cvmfs/s3/%s.s3.conf" % repo,
    ]
    for c in candidates:
        if os.access(c, os.R_OK):
            return c
    raise StageError(
        "no readable staging S3 config found:\n  %s"
        % "\n  ".join("%s  -- %s" % (c, path_problem(c)) for c in candidates))


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
    # The repository's S3 key prefix. Normally read from the repository's own
    # server.conf; the override exists for hosts that have no repository
    # configuration and for tests. It is NOT the repository name: production
    # uses "cvmfs/bits.cern.ch" for repository "bits.cern.ch".
    ap.add_argument("--repo-alias", default="",
                    help="S3 key prefix the repository's objects live under; "
                         "read from server.conf when not given")
    ap.add_argument("--server-conf", default="",
                    help="path to the repository's server.conf; default "
                         "/etc/cvmfs/repositories.d/<repo>/server.conf")
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
        # No default. The previous one -- http://cvmfs-stratum-zero.cern.ch/
        # cvmfs/<repo> -- names a stratum0 web front end rather than the object
        # store, and it has exactly the right SHAPE to satisfy every check
        # below: it ends with the production alias "cvmfs/bits.cern.ch", so the
        # URL/alias agreement test passes and the bucket root comes out as a
        # host that serves no staged objects at all. A plausible wrong answer
        # is worse here than no answer.
        stratum0 = (a.stratum0_url or "").strip()
        if not stratum0:
            # Names THIS repository and shows a placeholder, not a worked
            # example from another deployment. The previous wording ended with
            # "e.g. http://cvmfs-bits.s3.cern.ch/cvmfs/bits.cern.ch", and a
            # reader of a testbed job log reasonably took that production URL
            # for the value in use -- concluding the job was pointed at the
            # wrong bucket when in fact no value was set at all. An example is
            # not worth a misdiagnosis in the one message whose job is to say
            # that a value is MISSING.
            raise StageError(
                "no object-store URL for %s: set BITS_STRATUM0_URL or pass "
                "--stratum0-url.\n"
                "  Nothing was fetched -- this is a missing setting, not a "
                "wrong one.\n"
                "  The value is the S3 bucket root followed by this "
                "repository's alias:\n"
                "      http://<bucket-root>/<alias>\n"
                "  The alias is the text between the last comma and the '@' in "
                "CVMFS_UPSTREAM_STORAGE\n"
                "  in /etc/cvmfs/repositories.d/%s/server.conf.\n"
                "  bits-console sends it per community from ui-config.yaml's "
                "stratum0_url." % (a.repo, a.repo))
        stage = staging_prefix(a.host, user, a.job_id)

        # Read the repository's own server.conf BEFORE the prepare runs. It is
        # a local file read costing nothing, and the alternative is discovering
        # a missing server.conf after a multi-minute ingest has finished. A
        # --dry-run must still work on a laptop that has no repository
        # configuration at all, so there it degrades to a warning.
        override = a.repo_alias.strip("/")
        alias, conf_from_server = "", ""
        try:
            alias, conf_from_server = repo_upstream(a.repo, a.server_conf or None)
        except StageError:
            # An explicitly named --server-conf that cannot be read is an
            # error whatever else was passed: the operator pointed at a
            # specific file and it did not answer.
            if a.server_conf or not (a.dry_run or override):
                raise
            if override:
                print("[cvmfs-stage] no repository configuration read; using "
                      "--repo-alias", file=sys.stderr)
            else:
                print("[cvmfs-stage] no repository configuration read; "
                      "--dry-run continues without an alias", file=sys.stderr)

        if override:
            # Tested AFTER strip("/"): `--repo-alias /` used to fall through to
            # server.conf and now would blank a value already read from it,
            # leaving an empty alias that survives all the way to a "the
            # staging URL is wrong" report after the full ingest.
            if alias and override != alias:
                print("[cvmfs-stage] WARNING: --repo-alias %s overrides %s from "
                      "server.conf. The S3 configuration still comes from that "
                      "same server.conf line, so these two now describe "
                      "different stores -- pass --s3-conf as well if that is "
                      "not what you meant." % (override, alias), file=sys.stderr)
            alias = override

        # Nothing below can do anything useful without an alias, and a real run
        # that reaches the ingest without one wastes the whole prepare before
        # failing with a misleading message about the staging URL.
        if not alias and not a.dry_run:
            raise StageError(
                "no repository S3 alias: it was not found in server.conf and "
                "--repo-alias gave nothing usable")

        # The S3 configuration comes from the SAME CVMFS_UPSTREAM_STORAGE line
        # as the alias, so the store written to is by construction the store
        # the repository is served from. find_s3_conf's search order remains
        # only for hosts with no repository configuration at all: it prefers
        # $HOME/.bits/<repo>.s3.conf, which is a fine credential location but a
        # poor way to decide WHICH bucket -- it can name a different store than
        # the repository uses, and objects would then be written to one and
        # read back from the other. That is a silent 404, not an error.
        #
        # The fallback is kept live rather than made unreachable: an s3.conf is
        # conventionally 0600 owned by the publishing user, and a build host
        # where the repository's copy is root-only but the user has a readable
        # one in $HOME used to work. Losing that would be a regression, so an
        # unreadable server.conf-named file degrades to the search order with
        # a warning rather than failing.
        if a.s3_conf:
            s3_conf = find_s3_conf(a.repo, a.s3_conf)
        elif conf_from_server and os.access(conf_from_server, os.R_OK):
            s3_conf = conf_from_server
        else:
            if conf_from_server:
                print("[cvmfs-stage] %s,\n  named by CVMFS_UPSTREAM_STORAGE in "
                      "the repository's server.conf, %s.\n  Falling back to the "
                      "search order, which may name a different store."
                      % (conf_from_server, path_problem(conf_from_server)),
                      file=sys.stderr)
            s3_conf = find_s3_conf(a.repo)
        print("[cvmfs-stage] S3 config: %s" % s3_conf, file=sys.stderr)

        # Resolve the read-back URL NOW, not after the ingest. A URL that
        # disagrees with the alias is the failure this whole path exists to
        # catch, and discovering it once swissknife has already run costs the
        # entire prepare. --dry-run reaches this line too, so it is a real
        # preflight for the misconfiguration and not just a command printer.
        stage_url = staging_url(stratum0, alias, stage) if alias else ""

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
        print("[cvmfs-stage] repo=%s alias=%s base=%s"
              % (a.repo, alias or "<unknown>", base), file=sys.stderr)
        print("[cvmfs-stage] staging prefix: %s" % stage, file=sys.stderr)
        print("[cvmfs-stage] staged objects will be read from %s"
              % (stage_url or "<unresolved>"), file=sys.stderr)

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

        # Before walking: confirm the staging URL actually serves an object we
        # just wrote. The walk's fallback to the repository is what makes a bad
        # URL look like a bad prepare, so this question has to be asked while
        # the answer is still unambiguous.
        probe_staged_object(stage_url, m["root"])
        print("[cvmfs-stage] staging URL confirmed readable", file=sys.stderr)

        # The manifest names the ROOT catalog. prepub needs the SUBTREE one.
        # stage_url was resolved before the ingest; see there for why the
        # bucket root is not a fixed number of segments off the repository URL.
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
