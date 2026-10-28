# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The append-only event log: everything that happened, who did it and why.

It is also the audit chain. Each event's hash covers its own fields and the
previous event's hash, so editing or removing any event breaks every hash after
it; `verify` finds the first break. The outside copy (M4) is a second place the
chain is kept, so a rewrite on this host is detectable.

Events are appended inside the caller's transaction, so a change of state and
its event are written together or not at all.
"""

import hashlib
import json
from dataclasses import dataclass

from . import db

ACTOR_KINDS = ("person", "ci", "schedule", "worker", "advisor")
GENESIS = "0" * 64
MAX_PAYLOAD = 64 * 1024


@dataclass(frozen=True)
class Actor:
    kind: str
    id: str

    def __post_init__(self):
        if self.kind not in ACTOR_KINDS:
            raise ValueError("unknown actor kind %r" % self.kind)
        if not isinstance(self.id, str) or not self.id or len(self.id) > 128:
            raise ValueError("actor id must be 1-128 characters")


WORKER = Actor("worker", "orchestrator")


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(prev_hash, row):
    body = _canonical([row["seq"], row["ts_ms"], row["kind"], row["subject"],
                       row["actor_kind"], row["actor_id"], row["cause"],
                       row["payload"]])
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


def append(conn, kind, subject, actor, payload=None, cause=None):
    """Append one event; call inside `db.transaction`. Returns its seq."""
    if not conn.in_transaction:
        raise RuntimeError("events.append must run inside a transaction")
    if conn.execute("SELECT 1 FROM kv WHERE key = 'outside.behind'").fetchone():
        # Appending now would fork the chain from the outside copy's.
        raise RuntimeError("this database was restored from a copy older than the outside"
                           " audit copy: nothing is recorded until `outside recover` is run")
    text = _canonical(payload or {})
    if len(text) > MAX_PAYLOAD:
        raise ValueError("event payload over %d bytes" % MAX_PAYLOAD)
    last = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    row = {"seq": (last["seq"] + 1) if last else 1, "ts_ms": db.now_ms(),
           "kind": kind, "subject": subject, "actor_kind": actor.kind,
           "actor_id": actor.id, "cause": cause, "payload": text}
    prev = last["hash"] if last else GENESIS
    row["prev_hash"] = prev
    row["hash"] = _digest(prev, row)
    conn.execute("INSERT INTO events (seq, ts_ms, kind, subject, actor_kind, actor_id,"
                 " cause, payload, prev_hash, hash) VALUES (:seq, :ts_ms, :kind,"
                 " :subject, :actor_kind, :actor_id, :cause, :payload, :prev_hash, :hash)",
                 row)
    return row["seq"]


def _public(r):
    return {"seq": r["seq"], "ts_ms": r["ts_ms"], "kind": r["kind"],
            "subject": r["subject"], "actor": {"kind": r["actor_kind"], "id": r["actor_id"]},
            "cause": r["cause"], "payload": json.loads(r["payload"]), "hash": r["hash"]}


def since(conn, after=0, limit=200, subject=None):
    """Events after *after*, oldest first."""
    limit = max(1, min(int(limit), 1000))
    if subject:
        rows = conn.execute("SELECT * FROM events WHERE seq > ? AND subject = ?"
                            " ORDER BY seq LIMIT ?", (int(after), subject, limit))
    else:
        rows = conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                            (int(after), limit))
    return [_public(r) for r in rows]


def failing_steps(conn):
    """{step: {error, since_ms}} of the worker's steps failing now, from its
    step_failed and step_recovered events since it last started."""
    r = conn.execute("SELECT MAX(seq) FROM events WHERE subject = 'worker'"
                     " AND kind = 'worker.started'").fetchone()
    out = {}
    for e in conn.execute("SELECT kind, ts_ms, payload FROM events WHERE subject = 'worker'"
                          " AND seq > ? AND kind IN ('worker.step_failed', 'worker.step_recovered')"
                          " ORDER BY seq", (r[0] or 0,)):
        p = json.loads(e["payload"])
        step = str(p.get("step") or "")
        if e["kind"] == "worker.step_recovered":
            out.pop(step, None)
        else:          # a new error text of a step already failing keeps its start
            out[step] = {"error": p.get("error") or "", "since_ms": out.get(step, {}).get(
                "since_ms", e["ts_ms"])}
    return out


def verify(conn):
    """Walk the chain. Returns (count, None) when intact, or (count, seq) with the
    first event whose link or hash does not hold."""
    prev, expect, n = GENESIS, 1, 0
    for r in conn.execute("SELECT * FROM events ORDER BY seq"):
        n += 1
        if r["seq"] != expect or r["prev_hash"] != prev or _digest(prev, r) != r["hash"]:
            return n, r["seq"]
        prev, expect = r["hash"], expect + 1
    return n, None
