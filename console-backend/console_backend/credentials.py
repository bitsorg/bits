# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Persistent WebAuthn credential store (per GitLab user).

Credentials MUST survive restarts (unlike sessions), so this is file-backed
JSON. Single-process, low write rate (enrolments are rare); a lock serialises
writes. Multi-instance would need a shared DB. Stores only public material:
credential id (base64url), COSE public key (base64url), and the sign counter.
"""

import json
import os
import threading


class CredentialStore:
    def __init__(self, path=None):
        self._path = path
        self._lock = threading.Lock()
        self._data = {}
        self._load()

    def _load(self):
        if self._path and os.path.isfile(self._path):
            try:
                with open(self._path) as fh:
                    self._data = json.load(fh)
            except Exception:
                self._data = {}

    def _save(self):
        if not self._path:
            return
        tmp = self._path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self._data, fh)
        os.replace(tmp, self._path)

    def add(self, user, cred):
        with self._lock:
            creds = self._data.setdefault(user, [])
            if any(c["id"] == cred["id"] for c in creds):
                return          # idempotent: same credential id
            creds.append(cred)
            self._save()

    def get(self, user):
        with self._lock:
            return [dict(c) for c in self._data.get(user, [])]

    def update_sign_count(self, user, cred_id, count):
        with self._lock:
            for c in self._data.get(user, []):
                if c["id"] == cred_id:
                    c["sign_count"] = count
                    self._save()
                    return
