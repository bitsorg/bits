# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""In-memory, bounded stores for the backend's short-lived server-side state:
pending sign requests, cross-device CLI requests, enrolment grants, and in-flight
passkey-enrolment challenges. Each is bounded (expiry sweep + oldest-eviction past
a cap) so unauthenticated traffic cannot grow it without limit. Single-process
only — a multi-instance deployment needs a shared store.
"""

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


class CliSignStore(_BoundedStore):
    """Cross-device (CLI-initiated) sign requests: a terminal creates one, a human
    approves it in the browser, the CLI polls the result. Entries are mutable
    (status/envelope updated in place)."""

    def __init__(self, ttl_seconds=600, max_entries=64):
        super().__init__(ttl_seconds, max_entries)

    def _exps(self):
        return [(k, e["exp"]) for k, e in self._store.items()]

    def put(self, req_id, data):
        self._make_room()
        data = dict(data)
        data["exp"] = time.time() + self._ttl
        self._store[req_id] = data

    def get(self, req_id):
        entry = self._store.get(req_id)
        if not entry:
            return None
        if entry["exp"] < time.time():
            self._store.pop(req_id, None)
            return None
        return entry


class PreapprovalStore(_BoundedStore):
    """Build/publish pre-approvals: a logged-in admin passkey-approves a BUILD (by
    build_id) before its manifest exists; CI signs that build's manifest later,
    gated by this record. Keyed by build_id, mutable (status pending -> approved ->
    consumed). Long TTL so a slow build+publish can still reach the certify step."""

    def __init__(self, ttl_seconds=86400, max_entries=256):
        super().__init__(ttl_seconds, max_entries)

    def _exps(self):
        return [(k, e["exp"]) for k, e in self._store.items()]

    def put(self, build_id, data):
        self._make_room()
        data = dict(data)
        data["exp"] = time.time() + self._ttl
        self._store[build_id] = data

    def get(self, build_id):
        entry = self._store.get(build_id)
        if not entry:
            return None
        if entry["exp"] < time.time():
            self._store.pop(build_id, None)
            return None
        return entry

    def pop(self, build_id):
        """Remove and return the record (single-use consume by the CI sign step)."""
        return self._store.pop(build_id, None)

    def _make_room(self):
        # Evict oldest PENDING (or expired) first; an approved-but-not-yet-consumed
        # record is still needed by the CI sign step, so keep it unless the store is
        # entirely approved records.
        if len(self._store) >= self._max:
            self._sweep()
        while len(self._store) >= self._max:
            victim = next((k for k, e in self._store.items()
                           if e.get("status") != "approved"), None)
            self._store.pop(victim if victim is not None else next(iter(self._store)), None)


class EnrollmentGrantStore(_BoundedStore):
    """One-time, short-lived grants that let a user enrol their FIRST passkey.
    Issued by a bits-admin; consumed on enrolment. Keyed by target username."""

    def __init__(self, ttl_seconds=600, max_entries=10000):
        super().__init__(ttl_seconds, max_entries)

    def _exps(self):
        return list(self._store.items())

    def put(self, user):
        self._make_room()
        self._store[user] = time.time() + self._ttl

    def take(self, user) -> bool:
        exp = self._store.pop(user, None)
        return exp is not None and exp >= time.time()


class RegChallengeStore(_BoundedStore):
    """In-flight passkey-enrolment challenges between /webauthn/register/begin and
    /finish, keyed by username. Auth is stateless (no server session), so the
    ceremony's challenge is held here instead of on a session. Short-lived and
    single-use (popped at finish)."""

    def __init__(self, ttl_seconds=300, max_entries=10000):
        super().__init__(ttl_seconds, max_entries)

    def _exps(self):
        return [(k, e["exp"]) for k, e in self._store.items()]

    def put(self, user, data):
        self._make_room()
        data = dict(data)
        data["exp"] = time.time() + self._ttl
        self._store[user] = data

    def pop(self, user):
        entry = self._store.pop(user, None)
        if not entry:
            return None
        return entry if entry["exp"] >= time.time() else None
