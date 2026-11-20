# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-store's problems: its warnings and errors (GET /_admin/problems), each
folded into an issue as a failed build's failure is (failures.py), so that
fingerprints, signatures and /ops/issues serve both. An issue's source is 'store',
its package "bits-store".

collect - the problems bits-store last saw since the previous read (at most every
          COLLECT_EVERY_MS): a new one is recorded and counted on its issue; one
          seen again (its count grew) adds the difference.

The fingerprint is the problem's kind and its message with the subject (a key, a
volume, a repository) taken out, then normalised as a failure's error lines: the
same fault on many objects is one issue. Signatures read the message and
"reason:store:<kind>".
"""

import datetime
import json
import logging

from . import db, events, failures, reconcile

log = logging.getLogger("bits_orchestrator")

PACKAGE = "bits-store"
COLLECT_EVERY_MS = 5 * 60 * 1000
_SINCE, _AT = "store_problems.since", "store_problems.at"


def _iso(ms):
    """Milliseconds as issues hold times (GitLab's ISO form, UTC)."""
    t = datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (ms % 1000)


def reason(kind):
    return "store:" + kind


def fingerprint(kind, subject, message):
    """(fingerprint, key lines) of a problem: its kind, and its message without the subject."""
    text = message.replace(subject, "<subject>") if subject else message
    return failures.fingerprint(PACKAGE + ":" + kind, text, reason(kind))


def _record(conn, p, signatures):
    """Record one problem of bits-store's answer, or what it added since; returns
    (issue_id, new issue) or (None, False) when nothing changed."""
    message = failures.redact(str(p["message"]))[-failures.MAX_EXCERPT:]
    kind, subject = str(p["kind"]), str(p["subject"])
    first, last, count = int(p["first"]), int(p["last"]), int(p["count"])
    with db.transaction(conn):
        old = conn.execute("SELECT * FROM store_problems WHERE store_id = ? AND first_ms = ?",
                           (int(p["id"]), first)).fetchone()
        if old is not None:
            added = count - old["count"]
            if added <= 0:
                return None, False
            conn.execute("UPDATE store_problems SET count = ?, last_ms = ? WHERE id = ?",
                         (count, max(last, old["last_ms"]), old["id"]))
            conn.execute("UPDATE issues SET count = count + ?, last_seen = max(last_seen, ?) WHERE id = ?",
                         (added, _iso(last), old["issue_id"]))
            return old["issue_id"], False
        fp, keys = fingerprint(kind, subject, message)
        issue = conn.execute("SELECT * FROM issues WHERE fingerprint = ?", (fp,)).fetchone()
        new = issue is None
        if new:
            title = "%s: %s" % (kind, keys[0] if keys else message)
            cur = conn.execute(
                "INSERT INTO issues (fingerprint, package, title, excerpt, first_seen, last_seen,"
                " count, communities, platforms, signature_id, created_ms, source)"
                " VALUES (?,?,?,?,?,?,0,'[]','[]',?,?,'store')",
                (fp, PACKAGE, title[:300], message, _iso(first), _iso(last),
                 failures.match(signatures, message, reason(kind)), db.now_ms()))
            issue = conn.execute("SELECT * FROM issues WHERE id = ?", (cur.lastrowid,)).fetchone()
        conn.execute("UPDATE issues SET count = count + ?, first_seen = min(first_seen, ?),"
                     " last_seen = max(last_seen, ?) WHERE id = ?",
                     (count, _iso(first), _iso(last), issue["id"]))
        conn.execute(
            "INSERT INTO store_problems (store_id, first_ms, last_ms, count, level, kind, subject,"
            " message, fingerprint, issue_id, recorded_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(p["id"]), first, last, count, str(p["level"]), kind, subject, message, fp,
             issue["id"], db.now_ms()))
        if new:
            events.append(conn, "issue.opened", "issue:%d" % issue["id"], events.WORKER,
                          {"issue": issue["id"], "package": PACKAGE, "source": "store",
                           "title": issue["title"], "signature": issue["signature_id"]})
    return issue["id"], new


def collect(conn, read, now_ms=None):
    """Record bits-store's problems since the last read; *read(since)* answers
    them (store_admin.problems). Returns how many were recorded or grew; 0 when
    not due yet."""
    now = now_ms if now_ms is not None else db.now_ms()
    if now - int(reconcile._kv_get(conn, _AT) or 0) < COLLECT_EVERY_MS:
        return 0
    since = int(reconcile._kv_get(conn, _SINCE) or 0)
    signatures = failures._compiled(conn)
    changed, last = 0, since
    for p in read(since):
        try:
            issue_id, _ = _record(conn, p, signatures)
        except (KeyError, TypeError, ValueError) as exc:  # not bits-store's shape: skipped
            log.warning("bits-store problem %s: %s", json.dumps(p)[:200], exc)
            continue
        changed += issue_id is not None
        last = max(last, int(p.get("last") or 0))
    with db.transaction(conn):
        reconcile._kv_set(conn, _SINCE, str(last))  # the last one's again next time: it may grow
        reconcile._kv_set(conn, _AT, str(now))
    return changed
