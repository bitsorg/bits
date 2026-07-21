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
# Retention sweep (--retain)
# ---------------------------------------------------------------------------
#
# Policy, per architecture (mirrors `bits gc`'s manifest-rooted, fail-closed
# design, but sweeps the LOCAL workDir instead of the S3 store):
#
#   KEEP
#     * every package of the newest --keep-builds local build manifests
#       (MANIFESTS/bits-manifest-*.json) — "the last builds";
#     * every package of each VERIFIED signed common manifest given via
#       --trust-manifest — the groups' last certified sets (entries carry
#       their group). A manifest that does not verify ABORTS the sweep.
#     For each kept package BOTH artefacts stay, so it remains re-publishable
#     and uploadable to CVMFS without a rebuild:
#       - install tree   <arch>/[<family>/]<pkg>/<ver>-<rev>/   (CVMFS publish)
#       - store tarball  TARS/<arch>/store/<h2>/<hash>/         (S3 re-publish)
#
#   EVICT (older than --grace-days)
#     * install trees (identified by their .build-hash marker) not kept;
#     * TARS/<arch>/store/<h2>/<hash>/ dirs whose hash is not kept;
#     * BUILD/<hash> dirs whose hash is not kept;
#     * dangling symlinks under TARS/ and BUILD/;
#     * sentinels whose install dir is gone.

_NON_ARCH_DIRS = {"TARS", "BUILD", "TMP", "INSTALLROOT", "SOURCES", "SPECS",
                  "MIRROR", "REPOS", "MANIFESTS", "LOGS", "MODULES",
                  "wrapper-scripts"}
_HEX_HASH_RE = None   # compiled lazily (re imported locally)


def _entry_paths(work_dir, e):
    """(install_dir, content_hash) for a manifest package entry."""
    arch = str(e.get("effective_architecture") or "")
    pkg  = str(e.get("package") or "")
    if not arch or not pkg:
        return None, None
    ver = str(e.get("version") or "")
    rev = str(e.get("revision") or "")
    verrev = "%s-%s" % (ver, rev) if rev else ver
    fam = str(e.get("pkg_family") or "")
    parts = [work_dir, arch] + ([fam] if fam else []) + [pkg, verrev]
    return join(*parts), str(e.get("hash") or "")


def _local_build_manifests(work_dir, keep_builds):
    """Newest *keep_builds* build manifests per top-level architecture."""
    import glob
    import json
    files = [f for f in glob.glob(join(work_dir, "MANIFESTS",
                                       "bits-manifest-*.json"))
             if not os.path.islink(f)]
    files.sort(key=os.path.getmtime, reverse=True)
    kept, per_arch = [], {}
    for f in files:
        try:
            with open(f) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        arch = str(data.get("architecture") or "?")
        if per_arch.get(arch, 0) >= keep_builds:
            continue
        per_arch[arch] = per_arch.get(arch, 0) + 1
        kept.append((f, data))
    return kept


def _iter_install_dirs(archdir, max_depth=3):
    """Yield install trees under an architecture dir, identified by their
    .build-hash marker (never descending into one). Symlinks (latest, CVMFS
    indirections) are never followed."""
    base_depth = archdir.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(archdir):
        dirs[:] = [d for d in dirs if not os.path.islink(join(root, d))]
        if ".build-hash" in files:
            dirs[:] = []
            yield root
            continue
        if root.count(os.sep) - base_depth >= max_depth:
            dirs[:] = []


def _unlink_quiet(p):
    try:
        os.unlink(p)
    except OSError:
        pass


def _prune_empty_dirs(top):
    # Re-check emptiness with listdir: walk's dirs snapshot predates the rmdir
    # of children, so relying on it leaves the parents of pruned dirs behind.
    for root, _dirs, _files in os.walk(top, topdown=False):
        if root == top:
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass


# ── CVMFS publish record ────────────────────────────────────────────────────
# CVMFS publication is asynchronous (prepub service), so the build node learns
# about it from the console's cvmfs-status.json publish record. Markers under
# workDir/.published/<arch>/<pkg>/<ver>-<rev> persist that knowledge locally:
# a certified package is only considered "released" (and thus evictable) once
# its marker exists.

def published_marker_path(work_dir, arch, pkg, verrev):
    return join(work_dir, ".published", arch, pkg, verrev)


def record_published(work_dir, arch, pkg, verrev):
    """Persist "this package build is on CVMFS" (idempotent)."""
    p = published_marker_path(work_dir, arch, pkg, verrev)
    os.makedirs(dirname(p), exist_ok=True)
    with open(p, "a"):
        pass


def mark_published_from(work_dir, src):
    """Backfill publish markers from a cvmfs-status.json record (path/URL).

    Record entries carry package/version/platform plus a pkg_id of the form
    ``<package>-<version>-<revision>-<platform>`` — the revision is recovered
    from it so markers are revision-precise. Returns the number recorded.
    """
    import json
    path = src
    if "://" in str(src):
        from bits_helpers.download import downloadUrllib2
        dest = join(work_dir, "MANIFESTS", "retention")
        os.makedirs(dest, exist_ok=True)
        downloadUrllib2(src, dest, work_dir, dest_filename="cvmfs-status.json")
        path = join(dest, "cvmfs-status.json")
    with open(path) as fh:
        data = json.load(fh)
    n = 0
    for e in data.get("packages") or []:
        pkg  = str(e.get("package") or "")
        ver  = str(e.get("version") or "")
        plat = str(e.get("platform") or "")
        if not (pkg and ver and plat):
            continue
        pid  = str(e.get("pkg_id") or "")
        head, tail = "%s-%s-" % (pkg, ver), "-%s" % plat
        rev = pid[len(head):-len(tail)] \
            if pid.startswith(head) and pid.endswith(tail) else ""
        record_published(work_dir, plat, pkg,
                         "%s-%s" % (ver, rev) if rev else ver)
        n += 1
    info("retention: %d CVMFS publish marker(s) recorded from %s", n, src)
    return n


def _on_disk_arches(work_dir):
    """Architecture names present in the workDir (install trees and TARS)."""
    arches = set()
    for d in (os.listdir(work_dir) if exists(work_dir) else []):
        if d not in _NON_ARCH_DIRS and not d.startswith(".") \
           and os.path.isdir(join(work_dir, d)):
            arches.add(d)
    tars = join(work_dir, "TARS")
    for d in (os.listdir(tars) if exists(tars) else []):
        if os.path.isdir(join(tars, d)):
            arches.add(d)
    return arches


def retention_sweep(args, parser):
    """Manifest-rooted retention for the local workDir. Returns exit code.

    Policy (all architectures found in the workDir):
      * released content — uploaded to the store, in the arch's VERIFIED
        signed manifest AND marked published to CVMFS — is evicted (it is
        safe upstream and can be fetched back);
      * certified-but-not-yet-published content is kept (still needed to
        publish);
      * the newest --keep-builds build manifests' packages are kept unless
        released — this preserves the latest (possibly failed) iterations;
      * anything younger than --grace-days is never touched;
      * per-arch fail-closed: an architecture whose signed manifest cannot
        be fetched/verified is skipped entirely.
    """
    import re
    from bits_helpers import trust
    from bits_helpers.build import derive_trust_manifest_srcs
    from bits_helpers.gc import _localize_manifest

    work_dir = abspath(args.workDir)
    dry_run  = getattr(args, "dryRun", False)
    grace_s  = max(0.0, getattr(args, "graceDays", 1.0)) * 86400
    keep_n   = max(1, getattr(args, "keepBuilds", 2))
    now      = time.time()
    hex_re   = re.compile(r"^[0-9a-f]{40,64}$")

    # 0. Publish record → markers.
    src = getattr(args, "markPublishedFrom", None)
    if src:
        try:
            mark_published_from(work_dir, src)
        except Exception as exc:            # noqa: BLE001 — record only
            warning("retention: could not read publish record %s (%s) — "
                    "continuing; unpublished-looking content stays kept",
                    src, exc)

    def _is_published(e):
        install, _h = _entry_paths(work_dir, e)
        if not install:
            return False
        arch = str(e.get("effective_architecture") or "")
        return exists(published_marker_path(
            work_dir, arch, str(e.get("package")), basename(install)))

    # 1. Certified sets, per architecture found on disk (plus explicit ones).
    #    Fail-closed PER ARCH: only arches with a verified manifest are swept.
    arches = _on_disk_arches(work_dir)
    sources = {}                       # src -> arch it certifies ('' = explicit)
    store = str(getattr(args, "retainStore", None) or "")
    if store:
        base = store.split("::", 1)[0] if store.startswith(("http://", "https://")) else store
        for arch in sorted(arches):
            for s in derive_trust_manifest_srcs(base, None, arch):
                sources.setdefault(s, arch if ("-%s.json" % arch) in s else "shared")
    for s in (getattr(args, "trustManifests", None) or []):
        sources.setdefault(s, "")

    verified_arches = set()
    certified = []                     # verified entries across all manifests
    for s, arch_hint in sources.items():
        try:
            path = _localize_manifest(s, work_dir)
            kid, entries = trust.trusted_records(path)
        except Exception as exc:        # noqa: BLE001
            kid, entries = None, []
            debug("retention: %s: %s", s, exc)
        if not kid:
            warning("retention: %s not available/verified — architecture "
                    "'%s' will NOT be swept", s, arch_hint or "?")
            continue
        certified.extend(entries)
        verified_arches.update({str(e.get("effective_architecture") or "")
                                for e in entries})
        if arch_hint:
            verified_arches.add(arch_hint)
    if not sources:
        warning("retention: no --store/--trust-manifest — no architecture "
                "has a verified certified set, nothing will be swept")

    keep_paths, keep_hashes, released_n = set(), set(), 0

    def _keep(e):
        install, h = _entry_paths(work_dir, e)
        if install:
            keep_paths.add(os.path.normpath(install))
        if h:
            keep_hashes.add(h)

    # 2. Certified but not yet on CVMFS → keep (needed to publish).
    #    Certified AND published → evictable (safe upstream).
    for e in certified:
        if _is_published(e):
            released_n += 1
        else:
            _keep(e)

    # 3. The last builds (newest N local manifests per arch): keep their
    #    packages unless released — the latest iterations, failed ones
    #    included, are exactly what makes the next attempt fast.
    manifests = _local_build_manifests(work_dir, keep_n)
    released_keys = {os.path.normpath(_entry_paths(work_dir, e)[0])
                     for e in certified if _is_published(e)
                     and _entry_paths(work_dir, e)[0]}
    for _f, data in manifests:
        for e in data.get("packages") or []:
            install, _h = _entry_paths(work_dir, e)
            if install and os.path.normpath(install) in released_keys:
                continue
            _keep(e)
    info("retention: arches on disk: %s | verified: %s | certified: %d "
         "(released: %d) | last-build manifests: %d",
         ", ".join(sorted(arches)) or "-",
         ", ".join(sorted(a for a in verified_arches if a)) or "-",
         len(certified), released_n, len(manifests))

    if not verified_arches:
        error("retention: no architecture has a verified certified set — "
              "refusing to sweep")
        return 1

    stats = {"evicted": 0, "freed": 0}

    def _young(path):
        try:
            return (now - os.path.getmtime(path)) < grace_s
        except OSError:
            return True

    def _rm(path, what):
        size = _du(path)
        if dry_run:
            info("dry-run: would evict %s %s (%.1f MiB)", what, path, size / 1e6)
        else:
            info("evicting %s %s (%.1f MiB)", what, path, size / 1e6)
            shutil.rmtree(path, ignore_errors=True)
        stats["evicted"] += 1
        stats["freed"]   += size

    # 4. Install trees — ALL architectures with a verified certified set.
    for arch in sorted(arches):
        if arch not in verified_arches:
            continue                    # per-arch fail-closed
        archdir = join(work_dir, arch)
        if not os.path.isdir(archdir) or os.path.islink(archdir):
            continue
        for root in list(_iter_install_dirs(archdir)):
            if os.path.normpath(root) in keep_paths or _young(root):
                continue
            _rm(root, "install")
        if not dry_run:
            _prune_empty_dirs(archdir)

    # 5. Store tarball dirs, by content hash (defense in depth: only
    #    well-formed <h2>/<hash> paths are ever considered).
    tars = join(work_dir, "TARS")
    for arch in sorted(os.listdir(tars) if exists(tars) else []):
        if arch not in verified_arches:
            continue                    # per-arch fail-closed
        store = join(tars, arch, "store")
        if not os.path.isdir(store):
            continue
        for shard in sorted(os.listdir(store)):
            sh = join(store, shard)
            if not os.path.isdir(sh):
                continue
            for h in sorted(os.listdir(sh)):
                d = join(sh, h)
                if (not hex_re.match(h) or h[:2] != shard
                        or not os.path.isdir(d) or h in keep_hashes
                        or _young(d)):
                    continue
                _rm(d, "store tarball")
        if not dry_run:
            _prune_empty_dirs(store)

    # 6. BUILD dirs by hash; dangling *-latest links.
    bdir = join(work_dir, "BUILD")
    for name in sorted(os.listdir(bdir) if exists(bdir) else []):
        p = join(bdir, name)
        if os.path.islink(p):
            if not exists(p):
                _unlink_quiet(p)
            continue
        if (hex_re.match(name) and name not in keep_hashes
                and os.path.isdir(p) and not _young(p)):
            _rm(p, "build dir")

    # 7. Dangling symlinks under TARS (version links, dist-link trees).
    for root, dirs, files in os.walk(tars) if exists(tars) else []:
        if os.sep + "store" in root:
            dirs[:] = []
            continue
        for n in files + dirs:
            p = join(root, n)
            if os.path.islink(p) and not exists(p):
                _unlink_quiet(p)

    # 8. Sentinels whose install dir is gone.
    pkroot = join(work_dir, ".packages")
    if exists(pkroot):
        for root, _dirs, files in os.walk(pkroot):
            for n in files:
                s = join(root, n)
                rel = os.path.relpath(s, pkroot)   # <arch>/<pkg>/<version>
                if not exists(join(work_dir, rel)):
                    _unlink_quiet(s)
        if not dry_run:
            _prune_empty_dirs(pkroot)

    info("retention: %s%d item(s), %.2f GiB — kept %d install tree(s), "
         "%d content hash(es)",
         "dry-run — would evict " if dry_run else "evicted ",
         stats["evicted"], stats["freed"] / 1e9,
         len(keep_paths), len(keep_hashes))
    return 0


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

    # Manifest-rooted retention mode: standalone sweep, then exit.
    if getattr(args, "retain", False):
        sys.exit(retention_sweep(args, parser))

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
