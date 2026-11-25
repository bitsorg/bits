# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""At startup bits removes the download sentinels a killed run left, not a live run's."""

import os
import shutil
import tempfile
import time
import unittest

from bits_helpers.build import SENTINEL_MAX_AGE, remove_stale_sentinels

OTHER = str(os.getppid())   # a live process that is not this one: another bits run


class RemoveStaleSentinelsTest(unittest.TestCase):
  def test_only_those_a_killed_run_left(self):
    work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, work)
    made = {}
    for name, rel, content, age in (
        ("live", "TARS/el9/store/ab/ab12.downloading", OTHER, 0),
        ("live source", "SOURCES/cache/5e/5e77/zlib.tar.gz.downloading", OTHER, 0),
        ("dead", "TARS/el9/store/cd/cd34.downloading", "2147483646", 0),
        ("garbage", "SOURCES/cache/5e/5e78/a.tar.gz.downloading", "not a pid", 0),
        ("ours", "TARS/el9/store/ef/ef56.downloading", str(os.getpid()), 0),  # PID reused
        ("old", "SOURCES/cache/5e/5e79/b.tar.gz.downloading", OTHER, SENTINEL_MAX_AGE + 60)):
      made[name] = os.path.join(work, rel)
      os.makedirs(os.path.dirname(made[name]), exist_ok=True)
      with open(made[name], "w") as f:
        f.write(content)
      then = time.time() - age
      os.utime(made[name], (then, then))
    remove_stale_sentinels(work)
    self.assertEqual({n for n, p in made.items() if os.path.exists(p)}, {"live", "live source"})


if __name__ == "__main__":
  unittest.main()
