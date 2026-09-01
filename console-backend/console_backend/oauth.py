# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""GitLab OAuth2 Authorization-Code + PKCE, performed server-side.

The browser never sees the access token: /login redirects to GitLab, the callback
exchanges the code here, and the token is kept in the server-side session.
"""

import base64
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request

_TIMEOUT = 15


def new_pkce():
    """Return ``(code_verifier, code_challenge)`` for PKCE S256."""
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode("ascii")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


def authorize_url(settings, state: str, challenge: str) -> str:
    q = {
        "client_id": settings.oidc_client_id,
        "response_type": "code",
        "redirect_uri": settings.oidc_redirect_uri,
        "scope": settings.oidc_scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return settings.oidc_authorize_url + "?" + urllib.parse.urlencode(q)


def exchange_code(settings, code: str, verifier: str) -> str:
    """Exchange an authorization code for an access token. Returns the token, or
    raises RuntimeError. The token/secret never appear in the error text."""
    data = {
        "client_id": settings.oidc_client_id,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri,
        "code_verifier": verifier,
    }
    if settings.oidc_client_secret:
        data["client_secret"] = settings.oidc_client_secret
    req = urllib.request.Request(
        settings.oidc_token_url, data=urllib.parse.urlencode(data).encode(),
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as fh:
            tok = json.load(fh)
    except urllib.error.HTTPError as e:
        raise RuntimeError("token exchange failed: HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise RuntimeError("token endpoint unreachable: %s" % e.reason)
    except ValueError:
        raise RuntimeError("token endpoint returned a non-JSON body")
    tok_val = tok.get("access_token") if isinstance(tok, dict) else None
    if not tok_val:
        raise RuntimeError("token response had no access_token")
    return tok_val
