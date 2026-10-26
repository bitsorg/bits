# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Approvals that outlive the process: passkey-approved records in the
orchestrator's database.

* A **pre-approval** lets CI sign one build's manifests (by build id: a console
  pipeline id or a CLI build id), within its groups, up to a number of signs, for
  24 hours from the approval.
* A **standing approval** lets the builds of one schedule (its file, by content
  hash, and its groups) be certified without a person each time, for a number of
  runs or up to 30 days, whichever ends first. It is consumed by milestones M5
  and M7; this module records, lists, uses and revokes it. One for a batch of
  queued builds (build_queue.py) is bound to the builds themselves, for 7 days:
  as each starts, a run of it becomes the pre-approval of its pipeline.

Each record keeps the passkey assertion that approved it and the challenge it
answered. A standing approval also keeps the exact binding and nonce its
challenge was computed from, so `verify_standing` can check later that the
passkey signed what the record says; a pre-approval's binding is checked when it
is granted (a console one's challenge is a plain nonce). Every grant, use and
revocation is an event in the hash-chained log.

`Preapprovals` is what the sign path in main.py talks to. Pending pre-approvals
(a challenge waiting for its passkey) stay in memory, as before; approved ones
go to the database when BITS_ORCH_DB is set. Without it, everything stays in
memory (session.PreapprovalStore), as before M3.
"""

import contextlib
import hashlib
import json
import sqlite3

from .. import session
from . import db, events

PREAPPROVAL_TTL_MS = 24 * 3600 * 1000
PENDING_TTL_S = 600                # a pre-approval request waits this long for its passkey
API_BUSY_MS = 3000                 # the API waits this long for the worker's write lock
STANDING_MAX_DAYS = 30
QUEUE_DAYS = 7
PREAPPROVAL_SIGN_CAP = 16
STANDING_MAX_RUNS = 100
MAX_ASSERTION = 16 * 1024


class StoreError(Exception):
    """The approvals database cannot be used: callers fail closed (503)."""


_SIGNER = events.Actor("ci", "sign/preapproved")


def _actor(user):
    return events.Actor("person", user)


@contextlib.contextmanager
def _open(path):
    try:
        conn = db.open_checked(path, busy_ms=API_BUSY_MS)
    except (db.SchemaError, sqlite3.Error) as e:
        raise StoreError(str(e))
    try:
        yield conn
    except sqlite3.Error as e:
        raise StoreError(str(e))
    finally:
        conn.close()


def _assertion_text(assertion):
    text = json.dumps(assertion or {}, sort_keys=True)
    return text if len(text) <= MAX_ASSERTION else json.dumps({"truncated": True})


def _active(conn, kind, subject, now=None):
    now = now or db.now_ms()
    q = ("SELECT * FROM approvals WHERE kind = ? AND subject = ? AND revoked_ms IS NULL"
         " AND expires_ms > ?")
    if kind == "standing":
        q += " AND runs_left > 0"
    return conn.execute(q + " ORDER BY id DESC LIMIT 1", (kind, subject, now)).fetchone()


def public(r):
    """An approval as the API shows it (no assertion, no challenge)."""
    return {"id": r["id"], "kind": r["kind"], "subject": r["subject"],
            "groups": json.loads(r["groups"]), "approver": r["approver"], "via": r["via"],
            "binding": json.loads(r["binding"]), "packages": len(json.loads(r["packages"])),
            "created_ms": r["created_ms"], "expires_ms": r["expires_ms"], "signs": r["signs"],
            "sign_cap": r["sign_cap"], "runs_left": r["runs_left"],
            "revoked_ms": r["revoked_ms"], "revoked_by": r["revoked_by"]}


# --- pre-approvals, for the sign path -------------------------------------------

class Preapprovals:
    """The sign path's pre-approval store (same operations as
    session.PreapprovalStore): get, put, approve, reserve_sign, release_sign."""

    def __init__(self, settings_fn, sign_cap=PREAPPROVAL_SIGN_CAP):
        self._settings = settings_fn
        self._mem = session.PreapprovalStore()                      # no database: all here
        self._pending = session.PreapprovalStore(ttl_seconds=PENDING_TTL_S)
        self._cap = sign_cap

    def _path(self):
        return getattr(self._settings(), "orch_db", "") or ""

    def uses_db(self):
        return bool(self._path())

    @staticmethod
    def _record(r):
        return {"status": "approved", "id": r["id"], "groups": json.loads(r["groups"]),
                "user": r["approver"], "via": r["via"],
                "packages": json.loads(r["packages"]), "signs": r["signs"],
                "expires_ms": r["expires_ms"]}

    def get(self, build_id):
        """The approved pre-approval of *build_id*, else its pending one, else None."""
        path = self._path()
        if not path:
            return self._mem.get(build_id)
        with _open(path) as conn:
            r = _active(conn, "preapproval", build_id)
        if r is not None:
            return self._record(r)
        pending = self._pending.get(build_id)
        return pending if pending and pending.get("status") != "approved" else None

    def put(self, build_id, data):
        if data.get("status") == "approved":
            return self.approve(build_id, data)
        (self._pending if self._path() else self._mem).put(build_id, data)
        return True

    def approve(self, build_id, data, assertion=None):
        """Record the approved pre-approval; False when one is already approved."""
        path = self._path()
        if not path:
            return self._mem.approve(build_id, data, assertion)
        now = db.now_ms()
        challenge = data.get("challenge") or b""
        with _open(path) as conn:
            with db.transaction(conn):
                if _active(conn, "preapproval", build_id, now) is not None:
                    return False
                cur = conn.execute(
                    "INSERT INTO approvals (kind, subject, groups, approver, via, packages,"
                    " assertion, challenge, created_ms, expires_ms, sign_cap)"
                    " VALUES ('preapproval', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (build_id, json.dumps(sorted(data.get("groups") or [])), data["user"],
                     data.get("via") or "console", json.dumps(data.get("packages") or []),
                     _assertion_text(assertion),
                     challenge.hex() if isinstance(challenge, (bytes, bytearray)) else "",
                     now, now + PREAPPROVAL_TTL_MS, self._cap))
                events.append(conn, "approval.granted", "approval:%d" % cur.lastrowid,
                              _actor(data["user"]),
                              {"approval": cur.lastrowid, "kind": "preapproval",
                               "build_id": build_id, "groups": sorted(data.get("groups") or []),
                               "via": data.get("via") or "console",
                               "packages": len(data.get("packages") or [])})
        self._pending._store.pop(build_id, None)   # the pending one is spent
        return True

    def reserve_sign(self, build_id, cap, expect=None):
        """Take one sign of the build's active approval (atomic across
        processes); with *expect*, only of that approval. Returns what to give
        release_sign (the approval's id), or None when none is left, there is no
        approval, or it is not the expected one any more."""
        path = self._path()
        if not path:
            return self._mem.reserve_sign(build_id, cap, expect)
        with _open(path) as conn:
            with db.transaction(conn):
                r = _active(conn, "preapproval", build_id)
                if r is None or (expect is not None and r["id"] != expect):
                    return None
                cur = conn.execute("UPDATE approvals SET signs = signs + 1 WHERE id = ?"
                                   " AND signs < ? AND signs < sign_cap",
                                   (r["id"], cap))
                if cur.rowcount != 1:
                    return None
                events.append(conn, "approval.used", "approval:%d" % r["id"], _SIGNER,
                              {"approval": r["id"], "build_id": build_id, "sign": r["signs"] + 1})
        return r["id"]

    def release_sign(self, build_id, slot):
        """Give back a sign whose signing failed, to the approval it was taken
        on (even if that one has since been revoked or replaced)."""
        path = self._path()
        if not path:
            return self._mem.release_sign(build_id, slot)
        with _open(path) as conn:
            with db.transaction(conn):
                cur = conn.execute("UPDATE approvals SET signs = signs - 1 WHERE id = ?"
                                   " AND signs > 0", (int(slot),))
                if cur.rowcount == 1:
                    events.append(conn, "approval.released", "approval:%d" % int(slot), _SIGNER,
                                  {"approval": int(slot), "build_id": build_id})


# --- standing approvals ---------------------------------------------------------

def standing_binding(schedule, sha256, groups, runs, days):
    """The exact text a standing approval's passkey challenge is computed from."""
    return json.dumps(["standing", schedule, sha256, sorted(groups), int(runs), float(days)])


def standing_challenge(binding, nonce):
    return hashlib.sha256(hashlib.sha256(binding.encode()).digest() + nonce).digest()


def verify_standing(row):
    """Does the record's stored challenge come from its own binding and nonce
    (i.e. did the passkey sign what the record says)? The assertion itself is
    checked against that challenge by WebAuthn when it is granted."""
    b = json.loads(row["binding"])
    if "queue" in b:
        exact = queue_binding(b["queue"], b["community"], json.loads(row["groups"]), b["digests"])
    else:
        exact = standing_binding(b["schedule"], b["sha256"], json.loads(row["groups"]), b["runs"],
                                 b["days"])
    return (b.get("hashed") == exact and
            standing_challenge(exact, bytes.fromhex(b.get("nonce", ""))).hex() == row["challenge"])


def standing_subject(schedule, sha256):
    """What a standing approval is bound to: the schedule file and its content."""
    return "%s@%s" % (schedule, sha256)


def grant_standing(conn, schedule, sha256, groups, approver, runs, days, assertion, nonce):
    """Record a standing approval whose passkey assertion answered
    standing_challenge(standing_binding(...), *nonce*); None when an active one
    exists for the same schedule content. Returns its id."""
    if not 1 <= int(runs) <= STANDING_MAX_RUNS or not 0 < float(days) <= STANDING_MAX_DAYS:
        raise ValueError("runs must be 1-%d and days at most %d"
                         % (STANDING_MAX_RUNS, STANDING_MAX_DAYS))
    subject = standing_subject(schedule, sha256)
    now = db.now_ms()
    with db.transaction(conn):
        if _active(conn, "standing", subject, now) is not None:
            return None
        cur = conn.execute(
            "INSERT INTO approvals (kind, subject, groups, approver, binding, assertion,"
            " challenge, created_ms, expires_ms, runs_left) VALUES"
            " ('standing', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (subject, json.dumps(sorted(groups)), approver,
             json.dumps({"schedule": schedule, "sha256": sha256, "runs": int(runs),
                         "days": float(days), "nonce": nonce.hex(),
                         "hashed": standing_binding(schedule, sha256, groups, runs, days)}),
             _assertion_text(assertion),
             standing_challenge(standing_binding(schedule, sha256, groups, runs, days),
                                nonce).hex(), now,
             now + int(float(days) * 86400 * 1000), int(runs)))
        events.append(conn, "approval.granted", "approval:%d" % cur.lastrowid, _actor(approver),
                      {"approval": cur.lastrowid, "kind": "standing", "schedule": schedule,
                       "sha256": sha256, "groups": sorted(groups), "runs": int(runs),
                       "days": float(days)})
    return cur.lastrowid


def use_standing(conn, schedule, sha256, groups, run):
    """Take one run of the active standing approval for this schedule content,
    covering *groups*. Returns the approval row (before the run was taken), or
    None when there is none, it does not cover the groups, or no run is left."""
    if not groups:
        return None
    with db.transaction(conn):
        r = _active(conn, "standing", standing_subject(schedule, sha256))
        if r is None or not set(groups) <= set(json.loads(r["groups"])):
            return None
        cur = conn.execute("UPDATE approvals SET runs_left = runs_left - 1 WHERE id = ?"
                           " AND runs_left > 0", (r["id"],))
        if cur.rowcount != 1:
            return None
        events.append(conn, "approval.used", "approval:%d" % r["id"], events.WORKER,
                      {"approval": r["id"], "kind": "standing", "run": run,
                       "runs_left": r["runs_left"] - 1})
    return r


# --- a batch of queued builds ----------------------------------------------------

def queue_binding(batch, community, groups, digests):
    """The exact text a queued batch's passkey challenge is computed from: the
    builds that certify, by digest (build_queue.digest)."""
    return json.dumps(["queue", batch, community, sorted(groups), list(digests)])


def queue_subject(batch):
    return "queue:%s" % batch


def grant_queue(conn, batch, community, groups, digests, approver, assertion, nonce):
    """Within the caller's transaction: record the standing approval of a
    queued batch whose passkey assertion answered
    standing_challenge(queue_binding(...), *nonce*), one run per build, for
    QUEUE_DAYS, and give the builds started already their pre-approvals. None
    when the batch has one, else (its id, the pipelines pre-approved)."""
    binding = queue_binding(batch, community, groups, digests)
    now = db.now_ms()
    if _active(conn, "standing", queue_subject(batch), now) is not None:
        return None
    cur = conn.execute(
        "INSERT INTO approvals (kind, subject, groups, approver, binding, assertion,"
        " challenge, created_ms, expires_ms, runs_left) VALUES"
        " ('standing', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (queue_subject(batch), json.dumps(sorted(groups)), approver,
         json.dumps({"queue": batch, "community": community, "digests": list(digests),
                     "nonce": nonce.hex(), "hashed": binding}),
         _assertion_text(assertion), standing_challenge(binding, nonce).hex(), now,
         now + QUEUE_DAYS * 86400 * 1000, len(digests)))
    events.append(conn, "approval.granted", "approval:%d" % cur.lastrowid, _actor(approver),
                  {"approval": cur.lastrowid, "kind": "standing", "queue": batch,
                   "groups": sorted(groups), "runs": len(digests)})
    started = conn.execute("SELECT * FROM build_queue WHERE batch = ? AND state = 'dispatched'"
                           " ORDER BY id", (batch,)).fetchall()
    return cur.lastrowid, [r["pipeline_id"] for r in started if preapprove_queued(conn, r)]


def preapprove_queued(conn, entry):
    """Within the caller's transaction: a queued build that certifies, started
    as entry["pipeline_id"], takes one run of its batch's approval in force,
    which becomes the console pre-approval of that pipeline (the batch's
    assertion and challenge, the build in its binding). Returns the
    pre-approval's id, or None (no approval, not this build, already done)."""
    pid = entry["pipeline_id"]
    if not entry["certify"] or pid is None:
        return None
    r = _active(conn, "standing", queue_subject(entry["batch"]))
    if r is None or entry["digest"] not in json.loads(r["binding"]).get("digests", []) \
            or _active(conn, "preapproval", str(pid)) is not None:
        return None
    if conn.execute("UPDATE approvals SET runs_left = runs_left - 1 WHERE id = ? AND runs_left > 0",
                    (r["id"],)).rowcount != 1:
        return None
    now = db.now_ms()
    cur = conn.execute(
        "INSERT INTO approvals (kind, subject, groups, approver, via, binding, assertion,"
        " challenge, created_ms, expires_ms, sign_cap) VALUES"
        " ('preapproval', ?, ?, ?, 'console', ?, ?, ?, ?, ?, ?)",
        (str(pid), r["groups"], r["approver"],
         json.dumps({"queue": entry["batch"], "entry": entry["id"], "approval": r["id"]}),
         r["assertion"], r["challenge"], now, now + PREAPPROVAL_TTL_MS, PREAPPROVAL_SIGN_CAP))
    conn.execute("UPDATE build_queue SET approval_id = ? WHERE id = ?", (r["id"], entry["id"]))
    events.append(conn, "approval.used", "approval:%d" % r["id"], events.WORKER,
                  {"approval": r["id"], "kind": "standing", "queue": entry["batch"],
                   "entry": entry["id"], "runs_left": r["runs_left"] - 1})
    events.append(conn, "approval.granted", "approval:%d" % cur.lastrowid,
                  _actor(r["approver"]),
                  {"approval": cur.lastrowid, "kind": "preapproval", "build_id": str(pid),
                   "groups": json.loads(r["groups"]), "via": "queue", "from": r["id"]},
                  cause="pipeline:%d" % int(pid))
    return cur.lastrowid


def release_preapproval(conn, pipeline_id):
    """Within the caller's transaction: the pre-approval a queued build's
    pipeline took, when GitLab dropped that pipeline unstarted, is revoked (a
    retry of it certifies nothing) and its run given back to the batch's
    approval, for the build's next start. Returns whether there was one."""
    r = _active(conn, "preapproval", str(pipeline_id))
    if r is None:
        return False
    source = json.loads(r["binding"] or "{}").get("approval")
    conn.execute("UPDATE approvals SET revoked_ms = ?, revoked_by = 'worker' WHERE id = ?",
                 (db.now_ms(), r["id"]))
    if source:
        conn.execute("UPDATE approvals SET runs_left = runs_left + 1 WHERE id = ?", (int(source),))
    events.append(conn, "approval.released", "approval:%d" % r["id"], events.WORKER,
                  {"approval": r["id"], "build_id": str(pipeline_id), "from": source},
                  cause="pipeline:%d" % int(pipeline_id))
    return True


def revoke(conn, approval_id, by):
    """Revoke an approval, and the pre-approvals a queued batch's gave; False
    when it is unknown or already revoked."""
    with db.transaction(conn):
        cur = conn.execute("UPDATE approvals SET revoked_ms = ?, revoked_by = ? WHERE id = ?"
                           " AND revoked_ms IS NULL", (db.now_ms(), by, int(approval_id)))
        if cur.rowcount != 1:
            return False
        events.append(conn, "approval.revoked", "approval:%d" % int(approval_id), _actor(by),
                      {"approval": int(approval_id)})
        for r in conn.execute("SELECT id FROM approvals WHERE kind = 'preapproval' AND"
                              " revoked_ms IS NULL AND json_extract(binding, '$.approval') = ?",
                              (int(approval_id),)).fetchall():
            conn.execute("UPDATE approvals SET revoked_ms = ?, revoked_by = ? WHERE id = ?",
                         (db.now_ms(), by, r["id"]))
            events.append(conn, "approval.revoked", "approval:%d" % r["id"], _actor(by),
                          {"approval": r["id"], "with": int(approval_id)})
    return True


def get(conn, approval_id):
    return conn.execute("SELECT * FROM approvals WHERE id = ?", (int(approval_id),)).fetchone()


def listing(conn, kind=None, active_only=True, limit=200, communities=None, user=None):
    """Newest first. *communities* (lowercase; None = all): only approvals with
    one of those groups, or approved by *user*."""
    q, args = "SELECT * FROM approvals WHERE 1=1", []
    if communities is not None:
        names = sorted(communities)
        q += (" AND (approver = ? OR EXISTS (SELECT 1 FROM json_each(approvals.groups) j"
              " WHERE lower(j.value) IN (%s)))" % ",".join("?" * len(names)) if names
              else " AND approver = ?")
        args.append(user or "")
        args.extend(names)
    if kind:
        q += " AND kind = ?"
        args.append(kind)
    if active_only:
        q += " AND revoked_ms IS NULL AND expires_ms > ? AND (runs_left IS NULL OR runs_left > 0)"
        args.append(db.now_ms())
    q += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    return conn.execute(q, args).fetchall()
