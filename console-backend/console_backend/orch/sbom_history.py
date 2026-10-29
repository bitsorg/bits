# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Risk over time per release: what the Vulnerabilities page charts.

A release is a community's top-level package (the pipeline's PACKAGE, else
the first package the build asked for): its compiler and build-type variants
are built from the same sources, so its newest scanned build stands for it (of
two published on one day, the higher build id). Its open findings (not
triaged, in shipped packages) count by severity, and their risk is grype's (by
severity alone where the scan has none, as in the upgrade plans).

Once a day the worker records each release's counts and risk (sbom_history),
so a build grows riskier as new vulnerabilities are published. A day with no
record but a build of the release (before the first record, or the worker
down) gets that build's counts as they are when it is recorded, BACKFILL
builds a pass. Today's point is computed afresh."""

import datetime
import json

from . import db, sbom_scan, vuln_triage
from .sbom_plan import SEVERITY_RISK
from .sbom_scan import FINDINGS_INDEX, SEVERITIES

DAY_KEY = "sbom.history.day"       # kv: the day whose records are complete
BACKFILL = 50                      # earlier builds recorded in one pass, at most


def _day(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d")


def _builds(conn, communities=None):
    """The indexed builds with a good scan: {build_id: (community, top, day)};
    a build of several pipelines is of the newest one's release, as
    sbom_scan.listing shows it, and of the day it was first published."""
    out, pid = {}, {}
    for r in conn.execute(
            "SELECT b.pipeline_id, b.build_id, b.community, b.top_level, b.published_at,"
            " b.recorded_ms, p.package FROM builds b JOIN sbom_scans s ON s.build_id = b.build_id"
            " LEFT JOIN pipelines p ON p.id = b.pipeline_id"
            " WHERE s.index_version >= ? AND s.db_built != ''", (FINDINGS_INDEX,)):
        bid = r["build_id"]
        day = (r["published_at"] or "")[:10] or _day(r["recorded_ms"])
        if bid in out and r["pipeline_id"] < pid[bid]:
            out[bid] = out[bid][:2] + (min(out[bid][2], day),)
            continue
        try:
            asked = json.loads(r["top_level"] or "[]")
        except ValueError:
            asked = []
        top = r["package"] or (str(asked[0]) if asked else "") or bid
        out[bid] = (r["community"], top, min(day, out[bid][2]) if bid in out else day)
        pid[bid] = r["pipeline_id"]
    if communities is not None:
        out = {b: v for b, v in out.items() if v[0] in communities}
    return out


def counts(conn, builds, now=None):
    """{build_id: {severity: open findings, ..., "risk": their risk, "risks":
    {severity: their risk}}} of *builds* ({build_id: community})."""
    out = {b: dict({k: 0 for k in SEVERITIES}, risk=0.0, risks={k: 0.0 for k in SEVERITIES})
           for b in builds}
    if not builds:
        return out
    rules = vuln_triage.Rules.load(conn, set(builds.values()), now=now)
    ids = sorted(builds)
    for i in range(0, len(ids), 500):              # under SQLite's variable limit
        for f in sbom_scan._findings(conn, {b: builds[b] for b in ids[i:i + 500]}, rules):
            if f["not_shipped"] or f["rule"]:
                continue
            c, risk = out[f["build_id"]], f["risk"] or SEVERITY_RISK.get(f["severity"], 0.5)
            c[f["severity"]] += 1
            c["risks"][f["severity"]] += risk
    for c in out.values():
        c["risks"] = {k: round(v, 2) for k, v in c["risks"].items()}
        c["risk"] = round(sum(c["risks"].values()), 2)
    return out


def _newest(builds, before=None):
    """{(community, top): build_id}: each release's newest build (of a day
    before *before*, if given: {(community, top, day): build_id} then)."""
    newest = {}
    for bid, (community, top, day) in builds.items():
        key = (community, top) if before is None else (community, top, day)
        if before is not None and day >= before:
            continue
        if key not in newest or (day, bid) > (builds[newest[key]][2], newest[key]):
            newest[key] = bid
    return newest


def _record(conn, rows, got, replace):
    """Record [((community, top, day), build_id)] with the builds' counts *got*."""
    for (community, top, day), bid in rows:
        c = got[bid]
        conn.execute(
            "INSERT OR %s INTO sbom_history (day, community, top, build_id, critical, high,"
            " medium, low, other, risk, risks) VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            % ("REPLACE" if replace else "IGNORE"),
            (day, community, top, bid, *[c[k] for k in SEVERITIES], c["risk"],
             json.dumps(c["risks"], sort_keys=True)))


def snapshot(conn, now=None):
    """Record each release's counts and risk for today, once a day, and the days
    before with a build but no record (BACKFILL builds a pass; the day is
    complete once none is left). Returns how many rows were recorded."""
    now = db.now_ms() if now is None else now
    today = _day(now)
    if (conn.execute("SELECT value FROM kv WHERE key = ?", (DAY_KEY,)).fetchone() or [""])[0] == today:
        return 0
    builds = _builds(conn)
    have = {(r["community"], r["top"], r["day"]) for r in conn.execute(
        "SELECT community, top, day FROM sbom_history")}
    rows = [((c, t, today), bid) for (c, t), bid in _newest(builds).items()]
    missing = sorted((key, bid) for key, bid in _newest(builds, before=today).items()
                     if key not in have)
    # Counted before the write lock is taken (today's again each pass of a backfill).
    got = counts(conn, {bid: key[0] for key, bid in rows + missing[:BACKFILL]}, now=now)
    with db.transaction(conn):
        _record(conn, rows, got, replace=True)
        _record(conn, missing[:BACKFILL], got, replace=False)
        if len(missing) <= BACKFILL:
            conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                         " DO UPDATE SET value = excluded.value", (DAY_KEY, today))
    return len(rows) + len(missing[:BACKFILL])


def history(conn, communities=None, days=90, now=None):
    """{"releases": [{community, top, points: [{day, build_id, <severities>, risk, risks}]}],
    "builds": [{community, top, build_id, day}]}: the releases of *communities*
    (None: all) over the last *days* (0: all), oldest point first: the records,
    and today's point afresh."""
    now = db.now_ms() if now is None else now
    since = _day(now - days * 86400000) if days else ""
    today = _day(now)
    builds = _builds(conn, communities)
    points = {}                                     # (community, top) -> {day: point}
    for r in conn.execute("SELECT * FROM sbom_history WHERE day >= ? AND day < ?", (since, today)):
        if communities is None or r["community"] in communities:
            points.setdefault((r["community"], r["top"]), {})[r["day"]] = dict(
                {k: r[k] for k in SEVERITIES}, day=r["day"], build_id=r["build_id"], risk=r["risk"],
                risks=json.loads(r["risks"] or "{}"))
    newest = _newest(builds)
    got = counts(conn, {b: builds[b][0] for b in newest.values()}, now=now)
    for key, bid in newest.items():
        points.setdefault(key, {})[today] = dict(got[bid], day=today, build_id=bid)
    releases = [{"community": c, "top": t, "points": [p[d] for d in sorted(p)]}
                for (c, t), p in sorted(points.items())]
    marks = sorted(({"community": c, "top": t, "build_id": b, "day": d}
                    for b, (c, t, d) in builds.items() if d >= since),
                   key=lambda m: (m["day"], m["build_id"]))
    return {"releases": releases, "builds": marks}
