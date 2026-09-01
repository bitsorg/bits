# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""GitLab CI ID token (OIDC workload identity) verification + CI-signer policy.

A pipeline presents a short-lived GitLab-signed JWT (RS256). We verify signature
(against GitLab's JWKS), issuer, audience and expiry, then authorize by the token's
``project_path`` against a per-project group allow-list. No human is involved, so
this is the automation front door (design §3, Mode 3).
"""

import os

import jwt
from jwt import PyJWKClient

_JWKS_CLIENTS = {}   # jwks_url -> PyJWKClient (module-level so its key cache persists)


def _jwks_client(url):
    client = _JWKS_CLIENTS.get(url)
    if client is None:
        client = PyJWKClient(url, cache_keys=True)
        _JWKS_CLIENTS[url] = client
    return client


def load_ci_signers(settings) -> dict:
    """Parse ``<project_path> <group>...`` lines (``*`` = any group) into
    ``{project_path: {groups}}``. A project with no groups listed authorizes
    nothing (fail-closed)."""
    src = settings.ci_signers_source
    text = open(src).read() if (src and os.path.isfile(src)) else (src or "")
    policy = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        toks = line.split()
        policy[toks[0]] = set(toks[1:])
    return policy


def is_ci_authorized(project: str, group: str, policy: dict) -> bool:
    allowed = policy.get(project)
    if not allowed:
        return False
    return "*" in allowed or group in allowed


def verify_ci_token(token: str, settings, signing_key=None) -> dict:
    """Verify a GitLab CI ID token and return its claims, or raise. Enforces
    RS256, issuer, audience and the presence of exp/iat/aud/iss/sub."""
    if not (settings.oidc_issuer and settings.oidc_ci_audience
            and (signing_key or settings.jwks_url)):
        raise ValueError("CI token verification is not configured")
    key = signing_key
    if key is None:
        if not settings.jwks_url.startswith("https://"):
            raise ValueError("jwks_url must be https")   # no cleartext key fetch
        key = _jwks_client(settings.jwks_url).get_signing_key_from_jwt(token).key
    return jwt.decode(
        token, key=key, algorithms=["RS256"],
        audience=settings.oidc_ci_audience, issuer=settings.oidc_issuer,
        options={"require": ["exp", "iat", "aud", "iss", "sub"]})
