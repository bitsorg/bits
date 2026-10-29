# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The end-of-build reuse summary: what was reused, what was built, and why."""

import os
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bits_helpers import reuse_report
from bits_helpers.manifest import BuildManifest


def _e(pkg, outcome, why=None):
    e = {"package": pkg, "outcome": outcome}
    if why:
        e["rebuilt_because"] = why
    return e


PACKAGES = ([_e("defaults-release", "built_from_source")]
            + [_e("p%d" % i, "already_installed") for i in range(249)]
            + [_e("cldconfig", "from_store"), _e("k4rectracker", "from_store"),
               _e("key4hep", "from_store"),
               _e("fccanalyses", "built_from_source", "store tarball differs from the certified one"),
               _e("lcg.bits", "built_from_source", "never stored")])


class TestSummary(unittest.TestCase):

    def test_counts_names_and_reasons(self):
        text = reuse_report.format_summary(PACKAGES)
        self.assertEqual(text.splitlines(), [
            "Reuse: 254 package(s) — 249 already installed, 3 from the store, 2 built",
            "  from the store: cldconfig, k4rectracker, key4hep",
            "  built: fccanalyses (store tarball differs from the certified one), "
            "lcg.bits (never stored)",
            "  built, by reason: 1 store tarball differs from the certified one; 1 never stored"])

    def test_long_lists_are_cut_and_reasons_counted(self):
        built = [_e("b%d" % i, "built_from_source", "not in the store") for i in range(20)]
        text = reuse_report.format_summary(built, limit=3)
        self.assertIn("built: b0 (not in the store), b1 (not in the store), "
                      "b2 (not in the store), … 17 more", text)
        self.assertIn("built, by reason: 20 not in the store", text)

    def test_nothing_recorded_prints_nothing(self):
        self.assertEqual(reuse_report.format_summary([]), "")

    def test_metrics(self):
        lines = reuse_report.metric_lines(PACKAGES, "h-1", "x86_64-el9-gcc14-opt", "key4hep")
        base = 'instance="h-1",job="bits",arch="x86_64-el9-gcc14-opt",package="key4hep"'
        self.assertIn('bits_build_packages{%s,outcome="already_installed"} 249' % base, lines)
        self.assertIn('bits_build_packages{%s,outcome="from_store"} 3' % base, lines)
        self.assertIn('bits_build_rebuilt{%s,reason="never stored"} 1' % base, lines)


class TestRecorded(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_manifest_keeps_why_a_package_was_built(self):
        m = BuildManifest(work_dir=self.tmp, requested_packages=["X"], architecture="a",
                          defaults=["release"], config_dir=self.tmp, config_commit="c")
        spec = {"package": "X", "version": "1", "revision": "1", "hash": "h" * 40,
                "commit_hash": "0", "reuse_note": "not in the store"}
        m.add_package(spec, "built_from_source")
        m.add_package(dict(spec, package="Y"), "from_store")
        got = {e["package"]: e.get("rebuilt_because") for e in m.packages()}
        self.assertEqual(got, {"X": "not in the store", "Y": None})

    def test_reasons(self):
        from bits_helpers import build
        from bits_helpers.sync import NoRemoteSync
        store = SimpleNamespace(writeStore="b3://bucket")     # e.g. a CVMFS read + S3 write sync
        base = {"package": "P", "is_devel_pkg": False}
        cases = [
            (dict(base, is_devel_pkg=True), store, "development package"),
            (dict(base, force_rebuild=True), store, "forced rebuild"),
            (dict(base, redistributable="none"), store, "licence: never stored"),
            (base, NoRemoteSync(), "no remote store"),
            (base, store, "not found in the store"),
        ]
        for spec, helper, want in cases:
            with self.subTest(want=want):
                with patch.object(build, "is_virtual_package", lambda s: False):
                    self.assertEqual(build._rebuild_reason(spec, helper), want)
        with patch.object(build, "is_virtual_package", lambda s: True):
            self.assertEqual(build._rebuild_reason(base, store), "never stored")

    def test_the_report_never_fails_a_build(self):
        from bits_helpers import build
        args = SimpleNamespace(manifest=SimpleNamespace(packages=lambda: PACKAGES),
                               architecture="a")
        shown = []
        with patch.object(build, "banner", lambda fmt, *a: shown.append(fmt % a)), \
             patch("bits_helpers.metrics.push_prometheus", side_effect=OSError("down")):
            build._report_reuse(args, "key4hep", "http://metrics:8428", lambda: "h-1")
        self.assertTrue(shown and shown[0].startswith("Reuse: 254 package(s)"))


if __name__ == "__main__":
    unittest.main()
