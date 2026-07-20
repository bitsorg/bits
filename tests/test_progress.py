# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for build-progress reporting (bits_helpers/progress.py).

GitLab cannot UPDATE a running commit status (the API fires ``run!``, invalid
from ``running``, and rolls the update back with HTTP 400), so after the first
post every tick must CANCEL the current status and re-post ``running`` with the
new coverage. These tests pin that cancel+repost sequence and the tick
throttling; without them the console progress bar shows only 0 and 100%.
"""

import unittest
from unittest.mock import patch

from bits_helpers import progress


class TickTestCase(unittest.TestCase):

    def setUp(self):
        # Force the probe "ready" without a CI environment; reset counters.
        progress._state.update({
            "ready": True, "url": "http://gl/statuses/sha",
            "headers": {}, "context": "bits-build-progress/1",
            "pipeline_id": 1, "ref": "main",
        })
        progress.set_total(4)
        self.posts = []       # (state, coverage, description)
        self._p = patch.object(progress, "_post",
                               side_effect=lambda s, c, d: self.posts.append((s, c, d)))
        self._p.start()
        self.addCleanup(self._p.stop)
        self._now = 1000.0
        self._t = patch.object(progress.time, "monotonic", side_effect=lambda: self._now)
        self._t.start()
        self.addCleanup(self._t.stop)

    def test_first_tick_posts_running_only(self):
        progress.tick("A")
        self.assertEqual(self.posts, [("running", 25, "1/4 · A")])

    def test_update_cancels_then_reposts_running(self):
        # A running status cannot be re-posted; the update must free the slot
        # (cancel) and re-create it with the new coverage.
        progress.tick("A")
        self._now += 5
        progress.tick("B")
        self.assertEqual(self.posts, [
            ("running", 25, "1/4 · A"),
            ("canceled", 50, "2/4 · B"),
            ("running", 50, "2/4 · B"),
        ])

    def test_burst_ticks_are_throttled(self):
        # Reused packages tick in quick bursts; at most one post pair per 2 s.
        progress.tick("A")
        self._now += 0.5
        progress.tick("B")                      # skipped (< 2 s)
        self.assertEqual(len(self.posts), 1)

    def test_final_tick_is_never_skipped(self):
        progress.tick("A")
        self._now += 0.1
        progress.tick("B")                      # throttled
        progress.tick("C")                      # throttled
        progress.tick("D")                      # final: must post despite burst
        states = [p[0] for p in self.posts]
        self.assertEqual(states, ["running", "canceled", "running"])
        self.assertEqual(self.posts[-1], ("running", 100, "4/4 · D"))

    def test_unchanged_percent_not_reposted(self):
        # 1000 packages: consecutive ticks often round to the same percent —
        # do not spend two API calls to display the same number.
        progress.set_total(1000)
        progress.tick("A")                      # 1/1000 -> 0%
        self._now += 5
        progress.tick("B")                      # 2/1000 -> 0%, unchanged
        self.assertEqual(len(self.posts), 1)
        self._now += 31                          # ...but refresh after 30 s
        progress.tick("C")
        self.assertEqual([p[0] for p in self.posts],
                         ["running", "canceled", "running"])


if __name__ == "__main__":
    unittest.main()
