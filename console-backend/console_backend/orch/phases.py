# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""What a finished build pipeline's job spent its time on, for the console's
timeline: [kind, start_ms, end_ms] segments.

  queued   from the request (the build queue's, else the pipeline's creation)
           to the job's start
  setup    the job's start to the build (bits-phases.txt: build_start)
  build    bits build (build_start to build_end)
  prepub   per package, prepub's work: queueing, chunk/compress/upload, lock wait
  gateway  per package, the commit (gateway ingest and catalogs)
  publish  the publish step, when prepub recorded no measurements for it

A failed job's `failed_in` says where it stopped, from the marks of the steps
it passed (build_ok, publish_start, finish_start): in the build, after it
(certification, the BOMs to the store), in the publish, or after the publish
step (finish); '' when it did not fail or its marks were not kept.

The CI job writes bits-phases.txt ("<mark> <epoch seconds>" lines) and keeps
prepub's measurement records (measurements-<pipeline>.json) as artifacts; a
record covers [ts - total_s, ts], of which the last commit_s is the gateway's.
Overlapping packages are merged per kind. Without the artifacts (older or
failed jobs) the segments come from the job's own times. Derived, so no
events: it can be computed again, and is when the pipeline is retried.
"""

import datetime
import json
import logging
import math
import re

from . import build_queue, db

log = logging.getLogger("bits_orchestrator.phases")

MAX_AGE_MS = 7 * 24 * 3600 * 1000   # GitLab keeps the job's artifacts a week
PER_PASS = 20
PHASES_FILE = "bits-phases.txt"
MEASUREMENTS_FILE = "measurements-%d.json"
_FRACTION = re.compile(r"\.(\d+)")


def _ms(text):
    """GitLab's or Go's time (any offset, up to nanoseconds) in ms, or None."""
    if not text:
        return None
    try:
        text = _FRACTION.sub(lambda m: "." + (m.group(1) + "000000")[:6], str(text), 1)
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def build_job(jobs):
    """The latest attempt of the pipeline's build job, or None."""
    build = [j for j in jobs if j.get("stage") == "build" and j.get("started_at")]
    return max(build, key=lambda j: j.get("id", 0)) if build else None


def marks(text):
    """{mark: ms} of bits-phases.txt; the last of a repeated mark wins."""
    out = {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1]) * 1000
    return out


def _merge(intervals):
    out = []
    for s, e in sorted(i for i in intervals if i[1] > i[0]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def publish_segments(records):
    """prepub and gateway segments of prepub's measurement records."""
    prepub, gateway = [], []
    for r in records if isinstance(records, list) else []:
        if not isinstance(r, dict):
            continue
        end = _ms(r.get("ts"))
        try:
            total = float(r.get("total_s") or 0)
            commit = min(max(float(r.get("commit_s") or 0), 0.0), total)
        except (TypeError, ValueError):
            continue
        if end is None or not math.isfinite(total) or total <= 0:
            continue
        start, split = end - int(total * 1000), end - int(commit * 1000)
        prepub.append((start, split))
        gateway.append((split, end))
    return ([["prepub", s, e] for s, e in _merge(prepub)] +
            [["gateway", s, e] for s, e in _merge(gateway)])


def segments(requested_ms, job, phase_marks, records):
    """The segments of one pipeline's build job (see the module docstring),
    all but queued within the job's times: three clocks, and a retried job's
    measurements still hold the earlier attempt's records."""
    start, end = _ms(job.get("started_at")), _ms(job.get("finished_at"))
    if start is None:
        return []
    end = end or start
    m = phase_marks or {}
    out = []
    if requested_ms and requested_ms < start:
        out.append(["queued", requested_ms, start])
    b0 = m.get("build_start", start)
    b1 = m.get("build_end") or m.get("publish_start") or end
    if b0 > start:
        out.append(["setup", start, b0])
    out.append(["build", b0, max(b0, b1)])
    p0, p1 = m.get("publish_start"), m.get("publish_end")
    pub = publish_segments(records)
    if pub:
        out += pub
    elif p0 and p1 and p1 > p0:
        out.append(["publish", p0, p1])
    def clip(t):
        return min(max(t, start), end)
    clipped = [s if s[0] == "queued" else [s[0], clip(s[1]), clip(s[2])] for s in out]
    return sorted((s for s in clipped if s[2] > s[1] or s[0] == "build"),
                  key=lambda s: (s[1], s[2]))


def failed_in(job, phase_marks):
    """Where a failed build job stopped, from its marks (see the module docstring)."""
    m = phase_marks or {}
    if job.get("status") != "failed" or not m:
        return ""
    if "build_ok" not in m:              # build_end is written whatever the build's outcome
        return "build"
    if "finish_start" not in m:
        return "publish" if "publish_start" in m else "certify"
    return "finish"


def host(job, variables):
    """The build host it ran on: the one it was pinned to (bits-host-<name>, as
    the queue pins its builds), else its runner's description."""
    try:
        tag = str(json.loads(variables or "{}").get("BITS_RUNNER_TAG") or "")
    except (ValueError, AttributeError):
        tag = ""
    if tag.startswith(build_queue.HOST_TAG) and tag != build_queue.HOST_TAG:
        return tag[len(build_queue.HOST_TAG):]
    return str(job.get("runner") or "")


def _artifact(reader, job_id, name):
    from .. import forge_ops
    try:
        raw = reader.job_artifact(job_id, name)
    except forge_ops.ForgeError as e:
        if e.status in (0, 429) or e.status >= 500:
            raise
        log.warning("job %s: %s: %s", job_id, name, e)
        return None
    return raw.decode("utf-8", "replace") if raw is not None else None


def step(conn, reader):
    """Record the segments of the recently finished build pipelines that have
    none, or whose build job was retried since. Returns how many."""
    now = db.now_ms()
    rows = conn.execute(
        "SELECT p.id, p.jobs, p.variables, p.created_at, f.job_id AS done_job, (SELECT MIN(q.created_ms)"
        " FROM build_queue q WHERE q.pipeline_id = p.id) AS queued_ms"
        " FROM pipelines p LEFT JOIN pipeline_phases f ON f.pipeline_id = p.id"
        " WHERE p.is_build = 1 AND p.outcome != 'running' AND p.first_seen_ms > ?"
        " ORDER BY p.id DESC", (now - MAX_AGE_MS,)).fetchall()
    n = 0
    for r in rows:
        try:
            job = build_job(json.loads(r["jobs"] or "[]"))
        except ValueError:
            job = None
        if job is None or job.get("id") == r["done_job"]:
            continue
        text = _artifact(reader, job["id"], PHASES_FILE)
        meas = _artifact(reader, job["id"], MEASUREMENTS_FILE % r["id"])
        try:
            records = json.loads(meas) if meas else []
        except ValueError:
            records = []
        requested = r["queued_ms"] or _ms(r["created_at"])
        m = marks(text)
        segs = segments(requested, job, m, records)
        with db.transaction(conn):
            conn.execute("INSERT INTO pipeline_phases (pipeline_id, job_id, host, segments,"
                         " failed_in, recorded_ms) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(pipeline_id)"
                         " DO UPDATE SET job_id = excluded.job_id, host = excluded.host,"
                         " segments = excluded.segments, failed_in = excluded.failed_in,"
                         " recorded_ms = excluded.recorded_ms",
                         (r["id"], job["id"], host(job, r["variables"]), json.dumps(segs),
                          failed_in(job, m), now))
        n += 1
        if n >= PER_PASS:
            break
    return n


def for_pipelines(conn, ids):
    """{pipeline: {host, segments, failed_in}} of those recorded."""
    ids = sorted({int(i) for i in ids})
    if not ids:
        return {}
    rows = conn.execute("SELECT * FROM pipeline_phases WHERE pipeline_id IN (%s)"
                        % ",".join("?" * len(ids)), ids).fetchall()
    return {r["pipeline_id"]: {"host": r["host"], "segments": json.loads(r["segments"]),
                               "failed_in": r["failed_in"]} for r in rows}
