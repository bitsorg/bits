# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator's API, mounted on the console backend.

    GET  /ops/actions/catalogue        every action kind, its schema, its approval
    POST /ops/actions                  request an action {kind, params, idempotency_key?}
    GET  /ops/actions                  actions of the communities the caller administers
    GET  /ops/actions/{id}
    POST /ops/actions/{id}/approve
    POST /ops/actions/{id}/reject      {reason?}
    GET  /ops/events?after=&limit=&wait=   the event log (overall admins), long-polled
    GET  /ops/builds?community=&outcome=&pipelines=&limit=
                                       build pipelines and their builds
    GET  /ops/builds/{pipeline}        one, with its jobs, variables and events
    POST /ops/ci/published, /ops/ci/failed   {pipeline}: a CI job's hint to look
                                       at its pipeline now (GitLab CI ID token)

The API writes only requests and decisions; the worker runs actions. Identity
and the admin policy are the backend's own (`main._current`, `_resolved_policy`).
"""

import asyncio
import contextlib
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import audit, authz, ci_auth
from . import actions, db, events, kinds, reconcile

router = APIRouter()
_MAX_BODY = 64 * 1024


def _m():
    from .. import main          # imported late: main mounts this router
    return main


@contextlib.contextmanager
def _open():
    path = _m().settings.orch_db
    if not path:
        raise HTTPException(503, "orchestrator not configured (BITS_ORCH_DB)")
    try:
        conn = db.open_checked(path)
    except db.SchemaError as e:
        raise HTTPException(503, str(e))
    try:
        yield conn
    finally:
        conn.close()


async def _caller(request):
    data = await _m()._current_async(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    return data


def _policy(data):
    return _m()._resolved_policy(data["token"])


def _may_act(data, community):
    resolved = _policy(data)
    if community:
        return authz.is_admin_for(data["user"], community, resolved)
    return authz.admin_groups(data["user"], resolved)[0]


async def _body(request):
    raw = await _m()._read_capped(request, _MAX_BODY)
    try:
        payload = json.loads(raw or b"{}")
    except (ValueError, RecursionError):
        raise HTTPException(400, "expected a JSON object")
    if not isinstance(payload, dict):
        raise HTTPException(400, "expected a JSON object")
    return payload


def _public(row):
    keep = ("id", "kind", "params", "subject", "community", "state", "requested_by_kind",
            "requested_by_id", "approved_by_kind", "approved_by_id", "attempts",
            "result", "error", "created_ms", "updated_ms")
    return {k: row[k] for k in keep}


@router.get("/ops/actions/catalogue")
async def catalogue(request: Request):
    await _caller(request)
    return {"actions": actions.catalogue()}


@router.post("/ops/actions")
async def request_action(request: Request):
    data = await _caller(request)
    payload = await _body(request)
    name, params = payload.get("kind"), payload.get("params")
    key = payload.get("idempotency_key")
    if not isinstance(name, str) or (key is not None and not isinstance(key, str)):
        raise HTTPException(400, "expected {kind, params, idempotency_key?}")
    try:
        k = actions.kind(name)
        actions.check_params(k, params)
        community = await run_in_threadpool(k.scope, params, kinds.Deps(_m().settings))
    except actions.ActionError as e:
        raise HTTPException(400, str(e))
    except kinds.NotConfigured as e:
        raise HTTPException(503, str(e))
    except Exception as e:                # e.g. the forge could not be read
        raise HTTPException(502, "cannot resolve the action's community: %s" % e)
    allowed = await run_in_threadpool(_may_act, data, community)
    if not allowed:
        audit.record("ops_denied", op="action", kind=name, user=data["user"],
                     community=community or "*", principal="human")
        raise HTTPException(403, "%s is not an admin for %s"
                            % (data["user"], community or "every community"))

    def write():
        with _open() as conn:
            return actions.request(conn, name, params, events.Actor("person", data["user"]),
                                   community, True, idempotency_key=key)
    try:
        row, created = await run_in_threadpool(write)
    except actions.ActionError as e:
        raise HTTPException(400, str(e))
    audit.record("ops_action", kind=name, action=row["id"], user=data["user"],
                 community=community or "*", principal="human")
    return JSONResponse(_public(row), status_code=201 if created else 200)


@router.get("/ops/actions")
async def list_actions(request: Request, state: str = "", limit: int = 100):
    data = await _caller(request)
    if state and state not in actions.STATES:
        raise HTTPException(400, "unknown state")
    overall, groups = await run_in_threadpool(lambda: authz.admin_groups(data["user"], _policy(data)))

    mine = None if overall else {g.lower() for g in groups}

    def read():
        with _open() as conn:
            return [_public(r) for r in actions.listing(
                conn, state=state or None, communities=mine, limit=limit)]
    return {"actions": await run_in_threadpool(read)}


async def _visible(data, action_id):
    def read():
        with _open() as conn:
            return actions.get(conn, action_id)
    row = await run_in_threadpool(read)
    if row is None or not await run_in_threadpool(_may_act, data, row["community"]):
        raise HTTPException(404, "no such action")
    return row


@router.get("/ops/actions/{action_id}")
async def get_action(action_id: int, request: Request):
    data = await _caller(request)
    return _public(await _visible(data, action_id))


async def _decide(action_id, request, approve):
    data = await _caller(request)
    payload = await _body(request)
    row = await _visible(data, action_id)       # also the authority check
    actor = events.Actor("person", data["user"])

    def write():
        with _open() as conn:
            if approve:
                return actions.approve(conn, row["id"], actor, True)
            return actions.reject(conn, row["id"], actor, True,
                                  str(payload.get("reason") or ""))
    try:
        out = await run_in_threadpool(write)
    except actions.ActionError as e:
        raise HTTPException(409, str(e))
    audit.record("ops_action_" + ("approve" if approve else "reject"), action=row["id"],
                 kind=row["kind"], user=data["user"], community=row["community"] or "*",
                 principal="human")
    return _public(out)


@router.post("/ops/actions/{action_id}/approve")
async def approve_action(action_id: int, request: Request):
    return await _decide(action_id, request, True)


@router.post("/ops/actions/{action_id}/reject")
async def reject_action(action_id: int, request: Request):
    return await _decide(action_id, request, False)


@router.get("/ops/events")
async def list_events(request: Request, after: int = 0, limit: int = 200, wait: int = 0):
    """Events after *after*. With wait (seconds, at most 25) and nothing new yet,
    answer as soon as an event arrives or the wait ends. Overall admins only until
    events carry their community (M1)."""
    data = await _caller(request)
    if not await run_in_threadpool(lambda: authz.admin_groups(data["user"], _policy(data))[0]):
        raise HTTPException(403, "%s is not an overall admin" % data["user"])

    def read():
        with _open() as conn:
            return events.since(conn, after, limit)
    deadline = asyncio.get_running_loop().time() + max(0, min(int(wait), 25))
    while True:
        out = await run_in_threadpool(read)
        if out or asyncio.get_running_loop().time() >= deadline:
            return JSONResponse({"events": out}, headers={"Cache-Control": "no-store"})
        await asyncio.sleep(1)


# --- builds -------------------------------------------------------------------

async def _scope(data):
    """None for an overall admin (every community), else the communities the
    caller administers (lowercase)."""
    overall, groups = await run_in_threadpool(lambda: authz.admin_groups(data["user"], _policy(data)))
    return None if overall else {g.lower() for g in groups}


@router.get("/ops/builds")
async def list_builds(request: Request, community: str = "", outcome: str = "",
                      pipelines: str = "", limit: int = 50):
    """Build pipelines newest first, with the builds they published: those of the
    communities the caller administers (all, for an overall admin). *pipelines*
    is a comma-separated list of ids (the console asks for the rows it shows)."""
    data = await _caller(request)
    mine = await _scope(data)
    if community:
        c = community.strip().lower()
        if mine is not None and c not in mine:
            return {"builds": []}
        mine = {c}
    if outcome and outcome not in ("running", "success", "failed", "canceled"):
        raise HTTPException(400, "unknown outcome")
    ids = None
    if pipelines:
        try:
            ids = [int(x) for x in pipelines.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(400, "pipelines must be comma-separated ids")

    def read():
        with _open() as conn:
            return reconcile.list_builds(conn, communities=mine, outcome=outcome or None,
                                         pipelines=ids, limit=limit)
    return JSONResponse({"builds": await run_in_threadpool(read)},
                        headers={"Cache-Control": "no-store"})


@router.get("/ops/builds/{pipeline}")
async def get_build(pipeline: int, request: Request):
    data = await _caller(request)
    mine = await _scope(data)

    def read():
        with _open() as conn:
            return reconcile.get_build(conn, pipeline)
    b = await run_in_threadpool(read)
    if b is None or (mine is not None and b["community"] not in mine):
        raise HTTPException(404, "no such build")
    return JSONResponse(b, headers={"Cache-Control": "no-store"})


# --- CI hints -------------------------------------------------------------------

async def _ci_claims(request, settings):
    """The verified claims of the caller's CI ID token, from the build project on
    a protected ref; else HTTPException. Verified off the event loop: fetching
    GitLab's keys is network I/O."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "CI ID token required")
    aud = settings.orch_ci_audience
    if not aud or aud == settings.oidc_ci_audience:
        raise HTTPException(503, "BITS_ORCH_CI_AUDIENCE must be set, and differ from the"
                                 " signing audience")
    try:
        claims = await run_in_threadpool(ci_auth.verify_ci_token,
                                         auth[len("Bearer "):].strip(), settings, None, aud)
    except ValueError as e:
        raise HTTPException(503, str(e))
    except Exception:
        raise HTTPException(401, "invalid CI ID token")
    project = str(settings.forge_project)
    if project not in (str(claims.get("project_id", "")), str(claims.get("project_path", ""))):
        raise HTTPException(403, "not the build project")
    if str(claims.get("ref_protected", "")).lower() != "true":
        raise HTTPException(403, "not a protected ref")
    return claims


async def _hint(request, kind):
    settings = _m().settings
    claims = await _ci_claims(request, settings)
    payload = await _body(request)
    try:
        pid = int(str(payload.get("pipeline", "")))
    except ValueError:
        raise HTTPException(400, "expected {pipeline}")
    if str(claims.get("pipeline_id", "")) != str(pid):
        raise HTTPException(403, "the token is not from pipeline %d" % pid)

    def write():
        with _open() as conn:
            reconcile.add_hint(conn, pid, kind)
    await run_in_threadpool(write)
    return {"status": "noted"}


@router.post("/ops/ci/published")
async def ci_published(request: Request):
    """A hint only: the worker looks at the pipeline on its next pass and reads
    what was published from S3, never from this request."""
    return await _hint(request, "published")


@router.post("/ops/ci/failed")
async def ci_failed(request: Request):
    return await _hint(request, "failed")
