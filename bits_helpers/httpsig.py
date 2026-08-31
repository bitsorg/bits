# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits_helpers.httpsig — request signing for the cvmfs-prepub API.

Implements ADR-0008 D3 option T1, the client half of ``internal/httpsig`` in
cvmfs-bits.

Why sign instead of sending the token
-------------------------------------
A bearer token has to TRAVEL to be used, so anyone who observes one request —
a packet capture, a proxy log, a mirrored port — holds publish rights to a
production repository until the token is rotated, and the next request looks
identical to a genuine one. Signing keeps the shared secret on both ends and
puts only a per-request MAC on the wire: bound to one method, one URI, one
field set and one payload, single-use, and expiring within minutes.

It does NOT provide confidentiality or authenticate the server's responses.
Those need TLS; this composes with it rather than replacing it.

The wire format
---------------
::

    X-Bits-Auth: v1 key_id=<id> ts=<unix> nonce=<hex> fd=<hex> bh=<hex> mac=<hex>

    mac = HMAC-SHA256(secret, "\\n".join([
              "bits-hmac-v1", METHOD, request-uri, fd, bh, ts, nonce]))

``fd`` digests every non-payload form field; ``bh`` is the SHA-256 of the
payload, or ``"-"`` when there is none. The server checks the MAC before
reading the body, then confirms that the fields it parsed and the bytes it
stored match ``fd`` and ``bh`` — so a signature commits the client to exactly
the request the server acts on.
"""

import hashlib
import hmac
import secrets
import time
from typing import Mapping, Optional

HEADER_NAME = "X-Bits-Auth"
SCHEME = "v1"
CANONICAL_PREFIX = "bits-hmac-v1"
KEY_ID = "prepub"

#: Placeholder for ``bh`` when a request carries no payload.
NO_BODY = "-"


def fields_digest(fields: Mapping[str, str]) -> str:
    """Digest of the non-payload form fields.

    The encoding is length-prefixed — ``"<len(k)>:<k>=<len(v)>:<v>\\n"`` per
    field, sorted by key — so no combination of separators inside a key or
    value can make two different field sets produce the same digest.
    Canonicalisation ambiguity is the classic way request-signing schemes get
    broken, and these values legitimately contain ``=``, ``:``, spaces and
    occasionally newlines.

    Lengths are BYTE counts. Go's ``len()`` on a string counts bytes while
    Python's ``len()`` on a ``str`` counts code points, so a value like
    "héllo" would be prefixed 6 on one side and 5 on the other and every
    request would fail with an unexplainable 401. Everything below therefore
    works in bytes.
    """
    h = hashlib.sha256()
    for key in sorted(fields):
        key_bytes = key.encode("utf-8", "surrogateescape")
        value_bytes = fields[key].encode("utf-8", "surrogateescape")
        h.update(b"%d:%s=%d:%s\n" % (len(key_bytes), key_bytes,
                                     len(value_bytes), value_bytes))
    return h.hexdigest()


def canonical(method: str, uri: str, fd: str, bh: str, ts: int, nonce: str) -> str:
    """Build the string that is MAC'd.

    ``uri`` must be the full request URI INCLUDING any query string, never
    just the path: a handler that reads a query parameter would otherwise
    honour one appended to a captured request while the MAC still verified.
    """
    return "\n".join([CANONICAL_PREFIX, method.upper(), uri, fd, bh, str(ts), nonce])


def sign(
    secret: str,
    method: str,
    uri: str,
    fields: Optional[Mapping[str, str]] = None,
    body_hash: str = NO_BODY,
    key_id: str = KEY_ID,
) -> str:
    """Return the ``X-Bits-Auth`` header value for one request.

    A signature covers a single request, so it cannot be attached to a
    ``requests.Session`` the way a bearer token can — each call site signs its
    own request.
    """
    if not secret:
        raise ValueError("httpsig.sign: no secret supplied")
    fd = fields_digest(fields or {})
    ts = int(time.time())
    nonce = secrets.token_hex(16)
    mac = hmac.new(
        secret.encode("utf-8", "surrogateescape"),
        canonical(method, uri, fd, body_hash or NO_BODY, ts, nonce).encode(
            "utf-8", "surrogateescape"),
        hashlib.sha256,
    ).hexdigest()
    return (f"{SCHEME} key_id={key_id} ts={ts} nonce={nonce} "
            f"fd={fd} bh={body_hash or NO_BODY} mac={mac}")


