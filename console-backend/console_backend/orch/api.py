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

The API writes only requests and decisions; the worker runs actions. Identity
and the admin policy are the backend's own (`main._current`, `_resolved_policy`).
"""

import asyncio
import contextlib
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import audit, authz
from . import actions, db, events, kinds

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
