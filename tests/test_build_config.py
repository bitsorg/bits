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
                    reusePolicy="relaxed")
    self.assertEqual(cfg.oversubscribe, 2.5)
    self.assertIs(cfg.unleash_final, False)
    self.assertIs(cfg.critical_path_schedule, False)
    self.assertIs(cfg.require_signed_reuse, True)
    self.assertEqual(cfg.reuse_policy, "relaxed")

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
