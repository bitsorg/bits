"""
Tests for bits_helpers/build_stats.py — the self-tuning resource-stats loop
that feeds the --builders scheduler's ResourceManager.
"""

import json
import os
import tempfile
import unittest

from bits_helpers import build_stats as bs
from bits_helpers.resource_manager import ResourceManager


class _FakeScheduler:
    def debug(self, *a, **k):
        pass


def _write_trace(work_dir, pkg, samples):
    script_dir = os.path.join(work_dir, "SPECS", pkg)
    os.makedirs(script_dir, exist_ok=True)
    with open(os.path.join(script_dir, pkg + ".json"), "w") as fh:
        json.dump(samples, fh)
    return script_dir


class TestBuildStats(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_aggregate_takes_peaks(self):
        sd = _write_trace(self.dir, "root", [
            {"rss": 1_000_000_000, "cpu": 1400, "time": 100},
            {"rss": 3_000_000_000, "cpu": 2800, "time": 600},   # peak
            {"rss": 2_000_000_000, "cpu": 900,  "time": 650},   # time keeps rising
        ])
        path = bs.aggregate_and_write(self.dir, {"root": sd})
        self.assertEqual(path, bs.default_stats_path(self.dir))
        stats = json.load(open(path))
        pkg = stats["packages"]["build"]["root"]
        self.assertEqual(pkg["rss"], 3_000_000_000)
        self.assertEqual(pkg["cpu"], 2800)
        self.assertEqual(pkg["time"], 650)
        # schema essentials present
        self.assertIn("cpu", stats["resources"])
        self.assertEqual(stats["known"], [])
        self.assertEqual(len(stats["defaults"]["cpu"]), 1)

    def test_empty_traces_writes_nothing(self):
        sd = _write_trace(self.dir, "zlib", [])          # empty sample list
        path = bs.aggregate_and_write(self.dir, {"zlib": sd})
        self.assertIsNone(path)
        self.assertFalse(os.path.isfile(bs.default_stats_path(self.dir)))

    def test_missing_trace_skipped(self):
        # package with no json file at all → skipped, others still recorded
        sd_ok = _write_trace(self.dir, "ok", [{"rss": 10, "cpu": 50, "time": 5}])
        path = bs.aggregate_and_write(
            self.dir, {"ok": sd_ok, "gone": os.path.join(self.dir, "SPECS", "gone")})
        stats = json.load(open(path))
        self.assertIn("ok", stats["packages"]["build"])
        self.assertNotIn("gone", stats["packages"]["build"])

    def test_autoload_restamps_resources(self):
        sd = _write_trace(self.dir, "root", [{"rss": 5, "cpu": 7, "time": 9}])
        bs.aggregate_and_write(self.dir, {"root": sd})
        # corrupt the machine totals as if copied from another node
        path = bs.default_stats_path(self.dir)
        data = json.load(open(path))
        data["resources"] = {"cpu": -1, "rss": -1}
        json.dump(data, open(path, "w"))
        # autoload must re-stamp with sane current-machine values
        ap = bs.autoload_stats_path(self.dir)
        self.assertEqual(ap, path)
        restamped = json.load(open(ap))
        self.assertGreater(restamped["resources"]["cpu"], 0)

    def test_autoload_missing_returns_none(self):
        self.assertIsNone(bs.autoload_stats_path(self.dir))

    def test_output_consumable_by_resource_manager(self):
        sd = _write_trace(self.dir, "root", [{"rss": 2_000_000, "cpu": 200, "time": 60}])
        bs.aggregate_and_write(self.dir, {"root": sd})
        path = bs.autoload_stats_path(self.dir)
        rm = ResourceManager(json.load(open(path)), _FakeScheduler())
        # a package present in the stats and a brand-new one (uses defaults)
        admitted = rm.allocResourcesForExternals(
            ["build:root", "build:brandnew"], count=4)
        self.assertTrue(admitted)             # at least one fits an idle machine
        for name in admitted:
            self.assertTrue(name.startswith("build:"))


if __name__ == "__main__":
    unittest.main()
