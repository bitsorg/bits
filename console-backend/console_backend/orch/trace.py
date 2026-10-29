# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Who built and who approved what is on CVMFS (`bits trace`).

The index: every package path prepub published, with the build (the CI pipeline
id) that published it, read from prepub's measurement records (GET
/api/v1/measurements on each of BITS_ORCH_PREPUB_URLS), which prepub keeps for
every build. A build is read again while it is recent, since records still come.

`lookup` answers for one CVMFS path: the publishes of the longest published path
above it, and for each publishing pipeline and for the build that compiled the
package, who requested it (the console's verified BITS_CERTIFIER, or the queue's
requester) and its passkey approval, with the approval's event in the audit chain.
"""

import json
import logging
import time
from datetime import datetime

from . import db

log = logging.getLogger("bits_orchestrator.trace")

PER_PASS = 20                      # per prepub
RECENT_MS = 2 * 24 * 3600 * 1000   # a build this recent is read again: records still come
REREAD_MS = 15 * 60 * 1000
TIMEOUT_S = 10


def _get(url):
    import requests
    r = requests.get(url, timeout=TIMEOUT_S)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _ms(text):
    try:
        return int(datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def _due(conn, prepub, builds, now):
    """The builds of one prepub to read this pass: new ones, then recent ones not
    read for a while, at most PER_PASS."""
    seen = {r["build_id"]: r for r in conn.execute(
        "SELECT * FROM traced_builds WHERE prepub = ?", (prepub,))}
    new = [b for b in builds if b not in seen]
    again = [b for b in builds if b in seen and now - seen[b]["last_ms"] < RECENT_MS
             and now - seen[b]["checked_ms"] > REREAD_MS]
    return (new + again)[:PER_PASS]


def _unreachable(exc):
    """The prepub itself cannot be reached (not one build's bad answer)."""
    import requests
    if isinstance(exc, (requests.HTTPError, ValueError)):   # an answer, if a bad one
        return False
    return isinstance(exc, (requests.RequestException, OSError))


def step(conn, prepubs, get=_get):
    """Read the measurement records of new and recent builds into the index. A
    build prepub cannot answer for is marked read, and tried again with the
    recent ones; an unreachable prepub is left for the next pass."""
    n, now = 0, db.now_ms()
    for prepub in prepubs:
        base = prepub.rstrip("/")
        try:
            builds = get(base + "/api/v1/measurements") or []
        except Exception as exc:      # one unreachable prepub does not stop the others
            log.warning("%s: no measurements (%s)", base, exc)
            continue
        for build in _due(conn, base, [str(b) for b in builds if b and b != "latest"], now):
            try:
                records = get("%s/api/v1/measurements/%s" % (base, build)) or []
            except Exception as exc:
                log.warning("%s: build %s unreadable (%s)", base, build, exc)
                if _unreachable(exc):
                    break
                records = []
            rows = [r for r in records if isinstance(r, dict)] if isinstance(records, list) else []
            # No records yet: recent from now, so it is read again while they come.
            last = max([_ms(r.get("ts")) for r in rows] or [now])
            with db.transaction(conn):
                for r in rows:
                    if r.get("outcome") != "published" or not r.get("path") or not r.get("job_id"):
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO published_paths (repo, path, build_id, job_id,"
                        " ts_ms, replaced, prepub) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (str(r.get("repo") or ""), str(r["path"]).strip("/"), build,
                         str(r["job_id"]), _ms(r.get("ts")), int(bool(r.get("replaced"))), base))
                conn.execute(
                    "INSERT INTO traced_builds (prepub, build_id, records, last_ms, checked_ms)"
                    " VALUES (?, ?, ?, ?, ?) ON CONFLICT(prepub, build_id) DO UPDATE SET"
                    " records = excluded.records, checked_ms = excluded.checked_ms,"
                    " last_ms = CASE WHEN excluded.records > 0 THEN excluded.last_ms"
                    " ELSE traced_builds.last_ms END", (base, build, len(rows), last, now))
            n += 1
    return n


def _approval(conn, subject):
    """The passkey pre-approval of a build id (the newest not revoked, else the
    newest), with its audit event, or None."""
    a = conn.execute("SELECT * FROM approvals WHERE kind = 'preapproval' AND subject = ?"
                     " ORDER BY revoked_ms IS NOT NULL, id DESC LIMIT 1",
                     (str(subject),)).fetchone()
    if a is None:
        return None
    ev = conn.execute("SELECT seq, hash FROM events WHERE subject = ? AND kind ="
                      " 'approval.granted' ORDER BY seq LIMIT 1",
                      ("approval:%d" % a["id"],)).fetchone()
    return {"approval": a["id"], "approver": a["approver"], "via": a["via"],
            "approved_at": _iso(a["created_ms"]),
            "revoked_at": _iso(a["revoked_ms"]) if a["revoked_ms"] else "",
            "event": {"seq": ev["seq"], "hash": ev["hash"]} if ev else None}


def _iso(ms):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ms / 1000)) if ms else ""


def pipeline(conn, pid):
    """A pipeline as `bits trace` shows it: what, where, who requested, approval."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    p = conn.execute("SELECT * FROM pipelines WHERE id = ?", (pid,)).fetchone()
    if p is None:
        return None
    variables = json.loads(p["variables"] or "{}")
    q = conn.execute("SELECT requested_by, batch FROM build_queue WHERE pipeline_id = ?"
                     " ORDER BY id LIMIT 1", (pid,)).fetchone()
    # The queue's requester is the backend's own record; BITS_CERTIFIER is a pipeline
    # variable, which the backend sets on the pipelines it starts, but anyone
    # starting one in GitLab could set too.
    who, how = ((q["requested_by"], "queue") if q and q["requested_by"] else
                (variables.get("BITS_CERTIFIER", ""), "pipeline variable"))
    return {"pipeline": pid, "web_url": p["web_url"], "community": p["community"],
            "package": p["package"], "version": p["version"], "platform": p["platform"],
            "created_at": p["created_at"], "outcome": p["outcome"], "source": p["source"],
            "requested_by": who, "requested_by_from": how if who else "",
            "queue_batch": q["batch"] if q else "",
            "approval": _approval(conn, pid)}


def lookup(conn, repo, path, build_id="", visible=None):
    """What is known of *path* (repo-relative) and of the build *build_id* that
    compiled its package. *visible*: the communities whose pipelines may be
    shown (None: all); others are named by id only."""
    parts = [p for p in str(path).strip("/").split("/") if p]
    above = ["/".join(parts[:i]) for i in range(len(parts), 0, -1)]
    rows, published = [], ""
    if above:
        rows = conn.execute(
            "SELECT * FROM published_paths WHERE repo = ? AND path IN (%s)"
            " ORDER BY length(path) DESC, ts_ms" % ",".join("?" * len(above)),
            [repo] + above).fetchall()
        published = rows[0]["path"] if rows else ""
        rows = [r for r in rows if r["path"] == published]

    def shown(pid):
        p = pipeline(conn, pid)
        if p is None:
            return {"pipeline": pid, "known": False}
        if visible is not None and p["community"].lower() not in visible:
            return {"pipeline": p["pipeline"], "known": True, "visible": False}
        return dict(p, known=True, visible=True)

    def publish(r):
        p = shown(r["build_id"])
        if p.get("visible") is False:
            return p           # who, when and where: only for the pipeline's community
        return dict({"published_at": _iso(r["ts_ms"]), "job": r["job_id"],
                     "replaced": bool(r["replaced"]), "prepub": r["prepub"]}, **p)

    built = []
    if build_id:
        for b in conn.execute("SELECT pipeline_id FROM builds WHERE build_id = ?"
                              " AND pipeline_id != 0 ORDER BY pipeline_id", (build_id,)):
            built.append(shown(b["pipeline_id"]))
    return {"repo": repo, "path": "/".join(parts), "published_path": published,
            "publishes": [publish(r) for r in rows],
            "build_id": build_id, "built_by_pipelines": built,
            "indexed": bool(conn.execute("SELECT 1 FROM traced_builds LIMIT 1").fetchone())}
