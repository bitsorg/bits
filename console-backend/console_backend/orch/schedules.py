# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Nightly schedules (ADR bits 0007): what a community builds every night, on
which platforms, on which weekdays at what time of the bits instance's time zone.

A schedule (a row of the nightly matrix) builds one package at one version, with
one defaults chain and recipe repositories, on several platforms, and each of
its variants (ADR bits 0009: defaults chains put before its own). Its content
hash covers what is built and where it goes, not when: a standing approval is
bound to it (approvals.standing_subject("schedule:<id>", sha256)), so moving a
nightly to another time keeps its approval and adding a platform needs a new one.
Every change is an event with what it was before and after.
"""

import datetime as dt
import hashlib
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import db, events

# Matched whole (fullmatch), ASCII only: these become CI variables and bits arguments.
_RE = lambda p: re.compile(p, re.ASCII)
_NAME = _RE(r"[A-Za-z0-9][A-Za-z0-9 ._+()-]{0,99}")
_PKG = _RE(r"[A-Za-z0-9._+-]{1,100}")
_VERSION = _RE(r"[A-Za-z0-9._+-]{0,100}")
DEFAULTS_RE = _RE(r"[A-Za-z0-9._:+-]{0,200}")
_PLATFORM = _RE(r"[A-Za-z0-9._-]{1,64}")
_REPO = _RE(r"https://[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._~/-]+(@[A-Za-z0-9._/][A-Za-z0-9._/-]*)?")
_REPOS = _RE(r"[^\s]{0,2000}")
_SET_KEY = _RE(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
_SET_VALUE = _RE(r"[A-Za-z0-9._:/@+=-]{0,200}")
_ARGS = _RE(r"[A-Za-z0-9 \t=_.,/:@#+%^-]{0,500}")   # lib-pipeline-dispatch.sh's, spaces as such
_GROUP = _RE(r"[a-z0-9._-]{1,64}")
_DIR = _RE(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TIME = _RE(r"([01][0-9]|2[0-3]):[0-5][0-9]")
MAX_PLATFORMS = 32
MAX_VARIANTS = 16
MAX_CELLS = 64                     # platforms x variants: the builds of one night
MAX_CHAIN = 160                    # a variant::defaults, leaving room for a community's overlay
EVERY_DAY = 0b1111111              # bit 0 Monday ... bit 6 Sunday


class Invalid(ValueError):
    """A schedule that cannot be stored: why, for the person editing it."""


class Conflict(Exception):
    """The schedule changed since it was read (another revision)."""


def time_zone():
    """The bits instance's time zone (site configuration console.time_zone)."""
    from bits_helpers import site
    name = str(site.get("console.time_zone") or "UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise Invalid("console.time_zone %r is not a time zone" % name)


# --- what a schedule is -------------------------------------------------------

def _flag(p, key, default=False):
    v = p.get(key, default)
    if not isinstance(v, bool):
        raise Invalid("%s must be true or false" % key)
    return v


def _text(p, key, pattern, what):
    v = p.get(key, "")
    v = "" if v is None else v
    if not isinstance(v, str) or not pattern.fullmatch(v.strip()):
        raise Invalid("%s: %s" % (key, what))
    return v.strip()


def check(p, community):
    """The stored fields of a schedule from the request *p*, or Invalid."""
    if not isinstance(p, dict):
        raise Invalid("expected a JSON object")
    # community_dir: the community's directory in the console (communities/<dir>/).
    s = {"community": community.strip().lower(), "community_dir": community.strip()}
    if not _DIR.fullmatch(s["community_dir"]):
        raise Invalid("community: not a community name")
    s["name"] = _text(p, "name", _NAME, "1-100 letters, digits, spaces and ._+()-")
    s["package"] = _text(p, "package", _PKG, "a package name")
    s["version"] = _text(p, "version", _VERSION, "a version, or empty for the recipe's")
    s["defaults"] = _text(p, "defaults", DEFAULTS_RE, "a defaults chain (a::b), or empty")
    repos = [r for r in _text(p, "extra_repos", _REPOS, "url@branch;...").split(";") if r]
    for r in repos:
        if not _REPO.fullmatch(r):
            raise Invalid("extra_repos: %r is not https://host/path[@branch]" % r)
    s["extra_repos"] = ";".join(repos)
    sv = p.get("set") or {}
    if not isinstance(sv, dict) or len(sv) > 32 or not all(
            isinstance(k, str) and _SET_KEY.fullmatch(k) and isinstance(v, str) and _SET_VALUE.fullmatch(v)
            for k, v in sv.items()):
        raise Invalid("set: up to 32 variables, name=value without spaces")
    s["set"] = dict(sorted(sv.items()))
    plats = p.get("platforms")
    if not isinstance(plats, list) or not 0 < len(plats) <= MAX_PLATFORMS or not all(
            isinstance(x, str) and _PLATFORM.fullmatch(x) for x in plats) or len(set(plats)) != len(plats):
        raise Invalid("platforms: 1 to %d different platform names" % MAX_PLATFORMS)
    s["platforms"] = sorted(plats)
    var = p.get("variants") or []
    if not isinstance(var, list) or not all(isinstance(v, str) and DEFAULTS_RE.fullmatch(v) for v in var):
        raise Invalid("variants: defaults chains (a::b)")
    var = ["::".join(x for x in v.split("::") if x) for v in var]          # "a::" is "a"
    if len(var) > MAX_VARIANTS or not all(var) or len(set(var)) != len(var):
        raise Invalid("variants: up to %d different defaults chains (a::b)" % MAX_VARIANTS)
    if any(len(v) + 2 + len(s["defaults"]) > MAX_CHAIN for v in var):
        raise Invalid("variants: a variant and the defaults together over %d characters" % MAX_CHAIN)
    s["variants"] = sorted(var)
    if len(plats) * max(1, len(var)) > MAX_CELLS:
        raise Invalid("platforms x variants: at most %d builds a night" % MAX_CELLS)
    s["rebuild"], s["debug"] = _flag(p, "rebuild"), _flag(p, "debug")
    jobs = p.get("jobs", 0)
    if isinstance(jobs, bool) or not isinstance(jobs, int) or not 0 <= jobs <= 256:
        raise Invalid("jobs: 0 (the build host's) to 256")
    s["jobs"] = jobs
    s["extra_build_args"] = _text(p, "extra_build_args", _ARGS, "bits build arguments without quotes")
    if re.search(r"(^|\s)--defaults(=|\s|$)", s["extra_build_args"]):
        raise Invalid("extra_build_args: give the defaults in defaults (the build job resolves its CVMFS target with them)")
    days = p.get("days", EVERY_DAY)
    if isinstance(days, bool) or not isinstance(days, int) or not 0 < days <= EVERY_DAY:
        raise Invalid("days: a weekday mask, 1 (Monday) to 127 (every day)")
    s["days"] = days
    s["time"] = _text(p, "time", _TIME, "HH:MM")
    s["certify"], s["cvmfs"] = _flag(p, "certify"), _flag(p, "cvmfs")
    groups = p.get("certify_groups") or [s["community"]]
    if not isinstance(groups, list) or not 0 < len(groups) <= 8 or not all(
            isinstance(g, str) and _GROUP.fullmatch(g) for g in groups):
        raise Invalid("certify_groups: 1 to 8 group names")
    s["certify_groups"] = sorted(set(groups))
    s["enabled"] = _flag(p, "enabled", True)
    return s


_CONTENT = ("community", "package", "version", "defaults", "extra_repos", "set", "platforms",
            "rebuild", "debug", "jobs", "extra_build_args", "certify", "cvmfs", "certify_groups")


def content_sha256(s):
    """What is built and where it goes, not when (days, time) or whether (enabled).
    Variants count only when there are some: a schedule without keeps its hash."""
    content = {k: s[k] for k in _CONTENT}
    if s.get("variants"):
        content["variants"] = s["variants"]
    text = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def cells(s):
    """[(platform, variant)] of a night: every platform x every variant ('' when none)."""
    return [(p, v) for p in s["platforms"] for v in (s.get("variants") or [""])]


def cell_key(platform, variant):
    """A cell's name in the matrix: its platform, and its variant if it has one."""
    return "%s %s" % (platform, variant) if variant else platform


# --- when it runs ---------------------------------------------------------------

def _local(day, hhmm, tz):
    """The moment of *hhmm* on local *day*: a time skipped in spring is the first
    valid minute after it, one repeated in autumn its first occurrence."""
    h, m = (int(x) for x in hhmm.split(":"))
    naive = dt.datetime(day.year, day.month, day.day, h, m)
    for step in range(0, 180):
        t = naive + dt.timedelta(minutes=step)
        aware = t.replace(tzinfo=tz, fold=0)
        if aware.astimezone(dt.timezone.utc).astimezone(tz).replace(tzinfo=None) == t:
            return aware
    return naive.replace(tzinfo=tz)


def occurrences(s, after_ms, before_ms, tz):
    """[(night, ms)] of the schedule's times in (after_ms, before_ms], oldest
    first: night is the local date (ISO) of its scheduled time."""
    start = dt.datetime.fromtimestamp(after_ms / 1000, tz).date() - dt.timedelta(days=1)
    end = dt.datetime.fromtimestamp(before_ms / 1000, tz).date() + dt.timedelta(days=1)
    out, day = [], start
    while day <= end:
        if s["days"] >> day.weekday() & 1:
            ms = int(_local(day, s["time"], tz).timestamp() * 1000)
            if after_ms < ms <= before_ms:
                out.append((day.isoformat(), ms))
        day += dt.timedelta(days=1)
    return out


def next_run_ms(s, now_ms, tz):
    """The schedule's next time after *now_ms*, or None when it never runs."""
    found = occurrences(s, now_ms, now_ms + 8 * 86400 * 1000, tz)
    return found[0][1] if found else None


# --- the rows -------------------------------------------------------------------

def _row(r):
    d = dict(r)
    for k in ("set", "platforms", "certify_groups", "variants"):
        d[k] = json.loads(d.pop("set_vars") if k == "set" else d[k])
    for k in ("rebuild", "debug", "certify", "cvmfs", "enabled"):
        d[k] = bool(d[k])
    return d


def get(conn, sid):
    r = conn.execute("SELECT * FROM schedules WHERE id = ? AND deleted_ms = 0", (int(sid),)).fetchone()
    return _row(r) if r else None


def listing(conn, communities=None):
    """The schedules of *communities* (lowercase; None = all), by community and name."""
    q, args = "SELECT * FROM schedules WHERE deleted_ms = 0", []
    if communities is not None:
        names = sorted(communities)
        if not names:
            return []
        q += " AND community IN (%s)" % ",".join("?" * len(names))
        args.extend(names)
    return [_row(r) for r in conn.execute(q + " ORDER BY community, name, id", args)]


def _public(s):
    return {k: v for k, v in s.items() if k not in ("deleted_ms",)}


def _store(conn, s, sid=None):
    cols = ("community", "community_dir", "name", "package", "version", "defaults", "extra_repos", "set_vars",
            "platforms", "rebuild", "debug", "jobs", "extra_build_args", "days", "time", "certify",
            "cvmfs", "certify_groups", "enabled", "variants", "content_sha256")
    vals = (s["community"], s["community_dir"], s["name"], s["package"], s["version"], s["defaults"], s["extra_repos"],
            json.dumps(s["set"]), json.dumps(s["platforms"]), int(s["rebuild"]), int(s["debug"]),
            s["jobs"], s["extra_build_args"], s["days"], s["time"], int(s["certify"]),
            int(s["cvmfs"]), json.dumps(s["certify_groups"]), int(s["enabled"]),
            json.dumps(s["variants"]), content_sha256(s))
    return cols, vals


def _taken(conn, s, sid=None):
    r = conn.execute("SELECT id FROM schedules WHERE community = ? AND name = ? AND deleted_ms = 0"
                     " AND id != ?", (s["community"], s["name"], int(sid or 0))).fetchone()
    if r:
        raise Invalid("name: the community has a schedule %r already" % s["name"])


def create(conn, s, user):
    """Store a checked schedule (check); returns it."""
    now = db.now_ms()
    with db.transaction(conn):
        _taken(conn, s)
        cols, vals = _store(conn, s)
        cur = conn.execute("INSERT INTO schedules (%s, revision, last_fired_ms, created_by,"
                           " created_ms, updated_by, updated_ms) VALUES (%s, 1, ?, ?, ?, ?, ?)"
                           % (", ".join(cols), ", ".join("?" * len(cols))),
                           vals + (now, user, now, user, now))
        new = get(conn, cur.lastrowid)
        events.append(conn, "schedule.created", "schedule:%d" % new["id"],
                      events.Actor("person", user), {"after": _public(new)})
    return new


def update(conn, sid, s, user, revision):
    """Replace a schedule's fields; Conflict when it is no longer at *revision*.
    Pause and resume alone change whether it runs, and a new time fires nothing
    that went by before the change."""
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise Invalid("revision: the revision the change is made to")
    now = db.now_ms()
    with db.transaction(conn):
        old = get(conn, sid)
        if old is None:
            return None
        if s["community"] != old["community"]:
            raise Invalid("community: a schedule stays in its community")
        if revision != old["revision"]:
            raise Conflict("changed since: revision %d, not %d" % (old["revision"], revision))
        _taken(conn, s, sid)
        cols, vals = _store(conn, dict(s, enabled=old["enabled"], community_dir=old["community_dir"]))
        conn.execute("UPDATE schedules SET %s, revision = revision + 1, updated_by = ?,"
                     " updated_ms = ?, last_fired_ms = MAX(last_fired_ms, ?) WHERE id = ?"
                     % ", ".join("%s = ?" % c for c in cols), vals + (user, now, now, int(sid)))
        new = get(conn, sid)
        events.append(conn, "schedule.updated", "schedule:%d" % new["id"],
                      events.Actor("person", user), {"before": _public(old), "after": _public(new)})
    return new


def set_enabled(conn, sid, enabled, user):
    """Pause or resume; the schedule, or None when there is none (unchanged when
    it already is so: no event)."""
    now = db.now_ms()
    with db.transaction(conn):
        old = get(conn, sid)
        if old is None:
            return None
        if old["enabled"] != bool(enabled):
            # Resumed: from now on, no night missed while it was paused.
            conn.execute("UPDATE schedules SET enabled = ?, revision = revision + 1, updated_by = ?,"
                         " updated_ms = ?, last_fired_ms = MAX(last_fired_ms, ?) WHERE id = ?",
                         (int(bool(enabled)), user, now, now if enabled else 0, int(sid)))
            events.append(conn, "schedule.resumed" if enabled else "schedule.paused",
                          "schedule:%d" % old["id"], events.Actor("person", user),
                          {"before": {"enabled": old["enabled"]}, "after": {"enabled": bool(enabled)}})
        return get(conn, sid)


def delete(conn, sid, user):
    """Gone from the matrix and from firing; its runs stay. False when there is none."""
    now = db.now_ms()
    with db.transaction(conn):
        old = get(conn, sid)
        if old is None:
            return False
        conn.execute("UPDATE schedules SET deleted_ms = ?, enabled = 0, updated_by = ?, updated_ms = ?"
                     " WHERE id = ?", (now, user, now, int(sid)))
        events.append(conn, "schedule.deleted", "schedule:%d" % old["id"],
                      events.Actor("person", user), {"before": _public(old)})
    return True
