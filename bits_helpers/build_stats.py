# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Self-tuning build-resource statistics for the ``--builders`` scheduler.

When several packages build in parallel, the scheduler can avoid making the
node unresponsive if it knows roughly how much CPU and memory each package
needs, and only admits a new build when the machine still has budget for it.
That admission control already exists in :class:`bits_helpers.resource_manager.
ResourceManager`, but it requires a statistics file (``--resources``) that, in
practice, nobody produces by hand.

This module closes the loop automatically:

* :func:`aggregate_and_write` is called at the *end* of a ``--builders`` run.
  It reads the per-package resource-monitor JSON traces that
  ``--resource-monitoring`` already writes (one array of samples per package,
  see :mod:`bits_helpers.resource_monitor`) and distils them into the compact
  schema the ``ResourceManager`` consumes, written to a well-known path inside
  the work area.

* :func:`autoload_stats_path` is called at the *start* of the next run.  If a
  stats file from a previous build exists it is re-stamped with the current
  machine's CPU/RAM totals (so it stays correct even if the file was copied
  from a smaller or larger node) and its path returned, ready to be handed to
  the scheduler as ``buildStats``.

The schema produced is::

    {
      "resources": {"cpu": <ncpu*100>, "rss": <total_RAM_bytes>},
      "packages":  {"build": {"<pkg>": {"cpu": .., "rss": .., "time": ..}}},
      "known":     [],
      "defaults":  {"cpu": [..], "rss": [..], "time": [..]}
    }

``cpu`` is expressed on the same scale the monitor uses — summed
``psutil.cpu_percent`` across the process tree, i.e. ~100 per fully-busy core —
and ``rss`` in bytes, so machine totals and per-package peaks are directly
comparable.  ``defaults`` (the median of the observed packages) is used for any
package seen in a future run but absent from the file, so a brand-new heavy
package is charged a sensible cost rather than zero.
"""

import json
import multiprocessing
from os import makedirs
from os.path import join, isfile, dirname

from bits_helpers.log import debug, warning

# File written into the work area (e.g. ``sw/``) and re-read on the next run.
STATS_FILENAME = "bits_build_stats.json"


def default_stats_path(work_dir: str, arch: str = "") -> str:
    """Return the canonical stats-file path for *work_dir* / *arch*.

    Scoped per architecture (``<work_dir>/LOGS/<arch>/bits_build_stats.json``)
    so that concurrent builds of *different* platforms sharing one work area
    keep separate, correct timing histories instead of clobbering one file
    (build costs are platform-specific, so a shared file is also semantically
    wrong).
    """
    return join(work_dir, "LOGS", arch, STATS_FILENAME)


def machine_resources() -> dict:
    """Return this machine's total schedulable resources.

    ``cpu`` is ``ncpu * 100`` to match the monitor's summed-percent scale;
    ``rss`` is total physical memory in bytes (0 if psutil is unavailable).

    Host-based on purpose: in the production model one build job owns the whole
    machine and (under ``--docker``) pins every per-package container to the full
    host, so the ResourceManager budgets against the host.
    """
    cpu = (multiprocessing.cpu_count() or 1) * 100
    rss = 0
    try:
        import psutil
        rss = int(psutil.virtual_memory().total)
    except Exception as exc:  # pylint: disable=broad-except
        debug("machine_resources: could not read total RAM (%s); rss budget = 0", exc)
    return {"cpu": cpu, "rss": rss}


def _peak_from_trace(path: str):
    """Return ``{cpu, rss, time}`` peak from one monitor trace, or None."""
    try:
        with open(path) as fh:
            samples = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(samples, list) or not samples:
        return None
    cpu = max((int(s.get("cpu", 0)) for s in samples), default=0)
    rss = max((int(s.get("rss", 0)) for s in samples), default=0)
    time = max((int(s.get("time", 0)) for s in samples), default=0)
    if cpu <= 0 and rss <= 0:
        return None
    return {"cpu": cpu, "rss": rss, "time": time}


def _median(values):
    s = sorted(values)
    return s[len(s) // 2] if s else 0


def _integral_from_trace(path: str):
    """Return ``{core_seconds, time}`` for one monitor trace, or None.

    ``cpu`` in each sample is the summed process-tree percent (~100 per busy
    core), taken once per real sampling interval.  Integrating ``cpu/100`` over
    the actual spacing between samples (``diff`` of the relative ``time`` field)
    yields the *core-seconds* of useful work that package consumed — the basis
    for the whole-run CPU-utilisation estimate.
    """
    try:
        with open(path) as fh:
            samples = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(samples, list) or not samples:
        return None
    core_seconds = 0.0
    prev_t = 0
    duration = 0
    for s in samples:
        t = int(s.get("time", 0))
        cpu = int(s.get("cpu", 0))
        dt = t - prev_t
        if dt <= 0:
            dt = 1                      # defensive: keep monotonic spacing
        core_seconds += (cpu / 100.0) * dt
        prev_t = t
        duration = max(duration, t)
    return {"core_seconds": core_seconds, "time": duration}


def tuning_report(monitored: dict, wall_seconds: float, builders: int,
                  jobs: int, oversubscribe: float):
    """Estimate CPU utilisation for a finished --builders run and suggest knobs.

    Returns a dict (also embedded in the stats file and optionally printed) with
    the measured ``cpu_utilisation`` (0–1, core-seconds / (ncpu × wall)),
    ``avg_concurrency`` (mean number of packages building at once), a
    ``headroom`` flag, and a human ``recommendation``.  Returns None when there
    is not enough data (no traces, zero wall-clock).

    Heuristic: if utilisation is below target but the builder slots were mostly
    full, the limiter is per-package serial phases (configure/link/install/tar)
    → suggest a higher ``--oversubscribe`` (which raises each builder's ``-j``
    and, by design, leaves the memory budget untouched).  If the slots were
    often empty, the dependency graph is the limiter → suggest more
    ``--builders`` and/or reusing prebuilt tarballs.
    """
    builders = max(1, int(builders))
    ncpu = multiprocessing.cpu_count() or 1
    total_core_seconds = 0.0
    total_pkg_seconds = 0
    n = 0
    for pkg, script_dir in monitored.items():
        r = _integral_from_trace(join(script_dir, "%s.json" % pkg))
        if not r:
            continue
        total_core_seconds += r["core_seconds"]
        total_pkg_seconds += r["time"]
        n += 1
    if n == 0 or wall_seconds <= 0:
        return None

    try:
        oversubscribe = max(1.0, float(oversubscribe))
    except (TypeError, ValueError):
        oversubscribe = 1.0

    util = total_core_seconds / (ncpu * wall_seconds)
    avg_conc = total_pkg_seconds / wall_seconds
    busy_ratio = avg_conc / builders
    target = 0.90
    headroom = util < target

    report = {
        "ncpu": ncpu,
        "wall_seconds": round(wall_seconds, 1),
        "builders": builders,
        "jobs": int(jobs),
        "oversubscribe": round(oversubscribe, 2),
        "cpu_utilisation": round(min(util, 1.0), 3),
        "avg_concurrency": round(avg_conc, 2),
        "headroom": headroom,
    }

    if not headroom:
        report["recommendation"] = (
            "CPU averaged %.0f%% of %d cores — good utilisation; no change suggested."
            % (min(util, 1.0) * 100, ncpu)
        )
        report["suggested"] = {"builders": builders, "oversubscribe": round(oversubscribe, 2)}
        return report

    if busy_ratio >= 0.8:
        # Builder slots were busy but cores idle → per-package serial phases.
        new_ov = round(min(3.0, max(oversubscribe + 0.25,
                                    oversubscribe * target / max(util, 0.3))), 2)
        report["suggested"] = {"builders": builders, "oversubscribe": new_ov}
        report["recommendation"] = (
            "CPU averaged %.0f%% of %d cores while ~%.1f/%d builder slots were busy: "
            "per-package serial phases (configure/link/install/tar) left cores idle. "
            "Try --oversubscribe %.2f to raise each builder's -j (the memory budget is "
            "unchanged)." % (util * 100, ncpu, avg_conc, builders, new_ov)
        )
    else:
        # Slots often empty → dependency-graph / critical-path bound.
        new_b = builders + max(1, builders // 2)
        report["suggested"] = {"builders": new_b, "oversubscribe": round(oversubscribe, 2)}
        report["recommendation"] = (
            "CPU averaged %.0f%% of %d cores and only ~%.1f/%d builder slots were filled "
            "on average: the dependency graph was the limiter. Try --builders %d, and/or "
            "reuse prebuilt tarballs (remote/CVMFS store) for unchanged packages."
            % (util * 100, ncpu, avg_conc, builders, new_b)
        )
    return report


def aggregate_and_write(work_dir: str, monitored: dict, tuning: dict = None, arch: str = ""):
    """Aggregate per-package monitor traces into a stats file.

    Parameters
    ----------
    work_dir:
        The build work area; the stats file is written to
        ``default_stats_path(work_dir)``.
    monitored:
        Mapping ``{package_name: script_dir}`` where ``script_dir`` is the
        directory the resource monitor wrote ``<package_name>.json`` into.

    Returns the path written, or None when there was nothing to record.
    """
    packages = {}
    for pkg, script_dir in monitored.items():
        peak = _peak_from_trace(join(script_dir, "%s.json" % pkg))
        if peak:
            packages[pkg] = peak
    if not packages:
        debug("build_stats: no usable monitor traces; not writing stats file")
        return None

    defaults = {
        "cpu":  [_median([p["cpu"] for p in packages.values()])],
        "rss":  [_median([p["rss"] for p in packages.values()])],
        "time": [_median([p["time"] for p in packages.values()])],
    }
    stats = {
        "resources": machine_resources(),
        "packages":  {"build": packages},
        "known":     [],
        "defaults":  defaults,
    }
    if tuning:
        stats["tuning"] = tuning
    path = default_stats_path(work_dir, arch)
    try:
        makedirs(dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(stats, fh)
        debug("build_stats: wrote resource stats for %d packages to %s",
              len(packages), path)
        return path
    except OSError as exc:
        warning("build_stats: could not write stats to %s: %s", path, exc)
        return None


def autoload_stats_path(work_dir: str, arch: str = ""):
    """Return a re-stamped stats-file path for *work_dir* / *arch*, or None.

    The file's machine totals are overwritten with the *current* machine's
    resources before use, so a stats file produced on a different node is still
    safe to consume.  Returns None when no readable file exists.
    """
    path = default_stats_path(work_dir, arch)
    if not isfile(path):
        return None
    try:
        with open(path) as fh:
            stats = json.load(fh)
        if not isinstance(stats, dict) or "packages" not in stats:
            warning("build_stats: %s has unexpected shape; ignoring", path)
            return None
        stats["resources"] = machine_resources()
        with open(path, "w") as fh:
            json.dump(stats, fh)
        return path
    except (OSError, ValueError) as exc:
        warning("build_stats: ignoring unreadable stats %s: %s", path, exc)
        return None
