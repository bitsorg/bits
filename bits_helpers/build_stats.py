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
from os.path import join, isfile

from bits_helpers.log import debug, warning

# File written into the work area (e.g. ``sw/``) and re-read on the next run.
STATS_FILENAME = "bits_build_stats.json"


def default_stats_path(work_dir: str) -> str:
    """Return the canonical stats-file path for *work_dir*."""
    return join(work_dir, STATS_FILENAME)


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


def aggregate_and_write(work_dir: str, monitored: dict):
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
    path = default_stats_path(work_dir)
    try:
        with open(path, "w") as fh:
            json.dump(stats, fh)
        debug("build_stats: wrote resource stats for %d packages to %s",
              len(packages), path)
        return path
    except OSError as exc:
        warning("build_stats: could not write stats to %s: %s", path, exc)
        return None


def autoload_stats_path(work_dir: str):
    """Return a re-stamped stats-file path for *work_dir*, or None.

    The file's machine totals are overwritten with the *current* machine's
    resources before use, so a stats file produced on a different node is still
    safe to consume.  Returns None when no readable file exists.
    """
    path = default_stats_path(work_dir)
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
