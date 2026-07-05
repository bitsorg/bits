# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the best-effort reuse beacon (bits_helpers/beacon.py).

The defining property: the HTTP call must NEVER block the caller and NEVER
raise, no matter what the network does.
"""

import time
import unittest
from unittest.mock import patch

from bits_helpers import beacon


class TestReuseBeacon(unittest.TestCase):

    def test_noop_when_nothing_to_send(self):
        self.assertIsNone(beacon.send_reuse_beacon("", "b1", ["h1"]))
        self.assertIsNone(beacon.send_reuse_beacon("http://c", None, ["h1"]))
        self.assertIsNone(beacon.send_reuse_beacon("http://c", "b1", []))

    def test_does_not_block_caller_even_if_endpoint_hangs(self):
        # urlopen blocks for a long time; the caller must return immediately
        # because the request runs in a background daemon thread.
        def _hang(*a, **k):
            time.sleep(5)
        started = time.time()
        with patch.object(beacon, "urlopen", _hang):
            t = beacon.send_reuse_beacon("http://console", "b1", ["h1", "h2"])
        self.assertLess(time.time() - started, 0.5)   # returned without waiting
        self.assertIsNotNone(t)
        self.assertTrue(t.daemon)                      # won't keep process alive

    def test_never_raises_when_request_errors(self):
        def _boom(*a, **k):
            raise OSError("connection refused")
        with patch.object(beacon, "urlopen", _boom):
            t = beacon.send_reuse_beacon("http://console", "b1", ["h1"])
            t.join(2)
        self.assertFalse(t.is_alive())                 # worker finished, swallowed error

    def test_sends_expected_url_with_build_and_hashes(self):
        seen = []

        class _Resp:
            def close(self):
                pass

        def _capture(url, timeout=None):
            seen.append(url)
            return _Resp()

        with patch.object(beacon, "urlopen", _capture):
            t = beacon.send_reuse_beacon("http://console/", "release-abc", ["h1", "h2"])
            t.join(2)
        self.assertEqual(len(seen), 1)
        self.assertIn("/api/reuse?", seen[0])
        self.assertIn("build=release-abc", seen[0])
        self.assertIn("h1", seen[0])
        self.assertIn("h2", seen[0])

    def test_batches_large_hash_lists(self):
        seen = []

        class _Resp:
            def close(self):
                pass

        def _capture(url, timeout=None):
            seen.append(url)
            return _Resp()

        with patch.object(beacon, "urlopen", _capture):
            t = beacon.send_reuse_beacon("http://c", "b1",
                                         ["h%d" % i for i in range(450)], batch=200)
            t.join(2)
        self.assertEqual(len(seen), 3)   # 200 + 200 + 50


if __name__ == "__main__":
    unittest.main()
