# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-memory server-side session store + a short-lived login-state store.

Sessions are keyed by an opaque high-entropy id carried in an httponly cookie;
the id IS the secret (256-bit, unguessable), so no cookie signing is needed. Both
stores are bounded (expiry sweep + oldest-eviction past a cap) so unauthenticated
traffic cannot grow them without limit. Single-process only — a multi-instance
deployment needs a shared store.
"""

import secrets
import time


class _BoundedStore:
    def __init__(self, ttl_seconds, max_entries):
        self._store = {}          # dict preserves insertion order (oldest first)
        self._ttl = ttl_seconds
        self._max = max_entries

    def _sweep(self):
        now = time.time()
        for k in [k for k, exp in self._exps() if exp < now]:
            self._store.pop(k, None)

    def _exps(self):
        raise NotImplementedError

    def _make_room(self):
        if len(self._store) >= self._max:
            self._sweep()
        while len(self._store) >= self._max:      # still full: drop the oldest
            self._store.pop(next(iter(self._store)), None)


class SessionStore(_BoundedStore):
    def __init__(self, ttl_seconds=28800, max_entries=50000):
        super().__init__(ttl_seconds, max_entries)

    def _exps(self):
        return [(k, e["exp"]) for k, e in self._store.items()]

    def create(self, data) -> str:
        self._make_room()
        sid = secrets.token_urlsafe(32)
        self._store[sid] = {"data": data, "exp": time.time() + self._ttl}
        return sid

    def get(self, sid):
        if not sid:
            return None
        entry = self._store.get(sid)
        if not entry:
            return None
        if entry["exp"] < time.time():
            self._store.pop(sid, None)
            return None
        return entry["data"]

    def delete(self, sid):
        if sid:
            self._store.pop(sid, None)


class LoginStateStore(_BoundedStore):
    """Holds the PKCE verifier per ``state`` between /login and the callback.
    Entries are single-use and expire quickly (CSRF + replay protection)."""

    def __init__(self, ttl_seconds=600, max_entries=20000):
        super().__init__(ttl_seconds, max_entries)

    def _exps(self):
        return [(k, exp) for k, (_v, exp) in self._store.items()]

    def put(self, state: str, verifier: str):
        self._make_room()
        self._store[state] = (verifier, time.time() + self._ttl)

    def take(self, state: str):
        """Pop and return the verifier for *state*, or None if absent/expired."""
        entry = self._store.pop(state, None)
        if not entry:
            return None
        verifier, exp = entry
        return verifier if exp >= time.time() else None


class SignRequestStore(_BoundedStore):
    """Pending sign requests between /sign/request and /sign/approve. Holds the
    manifest bytes + digest so approval signs exactly what was authorized."""

    def __init__(self, ttl_seconds=300, max_entries=10000):
        super().__init__(ttl_seconds, max_entries)

    def _exps(self):
        return [(k, e["exp"]) for k, e in self._store.items()]

    def put(self, req_id, data):
        self._make_room()
        data = dict(data)
        data["exp"] = time.time() + self._ttl
        self._store[req_id] = data

    def pop(self, req_id):
        """Atomically claim (remove + return) a pending request. Single-process +
        no await inside, so this is atomic against the event loop — two concurrent
        approvals cannot both claim the same request."""
        entry = self._store.pop(req_id, None)
        if not entry:
            return None
        return entry if entry["exp"] >= time.time() else None
