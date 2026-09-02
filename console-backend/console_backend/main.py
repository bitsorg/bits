# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-console backend — relying party and the ONLY client of the security-proxy.

Humans are identified by the GitLab bearer token their browser already holds (the
bits-console SPA obtains it via OAuth PKCE); this backend verifies it against
GitLab rather than running its own OAuth/session. CI uses a GitLab ID token. The
frontend is a separate origin (GitLab Pages), allowed via CORS.
"""

import base64
import hashlib
import json
import os
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from . import audit, authz, ci_auth, config, credentials, identity, session, webauthn_rp
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url

try:  # bits_helpers is provided by the surrounding bits repo (on PYTHONPATH).
    from bits_helpers import trust
    _BITS_HELPERS = True
except Exception:  # pragma: no cover - environment check only
    trust = None
    _BITS_HELPERS = False

settings = config.Settings()
credstore = credentials.CredentialStore(settings.credentials_path)
sign_requests = session.SignRequestStore()
cli_signs = session.CliSignStore()
enroll_grants = session.EnrollmentGrantStore()
reg_challenges = session.RegChallengeStore()

app = FastAPI(title="bits-console backend", version="0.1.0")

_STATIC = os.path.join(os.path.dirname(__file__), "static")


@app.middleware("http")
async def _cors(request: Request, call_next):
    """Allow the GitLab Pages frontend (a different origin) to call this API. The
    page authenticates by sending the user's GitLab bearer token; no cookies are
    used, so credentials are not allowed and the origin is matched exactly."""
    origin = request.headers.get("origin")
    allow = settings.frontend_origin
    ok = bool(origin and allow and origin == allow)
    if request.method == "OPTIONS" and ok:
        resp = Response(status_code=204)
    else:
        resp = await call_next(request)
    # Vary on Origin whether or not this one matched, so a shared cache never
    # serves a CORS/no-CORS response to the wrong origin.
    vary = resp.headers.get("vary")
    if not vary:
        resp.headers["Vary"] = "Origin"
    elif "origin" not in vary.lower():
        resp.headers["Vary"] = vary + ", Origin"
    if ok:
        resp.headers["Access-Control-Allow-Origin"] = allow
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        resp.headers["Access-Control-Max-Age"] = "600"
    return resp


@app.get("/")
def index():
    """Dev convenience: serve the bundled approver page. In the split-origin
    deployment the real UI is the bits-console GitLab Pages site and this backend
    is API-only."""
    return FileResponse(os.path.join(_STATIC, "index.html"))


def _current(request: Request):
    """Identify the caller from the GitLab bearer token their browser already holds
    (verified against GitLab, briefly cached). Returns {'user','token'} or None. The
    token doubles as the credential used to resolve the admin policy."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[len("Bearer "):].strip()
    if not token:
        return None
    user = identity.verify_gitlab_token(settings.gitlab_api_url, token)
    if not user:
        return None
    return {"user": user, "token": token}


async def _current_async(request: Request):
    """`_current` does blocking GitLab I/O, so async endpoints offload it to a
    thread rather than call it directly and stall the event loop."""
    return await run_in_threadpool(_current, request)


@app.get("/healthz")
def healthz():
    """Liveness + wiring status. No secrets in the response."""
    return {
        "status": "ok",
        "bits_helpers": _BITS_HELPERS,
        "sign_proxy_configured": settings.sign_proxy_configured(),
        "webauthn_configured": settings.webauthn_configured(),
    }


def _resolved_policy(token):
    return authz.resolved_admin_policy(settings, token)


@app.get("/me")
def me(request: Request):
    """The authenticated user and their community-admin roles. 401 when there is
    no valid session. The GitLab access token stays server-side and is never
    returned."""
    data = _current(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    overall, groups = authz.admin_groups(data["user"], _resolved_policy(data["token"]))
    return {"user": data["user"], "overall_admin": overall, "admin_groups": groups}


_MAX_BODY = 32 * 1024 * 1024   # 32 MiB cap on a submitted manifest
_APPROVE_MAX = 256 * 1024      # a request_id + WebAuthn assertion is small
# The CLI store retains the manifest until approval and is reachable
# unauthenticated, so cap it hard (manifests are KiB/low-MiB JSON) — bounds
# memory. A per-IP rate limit at the reverse proxy is the belt-and-suspenders.
_CLI_MANIFEST_MAX = 4 * 1024 * 1024


def _groups_of(manifest) -> list:
    """The set of package groups in a common manifest (untagged/malformed entries
    -> 'common', so authz never crashes and stays fail-closed)."""
    pkgs = manifest.get("packages")
    if not isinstance(pkgs, list):
        pkgs = []
    groups = set()
    for p in pkgs:
        g = p.get("group") if isinstance(p, dict) else None
        groups.add(g if isinstance(g, str) and g else "common")
    return sorted(groups) or ["common"]


async def _authorize_sign(request: Request, groups):
    """Resolve the signing principal and the groups it is NOT allowed to sign.

    A JWT-shaped bearer is tried as a GitLab CI ID token (verified against GitLab's
    JWKS) and authorized by its project against the CI-signer policy. Any other
    bearer is treated as a human's GitLab token (verified via GET /user) and
    authorized by the community-admin policy. Returns ``(signer, principal_fields,
    denied_groups)``; raises 401 for a bad/absent principal.
    """
    auth = request.headers.get("authorization", "")
    token = auth[len("Bearer "):].strip() if auth.startswith("Bearer ") else ""
    if token and token.count(".") == 2:      # JWT-shaped -> the CI ID-token path
        try:
            claims = await run_in_threadpool(ci_auth.verify_ci_token, token, settings)
        except Exception:
            claims = None
        if claims is not None:
            project = str(claims.get("project_path") or "")
            pol = ci_auth.load_ci_signers(settings)
            denied = [g for g in groups if not ci_auth.is_ci_authorized(project, g, pol)]
            return ("ci:%s" % project,
                    {"principal": "ci", "project": project, "ref": claims.get("ref")},
                    denied)
    data = await _current_async(request)   # human: the bearer is the user's GitLab token
    if not data:
        raise HTTPException(401, "not authenticated")
    user = data["user"]
    resolved = _resolved_policy(data["token"])
    denied = [g for g in groups if not authz.is_admin_for(user, g, resolved)]
    return user, {"principal": "human"}, denied


def _proxy_token_or_503():
    if not settings.sign_proxy_configured():
        raise HTTPException(503, "signing proxy is not configured")
    token = os.environ.get(settings.sign_proxy_token_env)
    if not token:
        raise HTTPException(503, "signing proxy token is not available")
    return token


async def _read_capped(request: Request, maxb: int) -> bytes:
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > maxb:
        raise HTTPException(413, "request too large")
    body = await request.body()
    if len(body) > maxb:
        raise HTTPException(413, "request too large")
    return body


def _parse_manifest(body: bytes) -> list:
    try:
        manifest = json.loads(body)
        if not isinstance(manifest, dict):
            raise ValueError
    except (ValueError, RecursionError):
        raise HTTPException(400, "request body is not a JSON manifest")
    return _groups_of(manifest)


async def _do_sign(body, groups, signer, principal, proxy_token):
    """Key-policy check + sign the EXACT bytes via the proxy + audit. Assumes
    authorization (and, on the approval path, the WebAuthn assertion) passed."""
    try:
        kid, _pub = await run_in_threadpool(
            trust.proxy_pubkey, settings.sign_proxy_url, proxy_token)
    except RuntimeError as exc:
        raise HTTPException(502, "signing proxy pubkey failed: %s" % exc)
    policy = trust.load_key_policy()
    if policy is not None:
        badk = [g for g in groups if not trust.key_authorized(kid, g, policy)]
        if badk:
            audit.record("sign_denied", signer=signer, groups=groups,
                         reason="key_not_authorized", key_id=kid, denied=badk,
                         **principal)
            raise HTTPException(403, "signing key %s is not authorized for: %s"
                                % (kid, ", ".join(badk)))
    try:
        envelope = await run_in_threadpool(
            trust.sign_bytes_via_proxy, body, settings.sign_proxy_url, proxy_token)
    except RuntimeError as exc:
        raise HTTPException(502, "signing failed: %s" % exc)
    digest = hashlib.sha256(body).hexdigest()
    audit.record("sign", signer=signer, groups=groups, digest=digest,
                 key_id=envelope["key_id"], **principal)
    return {"envelope": envelope, "groups": groups, "signed_by": signer, "digest": digest}


@app.post("/sign")
async def sign(request: Request):
    """Single-shot sign: CI (Bearer ID token), or a human when WebAuthn is not in
    force. If WebAuthn is configured and the human has an enrolled passkey, they
    must use the digest-bound /sign/request + /sign/approve flow instead."""
    proxy_token = _proxy_token_or_503()
    body = await _read_capped(request, _MAX_BODY)
    groups = _parse_manifest(body)
    signer, principal, denied = await _authorize_sign(request, groups)
    if denied:
        audit.record("sign_denied", signer=signer, groups=groups, denied=denied,
                     **principal)
        raise HTTPException(403, "%s is not authorized to sign: %s"
                            % (signer, ", ".join(denied)))
    if (principal.get("principal") == "human" and settings.webauthn_configured()
            and (settings.webauthn_required or credstore.get(signer))):
        raise HTTPException(409, "WebAuthn approval required; use /sign/request "
                                 "then /sign/approve")
    return await _do_sign(body, groups, signer, principal, proxy_token)


@app.post("/sign/request")
async def sign_request(request: Request):
    """Human approval, step 1: authorize and return a WebAuthn challenge that IS
    the manifest digest (content binding). The manifest is held server-side so
    approval signs exactly these bytes."""
    data = await _current_async(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    if not settings.webauthn_configured():
        raise HTTPException(503, "WebAuthn is not configured")
    body = await _read_capped(request, _MAX_BODY)
    groups = _parse_manifest(body)
    user = data["user"]
    resolved = _resolved_policy(data["token"])
    denied = [g for g in groups if not authz.is_admin_for(user, g, resolved)]
    if denied:
        audit.record("sign_denied", signer=user, groups=groups, denied=denied,
                     principal="human")
        raise HTTPException(403, "%s is not a community admin for: %s"
                            % (user, ", ".join(denied)))
    creds = credstore.get(user)
    if not creds:
        raise HTTPException(400, "no passkey enrolled; POST /webauthn/register/begin first")
    # Store only the digest + metadata (not the manifest) so the pending store
    # can't be flooded with large bodies; the manifest is re-submitted at approve
    # and checked against this digest.
    digest = hashlib.sha256(body).digest()
    req_id = secrets.token_urlsafe(24)
    sign_requests.put(req_id, {"digest": digest, "groups": groups, "user": user})
    options = json.loads(webauthn_rp.authentication_options(settings, digest, creds))
    return {"request_id": req_id, "publicKey": options}


@app.post("/sign/approve")
async def sign_approve(request: Request):
    """Human approval, step 2: re-submit the manifest with the digest-bound
    WebAuthn assertion. The manifest must hash to the approved digest AND the
    assertion must verify over it; then persist the sign counter and sign."""
    data = await _current_async(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    proxy_token = _proxy_token_or_503()
    raw = await _read_capped(request, 2 * _MAX_BODY)   # base64 manifest + assertion
    try:
        payload = json.loads(raw)
        req_id = payload["request_id"]
        assertion = payload["assertion"]
        body = base64.b64decode(payload["manifest"], validate=True)
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "expected {request_id, assertion, manifest(base64)}")
    if len(body) > _MAX_BODY:
        raise HTTPException(413, "manifest too large")

    req = sign_requests.pop(req_id)   # atomic single-use claim (double-sign race)
    if not req or req["user"] != data["user"]:
        raise HTTPException(400, "unknown or expired sign request")
    # Content binding: the re-submitted manifest must hash to the approved digest.
    if hashlib.sha256(body).digest() != req["digest"]:
        raise HTTPException(400, "manifest does not match the approved request")
    # Re-check community-admin now (close the revoke-within-window gap).
    resolved = _resolved_policy(data["token"])
    denied = [g for g in req["groups"] if not authz.is_admin_for(data["user"], g, resolved)]
    if denied:
        audit.record("sign_denied", signer=data["user"], groups=req["groups"],
                     denied=denied, principal="human")
        raise HTTPException(403, "%s is not a community admin for: %s"
                            % (data["user"], ", ".join(denied)))
    cred_id = (assertion.get("rawId") or assertion.get("id")
               if isinstance(assertion, dict) else None)
    cred = next((c for c in credstore.get(req["user"]) if c["id"] == cred_id), None)
    if not cred:
        raise HTTPException(400, "unknown credential")
    try:
        new_count = await run_in_threadpool(
            webauthn_rp.verify_authentication, settings, json.dumps(assertion),
            req["digest"], cred)
    except Exception:
        audit.record("sign_denied", signer=req["user"], groups=req["groups"],
                     reason="approval_failed", principal="human")
        raise HTTPException(403, "approval verification failed")
    credstore.update_sign_count(req["user"], cred_id, new_count)   # clone detection
    return await _do_sign(body, req["groups"], req["user"],
                          {"principal": "human", "approval": "webauthn"}, proxy_token)


@app.post("/sign/cli/request")
async def cli_request(request: Request):
    """CLI, step 1: submit a manifest to be approved in the browser. Unauthenticated
    (the human approval is the authorization); bounded/expiring store limits abuse."""
    if not settings.webauthn_configured():
        raise HTTPException(503, "WebAuthn is not configured")
    body = await _read_capped(request, _CLI_MANIFEST_MAX)
    groups = _parse_manifest(body)
    digest = hashlib.sha256(body).digest()
    req_id = secrets.token_urlsafe(24)
    cli_signs.put(req_id, {"digest": digest, "groups": groups, "manifest": body,
                           "status": "pending", "envelope": None, "signed_by": None})
    approve_url = (settings.rp_origin or "").rstrip("/") + "/?approve=" + req_id
    return {"request_id": req_id, "approve_url": approve_url,
            "digest": digest.hex(), "groups": groups}


@app.get("/sign/cli/{req_id}")
def cli_pending(req_id: str, request: Request):
    """Browser approver: review a pending CLI request and get the WebAuthn
    challenge (== the digest). Requires community-admin of the request's groups."""
    data = _current(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    req = cli_signs.get(req_id)
    if not req:
        raise HTTPException(404, "no such request")
    if req["status"] != "pending":
        return {"status": req["status"], "groups": req["groups"]}
    user = data["user"]
    resolved = _resolved_policy(data["token"])
    denied = [g for g in req["groups"] if not authz.is_admin_for(user, g, resolved)]
    if denied:
        raise HTTPException(403, "%s is not a community admin for: %s"
                            % (user, ", ".join(denied)))
    creds = credstore.get(user)
    if not creds:
        raise HTTPException(400, "no passkey enrolled; enrol first")
    options = json.loads(webauthn_rp.authentication_options(settings, req["digest"], creds))
    return {"status": "pending", "groups": req["groups"], "digest": req["digest"].hex(),
            "manifest": req["manifest"].decode("utf-8", "replace"), "publicKey": options}


@app.post("/sign/cli/{req_id}/approve")
async def cli_approve(req_id: str, request: Request):
    """Browser approver: verify the digest-bound assertion and sign the stored
    manifest, storing the result for the CLI to poll."""
    data = await _current_async(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    proxy_token = _proxy_token_or_503()
    raw = await _read_capped(request, _APPROVE_MAX)
    try:
        assertion = json.loads(raw)["assertion"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "expected {assertion}")
    req = cli_signs.get(req_id)
    if not req or req["status"] != "pending":
        raise HTTPException(400, "unknown or already-handled request")
    user = data["user"]
    resolved = _resolved_policy(data["token"])
    denied = [g for g in req["groups"] if not authz.is_admin_for(user, g, resolved)]
    if denied:
        raise HTTPException(403, "%s is not a community admin for: %s"
                            % (user, ", ".join(denied)))
    cred_id = (assertion.get("rawId") or assertion.get("id")
               if isinstance(assertion, dict) else None)
    cred = next((c for c in credstore.get(user) if c["id"] == cred_id), None)
    if not cred:
        raise HTTPException(400, "unknown credential")
    req["status"] = "signing"   # claim before await so it can't be double-approved
    try:
        new_count = await run_in_threadpool(
            webauthn_rp.verify_authentication, settings, json.dumps(assertion),
            req["digest"], cred)
    except Exception:
        req["status"] = "pending"
        audit.record("sign_denied", signer=user, groups=req["groups"],
                     reason="approval_failed", principal="human", via="cli")
        raise HTTPException(403, "approval verification failed")
    credstore.update_sign_count(user, cred_id, new_count)
    try:
        result = await _do_sign(
            req["manifest"], req["groups"], user,
            {"principal": "human", "approval": "webauthn", "via": "cli"}, proxy_token)
    except BaseException:      # any failure leaves it retryable, not stuck "signing"
        req["status"] = "pending"
        raise
    req["status"] = "signed"
    req["envelope"] = result["envelope"]
    req["signed_by"] = user
    return {"status": "signed"}


@app.get("/sign/cli/{req_id}/result")
def cli_result(req_id: str):
    """CLI, step 2: poll for the signature. The signature is public, so this is
    unauthenticated (the request id is a 192-bit secret)."""
    req = cli_signs.get(req_id)
    if not req:
        raise HTTPException(404, "no such request")
    if req["status"] == "signed":
        return {"status": "signed", "envelope": req["envelope"],
                "signed_by": req["signed_by"], "groups": req["groups"]}
    return {"status": req["status"]}


@app.post("/webauthn/grant")
async def webauthn_grant(request: Request):
    """A bits-admin grants a user permission to enrol their FIRST passkey."""
    data = await _current_async(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    if not settings.webauthn_configured():
        raise HTTPException(503, "WebAuthn is not configured")
    overall, _ = authz.admin_groups(data["user"], _resolved_policy(data["token"]))
    if not overall:
        raise HTTPException(403, "only a bits admin may grant enrolment")
    raw = await _read_capped(request, 65536)
    try:
        target = json.loads(raw)["user"]
        if not isinstance(target, str) or not target:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "expected {user}")
    enroll_grants.put(target)
    audit.record("enroll_grant", granted_by=data["user"], user=target)
    return {"status": "granted", "user": target}


@app.post("/webauthn/register/begin")
def webauthn_register_begin(request: Request):
    """Start passkey enrolment. A first passkey needs a bits-admin grant (checked
    at finish); adding another needs step-up with an existing passkey (challenged
    here). The in-flight challenge is held server-side keyed by user (auth is
    stateless)."""
    data = _current(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    if not settings.webauthn_configured():
        raise HTTPException(503, "WebAuthn is not configured")
    user = data["user"]
    existing = credstore.get(user)
    options_json, challenge = webauthn_rp.registration_options(settings, user, existing)
    entry = {"reg_challenge": bytes_to_base64url(challenge), "stepup_challenge": None}
    resp = {"publicKey": json.loads(options_json)}
    if existing and settings.enrollment_authority:
        stepup = secrets.token_bytes(32)
        entry["stepup_challenge"] = bytes_to_base64url(stepup)
        resp["stepup"] = json.loads(
            webauthn_rp.authentication_options(settings, stepup, existing))
    reg_challenges.put(user, entry)
    return resp


@app.post("/webauthn/register/finish")
async def webauthn_register_finish(request: Request):
    data = await _current_async(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    user = data["user"]
    pending = reg_challenges.pop(user)   # single-use
    challenge = pending.get("reg_challenge") if pending else None
    stepup_challenge = pending.get("stepup_challenge") if pending else None
    if not challenge:
        raise HTTPException(400, "no registration in progress")
    body = await _read_capped(request, 65536)
    try:
        payload = json.loads(body)
        attestation = payload["attestation"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "expected {attestation, [stepup]}")

    existing = credstore.get(user)
    if settings.enrollment_authority:
        if existing:
            # Adding another passkey: prove control of an existing one (step-up).
            su = payload.get("stepup")
            if not stepup_challenge or not isinstance(su, dict):
                raise HTTPException(403, "step-up with an existing passkey required")
            su_id = su.get("rawId") or su.get("id")
            su_cred = next((c for c in existing if c["id"] == su_id), None)
            if not su_cred:
                raise HTTPException(400, "unknown step-up credential")
            try:
                su_count = await run_in_threadpool(
                    webauthn_rp.verify_authentication, settings, json.dumps(su),
                    base64url_to_bytes(stepup_challenge), su_cred)
            except Exception:
                raise HTTPException(403, "step-up verification failed")
            credstore.update_sign_count(user, su_cred["id"], su_count)   # clone detection
        elif not enroll_grants.take(user):
            # First passkey: requires a one-time bits-admin grant.
            raise HTTPException(403, "first enrolment requires a bits-admin grant")

    try:
        cred = webauthn_rp.verify_registration(
            settings, json.dumps(attestation), base64url_to_bytes(challenge))
    except Exception:
        raise HTTPException(400, "registration verification failed")
    credstore.add(user, cred)
    audit.record("enroll", user=user, credential_id=cred["id"],
                 authority=("stepup" if existing else "grant"))
    return {"status": "enrolled", "credential_id": cred["id"]}


