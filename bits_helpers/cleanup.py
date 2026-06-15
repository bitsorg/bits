# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits cleanup — evict stale packages from a persistent build workDir.

Sentinel files
--------------
Every package installed by bits (or used as a dependency) is tracked via a
sentinel file at::

    workDir/.packages/<arch>/<package>/<version>

The sentinel's ``mtime`` records the last time the package was touched —
either built from source or pulled in as a transitive dependency of a
subsequent build.  Transitive touching ensures that a dependency that has not
been built directly in N days is not evicted while a dependent package that
*was* built recently still references it.

The cleanup command supports two eviction modes, which may be combined:

* **Age-based** (``--max-age N``): remove packages whose sentinel ``mtime``
  is older than N days.  Intended to be run as a nightly cron job on the
  build runner host.
* **Disk-pressure** (``--min-free G``): when free space on the workDir
  filesystem drops below G GiB, remove least-recently-used packages (oldest
  sentinel first) until the threshold is satisfied.  Intended to be called
  as a pre-build hook in the CI pipeline.

Concurrency safety
------------------
A shared ``flock`` advisory lock is held on the sentinel file for the
duration of any build job that uses the package.  The cleanup command
requests an exclusive non-blocking lock before evicting; if it cannot
acquire the lock the package is actively in use and is skipped.  The OS
releases all flock locks automatically when a process exits (crash, kill, or
OOM included), so stale locks never accumulate and no cleanup on job start is
required.  Multiple concurrent build jobs from the same community are fully
supported without any "one job at a time" restriction.
"""

import fcntl
import os
import shutil
import sys
import time
from os.path import abspath, basename, dirname, exists, join
from typing import List, NamedTuple, Optional

from bits_helpers.log import debug, error, info, warning


# ---------------------------------------------------------------------------
# Sentinel helpers — used by both cleanup.py and build.py
# ---------------------------------------------------------------------------

def sentinel_path(work_dir: str, architecture: str, package: str, version: str) -> str:
    """Return the canonical path of the sentinel file for *package*/*version*."""
    return join(abspath(work_dir), ".packages", architecture, package, version)


def touch_sentinel(work_dir: str, architecture: str, package: str, version: str) -> None:
    """Create or update the sentinel for *package*/*version*.

    Updates the sentinel's ``mtime`` to *now* so that the cleanup command
    counts this package as recently used.  A brief shared flock is held
    during the touch to prevent a concurrent cleanup from evicting the
    package at the exact moment we are registering it.

    Safe to call from multiple concurrent processes — the flock and the
    ``utime`` call are both atomic at the OS level.
    """
    path = sentinel_path(work_dir, architecture, package, version)
    os.makedirs(dirname(path), exist_ok=True)
    try:
        with open(path, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                os.utime(path, None)          # set mtime = now
                fcntl.flock(fh, fcntl.LOCK_UN)
            except BlockingIOError:
                # Cleanup is about to evict this package — log and carry on.
                # The build will fail shortly with a "not found" error, which
                # is the correct outcome when a package has been evicted.
                warning(
                    "Cannot touch sentinel for %s/%s — eviction may be in progress",
                    package, version,
                )
    except OSError as exc:
        warning("Could not update sentinel for %s/%s: %s", package, version, exc)


def acquire_build_lock(work_dir: str, architecture: str, package: str,
                       version: str):
    """Acquire a shared flock on the sentinel for the duration of a build.

    Returns an open file object that holds the lock.  The caller **must**
    close it when the build (or use) of the package is complete::

        lock_fh = acquire_build_lock(work_dir, arch, pkg, ver)
        try:
            ...  # build or use the package
        finally:
            lock_fh.close()   # releases the shared lock

    The lock is *blocking*: if a cleanup is currently evicting this package
    the call will wait until eviction is finished.  In practice eviction is
    fast (``shutil.rmtree`` + ``unlink``), so the wait is negligible.
    """
    path = sentinel_path(work_dir, architecture, package, version)
    os.makedirs(dirname(path), exist_ok=True)
    fh = open(path, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_SH)   # blocking shared lock
        os.utime(path, None)              # refresh mtime while holding lock
    except OSError:
        fh.close()
        raise
    return fh


# ---------------------------------------------------------------------------
# Package inventory
# ---------------------------------------------------------------------------

class _SentinelEntry(NamedTuple):
    sentinel_path: str   # full path to the sentinel file
    pkg_dir: str         # full path to the installed package directory
    mtime: float         # sentinel mtime (seconds since epoch)
    size_bytes: int      # approximate disk usage of pkg_dir


def _du(path: str) -> int:
    """Return total disk usage of *path* in bytes (best-effort, follows symlinks)."""
    total = 0
    try:
        for dirpath, _dirs, filenames in os.walk(path):
            for fname in filenames:
                fp = join(dirpath, fname)
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _list_sentinels(work_dir: str, architecture: str) -> List[_SentinelEntry]:
    """Return all known packages sorted by mtime ascending (oldest first)."""
    sentinel_root = join(abspath(work_dir), ".packages", architecture)
    if not exists(sentinel_root):
        return []

    entries = []
    try:
        packages = sorted(os.listdir(sentinel_root))
    except OSError:
        return []

    for package in packages:
        pkg_sentinel_dir = join(sentinel_root, package)
        if not os.path.isdir(pkg_sentinel_dir):
            continue
        try:
            versions = sorted(os.listdir(pkg_sentinel_dir))
        except OSError:
            continue
        for version in versions:
            spath = join(pkg_sentinel_dir, version)
            if not os.path.isfile(spath):
                continue
            pkg_dir = join(abspath(work_dir), architecture, package, version)
            try:
                mtime = os.path.getmtime(spath)
            except OSError:
                continue
            size = _du(pkg_dir) if exists(pkg_dir) else 0
            entries.append(_SentinelEntry(
                sentinel_path=spath,
                pkg_dir=pkg_dir,
                mtime=mtime,
                size_bytes=size,
            ))

    entries.sort(key=lambda e: e.mtime)
    return entries


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------

def _free_bytes(path: str) -> int:
    """Return available bytes on the filesystem containing *path*."""
    st = os.statvfs(path)
    return st.f_frsize * st.f_bavail


def _try_evict(entry: _SentinelEntry, dry_run: bool) -> bool:
    """Attempt to evict a single package.

    Tries an exclusive non-blocking flock on the sentinel.  Returns True if
    the package was (or would be, in dry-run mode) evicted, False if it is
    currently in use by another build job or cannot be removed.
    """
    try:
        fh = open(entry.sentinel_path, "r+")
    except OSError:
        return False
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        debug("Skipping %s — held by another job", entry.pkg_dir)
        fh.close()
        return False

    # We hold the exclusive lock — safe to evict.
    try:
        pkg_name = basename(dirname(entry.sentinel_path))
        pkg_ver  = basename(entry.sentinel_path)
        age_days = (time.time() - entry.mtime) / 86400
        size_mib = entry.size_bytes / 1e6
        if dry_run:
            info("dry-run: would evict %s/%s  (%.1f MiB, %.0f days old)",
                 pkg_name, pkg_ver, size_mib, age_days)
            return True

        info("Evicting %s/%s  (%.1f MiB, %.0f days old)",
             pkg_name, pkg_ver, size_mib, age_days)
        if exists(entry.pkg_dir):
            shutil.rmtree(entry.pkg_dir, ignore_errors=True)
        # Remove sentinel; ignore errors (e.g. raced with another cleanup).
        try:
            os.unlink(entry.sentinel_path)
        except OSError:
            pass
        # Clean up empty parent directories.
        pkg_sentinel_dir = dirname(entry.sentinel_path)
        arch_sentinel_dir = dirname(pkg_sentinel_dir)
        for d in (pkg_sentinel_dir, arch_sentinel_dir):
            try:
                os.rmdir(d)
            except OSError:
                pass   # not empty — fine
        return True
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def doCleanup(args, parser):
    """Dispatch target for ``bits cleanup``."""
    work_dir    = abspath(args.workDir)
    arch        = args.architecture
    max_age     = getattr(args, "maxAgeDays", None)
    min_free_gb = getattr(args, "minFreeGb", None)
    dry_run     = getattr(args, "dryRun", False)
    disk_only   = getattr(args, "diskPressureOnly", False)

    if not exists(work_dir):
        info("workDir %s does not exist — nothing to clean", work_dir)
        sys.exit(0)

    entries = _list_sentinels(work_dir, arch)
    if not entries:
        info("No package sentinels found under %s/.packages/%s", work_dir, arch)
        sys.exit(0)

    evicted = 0
    freed   = 0

    # ── Disk-pressure eviction (LRU — oldest sentinel first) ──────────────
    if min_free_gb is not None:
        threshold = int(min_free_gb * 1024 ** 3)
        free = _free_bytes(work_dir)
        if free < threshold:
            info("Disk pressure: %.1f GiB free < %.1f GiB threshold — "
                 "evicting LRU packages", free / 1e9, min_free_gb)
            remaining = list(entries)
            for entry in remaining:
                if _free_bytes(work_dir) >= threshold:
                    break
                if _try_evict(entry, dry_run):
                    evicted += 1
                    freed += entry.size_bytes
                    entries.remove(entry)
        else:
            debug("Disk OK: %.1f GiB free (threshold %.1f GiB)",
                  free / 1e9, min_free_gb)

    # ── Age-based eviction ────────────────────────────────────────────────
    if not disk_only and max_age:  # max_age=0 or None → age-based eviction disabled
        cutoff = time.time() - max_age * 86400
        for entry in list(entries):
            if entry.mtime >= cutoff:
                break   # list is sorted oldest-first; nothing older follows
            if _try_evict(entry, dry_run):
                evicted += 1
                freed += entry.size_bytes

    if evicted:
        info("Evicted %d package(s), freed %.1f MiB%s",
             evicted, freed / 1e6, " (dry-run)" if dry_run else "")
    else:
        info("Nothing to evict%s", " (dry-run)" if dry_run else "")
