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

import json
import logging
import re

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

def _run(conn, s, night, platform, attempt, state, reason="", entry=None):
    now = db.now_ms()
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
    if a["runs_left"] < n:                      # each build's pre-approval takes a run
        return None, "built, not certified: the standing approval has %d runs left" % a["runs_left"]
    return a["approver"], ""


def fire_night(conn, s, night, platforms, cfg, ref, why):
    """Within the caller's transaction: queue *night*'s builds of *platforms*
    (a platform still waiting or building skips it, one that cannot be built is
    not run). Returns the runs' states."""
    go, states = [], {}
    for p in platforms:
        attempt = _attempt(conn, s["id"], night, p)
        if _still_open(conn, s["id"], p):
            _run(conn, s, night, p, attempt, "skipped", "the previous night still runs")
            states[p] = "skipped"
            continue
        try:
            resolve_platform(p, cfg["platforms"], cfg["community"])
        except Unbuildable as e:
            _run(conn, s, night, p, attempt, "not_run", str(e))
            states[p] = "not_run"
            continue
        go.append((p, attempt))
    certifier, note = _certifier(conn, s, len(go))
    if go:
        builds = [("%s%s (%s)" % (s["package"], " " + s["version"] if s["version"] else "", p),
                   variables(s, p, cfg, certifier)) for p, _a in go]
        batch, ids = build_queue.add_in(conn, s["community"], subject(s["id"]), ref, builds,
                                        events.WORKER, batch="%s:%s:%d" % (subject(s["id"]), night,
                                                                           go[0][1]))
        for (p, attempt), entry in zip(go, ids):
            _run(conn, s, night, p, attempt, "queued", note, entry)
            states[p] = "queued"
    events.append(conn, "schedule.fired", subject(s["id"]), events.WORKER,
                  {"night": night, "why": why, "runs": states, "certifier": certifier})
    return states


def _not_run(conn, s, night, reason):
    for p in s["platforms"]:
        _run(conn, s, night, p, _attempt(conn, s["id"], night, p), "not_run", reason)
    events.append(conn, "schedule.not_run", subject(s["id"]), events.WORKER,
                  {"night": night, "reason": reason})


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
                _not_run(conn, s, n, "the orchestrator was not running")
            if late:
                _not_run(conn, s, night, "not fired in time: the orchestrator was not running,"
                                         " or the console's configuration could not be read")
            elif isinstance(cfg, Unbuildable):
                _not_run(conn, s, night, str(cfg))
            else:
                fire_night(conn, s, night, s["platforms"], cfg, ref, "scheduled")
                fired += 1
            conn.execute("UPDATE schedules SET last_fired_ms = ? WHERE id = ?", (now, s["id"]))
    if unread:
        raise RuntimeError("the console's configuration could not be read for schedules %s"
                           % ", ".join(map(str, unread)))
    return fired
