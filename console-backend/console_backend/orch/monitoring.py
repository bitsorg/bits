# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Monitoring tab's snapshot as push-agent (bits-console/monitoring) writes it
beside VictoriaMetrics (BITS_MONITORING_SNAPSHOT): whole for the admins and
developers of the community asking, its overview for any other signed-in user:
the build hosts unnamed, no host's history (the publishers keep their names, which
the console's config lists anyway)."""

import copy
import json
import time

MAX_AGE_S = 600        # an older one: push-agent has stopped
PER_HOST = ("load_hist", "disk_hist", "mem_hist", "prepub_load_hist", "prepub_mem_hist",
            "prepub_disk_hist")
PUBLISHERS = ("prepub", "prepub_testbed")   # the snapshot's keys, one per scrape role


def read(path, now=None):
    """The snapshot at *path*, or None when there is none or it is too old."""
    try:
        with open(path) as f:
            snap = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(snap, dict) or not isinstance(snap.get("ts"), (int, float)):
        return None
    return snap if (now if now is not None else time.time()) - snap["ts"] <= MAX_AGE_S else None


def overview(snap):
    """*snap* with its build hosts unnamed and no per-host history."""
    out = copy.deepcopy(snap)
    hosts = (out.get("current") or {}).get("hosts")
    for i, h in enumerate(hosts if isinstance(hosts, list) else [], 1):
        if isinstance(h, dict):
            h["instance"] = "host %d" % i
    for window in (out.get("series") or {}).values():
        if not isinstance(window, dict):
            continue
        for part in [window] + [window.get(k) for k in PUBLISHERS[1:]]:   # other roles nest theirs
            if isinstance(part, dict):
                for k in PER_HOST:
                    part.pop(k, None)
    return out
