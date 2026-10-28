# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Commit CI (ADR bits 0009): a schedule may follow branches, its watch list
(url@branch;...). Every WATCH_S the worker reads each branch's head with git's
own protocol over HTTPS (protocol v2 ls-refs: no git binary, no API quota),
keeping the heads it read and those it last built (schedule_watches). A
schedule whose heads changed fires a commit run of every cell once they have
held still for SETTLE_MS (or have waited MAX_WAIT_MS) and none of its commit
runs is still queued or running; nothing is cancelled. A branch first seen (or
seen again) is not built: only a change is. Commit runs certify and publish
nothing, and record the heads when they fired: a build reads its branches then.
"""

import datetime as dt
import ipaddress
import json
import logging
import socket
import time
import urllib.parse

import requests

from . import db, events, nightlies, schedules

log = logging.getLogger("bits_orchestrator.watches")

SETTLE_MS = 5 * 60 * 1000          # a head unchanged this long is a burst of pushes ended
MAX_WAIT_MS = 30 * 60 * 1000       # a branch pushed to all the time still builds this often
_CHECKED, _FAILED = "watches.checked_ms", "watches.failed"
_PASS_S = 120                      # a pass reads no longer: the rest wait for the next
_DEADLINE_S = 15                   # a whole answer, at most this long
_MAX_BYTES = 64 * 1024             # ... and this big: refs under one branch name
_HEX = frozenset("0123456789abcdef")


class WatchError(Exception):
    """A repository whose heads cannot be read now (it is tried again)."""


def _pkt(text):
    b = text.encode()
    return b"%04x" % (len(b) + 4) + b


def _no_auth(r):                   # not the worker's ~/.netrc: these are anyone's repositories
    return r


def _public(url):
    """The upload-pack URL of *url*; WatchError unless its host is public (DNS is
    read again on connect: this keeps out a mistyped or internal host, not a
    rebinding one, which only sees an ls-refs request)."""
    u = urllib.parse.urlsplit(url)
    try:
        port = u.port or 443
    except ValueError:
        raise WatchError("%s: not a port" % url)
    if u.scheme != "https" or not u.hostname:
        raise WatchError("%s: not an https URL" % url)
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(u.hostname, port, proto=socket.IPPROTO_TCP)}
    except (OSError, UnicodeError) as e:
        raise WatchError("%s: %s" % (url, e))
    for a in addrs:
        if not ipaddress.ip_address(a.split("%")[0]).is_global:
            raise WatchError("%s: %s is not a public address" % (url, a))
    path = u.path.rstrip("/")
    return urllib.parse.urlunsplit(("https", u.netloc, (path if path.endswith(".git") else path + ".git")
                                    + "/git-upload-pack", "", ""))


def _body(r, url):
    data, until = b"", time.monotonic() + _DEADLINE_S
    try:
        for chunk in r.iter_content(8192):
            data += chunk
            if len(data) > _MAX_BYTES:
                raise WatchError("%s: an answer over %d bytes" % (url, _MAX_BYTES))
            if time.monotonic() > until:
                raise WatchError("%s: no whole answer in %ds" % (url, _DEADLINE_S))
    except requests.RequestException as e:
        raise WatchError("%s: %s" % (url, str(e)[:200]))
    finally:
        r.close()
    return data


def _refs(data, url):
    """{ref: sha} of an ls-refs answer; WatchError for anything else."""
    out, i = {}, 0
    while True:
        size = data[i:i + 4]
        n = int(size, 16) if len(size) == 4 and set(size) <= set(b"0123456789abcdef") else -1
        if n == 0 and i + 4 == len(data):           # the flush that ends the list
            return out
        if n < 5 or i + n > len(data):
            raise WatchError("%s: not a whole git protocol v2 answer" % url)
        line = data[i + 4:i + n].decode("utf-8", "replace").rstrip("\n")
        if line.startswith("ERR "):
            raise WatchError("%s: %s" % (url, line[4:200]))
        words = line.split(" ")
        if len(words) < 2 or len(words[0]) not in (40, 64) or not set(words[0]) <= _HEX:
            raise WatchError("%s: not a git protocol v2 answer" % url)
        out[words[1]] = words[0]
        i += n


def head(url, branch):
    """The commit at the head of *branch* of the repository at *url* (https, a
    public host), or '' when it has no such branch."""
    target, ref = _public(url), "refs/heads/" + branch
    body = _pkt("command=ls-refs\n") + b"0001" + _pkt("ref-prefix %s\n" % ref) + b"0000"
    try:
        r = requests.post(target, data=body, timeout=(5, 10), stream=True, allow_redirects=False,
                          auth=_no_auth, headers={
                              "Content-Type": "application/x-git-upload-pack-request",
                              "Accept": "application/x-git-upload-pack-result", "Git-Protocol": "version=2",
                              "Accept-Encoding": "identity", "User-Agent": "git/2 bits-orchestrator"})
    except requests.RequestException as e:
        raise WatchError("%s: %s" % (url, str(e)[:200]))
    if r.status_code != 200:
        r.close()
        raise WatchError("%s: HTTP %s" % (url, r.status_code))
    return _refs(_body(r, url), url).get(ref, "")


def _kv(conn, key):
    r = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def read(conn, now=None, every_s=120, get_head=head):
    """Read the heads of every enabled schedule's watch list, at most every
    *every_s*. Returns how many heads changed; WatchError after the others were
    read when some could not be (and on each pass until a read is due)."""
    now = now or db.now_ms()
    last = _kv(conn, _CHECKED)
    if last is not None and now - int(last) < every_s * 1000:
        if _kv(conn, _FAILED):                        # a step failing until the next read, not on and off
            raise WatchError(_kv(conn, _FAILED))
        return 0
    seen, failed, changed, want, bad = {}, [], 0, {}, set()
    until = time.monotonic() + _PASS_S
    for s in schedules.listing(conn):
        if s["enabled"] and schedules.watches(s):
            want[s["id"]] = (s, schedules.watches(s))
    for _s, ws in want.values():
        for w in ws:
            if w in seen:
                continue
            seen[w] = None                            # not read now: as it was
            if w[0] in bad or time.monotonic() > until:
                continue                              # a repository that failed, or no time left
            try:
                seen[w] = get_head(*w)
            except Exception as e:                    # anything: the other repositories are read
                bad.add(w[0])
                failed.append(str(e) if isinstance(e, WatchError) else "%s: %r" % (w[0], e))
    if time.monotonic() > until:
        failed.append("not all branches read in %ds: the rest on the next read" % _PASS_S)
    with db.transaction(conn):
        for sid in {r[0] for r in conn.execute("SELECT DISTINCT schedule_id FROM schedule_watches")} - set(want):
            conn.execute("DELETE FROM schedule_watches WHERE schedule_id = ?", (sid,))
        for sid, (s, ws) in want.items():
            have = {(r["repo"], r["branch"]): r for r in conn.execute(
                "SELECT * FROM schedule_watches WHERE schedule_id = ?", (sid,))}
            for w in set(have) - set(ws):
                conn.execute("DELETE FROM schedule_watches WHERE schedule_id = ? AND repo = ? AND branch = ?",
                             (sid,) + w)
            for w in ws:
                sha, old = seen[w], have.get(w)
                if sha is None or (old is not None and old["sha"] == sha):
                    continue
                if old is None:                       # first seen: its head is the one built
                    conn.execute("INSERT INTO schedule_watches (schedule_id, repo, branch, sha, built_sha,"
                                 " changed_ms) VALUES (?, ?, ?, ?, ?, ?)", (sid,) + w + (sha, sha, now))
                    continue
                changed += 1
                again = not old["sha"]                # gone and back: seen again, not built
                moved = old["moved_ms"] if old["sha"] != old["built_sha"] and old["moved_ms"] else now
                conn.execute("UPDATE schedule_watches SET sha = ?, built_sha = ?, changed_ms = ?, moved_ms = ?"
                             " WHERE schedule_id = ? AND repo = ? AND branch = ?",
                             (sha, sha if again else old["built_sha"], now, moved, sid) + w)
                events.append(conn, "schedule.head_moved", nightlies.subject(sid), events.WORKER,
                              {"repo": w[0], "branch": w[1], "from": old["sha"], "to": sha})
        for k, v in ((_CHECKED, str(now)), (_FAILED, "; ".join(failed[:5]))):
            conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                         " SET value = excluded.value", (k, v))
    if failed:
        raise WatchError("; ".join(failed[:5]))
    return changed


def ready(conn, s, now):
    """The heads to build (JSON) when *s* should fire a commit run now, else None."""
    want = set(schedules.watches(s))
    rows = [r for r in conn.execute("SELECT * FROM schedule_watches WHERE schedule_id = ?", (s["id"],))
            if (r["repo"], r["branch"]) in want and r["sha"]]     # a branch gone holds nothing
    moved = [r for r in rows if r["sha"] != r["built_sha"]]
    if not moved:
        return None
    if (any(now - r["changed_ms"] < SETTLE_MS for r in moved)
            and now - min(r["moved_ms"] for r in moved) < MAX_WAIT_MS):
        return None                                   # still moving
    if conn.execute("SELECT 1 FROM schedule_runs WHERE schedule_id = ? AND fired_by = 'commit'"
                    " AND state IN ('queued', 'running') LIMIT 1", (s["id"],)).fetchone():
        return None                                   # one goes on: after it
    return json.dumps({"%s@%s" % (r["repo"], r["branch"]): r["sha"] for r in rows}, sort_keys=True)


def step(conn, reader, ref, tz, now=None, every_s=120, get_head=head):
    """The worker step: read the heads, then fire the commit runs that are ready."""
    now = now or db.now_ms()
    errors = []
    try:
        read(conn, now, every_s, get_head)
    except WatchError as e:
        errors.append(str(e))                         # fire on what was read; say so after
    cfgs, fired = {}, 0
    for s in schedules.listing(conn):
        if not s["enabled"] or not schedules.watches(s) or ready(conn, s, now) is None:
            continue
        if s["community_dir"] not in cfgs:            # read before the write lock
            try:
                cfgs[s["community_dir"]] = nightlies.config(reader, s["community_dir"], ref)
            except Exception as e:                    # an unreadable config: the next pass
                errors.append("schedule %s: the console's configuration: %s" % (s["id"], e))
                continue
        with db.transaction(conn):
            fresh = schedules.get(conn, s["id"])
            heads = ready(conn, fresh, now) if fresh and fresh["enabled"] else None
            if heads is None:
                continue
            night = dt.datetime.fromtimestamp(now / 1000, tz).date().isoformat()
            nightlies.fire_night(conn, fresh, night, schedules.cells(fresh), cfgs[s["community_dir"]], ref,
                                 "commit", now, fired_by="commit", commits=heads)
            for w, sha in json.loads(heads).items():
                repo, branch = w.rsplit("@", 1)
                conn.execute("UPDATE schedule_watches SET built_sha = ?, moved_ms = 0 WHERE schedule_id = ?"
                             " AND repo = ? AND branch = ?", (sha, s["id"], repo, branch))
            fired += 1
    if errors:
        raise WatchError("; ".join(errors[:5]))
    return fired


def heads(conn, sid):
    """What a schedule follows, as last read: [{repo, branch, sha, built_sha, changed_ms}]."""
    return [{k: r[k] for k in ("repo", "branch", "sha", "built_sha", "changed_ms")} for r in conn.execute(
        "SELECT * FROM schedule_watches WHERE schedule_id = ? ORDER BY repo, branch", (int(sid),))]
