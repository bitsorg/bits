# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The outside copy of the audit chain (M4).

The worker sends every event to the audit-log service on another node
(bits-services/audit-log, on cvmfs-bits-01). That service keeps only what links
onto the chain it already has, and has no way to change or remove an event, so
once an event is there a rewrite of this host's database shows.

Each pass the worker asks the service for its head, the last event it holds:

  * that event must be this database's event of the same seq, with the same
    hash. If not, the two chains have diverged (this database was rewritten, or
    restored from a copy older than the outside one), or the service refused a
    batch: the worker records outside.diverged, stops sending, and GET
    /ops/outside reports it until someone resolves the cause and runs
    `outside clear --by <user>`;
  * then it sends the events after the head, in batches.

    python -m console_backend.orch.outside verify [--db PATH]

walks the outside copy and this database side by side and reports the first
event where they differ; `status` shows the last head and any alarm.
"""

import argparse
import json
import logging
import os
import sys

import requests

from . import db, events

log = logging.getLogger("bits_orchestrator.outside")

BATCH = 500
BATCH_BYTES = 4 << 20         # the service takes at most 8 MB a request
MAX_BATCHES = 10
FULL_VERIFY_MS = 24 * 3600 * 1000
ALARM = "outside.alarm"


class OutsideError(Exception):
    pass


def _token(value):
    """The token, or the contents of the file it names."""
    if value and value.startswith("/"):
        if not os.path.isfile(value):
            raise OutsideError("BITS_ORCH_AUDIT_TOKEN names %s, which is not a file" % value)
        with open(value) as fh:
            return fh.read().strip()
    return value


def _local_http(url):
    """http to localhost, or to a single-label host name: a compose service on
    an internal network (audit-log on this host), never one across the network."""
    from urllib.parse import urlparse
    u = urlparse(url)
    host = u.hostname or ""
    return u.scheme == "http" and (host in ("localhost", "127.0.0.1") or (host and "." not in host))


class Client:
    """HTTP client of the audit-log service."""

    def __init__(self, url, token, cafile=None, timeout=20, session=None):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": "Bearer " + _token(token)}
        self.verify = cafile or True
        self.timeout = timeout
        self.http = session or requests

    @classmethod
    def from_settings(cls, settings):
        if not settings.orch_audit_url:
            return None
        if not settings.orch_audit_token:
            raise OutsideError("BITS_ORCH_AUDIT_URL is set but BITS_ORCH_AUDIT_TOKEN is not")
        url = settings.orch_audit_url
        if not (url.startswith("https://") or _local_http(url)):
            raise OutsideError("BITS_ORCH_AUDIT_URL must be https (the token is a bearer),"
                               " or http to a container on this host's internal network")
        return cls(settings.orch_audit_url, settings.orch_audit_token,
                   settings.orch_audit_cafile or None)

    def _call(self, method, path, body=None, ok=(200,)):
        try:
            r = self.http.request(method, self.url + path, headers=self.headers, json=body,
                                  timeout=self.timeout, verify=self.verify)
        except requests.RequestException as e:
            raise OutsideError("audit log unreachable: %s" % str(e)[:200])
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code not in ok:
            raise OutsideError("audit log %s %s -> %s: %s" % (method, path, r.status_code,
                                                               str(data.get("error", ""))[:300]))
        return r.status_code, data

    def head(self):
        return self._call("GET", "/v1/head")[1]

    def send(self, rows):
        status, data = self._call("POST", "/v1/events", {"events": rows}, ok=(200, 409))
        return status == 200, data

    def events(self, after, limit=1000):
        return self._call("GET", "/v1/events?after=%d&limit=%d" % (after, limit))[1].get("events", [])


def _row(r):
    return {k: r[k] for k in ("seq", "ts_ms", "kind", "subject", "actor_kind", "actor_id",
                              "cause", "payload", "prev_hash", "hash")}


def _kv(conn, key):
    r = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def _alarm(conn, reason, head):
    if _kv(conn, ALARM):
        return
    with db.transaction(conn):
        conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                     " DO UPDATE SET value = excluded.value",
                     (ALARM, json.dumps({"reason": reason, "head": head, "ms": db.now_ms()})))
        events.append(conn, "outside.diverged", "outside", events.WORKER,
                      {"reason": reason, "outside_head": head})
    log.error("outside audit copy diverged: %s", reason)


def _check_local(conn, now=None):
    """Verify this chain before vouching for it: every pass, the events after
    the last verified one; once a day, all of it. Returns a problem or None."""
    now = now or db.now_ms()
    mark = json.loads(_kv(conn, "outside.verified") or "null") or {}
    full = now - int(mark.get("full_ms") or 0) >= FULL_VERIFY_MS
    if full:
        n, broken = events.verify(conn)
        if broken is not None:
            return "this database's chain breaks at event %d" % broken
        last = conn.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        mark = {"seq": last["seq"] if last else 0, "hash": last["hash"] if last else events.GENESIS,
                "full_ms": now}
    else:
        seq, prev = int(mark.get("seq") or 0), mark.get("hash") or events.GENESIS
        if seq:
            r = conn.execute("SELECT hash FROM events WHERE seq = ?", (seq,)).fetchone()
            if r is None or r["hash"] != prev:
                return "this database's event %d changed since it was verified" % seq
        for r in conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq", (seq,)):
            if r["seq"] != seq + 1 or r["prev_hash"] != prev or events._digest(prev, r) != r["hash"]:
                return "this database's chain breaks at event %d" % r["seq"]
            seq, prev = r["seq"], r["hash"]
        mark = dict(mark, seq=seq, hash=prev)
    with db.transaction(conn):
        conn.execute("INSERT INTO kv (key, value) VALUES ('outside.verified', ?) ON CONFLICT(key)"
                     " DO UPDATE SET value = excluded.value", (json.dumps(mark),))
    return None


def status(conn):
    alarm = _kv(conn, ALARM)
    last = conn.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
    shipped = _kv(conn, "outside.head")
    return {"local_seq": last["seq"] if last else 0,
            "outside": json.loads(shipped) if shipped else None,
            "alarm": json.loads(alarm) if alarm else None}


def ship(conn, client, batch=BATCH, max_batches=MAX_BATCHES):
    """Check the outside head against this chain, then send what follows it.
    Returns the number of events sent."""
    if client is None or _kv(conn, ALARM):
        return 0          # an alarm stops shipping until it is cleared by hand
    problem = _check_local(conn)
    if problem:
        _alarm(conn, problem, None)
        return 0
    head = client.head()
    seq, h = int(head.get("seq") or 0), str(head.get("hash") or events.GENESIS)
    last = json.loads(_kv(conn, "outside.head") or "null") or {}
    if seq < int(last.get("seq") or 0):
        _alarm(conn, "the outside copy went back from event %d to %d: it lost events"
                     % (last["seq"], seq), head)
        return 0
    if seq:
        mine = conn.execute("SELECT hash FROM events WHERE seq = ?", (seq,)).fetchone()
        if mine is None:
            _alarm(conn, "the outside copy has event %d, this database does not: it was"
                         " restored from an older copy, or rewritten" % seq, head)
            return 0
        if mine["hash"] != h:
            _alarm(conn, "event %d differs from the outside copy: this database was"
                         " rewritten" % seq, head)
            return 0
    elif h != events.GENESIS:
        _alarm(conn, "the outside copy is empty but its head is not genesis", head)
        return 0
    sent = 0
    for _ in range(max_batches):
        rows, size = [], 0
        for r in conn.execute("SELECT * FROM events WHERE seq > ? ORDER BY seq LIMIT ?",
                              (seq, batch)):
            size += len(r["payload"]) + 512
            if rows and size > BATCH_BYTES:
                break
            rows.append(_row(r))
        if not rows:
            break
        ok, data = client.send(rows)
        if not ok:
            _alarm(conn, "the outside copy refused events after %d: %s"
                   % (seq, str(data.get("error", ""))[:300]), data.get("head"))
            return sent
        sent += len(rows)
        seq, h = rows[-1]["seq"], rows[-1]["hash"]
    with db.transaction(conn):
        conn.execute("INSERT INTO kv (key, value) VALUES ('outside.head', ?) ON CONFLICT(key)"
                     " DO UPDATE SET value = excluded.value",
                     (json.dumps({"seq": seq, "hash": h, "ms": db.now_ms()}),))
    return sent


def clear(conn, who):
    """Clear the alarm once its cause is resolved, so shipping resumes."""
    alarm = _kv(conn, ALARM)
    if not alarm:
        return False
    with db.transaction(conn):
        # Forget the last head too: after a re-seeded outside copy, its head is
        # the one to follow.
        conn.execute("DELETE FROM kv WHERE key IN (?, 'outside.head')", (ALARM,))
        events.append(conn, "outside.cleared", "outside", events.Actor("person", who),
                      {"alarm": json.loads(alarm)})
    return True


def verify(conn, client, page=1000):
    """Walk both copies. Returns (checked, problem): problem is None when the
    outside copy is an intact prefix of (or equal to) this chain."""
    after, prev, checked = 0, events.GENESIS, 0
    while True:
        got = client.events(after, page)
        if not got:
            return checked, None
        for o in got:
            seq = int(o.get("seq") or 0)
            if seq != after + 1:
                return checked, "outside copy skips from %d to %d" % (after, seq)
            if o.get("prev_hash") != prev or events._digest(prev, o) != o.get("hash"):
                return checked, "outside copy's own chain breaks at %d" % seq
            mine = conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
            if mine is None:
                return checked, "event %d is outside but not here" % seq
            if _row(mine) != {k: o.get(k) for k in _row(mine)}:
                return checked, "event %d differs between here and outside" % seq
            prev, after, checked = o["hash"], seq, checked + 1


def main(argv=None):
    from .. import config
    ap = argparse.ArgumentParser(prog="python -m console_backend.orch.outside")
    ap.add_argument("command", choices=["verify", "status", "clear"])
    ap.add_argument("--db", default=None, help="the orchestrator database (default BITS_ORCH_DB)")
    ap.add_argument("--by", default=None, help="clear: who resolved it (recorded)")
    args = ap.parse_args(argv)
    settings = config.Settings()
    conn = db.open_checked(args.db or settings.orch_db)
    if args.command == "status":
        print(json.dumps(status(conn), indent=1))
        return 0
    if args.command == "clear":
        if not args.by:
            raise SystemExit("clear needs --by <user>")
        print("cleared" if clear(conn, args.by) else "no alarm")
        return 0
    client = Client.from_settings(settings)
    if client is None:
        raise SystemExit("BITS_ORCH_AUDIT_URL is not set")
    n, local_break = events.verify(conn)
    checked, problem = verify(conn, client)
    print("local chain: %d events, %s" % (n, "intact" if local_break is None
                                          else "breaks at %d" % local_break))
    print("outside copy: %d events checked, %s" % (checked, problem or "matches this chain"))
    return 0 if (problem is None and local_break is None) else 1


if __name__ == "__main__":
    sys.exit(main())
