# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""What an overall admin does to a build host, as actions: pause or resume its
runners in GitLab (a paused runner takes no job, so the queue starts nothing
there), and refresh its builder images. The refresh is a pipeline pinned to the
host: GitLab starts it when the host's runner takes it, not while it is paused."""

from .. import forge_ops
from . import actions, build_queue

HOST = {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$",
        "description": "a build host: the <name> of its runner's bits-host-<name> tag"}
_PARAMS = {"type": "object", "properties": {"host": HOST}, "required": ["host"],
           "additionalProperties": False}


def _forge_error(e):
    if e.status == 0 or e.status == 429 or e.status >= 500:
        return actions.Retry("forge %s: %s" % (e.status, e.message))
    return RuntimeError("forge %s: %s" % (e.status, e.message))


def runners(forge, host):
    """The project's own runners tagged bits-host-<host>; an error when none is."""
    tag = build_queue.HOST_TAG + host
    try:
        found = [x for x in forge.list_runners(tag) or []
                 if isinstance(x, dict) and not build_queue.shared_runner(x)]
    except forge_ops.ForgeError as e:
        raise _forge_error(e)
    if not found:
        raise RuntimeError("no runner of the build project is tagged %s" % tag)
    return found


def _paused(runner):
    return bool(runner.get("paused")) or runner.get("active") is False


def _set_paused(params, deps, paused):
    forge = deps.forge()
    found = runners(forge, params["host"])
    changed = []
    for x in found:
        if _paused(x) != paused:
            try:
                forge.set_runner_paused(x["id"], paused)
            except forge_ops.ForgeError as e:
                err = _forge_error(e)        # what was changed before it: a rerun skips those
                raise type(err)("runner %s: %s%s" % (x["id"], err, "; changed before it: %s" % changed
                                                     if changed else ""))
            changed.append(x["id"])
    return {"runners": [x["id"] for x in found], "changed": changed}


def ref(settings):
    """The branch host pipelines run on (as the nightlies')."""
    refs = settings.forge_refs
    return "main" if "main" in refs else sorted(refs)[0]


def pipeline(deps, host, variables):
    """A pipeline pinned to *host*, with *variables* ({key: value}). A retry after
    GitLab's answer was lost may start a second one: both jobs bear that."""
    forge = deps.forge()
    runners(forge, host)
    pinned = dict(variables, BITS_RUNNER_TAG=build_queue.HOST_TAG + host)
    try:
        out = forge.trigger_pipeline(ref(deps.settings),
                                     [{"key": k, "value": str(v)} for k, v in pinned.items()])
    except forge_ops.ForgeError as e:
        raise _forge_error(e)
    return {"pipeline": out.get("id"), "web_url": out.get("web_url")}


def _no_community(params, deps):
    return ""


def _subject(params):
    return "host:%s" % params["host"]


actions.register(actions.Kind(
    name="host.pause", summary="Pause a build host's runners in GitLab: it takes no build",
    params=_PARAMS, approval="requester", subject=_subject, scope=_no_community,
    run=lambda p, d: _set_paused(p, d, True), safe_to_rerun=True))

actions.register(actions.Kind(
    name="host.resume", summary="Resume a build host's runners in GitLab",
    params=_PARAMS, approval="requester", subject=_subject, scope=_no_community,
    run=lambda p, d: _set_paused(p, d, False), safe_to_rerun=True))

actions.register(actions.Kind(
    name="host.refresh", summary="Pull a build host's builder images again (a pipeline on it)",
    params=_PARAMS, approval="requester", subject=_subject, scope=_no_community,
    run=lambda p, d: pipeline(d, p["host"], {"REFRESH_IMAGES_JOB": "true"}), safe_to_rerun=True))
