# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Reconcile: what the worker knows of builds, from GitLab and S3, never from
what a job reports about itself.

Each pass:

1. hints    - pipelines a CI job pointed at (POST /ops/ci/*): looked at now,
              even when older than the discovery window.
2. discover - pipelines of the build project updated since the last pass
              (with a few minutes' overlap); a new one's variables are read once.
3. settle   - for every build pipeline still running, or changed on GitLab, the
              outcome is decided from its CI jobs, as the console does: bits'
              own progress status can hold a pipeline "running" after every job
              finished, or fail it long after its jobs succeeded.
4. index    - a pipeline's builds are read from the S3 build index
              (BUILDS/by-pipeline/<pipeline>/<build_id>.json) and their BOMs,
              once there: a build's index is written after all its tarballs
              and BOMs are in the store, while the job goes on to CVMFS.
5. index_cli - a build made with the CLI is recorded once the worker certified
              it, under pipeline 0 (CLI_PIPELINE), so it is scanned like the others.

Each change is an event: pipeline.seen, pipeline.success|failed|canceled|running
(running again: a retry), build.published.
"""

import datetime
import json
import logging
import re

from .. import forge_ops
from . import db, events

log = logging.getLogger("bits_orchestrator")

BUILD_SOURCES = {"api", "trigger", "web"}
ACTIVE = {"created", "waiting_for_resource", "preparing", "pending", "running",
          "scheduled", "manual"}
# A manual job nobody started is finished as far as the build is concerned.
TERMINAL_JOB = {"success", "failed", "canceled", "skipped", "manual"}
_VARS = ("COMMUNITY", "PACKAGE", "VERSION", "PLATFORM", "PUBLISH_CVMFS")
_QUEUE_VARS = ("BITS_RUNNER_TAG", "BITS_QUEUE_ENTRY")
MAX_VARIABLES = 16 * 1024
OVERLAP = datetime.timedelta(minutes=5)
RECHECK_MS = 120 * 1000            # a running pipeline's jobs, at most this often
INDEX_RETRY_MS = 10 * 60 * 1000    # a finished pipeline with no index, looked at again
INDEX_GIVE_UP_MS = 6 * 3600 * 1000
STALE_AFTER = datetime.timedelta(days=3)   # a build job times out after 24 h
HINT_MAX_AGE_MS = 24 * 3600 * 1000
MAX_PER_STEP = 50                  # pipelines settled or indexed per pass
CLI_PIPELINE = 0                   # the builds made with the CLI (db migration 18)


def _iso(dt):
    return dt.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(text):
    """GitLab's time (any offset, fractional seconds) as an aware datetime, or None."""
    try:
        dt = datetime.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)


def _gone(e):
    return isinstance(e, forge_ops.ForgeError) and e.status == 404


def _kv_get(conn, key, default=None):
    r = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else default


def _kv_set(conn, key, value):
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                 " DO UPDATE SET value = excluded.value", (key, value))


def _subject(pid):
    return "pipeline:%d" % int(pid)


# --- outcome from jobs ----------------------------------------------------

def outcome_from_jobs(gl_status, jobs):
    """The pipeline's outcome: 'running', 'success', 'failed' or 'canceled'.

    From its latest job attempts, not from GitLab's pipeline status: decided once
    every job is finished, or as soon as a build-stage job has failed (a later
    status job may be wedged and cannot rescue it). With no jobs to go by,
    GitLab's own status decides."""
    latest = {}
    for j in jobs:
        key = (j.get("stage"), j.get("name"))
        if key not in latest or j.get("id", 0) > latest[key].get("id", 0):
            latest[key] = j
    jl = list(latest.values())
    if not jl:
        if gl_status in ACTIVE:
            return "running"
        return {"success": "success", "canceled": "canceled", "skipped": "canceled"}.get(
            gl_status, "failed")
    hard_fail = [j for j in jl if j.get("status") == "failed" and not j.get("allow_failure")]
    build = [j for j in jl if j.get("stage") == "build"]
    build_failed = (build and all(j.get("status") in TERMINAL_JOB for j in build)
                    and any(j in hard_fail for j in build))
    if not build_failed and not all(j.get("status") in TERMINAL_JOB for j in jl):
        return "running"
    if hard_fail:
        return "failed"
    if any(j.get("status") == "canceled" for j in jl):
        return "canceled"
    if any(j.get("status") == "success" for j in jl):
        return "success"
    return "canceled"


def job_summary(jobs):
    """What the console and the failure collection (M2) need of each job."""
    keep = ("id", "name", "stage", "status", "allow_failure", "failure_reason",
            "started_at", "finished_at", "web_url")
    out = []
    for j in sorted(jobs, key=lambda j: j.get("id", 0)):
        d = {k: j.get(k) for k in keep if j.get(k) is not None}
        runner = j.get("runner") or {}
        if runner.get("description"):
            d["runner"] = runner["description"]
        if runner.get("id"):
            d["runner_id"] = runner["id"]                # descriptions may be empty or shared
        out.append(d)
    return out


# --- recording pipelines --------------------------------------------------

def _vars_dict(variables):
    out = {}
    for v in variables or []:
        if isinstance(v, dict) and v.get("key"):
            out[str(v["key"])] = str(v.get("value") or "")
    return out


_NAME = re.compile(r"^\s*\[([^\]]+)\]\s*(\S+)\s*(.*?)\s*\(([^)]*)\)")


def _from_name(name):
    """COMMUNITY, PACKAGE, VERSION, PLATFORM from the build pipeline's name, which
    its workflow sets to '[COMMUNITY] PACKAGE VERSION (PLATFORM)<suffix>': what
    the console shows when the variables cannot be read."""
    m = _NAME.match(name or "")
    if not m:
        return {}
    return dict(zip(("COMMUNITY", "PACKAGE", "VERSION", "PLATFORM"),
                    (g.strip() for g in m.groups())))


def _var(variables, name):
    """A variable by name, case-insensitively (as the backend's pipeline
    community check reads COMMUNITY)."""
    for k, v in variables.items():
        if k.upper() == name:
            return v
    return ""


def record_pipeline(conn, reader, p):
    """Insert or update one pipeline from GitLab's listing *p*. A new one's
    variables are read once (they never change). Returns its row."""
    pid = int(p["id"])
    old = conn.execute("SELECT * FROM pipelines WHERE id = ?", (pid,)).fetchone()
    # Only what this response carries: the listing has no finished_at, the
    # single-pipeline read does; a field it lacks is kept, not blanked.
    names = {"status": "gl_status", "name": "name", "ref": "ref", "sha": "sha",
             "web_url": "web_url", "created_at": "created_at", "updated_at": "updated_at",
             "finished_at": "finished_at"}
    fields = {col: str(p.get(key) or "") for key, col in names.items() if key in p}
    if old is not None:
        if all(old[k] == v for k, v in fields.items()):
            return old
        # Changed on GitLab: its jobs are looked at again on this pass (a retry
        # makes it running again; a wedged status job after a failed build does not).
        sets = ", ".join("%s = :%s" % (c, c) for c in sorted(fields))
        with db.transaction(conn):
            conn.execute("UPDATE pipelines SET %s, checked_ms = 0 WHERE id = :id" % sets,
                         dict(fields, id=pid))
        return conn.execute("SELECT * FROM pipelines WHERE id = ?", (pid,)).fetchone()
    is_build = (p.get("source") in BUILD_SOURCES)
    variables = {}
    if is_build:
        # Unreadable variables (no ops token, GitLab refusing) never hold up
        # discovery: the pipeline's name carries what is needed to record it.
        try:
            variables = _vars_dict(reader.pipeline_variables(pid))
        except Exception as exc:
            log.warning("variables of pipeline %s: %s; using its name", pid, exc)
            variables = _from_name(p.get("name"))
    is_build = is_build and bool(_var(variables, "PACKAGE"))
    text = json.dumps(variables, sort_keys=True)
    if len(text) > MAX_VARIABLES:
        # What the build queue reads of a pipeline is kept too.
        text = json.dumps({k: _var(variables, k) for k in _VARS + _QUEUE_VARS}, sort_keys=True)
    row = dict({col: "" for col in names.values()}, **fields)
    row.update(id=pid, is_build=int(is_build), source=p.get("source") or "",
               community=_var(variables, "COMMUNITY").strip().lower(),
               package=_var(variables, "PACKAGE"), version=_var(variables, "VERSION"),
               platform=_var(variables, "PLATFORM"), variables=text,
               first_seen_ms=db.now_ms())
    with db.transaction(conn):
        conn.execute("INSERT INTO pipelines (id, is_build, community, name, source, ref, sha,"
                     " web_url, package, version, platform, variables, gl_status, created_at,"
                     " updated_at, finished_at, first_seen_ms) VALUES (:id, :is_build,"
                     " :community, :name, :source, :ref, :sha, :web_url, :package, :version,"
                     " :platform, :variables, :gl_status, :created_at, :updated_at,"
                     " :finished_at, :first_seen_ms)", row)
        if is_build:
            events.append(conn, "pipeline.seen", _subject(pid), events.WORKER,
                          {"pipeline": pid, "community": row["community"],
                           "package": row["package"], "version": row["version"],
                           "platform": row["platform"], "status": row["gl_status"]})
    return conn.execute("SELECT * FROM pipelines WHERE id = ?", (pid,)).fetchone()


def discover(conn, reader, lookback_days=7.0, max_rounds=20):
    """Record the pipelines updated since the last pass. Returns how many.

    Always the first page after a moving start: a listing sorted by update time
    shifts while it is read page by page (a pipeline that updates moves to the
    end), which can skip one for good. The start moves to the newest update read
    (minus a second; already-recorded pipelines cost nothing); when a full page
    shares one second, the next page of that second is read instead. The next
    pass starts a few minutes back to catch updates recorded out of order, unless
    this pass stopped at max_rounds: then it continues exactly from here."""
    cursor = _when(_kv_get(conn, "gitlab.updated_after"))
    page = 1
    if cursor is None:
        since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=lookback_days)
    elif _kv_get(conn, "gitlab.continue") == "1":
        since, page = cursor, int(_kv_get(conn, "gitlab.page", "1") or 1)
    else:
        since = cursor - OVERLAP
    newest, n, truncated = cursor, 0, True
    for _ in range(max_rounds):
        got = reader.list_pipelines(_iso(since), page=page)
        for p in got:
            record_pipeline(conn, reader, p)
            n += 1
            t = _when(p.get("updated_at"))
            if t and (newest is None or t > newest):
                newest = t
        if len(got) < 100:
            truncated = False
            break
        step = max((_when(p.get("updated_at")) for p in got if _when(p.get("updated_at"))),
                   default=since) - datetime.timedelta(seconds=1)
        if step > since:
            since, page = step, 1
        else:
            page += 1
    with db.transaction(conn):
        if truncated:                       # the next pass reads exactly this next
            _kv_set(conn, "gitlab.updated_after", _iso(since))
            _kv_set(conn, "gitlab.page", str(page))
        elif newest is not None:
            _kv_set(conn, "gitlab.updated_after", _iso(newest))
        _kv_set(conn, "gitlab.continue", "1" if truncated else "0")
    return n


def apply_hints(conn, reader):
    """Look now at the pipelines CI jobs pointed at. Unknown ones are fetched
    directly; known ones are re-checked and re-indexed on this pass. A hint for
    a pipeline GitLab does not have, or older than a day, is dropped."""
    hints = conn.execute("SELECT * FROM hints ORDER BY ts_ms LIMIT ?",
                         (MAX_PER_STEP,)).fetchall()
    now = db.now_ms()
    for h in hints:
        pid, done = h["pipeline_id"], True
        try:
            if conn.execute("SELECT 1 FROM pipelines WHERE id = ?", (pid,)).fetchone() is None:
                record_pipeline(conn, reader, reader.get_pipeline(pid))
        except Exception as exc:
            done = _gone(exc) or now - h["ts_ms"] > HINT_MAX_AGE_MS
            log.warning("hint for pipeline %s: %s%s", pid, exc, " (dropped)" if done else "")
        with db.transaction(conn):
            if done:
                conn.execute("UPDATE pipelines SET checked_ms = 0, indexed_ms = 0"
                             " WHERE id = ?", (pid,))
                conn.execute("DELETE FROM hints WHERE pipeline_id = ? AND ts_ms = ?",
                             (pid, h["ts_ms"]))
    return len(hints)


def _set_outcome(conn, r, outcome, jobs_json, extra=None):
    with db.transaction(conn):
        conn.execute("UPDATE pipelines SET outcome = ?, jobs = ?, checked_ms = ?"
                     " WHERE id = ?", (outcome, jobs_json, db.now_ms(), r["id"]))
        if outcome == r["outcome"]:
            if outcome == "failed" and jobs_json != r["jobs"]:
                # A job failed after the build did (an on_failure job): collect again;
                # jobs already recorded are skipped.
                conn.execute("UPDATE pipelines SET collected_ms = 0 WHERE id = ?", (r["id"],))
            return False
        # Finished (or running again): its builds are indexed, and its failures
        # collected, when it ends.
        conn.execute("UPDATE pipelines SET indexed_ms = 0, collected_ms = 0 WHERE id = ?",
                     (r["id"],))
        payload = {"pipeline": r["id"], "community": r["community"],
                   "gl_status": r["gl_status"]}
        payload.update(extra or {})
        events.append(conn, "pipeline." + outcome, _subject(r["id"]), events.WORKER, payload)
    return True


def settle(conn, reader):
    """Decide the outcome of build pipelines not settled yet. Returns how many
    changed. One pipeline GitLab cannot answer for does not hold up the others:
    a deleted one (404) is settled from its last known status, any other error
    is tried again on a later pass."""
    now = db.now_ms()
    # Running ones at most every RECHECK_MS; any one GitLab changed (or a hint
    # pointed at) now.
    rows = conn.execute(
        "SELECT * FROM pipelines WHERE is_build = 1 AND (checked_ms = 0 OR"
        " (outcome = 'running' AND checked_ms <= ?)) ORDER BY checked_ms, id LIMIT ?",
        (now - RECHECK_MS, MAX_PER_STEP)).fetchall()
    stale_before = datetime.datetime.now(datetime.timezone.utc) - STALE_AFTER
    changed = 0
    for r in rows:
        try:
            jobs = reader.pipeline_jobs(r["id"])
        except Exception as exc:
            if _gone(exc):
                out = outcome_from_jobs(r["gl_status"], [])
                changed += _set_outcome(conn, r, "canceled" if out == "running" else out,
                                        r["jobs"], {"gone": True})
            else:
                log.warning("jobs of pipeline %s: %s", r["id"], exc)
                with db.transaction(conn):
                    conn.execute("UPDATE pipelines SET checked_ms = ? WHERE id = ?",
                                 (db.now_ms(), r["id"]))
            continue
        outcome = outcome_from_jobs(r["gl_status"], jobs)
        summary = job_summary(jobs)
        extra = {"failed_jobs": [j["name"] for j in summary
                                 if j.get("status") == "failed" and not j.get("allow_failure")]}
        created = _when(r["created_at"])
        if outcome == "running" and created and created < stale_before:
            # Nothing runs this long: GitLab's word, or failed, and no more polling.
            outcome = outcome_from_jobs(r["gl_status"], [])
            outcome = "failed" if outcome == "running" else outcome
            extra["stale"] = True
        changed += _set_outcome(conn, r, outcome, json.dumps(summary), extra)
    return changed


# --- builds from the S3 index ---------------------------------------------

def _bom_facts(s3, bucket, keys):
    """arches, package count, top-level packages and publish time of a build's
    BOMs; an unreadable or non-BOM key is skipped (and logged)."""
    from bits_helpers.cvmfs_release import get_json, is_bom
    arches, packages, top, published = set(), 0, [], ""
    for key in keys:
        try:
            bom = get_json(s3, bucket, key)
        except Exception as exc:
            log.warning("unreadable BOM %s: %s", key, exc)
            continue
        if not is_bom(bom):
            continue
        arches.add(str(bom["effective_architecture"]))
        packages += len(bom["packages"])
        if not top and isinstance(bom.get("requested_packages"), list):
            top = [str(x) for x in bom["requested_packages"]]
        published = max(published, str(bom.get("published_at") or ""))
    return sorted(arches), packages, top, published


def _index_one(conn, s3, bucket, r):
    """Read one pipeline's build index and record its builds, all in one
    transaction with indexed_ms, so a failure part way records nothing."""
    from bits_helpers.cvmfs_release import get_json, list_keys
    found = {}
    for key in list_keys(s3, bucket, "BUILDS/by-pipeline/%d/" % r["id"]):
        try:
            idx = get_json(s3, bucket, key)
        except Exception as exc:
            log.warning("unreadable build index %s: %s", key, exc)
            continue
        if isinstance(idx, dict) and idx.get("build_id"):
            found[str(idx["build_id"])] = [k for k in idx.get("keys") or [] if isinstance(k, str)]
    known = {b["build_id"] for b in conn.execute(
        "SELECT build_id FROM builds WHERE pipeline_id = ?", (r["id"],))}
    new = [(b, keys, _bom_facts(s3, bucket, keys)) for b, keys in sorted(found.items())
           if b not in known]
    with db.transaction(conn):
        conn.execute("UPDATE pipelines SET indexed_ms = ? WHERE id = ?", (db.now_ms(), r["id"]))
        for build_id, keys, (arches, n, top, published) in new:
            conn.execute("INSERT INTO builds (pipeline_id, build_id, community, arches,"
                         " bom_keys, top_level, packages, published_at, recorded_ms)"
                         " VALUES (?,?,?,?,?,?,?,?,?)",
                         (r["id"], build_id, r["community"], json.dumps(arches),
                          json.dumps(keys), json.dumps(top), n, published, db.now_ms()))
            events.append(conn, "build.published", _subject(r["id"]), events.WORKER,
                          {"pipeline": r["id"], "build_id": build_id,
                           "community": r["community"], "arches": arches, "packages": n})
    return len(new)


def index_cli(conn, store):
    """Record the builds made with the CLI that the worker certified, as
    pipeline CLI_PIPELINE's (again when certified again): their BOMs those it
    signed, their community the pre-approval's group (a community with another
    certify_group has its CLI builds apart). Returns how many were recorded."""
    if store is None:
        return 0
    s3, bucket = store
    rows = conn.execute(
        "SELECT c.build_id, c.approval_id, c.updated_ms, a.groups FROM certify_runs c"
        " JOIN approvals a ON a.id = c.approval_id LEFT JOIN builds b ON"
        " b.pipeline_id = c.pipeline_id AND b.build_id = c.build_id"
        " WHERE c.pipeline_id = ? AND c.arch = '*' AND c.state = 'signed'"
        " AND (b.build_id IS NULL OR b.recorded_ms < c.updated_ms)"
        " ORDER BY c.updated_ms LIMIT ?", (CLI_PIPELINE, MAX_PER_STEP)).fetchall()
    n = 0
    for r in rows:
        keys = sorted({k for (b,) in conn.execute(
            "SELECT boms FROM certify_runs WHERE pipeline_id = ? AND build_id = ? AND arch != '*'"
            " AND state = 'signed' AND approval_id = ?", (CLI_PIPELINE, r["build_id"], r["approval_id"]))
            for k, _sha in json.loads(b or "[]")})
        arches, packages, top, published = _bom_facts(s3, bucket, keys)
        # A BOM not read now is tried again on the next pass, for INDEX_GIVE_UP_MS.
        if len(arches) < len(keys) and db.now_ms() - r["updated_ms"] < INDEX_GIVE_UP_MS:
            continue
        community = str((json.loads(r["groups"] or "[]") or [""])[0]).lower()
        with db.transaction(conn):
            conn.execute("INSERT INTO builds (pipeline_id, build_id, community, arches,"
                         " bom_keys, top_level, packages, published_at, recorded_ms)"
                         " VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(pipeline_id, build_id) DO UPDATE"
                         " SET community = excluded.community, arches = excluded.arches,"
                         " bom_keys = excluded.bom_keys, top_level = excluded.top_level,"
                         " packages = excluded.packages, published_at = excluded.published_at,"
                         " recorded_ms = excluded.recorded_ms",
                         (CLI_PIPELINE, r["build_id"], community, json.dumps(arches),
                          json.dumps(keys), json.dumps(top), packages, published, db.now_ms()))
            events.append(conn, "build.published", "build:%s" % r["build_id"], events.WORKER,
                          {"pipeline": CLI_PIPELINE, "build_id": r["build_id"],
                           "community": community, "arches": arches, "packages": packages})
        n += 1
    return n


def index(conn, store):
    """Record the builds of pipelines from the S3 build index, running ones
    too. Returns how many builds were recorded. *store* is (s3, bucket) or None."""
    if store is None:
        return 0
    s3, bucket = store
    now = db.now_ms()
    # Indexed when it starts or finishes (indexed_ms reset to 0), when a hint
    # points at it, and, while it has no builds yet, every INDEX_RETRY_MS while it
    # runs and until INDEX_GIVE_UP_MS after it finished (checked_ms).
    rows = conn.execute(
        "SELECT * FROM pipelines p WHERE is_build = 1 AND"
        " (indexed_ms = 0 OR (indexed_ms <= ? AND checked_ms >= ? AND NOT EXISTS"
        " (SELECT 1 FROM builds b WHERE b.pipeline_id = p.id))) ORDER BY indexed_ms, id"
        " LIMIT ?", (now - INDEX_RETRY_MS, now - INDEX_GIVE_UP_MS, MAX_PER_STEP)).fetchall()
    recorded = 0
    for r in rows:
        try:
            recorded += _index_one(conn, s3, bucket, r)
        except Exception as exc:
            log.warning("build index of pipeline %s: %s", r["id"], exc)
            with db.transaction(conn):       # tried again after INDEX_RETRY_MS
                conn.execute("UPDATE pipelines SET indexed_ms = ? WHERE id = ?",
                             (db.now_ms(), r["id"]))
    return recorded


# --- reads for the API ----------------------------------------------------

def _pipeline_public(r, builds):
    return {"pipeline": r["id"], "community": r["community"], "name": r["name"],
            "package": r["package"], "version": r["version"], "platform": r["platform"],
            "source": r["source"], "ref": r["ref"], "web_url": r["web_url"],
            "gl_status": r["gl_status"], "outcome": r["outcome"],
            "created_at": r["created_at"], "updated_at": r["updated_at"],
            "finished_at": r["finished_at"],
            "builds": [{"build_id": b["build_id"], "state": b["state"],
                        "arches": json.loads(b["arches"]), "packages": b["packages"],
                        "top_level": json.loads(b["top_level"]),
                        "published_at": b["published_at"]} for b in builds]}


def list_builds(conn, communities=None, outcome=None, pipelines=None, limit=50):
    """Build pipelines newest first, with their builds. *communities* (lowercase)
    None = all; *pipelines* restricts to those ids."""
    q, args = "SELECT * FROM pipelines WHERE is_build = 1", []
    if communities is not None:
        names = sorted(communities)
        if not names:
            return []
        q += " AND community IN (%s)" % ",".join("?" * len(names))
        args.extend(names)
    if outcome:
        q += " AND outcome = ?"
        args.append(outcome)
    if pipelines is not None:
        ids = sorted({int(p) for p in pipelines})[:200]
        if not ids:
            return []
        q += " AND id IN (%s)" % ",".join("?" * len(ids))
        args.extend(ids)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 200)))
    rows = conn.execute(q, args).fetchall()
    out = []
    for r in rows:
        b = conn.execute("SELECT * FROM builds WHERE pipeline_id = ? ORDER BY build_id",
                         (r["id"],)).fetchall()
        out.append(_pipeline_public(r, b))
    return out


def get_build(conn, pid):
    r = conn.execute("SELECT * FROM pipelines WHERE id = ? AND is_build = 1",
                     (int(pid),)).fetchone()
    if r is None:
        return None
    b = conn.execute("SELECT * FROM builds WHERE pipeline_id = ? ORDER BY build_id",
                     (r["id"],)).fetchall()
    out = _pipeline_public(r, b)
    out["jobs"] = json.loads(r["jobs"])
    out["variables"] = json.loads(r["variables"])
    out["events"] = events.since(conn, 0, 200, subject=_subject(r["id"]))
    return out


def add_hint(conn, pid, kind):
    with db.transaction(conn):
        conn.execute("INSERT INTO hints (pipeline_id, kind, ts_ms) VALUES (?, ?, ?)"
                     " ON CONFLICT(pipeline_id) DO UPDATE SET kind = excluded.kind,"
                     " ts_ms = excluded.ts_ms", (int(pid), kind, db.now_ms()))
