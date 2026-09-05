# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase C: cached GitHub catalog proxy (console_backend/catalog.py + /gh route)."""

import unittest
from unittest.mock import patch

import requests
from fastapi.testclient import TestClient

from console_backend import catalog
from console_backend.main import app


class FakeResp:
    """Mimics a streamed requests.Response (iter_content + close)."""
    def __init__(self, status, body=b"", etag=None, ctype="application/json", chunks=None):
        self.status_code = status
        self.headers = {"Content-Type": ctype}
        if etag:
            self.headers["ETag"] = etag
        self._chunks = chunks if chunks is not None else ([body] if body else [])

    def iter_content(self, n):
        for c in self._chunks:
            yield c

    def close(self):
        pass


class TestAllowlist(unittest.TestCase):
    def test_only_known_read_shapes(self):
        c = catalog.CatalogCache()
        self.assertTrue(c.allowed("/repos/o/r"))                      # meta
        self.assertTrue(c.allowed("/repos/o/r/git/trees/main"))       # tree
        self.assertTrue(c.allowed("/repos/o/r/git/blobs/abc123"))     # blob
        self.assertTrue(c.allowed("/repos/o/r/tags"))
        self.assertTrue(c.allowed("/repos/o/r/branches"))
        self.assertFalse(c.allowed("/repos/o/r/issues"))              # not a console read
        self.assertFalse(c.allowed("/repos/o/r/pulls"))
        self.assertFalse(c.allowed("/repos/o/r/actions/secrets"))
        self.assertFalse(c.allowed("/user"))
        self.assertFalse(c.allowed("/repos/o/r/git/trees/..%2f"))     # literal .. rejected
        self.assertFalse(c.allowed("/repos/o/../secret/tags"))

    def test_optional_owner_narrowing(self):
        c = catalog.CatalogCache(owners=["bitsorg", "alisw"])
        self.assertTrue(c.allowed("/repos/ALISW/alidist/tags"))       # case-insensitive
        self.assertFalse(c.allowed("/repos/evil/x/tags"))             # owner not allowed


class TestCache(unittest.TestCase):
    @patch("console_backend.catalog.requests.get")
    def test_serves_from_cache_within_ttl(self, gget):
        gget.return_value = FakeResp(200, b'{"a":1}', etag='"e1"')
        c = catalog.CatalogCache(ttl=300)
        self.assertEqual(c.fetch("/repos/o/r/tags")[:2], (200, b'{"a":1}'))
        c.fetch("/repos/o/r/tags")
        self.assertEqual(gget.call_count, 1)          # second served from cache

    @patch("console_backend.catalog.requests.get")
    def test_304_revalidation_serves_stale_and_sends_etag(self, gget):
        gget.side_effect = [FakeResp(200, b'BODY', etag='"e1"'), FakeResp(304)]
        c = catalog.CatalogCache(ttl=0)               # always revalidate
        c.fetch("/repos/o/r/tags")
        self.assertEqual(c.fetch("/repos/o/r/tags")[:2], (200, b'BODY'))
        self.assertEqual(gget.call_count, 2)
        self.assertEqual(gget.call_args.kwargs["headers"].get("If-None-Match"), '"e1"')

    @patch("console_backend.catalog.requests.get")
    def test_no_redirects_and_stream(self, gget):
        gget.return_value = FakeResp(200, b"{}")
        catalog.CatalogCache(ttl=300).fetch("/repos/o/r")
        self.assertIs(gget.call_args.kwargs.get("allow_redirects"), False)
        self.assertIs(gget.call_args.kwargs.get("stream"), True)

    @patch("console_backend.catalog.requests.get")
    def test_oversized_body_not_cached_returns_502(self, gget):
        big = [b"x" * 1024] * 40                       # 40 KiB in 1 KiB chunks
        gget.return_value = FakeResp(200, chunks=big)
        c = catalog.CatalogCache(ttl=300, max_bytes=8 * 1024)   # 8 KiB cap
        status, body, _ = c.fetch("/repos/o/r/git/trees/main")
        self.assertEqual(status, 502)
        self.assertNotIn("/repos/o/r/git/trees/main", c._store)  # not cached

    @patch("console_backend.catalog.requests.get")
    def test_serves_stale_on_upstream_error(self, gget):
        gget.side_effect = [FakeResp(200, b'BODY'), requests.RequestException("boom")]
        c = catalog.CatalogCache(ttl=0)
        c.fetch("/repos/o/r/tags")
        self.assertEqual(c.fetch("/repos/o/r/tags")[:2], (200, b'BODY'))

    @patch("console_backend.catalog.requests.get")
    def test_serves_stale_on_rate_limit(self, gget):
        gget.side_effect = [FakeResp(200, b'BODY'), FakeResp(403, b'{"message":"rate limit"}')]
        c = catalog.CatalogCache(ttl=0)
        c.fetch("/repos/o/r/tags")
        self.assertEqual(c.fetch("/repos/o/r/tags")[:2], (200, b'BODY'))

    @patch("console_backend.catalog.requests.get")
    def test_token_sent_when_configured(self, gget):
        gget.return_value = FakeResp(200, b"{}")
        catalog.CatalogCache(token="ght", ttl=300).fetch("/repos/o/r")
        self.assertEqual(gget.call_args.kwargs["headers"].get("Authorization"), "Bearer ght")


class TestGhEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_rejects_non_read_shape(self):
        self.assertEqual(self.client.get("/gh/user").status_code, 403)
        self.assertEqual(self.client.get("/gh/repos/o/r/issues").status_code, 403)

    @patch("console_backend.catalog.requests.get")
    def test_proxies_repos_with_query(self, gget):
        gget.return_value = FakeResp(200, b'[{"name":"v1"}]')
        r = self.client.get("/gh/repos/bitsorg/bits/tags?per_page=99")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b'[{"name":"v1"}]')
        self.assertIn("/repos/bitsorg/bits/tags?per_page=99", gget.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
