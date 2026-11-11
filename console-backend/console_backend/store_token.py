# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Store tokens (bits-store docs/TOKEN.md): signed capabilities bits-store checks,
so members and builds read and write the store without storage keys. A token this
signer makes is one bits-store accepts; anything it would refuse is refused here.
"""

import base64
import re
import secrets
import time

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

TYP = "bits-store+jwt"
MAX_LEN = 4000
MAX_LIFETIME = 7 * 86400  # the verifier's default maximum
MAX_TIME = 2**53 - 1
METHODS = ("GET", "PUT")
_SEGMENT = re.compile(r"[A-Za-z0-9._~+!@=,:*-]{1,255}")
_SEED = re.compile(r"[A-Za-z0-9_-]{43}")


def check_pattern(p):
    """Raise ValueError unless *p* is a capability pattern (TOKEN.md, paths and
    patterns): '/' and segments, '*' within a segment (at most 4), '**' only as
    the whole last segment."""
    segs = p.split("/")[1:] if isinstance(p, str) and p.startswith("/") else []
    if not segs:
        raise ValueError("pattern %r: not /<segment>[/...]" % (p,))
    for i, s in enumerate(segs):
        last_any = s == "**" and i == len(segs) - 1
        if (not _SEGMENT.fullmatch(s) or s in (".", "..") or s.count("*") > 4
                or ("**" in s and not last_any)):
            raise ValueError("pattern %r: bad segment %r" % (p, s))


class StoreTokenSigner:
    """Signs store tokens with one Ed25519 key, known to bits-store by *kid* with
    *signer* 'user' (bits-console) or 'build' (the orchestrator)."""

    def __init__(self, kid, key, issuer, audience, signer="user", max_lifetime=MAX_LIFETIME):
        if not all(isinstance(v, str) and v for v in (kid, issuer, audience)):
            raise ValueError("kid, issuer and audience must be non-empty strings")
        if signer not in ("user", "build") or not isinstance(key, Ed25519PrivateKey):
            raise ValueError("signer is 'user' or 'build', key an Ed25519 private key")
        if not (isinstance(max_lifetime, int) and 60 <= max_lifetime <= MAX_LIFETIME):
            raise ValueError("max_lifetime: 60 to %d seconds, as the verifier's" % MAX_LIFETIME)
        self.kid, self.key, self.issuer, self.audience = kid, key, issuer, audience
        self.signer, self.max_lifetime = signer, max_lifetime

    @classmethod
    def from_seed(cls, kid, seed, issuer, audience, **kw):
        """A signer from the key's raw 32-byte seed, base64url without padding
        (surrounding whitespace, as from a file, is ignored)."""
        seed = seed.strip() if isinstance(seed, str) else ""
        if not _SEED.fullmatch(seed):
            raise ValueError("signing key: not a base64url 32-byte Ed25519 seed")
        raw = base64.urlsafe_b64decode(seed + "=")
        return cls(kid, Ed25519PrivateKey.from_private_bytes(raw), issuer, audience, **kw)

    def sign(self, sub, grp, caps, lifetime, now=None, jti=None):
        """A token for *sub* in group *grp*, allowing *caps* ((method, pattern)
        pairs) for *lifetime* seconds from *now*."""
        if not (isinstance(sub, str) and sub and isinstance(grp, str) and grp):
            raise ValueError("sub and grp must be non-empty strings")
        if sub.startswith("build:") != (self.signer == "build"):
            raise ValueError("sub is build:<job> exactly when the signer is 'build'")
        try:
            caps = [(m, p) for m, p in caps]  # once: caps may be a generator
        except (TypeError, ValueError):
            raise ValueError("caps: (method, pattern) pairs") from None
        if not caps:
            raise ValueError("no capabilities")
        for m, p in caps:
            if m not in METHODS:
                raise ValueError("method %r: GET or PUT" % (m,))
            check_pattern(p)
        if not (isinstance(lifetime, int) and 0 < lifetime <= self.max_lifetime):
            raise ValueError("lifetime: 1 to %d seconds" % self.max_lifetime)
        if jti is not None and not (isinstance(jti, str) and jti):
            raise ValueError("jti: a non-empty string")
        try:
            iat = int(time.time() if now is None else now)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("now: seconds since the epoch") from None
        if not 0 <= iat <= MAX_TIME - lifetime:
            raise ValueError("now: out of the token's time range")
        claims = {"iss": self.issuer, "aud": self.audience, "sub": sub, "grp": grp,
                  "iat": iat, "exp": iat + lifetime, "jti": jti or secrets.token_urlsafe(16),
                  "cap": [{"m": m, "p": p} for m, p in caps]}
        tok = jwt.encode(claims, self.key, algorithm="EdDSA",
                         headers={"kid": self.kid, "typ": TYP})
        if len(tok) > MAX_LEN:
            raise ValueError("token of %d bytes: at most %d" % (len(tok), MAX_LEN))
        return tok
