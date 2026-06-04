"""Tests for the `bits stats` resource report (bits_helpers/stats.py)."""

import json
import os
import sys
import tempfile
import shutil
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers import stats

GB = 1024 ** 3


class FormattingTest(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(stats.human_bytes(0), "0 B")
        self.assertEqual(stats.human_bytes(512), "512 B")
        self.assertEqual(stats.human_bytes(1536), "1.5 KiB")
        self.assertEqual(stats.human_bytes(int(6.2 * GB)), "6.2 GiB")

    def test_human_time(self):
        self.assertEqual(stats.human_time(8), "8s")
        self.assertEqual(stats.human_time(125), "2m05s")
        self.assertEqual(stats.human_time(3661), "1h01m01s")

    def test_cores(self):
        self.assertAlmostEqual(stats.cores(760), 7.6)


class TraceMetricsTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _trace(self, samples):
        p = os.path.join(self.d, "t.json")
        json.dump(samples, open(p, "w"))
        return p

    def test_peaks_and_average(self):
        # constant 200% cpu (2 cores) for 10s, rss climbing to 1 GiB
        samples = [{"time": t, "cpu": 200, "rss": int(t / 10 * GB), "num_threads": t}
                   for t in range(0, 11)]
        m = stats.trace_metrics(self._trace(samples))
        self.assertEqual(m["peak_cpu"], 200)
        self.assertEqual(m["peak_rss"], GB)
        self.assertEqual(m["peak_threads"], 10)
        self.assertAlmostEqual(m["avg_cpu"], 200.0, places=1)
        self.assertEqual(m["duration"], 10)

    def test_mem_per_thread(self):
        # 4 GiB across 8 threads -> 0.5 GiB/thread (worst-case per sample)
        samples = [{"time": t, "cpu": 800, "rss": 4 * GB, "num_threads": 8}
                   for t in range(0, 5)]
        m = stats.trace_metrics(self._trace(samples))
        self.assertEqual(m["mem_per_thread"], GB // 2)

    def test_empty_or_bad(self):
        self.assertIsNone(stats.trace_metrics(self._trace([])))
        bad = os.path.join(self.d, "bad.json"); open(bad, "w").write("not json")
        self.assertIsNone(stats.trace_metrics(bad))


class FlagsTest(unittest.TestCase):
    RES = {"cpu": 800, "rss": 16 * GB}

    def test_underthreaded_fires(self):
        m = [{"package": "Gaudi", "peak_rss": GB, "peak_cpu": 110,
              "time": 600, "avg_cpu": 100, "cpu_seconds": 600, "peak_threads": 3}]
        f = stats.flags(self.RES, m)
        self.assertTrue(any("j$JOBS" in msg for _, _, msg in f))

    def test_underthreaded_not_fired_when_fast(self):
        # short build -> not flagged even if single-threaded
        m = [{"package": "zlib", "peak_rss": GB, "peak_cpu": 90,
              "time": 8, "avg_cpu": 90, "cpu_seconds": 7, "peak_threads": 1}]
        self.assertEqual(stats.flags(self.RES, m), [])

    def test_underthreaded_not_fired_when_parallel(self):
        # long build but using many cores -> fine
        m = [{"package": "ROOT", "peak_rss": GB, "peak_cpu": 760,
              "time": 1800, "avg_cpu": 600, "cpu_seconds": 1080000, "peak_threads": 8}]
        f = stats.flags(self.RES, m)
        self.assertFalse(any("j$JOBS" in msg for _, _, msg in f))

    def test_oom_risk_fires(self):
        m = [{"package": "big", "peak_rss": int(9 * GB), "peak_cpu": 800,
              "time": 600, "avg_cpu": 700, "cpu_seconds": 1, "peak_threads": 8}]
        f = stats.flags(self.RES, m)
        self.assertTrue(any("mem_per_job" in msg for _, _, msg in f))

    def test_no_flags_when_modest(self):
        m = [{"package": "ok", "peak_rss": int(2 * GB), "peak_cpu": 700,
              "time": 600, "avg_cpu": 650, "cpu_seconds": 1, "peak_threads": 8}]
        self.assertEqual(stats.flags(self.RES, m), [])


class CollectAndRenderTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        json.dump({
            "resources": {"cpu": 800, "rss": 16 * GB},
            "packages": {"build": {
                "ROOT":  {"cpu": 760, "rss": int(6.2 * GB), "time": 1850},
                "Gaudi": {"cpu": 110, "rss": GB, "time": 420},
            }},
        }, open(os.path.join(self.d, "bits_build_stats.json"), "w"))
        sd = os.path.join(self.d, "SPECS", "arch", "Gaudi", "v-1")
        os.makedirs(sd)
        json.dump([{"time": t, "cpu": 100, "rss": GB, "num_threads": 3}
                   for t in range(0, 420, 5)],
                  open(os.path.join(sd, "Gaudi.json"), "w"))

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_collect_merges_trace(self):
        res, metrics = stats.collect(self.d)
        self.assertEqual(res["rss"], 16 * GB)
        gaudi = [m for m in metrics if m["package"] == "Gaudi"][0]
        self.assertIsNotNone(gaudi["avg_cpu"])     # came from the trace
        self.assertEqual(gaudi["peak_threads"], 3)
        self.assertEqual(gaudi["mem_per_thread"], GB // 3)   # 1 GiB / 3 threads
        root = [m for m in metrics if m["package"] == "ROOT"][0]
        self.assertIsNone(root["avg_cpu"])         # no trace

    def test_render_text_has_sections_and_flag(self):
        res, metrics = stats.collect(self.d)
        out = stats.render_text(res, metrics, top=10, sort_key="time")
        self.assertIn("Build resource summary", out)
        self.assertIn("Top", out)
        self.assertIn("Gaudi", out)
        self.assertIn("MEM/THR", out)  # memory-per-thread column present
        self.assertIn("j$JOBS", out)   # under-threaded flag for Gaudi

    def test_missing_stats_returns_empty(self):
        res, metrics = stats.collect(tempfile.mkdtemp())
        self.assertEqual(metrics, [])


if __name__ == "__main__":
    unittest.main()
