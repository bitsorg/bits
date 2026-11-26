# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""`bits store ls|rm --stale-rev-markers`: revision claims of builds that failed."""

import datetime
import os
import subprocess
import unittest

from bits_helpers.rev_index import marker_selected, stale_markers

NOW = datetime.datetime(2026, 11, 26, tzinfo=datetime.timezone.utc)
OLD, NEW = NOW - datetime.timedelta(days=30), NOW - datetime.timedelta(days=1)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class StaleMarkersTest(unittest.TestCase):
  def test_old_and_without_a_tarball(self):
    m = "MANIFESTS/rev-index/el9/zlib/1.3-%s"
    markers = [(m % 1, 40, OLD),             # its tarball is stored: kept
               (m % 2, 40, OLD),             # failed build: stale
               (m % 3, 40, NEW),             # still building, perhaps: kept
               (m % 4, 40, None),            # no time known: kept
               ("MANIFESTS/rev-index/el9/zlib/x/1.3-5", 40, OLD)]   # not a marker
    stored = {"zlib-1.3-1.el9.tar.gz", "zlib-1.3-2.el10.tar.gz"}    # another arch's
    self.assertEqual(stale_markers(markers, stored, 7, NOW), {m % 2: 40})
    self.assertEqual(stale_markers(markers, stored, 0.5, NOW), {m % 2: 40, m % 3: 40})

  def test_selected_by_package_and_version(self):
    k = "MANIFESTS/rev-index/el9/zlib/1.3-2"
    self.assertTrue(marker_selected(k))
    self.assertTrue(marker_selected(k, ["zlib"], ["1.3"]))
    self.assertFalse(marker_selected(k, ["xz"]))
    self.assertFalse(marker_selected(k, None, ["1"]))
    self.assertFalse(marker_selected("MANIFESTS/rev-index/el9/root/v6.30-alice2-3", None,
                                     ["v6.30"]))            # another version, not a revision
    self.assertFalse(marker_selected("MANIFESTS/rev-index/el9/zlib/x/1.3-2"))

  def test_listed_in_help(self):
    env = dict(os.environ, BITS_AWS_KEYS_FILE=os.path.join(ROOT, "tests", "_no_such_s3keys"))
    for verb in ("ls", "rm"):
      r = subprocess.run(["bash", os.path.join(ROOT, "bitsStore"), verb, "-h"],
                         capture_output=True, text=True, env=env)
      self.assertEqual(r.returncode, 0, r.stderr)
      self.assertIn("--stale-rev-markers", r.stdout)
    r = subprocess.run(["bash", os.path.join(ROOT, "bitsStore"), "rm", "--stale-rev-markers",
                        "--before", "2026-01-01"], capture_output=True, text=True, env=env)
    self.assertIn("takes only --arch/--package/--version", r.stderr)


if __name__ == "__main__":
  unittest.main()
