# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-console backend — relying party and the ONLY client of the security-proxy.

B1: health + config. B2: GitLab OAuth2 (Authorization Code + PKCE) performed
server-side, so the browser never holds the access token — it lives in the
server-side session. B3 (signing) and B4 (CI identity) layer on top.
"""

import secrets

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from . import authz, config, oauth, session

try:  # bits_helpers is provided by the surrounding bits repo (on PYTHONPATH).
    from bits_helpers import forge, trust  # noqa: F401  (trust used by B3)
    _BITS_HELPERS = True
except Exception:  # pragma: no cover - environment check only
    forge = None
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


@app.post("/logout")
def logout(request: Request):
    sessions.delete(request.cookies.get(_COOKIE))
    resp = JSONResponse({"status": "logged out"})
    resp.delete_cookie(_COOKIE, path="/")
    return resp
