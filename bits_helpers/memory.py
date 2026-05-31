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
    swapping).  On macOS it sums free + inactive pages reported by
    ``vm_stat``.

    Returns 0 when detection fails so that callers can treat 0 as "unknown"
    and skip capping.
    """
    system = platform.system()
    try:
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
    free     = pages.get("Pages free", 0)
    inactive = pages.get("Pages inactive", 0)
    return (free + inactive) * page_bytes // (1024 * 1024)


# ── Main public function ──────────────────────────────────────────────────────

def effective_jobs(requested: int, spec: dict, builders: int = 1) -> int:
    """Return the number of parallel jobs to use for *spec*.

    The return value bounds two independent oversubscription axes so that the
    *whole run* stays within one machine's worth of threads and RAM no matter
    how many packages build concurrently (``--builders``):

    * **CPU / load.**  ``requested`` is divided by the number of concurrent
      builders, so the collective ``-j`` of all builders never exceeds the
      single-builder budget.  This applies to *every* recipe, capped or not.

    * **Memory.**  When the recipe declares ``mem_per_job`` the available
      memory — itself split across the concurrent builders to avoid the
      sampling race where several heavy builds start together and each reads
      the full free RAM — is divided by the per-job footprint.

    The result is::

        min(requested, requested // builders, memory_cap_per_builder)

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
    """
    builders = max(1, int(builders))
    cpu_cap = max(1, requested // builders)         # global CPU/load budget

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
