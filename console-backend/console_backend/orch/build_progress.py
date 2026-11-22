# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Running builds' progress, as their build jobs push it to VictoriaMetrics with
--monitor (bits_build_packages_*, by pipeline and CI job; BITS_METRICS_URL)."""

import requests

from console_backend import store_series

RECENT = "1h"          # a job silent longer than this has no progress shown
NAMES = "bits_build_packages_(started|total|ended)|bits_build_package_started"


def _rows(session, base, query, timeout):
    # nocache: else VictoriaMetrics answers as of 30 s ago (-search.latencyOffset)
    return store_series._ask(session, base, "query", {"query": query, "nocache": "1"}, timeout)


def for_pipelines(base, pipelines, timeout=(2, 5)):
    """{pipeline: {"started", "total", "package", "ended", "at" (unix time)}} for
    *pipelines* (ints): per pipeline, its CI job that pushed last. Unreachable on a failure."""
    ids = sorted({int(p) for p in pipelines})
    if not ids:
        return {}
    sel = 'pipeline=~"%s"' % "|".join(map(str, ids))
    jobs = {}
    with requests.Session() as s:
        s.trust_env = False                    # an internal address: no proxy from the environment
        try:
            for r in _rows(s, base, "tlast_over_time(bits_build_packages_started{%s}[%s])"
                           % (sel, RECENT), timeout):
                jobs.setdefault((r["metric"]["pipeline"], r["metric"].get("ci_job", "")),
                                {})["at"] = store_series._number(r["value"][1])
            for r in _rows(s, base, 'last_over_time({__name__=~"%s",%s}[%s])' % (NAMES, sel, RECENT),
                           timeout):
                m, value = r["metric"], store_series._number(r["value"][1])
                job = jobs.get((m["pipeline"], m.get("ci_job", "")))
                if job is None or value is None:
                    continue
                name = m["__name__"][len("bits_build_"):]
                if name != "package_started":
                    job[name[len("packages_"):]] = value
                elif value >= job.get("_place", -1):
                    job["_place"], job["package"] = value, m.get("package", "")[:100]
            out = {}
            for (pipeline, _), job in jobs.items():
                if None in (job.get("at"), job.get("started")) or not job.get("total"):
                    continue
                p = int(pipeline)
                if p not in out or job["at"] > out[p]["at"]:
                    out[p] = {"started": int(job["started"]), "total": int(job["total"]),
                              "package": job.get("package", ""), "ended": bool(job.get("ended")),
                              "at": int(job["at"])}
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            raise store_series.Unreachable("VictoriaMetrics answered in another shape: %s"
                                           % type(exc).__name__) from None
    return out
