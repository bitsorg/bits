# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The Store page's history: bits-store's metrics as VictoriaMetrics keeps them
(BITS_METRICS_URL, an internal network). A fixed set of queries per time window,
so nothing from the browser reaches PromQL."""

import math
import time

import requests

# window: (its length, a bucket's) in seconds, a whole number of buckets (90d: 13 weeks)
WINDOWS = {"1h": (3600, 300), "6h": (21600, 1800), "1d": (86400, 3600), "1w": (604800, 43200),
           "30d": (2592000, 86400), "90d": (7862400, 604800), "1y": (31536000, 2628000)}

# name: (query, its labels); the levels have a point more, at the window's start. Instances
# sharing a bucket report the same content: the max of each binding's, summed.
_HELD = "sum by (format) (max by (volume, key_prefix, binding, format) (%s))"
LEVELS = {"stored_bytes": (_HELD % "bits_store_content_bytes", ("format",)),
          "stored_objects": (_HELD % "bits_store_content_objects", ("format",)),
          "git_disk_bytes": ("sum(bits_store_git_bytes)", ())}
BUCKETS = {"requests": ("sum by (format) (increase(bits_store_requests_total[{step}s]))", ("format",)),
           "fills": ("sum by (format, result) (increase(bits_store_fills_total[{step}s]))", ("format", "result"))}
WHOLE = {"p95_seconds": "histogram_quantile(0.95, sum by (le) (increase(bits_store_request_seconds_bucket[{window}s])))",
         "answered": '(sum(increase(bits_store_requests_total{{code!~"5.."}}[{window}s])) or vector(0))'
                     " / sum(increase(bits_store_requests_total[{window}s]))"}


class Unreachable(Exception):
    """VictoriaMetrics did not answer as it does."""


def _ask(session, base, path, params, timeout):
    try:
        r = session.get("%s/api/v1/%s" % (base.rstrip("/"), path), params=params, timeout=timeout,
                        allow_redirects=False)
        body = r.json()
    except (requests.RequestException, ValueError) as exc:
        raise Unreachable("VictoriaMetrics: %s" % type(exc).__name__) from None
    if r.status_code != 200 or not isinstance(body, dict) or body.get("status") != "success":
        raise Unreachable("VictoriaMetrics answered %d" % r.status_code)
    result = (body.get("data") or {}).get("result")
    if not isinstance(result, list):
        raise Unreachable("VictoriaMetrics answered without a result")
    return result


def _number(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _series(result, labels, points):
    """{label value (joined by '/'): [value per point, None where missing]}."""
    at = {t: i for i, t in enumerate(points)}
    out = {}
    for s in result:
        metric, values = s.get("metric") or {}, s.get("values") or []
        row = out.setdefault("/".join(str(metric.get(k, "")) for k in labels) or "all", [None] * len(points))
        for t, v in values:
            i = at.get(int(_number(t) or -1))
            if i is not None:
                row[i] = _number(v)
    return out


def series(base, window, now=None, timeout=(5, 30)):
    """The Store page's history over *window* (a WINDOWS key) up to now (the minute):
    the points' times, the levels at each, what each bucket ending at a point added
    (the first point's bucket is before the window), and the window's p95 latency
    and answered share. ValueError for another window."""
    if window not in WINDOWS:
        raise ValueError("window: one of %s" % ", ".join(WINDOWS))
    length, step = WINDOWS[window]
    end = int(now if now is not None else time.time()) // 60 * 60
    start = end - length
    points = list(range(start, end + 1, step))
    out = {"window": window, "step": step, "points": points}
    with requests.Session() as s:
        s.trust_env = False                     # an internal address: no proxy from the environment
        rng = lambda q: _ask(s, base, "query_range", {"query": q, "start": start, "end": end, "step": step}, timeout)
        try:
            for name, (q, labels) in LEVELS.items():
                out[name] = _series(rng(q), labels, points)
            for name, (q, labels) in BUCKETS.items():
                out[name] = _series(rng(q.format(step=step)), labels, points)
            for name, q in WHOLE.items():
                r = _ask(s, base, "query", {"query": q.format(window=length), "time": end}, timeout)
                out[name] = _number(r[0]["value"][1]) if r else None
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise Unreachable("VictoriaMetrics answered in another shape: %s" % type(exc).__name__) from None
    return out
