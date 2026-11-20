# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-store's admin API (its README, "The admin API"), for the console's Store page:
reached on its own address (BITS_STORE_ADMIN_URL, an internal network) with a short
admin token per request, never through the store's public address."""

import base64
import json
import re
import secrets
import time
from urllib.parse import parse_qsl

import requests

from . import audit, signproxy, store_token

VIEWS = ("summary", "objects", "object", "access", "withdrawn", "gc", "gc/files", "gc/log",  # GET
         "problems", "git", "fills")
ACTIONS = ("withdraw", "lift", "delete", "gc/plan", "gc/apply")  # PUT, a JSON object
MAX_QUERY = 2048
MAX_BODY = 4096                                     # as bits-store takes them
AUDITED = ("binding", "sha256", "key", "etag", "id", "files", "why",  # an action's fields, audited
           "grace", "read", "reads", "keep_builds", "max")
TOKEN_TTL = 60                                      # a token per request: a minute
_STRAY_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")


class Unreachable(Exception):
    """bits-store did not answer as its admin API does (down, refused the token)."""


def params(query):
    """The query's (name, value) pairs, decoded as bits-store would; ValueError when it
    is too long, not ASCII (escape the rest), has a % not followed by two hex digits,
    or does not decode to UTF-8."""
    if len(query) > MAX_QUERY:
        raise ValueError("too long")
    if not query.isascii() or _STRAY_PERCENT.search(query):
        raise ValueError("not ASCII with %XX escapes")
    try:
        return parse_qsl(query, keep_blank_values=True, errors="strict")
    except UnicodeDecodeError:
        raise ValueError("an escape that is not UTF-8") from None


def get(base, view, pairs, token, timeout=(5, 60)):
    """(status, JSON body) of GET <base>/_admin/<view> with *pairs* as its query:
    bits-store's own answer when it took the token (200, 400, 404); Unreachable
    otherwise. No proxy from the environment: the admin API is on an internal network."""
    if view not in VIEWS:
        raise ValueError("not an admin view: %r" % (view,))
    with requests.Session() as s:
        s.trust_env = False
        try:
            r = s.get("%s/_admin/%s" % (base.rstrip("/"), view), params=pairs,
                      headers={"Authorization": "Bearer " + token}, timeout=timeout, allow_redirects=False)
        except requests.RequestException as exc:
            raise Unreachable("bits-store's admin API: %s" % type(exc).__name__) from None
    if r.status_code not in (200, 400, 404):    # 401/403: the store does not take the console's key
        raise Unreachable("bits-store's admin API answered %d" % r.status_code)
    if view == "gc/log" and r.status_code == 200:
        return 200, r.text                      # plain text
    return _answer(r)


_KEY = re.compile(r"[a-z0-9_]+")


def body(raw):
    """The action's JSON object; ValueError unless each key is lower-case ASCII and
    once: bits-store's Go decoder folds case and keeps the last, so the audit reads
    exactly the fields it acts on."""
    def pairs(items):
        keys = [k for k, _ in items]
        if len(set(keys)) != len(keys) or not all(_KEY.fullmatch(k) for k in keys):
            raise ValueError("keys: lower-case, each once")
        return dict(items)
    try:
        obj = json.loads(raw, object_pairs_hook=pairs)
    except RecursionError:
        raise ValueError("too deep") from None
    if not isinstance(obj, dict):
        raise ValueError("not a JSON object")
    return obj


def put(base, action, body, token, timeout=(5, 60)):
    """(status, JSON body) of PUT <base>/_admin/<action> with the JSON *body* (bytes):
    bits-store's answer when it took the token (200, 202, 400, 404, 409); else Unreachable."""
    if action not in ACTIONS:
        raise ValueError("not an admin action: %r" % (action,))
    with requests.Session() as s:
        s.trust_env = False
        try:
            r = s.put("%s/_admin/%s" % (base.rstrip("/"), action), data=body, timeout=timeout,
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
                      allow_redirects=False)
        except requests.RequestException as exc:
            raise Unreachable("bits-store's admin API: %s" % type(exc).__name__) from None
    if r.status_code not in (200, 202, 400, 404, 409):
        raise Unreachable("bits-store's admin API answered %d" % r.status_code)
    return _answer(r)


def _answer(r):
    try:
        body = r.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {"error": "not found"} if r.status_code == 404 else None
    if body is None:
        raise Unreachable("bits-store's admin API answered %d without JSON" % r.status_code)
    return r.status_code, body


def admin_token(settings, sub, caps, ttl=TOKEN_TTL):
    """(token, jti) of bits-store's admin key for *sub* (admin:<who>), allowing *caps*:
    signed by the security proxy's route when set, else from the key's seed. For a
    caller outside a request (the orchestrator); the API signs its own (main.py)."""
    st = settings
    key = st.store_admin_token_key
    if st.store_admin_token_sign_route:
        from bits_helpers import trust  # the surrounding bits repo's

        def key(data):
            env = signproxy.call(st, trust.sign_bytes_via_proxy, data, route=st.store_admin_token_sign_route,
                                 prefix=st.store_admin_token_sign_prefix,
                                 agent_socket=st.store_token_sign_socket or None,
                                 host=(st.store_token_sign_host or None) if st.store_token_sign_socket else None)
            sig = base64.b64decode(env.get("sig", ""), validate=True)
            if len(sig) != 64:
                raise RuntimeError("the sign proxy returned no Ed25519 signature")
            return sig
        signer = store_token.StoreTokenSigner(st.store_admin_token_kid, key, st.store_token_issuer,
                                              st.store_token_audience, signer="admin")
    else:
        signer = store_token.StoreTokenSigner.from_seed(st.store_admin_token_kid, key, st.store_token_issuer,
                                                        st.store_token_audience, signer="admin")
    jti, now = secrets.token_urlsafe(16), int(time.time())
    tok = signer.sign(sub, "admin", caps, ttl, now=now, jti=jti)
    audit.record("store_token", user=sub, group="admin", jti=jti, exp=now + ttl, principal="worker")
    return tok, jti


def problems(settings, since, sub="admin:bits-orchestrator"):
    """bits-store's problems last seen at or after *since* (ms), oldest first (at
    most 1000: ask again from the last one's); Unreachable when it does not answer."""
    tok, _ = admin_token(settings, sub, [("GET", "/_admin/problems")])
    code, body = get(settings.store_admin_url, "problems", [("since", str(int(since))), ("limit", "1000")], tok)
    if code != 200:
        raise Unreachable("bits-store's problems answered %d (no index?)" % code)
    return body.get("problems") or []
