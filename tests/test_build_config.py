# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Reconciliation guard for BuildConfig.from_args.

Locks the per-knob effective value against the expression each doBuild read site
uses, with independently-written expected values (not the from_args expression),
so a future divergence in either place is caught.
"""

import unittest
from argparse import Namespace

from bits_helpers.build_config import BuildConfig


class BuildConfigFromArgsTest(unittest.TestCase):

  def _cfg(self, **kw):
    return BuildConfig.from_args(Namespace(**kw))

  def test_resolved_values_pass_through(self):
    cfg = self._cfg(oversubscribe=2.5, unleashFinal=False,
                    criticalPathSchedule=False, requireSignedReuse=True,
                    reusePolicy="relaxed",
                    memPerJobDefault=2048, parallelDownloads=8,
                    parallelSources=4, prefetchWorkers=3,
                    reuseOverlay="/sw/MODULES/ov", reuseCvmfsBase="/cvmfs/x/Packages",
                    reuseBeacon="https://beacon", storeIntegrity=True,
                    monitor="on", monitorUrl="https://m", monitorInstance="i7",
                    monitorInterval=30.0, monitorDiskInterval=90.0,
                    brew=True, autoPatch=False, autoResources=True,
                    provider_policy={"p": "prepend"}, buildLocal=["a", "b"],
                    cvmfsPrefix="/cvmfs/alice")
    self.assertEqual(cfg.oversubscribe, 2.5)
    self.assertIs(cfg.unleash_final, False)
    self.assertIs(cfg.critical_path_schedule, False)
    self.assertIs(cfg.require_signed_reuse, True)
    self.assertEqual(cfg.reuse_policy, "relaxed")
    self.assertEqual(cfg.mem_per_job_default, 2048)
    self.assertEqual(cfg.parallel_downloads, 8)
    self.assertEqual(cfg.parallel_sources, 4)
    self.assertEqual(cfg.prefetch_workers, 3)
    self.assertEqual(cfg.reuse_overlay, "/sw/MODULES/ov")
    self.assertEqual(cfg.reuse_cvmfs_base, "/cvmfs/x/Packages")
    self.assertEqual(cfg.reuse_beacon, "https://beacon")
    self.assertIs(cfg.store_integrity, True)
    self.assertEqual(cfg.monitor, "on")
    self.assertEqual(cfg.monitor_url, "https://m")
    self.assertEqual(cfg.monitor_instance, "i7")
    self.assertEqual(cfg.monitor_interval, 30.0)
    self.assertEqual(cfg.monitor_disk_interval, 90.0)
    self.assertIs(cfg.brew, True)
    self.assertIs(cfg.auto_patch, False)
    self.assertIs(cfg.auto_resources, True)
    self.assertEqual(cfg.provider_policy, {"p": "prepend"})
    self.assertEqual(cfg.build_local, ["a", "b"])
    self.assertEqual(cfg.cvmfs_prefix, "/cvmfs/alice")

  def test_oversubscribe_zero_guards_to_one(self):
    # Resolution may set 0.0 (system: build_oversubscribe: 0); the read sites
    # guard `... or 1.0`, so cfg must yield 1.0, never 0.0.
    self.assertEqual(self._cfg(oversubscribe=0.0).oversubscribe, 1.0)

  def test_absent_knobs_take_the_site_hard_defaults(self):
    # A pre-resolution / non-build args: each field must equal the hard default
    # the getattr at the read site would have used.
    cfg = BuildConfig.from_args(Namespace())
    self.assertEqual(cfg.oversubscribe, 1.0)
    self.assertIs(cfg.unleash_final, True)
    self.assertIs(cfg.critical_path_schedule, True)
    self.assertIs(cfg.require_signed_reuse, False)
    self.assertEqual(cfg.reuse_policy, "strict")
    self.assertEqual(cfg.mem_per_job_default, 0)
    self.assertEqual(cfg.parallel_downloads, 2)
    self.assertEqual(cfg.parallel_sources, 1)
    self.assertEqual(cfg.prefetch_workers, -1)
    self.assertIsNone(cfg.reuse_overlay)
    self.assertIsNone(cfg.reuse_cvmfs_base)
    self.assertIsNone(cfg.reuse_beacon)
    self.assertIs(cfg.store_integrity, False)
    self.assertIsNone(cfg.monitor)
    self.assertIsNone(cfg.monitor_url)
    self.assertIsNone(cfg.monitor_instance)
    self.assertIsNone(cfg.monitor_interval)
    self.assertIsNone(cfg.monitor_disk_interval)
    self.assertIs(cfg.brew, False)
    self.assertIs(cfg.auto_patch, True)
    self.assertIs(cfg.auto_resources, False)
    self.assertEqual(cfg.provider_policy, {})
    self.assertIsNone(cfg.build_local)
    self.assertIsNone(cfg.cvmfs_prefix)

  def test_reuse_policy_falsy_guards_to_strict(self):
    self.assertEqual(self._cfg(reusePolicy="").reuse_policy, "strict")
    self.assertEqual(self._cfg(reusePolicy=None).reuse_policy, "strict")

  def test_relaxed_is_preserved(self):
    self.assertEqual(self._cfg(reusePolicy="relaxed").reuse_policy, "relaxed")

  def test_frozen(self):
    cfg = BuildConfig.from_args(Namespace())
    with self.assertRaises(Exception):
      cfg.oversubscribe = 9.0


if __name__ == "__main__":
  unittest.main()
