# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

# Standard library
import subprocess
from threading import Thread
from json import dump as json_dump
from time import time, sleep

# Third-party
import psutil

# Internal
from bits_helpers.cmd import monitor_progress

# Sampling interval in seconds
SAMPLE_INTERVAL = 1.0

def update_monitor_stats(proc, cpu_initialized):
    """Collect resource stats for all children of *proc*.

    *cpu_initialized* is the CALLER-OWNED set of PIDs whose psutil CPU counter
    has been primed (the first ``cpu_percent`` call always returns 0.0). It
    must be private to one monitoring thread: with ``--builders N`` there is
    one monitor thread per concurrently building package, and a shared
    module-level set (the previous design) let thread A's
    ``intersection_update`` evict the PIDs thread B had just primed — B then
    re-primed every sample and recorded ~0% CPU, silently corrupting the
    per-package stats that feed the history-driven scheduler.

    Returns a dict with cumulative CPU%, memory, thread, and FD counts, or an
    empty dict when the process has no children or has already exited.
    """
    children = []
    try:
        children = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {}

    stats = {
        "rss": 0, "vms": 0, "shared": 0, "data": 0,
        "uss": 0, "pss": 0,
        "num_fds": 0, "num_threads": 0,
        "processes": 0, "cpu": 0,
    }
    if not children:
        return stats
    stats["processes"] = len(children)

    # Step 1: Initialise CPU counters for new PIDs (first sample always returns 0).
    current_pids = set()
    for p in children:
        pid = p.pid
        current_pids.add(pid)
        if pid not in cpu_initialized:
            try:
                p.cpu_percent(interval=None)
                cpu_initialized.add(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    # Step 2: Sleep once to allow a meaningful CPU measurement window.
    sleep(SAMPLE_INTERVAL)

    # Step 3: Collect CPU%, memory, threads, and file descriptors.
    for p in children:
        try:
            stats["cpu"] += int(p.cpu_percent(interval=None))
            try:
                mem = p.memory_full_info()
                stats["uss"] += getattr(mem, "uss", 0)
                stats["pss"] += getattr(mem, "pss", 0)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                mem = p.memory_info()
            for a in ["rss", "vms", "shared", "data"]:
                stats[a] += getattr(mem, a, 0)
            stats["num_threads"] += p.num_threads()
            try:
                stats["num_fds"] += p.num_fds()
            except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
                # num_fds() is not available on Windows
                pass
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # Step 4: Remove PIDs that have exited since Step 1.
    cpu_initialized.intersection_update(current_pids)
    return stats


def monitor_stats(p_id, stats_file_name):
    """Periodically sample resource usage of process *p_id* until it exits.

    Results are written as a JSON array to *stats_file_name*.
    """
    stime = int(time())
    p = psutil.Process(p_id)
    data = []
    cpu_initialized = set()      # thread-local: see update_monitor_stats
    while p.is_running():
        stats = update_monitor_stats(p, cpu_initialized)
        if not stats:
            sleep(SAMPLE_INTERVAL)
            continue
        stats["time"] = int(time() - stime)
        data.append(stats)
    with open(stats_file_name, "w") as sf:
        json_dump(data, sf)


def run_monitor_on_command(command, stats_file_name, printer, timeout=None):
    """Run *command* in a subprocess while recording its resource usage.

    Launches a monitoring thread that writes periodic resource snapshots to
    *stats_file_name* (JSON array).  Returns the command's exit code.
    """
    popen = subprocess.Popen(
        command, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        close_fds=True,
    )
    mon_thd = Thread(target=monitor_stats, args=(popen.pid, stats_file_name))
    mon_thd.start()
    returncode = monitor_progress(popen, printer, timeout)
    mon_thd.join()  # wait for the monitoring thread to flush its output
    return returncode
