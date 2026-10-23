# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A build's CycloneDX SBOM as an HTML report, drawn by CycloneDX Sunshine
(vendor/sunshine, unmodified): components, dependencies and licences.

Sunshine runs as a separate process, a few at a time and under a time limit;
a request that finds every slot busy is refused rather than queued, so reports
cannot tie up the backend. Reports are kept in memory by the sha256 of the SBOM
and the name they show."""

import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import OrderedDict

SUNSHINE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "vendor", "sunshine", "sunshine.py")
TIMEOUT_S = 120
WAIT_S = 5          # for a free slot
PARALLEL = 2
KEEP = 16

_slots = threading.BoundedSemaphore(PARALLEL)
_lock = threading.Lock()
_cache = OrderedDict()


class Failed(Exception):
    """No report: Sunshine failed or timed out."""


class Busy(Failed):
    """No report now: every slot stayed busy."""


def _file_name(name):
    # The report shows its input's file name: the build id, made path-safe.
    return (re.sub(r"[^A-Za-z0-9._-]", "_", name or "")[:120] or "sbom") + ".cdx.json"


def _run(sbom, name):
    with tempfile.TemporaryDirectory(prefix="sbom-report-") as d:
        src, out = os.path.join(d, _file_name(name)), os.path.join(d, "report.html")
        with open(src, "wb") as f:
            f.write(sbom)
        try:
            p = subprocess.run([sys.executable, "-I", SUNSHINE, "-nl", "-i", src, "-o", out],
                               cwd=d, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            raise Failed("the report took longer than %d s" % TIMEOUT_S) from None
        # Sunshine exits 0 on a parse error too, without writing the report.
        if p.returncode != 0 or not os.path.isfile(out):
            said = [x for x in (p.stdout + p.stderr).splitlines() if x.strip()]
            raise Failed(said[-1][:300] if said else "no report (exit %d)" % p.returncode)
        with open(out, "rb") as f:
            return f.read()


def _cached(key):
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    return None


def render(sbom, name=""):
    """The HTML report (bytes) of the CycloneDX document *sbom* (bytes)."""
    key = (hashlib.sha256(sbom).hexdigest(), name)
    page = _cached(key)
    if page is not None:
        return page
    if not _slots.acquire(timeout=WAIT_S):
        raise Busy("every report slot is busy; try again shortly")
    try:
        page = _cached(key)          # drawn while this request waited
        if page is not None:
            return page
        page = _run(sbom, name)
    finally:
        _slots.release()
    with _lock:
        _cache[key] = page
        while len(_cache) > KEEP:
            _cache.popitem(last=False)
    return page
