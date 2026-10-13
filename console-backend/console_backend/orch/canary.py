# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The canary (M8a, until M8b): the worker's certifications, written under the
canary prefix, compared with the bits-manifests CI's certifications of the
same builds. M8b, where the worker writes MANIFESTS/ itself, deletes this
module with its tables (ci_outputs, canary_compare).

The CI's side is recorded by the shadow (`record_ci`) as a content digest per
build and architecture. A worker certification computed on the commit the
CI's merge was based on is 'match' or 'mismatch'; one computed on another
version of the set is 'rebased' (not evidence either way). One the CI never
certified is 'unmatched' after a while, a CI certification without one
'missing'.
"""

import hashlib
import json
import logging

from . import db, events, replay, shadow

log = logging.getLogger("bits_orchestrator.canary")
DAY_MS = 24 * 3600 * 1000


def content_digest(manifest):
    """What the comparison compares: a manifest without its timestamps, its
    certifiers and its source_commit."""
    c = shadow.content(manifest)
    c.pop("source_commit", None)
    return hashlib.sha256(shadow._canonical(c).encode()).hexdigest()


def record_ci(conn, settings, cert_pid, sha, before, new_root, old_root, parsed):
    """The CI's side: what it signed for the build its push added, per
    architecture, as a content digest. In the shadow's transaction (no I/O);
    only while the worker certifies, for certifications seen since it began.
    A problem here never stops the shadow's own result."""
    if not settings.orch_certify:
        return 0
    try:
        since = conn.execute("SELECT value FROM kv WHERE key = 'certify.since'").fetchone()
        seen = conn.execute("SELECT created_ms FROM certifications WHERE pipeline_id = ?",
                            (cert_pid,)).fetchone()
        if since is None or seen is None or seen["created_ms"] < int(since["value"]):
            return 0
        pid, mr_docs = replay.build_of(new_root, old_root)
        by_arch = {}
        for doc in mr_docs.values():
            if isinstance(doc, dict) and doc.get("build_id"):
                by_arch[shadow._bom_arch(json.dumps(doc)) or ""] = str(doc["build_id"])
        rows = []
        for arch, (doc, kid, source) in parsed.items():
            bid = by_arch.get(arch)
            if doc is None or kid is None or source == "superseded" or not bid:
                continue
            rows.append((cert_pid, pid, bid, arch, content_digest(doc), sha, before or "",
                         db.now_ms()))
        # (A savepoint: a failed insert leaves the shadow's transaction as it was.)
        conn.execute("SAVEPOINT canary_ci")
        try:
            conn.executemany("INSERT OR IGNORE INTO ci_outputs (certification, pipeline_id,"
                             " build_id, arch, content, sha, before_sha, created_ms)"
                             " VALUES (?,?,?,?,?,?,?,?)", rows)
        except Exception:
            conn.execute("ROLLBACK TO canary_ci")
            raise
        finally:
            conn.execute("RELEASE canary_ci")
        return len(rows)
    except (replay.Missing, replay.Refuse):
        return 0
    except Exception:
        log.exception("canary: the CI's side of certification %s", cert_pid)
        return 0


def _compared(conn, run_id, state, certification, now):
    """One worker certification's result. Its manifest bytes are kept only
    where they explain something."""
    conn.execute("INSERT INTO canary_compare (run_id, compare, ci_certification, created_ms)"
                 " VALUES (?, ?, ?, ?)", (run_id, state, certification, now))
    if state != "mismatch":
        conn.execute("UPDATE certify_runs SET manifest = NULL WHERE id = ?", (run_id,))


def match(conn, settings, now=None):
    """Pair the worker's and the CI's certifications of the same build and
    architecture, whichever came first; then the ones left alone too long."""
    now = now or db.now_ms()
    n = 0
    pairs = conn.execute(
        "SELECT c.id, c.pipeline_id, c.build_id, c.arch, c.manifest, c.set_commit, c.in_set,"
        " o.certification, o.content AS theirs, o.sha, o.before_sha FROM certify_runs c"
        " JOIN ci_outputs o ON o.pipeline_id = c.pipeline_id AND o.build_id = c.build_id"
        " AND o.arch = c.arch WHERE c.state = 'signed' AND c.manifest IS NOT NULL AND"
        " o.compare = '' AND NOT EXISTS (SELECT 1 FROM canary_compare k WHERE k.run_id = c.id)"
        " ORDER BY c.id").fetchall()
    done_c, done_o = set(), set()
    for p in pairs:
        o = (p["certification"], p["build_id"], p["arch"])
        if p["id"] in done_c or o in done_o:
            continue                       # ('again', below, for the CI's other one)
        done_c.add(p["id"])
        done_o.add(o)
        same_base = p["set_commit"] == (p["sha"] if p["in_set"] else p["before_sha"])
        mine = content_digest(json.loads(bytes(p["manifest"])))
        state = ("rebased" if not same_base else "match" if mine == p["theirs"]
                 else "mismatch")
        with db.transaction(conn):
            _compared(conn, p["id"], state, p["certification"], now)
            conn.execute("UPDATE ci_outputs SET compare = ? WHERE certification = ? AND"
                         " build_id = ? AND arch = ?",
                         (state, p["certification"], p["build_id"], p["arch"]))
            events.append(conn, "canary." + state, "pipeline:%d" % p["pipeline_id"], events.WORKER,
                          {"pipeline": p["pipeline_id"], "build_id": p["build_id"],
                           "arch": p["arch"], "certification": p["certification"]})
        n += 1
    # The CI certifying the same build again: the worker's was compared already.
    for r in conn.execute(
            "SELECT o.certification, o.build_id, o.arch FROM ci_outputs o WHERE o.compare = ''"
            " AND EXISTS (SELECT 1 FROM certify_runs c JOIN canary_compare k ON k.run_id = c.id"
            " WHERE c.pipeline_id = o.pipeline_id AND c.build_id = o.build_id AND"
            " c.arch = o.arch)").fetchall():
        with db.transaction(conn):
            conn.execute("UPDATE ci_outputs SET compare = 'again' WHERE certification = ? AND"
                         " build_id = ? AND arch = ?", (r["certification"], r["build_id"], r["arch"]))
    old = now - int(settings.orch_canary_unmatched_days * DAY_MS)
    for r in conn.execute(
            "SELECT id, pipeline_id, build_id, arch FROM certify_runs c WHERE state = 'signed'"
            " AND arch != '*' AND created_ms < ? AND NOT EXISTS (SELECT 1 FROM canary_compare k"
            " WHERE k.run_id = c.id)", (old,)).fetchall():
        with db.transaction(conn):
            _compared(conn, r["id"], "unmatched", None, now)
            events.append(conn, "canary.unmatched", "pipeline:%d" % r["pipeline_id"],
                          events.WORKER, {"pipeline": r["pipeline_id"],
                                          "build_id": r["build_id"], "arch": r["arch"]})
        n += 1
    late = now - int(settings.orch_canary_missing_days * DAY_MS)
    for r in conn.execute(
            "SELECT o.* FROM ci_outputs o WHERE o.compare = '' AND o.created_ms < ? AND NOT"
            " EXISTS (SELECT 1 FROM certify_runs c WHERE c.pipeline_id = o.pipeline_id AND"
            " c.build_id = o.build_id AND c.arch = o.arch AND c.state = 'signed')",
            (late,)).fetchall():
        why = conn.execute("SELECT state, reason FROM certify_runs WHERE pipeline_id = ? AND"
                           " build_id = ? AND arch = '*'", (r["pipeline_id"], r["build_id"])).fetchone()
        with db.transaction(conn):
            conn.execute("UPDATE ci_outputs SET compare = 'missing' WHERE certification = ? AND"
                         " build_id = ? AND arch = ?", (r["certification"], r["build_id"], r["arch"]))
            events.append(conn, "canary.missing", "pipeline:%d" % r["pipeline_id"], events.WORKER,
                          {"pipeline": r["pipeline_id"], "build_id": r["build_id"],
                           "arch": r["arch"], "certification": r["certification"],
                           "worker": ("%s: %s" % (why["state"], why["reason"]) if why
                                      else "never considered")})
        n += 1
    return n


def summary(conn, since_ms=0):
    """The comparison's results since *since_ms*, by result."""
    out = {r["compare"]: r["n"] for r in conn.execute(
        "SELECT compare, COUNT(*) AS n FROM canary_compare WHERE created_ms >= ?"
        " GROUP BY compare", (since_ms,))}
    missing = conn.execute("SELECT COUNT(*) FROM ci_outputs WHERE compare = 'missing' AND"
                           " created_ms >= ?", (since_ms,)).fetchone()[0]
    if missing:
        out["missing"] = missing
    return out


def annotate(conn, rows):
    """The comparison's result on each of certifier.listing's rows."""
    ids = [r["id"] for r in rows]
    found = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for k in conn.execute("SELECT run_id, compare, ci_certification FROM canary_compare"
                              " WHERE run_id IN (%s)" % ",".join("?" * len(part)), part):
            found[k["run_id"]] = (k["compare"], k["ci_certification"])
    for r in rows:
        r["compare"], r["ci_certification"] = found.get(r["id"], ("", None))
    return rows
