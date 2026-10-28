# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The nights of the schedules (ADR bits 0007): firing them, one queued build
per platform, and their runs (schedule_runs), one per platform and night.

A schedule fires when its time has come since it last fired: one build per
platform goes to the build queue, with the variables the GitLab fan-out sends
(scripts/lib-pipeline-dispatch.sh in bits-console), resolved from the console's
config/platforms.yaml and the community's ui-config.yaml. A night missed while
the worker was down fires on its return within GRACE_MS; older ones are not
run. A platform whose previous night still waits or builds skips this one.
A certifying schedule's builds certify under its standing approval in force
(schedule:<id>@<content_sha256>); without one they build and certify nothing.
"""

import datetime as dt
import json
import logging
import re
import secrets

import yaml

from . import approvals, build_queue, db, events, schedules

log = logging.getLogger("bits_orchestrator.nightlies")

GRACE_MS = 6 * 3600 * 1000
SUFFIX = " [nightly]"              # the pipeline name's, as the GitLab fan-out's
_OPEN = ("queued", "running")
_RESOLVED = re.compile(r"[A-Za-z0-9._:/@+-]{1,300}", re.ASCII)   # an image, a directory, an arch


class Unbuildable(ValueError):
    """A night that cannot be built as configured: why (recorded on its runs)."""


def subject(sid):
    """What a schedule's standing approval is bound to, with its content hash."""
    return "schedule:%d" % int(sid)


# --- what a night's build is ---------------------------------------------------

def resolve_platform(platform, defaults, community_cfg):
    """(image, install_dir, bits architecture, runner architecture) of *platform*:
    the community's platforms entry (name or name|image|install_dir|arch) first,
    then config/platforms.yaml, as lib-pipeline-dispatch.sh resolve_platform.
    Unbuildable when neither has it, or a value is not a plain word."""
    img = install_dir = arch = ""
    listed = False
    for e in (community_cfg.get("platforms") or []):
        if isinstance(e, str):
            parts = [x.strip() for x in e.split("|")]
            if parts and parts[0] == platform:
                img, install_dir, arch = (parts[1:] + ["", "", ""])[:3]
                listed = True
                break
    d = next((p for p in defaults if isinstance(p, dict) and p.get("name") == platform), None)
    if not listed and d is None:
        raise Unbuildable("platform %s is in neither the community's ui-config.yaml nor"
                          " config/platforms.yaml" % platform)
    d = d or {}
    img = img or str(d.get("image") or d.get("docker_image") or "")
    install_dir = install_dir or str(d.get("install_dir") or platform)
    arch = arch or str(d.get("arch") or platform)
    for what, v in (("image", img), ("install_dir", install_dir), ("arch", arch)):
        if v and not _RESOLVED.fullmatch(v):
            raise Unbuildable("platform %s: its %s %r is not a plain word" % (platform, what, v))
    return img, install_dir, arch, "aarch64" if "aarch64" in platform else "x86_64"


def build_args(s, platform_arch, image, max_jobs):
    """BITS_BUILD_ARGS, in lib-pipeline-dispatch.sh's order, with the schedule's
    `set` variables before its extra arguments."""
    a = []
    if s["rebuild"]:
        a += ["--force-rebuild", s["package"]]
    if platform_arch:
        a += ["--architecture", platform_arch]
    if image:
        a += ["--docker", "--docker-image", image]
    if s["jobs"]:
        a += ["-j", str(s["jobs"])]
    if max_jobs > 0:
        a.append("--docker-extra-args=--cpus=%d" % max_jobs)
    a += ["--sandbox", "off"]
    if s["debug"]:
        a.append("--debug")
    if s["defaults"]:
        a += ["--defaults", s["defaults"]]
    for k, v in s["set"].items():
        a += ["--set", "%s=%s" % (k, v)]
    if s["extra_build_args"]:
        a.append(" ".join(s["extra_build_args"].split()))
    return " ".join(a)


def variables(s, platform, cfg, certifier):
    """The queued build of *platform*: [{key, value}]. *cfg*: config(); *certifier*:
    who certifies (the standing approval's approver), or None."""
    img, install_dir, arch, runner = resolve_platform(platform, cfg["platforms"], cfg["community"])
    v = [("PACKAGE", s["package"]), ("PLATFORM", platform), ("RUNNER_ARCH", runner),
         ("BITS_RUNNER_TAG", build_queue.ARCH_TAG + runner), ("COMMUNITY", s["community_dir"]),
         ("CVMFS_INSTALL_DIR", install_dir), ("BITS_BUILD_ARGS", build_args(s, arch, img, cfg["max_jobs"])),
         ("VERSION", s["version"]), ("BITS_EXTRA_REPOS", s["extra_repos"]),
         ("PUBLISH_CVMFS", "true" if s["cvmfs"] else ""),
         ("CERTIFY", "true" if certifier else ""), ("BITS_CERTIFIER", certifier or ""),
         ("NIGHTLY_SUFFIX", SUFFIX)]
    return [{"key": k, "value": x} for k, x in v if x]


def _yaml(reader, path, ref):
    raw = reader.file_raw(path, ref)
    if raw is None:
        return {}
    doc = yaml.safe_load(raw.decode() if isinstance(raw, bytes) else raw) or {}
    return doc if isinstance(doc, dict) else {}


def config(reader, community_dir, ref):
    """What resolving a community's platforms needs, from the console project at *ref*.
    Unbuildable when its ui-config.yaml is not there or does not parse; a GitLab
    that cannot be read raises as it does (the night is tried again)."""
    try:
        community = _yaml(reader, "communities/%s/ui-config.yaml" % community_dir, ref)
        platforms = _yaml(reader, "config/platforms.yaml", ref).get("platforms") or []
        build = _yaml(reader, "config/monitoring.yaml", ref).get("build") or {}
    except yaml.YAMLError as e:
        raise Unbuildable("the console's configuration does not parse: %s" % str(e)[:200])
    if not community:
        raise Unbuildable("communities/%s/ui-config.yaml is not in the console project" % community_dir)
    try:
        max_jobs = int(build.get("max_jobs_per_container") or 0)
    except (TypeError, ValueError):
        max_jobs = 0
    return {"platforms": platforms if isinstance(platforms, list) else [], "community": community,
            "max_jobs": max_jobs}


# --- runs --------------------------------------------------------------------------

def _run(conn, s, night, platform, attempt, state, reason="", entry=None, now=None):
    now = now or db.now_ms()
    conn.execute("INSERT INTO schedule_runs (schedule_id, night, platform, attempt, state,"
                 " content_sha256, queue_entry, reason, created_ms, updated_ms)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (s["id"], night, platform, attempt, state, s["content_sha256"], entry,
                  reason[:500], now, now))


def _attempt(conn, sid, night, platform):
    r = conn.execute("SELECT MAX(attempt) AS a FROM schedule_runs WHERE schedule_id = ? AND"
                     " night = ? AND platform = ?", (sid, night, platform)).fetchone()
    return (r["a"] or 0) + 1


def _still_open(conn, sid, platform):
    return conn.execute("SELECT 1 FROM schedule_runs WHERE schedule_id = ? AND platform = ?"
                        " AND state IN ('queued', 'running') LIMIT 1", (sid, platform)).fetchone()


def _certifier(conn, s, n):
    """(who certifies *n* builds of the night, or None; why not)."""
    if not s["certify"]:
        return None, ""
    a = approvals.standing_in_force(conn, subject(s["id"]), s["content_sha256"])
    if not a or not set(s["certify_groups"]) <= set(a["groups"]):
        return None, "built, not certified: no standing approval"
    # Each build's pre-approval takes a run when it starts: those still waiting count.
    waiting = conn.execute("SELECT COUNT(*) AS n FROM build_queue WHERE requested_by = ? AND certify = 1"
                           " AND state IN ('queued', 'dispatching')", (subject(s["id"]),)).fetchone()["n"]
    if a["runs_left"] - waiting < n:
        return None, "built, not certified: the standing approval has %d runs left" % a["runs_left"]
    return a["approver"], ""


def fire_night(conn, s, night, platforms, cfg, ref, why, now=None):
    """Within the caller's transaction: queue *night*'s builds of *platforms*
    (a platform still waiting or building skips it, one that cannot be built is
    not run). Returns the runs' states."""
    go, states = [], {}
    for p in platforms:
        attempt = _attempt(conn, s["id"], night, p)
        if _still_open(conn, s["id"], p):
            if why == "scheduled":                 # Run now records nothing: its run still goes on
                _run(conn, s, night, p, attempt, "skipped", "the previous night still runs", now=now)
            states[p] = "skipped"
            continue
        try:
            resolve_platform(p, cfg["platforms"], cfg["community"])
        except Unbuildable as e:
            _run(conn, s, night, p, attempt, "not_run", str(e), now=now)
            states[p] = "not_run"
            continue
        go.append((p, attempt))
    certifier, note = _certifier(conn, s, len(go))
    if go:
        builds = [("%s%s (%s)" % (s["package"], " " + s["version"] if s["version"] else "", p),
                   variables(s, p, cfg, certifier)) for p, _a in go]
        batch, ids = build_queue.add_in(conn, s["community"], subject(s["id"]), ref, builds,
                                        events.WORKER, batch="%s:%s:%s" % (subject(s["id"]), night,
                                                                           secrets.token_hex(4)))
        for (p, attempt), entry in zip(go, ids):
            _run(conn, s, night, p, attempt, "queued", note, entry, now=now)
            states[p] = "queued"
    events.append(conn, "schedule.fired", subject(s["id"]), events.WORKER,
                  {"night": night, "why": why, "runs": states, "certifier": certifier})
    return states


def _not_run(conn, s, night, reason, now=None):
    for p in s["platforms"]:
        _run(conn, s, night, p, _attempt(conn, s["id"], night, p), "not_run", reason, now=now)
    events.append(conn, "schedule.not_run", subject(s["id"]), events.WORKER,
                  {"night": night, "reason": reason})


def run_now(conn, sid, platforms, cfg, ref, tz, user, now=None):
    """Within the caller's transaction: Run now (all platforms) or Run again (some):
    today's night, each platform's next attempt. The states, or None when the
    schedule is not there."""
    s = schedules.get(conn, sid)
    if s is None:
        return None
    now = now or db.now_ms()
    night = dt.datetime.fromtimestamp(now / 1000, tz).date().isoformat()
    unknown = set(platforms or []) - set(s["platforms"])
    if len(set(platforms or [])) != len(platforms or []):
        raise schedules.Invalid("platforms: each once")
    if unknown:
        raise schedules.Invalid("platforms: not the schedule's: %s" % ", ".join(sorted(unknown)))
    return fire_night(conn, s, night, platforms or s["platforms"], cfg, ref, "run now by %s" % user, now)


def due(conn, now, tz):
    """[(schedule, [(night, ms)])] of the enabled schedules whose time came since they last fired."""
    out = []
    for s in schedules.listing(conn):
        if s["enabled"]:
            occ = schedules.occurrences(s, s["last_fired_ms"], now, tz)
            if occ:
                out.append((s, occ))
    return out


def step(conn, reader, ref, tz, now=None):
    """The worker step: fire every schedule that is due. Returns how many nights fired."""
    now = now or db.now_ms()
    fired, cfgs, unread = 0, {}, []
    for s, occ in due(conn, now, tz):
        night, ms = occ[-1]
        late = now - ms > GRACE_MS
        cfg = None
        if not late:
            if s["community_dir"] not in cfgs:             # read before the write lock
                try:
                    cfgs[s["community_dir"]] = config(reader, s["community_dir"], ref)
                except Unbuildable as e:
                    cfgs[s["community_dir"]] = e
                except Exception as e:                     # GitLab: this night again next pass
                    log.warning("schedule %s: the console's configuration: %s", s["id"], e)
                    unread.append(s["id"])
                    continue
            cfg = cfgs[s["community_dir"]]
        with db.transaction(conn):
            fresh = schedules.get(conn, s["id"])
            if fresh is None or not fresh["enabled"] or fresh["last_fired_ms"] != s["last_fired_ms"] \
                    or fresh["content_sha256"] != s["content_sha256"]:
                continue                                    # changed meanwhile: the next pass
            for n, _ms in occ[-15:-1]:                    # the last two weeks' at most
                _not_run(conn, s, n, "the orchestrator was not running", now)
            if late:
                _not_run(conn, s, night, "not fired in time: the orchestrator was not running,"
                                         " or the console's configuration could not be read", now)
            elif isinstance(cfg, Unbuildable):
                _not_run(conn, s, night, str(cfg), now)
            else:
                fire_night(conn, s, night, s["platforms"], cfg, ref, "scheduled", now)
                fired += 1
            conn.execute("UPDATE schedules SET last_fired_ms = ? WHERE id = ?", (now, s["id"]))
    if unread:
        raise RuntimeError("the console's configuration could not be read for schedules %s"
                           % ", ".join(map(str, unread)))
    return fired


# --- what became of them -----------------------------------------------------

NO_HOST_MS = 12 * 3600 * 1000      # still waiting for a host this long after it fired: not run
RETRIED_MS = 3 * 86400 * 1000      # a failed run's pipeline looked at again (retried in GitLab)


def _outcome(conn, pid):
    """(state, build_id, failed_at) of a run's pipeline, or None while not recorded."""
    p = conn.execute("SELECT outcome FROM pipelines WHERE id = ? AND is_build = 1", (pid,)).fetchone()
    if p is None:
        return None
    b = conn.execute("SELECT build_id FROM builds WHERE pipeline_id = ? ORDER BY build_id LIMIT 1",
                     (pid,)).fetchone()
    f = conn.execute("SELECT package, job_name FROM failures WHERE pipeline_id = ? ORDER BY id LIMIT 1",
                     (pid,)).fetchone()
    state = {"running": "running", "success": "passed", "failed": "failed",
             "canceled": "not_run"}.get(p["outcome"], "running")
    where = "%s (%s)" % (f["package"], f["job_name"]) if f and state == "failed" else ""
    return state, b["build_id"] if b else "", where


def results(conn, now=None):
    """The worker step: each open run (and a recently failed one) from its queued
    build and its pipeline; one waiting NO_HOST_MS is taken out of the queue."""
    now = now or db.now_ms()
    rows = conn.execute(
        "SELECT r.*, q.state AS q_state, q.reason AS q_reason, q.pipeline_id AS q_pipeline"
        " FROM schedule_runs r LEFT JOIN build_queue q ON q.id = r.queue_entry"
        " WHERE r.state IN ('queued', 'running') OR (r.state = 'failed' AND r.created_ms > ?)",
        (now - RETRIED_MS,)).fetchall()
    for r in rows:
        new = dict(state=r["state"], reason=r["reason"], pipeline_id=r["pipeline_id"],
                   build_id=r["build_id"], failed_at=r["failed_at"])
        cancel = False
        if r["q_state"] is None:
            new.update(state="not_run", reason="its queued build is gone")
        elif r["q_state"] == "canceled":
            new.update(state="not_run", reason=r["q_reason"] or "canceled")
        elif r["q_state"] == "refused":
            new.update(state="not_run", reason=r["q_reason"] or "refused by the queue")
        elif r["q_state"] == "queued" and now - r["created_ms"] > NO_HOST_MS:
            new.update(state="not_run", reason="no free build host for 12 hours")
            cancel = True
        elif r["q_pipeline"] is not None:
            got = _outcome(conn, r["q_pipeline"])
            if got is not None:
                new.update(state=got[0], build_id=got[1], failed_at=got[2], pipeline_id=r["q_pipeline"],
                           reason=r["reason"] if got[0] != "not_run" else "the pipeline was canceled")
        if new == {k: r[k] for k in new}:
            continue
        with db.transaction(conn):
            if cancel:
                if conn.execute("UPDATE build_queue SET state = 'canceled', reason = ?, updated_ms = ?"
                                " WHERE id = ? AND state = 'queued'",
                                (new["reason"], now, r["queue_entry"])).rowcount != 1:
                    continue                                # it started meanwhile
                q = build_queue.get(conn, r["queue_entry"])
                events.append(conn, "queue.canceled", "queue:%s" % q["batch"], events.WORKER,
                              {"entry": q["id"], "reason": new["reason"]})
            conn.execute("UPDATE schedule_runs SET state = ?, reason = ?, pipeline_id = ?, build_id = ?,"
                         " failed_at = ?, updated_ms = ? WHERE id = ?",
                         (new["state"], new["reason"][:500], new["pipeline_id"], new["build_id"],
                          new["failed_at"], now, r["id"]))


# --- the matrix --------------------------------------------------------------------

def matrix(conn, communities, nights, tz, now=None):
    """The nightly matrix of *communities* (lowercase; None = all) over the last
    *nights* nights: one row per schedule, a cell per platform and night, its
    latest attempt."""
    now = now or db.now_ms()
    today = dt.datetime.fromtimestamp(now / 1000, tz).date()
    days = [(today - dt.timedelta(days=i)).isoformat() for i in range(nights - 1, -1, -1)]
    rows = []
    for s in schedules.listing(conn, communities):
        cells = {}
        for r in conn.execute(
                "SELECT * FROM schedule_runs WHERE schedule_id = ? AND night >= ? ORDER BY attempt",
                (s["id"], days[0])):
            cells.setdefault(r["platform"], {})[r["night"]] = {
                k: r[k] for k in ("state", "attempt", "reason", "failed_at", "pipeline_id", "build_id")}
        rows.append({"schedule": {k: s[k] for k in ("id", "name", "package", "version", "defaults",
                                                    "platforms", "enabled", "time", "days")},
                     "cells": cells,
                     "failed": {n: sum(1 for p in cells.values() if p.get(n, {}).get("state") == "failed")
                                for n in days}})
    return {"nights": days, "rows": rows}
