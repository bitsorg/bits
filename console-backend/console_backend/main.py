# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-console backend — relying party and the ONLY client of the security-proxy.

B1: health + config. B2: GitLab OAuth2 (Authorization Code + PKCE) performed
server-side, so the browser never holds the access token — it lives in the
server-side session. B3 (signing) and B4 (CI identity) layer on top.
"""

import hashlib
import json
import os
import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from . import audit, authz, config, oauth, session

try:  # bits_helpers is provided by the surrounding bits repo (on PYTHONPATH).
    from bits_helpers import forge, trust
    _BITS_HELPERS = True
except Exception:  # pragma: no cover - environment check only
    forge = None
    trust = None
    _BITS_HELPERS = False

settings = config.Settings()
sessions = session.SessionStore(settings.session_ttl_seconds)
login_states = session.LoginStateStore()

app = FastAPI(title="bits-console backend", version="0.1.0")

_COOKIE = "bits_session"
_STATE_COOKIE = "bits_oauth_state"


def _current(request: Request):
    return sessions.get(request.cookies.get(_COOKIE))


@app.get("/healthz")
def healthz():
    """Liveness + wiring status. No secrets in the response."""
    return {
        "status": "ok",
        "bits_helpers": _BITS_HELPERS,
        "sign_proxy_configured": settings.sign_proxy_configured(),
        "oidc_configured": settings.oidc_configured(),
    }


@app.get("/login")
def login():
    """Start GitLab OAuth: redirect to the authorize endpoint with PKCE + state."""
    if not settings.oidc_configured():
        raise HTTPException(503, "OIDC is not configured")
    verifier, challenge = oauth.new_pkce()
    state = secrets.token_urlsafe(24)
    login_states.put(state, verifier)
    resp = RedirectResponse(oauth.authorize_url(settings, state, challenge),
                            status_code=302)
    # Bind the state to THIS browser (login-CSRF defence): the callback must
    # present the same value back in a cookie, so an attacker cannot complete a
    # login they started into the victim's browser.
    resp.set_cookie(_STATE_COOKIE, state, httponly=True,
                    secure=settings.session_cookie_secure, samesite="lax",
                    max_age=600, path="/")
    return resp


@app.get("/oauth-callback")
def oauth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    """Exchange the code server-side, identify the user, establish a session."""
    if error:
        raise HTTPException(400, "authorization was denied or failed")
    if not code or not state:
        raise HTTPException(400, "missing code or state")
    # Login-CSRF: the state must match the one bound to this browser at /login.
    if request.cookies.get(_STATE_COOKIE) != state:
        raise HTTPException(400, "state does not match this browser")
    verifier = login_states.take(state)   # single-use; None if unknown/expired
    if verifier is None:
        raise HTTPException(400, "invalid or expired login state")
    try:
        token = oauth.exchange_code(settings, code, verifier)
    except RuntimeError as exc:
        raise HTTPException(502, str(exc))
    try:
        user = forge.gitlab_identify(settings.gitlab_api_url, token) if forge else None
    except Exception:
        raise HTTPException(502, "GitLab identity lookup failed")
    if not user:
        raise HTTPException(401, "could not identify the GitLab user")
    sid = sessions.create({"user": user, "token": token})
    # Fixed redirect target (never a user-supplied URL) to avoid open redirects.
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(_COOKIE, sid, httponly=True, secure=settings.session_cookie_secure,
                    samesite="lax", max_age=settings.session_ttl_seconds, path="/")
    resp.delete_cookie(_STATE_COOKIE, path="/")
    return resp


def _resolved_policy(token):
    return authz.resolved_admin_policy(settings, token)


def require_community_admin(request: Request, group: str) -> str:
    """Gate for signing (B3): 401 if not logged in, 400 if no group is given,
    403 if the session user is not an admin of *group*. Returns the username on
    success. A falsy *group* must NOT fall through to any default (fail closed)."""
    data = _current(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    if not group:
        raise HTTPException(400, "no group specified")
    user = data["user"]
    if not authz.is_admin_for(user, group, _resolved_policy(data["token"])):
        raise HTTPException(403, "%s is not a community admin for '%s'" % (user, group))
    return user


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


@app.post("/sign")
async def sign(request: Request):
    """Mode 1: sign a completed common manifest through the security-proxy.

    Signs the EXACT submitted bytes (so the signature matches what consumers
    verify). Authorization is derived from the manifest content — the caller must
    be a community admin of *every* group present — and the proxy key must be
    authorized for those groups (bits key-policy). The GitLab token and the gate
    token never appear in the response or the audit record.
    """
    data = _current(request)
    if not data:
        raise HTTPException(401, "not authenticated")
    if not settings.sign_proxy_configured():
        raise HTTPException(503, "signing proxy is not configured")
    proxy_token = os.environ.get(settings.sign_proxy_token_env)
    if not proxy_token:
        raise HTTPException(503, "signing proxy token is not available")

    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BODY:
        raise HTTPException(413, "manifest too large")
    body = await request.body()          # the exact bytes to sign
    if len(body) > _MAX_BODY:
        raise HTTPException(413, "manifest too large")
    try:
        manifest = json.loads(body)
        if not isinstance(manifest, dict):
            raise ValueError
    except (ValueError, RecursionError):
        raise HTTPException(400, "request body is not a JSON manifest")
    groups = _groups_of(manifest)

    user = data["user"]
    resolved = _resolved_policy(data["token"])
    denied = [g for g in groups if not authz.is_admin_for(user, g, resolved)]
    if denied:
        audit.record("sign_denied", user=user, groups=groups,
                     reason="not_community_admin", denied=denied)
        raise HTTPException(403, "%s is not a community admin for: %s"
                            % (user, ", ".join(denied)))

    # Producer-side key-policy: the proxy key must be allowed to certify these
    # groups (mirrors certify._prepare_common). Blocking urllib calls are run off
    # the event loop so a slow proxy cannot stall other requests.
    try:
        kid, _pub = await run_in_threadpool(
            trust.proxy_pubkey, settings.sign_proxy_url, proxy_token)
    except RuntimeError as exc:
        raise HTTPException(502, "signing proxy pubkey failed: %s" % exc)
    policy = trust.load_key_policy()
    if policy is not None:
        badk = [g for g in groups if not trust.key_authorized(kid, g, policy)]
        if badk:
            audit.record("sign_denied", user=user, groups=groups,
                         reason="key_not_authorized", key_id=kid, denied=badk)
            raise HTTPException(403, "signing key %s is not authorized for: %s"
                                % (kid, ", ".join(badk)))

    try:
        envelope = await run_in_threadpool(
            trust.sign_bytes_via_proxy, body, settings.sign_proxy_url, proxy_token)
    except RuntimeError as exc:
        raise HTTPException(502, "signing failed: %s" % exc)

    digest = hashlib.sha256(body).hexdigest()
    audit.record("sign", user=user, groups=groups, digest=digest,
                 key_id=envelope["key_id"])
    return {"envelope": envelope, "groups": groups, "signed_by": user, "digest": digest}


@app.post("/logout")
def logout(request: Request):
    sessions.delete(request.cookies.get(_COOKIE))
    resp = JSONResponse({"status": "logged out"})
    resp.delete_cookie(_COOKIE, path="/")
    return resp
