# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The canary (M8a, until M8b): the worker's certifications, written under the
canary prefix, compared with the bits-manifests CI's certifications of the
same builds. M8b, where the worker writes MANIFESTS/ itself, deletes this
module with its tables (ci_outputs, canary_compare).

The CI's side is recorded by the shadow (`record_ci`) as a content digest per
build and architecture (timestamps, certifiers, source_commit and the packages'
provenance left out). A worker certification made from the same inputs as the
CI's, for its architecture (the CI's merge base, or a set version that differs
from it only in other architectures' BOMs), is 'match' or 'mismatch'; one made
from other inputs is 'rebased' and one whose CI base never reached the set
'unbased' (neither is evidence either way), and one compared with a digest
recorded before this rule 'stale'. One the CI never certified is
'unmatched' after a while, a CI certification without one 'missing'.
"""

import hashlib
import json
import logging

from . import certified, db, events, replay, shadow

log = logging.getLogger("bits_orchestrator.canary")
DAY_MS = 24 * 3600 * 1000


# Per package: which build's BOM recorded the hash, and when. The same hash in
# two BOMs (a build published again) is the same object; the CI's merge keeps
# the first copy in path order, the worker this pipeline's own.
PROVENANCE = ("built_by", "completed_at", "bits_version", "bits_dist_hash")
DIGEST = "v2:"


def content_digest(manifest):
    """What the comparison compares: a manifest without its timestamps, its
    certifiers, its source_commit and its packages' provenance."""
    c = shadow.content(manifest)
    c.pop("source_commit", None)
    c["packages"] = [{k: v for k, v in e.items() if k not in PROVENANCE}
                     if isinstance(e, dict) else e for e in c.get("packages") or []]
    return DIGEST + hashlib.sha256(shadow._canonical(c).encode()).hexdigest()


def _same_inputs(conn, p):
    """Whether the worker and the CI certified this architecture from the same
    set: the worker's set version is the CI's merge base, or no BOM of this
    architecture but the build's own differs between the two. None while the
    CI's merge base may still be taken into the set; "unbased" once it cannot
    be (the set has moved past that pipeline without a version for it)."""
    if p["set_commit"] == p["before_sha"]:
        return True
    base = conn.execute("SELECT version FROM set_versions WHERE commit_sha = ? ORDER BY"
                        " version DESC LIMIT 1", (p["before_sha"],)).fetchone()
    if base is None or p["set_version"] is None:
        past = certified.followed(conn)
        return "unbased" if past is not None and past >= p["certification"] else None
    lo, hi = sorted((p["set_version"], base["version"]))
    return conn.execute(
        "SELECT 1 FROM certified WHERE arch = ? AND build_id != ? AND ((added_version > ?"
        " AND added_version <= ?) OR (removed_version > ? AND removed_version <= ?)) LIMIT 1",
        (p["arch"], p["build_id"], lo, hi, lo, hi)).fetchone() is None


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
    where they explain something: a difference, or a digest of the CI's that
    could not be compared."""
    conn.execute("INSERT INTO canary_compare (run_id, compare, ci_certification, created_ms)"
                 " VALUES (?, ?, ?, ?)", (run_id, state, certification, now))
    if state not in ("mismatch", "stale"):
        conn.execute("UPDATE certify_runs SET manifest = NULL WHERE id = ?", (run_id,))


def match(conn, settings, now=None):
    """Pair the worker's and the CI's certifications of the same build and
    architecture, whichever came first; then the ones left alone too long."""
    now = now or db.now_ms()
    n = 0
    pairs = conn.execute(
        "SELECT c.id, c.pipeline_id, c.build_id, c.arch, c.manifest, c.set_commit, c.set_version,"
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
        same = _same_inputs(conn, p)
        if same is None:
            continue                       # the set has not taken in the CI's base yet
        mine = content_digest(json.loads(bytes(p["manifest"])))
        state = ("unbased" if same == "unbased" else "rebased" if not same
                 else "stale" if not str(p["theirs"]).startswith(DIGEST)   # recorded before v2
                 else "match" if mine == p["theirs"] else "mismatch")
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
