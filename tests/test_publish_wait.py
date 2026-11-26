# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""`bits cvmfs publish --wait`: the jobs are waited for within one --wait-timeout."""

import unittest
from unittest import mock

import bits_helpers.cvmfs_publish as cp


class WaitTest(unittest.TestCase):
  def test_one_deadline_for_all(self):
    clock = [0.0]
    asked = []

    def poll(url, token, jid, timeout, **kw):
      asked.append((jid, timeout))
      clock[0] += 400 if jid != "j1" else 10        # j2 and j3 run long
      return "published"
    lines = []
    jobs = [("j1", "a"), ("j2", "b"), ("j3", "c"), ("j4", "d")]
    with mock.patch("bits_helpers.prepub.poll_job", poll), \
         mock.patch.object(cp.time, "monotonic", lambda: clock[0]):
      rc = cp._wait_for({"prepub_url": "u", "token": "t", "wait_timeout": 600}, jobs,
                        lines.extend)
    self.assertEqual(asked, [("j1", 600), ("j2", 590), ("j3", 190), ("j4", 1)])   # what is left
    self.assertEqual((rc, lines), (0, []))      # j4 was published meanwhile: asked once
    self.assertEqual(jobs, [])


  def test_past_the_deadline_one_not_done_fails(self):
    def poll(url, token, jid, timeout, **kw):
      raise SystemExit("Timeout waiting for job %s" % jid)
    lines = []
    with mock.patch("bits_helpers.prepub.poll_job", poll):
      rc = cp._wait_for({"prepub_url": "u", "token": "t", "wait_timeout": 0}, [("j", "a")],
                        lines.extend)
    self.assertEqual(rc, 1)
    self.assertIn("FAILED a: job j", lines[0])


if __name__ == "__main__":
  unittest.main()
