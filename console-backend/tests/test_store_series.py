# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""GET /store-admin/series: bits-store's history from VictoriaMetrics, fixed queries
per window, for overall admins only."""

import unittest
from unittest import mock

import requests
from fastapi.testclient import TestClient

from console_backend import audit, main, store_series
from test_store_admin import Answer, _configure

NOW = 1_795_000_000


class StoreSeriesTest(unittest.TestCase):
    def setUp(self):
        _configure(BITS_METRICS_URL="http://victoriametrics:8428")
        self.client = TestClient(main.app)
        self.asked, self.down = [], None

        def fake_get(session, url, params=None, timeout=None, allow_redirects=True):
            self.asked.append((url, dict(params), session.trust_env, allow_redirects))
            if self.down is not None:
                if isinstance(self.down, Exception):
                    raise self.down
                return self.down
            return Answer(200, {"status": "success", "data": {"result": self.result(url, params)}})
        p = mock.patch.object(requests.Session, "get", fake_get)
        p.start()
        self.addCleanup(p.stop)
        self.audited = []
        a = mock.patch.object(audit, "record", lambda kind, **kw: self.audited.append((kind, kw)))
        a.start()
        self.addCleanup(a.stop)

    def result(self, url, params):
        q = params["query"]
        if url.endswith("/query"):
            return [{"metric": {}, "value": [params["time"], "0.038" if "quantile" in q else "0.994"]}]
        t0, step = params["start"], params["step"]
        if "content_bytes" in q:     # tarballs at each point but the second; a point not on the grid
            return [{"metric": {"format": "tarballs"}, "values": [[t0, "10"], [t0 + 2 * step, "30"], [t0 + 7, "99"]]},
                    {"metric": {"format": "sources"}, "values": [[t0 + step, "NaN"]]}]
        if "fills_total" in q:
            return [{"metric": {"format": "sources", "result": "failed"}, "values": [[t0 + step, "2"]]}]
        return []

    def get(self, user, path):
        return self.client.get(path, headers={"Authorization": "Bearer %s" % user} if user else {})

    def test_the_history(self):
        with mock.patch.object(store_series.time, "time", lambda: NOW):
            r = self.get("carol", "/store-admin/series?window=1d")
        self.assertEqual((r.status_code, r.headers.get("cache-control")), (200, "no-store"))
        d = r.json()
        end = NOW // 60 * 60
        self.assertEqual((d["window"], d["step"], d["points"][0], d["points"][-1], len(d["points"])),
                         ("1d", 3600, end - 86400, end, 25))
        self.assertEqual(d["stored_bytes"]["tarballs"][:4], [10, None, 30, None])
        self.assertEqual(d["stored_bytes"]["sources"][:2], [None, None])        # NaN: no value
        self.assertEqual(d["fills"]["sources/failed"][:2], [None, 2])
        self.assertEqual((d["requests"], d["git_disk_bytes"], d["p95_seconds"], d["answered"]), ({}, {}, 0.038, 0.994))
        urls = {u.rsplit("/", 1)[1] for u, *_ in self.asked}
        self.assertEqual(urls, {"query_range", "query"})
        self.assertTrue(all(u.startswith("http://victoriametrics:8428/api/v1/") and not env and not redirects
                            for u, _, env, redirects in self.asked))
        ranges = [p for u, p, *_ in self.asked if u.endswith("query_range")]
        self.assertIn("increase(bits_store_requests_total[3600s])", ranges[3]["query"])
        self.assertTrue(all(p["start"] == end - 86400 and p["end"] == end and p["step"] == 3600 for p in ranges))
        whole = [p for u, p, *_ in self.asked if u.endswith("/query")]
        self.assertTrue(all("[86400s]" in p["query"] and p["time"] == end for p in whole))
        self.assertIn("max by (volume, key_prefix, binding, format)", ranges[0]["query"])

    def test_every_window_ends_now(self):
        for w, (length, step) in store_series.WINDOWS.items():
            d = store_series.series("http://vm", w, now=NOW)
            self.assertEqual((d["points"][-1], d["points"][0], length % step), (NOW // 60 * 60, NOW // 60 * 60 - length, 0), w)

    def test_refused(self):
        for user, path, code in ((None, "/store-admin/series", 401), ("alice", "/store-admin/series", 403),
                                 ("carol", "/store-admin/series?window=2d", 400),
                                 ("carol", "/store-admin/series?window=1d%29", 400)):
            self.assertEqual(self.get(user, path).status_code, code, (user, path))
        self.assertEqual(self.asked, [])                         # VictoriaMetrics never asked
        self.assertIn(("store_admin_denied", {"user": "alice", "view": "series", "principal": "human"}), self.audited)
        _configure(BITS_METRICS_URL=None)
        self.assertEqual(self.get("carol", "/store-admin/series").status_code, 503)
        with self.assertRaises(ValueError):
            _configure(BITS_METRICS_URL="http://vm/api?x=1")

    def test_victoriametrics_down(self):
        for down in (requests.ConnectionError("down"), Answer(500, {"status": "error"}), Answer(200, ValueError()),
                     Answer(200, {"status": "success", "data": {}}), Answer(200, [1]),
                     Answer(200, {"status": "success", "data": {"result": [{"values": [["x"]]}]}})):
            self.down = down
            self.assertEqual(self.get("carol", "/store-admin/series").status_code, 502, down)


if __name__ == "__main__":
    unittest.main()
