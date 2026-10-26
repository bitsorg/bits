# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The build queue: the console's builds wait here, and the worker starts them
one per build host. Long builds then never wait in GitLab, where a pending job
counts against the project's limit of jobs in active pipelines (and blocks the
Pages pipeline), and never share a host.

A build names its host (BITS_RUNNER_TAG bits-host-<name>) or an architecture
(bits-build-<arch>). The hosts are the build project's config/build-hosts.yaml
(name, tag, arch); a host's runner is the one with its unique tag.
Each pass (`dispatch`) starts, oldest first, every build that has a free host,
pinned to that host. A host is busy while a build pipeline runs on it: one the
queue started there, one pinned to it, or one with a job its runner took. A
running pipeline routed by architecture and not on a runner yet will take a
host of that architecture, so it holds one. A host whose runner GitLab does not
see online (switched off, asleep, paused) is not free: its builds wait here,
not in GitLab, which drops a job no runner can take. One GitLab dropped all
the same (stuck, no job started) is queued again.

States: queued -> dispatching -> dispatched; queued -> canceled | refused. A
build being started carries BITS_QUEUE_ENTRY, a token new for each attempt, so
one left dispatching (the worker stopped, GitLab's answer lost) is found among
the recorded pipelines (`recover`), not started twice. While GitLab refuses
pipelines (its job limit), no build is started for PAUSE_MS.
"""

import hashlib
import json
import logging
import secrets
import time

from . import db, events

log = logging.getLogger("bits_orchestrator.queue")

HOST_TAG, ARCH_TAG = "bits-host-", "bits-build-"
ENTRY_VAR = "BITS_QUEUE_ENTRY"
MAX_ATTEMPTS = 5                   # GitLab refusing one build this often: refused
RECOVER_MS = 15 * 60 * 1000        # a crashed start not among the pipelines by then: queued again
UNRECORDED_MS = 24 * 3600 * 1000   # a started pipeline never recorded holds its host this long
RECENT_MS = 24 * 3600 * 1000       # finished entries the listing shows
PAUSE_MS = 5 * 60 * 1000           # GitLab refusing pipelines: none started for this long
HOSTS_TTL_S = 300
MAX_BUILDS = 64                    # in one request (the console's matrix cap)
_JOB_DONE = {"success", "failed", "canceled", "skipped", "manual"}
_STUCK = {"stuck_or_timeout_failure", "no_matching_runner"}   # GitLab dropped it, unstarted


class Stop(Exception):
    """GitLab cannot start pipelines now (unreachable, its token, the job
    limit): the pass ends, the build keeps its place."""


def _vars(variables):
    """{key: value} of a [{key, value}] list."""
    return {str(v["key"]): str(v.get("value") or "") for v in variables
            if isinstance(v, dict) and v.get("key")}


def digest(ref, name, variables):
    """What a build is, as a passkey approves it: its ref, name and variables."""
    text = json.dumps([ref, name or "", sorted(_vars(variables).items())])
    return hashlib.sha256(text.encode()).hexdigest()


def certifies(variables):
    return _vars(variables).get("CERTIFY", "").strip().lower() == "true"


def check_target(variables):
    """The build's routing tag, or a ValueError: the queue routes only to a
    build host or an architecture."""
    tag = _vars(variables).get("BITS_RUNNER_TAG", "").strip()
    if not any(tag.startswith(p) and len(tag) > len(p) for p in (HOST_TAG, ARCH_TAG)):
        raise ValueError("BITS_RUNNER_TAG must be %s<host> or %s<arch>, not %r"
                         % (HOST_TAG, ARCH_TAG, tag))
    return tag


# --- requests ---------------------------------------------------------------

def add(conn, community, user, ref, builds):
    """Queue *builds* ([(name, variables)], checked by the API) for *user*.
    Returns (batch, [ids])."""
    if not 0 < len(builds) <= MAX_BUILDS:
        raise ValueError("1 to %d builds in one request" % MAX_BUILDS)
    batch, now, ids = secrets.token_hex(8), db.now_ms(), []
    with db.transaction(conn):
        for name, variables in builds:
            cur = conn.execute(
                "INSERT INTO build_queue (batch, community, requested_by, ref, name, variables,"
                " digest, target, certify, state, created_ms, updated_ms)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)",
                (batch, community, user, ref, name or "", json.dumps(variables),
                 digest(ref, name, variables), check_target(variables),
                 int(certifies(variables)), now, now))
            ids.append(cur.lastrowid)
        events.append(conn, "queue.requested", "queue:%s" % batch, events.Actor("person", user),
                      {"batch": batch, "community": community, "entries": ids})
    return batch, ids


def cancel(conn, entry_id, by):
    """Take a build that has not started out of the queue; False when it has."""
    with db.transaction(conn):
        r = conn.execute("SELECT * FROM build_queue WHERE id = ?", (int(entry_id),)).fetchone()
        cur = conn.execute("UPDATE build_queue SET state = 'canceled', updated_ms = ? WHERE id = ?"
                           " AND state = 'queued'", (db.now_ms(), int(entry_id)))
        if cur.rowcount != 1:
            return False
        events.append(conn, "queue.canceled", "queue:%s" % r["batch"], events.Actor("person", by),
                      {"entry": r["id"]})
    return True


def get(conn, entry_id):
    return conn.execute("SELECT * FROM build_queue WHERE id = ?", (int(entry_id),)).fetchone()


def public(r, ahead=None):
    v = _vars(json.loads(r["variables"]))
    d = {k: r[k] for k in ("id", "batch", "community", "requested_by", "name", "target", "state",
                           "reason", "host", "pipeline_id", "created_ms", "dispatched_ms")}
    d.update(certify=bool(r["certify"]), package=v.get("PACKAGE", ""),
             version=v.get("VERSION", ""), platform=v.get("PLATFORM", ""))
    if ahead is not None:
        d["ahead"] = ahead
    return d


def listing(conn, communities=None, limit=200):
    """Builds waiting or starting, then those that left the queue in the last
    day, newest first; *communities* (lowercase; None = all). A waiting build
    says how many wait before it (of every community)."""
    q, args = "SELECT * FROM build_queue WHERE (state IN ('queued', 'dispatching') OR updated_ms > ?)", \
        [db.now_ms() - RECENT_MS]
    if communities is not None:
        names = sorted(communities)
        q += " AND lower(community) IN (%s)" % ",".join("?" * len(names)) if names else " AND 0"
        args.extend(names)
    rows = conn.execute(q + " ORDER BY id DESC LIMIT ?", args + [max(1, min(int(limit), 500))]).fetchall()
    waiting = [r["id"] for r in conn.execute("SELECT id FROM build_queue WHERE state = 'queued'"
                                             " ORDER BY id")]
    place = {i: n for n, i in enumerate(waiting)}
    return [public(r, place.get(r["id"])) for r in rows]


def host_status(conn):
    """The hosts as the last pass saw them: [{name, arch, busy, offline}]."""
    r = conn.execute("SELECT value FROM kv WHERE key = 'queue.hosts'").fetchone()
    return json.loads(r["value"]) if r else []


# --- the hosts ----------------------------------------------------------------

def parse_hosts(text):
    """[{name, tag, arch}] of build-hosts.yaml; an entry without an
    arch takes only builds pinned to it."""
    import yaml
    doc = yaml.safe_load(text or "") or {}
    out = []
    for h in (doc.get("hosts") or []) if isinstance(doc, dict) else []:
        if not isinstance(h, dict):
            continue
        name, tag = str(h.get("name") or "").strip(), str(h.get("tag") or "").strip()
        if not name or not tag.startswith(HOST_TAG):
            log.warning("build host %r: needs a name and a %s<name> tag", h, HOST_TAG)
            continue
        out.append({"name": name, "tag": tag, "arch": str(h.get("arch") or "").strip()})
    return out


_hosts_cache = {}


def hosts(reader, path, ref="main"):
    """The build hosts, read from the build project at most every HOSTS_TTL_S."""
    now = time.monotonic()
    hit = _hosts_cache.get((path, ref))
    if hit and now - hit[0] < HOSTS_TTL_S:
        return hit[1]
    raw = reader.file_raw(path, ref) or b""
    found = parse_hosts(raw.decode() if isinstance(raw, bytes) else raw)
    _hosts_cache[(path, ref)] = (now, found)
    return found


def _occupancy(conn, hs, named):
    """(names of busy hosts, {arch: running pipelines not on a runner yet});
    *named* maps a runner's description to its host."""
    now, busy, waiting = db.now_ms(), set(), {}
    for r in conn.execute(
            "SELECT q.host FROM build_queue q LEFT JOIN pipelines p ON p.id = q.pipeline_id"
            " WHERE q.state = 'dispatching' OR (q.state = 'dispatched' AND"
            " ((p.is_build = 1 AND p.outcome = 'running') OR"
            " ((p.id IS NULL OR p.is_build = 0) AND q.dispatched_ms > ?)))",
            (now - UNRECORDED_MS,)):
        busy.add(r["host"])
    by_tag = {h["tag"]: h["name"] for h in hs}
    for r in conn.execute(
            "SELECT variables, jobs FROM pipelines WHERE is_build = 1 AND outcome = 'running'"
            " AND id NOT IN (SELECT pipeline_id FROM build_queue WHERE pipeline_id IS NOT NULL)"):
        try:
            tag = str(json.loads(r["variables"] or "{}").get("BITS_RUNNER_TAG") or "")
            runners = {j.get("runner") for j in json.loads(r["jobs"] or "[]")
                       if j.get("runner") and j.get("status") not in _JOB_DONE}
        except (ValueError, AttributeError):
            continue
        on = {named[x] for x in runners if x in named}
        if on:
            busy |= on
        elif tag in by_tag:
            busy.add(by_tag[tag])
        elif tag.startswith(ARCH_TAG):
            arch = tag[len(ARCH_TAG):]
            waiting[arch] = waiting.get(arch, 0) + 1
    return busy, waiting


# --- starting builds ------------------------------------------------------------

def _unanswered(e):
    """GitLab may have created the pipeline: the answer was lost, or it failed."""
    return e.status == 0 or e.status >= 500


def _refusing(e):
    """GitLab refuses every pipeline now (its token, its job limit)."""
    msg = str(e.message or "").lower()
    return e.status in (401, 403, 429) or "exceeded" in msg or "retry later" in msg


def _pause(conn, why):
    with db.transaction(conn):
        conn.execute("INSERT INTO kv (key, value) VALUES ('queue.pause', ?) ON CONFLICT(key)"
                     " DO UPDATE SET value = excluded.value",
                     (json.dumps({"until": db.now_ms() + PAUSE_MS, "why": why}),))


def _requeue(conn, r, reason, count=True, state="dispatching", then=None):
    """Back in its place, or refused after MAX_ATTEMPTS refusals; *then(conn)*
    runs in the same transaction."""
    refused = count and r["attempts"] >= MAX_ATTEMPTS
    with db.transaction(conn):
        if then is not None:
            then(conn)
        conn.execute("UPDATE build_queue SET state = ?, reason = ?, host = '', attempts = ?,"
                     " pipeline_id = NULL, updated_ms = ? WHERE id = ? AND state = ?",
                     ("refused" if refused else "queued", reason[:500],
                      r["attempts"] if count else max(0, r["attempts"] - 1), db.now_ms(), r["id"],
                      state))
        events.append(conn, "queue.refused" if refused else "queue.requeued", "queue:%s" % r["batch"],
                      events.WORKER, {"entry": r["id"], "reason": reason[:500]})


def _refuse(conn, r, reason):
    with db.transaction(conn):
        conn.execute("UPDATE build_queue SET state = 'refused', reason = ?, updated_ms = ?"
                     " WHERE id = ? AND state = 'queued'", (reason[:500], db.now_ms(), r["id"]))
        events.append(conn, "queue.refused", "queue:%s" % r["batch"], events.WORKER,
                      {"entry": r["id"], "reason": reason[:500]})


def _dispatched(conn, entry_id, pipeline_id, then=None):
    """The build started as *pipeline_id*: its request is amended with it.
    *then(conn, row)* runs in the same transaction (the worker's: a certifying
    build's pre-approval, approvals.preapprove_queued)."""
    now = db.now_ms()
    with db.transaction(conn):
        conn.execute("UPDATE build_queue SET state = 'dispatched', pipeline_id = ?, reason = '',"
                     " dispatched_ms = ?, updated_ms = ? WHERE id = ?",
                     (int(pipeline_id), now, now, entry_id))
        r = get(conn, entry_id)
        events.append(conn, "queue.dispatched", "queue:%s" % r["batch"], events.WORKER,
                      {"entry": r["id"], "pipeline": int(pipeline_id), "host": r["host"],
                       "digest": r["digest"]}, cause="pipeline:%d" % int(pipeline_id))
        if then is not None:
            then(conn, r)


def recover(conn, then=None):
    """A build left dispatching (the worker stopped while starting it, or
    GitLab's answer was lost): adopted when the pipeline of this attempt was
    recorded, else queued again after RECOVER_MS."""
    for r in conn.execute("SELECT * FROM build_queue WHERE state = 'dispatching'").fetchall():
        p = conn.execute("SELECT id FROM pipelines WHERE json_extract(variables, ?) = ?"
                         " ORDER BY id LIMIT 1", ("$." + ENTRY_VAR, r["token"])).fetchone()
        if p is not None and r["token"]:
            _dispatched(conn, r["id"], p["id"], then)
        elif db.now_ms() - r["updated_ms"] > RECOVER_MS:
            _requeue(conn, r, "not started: no pipeline of it was seen", count=False)


def dropped(jobs):
    """Did GitLab drop the pipeline of *jobs* before any runner took one of them
    (stuck pending: no runner it could give them to)?"""
    return (any(j.get("failure_reason") in _STUCK for j in jobs)
            and not any(j.get("runner") or j.get("started_at") for j in jobs))


def unstick(conn):
    """The builds GitLab dropped unstarted (a host's runner went away after the
    build was started there): queued again, refused after MAX_ATTEMPTS."""
    for r in conn.execute(
            "SELECT q.*, p.jobs AS p_jobs FROM build_queue q JOIN pipelines p ON p.id = q.pipeline_id"
            " WHERE q.state = 'dispatched' AND p.outcome = 'failed'").fetchall():
        try:
            jobs = [j for j in json.loads(r["p_jobs"] or "[]") if isinstance(j, dict)]
        except ValueError:
            continue
        if dropped(jobs):
            from . import approvals
            _requeue(conn, r, "GitLab dropped pipeline %d on %s: no runner took it"
                     % (r["pipeline_id"], r["host"] or "?"), state="dispatched",
                     then=lambda c, pid=r["pipeline_id"]: approvals.release_preapproval(c, pid))


def runner_hosts(forge, hs):
    """({hosts a runner with their unique tag serves, online and not paused},
    {runner description: host}) from GitLab; None when it cannot tell (then
    every host counts as online)."""
    from .. import forge_ops
    online, named = set(), {}
    try:
        for h in hs:
            for x in forge.list_runners(h["tag"]) or []:
                if not isinstance(x, dict):
                    continue
                d = str(x.get("description") or "")
                if named.get(d, h["name"]) != h["name"] and d not in _warned:
                    _warned.add(d)
                    log.warning("runner %r serves hosts %s and %s: give it one bits-host- tag",
                                d, named[d], h["name"])
                named[d] = h["name"]
                if x.get("paused") or x.get("active") is False:
                    continue
                if (x.get("status") or ("online" if x.get("online") else "")) == "online":
                    online.add(h["name"])
    except (forge_ops.ForgeError, ValueError) as e:
        log.warning("cannot list the runners (%s): hosts are taken as online", e)
        return None
    _named.clear()
    _named.update(named)
    for h in hs:
        if h["name"] not in named.values() and h["name"] not in _warned:
            _warned.add(h["name"])
            log.warning("build host %s: no runner of the project has its tag %s", h["name"], h["tag"])
    return online, named


_warned, _named = set(), {}            # the last runner descriptions GitLab gave


def _start(conn, forge, r, host, then):
    token = secrets.token_hex(12)
    with db.transaction(conn):
        cur = conn.execute("UPDATE build_queue SET state = 'dispatching', host = ?, token = ?,"
                           " attempts = attempts + 1, updated_ms = ? WHERE id = ?"
                           " AND state = 'queued'", (host["name"], token, db.now_ms(), r["id"]))
        if cur.rowcount != 1:
            return False                   # canceled meanwhile
    r = get(conn, r["id"])
    variables = [v for v in json.loads(r["variables"])
                 if str(v.get("key")) not in ("BITS_RUNNER_TAG", ENTRY_VAR)]
    variables += [{"key": "BITS_RUNNER_TAG", "value": host["tag"]},
                  {"key": ENTRY_VAR, "value": token}]
    from .. import forge_ops
    try:
        res = forge.trigger_pipeline(r["ref"], variables, r["name"] or None)
    except forge_ops.ForgeError as e:
        why = "GitLab %s: %s" % (e.status, e.message)
        if _unanswered(e):
            raise Stop(why)                # left dispatching: recover decides
        _requeue(conn, r, why, count=not _refusing(e))
        if _refusing(e):
            _pause(conn, why)
            raise Stop(why)
        return False
    if not res.get("id") or res.get("status") == "failed":
        # Created failed, with no jobs (GitLab's job limit): it never ran.
        why = "GitLab created pipeline %s failed" % res.get("id")
        if res.get("id"):
            try:
                forge.delete_pipeline(res["id"])
            except forge_ops.ForgeError as e:
                log.warning("cannot delete pipeline %s: %s", res["id"], e)
        _requeue(conn, r, why, count=False)
        _pause(conn, why)
        raise Stop(why)
    _dispatched(conn, r["id"], res["id"], then)
    return True


def dispatch(conn, forge, hs, then=None):
    """Start every waiting build that has a free host, its runner online. Returns how many."""
    recover(conn, then)
    unstick(conn)
    r = conn.execute("SELECT value FROM kv WHERE key = 'queue.pause'").fetchone()
    pause = json.loads(r["value"]) if r else {}
    if pause.get("until", 0) > db.now_ms():
        raise Stop(pause["why"])           # the same text: one event for the pause
    if not hs:
        if conn.execute("SELECT 1 FROM build_queue WHERE state = 'queued' LIMIT 1").fetchone():
            raise RuntimeError("builds are queued but there are no build hosts (build-hosts.yaml)")
        return 0
    seen = runner_hosts(forge, hs)
    online, named = seen or (None, {**{h["name"]: h["name"] for h in hs}, **_named})
    busy, waiting = _occupancy(conn, hs, named)
    offline = {h["name"] for h in hs if online is not None and h["name"] not in online}
    snapshot = [{"name": h["name"], "arch": h["arch"], "busy": h["name"] in busy,
                 "offline": h["name"] in offline} for h in hs]
    with db.transaction(conn):
        conn.execute("INSERT INTO kv (key, value) VALUES ('queue.hosts', ?) ON CONFLICT(key)"
                     " DO UPDATE SET value = excluded.value", (json.dumps(snapshot),))
    by_tag = {h["tag"]: h for h in hs}

    def free(arch):
        return [h for h in hs if h["arch"] == arch and h["name"] not in busy | offline]
    n = 0
    for r in conn.execute("SELECT * FROM build_queue WHERE state = 'queued' ORDER BY id").fetchall():
        if r["target"].startswith(HOST_TAG):
            if r["target"] not in by_tag:
                _refuse(conn, r, "%s is not a build host (build-hosts.yaml)" % r["target"])
                continue
            pick = by_tag[r["target"]] if by_tag[r["target"]]["name"] not in busy | offline else None
            arch = pick["arch"] if pick else ""
        else:
            arch = r["target"][len(ARCH_TAG):]
            if not any(h["arch"] == arch for h in hs):
                _refuse(conn, r, "no build host of architecture %s (build-hosts.yaml)" % arch)
                continue
            pick = (free(arch) or [None])[0]
        # A pipeline routed to this architecture and not on a runner yet will
        # take one of its free hosts: those stay free for it.
        if pick is None or (arch and len(free(arch)) <= waiting.get(arch, 0)):
            continue
        busy.add(pick["name"])
        if _start(conn, forge, r, pick, then):
            n += 1
    return n
