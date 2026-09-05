# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Cached, authenticated read-through proxy for the GitHub REST reads the console
does when browsing packages (repo trees, tags, branches, blob metadata, repo meta).

The browser hits these UNAUTHENTICATED — 60 req/hr per user IP — and runs out,
which is what makes package browsing fail on busy days. Here one shared read-only
token (5000/hr) plus a short TTL cache with ETag revalidation serves every user
from a single upstream fetch, so no one burns their own limit. A 304 from GitHub
does not count against the rate limit, so keeping the cache warm is ~free.

PUBLIC DATA ONLY. The token MUST be a public-read token (a no-scope classic token
is enough): this proxy is unauthenticated to callers, so a token that could read
private repos would expose them. The route is locked to GET of the handful of
read SHAPES the console uses under /repos/<owner>/<repo>/... — never an open proxy,
never a write, never a redirect off GitHub. Owners are arbitrary by design (recipe
providers resolve to any GitHub owner), so the optional owner allowlist only
narrows further; it is not the primary gate — the path-shape allowlist is.
"""

import logging
import re
import threading
import time

import requests

_GITHUB = "https://api.github.com"
_log = logging.getLogger("console_backend.catalog")

# The exact read shapes the console fetches (query already stripped before match):
#   /repos/{o}/{r}                 repo meta
#   /repos/{o}/{r}/git/trees/...   recursive tree
#   /repos/{o}/{r}/git/blobs/...   recipe file blob
#   /repos/{o}/{r}/tags            tags
#   /repos/{o}/{r}/branches        branches
_SHAPES = re.compile(r"^/repos/[^/]+/[^/]+(?:|/git/trees/.+|/git/blobs/.+|/tags|/branches)$")


class CatalogCache:
    def __init__(self, token="", owners=(), ttl=300, timeout=10,
                 max_entries=2048, max_bytes=32 * 1024 * 1024):
        self._token = token or ""
        self._owners = {o.lower() for o in owners}
        self._ttl = int(ttl)
        self._timeout = timeout
        self._max = max_entries
        self._max_bytes = max_bytes
        self._lock = threading.Lock()
        self._store = {}   # full-path(+query) -> {etag, body(bytes), ctype, ts}
        if not self._token:
            _log.warning("catalog proxy has no GitHub token: upstream reads are "
                         "unauthenticated (shared 60/hr). Set BITS_GITHUB_TOKEN "
                         "(public-read) to raise the limit to 5000/hr.")

    def allowed(self, path):
        """`path` is the path portion (query stripped). Locked to the console's read
        shapes; rejects traversal; then, if an owner allowlist is set, the owner too."""
        if ".." in path or not _SHAPES.match(path):
            return False
        owner = path.split("/")[2].lower()
        return (not self._owners) or (owner in self._owners)

    def _headers(self, etag=None):
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            h["Authorization"] = "Bearer " + self._token
        if etag:
            h["If-None-Match"] = etag
        return h

    def _read_capped(self, r):
        """Stream the body but stop past the cap so a huge/abusive response can't
        blow up memory. Returns bytes, or None if it exceeds max_bytes."""
        total, chunks = 0, []
        for ch in r.iter_content(65536):
            total += len(ch)
            if total > self._max_bytes:
                return None
            chunks.append(ch)
        return b"".join(chunks)

    def fetch(self, full):
        """full = path + optional '?query'. Returns (status, body_bytes, content_type).
        Serves fresh-from-cache within TTL (refreshing recency); else conditionally
        revalidates; serves stale on any upstream error or rate-limit so browsing
        never hard-fails. Never follows redirects; caps the body size."""
        now = time.time()
        with self._lock:
            ent = self._store.get(full)
            if ent and now - ent["ts"] < self._ttl:
                ent["ts"] = now                       # LRU: recency on hit
                return 200, ent["body"], ent["ctype"]
        try:
            r = requests.get(_GITHUB + full, headers=self._headers(ent.get("etag") if ent else None),
                             timeout=self._timeout, allow_redirects=False, stream=True)
        except requests.RequestException:
            if ent:
                return 200, ent["body"], ent["ctype"]   # stale on network error
            raise
        try:
            code = r.status_code
            if code == 304 and ent:
                with self._lock:
                    ent["ts"] = now                      # revalidated: free refresh
                return 200, ent["body"], ent["ctype"]
            body = self._read_capped(r)
            if body is None:                             # oversized upstream body
                if ent:
                    return 200, ent["body"], ent["ctype"]
                return 502, b'{"message":"catalog: upstream response too large"}', "application/json"
            ctype = r.headers.get("Content-Type", "application/json")
            if code == 200:
                entry = {"etag": r.headers.get("ETag"), "body": body, "ctype": ctype, "ts": now}
                with self._lock:
                    if full not in self._store and len(self._store) >= self._max:
                        self._store.pop(min(self._store, key=lambda k: self._store[k]["ts"]), None)
                    self._store[full] = entry
                return 200, body, ctype
            if ent:
                return 200, ent["body"], ent["ctype"]    # stale on 403/5xx/rate-limit
            return code, body, ctype
        finally:
            r.close()
