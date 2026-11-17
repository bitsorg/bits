# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits-store's admin API (its README, "The admin API"), for the console's Store page:
reached on its own address (BITS_STORE_ADMIN_URL, an internal network) with a short
admin token per request, never through the store's public address."""

import re
from urllib.parse import parse_qsl

import requests

VIEWS = ("summary", "objects", "object", "access")  # GET /_admin/<view>, read only
MAX_QUERY = 2048
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
    try:
        body = r.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        body = {"error": "not found"} if r.status_code == 404 else None
    if body is None:
        raise Unreachable("bits-store's admin API answered %d without JSON" % r.status_code)
    return r.status_code, body
