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


def touch_sentinel(work_dir: str, architecture: str, package: str, version: str,
                   record_size: bool = False) -> None:
    """Create or update the sentinel for *package*/*version*.

    Updates the sentinel's ``mtime`` to *now* so that the cleanup command
    counts this package as recently used.  A brief shared flock is held
    during the touch to prevent a concurrent cleanup from evicting the
    package at the exact moment we are registering it.

    When *record_size* is true (call this from the build/install path, once),
    the package's disk usage is computed and written as the sentinel's content
    so that the cleanup command can read it back instead of walking the whole
    install tree on every invocation.  Transitive "package was used" touches
    leave *record_size* false: they only bump the mtime and preserve any
    previously recorded size.

    Safe to call from multiple concurrent processes — the flock, the optional
    rewrite and the ``utime`` call are all done while holding the lock.
    """
    path = sentinel_path(work_dir, architecture, package, version)
    os.makedirs(dirname(path), exist_ok=True)
    try:
        with open(path, "a+") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                if record_size:
                    # Compute du once, here, and cache it. pkg_dir mirrors the
                    # flat <arch>/<pkg>/<ver> layout used by _list_sentinels.
                    pkg_dir = join(abspath(work_dir), architecture, package, version)
                    size_bytes = _du(pkg_dir) if exists(pkg_dir) else 0
                    fh.seek(0)
                    fh.truncate()
                    fh.write("%d\n" % size_bytes)
                    fh.flush()
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


def _read_cached_size(sentinel_file: str) -> Optional[int]:
    """Return the disk usage recorded in a sentinel, or None if unavailable.

    Tolerates empty / legacy / partially-written sentinels by returning None,
    so callers fall back to computing the size on demand.
    """
    try:
        with open(sentinel_file) as fh:
            return int(fh.readline().strip())
    except (OSError, ValueError):
        return None


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
    sentinel_path: str          # full path to the sentinel file
    pkg_dir: str                # full path to the installed package directory
    mtime: float                # sentinel mtime (seconds since epoch)
    size_bytes: Optional[int]   # cached disk usage, or None if not recorded yet


def _resolve_size(entry: "_SentinelEntry") -> int:
    """Return the package size, using the cached value or computing it on demand.

    Only ever called on the eviction path, so a cache miss (legacy sentinel)
    walks the tree for at most the handful of packages actually being evicted —
    never for the whole inventory.
    """
    if entry.size_bytes is not None:
        return entry.size_bytes
    return _du(entry.pkg_dir) if exists(entry.pkg_dir) else 0


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
            # Read the size the build recorded in the sentinel. Do NOT walk the
            # install tree here: sizes are only needed for packages we actually
            # evict, so we defer that to _resolve_size on the eviction path.
            entries.append(_SentinelEntry(
                sentinel_path=spath,
                pkg_dir=pkg_dir,
                mtime=mtime,
                size_bytes=_read_cached_size(spath),
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


def _try_evict(entry: _SentinelEntry, dry_run: bool) -> Optional[int]:
    """Attempt to evict a single package.

    Tries an exclusive non-blocking flock on the sentinel.  Returns the number
    of bytes that were (or would be, in dry-run mode) freed, or None if the
    package is currently in use by another build job or cannot be removed.
    """
    try:
        fh = open(entry.sentinel_path, "r+")
    except OSError:
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        debug("Skipping %s — held by another job", entry.pkg_dir)
        fh.close()
        return None

    # We hold the exclusive lock — safe to evict.
    try:
        pkg_name = basename(dirname(entry.sentinel_path))
        pkg_ver  = basename(entry.sentinel_path)
        age_days = (time.time() - entry.mtime) / 86400
        size_bytes = _resolve_size(entry)
        size_mib = size_bytes / 1e6
        if dry_run:
            info("dry-run: would evict %s/%s  (%.1f MiB, %.0f days old)",
                 pkg_name, pkg_ver, size_mib, age_days)
            return size_bytes

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
        return size_bytes
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
                freed_bytes = _try_evict(entry, dry_run)
                if freed_bytes is not None:
                    evicted += 1
                    freed += freed_bytes
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
            freed_bytes = _try_evict(entry, dry_run)
            if freed_bytes is not None:
                evicted += 1
                freed += freed_bytes

    if evicted:
        info("Evicted %d package(s), freed %.1f MiB%s",
             evicted, freed / 1e6, " (dry-run)" if dry_run else "")
    else:
        info("Nothing to evict%s", " (dry-run)" if dry_run else "")
