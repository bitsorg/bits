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
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from starlette.concurrency import run_in_threadpool

from . import (audit, auth_oidc, authz, catalog, ci_auth, config, credentials,
               forge_ops, identity, session, webauthn_rp)
from fastapi.responses import RedirectResponse
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
preapprovals = session.PreapprovalStore()
enroll_grants = session.EnrollmentGrantStore()
reg_challenges = session.RegChallengeStore()
oidc_states = session.OidcStateStore()
sessions = session.SessionStore(ttl_seconds=settings.session_ttl)
catalog_cache = catalog.CatalogCache(token=settings.github_token,
                                     owners=settings.catalog_owners,
                                     ttl=settings.catalog_ttl)

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


@app.get("/approve")
def approve_page():
    """The cross-device passkey approve page, served from THIS backend's own origin
    (not the SSO-gated Pages frontend). A phone opens the QR link here and approves
    with a passkey — identity comes from the passkey, so there is no login. Fetches
    to /sign/cli/* are same-origin (no CORS, no bearer)."""
    return FileResponse(os.path.join(_STATIC, "approve.html"))


@app.get("/gh/{path:path}")
async def gh_proxy(path: str, request: Request):
    """Cached, authenticated passthrough for the console's GitHub package/recipe
    reads (repo trees, tags, branches, blobs). Public data, no bearer: the browser
    would otherwise hit GitHub unauthenticated at 60/hr per IP and run out. Locked
    to GET /repos/<allowed-owner>/... so it is never an open proxy."""
    full = "/" + path
    if not catalog_cache.allowed(full):
        raise HTTPException(status_code=403, detail="path not allowed")
    if request.url.query:
        full += "?" + request.url.query
    status, body, ctype = await run_in_threadpool(catalog_cache.fetch, full)
    return Response(content=body, status_code=status, media_type=ctype)


@app.post("/ops/build")
async def ops_build(request: Request):
    """Trigger a build pipeline on the shared project on the caller's behalf, using
    the backend's forge token — gated by the caller's identity AND their admin rights
    for the TARGET community (deny-by-default). The caller holds no forge write scope;
    this backend check is the only gate, so it must pass before anything is actuated.

    Body: {community, ref?, name?, variables?[]}. `community` is authorized against the
    single admin policy; group-admin of that community or an overall (bits-)admin may
    build. The community is resolved here — never taken from the pipeline variables."""
    data = await _current_async(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    forge = forge_ops.GitLabForge.from_settings(settings)   # per-call: settings may change
    if forge is None:
        raise HTTPException(503, "ops backend not configured (BITS_FORGE_OPS_TOKEN/PROJECT)")
    raw = await _read_capped(request, _MAX_BODY)
    try:
        payload = json.loads(raw)
        community = str(payload["community"]).strip().lower()
        ref = str(payload.get("ref") or "main").strip()
        name = payload.get("name")
        variables = payload.get("variables") or []
        if not community or len(community) > 64 \
                or not all(c.isalnum() or c in "-_." for c in community) \
                or not _valid_ref(ref) or not isinstance(variables, list) \
                or (name is not None and (not isinstance(name, str) or len(name) > 256)):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "expected {community, ref?, name?, variables[]}")
    # Community isolation (all communities share ONE project, and the backend gate
    # is the only per-user check once we act with the project token):
    #   * ref is pinned to an allowlist, so an admin cannot trigger an arbitrary
    #     ref whose committed CI config could publish elsewhere; and
    #   * the build must carry exactly one COMMUNITY variable equal to the community
    #     we authorize, so a community-A admin cannot build/publish as B.
    # NOTE (Phase 1 residual): other build variables are NOT individually reconciled
    # against `community`; full isolation also relies on the certify/sign step, which
    # re-authorizes publishing against the manifest's actual groups (M1). A complete
    # per-variable audit of the shared pipeline is a pre-production follow-up.
    if ref not in settings.forge_refs:
        raise HTTPException(400, "ref %r is not allowed for builds" % ref)
    comm_vars = [v for v in variables if isinstance(v, dict)
                 and str(v.get("key", "")).upper() == "COMMUNITY"]
    if len(comm_vars) != 1 or str(comm_vars[0].get("value", "")).strip().lower() != community:
        raise HTTPException(400, "exactly one COMMUNITY variable equal to the authorized community is required")
    user = data["user"]
    resolved = _resolved_policy(data["token"])
    if not authz.is_admin_for(user, community, resolved):
        audit.record("ops_denied", op="build", user=user, community=community, principal="human")
        raise HTTPException(403, "%s is not an admin for community %s" % (user, community))
    try:
        result = await run_in_threadpool(forge.trigger_pipeline, ref, variables, name)
    except forge_ops.ForgeError as e:
        audit.record("ops_error", op="build", user=user, community=community, status=e.status)
        raise HTTPException(502, "forge: %s" % e.message)
    audit.record("ops_build", user=user, community=community,
                 pipeline=result.get("id"), principal="human")
    return result


def _return_allowed(url):
    """The post-login redirect target must be one of the allow-listed frontend
    origins — never an attacker-supplied URL (open-redirect / token exfiltration)."""
    if not url:
        return False
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except Exception:
        return False
    return ("%s://%s" % (p.scheme, p.netloc)) in settings.login_return_allow


@app.get("/auth/login")
def auth_login(return_to: str = ""):
    """Phase 2 login, step 1: remember state + PKCE, redirect to GitLab's authorize.
    `return_to` is the allow-listed frontend URL we hand the session back to."""
    if not settings.oidc_login_configured():
        raise HTTPException(503, "OIDC login not configured")
    rt = return_to or (settings.login_return_allow[0] if settings.login_return_allow else "")
    if not _return_allowed(rt):
        raise HTTPException(400, "return_to is not an allowed frontend origin")
    state = secrets.token_urlsafe(24)
    verifier, challenge = auth_oidc.new_pkce()
    oidc_states.put(state, {"return": rt, "verifier": verifier})
    resp = RedirectResponse(auth_oidc.authorize_url(settings, state, challenge), status_code=302)
    # Bind the callback to THIS browser (login-CSRF defense). A SameSite=Lax cookie
    # is still sent on the top-level GET return from GitLab; the callback requires it
    # to equal `state`, so an attacker cannot feed a victim a pre-made callback link
    # (which would log the victim in as the attacker's identity).
    resp.set_cookie("bits_login_state", state, max_age=600, httponly=True,
                    secure=True, samesite="lax", path="/auth")
    return resp


@app.get("/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = ""):
    """Login step 2: verify state (single-use + browser-bound cookie), exchange the
    code (confidential client), verify the id_token, issue a backend session, and hand
    it back to the frontend in the URL fragment so it never hits a server log."""
    if not settings.oidc_login_configured():
        raise HTTPException(503, "OIDC login not configured")
    cookie_state = request.cookies.get("bits_login_state")
    st = oidc_states.pop(state) if state else None
    if not code or not st or not cookie_state or cookie_state != state:
        raise HTTPException(400, "invalid or expired login state")
    try:
        tokens = await run_in_threadpool(auth_oidc.exchange_code, settings, code, st["verifier"])
        claims = await run_in_threadpool(auth_oidc.verify_id_token, settings, tokens["id_token"])
    except Exception as e:                              # noqa: BLE001 - report, don't leak
        audit.record("login_failed", reason=str(e)[:120])
        raise HTTPException(401, "login verification failed")
    user, groups = auth_oidc.claims_user_groups(claims)
    if not user and tokens.get("access_token"):
        # id_token carried no username claim (scope openid without profile) — resolve
        # it from GET /user with the read_api access token (used once, not stored).
        user = await run_in_threadpool(identity.verify_gitlab_token,
                                       settings.gitlab_api_url, tokens["access_token"])
    if not user:
        raise HTTPException(401, "no user identity in token")
    token = secrets.token_urlsafe(32)
    sessions.put(token, user, groups)
    audit.record("login", user=user, groups=groups, principal="human")
    sep = "&" if "#" in st["return"] else "#"
    resp = RedirectResponse(st["return"] + sep + "bits_session=" + token, status_code=302)
    resp.delete_cookie("bits_login_state", path="/auth")
    return resp


@app.post("/auth/logout")
async def auth_logout(request: Request):
    """Revoke the caller's backend session."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        sessions.revoke(auth[len("Bearer "):].strip())
    return {"status": "logged out"}


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
    # Phase 2: a backend session bearer takes precedence — identity + e-groups come
    # from it, and no GitLab token is involved. `token` is None so callers know there
    # is no GitLab credential to act with (authz uses the session's groups: P2.2).
    sess = sessions.get(token)
    if sess:
        return {"user": sess["user"], "groups": sess["groups"],
                "token": None, "session": token}
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


_pubkey_cache = {"kid": None, "exp": 0.0}


@app.get("/trust/pubkey")
def trust_pubkey():
    """Public: the current signing key's id, so a signer (e.g. the certify CI job)
    can do its producer-side key/group check without holding the key. Read from the
    proxy; the private key never leaves it. Cached briefly so an unauthenticated
    flood doesn't translate 1:1 into proxy calls (the key rarely rotates)."""
    proxy_token = _proxy_token_or_503()
    now = time.time()
    if _pubkey_cache["kid"] and _pubkey_cache["exp"] > now:
        return {"key_id": _pubkey_cache["kid"]}
    try:
        kid, _pub = trust.proxy_pubkey(settings.sign_proxy_url, proxy_token)
    except RuntimeError as exc:
        raise HTTPException(502, "signing proxy pubkey failed: %s" % exc)
    _pubkey_cache.update(kid=kid, exp=now + 60)
    return {"key_id": kid}


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
# A build's pre-approval signs one manifest PER ARCHITECTURE (certify signs each),
# so it is bounded multi-use, not single-use: this caps how many signatures one
# human pre-approval can produce — generous for a build's arches (+ shared), tight
# enough to bound a compromised CI reusing a live pre-approval within its TTL.
_PREAPPROVAL_SIGN_CAP = 16
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


def _valid_build_id(build_id) -> bool:
    """A build_id is a CI pipeline id: short, alnum plus - _ . only. Bounding it
    keeps it safe as a store key and in audit lines (no log injection / bloat)."""
    return bool(build_id) and len(build_id) <= 64 \
        and all(c.isalnum() or c in "-_." for c in build_id)


def _valid_ref(ref) -> bool:
    """A git ref (branch/tag) to build. Bounded, no shell/log-hostile characters."""
    return bool(ref) and len(ref) <= 255 \
        and all(c.isalnum() or c in "-_./" for c in ref)


def _new_challenge(digest):
    """A per-request WebAuthn challenge binding the manifest digest (content) AND a
    fresh 128-bit nonce. The digest keeps the assertion tied to these exact bytes;
    the nonce makes it single-use — a captured assertion for a manifest can't be
    replayed against a NEW request for the same (identical) manifest."""
    return hashlib.sha256(digest + secrets.token_bytes(16)).digest()


@app.post("/sign/request")
async def sign_request(request: Request):
    """Human approval, step 1: authorize and return a per-request WebAuthn challenge
    binding the manifest digest (content) plus a nonce (freshness). The manifest is
    held server-side so approval signs exactly these bytes."""
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
    challenge = _new_challenge(digest)
    req_id = secrets.token_urlsafe(24)
    sign_requests.put(req_id, {"digest": digest, "challenge": challenge,
                               "groups": groups, "user": user})
    options = json.loads(webauthn_rp.authentication_options(settings, challenge, creds))
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
            req["challenge"], cred)
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
    cli_signs.put(req_id, {"digest": digest, "challenge": _new_challenge(digest),
                           "groups": groups, "manifest": body,
                           "status": "pending", "envelope": None, "signed_by": None})
    # The caller (CLI) builds the approve URL itself from the backend URL it already
    # connected to (its --console arg) + /approve?approve=<request_id>. The backend
    # does NOT construct it: that avoids trusting a proxied Host header and needs no
    # backend-origin config. The approve page is served here at /approve.
    return {"request_id": req_id, "digest": digest.hex(), "groups": groups}


@app.get("/sign/cli/{req_id}")
def cli_pending(req_id: str):
    """Browser approver: review a pending CLI request and get the WebAuthn
    challenge (== the digest). Unauthenticated by bearer: the 192-bit req_id is
    the secret (as with /result), and the approver is identified by their passkey
    at approve time. Uses discoverable credentials (empty allowCredentials) so the
    device can offer any enrolled passkey for the RP — no user is named here."""
    if not settings.webauthn_configured():
        raise HTTPException(503, "WebAuthn is not configured")
    req = cli_signs.get(req_id)
    if not req:
        raise HTTPException(404, "no such request")
    if req["status"] != "pending":
        return {"status": req["status"], "groups": req["groups"]}
    options = json.loads(webauthn_rp.authentication_options(settings, req["challenge"], []))
    return {"status": "pending", "groups": req["groups"], "digest": req["digest"].hex(),
            "manifest": req["manifest"].decode("utf-8", "replace"), "publicKey": options}


@app.post("/sign/cli/{req_id}/approve")
async def cli_approve(req_id: str, request: Request):
    """Browser approver: verify the digest-bound assertion and sign the stored
    manifest, storing the result for the CLI to poll. Passkey-only (no bearer):
    the approver is the enrolled user the assertion's credential maps to — which
    is safe because enrolment is the gated step (admin + grant). Admin authority
    is re-checked from that user against the service-token-resolved policy."""
    proxy_token = _proxy_token_or_503()
    raw = await _read_capped(request, _APPROVE_MAX)
    try:
        assertion = json.loads(raw)["assertion"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "expected {assertion}")
    req = cli_signs.get(req_id)
    if not req or req["status"] != "pending":
        raise HTTPException(400, "unknown or already-handled request")
    cred_id = (assertion.get("rawId") or assertion.get("id")
               if isinstance(assertion, dict) else None)
    found = credstore.user_for(cred_id)
    if not found:
        raise HTTPException(400, "unknown credential")
    user, cred = found
    # Re-check admin from the passkey's user. No approver token is available on
    # this path, so resolve with the service token (static `* @user` policies
    # need no token at all).
    resolved = _resolved_policy(None)
    denied = [g for g in req["groups"] if not authz.is_admin_for(user, g, resolved)]
    if denied:
        audit.record("sign_denied", signer=user, groups=req["groups"],
                     denied=denied, principal="human", via="cli")
        raise HTTPException(403, "%s is not a community admin for: %s"
                            % (user, ", ".join(denied)))
    # Claim before the first await so concurrent approves can't both sign. The
    # section above (get -> user_for -> policy -> is_admin_for) is all synchronous,
    # so under the single-threaded event loop it runs to here atomically — keep it
    # await-free or this guarantee breaks.
    req["status"] = "signing"
    try:
        new_count = await run_in_threadpool(
            webauthn_rp.verify_authentication, settings, json.dumps(assertion),
            req["challenge"], cred)
    except Exception:
        req["status"] = "pending"
        audit.record("sign_denied", signer=user, groups=req["groups"],
                     reason="approval_failed", principal="human", via="cli")
        raise HTTPException(403, "approval verification failed")
    try:      # any failure past the claim leaves it retryable, not stuck "signing"
        credstore.update_sign_count(user, cred_id, new_count)
        result = await _do_sign(
            req["manifest"], req["groups"], user,
            {"principal": "human", "approval": "webauthn", "via": "cli"}, proxy_token)
    except BaseException:
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


@app.post("/preapprove/request")
async def preapprove_request(request: Request):
    """Build/publish pre-approval, step 1. A logged-in admin authorizes signing the
    manifest a specific build (build_id) will produce, BEFORE it exists. Returns a
    WebAuthn challenge; the human approves the BUILD, and CI signs the resulting
    manifest later (F2), gated by this pre-approval and stamped "pre-approved by X"."""
    data = await _current_async(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    if not settings.webauthn_configured():
        raise HTTPException(503, "WebAuthn is not configured")
    raw = await _read_capped(request, _APPROVE_MAX)
    try:
        payload = json.loads(raw)
        bid = payload["build_id"]
        if not isinstance(bid, (str, int)):
            raise ValueError
        build_id = str(bid).strip()
        groups = payload.get("groups") or []
        if not _valid_build_id(build_id) or not isinstance(groups, list) \
                or not all(isinstance(g, str) and g for g in groups):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "expected {build_id, groups[]}")
    user = data["user"]
    resolved = _resolved_policy(data["token"])
    denied = [g for g in groups if not authz.is_admin_for(user, g, resolved)]
    if denied:
        audit.record("preapprove_denied", signer=user, groups=groups, denied=denied,
                     principal="human")
        raise HTTPException(403, "%s is not a community admin for: %s"
                            % (user, ", ".join(denied)))
    # Don't let a fresh request silently destroy a live approval CI may still be
    # consuming (a build signs once per architecture over the record's TTL).
    existing = preapprovals.get(build_id)
    if existing and existing["status"] == "approved":
        raise HTTPException(409, "build %s is already pre-approved" % build_id)
    creds = credstore.get(user)
    if not creds:
        raise HTTPException(400, "no passkey enrolled; enrol first")
    # No manifest yet, so the challenge is a fresh nonce; the pre-approval RECORD
    # binds build_id + groups + user, which the sign step (F2) checks.
    challenge = secrets.token_bytes(32)
    preapprovals.put(build_id, {"groups": groups, "user": user, "challenge": challenge,
                                "status": "pending"})
    options = json.loads(webauthn_rp.authentication_options(settings, challenge, creds))
    return {"build_id": build_id, "groups": groups, "publicKey": options}


@app.post("/preapprove/approve")
async def preapprove_approve(request: Request):
    """Build/publish pre-approval, step 2. Verify the passkey assertion and mark the
    build_id pre-approved for signing (by this admin). CI consumes it later (F2)."""
    data = await _current_async(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    raw = await _read_capped(request, _APPROVE_MAX)
    try:
        payload = json.loads(raw)
        build_id = str(payload["build_id"]).strip()
        assertion = payload["assertion"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(400, "expected {build_id, assertion}")
    req = preapprovals.get(build_id)
    if not req or req["status"] != "pending":
        raise HTTPException(400, "unknown or already-handled pre-approval")
    user = data["user"]
    if req["user"] != user:
        raise HTTPException(403, "pre-approval belongs to another user")
    # Re-check admin now (close the revoke-within-window gap).
    resolved = _resolved_policy(data["token"])
    denied = [g for g in req["groups"] if not authz.is_admin_for(user, g, resolved)]
    if denied:
        audit.record("preapprove_denied", signer=user, groups=req["groups"],
                     denied=denied, principal="human")
        raise HTTPException(403, "%s is not a community admin for: %s"
                            % (user, ", ".join(denied)))
    cred_id = (assertion.get("rawId") or assertion.get("id")
               if isinstance(assertion, dict) else None)
    cred = next((c for c in credstore.get(user) if c["id"] == cred_id), None)
    if not cred:
        raise HTTPException(400, "unknown credential")
    try:
        new_count = await run_in_threadpool(
            webauthn_rp.verify_authentication, settings, json.dumps(assertion),
            req["challenge"], cred)
    except Exception:
        audit.record("preapprove_denied", signer=user, groups=req["groups"],
                     reason="approval_failed", principal="human")
        raise HTTPException(403, "approval verification failed")
    credstore.update_sign_count(user, cred_id, new_count)
    # Re-fetch the live record: a concurrent request could have replaced it (or a
    # second approve already flipped it) during the verify await. Only flip if it is
    # still the same pending record for this user.
    live = preapprovals.get(build_id)
    if not live or live["status"] != "pending" or live.get("user") != user:
        raise HTTPException(409, "pre-approval changed during approval; retry")
    live["status"] = "approved"
    audit.record("preapproved", signer=user, groups=live["groups"], build_id=build_id,
                 principal="human")
    return {"status": "approved", "build_id": build_id}


@app.post("/sign/preapproved")
async def sign_preapproved(request: Request):
    """CI signs a manifest for a build a human PRE-APPROVED. Requires BOTH a CI OIDC
    identity (the BITS_CI_SIGNERS gate) AND an approved pre-approval for build_id whose
    groups cover the manifest. Signs the exact bytes via the proxy and stamps provenance
    'pre-approved by <user>'. Bounded multi-use: one build is signed once per arch, so
    the same pre-approval signs several manifests up to a cap, within its TTL."""
    proxy_token = _proxy_token_or_503()
    build_id = request.query_params.get("build_id", "").strip()
    if not _valid_build_id(build_id):
        raise HTTPException(400, "valid build_id query parameter required")
    body = await _read_capped(request, _MAX_BODY)
    groups = _parse_manifest(body)
    if not groups:                       # _parse_manifest is fail-closed; belt-and-braces
        raise HTTPException(400, "manifest has no package groups")
    signer, principal, denied = await _authorize_sign(request, groups)
    if principal.get("principal") != "ci":
        raise HTTPException(403, "this endpoint requires a CI identity")
    if denied:
        audit.record("sign_denied", signer=signer, groups=groups, denied=denied,
                     build_id=build_id, **principal)
        raise HTTPException(403, "%s is not authorized to sign: %s"
                            % (signer, ", ".join(denied)))
    # A human must have pre-approved THIS build, covering these groups.
    pre = preapprovals.get(build_id)
    if not pre or pre["status"] != "approved":
        audit.record("sign_denied", signer=signer, groups=groups, build_id=build_id,
                     reason="no_preapproval", **principal)
        raise HTTPException(403, "no human pre-approval for build %s" % build_id)
    # The manifest's groups must all be within the AUTHORITY of the human who
    # pre-approved this build (resolved via the service token / static policy) — not
    # a declared list: a build's manifest can legitimately include packages from
    # other groups (cross-group builds) the SPA could not foresee at approval time.
    resolved = _resolved_policy(None)
    denied2 = [g for g in groups if not authz.is_admin_for(pre["user"], g, resolved)]
    if denied2:
        audit.record("sign_denied", signer=signer, groups=groups, denied=denied2,
                     reason="preapprover_not_admin", build_id=build_id,
                     preapproved_by=pre["user"], **principal)
        raise HTTPException(403, "%s (who pre-approved build %s) is not an admin for: %s"
                            % (pre["user"], build_id, ", ".join(denied2)))
    # Bounded multi-use: one build is signed once per architecture, so the same
    # pre-approval signs several manifests — capped, and only within its TTL. RESERVE
    # the slot before the sign await (this check+increment is synchronous, so it is
    # atomic against the event loop — concurrent calls cannot both pass the cap), and
    # release it on failure so a failed sign does not burn the build's budget.
    if pre.get("signs", 0) >= _PREAPPROVAL_SIGN_CAP:
        audit.record("sign_denied", signer=signer, groups=groups, build_id=build_id,
                     reason="preapproval_sign_cap", **principal)
        raise HTTPException(429, "pre-approval for build %s reached its sign limit" % build_id)
    pre["signs"] = pre.get("signs", 0) + 1
    meta = {"principal": "ci", "project": principal.get("project"),
            "approval": "preapproved", "preapproved_by": pre["user"], "build_id": build_id}
    try:
        result = await _do_sign(body, groups, signer, meta, proxy_token)
    except BaseException:
        pre["signs"] = max(0, pre.get("signs", 1) - 1)   # release the reserved slot
        raise
    # Provenance is ADVISORY and authoritative in the audit log. Do NOT put it inside
    # the envelope: the signature covers the manifest bytes only, so envelope fields
    # would look signed but are forgeable. Return as sibling fields for CI to record.
    result["approval"] = "preapproved"
    result["preapproved_by"] = pre["user"]
    result["build_id"] = build_id
    return result


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


