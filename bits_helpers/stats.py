# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""``bits stats`` — human-readable build resource report.

Reads the resource data produced when monitoring is active:

* ``<work_dir>/LOGS/<arch>/bits_build_stats.json`` — per-package peaks (cpu, rss, time) plus
  the machine's total schedulable resources (written by ``build_stats.py``).
* ``<work_dir>/SPECS/<arch>/<pkg>/<ver-rev>/<pkg>.json`` — the per-package
  time-series trace (one sample per second). Used, when present, to derive the
  *average* CPU (not just the peak) and the peak thread count.

The report leads with the essentials (headline + top-offender tables) and ends
with flags that each point at a concrete action (a recipe or scheduler fix).
"""

import json
import os
from glob import glob
from os.path import join, isfile

from bits_helpers.build_stats import default_stats_path, STATS_FILENAME
from bits_helpers.log import error, info
from bits_helpers.utilities import human_bytes


# ── formatting helpers ──────────────────────────────────────────────────────

def human_time(seconds):
    s = int(seconds or 0)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return "%dh%02dm%02ds" % (h, m, sec)
    if m:
        return "%dm%02ds" % (m, sec)
    return "%ds" % sec


def cores(cpu_percent):
    """Monitor CPU is a summed percentage (100 == one fully-used core)."""
    return (cpu_percent or 0) / 100.0


# ── data loading ────────────────────────────────────────────────────────────

def load_build_stats(work_dir, arch=""):
    """Return the parsed bits_build_stats.json dict, or None.

    Stats are written per-architecture under ``<work_dir>/LOGS/<arch>/``. With
    *arch* we read that file directly; without it (the ``bits stats`` command
    does not resolve the architecture) we pick the most recently written one
    under ``LOGS/``, and still accept the legacy flat ``<work_dir>`` location.
    """
    if arch:
        path = default_stats_path(work_dir, arch)
    else:
        candidates = [p for p in glob(join(work_dir, "LOGS", "*", STATS_FILENAME))
                      if isfile(p)]
        legacy = join(work_dir, STATS_FILENAME)        # pre-relocation location
        if isfile(legacy):
            candidates.append(legacy)
        if not candidates:
            return None
        path = max(candidates, key=os.path.getmtime)
    if not isfile(path):
        return None
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def find_trace(work_dir, package):
    """Locate a package's monitor trace under SPECS/, or None."""
    matches = glob(join(work_dir, "SPECS", "*", package, "*", "%s.json" % package))
    return matches[0] if matches else None


def trace_metrics(path):
    """Derive {avg_cpu, peak_cpu, peak_rss, peak_threads, cpu_seconds, duration}
    from a monitor trace, or None when it is unusable."""
    try:
        with open(path) as fh:
            samples = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(samples, list) or not samples:
        return None

    prev_t = 0
    cpu_seconds = 0.0          # Σ cpu*dt / 100  (CPU-core-seconds)
    weighted_cpu = 0.0         # Σ cpu*dt        (for time-weighted average)
    total_dt = 0.0
    peak_cpu = peak_rss = peak_threads = 0
    mem_per_thread = 0         # worst-case rss / threads across samples
    for s in samples:
        t = int(s.get("time", prev_t))
        dt = max(t - prev_t, 0) or 1   # default 1s cadence if timestamps repeat
        prev_t = t
        c = int(s.get("cpu", 0))
        weighted_cpu += c * dt
        cpu_seconds += c * dt / 100.0
        total_dt += dt
        peak_cpu = max(peak_cpu, c)
        rss = int(s.get("rss", 0))
        peak_rss = max(peak_rss, rss)
        th = int(s.get("num_threads", 0))
        peak_threads = max(peak_threads, th)
        mem_per_thread = max(mem_per_thread, rss / max(th, 1))
    avg_cpu = (weighted_cpu / total_dt) if total_dt else 0
    return {
        "avg_cpu": avg_cpu, "peak_cpu": peak_cpu, "peak_rss": peak_rss,
        "peak_threads": peak_threads, "mem_per_thread": int(mem_per_thread),
        "cpu_seconds": cpu_seconds, "duration": prev_t,
    }


def collect(work_dir, arch=""):
    """Return (resources, [per-package metric dicts]).

    Each metric dict: package, peak_rss, peak_cpu, avg_cpu, time, cpu_seconds,
    peak_threads. Peaks come from bits_build_stats.json; avg_cpu / peak_threads /
    cpu_seconds come from the trace when available.
    """
    stats = load_build_stats(work_dir, arch)
    if not stats:
        return None, []
    resources = stats.get("resources", {}) or {}
    pkgs = (stats.get("packages", {}) or {}).get("build", {}) or {}
    out = []
    for pkg, peak in sorted(pkgs.items()):
        m = {
            "package": pkg,
            "peak_rss": int(peak.get("rss", 0)),
            "peak_cpu": int(peak.get("cpu", 0)),
            "time": int(peak.get("time", 0)),
            "avg_cpu": None, "cpu_seconds": None, "peak_threads": None,
            "mem_per_thread": None,
        }
        tr = find_trace(work_dir, pkg)
        if tr:
            tm = trace_metrics(tr)
            if tm:
                m["avg_cpu"] = tm["avg_cpu"]
                m["cpu_seconds"] = tm["cpu_seconds"]
                m["peak_threads"] = tm["peak_threads"]
                m["mem_per_thread"] = tm["mem_per_thread"]
                # Trace peaks are at least as accurate as the aggregated ones.
                m["peak_rss"] = max(m["peak_rss"], tm["peak_rss"])
                m["peak_cpu"] = max(m["peak_cpu"], tm["peak_cpu"])
                if not m["time"]:
                    m["time"] = tm["duration"]
        out.append(m)
    return resources, out


# ── analysis: flags that point at an action ─────────────────────────────────

# A build is "heavy" enough to be worth flagging only past this wall time.
HEAVY_TIME_S = 120
# Below this average core usage a heavy build is considered under-threaded.
UNDERTHREADED_CORES = 1.5
# Peak RSS above this fraction of RAM is an OOM risk under --builders.
OOM_RSS_FRACTION = 0.5


def flags(resources, metrics):
    """Return a list of (package, severity, message) actionable findings."""
    ram = int(resources.get("rss", 0))
    found = []
    for m in metrics:
        pkg = m["package"]
        # Under-threaded heavy build (needs avg_cpu from a trace).
        if (m["avg_cpu"] is not None and m["time"] >= HEAVY_TIME_S
                and cores(m["avg_cpu"]) < UNDERTHREADED_CORES):
            found.append((pkg, "warn",
                "%s ran %s using only %.1f cores on average -- the recipe is "
                "likely not building in parallel. Add `${JOBS:+-j$JOBS}` to its "
                "make/cmake --build step." % (pkg, human_time(m["time"]), cores(m["avg_cpu"]))))
        # OOM risk under parallel builds.
        if ram and m["peak_rss"] > ram * OOM_RSS_FRACTION:
            mb = max(1, m["peak_rss"] // (1024 * 1024))
            found.append((pkg, "warn",
                "%s peaked at %s (%.0f%% of %s RAM). Under --builders it can drive "
                "the machine to OOM; set `mem_per_job: %d` on the recipe so the "
                "scheduler reserves for it." % (pkg, human_bytes(m["peak_rss"]),
                100.0 * m["peak_rss"] / ram, human_bytes(ram), mb)))
    return found


# ── rendering ───────────────────────────────────────────────────────────────

def _table(rows, headers, aligns):
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(str(c)))
    def fmt(cells):
        return "  ".join(
            (str(c).rjust(widths[i]) if aligns[i] == "r" else str(c).ljust(widths[i]))
            for i, c in enumerate(cells))
    out = [fmt(headers), fmt(["-" * w for w in widths])]
    out += [fmt(r) for r in rows]
    return "\n".join(out)


def render_text(resources, metrics, top, sort_key):
    if not metrics:
        return "No resource data recorded (run a build with --resource-monitoring)."
    ram = int(resources.get("rss", 0))
    ncores = cores(int(resources.get("cpu", 0)))
    total_time = sum(m["time"] for m in metrics)
    cpu_secs = sum((m["cpu_seconds"] or 0) for m in metrics)
    heaviest = max(metrics, key=lambda m: m["peak_rss"])
    slowest = max(metrics, key=lambda m: m["time"])

    lines = []
    lines.append("Build resource summary")
    lines.append("=" * 22)
    lines.append("Packages monitored : %d" % len(metrics))
    lines.append("Machine            : %.0f cores, %s RAM" % (ncores, human_bytes(ram)))
    lines.append("Serial build time  : %s (sum of per-package wall times)" % human_time(total_time))
    if cpu_secs:
        lines.append("CPU work           : %.1f core-hours" % (cpu_secs / 3600.0))
    lines.append("Peak memory        : %s (%s)" % (human_bytes(heaviest["peak_rss"]), heaviest["package"]))
    lines.append("Longest build      : %s (%s, %.0f%% of serial time)"
                 % (human_time(slowest["time"]), slowest["package"],
                    (100.0 * slowest["time"] / total_time) if total_time else 0))
    lines.append("")

    keymap = {
        "time": lambda m: m["time"],
        "rss":  lambda m: m["peak_rss"],
        "cpu":  lambda m: m["peak_cpu"],
    }
    ordered = sorted(metrics, key=keymap.get(sort_key, keymap["time"]), reverse=True)[:top]
    rows = []
    for m in ordered:
        avg = "%.1f" % cores(m["avg_cpu"]) if m["avg_cpu"] is not None else "-"
        mpt = human_bytes(m["mem_per_thread"]) if m["mem_per_thread"] is not None else "-"
        rows.append([
            m["package"],
            human_time(m["time"]),
            human_bytes(m["peak_rss"]),
            "%.1f" % cores(m["peak_cpu"]),
            avg,
            m["peak_threads"] if m["peak_threads"] is not None else "-",
            mpt,
        ])
    lines.append("Top %d packages by %s:" % (len(rows), sort_key))
    lines.append(_table(rows,
                        ["PACKAGE", "TIME", "PEAK RSS", "PEAK CPU", "AVG CPU", "THREADS", "MEM/THR"],
                        ["l", "r", "r", "r", "r", "r", "r"]))
    lines.append("")

    findings = flags(resources, metrics)
    if findings:
        lines.append("Flags (%d):" % len(findings))
        for pkg, sev, msg in findings:
            lines.append("  [!] %s" % msg)
    else:
        lines.append("Flags: none -- no obvious memory or parallelism concerns.")
    return "\n".join(lines)


def render_package(work_dir, package):
    tr = find_trace(work_dir, package)
    if not tr:
        return "No trace found for %s under %s/SPECS." % (package, work_dir)
    tm = trace_metrics(tr)
    if not tm:
        return "Trace for %s is empty or unreadable (%s)." % (package, tr)
    lines = ["Resource detail: %s" % package, "-" * (17 + len(package))]
    lines.append("Duration     : %s" % human_time(tm["duration"]))
    lines.append("Peak RSS     : %s" % human_bytes(tm["peak_rss"]))
    lines.append("Peak CPU     : %.1f cores" % cores(tm["peak_cpu"]))
    lines.append("Average CPU  : %.1f cores" % cores(tm["avg_cpu"]))
    lines.append("CPU work     : %.2f core-minutes" % (tm["cpu_seconds"] / 60.0))
    lines.append("Peak threads : %d" % tm["peak_threads"])
    lines.append("Mem/thread   : %s (peak RSS / threads; cap JOBS or set "
                 "mem_per_job if high)" % human_bytes(tm["mem_per_thread"]))
    lines.append("Trace        : %s" % tr)
    return "\n".join(lines)


# ── entry point ─────────────────────────────────────────────────────────────

def doStats(args, parser):
    work_dir = getattr(args, "workDir", "sw")
    arch = getattr(args, "architecture", "") or ""
    as_json = getattr(args, "json_output", False)

    if getattr(args, "package", None):
        if as_json:
            tr = find_trace(work_dir, args.package)
            tm = trace_metrics(tr) if tr else None
            print(json.dumps({"package": args.package, "metrics": tm}, indent=2))
        else:
            print(render_package(work_dir, args.package))
        return

    resources, metrics = collect(work_dir, arch)
    if metrics is None or not metrics:
        if not load_build_stats(work_dir, arch):
            error("No resource stats found under %s/LOGS/. "
                  "Run a build with --resource-monitoring first.", work_dir)
            parser.exit(1)
        info("Resource stats file present but contained no package data.")
        return

    if as_json:
        print(json.dumps({
            "resources": resources,
            "packages": metrics,
            "flags": [{"package": p, "severity": s, "message": m}
                      for p, s, m in flags(resources, metrics)],
        }, indent=2))
    else:
        print(render_text(resources, metrics,
                          top=getattr(args, "top", 10),
                          sort_key=getattr(args, "sort", "time")))
