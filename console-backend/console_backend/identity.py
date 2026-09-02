# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Identify a caller from the GitLab token their browser already holds.

The bits-console SPA authenticates the user with GitLab (OAuth PKCE) and keeps a
short-lived access token in the browser. Rather than run a second, server-side
OAuth here, this backend accepts that token as a bearer and verifies it against
GitLab (``GET /user``). The token IS the identity: only its owner can present it,
so the returned username is authenticated.

Verification uses ``Authorization: Bearer`` — required for OAuth access tokens (a
PAT also works with it). Successful lookups are cached briefly to avoid a GitLab
round-trip on every request; failures are not cached.
"""

import threading
import time

try:  # requests is a backend dependency; guard so imports never hard-fail.
    import requests
except Exception:  # pragma: no cover
    requests = None

_CACHE = {}          # token -> (username_or_None, expiry)
_LOCK = threading.Lock()
_TTL = 60            # seconds a verified identity is trusted without re-checking
_NEG_TTL = 5         # briefly cache failures too, to blunt bad-token amplification
_MAX = 4096          # cap the cache; clear wholesale when full (rare)


def _fetch(api_url, token, timeout):
    if requests is None:
        return None
    try:
        resp = requests.get("%s/user" % api_url.rstrip("/"),
                            headers={"Authorization": "Bearer " + token},
                            timeout=timeout)
        if resp.status_code != 200:
            return None
        return (resp.json() or {}).get("username")
    except Exception:
        return None


def verify_gitlab_token(api_url, token, ttl=_TTL, timeout=10):
    """Return the GitLab username owning *token*, or None if it is invalid/unusable.
    Caches a success for *ttl* seconds and a failure for a few seconds (so a stream
    of distinct bad tokens can't force one GitLab round-trip each)."""
    if not api_url or not token:
        return None
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(token)
        if hit and hit[1] > now:
            return hit[0]
    user = _fetch(api_url, token, timeout)
    with _LOCK:
        if len(_CACHE) >= _MAX:
            _CACHE.clear()
        _CACHE[token] = (user, now + (ttl if user else _NEG_TTL))
    return user
