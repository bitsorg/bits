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
    GET  /ops/issues?community=&package=&limit=   deduplicated failures, last seen first
    GET  /ops/issues/{id}              one, with its occurrences
    GET  /ops/failures?pipeline=&community=&limit=   failed jobs' records, with excerpts
    GET  /ops/signatures               the reviewed patterns that name a cause
    GET  /ops/certifications?state=&limit=   bits-manifests certifications and their
                                       sign runs: the worker's shadow comparison
    GET  /ops/certifications/summary?days=   sign runs by state, from the git tree,
                                       from the certified set and the worker's own
                                       certification (the M4, M5 and M8a gates)
    GET  /ops/outside                  the outside audit copy: its head, any alarm
    GET  /ops/certified?group=&arch=&removed=&limit=   the certified set (the groups
                                       the caller administers), its version and summary
    GET  /ops/certified/bom/{sha256}   one certified BOM, its exact bytes
    POST /approvals/standing/request   {schedule, sha256, groups[], runs, days}: a passkey
                                       challenge bound to them
    POST /approvals/standing/approve   {request_id, assertion}
    GET  /approvals?kind=&all=         approvals of the caller's communities
    POST /approvals/{id}/revoke        by an admin of all its groups

The API writes only requests and decisions; the worker runs actions. Identity
and the admin policy are the backend's own (`main._current`, `_resolved_policy`).
"""

import asyncio
import contextlib
import hashlib
import json
import re
import secrets

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .. import audit, authz, ci_auth, session
from . import (actions, approvals, certified, db, events, failures, kinds, outside,
               reconcile, shadow)

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


async def _overall(request):
    data = await _caller(request)
    if not await run_in_threadpool(lambda: authz.admin_groups(data["user"], _policy(data))[0]):
        raise HTTPException(403, "%s is not an overall admin" % data["user"])
    return data


@router.get("/ops/certifications")
async def list_certifications(request: Request, state: str = "", limit: int = 50):
    """Certifications of bits-manifests, newest first, with the worker's shadow
    comparison of each architecture (overall admins: they cover every group)."""
    await _overall(request)
    if state and state not in ("pending", "compared", "skipped", "failed"):
        raise HTTPException(400, "unknown state %r" % state)
    return {"certifications": await run_in_threadpool(_read, shadow.runs, limit, state)}


@router.get("/ops/certifications/summary")
async def certifications_summary(request: Request, days: float = 14):
    await _overall(request)
    since = db.now_ms() - int(max(0.0, min(float(days), 366.0)) * 86400 * 1000)
    return {"since_ms": since,
            "sign_runs": await run_in_threadpool(_read, shadow.summary, since),
            "from_set": await run_in_threadpool(_read, shadow.summary, since, "set"),
            "replay": await run_in_threadpool(_read, shadow.summary, since, "replay")}


@router.get("/ops/certified")
async def list_certified(request: Request, group: str = "", arch: str = "", removed: int = 0,
                         limit: int = 1000):
    """The certified set: an overall admin sees every group, a community admin
    their own."""
    data = await _caller(request)
    groups = await _scope(data)
    return await run_in_threadpool(_read, certified.listing, groups, group.strip(), arch.strip(),
                                   bool(removed), limit)


@router.get("/ops/certified/bom/{sha256}")
async def certified_bom(sha256: str, request: Request):
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise HTTPException(400, "expected a sha256")
    data = await _caller(request)
    groups = await _scope(data)
    body = await run_in_threadpool(_read, certified.bom, sha256, groups)
    if body is None:
        raise HTTPException(404, "no such certified BOM")
    return Response(content=body, media_type="application/json",
                    headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/ops/outside")
async def outside_status(request: Request):
    await _overall(request)
    return await run_in_threadpool(_read, outside.status)


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


# --- failures -------------------------------------------------------------------

async def _communities(data, community):
    """The communities a read covers: the caller's (None = all, for an overall
    admin), narrowed to *community* when given."""
    mine = await _scope(data)
    if community:
        c = community.strip().lower()
        if mine is not None and c not in mine:
            return set()
        return {c}
    return mine


def _read(fn, *args, **kw):
    with _open() as conn:
        return fn(conn, *args, **kw)


@router.get("/ops/issues")
async def list_issues(request: Request, community: str = "", package: str = "", limit: int = 100):
    """Issues seen in the caller's communities. An issue seen in several shows
    its full counts; its occurrences (GET /ops/issues/{id}) are only theirs."""
    data = await _caller(request)
    mine = await _communities(data, community)
    out = await run_in_threadpool(_read, failures.list_issues, communities=mine,
                                  package=package or None, limit=limit)
    return JSONResponse({"issues": out}, headers={"Cache-Control": "no-store"})


@router.get("/ops/issues/{issue_id}")
async def get_issue(issue_id: int, request: Request):
    data = await _caller(request)
    mine = await _scope(data)
    out = await run_in_threadpool(_read, failures.get_issue, issue_id, communities=mine)
    if out is None:
        raise HTTPException(404, "no such issue")
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@router.get("/ops/failures")
async def list_failures(request: Request, pipeline: int = 0, community: str = "",
                        limit: int = 100):
    data = await _caller(request)
    mine = await _communities(data, community)
    out = await run_in_threadpool(_read, failures.list_failures, communities=mine,
                                  pipeline=pipeline or None, limit=limit)
    return JSONResponse({"failures": out}, headers={"Cache-Control": "no-store"})


@router.get("/ops/signatures")
async def list_signatures(request: Request):
    await _caller(request)
    return {"signatures": await run_in_threadpool(_read, failures.list_signatures)}


# --- approvals ------------------------------------------------------------------

_standing_pending = session.CliSignStore(ttl_seconds=300, max_entries=64)
_GROUP_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_SCHEDULE_RE = re.compile(r"communities/[A-Za-z0-9._-]+/pipelines/[A-Za-z0-9._-]+\.json")


def _admin_of_all(data, groups):
    resolved = _policy(data)
    return all(authz.is_admin_for(data["user"], g, resolved) for g in groups)


@router.post("/approvals/standing/request")
async def standing_request(request: Request):
    """A community admin approves a schedule's builds for certification without a
    person each time: bound to the schedule file's content (sha256) and its groups,
    for *runs* runs or *days* days (at most 30), whichever ends first. Step 1:
    a passkey challenge over exactly that."""
    data = await _caller(request)
    m = _m()
    if not m.settings.webauthn_configured():
        raise HTTPException(503, "WebAuthn is not configured")
    if not m.settings.orch_db:
        raise HTTPException(503, "orchestrator not configured (BITS_ORCH_DB)")
    payload = await _body(request)
    try:
        schedule = str(payload["schedule"])
        sha = str(payload["sha256"]).lower()
        groups = sorted(set(payload["groups"]))
        runs = payload["runs"]
        days = payload["days"]
        if not _SCHEDULE_RE.fullmatch(schedule) or ".." in schedule or not _HEX64.fullmatch(sha) \
                or not groups or not all(isinstance(g, str) and _GROUP_RE.fullmatch(g) for g in groups) \
                or not isinstance(runs, int) or isinstance(runs, bool) \
                or not 1 <= runs <= approvals.STANDING_MAX_RUNS \
                or not isinstance(days, (int, float)) or isinstance(days, bool) \
                or not 0 < days <= approvals.STANDING_MAX_DAYS:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "expected {schedule, sha256, groups[], runs (1-%d), days (<= %d)}"
                            % (approvals.STANDING_MAX_RUNS, approvals.STANDING_MAX_DAYS))
    if not await run_in_threadpool(_admin_of_all, data, groups):
        audit.record("standing_denied", user=data["user"], groups=groups, schedule=schedule,
                     principal="human")
        raise HTTPException(403, "%s is not an admin for all of: %s"
                            % (data["user"], ", ".join(groups)))
    creds = m.credstore.get(data["user"])
    if not creds:
        raise HTTPException(400, "no passkey enrolled; enrol first")
    # The passkey signs exactly this binding (plus a fresh nonce); the record keeps
    # both, so the approval can be checked again later (approvals.verify_standing).
    nonce = secrets.token_bytes(16)
    challenge = approvals.standing_challenge(
        approvals.standing_binding(schedule, sha, groups, runs, days), nonce)
    req_id = secrets.token_urlsafe(24)
    _standing_pending.put(req_id, {"user": data["user"], "schedule": schedule, "sha256": sha,
                                   "groups": groups, "runs": runs, "days": days,
                                   "challenge": challenge, "nonce": nonce})
    options = json.loads(m.webauthn_rp.authentication_options(m.settings, challenge, creds))
    return {"request_id": req_id, "schedule": schedule, "sha256": sha, "groups": groups,
            "runs": runs, "days": days, "publicKey": options}


@router.post("/approvals/standing/approve")
async def standing_approve(request: Request):
    data = await _caller(request)
    m = _m()
    payload = await _body(request)
    req_id, assertion = payload.get("request_id"), payload.get("assertion")
    if not isinstance(req_id, str) or not isinstance(assertion, dict):
        raise HTTPException(400, "expected {request_id, assertion}")
    req = _standing_pending.get(req_id)
    if not req:
        raise HTTPException(400, "unknown or expired request")
    if req["user"] != data["user"]:
        raise HTTPException(403, "the request belongs to another user")
    _standing_pending._store.pop(req_id, None)         # single use, before any await
    if not await run_in_threadpool(_admin_of_all, data, req["groups"]):
        raise HTTPException(403, "%s is no longer an admin for all of: %s"
                            % (data["user"], ", ".join(req["groups"])))
    cred_id = assertion.get("rawId") or assertion.get("id")
    cred = next((c for c in m.credstore.get(data["user"]) if c["id"] == cred_id), None)
    if not cred:
        raise HTTPException(400, "unknown credential")
    try:
        count = await run_in_threadpool(m.webauthn_rp.verify_authentication, m.settings,
                                        json.dumps(assertion), req["challenge"], cred)
    except Exception:
        audit.record("standing_denied", user=data["user"], groups=req["groups"],
                     schedule=req["schedule"], reason="approval_failed", principal="human")
        raise HTTPException(403, "approval verification failed")
    m.credstore.update_sign_count(data["user"], cred_id, count)

    def write():
        with _open() as conn:
            return approvals.grant_standing(conn, req["schedule"], req["sha256"], req["groups"],
                                            data["user"], req["runs"], req["days"], assertion,
                                            req["nonce"])
    aid = await run_in_threadpool(write)
    if aid is None:
        raise HTTPException(409, "this schedule (as it is now) already has a standing approval")
    audit.record("standing_approved", user=data["user"], groups=req["groups"], approval=aid,
                 schedule=req["schedule"], sha256=req["sha256"], runs=req["runs"],
                 days=req["days"], principal="human")
    return {"status": "approved", "approval": aid}


@router.get("/approvals")
async def list_approvals(request: Request, kind: str = "", include_all: int = Query(0, alias="all"),
                         limit: int = 200):
    """Approvals any of whose groups the caller administers, or that they gave
    (every one, for an overall admin); active ones unless all=1."""
    data = await _caller(request)
    if kind and kind not in ("preapproval", "standing"):
        raise HTTPException(400, "unknown kind")
    mine = await _scope(data)

    def read():
        with _open() as conn:
            rows = approvals.listing(conn, kind=kind or None, active_only=not include_all,
                                     limit=limit, communities=mine, user=data["user"])
        return [approvals.public(r) for r in rows]
    return JSONResponse({"approvals": await run_in_threadpool(read)},
                        headers={"Cache-Control": "no-store"})


@router.post("/approvals/{approval_id}/revoke")
async def revoke_approval(approval_id: int, request: Request):
    """Revoke an approval at once. No passkey: it only takes authority away. The
    caller must administer every one of its groups."""
    data = await _caller(request)

    def read():
        with _open() as conn:
            return approvals.get(conn, approval_id)
    r = await run_in_threadpool(read)
    if r is None:
        raise HTTPException(404, "no such approval")
    groups = json.loads(r["groups"])
    # Its approver, an admin of every one of its groups, or an overall admin. A
    # pre-approval declared with no groups is for its approver and overall admins
    # only (all([]) would let anyone).
    overall = (await _scope(data)) is None
    allowed = overall or r["approver"] == data["user"] or (
        groups and await run_in_threadpool(_admin_of_all, data, groups))
    if not allowed:
        raise HTTPException(403, "not authorized to revoke approval %d" % approval_id)

    def write():
        with _open() as conn:
            return approvals.revoke(conn, approval_id, data["user"])
    if not await run_in_threadpool(write):
        raise HTTPException(409, "already revoked")
    audit.record("approval_revoked", user=data["user"], approval=approval_id, kind=r["kind"],
                 subject=r["subject"], principal="human")
    return {"status": "revoked", "approval": approval_id}
