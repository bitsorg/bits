# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Actions: every change of state the orchestrator makes is one.

An action is requested (by a person, CI, a schedule, the worker, or later the
advisor), approved, then run by the worker. Each kind has a JSON schema for its
parameters (the catalogue the console reads, and later the advisor's tool list),
the community it acts for, and the approval it needs:

* "requester": an authorized requester is enough; the action is approved when
  requested.
* "approver": someone authorized must approve it separately.

An advisor can only propose: its actions start in "proposed" whatever the kind,
and only a person can approve them. The worker checks the approver's authority
again when it runs the action, not only when it was approved.

States: proposed -> approved -> running -> done | failed; proposed -> rejected.
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import db, events

STATES = ("proposed", "approved", "running", "done", "failed", "rejected")
APPROVALS = ("requester", "approver")
MAX_ATTEMPTS = 5
MAX_PARAMS = 16 * 1024


class ActionError(Exception):
    """A request that cannot be accepted (unknown kind, bad params, wrong state)."""


class Retry(Exception):
    """Raised by a runner for a transient failure: the action is tried again
    later, up to MAX_ATTEMPTS."""


@dataclass(frozen=True)
class Kind:
    name: str
    summary: str
    params: dict                                  # JSON schema (subset, see validate)
    approval: str                                 # "requester" | "approver"
    subject: Callable[[dict], str]                # what it acts on, e.g. "pipeline:123"
    scope: Callable[[dict, object], str]          # its community; "" = overall admins only
    run: Callable[[dict, object], Optional[dict]]
    safe_to_rerun: bool = False                   # may run again after a crash mid-run
    requesters: frozenset = field(default=frozenset(events.ACTOR_KINDS))

    def __post_init__(self):
        if self.approval not in APPROVALS:
            raise ValueError("approval must be one of %s" % (APPROVALS,))


REGISTRY = {}


def register(kind):
    if kind.name in REGISTRY:
        raise ValueError("action kind %r registered twice" % kind.name)
    REGISTRY[kind.name] = kind
    return kind


def kind(name):
    k = REGISTRY.get(name)
    if k is None:
        raise ActionError("unknown action kind %r" % name)
    return k


def catalogue():
    """Every kind with its parameter schema and the approval it needs."""
    return [{"kind": k.name, "summary": k.summary, "approval": k.approval,
             "params": k.params} for k in sorted(REGISTRY.values(), key=lambda k: k.name)]


# --- parameters -------------------------------------------------------------

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
          "array": list, "object": dict, "null": type(None)}


def validate(schema, value, path="params"):
    """Validate against the JSON-schema subset the catalogue uses: type, properties,
    required, additionalProperties: false, enum, pattern, maxLength, minimum,
    maximum, items, maxItems. Raises ActionError naming the first problem."""
    t = schema.get("type")
    if t:
        py = _TYPES[t]
        if not isinstance(value, py) or (t in ("integer", "number") and isinstance(value, bool)):
            raise ActionError("%s: expected %s" % (path, t))
    if "enum" in schema and not any(value == e and type(value) is type(e)
                                    for e in schema["enum"]):
        raise ActionError("%s: must be one of %s" % (path, schema["enum"]))
    if isinstance(value, str):
        if len(value) > schema.get("maxLength", 1024):
            raise ActionError("%s: too long" % path)
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ActionError("%s: does not match %s" % (path, schema["pattern"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ActionError("%s: below %s" % (path, schema["minimum"]))
        if "maximum" in schema and value > schema["maximum"]:
            raise ActionError("%s: above %s" % (path, schema["maximum"]))
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 256):
            raise ActionError("%s: too many items" % path)
        for i, item in enumerate(value):
            validate(schema.get("items", {}), item, "%s[%d]" % (path, i))
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise ActionError("%s: %s is required" % (path, name))
        for name, v in value.items():
            if name not in props:
                if schema.get("additionalProperties", True) is False:
                    raise ActionError("%s: unexpected %s" % (path, name))
                continue
            validate(props[name], v, "%s.%s" % (path, name))


def check_params(k, params):
    if not isinstance(params, dict):
        raise ActionError("params must be an object")
    if len(json.dumps(params)) > MAX_PARAMS:
        raise ActionError("params over %d bytes" % MAX_PARAMS)
    validate(k.params, params)


# --- rows -------------------------------------------------------------------

def _row(r):
    if r is None:
        return None
    d = dict(r)
    d["params"] = json.loads(d["params"])
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


def get(conn, action_id):
    return _row(conn.execute("SELECT * FROM actions WHERE id = ?", (int(action_id),)).fetchone())


def listing(conn, state=None, communities=None, limit=100):
    """Newest first. *communities*: only those (lowercase names); None = all."""
    q, args = "SELECT * FROM actions WHERE 1=1", []
    if state:
        q += " AND state = ?"
        args.append(state)
    if communities is not None:
        names = sorted(communities)
        q += " AND lower(community) IN (%s)" % ",".join("?" * len(names)) if names else " AND 0"
        args.extend(names)
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    return [_row(r) for r in conn.execute(q, args)]


def _event(conn, what, row, actor, extra=None, cause=None):
    payload = {"action": row["id"], "kind": row["kind"], "community": row["community"]}
    payload.update(extra or {})
    events.append(conn, "action." + what, row["subject"], actor, payload,
                  cause=cause or "action:%d" % row["id"])


# --- lifecycle --------------------------------------------------------------

def request(conn, kind_name, params, actor, community, authorized,
            idempotency_key=None, cause=None):
    """Record a requested action. *authorized* is the caller's verdict that
    *actor* may act for *community* (the API and the worker decide it; this
    module does no I/O). Returns (row, created). A request repeating an existing
    (kind, requester, idempotency_key) returns that action unchanged; the same
    key with other parameters is an error."""
    k = kind(kind_name)
    check_params(k, params)
    if actor.kind not in k.requesters:
        raise ActionError("%s may not request %s" % (actor.kind, k.name))
    if actor.kind != "advisor" and not authorized:
        raise ActionError("%s %s is not authorized for %s"
                          % (actor.kind, actor.id, community or "overall admin"))
    key = idempotency_key or uuid.uuid4().hex
    if len(key) > 200:
        raise ActionError("idempotency key too long")
    auto = actor.kind != "advisor" and k.approval == "requester"
    now = db.now_ms()
    with db.transaction(conn):
        old = _row(conn.execute(
            "SELECT * FROM actions WHERE kind = ? AND requested_by_kind = ? AND"
            " requested_by_id = ? AND idempotency_key = ?",
            (k.name, actor.kind, actor.id, key)).fetchone())
        if old:
            if old["params"] != params or old["community"] != community:
                raise ActionError("idempotency key %r was used for another %s" % (key, k.name))
            return old, False
        cur = conn.execute(
            "INSERT INTO actions (kind, params, idempotency_key, subject, community, state,"
            " requested_by_kind, requested_by_id, approved_by_kind, approved_by_id, cause,"
            " created_ms, updated_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (k.name, json.dumps(params, sort_keys=True), key, k.subject(params), community,
             "approved" if auto else "proposed", actor.kind, actor.id,
             actor.kind if auto else None, actor.id if auto else None, cause, now, now))
        row = _row(conn.execute("SELECT * FROM actions WHERE id = ?",
                                (cur.lastrowid,)).fetchone())
        _event(conn, "requested", row, actor, {"params": params}, cause=cause)
        if auto:
            _event(conn, "approved", row, actor, {"by": "requester"})
    return row, True


def _decide(conn, action_id, actor, authorized, to, reason=None):
    if actor.kind != "person":
        raise ActionError("only a person can approve or reject an action")
    with db.transaction(conn):
        row = get(conn, action_id)
        if row is None:
            raise ActionError("no action %s" % action_id)
        if row["state"] != "proposed":
            raise ActionError("action %s is %s, not proposed" % (action_id, row["state"]))
        if not authorized:
            raise ActionError("%s is not authorized for %s"
                              % (actor.id, row["community"] or "overall admin"))
        k = REGISTRY.get(row["kind"])
        if (to == "approved" and k is not None and k.approval == "approver"
                and (row["requested_by_kind"], row["requested_by_id"]) == (actor.kind, actor.id)):
            raise ActionError("%s needs an approver other than its requester" % row["kind"])
        if to == "approved":
            conn.execute("UPDATE actions SET state = 'approved', approved_by_kind = ?,"
                         " approved_by_id = ?, updated_ms = ? WHERE id = ?",
                         (actor.kind, actor.id, db.now_ms(), row["id"]))
            _event(conn, "approved", row, actor)
        else:
            conn.execute("UPDATE actions SET state = 'rejected', error = ?, updated_ms = ?"
                         " WHERE id = ?", (reason, db.now_ms(), row["id"]))
            _event(conn, "rejected", row, actor, {"reason": reason})
    return get(conn, action_id)


def approve(conn, action_id, actor, authorized):
    return _decide(conn, action_id, actor, authorized, "approved")


def reject(conn, action_id, actor, authorized, reason=""):
    return _decide(conn, action_id, actor, authorized, "rejected", (reason or "")[:500])


def claim_next(conn):
    """Take the oldest approved action that is due, whose subject has nothing
    running and no older approved action waiting (one backing off, say), and mark
    it running: actions on one subject run in the order they were approved for.
    Returns it, or None."""
    now = db.now_ms()
    with db.transaction(conn):
        r = conn.execute(
            "SELECT * FROM actions a WHERE state = 'approved' AND not_before_ms <= ?"
            " AND NOT EXISTS (SELECT 1 FROM actions b WHERE b.subject = a.subject"
            " AND b.state = 'running') AND NOT EXISTS (SELECT 1 FROM actions c"
            " WHERE c.subject = a.subject AND c.state = 'approved' AND c.id < a.id)"
            " ORDER BY id LIMIT 1", (now,)).fetchone()
        if r is None:
            return None
        conn.execute("UPDATE actions SET state = 'running', attempts = attempts + 1,"
                     " updated_ms = ? WHERE id = ?", (now, r["id"]))
        row = get(conn, r["id"])
        _event(conn, "started", row, events.WORKER, {"attempt": row["attempts"]})
    return row


def finish(conn, row, result=None, error=None, retry=False):
    """Record how a run ended: done, failed, or (retry) approved again after a
    backoff, until MAX_ATTEMPTS."""
    now = db.now_ms()
    with db.transaction(conn):
        if error is None:
            conn.execute("UPDATE actions SET state = 'done', result = ?, error = NULL,"
                         " updated_ms = ? WHERE id = ?",
                         (json.dumps(result) if result is not None else None, now, row["id"]))
            _event(conn, "done", row, events.WORKER, {"result": result})
        elif retry and row["attempts"] < MAX_ATTEMPTS:
            delay = 30000 * 2 ** (row["attempts"] - 1)
            conn.execute("UPDATE actions SET state = 'approved', error = ?, not_before_ms = ?,"
                         " updated_ms = ? WHERE id = ?",
                         (error[:1000], now + delay, now, row["id"]))
            _event(conn, "retrying", row, events.WORKER,
                   {"error": error[:1000], "attempt": row["attempts"], "in_ms": delay})
        else:
            conn.execute("UPDATE actions SET state = 'failed', error = ?, updated_ms = ?"
                         " WHERE id = ?", (error[:1000], now, row["id"]))
            _event(conn, "failed", row, events.WORKER,
                   {"error": error[:1000], "attempt": row["attempts"]})


def recover(conn):
    """At worker start: an action left running was interrupted. Run it again if
    its kind allows and it has attempts left, else fail it so a person decides
    (an action that crashes the worker must not run on every restart)."""
    rows = [_row(r) for r in conn.execute("SELECT * FROM actions WHERE state = 'running'")]
    for row in rows:
        k = REGISTRY.get(row["kind"])
        with db.transaction(conn):
            if k is not None and k.safe_to_rerun and row["attempts"] < MAX_ATTEMPTS:
                conn.execute("UPDATE actions SET state = 'approved', updated_ms = ?"
                             " WHERE id = ?", (db.now_ms(), row["id"]))
                _event(conn, "interrupted", row, events.WORKER, {"then": "rerun"})
            else:
                conn.execute("UPDATE actions SET state = 'failed', error = ?, updated_ms = ?"
                             " WHERE id = ?", ("interrupted while running", db.now_ms(),
                                               row["id"]))
                _event(conn, "interrupted", row, events.WORKER, {"then": "failed"})
    return len(rows)


def run_due(conn, deps, authorize, budget=20):
    """Run up to *budget* due actions. *authorize(actor, community)* re-checks the
    approver's authority now; a lapsed authority fails the action."""
    n = 0
    while n < budget:
        row = claim_next(conn)
        if row is None:
            break
        n += 1
        k = REGISTRY.get(row["kind"])
        if k is None:
            finish(conn, row, error="unknown action kind %s" % row["kind"])
            continue
        try:
            approver = events.Actor(row["approved_by_kind"], row["approved_by_id"])
        except (TypeError, ValueError):
            finish(conn, row, error="action has no valid approver")
            continue
        try:
            ok = authorize(approver, row["community"])
        except Exception as exc:          # policy unreadable: try again later
            finish(conn, row, error="authority check failed: %s" % exc, retry=True)
            continue
        if not ok:
            finish(conn, row, error="%s is no longer authorized for %s"
                   % (approver.id, row["community"] or "overall admin"))
            continue
        try:
            result = k.run(row["params"], deps)
        except Retry as exc:
            finish(conn, row, error=str(exc) or "transient failure", retry=True)
        except Exception as exc:
            finish(conn, row, error="%s: %s" % (type(exc).__name__, exc))
        else:
            finish(conn, row, result=result)
    return n
