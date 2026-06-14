"""
Memory-aware parallel-job capping.

When a recipe sets ``mem_per_job`` bits will query the current available
system memory and lower ``$JOBS`` so that the total memory committed by the
build never exceeds what is physically available.  This prevents the kernel
from swapping on memory-hungry compilers such as LLVM or ROOT.

Recipe fields
-------------
mem_per_job : int or str
    Expected peak RSS per parallel compilation process.  Accepts a plain
    integer (interpreted as MiB) or a string with an optional unit suffix:
    ``512``, ``"1500"``, ``"1.5 GiB"``, ``"2 GB"``.

mem_utilisation : float  (default 0.9)
    Fraction of the detected available memory that bits is allowed to commit,
    in the range 0.0–1.0.  Lowering this gives more headroom for the OS and
    other processes.  Only used when ``mem_per_job`` is also set.

Examples
--------
::

    # LLVM — each clang process can peak at ~2 GiB with LTO
    mem_per_job: 2048

    # ROOT — template-heavy; be more conservative on shared build hosts
    mem_per_job: 1500
    mem_utilisation: 0.80

    # zlib — tiny; omit the field entirely and $JOBS is used as-is
"""

import math
import platform
import re
import subprocess

from bits_helpers.log import debug, warning

# ── Unit table: suffix → multiplier relative to MiB ──────────────────────────
_UNIT_MiB = {
    "":    1,
    "m":   1,
    "mb":  1,
    "mib": 1,
    "g":   1024,
    "gb":  1024,
    "gib": 1024,
    "t":   1024 * 1024,
    "tb":  1024 * 1024,
    "tib": 1024 * 1024,
}


def parse_memory(value) -> int:
    """Parse a memory value and return the result in MiB.

    Accepts:
    - An integer or float (treated as MiB).
    - A string like ``"512"``, ``"1.5 GiB"``, ``"2GB"``, ``"2048 MB"``.

    Raises ``ValueError`` for unrecognised formats.
    """
    if isinstance(value, (int, float)):
        result = int(value)
        if result <= 0:
            raise ValueError("mem_per_job must be a positive number, got %r" % value)
        return result

    text = str(value).strip()
    m = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]*)", text)
    if not m:
        raise ValueError("Cannot parse memory value %r" % value)

    number = float(m.group(1))
    unit   = m.group(2).lower()
    if unit not in _UNIT_MiB:
        raise ValueError(
            "Unknown memory unit %r in %r.  "
            "Supported units: MiB, GiB, MB, GB (and lower-case variants)." % (unit, value)
        )
    result = int(number * _UNIT_MiB[unit])
    if result <= 0:
        raise ValueError("mem_per_job must be a positive number, got %r" % value)
    return result


def available_memory_mib() -> int:
    """Return a conservative estimate of *currently available* memory in MiB.

    On Linux this is ``MemAvailable`` from ``/proc/meminfo`` (the kernel's own
    estimate of how much memory can be given to a new workload without
    swapping).  On macOS it sums the reclaimable ``vm_stat`` buckets (free +
    inactive + speculative + purgeable).  When the optional ``psutil`` package
    is importable, its vetted cross-platform reading is preferred over both.

    Returns 0 when detection fails so that callers can treat 0 as "unknown"
    and skip capping.
    """
    system = platform.system()
    try:
        # Prefer psutil's vetted reading when the optional dependency is present:
        # it is more accurate than the per-OS heuristics below — notably on macOS,
        # where vm_stat's overlapping page buckets make an exact "available" hard.
        try:
            import psutil
            avail = int(psutil.virtual_memory().available) // (1024 * 1024)
            if avail > 0:
                return avail
        except Exception:  # pylint: disable=broad-except
            pass  # psutil absent/failed → fall back to per-OS detection below
        if system == "Linux":
            return _available_linux()
        elif system == "Darwin":
            return _available_darwin()
        else:
            debug("available_memory_mib: unsupported platform %r, skipping cap", system)
            return 0
    except Exception as exc:  # pylint: disable=broad-except
        warning("Could not detect available memory (%s); $JOBS will not be capped.", exc)
        return 0


def _available_linux() -> int:
    with open("/proc/meminfo") as fh:
        info = {}
        for line in fh:
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])
    # MemAvailable is present on Linux 3.14+; fall back to MemFree
    kib = info.get("MemAvailable") or info.get("MemFree", 0)
    return kib // 1024


def _available_darwin() -> int:
    """macOS estimate of memory available to a new workload, in MiB.

    Mirrors the Linux ``MemAvailable`` semantics: file-backed cache, purgeable
    and speculative pages are *reclaimable* and therefore count as available.
    macOS keeps "Pages free" tiny (most idle RAM is reclaimable cache), so the
    old ``free + inactive`` sum drastically underreported available memory and
    throttled heavy builds (e.g. ROOT to ``-j2`` on a machine that runs
    ``-j10`` fine).

    Estimate = the reclaimable ``vm_stat`` buckets (free + inactive +
    speculative + purgeable).  An earlier version used
    ``physical − (anonymous + wired + compressed)``, but "Anonymous pages"
    includes *inactive* anonymous pages, which the kernel reclaims by
    compressing/swapping — so subtracting all of them under-reported available
    memory and throttled heavy builds (ROOT to -j2 on an idle 24 GB Mac).
    ``psutil``, when importable, is preferred over this by the caller.
    """
    out = subprocess.check_output(["vm_stat"], text=True)
    pages = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            try:
                pages[key.strip()] = int(val.strip().rstrip("."))
            except ValueError:
                pass
    page_bytes = 4096
    try:
        page_bytes = int(
            subprocess.check_output(
                ["sysctl", "-n", "hw.pagesize"], text=True
            ).strip()
        )
    except Exception:  # pylint: disable=broad-except
        pass

    # Available ≈ the reclaimable pages the kernel can hand to a new workload
    # without OOM/swap: free + inactive + speculative + purgeable. We do NOT
    # add "File-backed pages": those overlap "Pages inactive" (inactive file
    # pages are counted in both buckets), so adding them would double-count.
    reclaimable = sum(pages.get(k, 0) for k in (
        "Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"))
    return reclaimable * page_bytes // (1024 * 1024)


# ── Main public function ──────────────────────────────────────────────────────

def effective_jobs(requested: int, spec: dict, builders: int = 1,
                   oversubscribe: float = 1.0) -> int:
    """Return the number of parallel jobs to use for *spec*.

    The return value bounds two independent oversubscription axes so that the
    *whole run* stays within one machine's worth of threads and RAM no matter
    how many packages build concurrently (``--builders``):

    * **CPU / load.**  ``requested`` is divided by the number of concurrent
      builders, so the collective ``-j`` of all builders stays near the
      single-builder budget.  An *oversubscribe* factor (>= 1.0) multiplies the
      per-builder share before the split so that idle builder slots — a deep
      dependency tree rarely keeps all ``--builders`` busy at once — do not
      leave cores unused.  The per-package value is still clamped to
      ``requested`` (``min`` below), so a single-builder build is unaffected and
      no one ``make`` ever runs more than one machine's worth of threads; the
      mild overshoot when several builders *are* busy is absorbed by the OS
      scheduler and the nice ladder.  This applies to *every* recipe.

    * **Memory.**  When the recipe declares ``mem_per_job`` the available
      memory — split across the ``--builders`` *maximum* (NOT scaled by
      *oversubscribe*) to avoid the sampling race where several heavy builds
      start together and each reads the full free RAM — is divided by the
      per-job footprint.  Memory stays conservative on purpose: CPU
      oversubscription degrades gracefully, memory oversubscription means
      OOM/swap, so the memory cap remains authoritative.

    The result is::

        min(requested, ceil(requested * oversubscribe / builders), memory_cap)

    Always returns at least 1 so the build is never completely stalled.

    Parameters
    ----------
    requested:
        The ``-j N`` value (or CPU count) chosen by the user / scheduler.
    spec:
        The package spec dict as returned by ``getPackageList``.
    builders:
        The number of packages building in parallel (``--builders``).  The CPU
        and memory budgets are split across this many concurrent builders.
        Defaults to 1 (single-builder behaviour, unchanged).
    oversubscribe:
        Factor (>= 1.0) applied to the per-builder CPU share only.  1.0 (the
        default) keeps the previous behaviour exactly.  Has no effect on the
        memory cap, nor on single-builder builds (the ``min(requested, …)``
        clamp absorbs it).
    """
    builders = max(1, int(builders))
    try:
        oversubscribe = float(oversubscribe)
    except (TypeError, ValueError):
        oversubscribe = 1.0
    oversubscribe = max(1.0, oversubscribe)
    # CPU/load budget: per-builder share, optionally oversubscribed, ceil'd so
    # the integer split does not waste the remainder. Clamped to `requested`
    # below so a lone build never exceeds one machine's worth of threads.
    cpu_cap = max(1, math.ceil(requested * oversubscribe / builders))

    raw = spec.get("mem_per_job")
    if raw is None:
        return min(requested, cpu_cap)              # no mem hint → CPU cap only

    try:
        mem_per_job = parse_memory(raw)
    except ValueError as exc:
        warning("Ignoring invalid mem_per_job for %r: %s", spec.get("package", "?"), exc)
        return min(requested, cpu_cap)

    utilisation = float(spec.get("mem_utilisation", 0.9))
    if not (0.0 < utilisation <= 1.0):
        warning(
            "mem_utilisation for %r is %s, which is outside (0, 1]; "
            "using default 0.9.",
            spec.get("package", "?"), utilisation,
        )
        utilisation = 0.9

    avail = available_memory_mib()
    if avail <= 0:
        return min(requested, cpu_cap)              # detection failed → CPU cap only

    # Split the RAM budget across the concurrent builders so that builds
    # starting in the same scheduling tick do not each claim the whole machine.
    memory_cap = max(1, int((avail / builders) * utilisation / mem_per_job))
    jobs = min(requested, cpu_cap, memory_cap)

    if jobs < requested:
        debug(
            "Package %r: capping $JOBS %d → %d "
            "(%d MiB available, %d builders, %d MiB/job, %.0f%% utilisation)",
            spec.get("package", "?"), requested, jobs,
            avail, builders, mem_per_job, utilisation * 100,
        )
    return jobs
