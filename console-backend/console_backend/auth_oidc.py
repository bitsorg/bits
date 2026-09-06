# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase 2: the backend as a GitLab OIDC client. Runs the authorization-code flow on
/auth/login + /auth/callback, verifies the returned id_token, and yields the user +
groups so the backend can issue its OWN session — the browser then holds no GitLab
token. Confidential client (client secret) plus PKCE for defense in depth. The
id_token is verified against GitLab's JWKS (reused from ci_auth)."""

import base64
import hashlib
import secrets
from urllib.parse import urlencode

import jwt
import requests

from . import ci_auth   # reuse the cached PyJWKClient


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def new_pkce():
    """(verifier, challenge) for PKCE S256."""
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def authorize_url(settings, state, challenge, scope="openid email profile", max_age=None):
    params = {
        "client_id": settings.oidc_login_client_id,
        "redirect_uri": settings.oidc_login_redirect,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if max_age is not None:
        params["max_age"] = str(int(max_age))   # timeout-based re-auth (P2.3)
    return "%s?%s" % (settings.oidc_authorize_url, urlencode(params))


def exchange_code(settings, code, verifier, timeout=15):
    """Exchange the auth code for tokens (confidential client). Returns the id_token
    JWT string, or raises ValueError. The client secret never leaves the backend."""
    r = requests.post(settings.oidc_token_url, timeout=timeout, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_login_redirect,
        "client_id": settings.oidc_login_client_id,
        "client_secret": settings.oidc_login_client_secret,
        "code_verifier": verifier,
    })
    if r.status_code // 100 != 2:
        raise ValueError("token endpoint returned %s" % r.status_code)
    tok = (r.json() or {}).get("id_token")
    if not tok:
        raise ValueError("no id_token in token response")
    return tok


def verify_id_token(settings, id_token):
    """Verify signature (JWKS), issuer, audience (== our client id) and exp; return
    the claims, or raise."""
    key = ci_auth._jwks_client(settings.jwks_url).get_signing_key_from_jwt(id_token).key
    return jwt.decode(id_token, key, algorithms=["RS256"],
                      audience=settings.oidc_login_client_id,
                      issuer=settings.oidc_issuer, leeway=30,   # small clock skew
                      options={"require": ["exp", "iat", "aud", "iss", "sub"]})


def claims_user_groups(claims):
    """Username + e-groups from GitLab OIDC claims (for identity + authz)."""
    user = (claims.get("preferred_username") or claims.get("nickname")
            or claims.get("sub") or "")
    groups = claims.get("groups_direct") or claims.get("groups") or []
    if isinstance(groups, str):
        groups = [groups]
    return user, [g for g in groups if isinstance(g, str)]
