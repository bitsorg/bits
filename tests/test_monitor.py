# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the opt-in build-host monitor (bits_helpers.monitor)."""
import os
import unittest
from unittest.mock import patch

from bits_helpers import monitor
from bits_helpers.monitor import BuildMonitor, default_instance, _q


class InstanceTests(unittest.TestCase):
    def test_appends_runner_id(self):
        with patch.dict(os.environ, {"BITS_RUNNER_ID": "42"}, clear=False):
            self.assertTrue(default_instance().endswith("-42"))

    def test_ci_runner_id_fallback(self):
        env = {k: "" for k in ("BITS_RUNNER_ID",)}
        env["CI_RUNNER_ID"] = "job7"
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(default_instance().endswith("-job7"))

    def test_no_runner_id_is_bare_host(self):
        clear = {k: "" for k in ("BITS_RUNNER_ID", "CI_RUNNER_ID", "CI_RUNNER_SHORT_TOKEN")}
        with patch.dict(os.environ, clear, clear=False):
            self.assertNotIn("--", default_instance())  # no trailing empty id


class LabelTests(unittest.TestCase):
    def test_escapes_quotes_and_newlines(self):
        self.assertEqual(_q('a"b\nc'), 'a\\"b c')

    def test_label_carries_instance_and_job(self):
        m = BuildMonitor("http://x", instance="host-1")
        self.assertEqual(m._lbl(), 'instance="host-1",job="bits"')
        self.assertIn('mountpoint="/sw"', m._lbl('mountpoint="/sw"'))


class ByteParseTests(unittest.TestCase):
    def test_binary_and_decimal_units(self):
        self.assertEqual(BuildMonitor._bytes("1KiB"), 1024)
        self.assertEqual(BuildMonitor._bytes("2GB"), 2_000_000_000)
        self.assertEqual(BuildMonitor._bytes("1.5MiB"), int(1.5 * 1024 ** 2))
        self.assertEqual(BuildMonitor._bytes("1000"), 1000)

    def test_bad_input_is_none(self):
        self.assertIsNone(BuildMonitor._bytes("N/A"))

    def test_pct(self):
        self.assertEqual(BuildMonitor._pct("12.34%"), "12.3")
        self.assertIsNone(BuildMonitor._pct("--"))


class ActiveLinesTests(unittest.TestCase):
    def test_active_gauges_track_set_active(self):
        m = BuildMonitor("http://x", instance="h")
        m.set_active("ROOT", "x86_64-el9-opt", True)
        m.set_active("Boost", "x86_64-el9-opt", True)
        lines = m._active_lines()
        self.assertIn('bits_build_active_count{instance="h",job="bits"} 2', lines)
        self.assertTrue(any('package="ROOT"' in l and 'arch="x86_64-el9-opt"' in l
                            and l.startswith("bits_build_active{") for l in lines))
        m.set_active("ROOT", "x86_64-el9-opt", False)
        self.assertIn('bits_build_active_count{instance="h",job="bits"} 1', m._active_lines())


class PushTests(unittest.TestCase):
    def test_empty_lines_never_posts(self):
        m = BuildMonitor("http://x", instance="h")
        with patch("urllib.request.urlopen") as uo:
            m._push([])
            uo.assert_not_called()

    def test_push_posts_prometheus_body(self):
        m = BuildMonitor("http://vm:8428", instance="h")
        seen = {}

        class _Resp:
            def close(self): pass

        def _fake_urlopen(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = req.data
            return _Resp()

        with patch("urllib.request.urlopen", _fake_urlopen):
            m._push(['node_load1{instance="h",job="bits"} 1.5'])
        self.assertEqual(seen["url"], "http://vm:8428/api/v1/import/prometheus")
        self.assertIn(b"node_load1", seen["body"])

    def test_push_swallows_errors(self):
        m = BuildMonitor("http://vm:8428", instance="h")
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            m._push(["x 1"])  # must not raise


class ModuleApiTests(unittest.TestCase):
    def test_start_without_url_is_noop(self):
        self.assertIsNone(monitor.start_monitor(url=""))

    def test_note_build_without_monitor_is_safe(self):
        monitor.stop_monitor()          # ensure singleton cleared
        monitor.note_build("X", "arch", True)  # must not raise


if __name__ == "__main__":
    unittest.main()
