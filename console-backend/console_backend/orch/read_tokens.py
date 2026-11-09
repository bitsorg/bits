# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read tokens: what a script or an agent reads the orchestrator with, without a
GitLab token. `bitsread.v1.<payload>.<signature>`, the payload base64url JSON
{sub, communities, exp, jti} and the signature HMAC-SHA256 over the text before it
with BITS_READ_TOKEN_KEY. A token is good only while its row (jti) is in orch.db,
unrevoked and unexpired; it reads only ROUTES, by GET, and only its communities
("*": every one, the overall admins' reads too)."""

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3

from . import db

PREFIX = "bitsread.v1."
MAX_DAYS = 90
MIN_KEY = 32                 # characters of BITS_READ_TOKEN_KEY, at least
USED_EVERY_MS = 60 * 1000    # last_used_ms is written at most this often
ROUTES = frozenset((
    "/ops/failures",
    "/ops/builds/{pipeline}",
    "/ops/builds/{pipeline}/jobs/{job_id}/log",
    "/ops/chat/gaps",
    "/ops/chat/feedback",
))
_SUB = re.compile(r"[\w.@-]{1,64}")
_COMMUNITY = re.compile(r"[\w.-]{1,64}")


def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _key(settings):
    key = (getattr(settings, "read_token_key", "") or "").encode()
    return key if len(key) >= MIN_KEY else None


def _sign(key, head):
    return _b64(hmac.new(key, head.encode(), hashlib.sha256).digest())


def mint(conn, settings, sub, communities, days, issued_by):
    """A new token for *sub*, reading *communities* (["*"] for all) for *days*."""
    key = _key(settings)
    if key is None:
        raise ValueError("read tokens are off (BITS_READ_TOKEN_KEY, %d characters at least)" % MIN_KEY)
    if not (isinstance(sub, str) and _SUB.fullmatch(sub)):
        raise ValueError("sub is a name of letters, digits and ._@-")
    if not (isinstance(communities, list) and communities):
        raise ValueError("communities is a list of community names, or [\"*\"]")
    if communities != ["*"]:
        if not all(isinstance(c, str) and _COMMUNITY.fullmatch(c) for c in communities):
            raise ValueError("communities is a list of community names, or [\"*\"]")
        communities = sorted({c.lower() for c in communities})
    if not (isinstance(days, int) and not isinstance(days, bool) and 1 <= days <= MAX_DAYS):
        raise ValueError("days is from 1 to %d" % MAX_DAYS)
    now = db.now_ms()
    payload = {"sub": sub, "communities": communities, "exp": now + days * 86400 * 1000,
               "jti": secrets.token_urlsafe(12)}
    with db.transaction(conn):
        conn.execute("INSERT INTO read_tokens (jti, sub, communities, exp_ms, issued_by,"
                     " created_ms) VALUES (?, ?, ?, ?, ?, ?)",
                     (payload["jti"], sub, json.dumps(communities), payload["exp"],
                      issued_by, now))
    head = PREFIX + _b64(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return head + "." + _sign(key, head), payload


def verify(conn, settings, token):
    """{sub, communities (None: all), jti} of a good token, else None."""
    key = _key(settings)
    if key is None or not isinstance(token, str) or not token.startswith(PREFIX):
        return None
    head, _, sig = token.rpartition(".")
    try:
        if not head.startswith(PREFIX) or not hmac.compare_digest(sig.encode("ascii"),
                                                                   _sign(key, head).encode()):
            return None
    except UnicodeEncodeError:
        return None
    try:
        p = json.loads(_unb64(head[len(PREFIX):]))
        jti, exp, communities = str(p["jti"]), int(p["exp"]), p["communities"]
    except (ValueError, KeyError, TypeError):
        return None
    now = db.now_ms()
    row = conn.execute("SELECT * FROM read_tokens WHERE jti = ?", (jti,)).fetchone()
    if (row is None or row["revoked_ms"] or now >= exp or now >= row["exp_ms"]
            or json.loads(row["communities"]) != communities or row["sub"] != p.get("sub")):
        return None
    if now - row["last_used_ms"] >= USED_EVERY_MS:
        try:
            with db.transaction(conn):
                conn.execute("UPDATE read_tokens SET last_used_ms = ? WHERE jti = ?", (now, jti))
        except sqlite3.OperationalError:     # busy: the read goes on, the time next use
            pass
    return {"sub": row["sub"], "jti": jti,
            "communities": None if communities == ["*"] else frozenset(communities)}


def revoke(conn, jti, by):
    with db.transaction(conn):
        return conn.execute("UPDATE read_tokens SET revoked_ms = ?, revoked_by = ? WHERE jti = ?"
                            " AND revoked_ms = 0", (db.now_ms(), by, jti)).rowcount
