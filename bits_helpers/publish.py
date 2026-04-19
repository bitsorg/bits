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
import tempfile
from os.path import abspath, basename, exists, join

from bits_helpers.log import debug, error, info, banner
from bits_helpers.utilities import detectArch


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
    """Orchestrate the build-host publishing pipeline."""

    architecture = getattr(args, "architecture", None) or detectArch()
    work_dir     = abspath(args.workDir)
    package      = args.package
    version      = getattr(args, "version", None)
    cvmfs_target = args.cvmfsTarget
    spool        = args.spool
    scratch_dir  = getattr(args, "scratchDir", None)
    rsync_opts   = getattr(args, "rsyncOpts", None)

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
    info("spool       : %s", spool)

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
        #      all embedded paths are already correct for CVMFS.  Stream
        #      the working copy to the spool in a single bulk rsync.
        # ------------------------------------------------------------------
        info("--no-relocate: skipping relocation (package built at final CVMFS path)")
        info("Transferring tree to spool …")
        _rsync_to_spool(copy_dir + "/", spool, pkg_id,
                        extra_opts=rsync_opts, remove_source=False)
    else:
        # ------------------------------------------------------------------
        # 3. Start inotifywait watcher (overlaps with relocation)
        # ------------------------------------------------------------------
        watcher = _stream_with_inotify(copy_dir, spool, pkg_id, rsync_opts)

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
        # 5. Stop watcher; bulk-rsync if inotify was unavailable
        # ------------------------------------------------------------------
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
    # 6. Write sentinel
    # ------------------------------------------------------------------
    info("Writing sentinel %s.done …", pkg_id)
    _write_sentinel(spool, pkg_id, cvmfs_target, rsync_opts=rsync_opts)

    # ------------------------------------------------------------------
    # 7. Cleanup working copy
    # ------------------------------------------------------------------
    info("Cleaning up working copy …")
    shutil.rmtree(copy_dir, ignore_errors=True)
    if not scratch_dir:
        shutil.rmtree(_tmpparent, ignore_errors=True)

    info("Done — package %s queued for ingestion.", pkg_id)
