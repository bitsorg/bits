"""
Tests for bits_helpers/memory.py.

All OS-level calls (open /proc/meminfo, subprocess) are mocked so the
suite runs identically on Linux, macOS, and inside restricted sandboxes.
"""

import platform
import unittest
from textwrap import dedent
from unittest.mock import MagicMock, mock_open, patch

from bits_helpers.memory import (
    available_memory_mib,
    effective_jobs,
    parse_memory,
)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  1.  parse_memory                                                        ║
# ╚══════════════════════════════════════════════════════════════════════════╝

class TestParseMemory(unittest.TestCase):
    """parse_memory() must accept integers, floats, and annotated strings."""

    # ── plain numbers ────────────────────────────────────────────────────────

    def test_plain_int(self):
        self.assertEqual(parse_memory(512), 512)

    def test_plain_float(self):
        self.assertEqual(parse_memory(1.5), 1)

    def test_plain_zero_raises(self):
        with self.assertRaises(ValueError):
            parse_memory(0)

    def test_negative_raises(self):
        with self.assertRaises(ValueError):
            parse_memory(-1)

    # ── string without unit → MiB ────────────────────────────────────────────

    def test_string_int_no_unit(self):
        self.assertEqual(parse_memory("1024"), 1024)

    def test_string_float_no_unit(self):
        self.assertEqual(parse_memory("1.5"), 1)

    # ── MiB / MB variants ───────────────────────────────────────────────────

    def test_mib_uppercase(self):
        self.assertEqual(parse_memory("512 MiB"), 512)

    def test_mb_lowercase(self):
        self.assertEqual(parse_memory("512mb"), 512)

    def test_m_shorthand(self):
        self.assertEqual(parse_memory("512m"), 512)

    # ── GiB / GB variants ───────────────────────────────────────────────────

    def test_gib(self):
        self.assertEqual(parse_memory("2 GiB"), 2048)

    def test_gb(self):
        self.assertEqual(parse_memory("2GB"), 2048)

    def test_g_shorthand(self):
        self.assertEqual(parse_memory("2g"), 2048)

    def test_fractional_gib(self):
        self.assertEqual(parse_memory("1.5 GiB"), 1536)

    # ── TiB ─────────────────────────────────────────────────────────────────

    def test_tib(self):
        self.assertEqual(parse_memory("1 TiB"), 1024 * 1024)

    # ── error cases ──────────────────────────────────────────────────────────

    def test_unknown_unit_raises(self):
        with self.assertRaises(ValueError):
            parse_memory("512 XB")

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_memory("lots")

    def test_case_insensitive_unit(self):
        self.assertEqual(parse_memory("2 gib"), 2048)
        self.assertEqual(parse_memory("2 GIB"), 2048)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  2.  available_memory_mib                                                ║
# ╚══════════════════════════════════════════════════════════════════════════╝

_PROC_MEMINFO = dedent("""\
    MemTotal:       16384000 kB
    MemFree:         4096000 kB
    MemAvailable:    8192000 kB
    Buffers:          512000 kB
    Cached:          2048000 kB
""")

_VM_STAT_OUTPUT = dedent("""\
    Mach Virtual Memory Statistics: (page size of 4096 bytes)
    Pages free:                           512000.
    Pages active:                        1024000.
    Pages inactive:                       512000.
    Pages speculative:                     64000.
    Pages wired down:                     256000.
""")


class TestAvailableMemoryMib(unittest.TestCase):

    @patch("platform.system", return_value="Linux")
    def test_linux_uses_mem_available(self, _mock_sys):
        with patch("builtins.open", mock_open(read_data=_PROC_MEMINFO)):
            mib = available_memory_mib()
        # MemAvailable = 8 192 000 kB → 8 000 MiB
        self.assertEqual(mib, 8000)

    @patch("platform.system", return_value="Linux")
    def test_linux_falls_back_to_mem_free(self, _mock_sys):
        data = _PROC_MEMINFO.replace("MemAvailable:", "MemUnavailable:")
        with patch("builtins.open", mock_open(read_data=data)):
            mib = available_memory_mib()
        # MemFree = 4 096 000 kB → 4 000 MiB
        self.assertEqual(mib, 4000)

    @patch("platform.system", return_value="Linux")
    def test_linux_returns_zero_on_error(self, _mock_sys):
        with patch("builtins.open", side_effect=OSError("permission denied")):
            mib = available_memory_mib()
        self.assertEqual(mib, 0)

    @patch("subprocess.check_output")
    @patch("platform.system", return_value="Darwin")
    def test_darwin_sums_free_and_inactive(self, _mock_sys, mock_sub):
        # First call → vm_stat, second call → sysctl hw.pagesize
        mock_sub.side_effect = [_VM_STAT_OUTPUT, "4096\n"]
        mib = available_memory_mib()
        # (512000 + 512000) * 4096 / 1024**2 = 4000 MiB
        self.assertEqual(mib, 4000)

    @patch("platform.system", return_value="Windows")
    def test_unknown_platform_returns_zero(self, _mock_sys):
        mib = available_memory_mib()
        self.assertEqual(mib, 0)

    @patch("platform.system", return_value="Linux")
    def test_exception_returns_zero(self, _mock_sys):
        with patch("builtins.open", side_effect=Exception("unexpected")):
            mib = available_memory_mib()
        self.assertEqual(mib, 0)


# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  3.  effective_jobs                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════╝

def _avail(mib):
    """Patch available_memory_mib to return a fixed value."""
    return patch("bits_helpers.memory.available_memory_mib", return_value=mib)


class TestEffectiveJobs(unittest.TestCase):

    # ── no mem_per_job → passthrough ─────────────────────────────────────────

    def test_no_hint_returns_requested(self):
        spec = {"package": "zlib"}
        self.assertEqual(effective_jobs(8, spec), 8)

    def test_empty_spec_returns_requested(self):
        self.assertEqual(effective_jobs(16, {}), 16)

    # ── memory capping ───────────────────────────────────────────────────────

    def test_cap_applied_when_memory_tight(self):
        # 8 GiB available, 2 GiB/job, 90% utilisation → floor(8192*0.9/2048) = 3
        spec = {"package": "llvm", "mem_per_job": 2048}
        with _avail(8192):
            jobs = effective_jobs(16, spec)
        self.assertEqual(jobs, 3)

    def test_no_cap_when_memory_ample(self):
        # 64 GiB available, 2 GiB/job → floor(65536*0.9/2048) = 28 > 8 requested
        spec = {"package": "llvm", "mem_per_job": 2048}
        with _avail(65536):
            jobs = effective_jobs(8, spec)
        self.assertEqual(jobs, 8)

    def test_minimum_is_one(self):
        # Only 256 MiB available, 2 GiB/job → cap = 0, but floor at 1
        spec = {"package": "llvm", "mem_per_job": 2048}
        with _avail(256):
            jobs = effective_jobs(8, spec)
        self.assertEqual(jobs, 1)

    # ── mem_utilisation ──────────────────────────────────────────────────────

    def test_custom_utilisation(self):
        # 8 GiB available, 1 GiB/job, 50% utilisation → floor(8192*0.5/1024) = 4
        spec = {"package": "root", "mem_per_job": 1024, "mem_utilisation": 0.5}
        with _avail(8192):
            jobs = effective_jobs(16, spec)
        self.assertEqual(jobs, 4)

    def test_utilisation_default_is_ninety_percent(self):
        # 10000 MiB available, 1000 MiB/job, default 0.9 → floor(10000*0.9/1000) = 9
        spec = {"package": "pkg", "mem_per_job": 1000}
        with _avail(10000):
            jobs = effective_jobs(16, spec)
        self.assertEqual(jobs, 9)

    def test_invalid_utilisation_uses_default(self):
        # util=2.0 is out of range; should fall back to 0.9
        spec = {"package": "pkg", "mem_per_job": 1000, "mem_utilisation": 2.0}
        with _avail(10000):
            jobs = effective_jobs(16, spec)
        # floor(10000 * 0.9 / 1000) = 9
        self.assertEqual(jobs, 9)

    def test_zero_utilisation_uses_default(self):
        spec = {"package": "pkg", "mem_per_job": 1000, "mem_utilisation": 0.0}
        with _avail(10000):
            jobs = effective_jobs(16, spec)
        self.assertEqual(jobs, 9)

    # ── memory string syntax via parse_memory ────────────────────────────────

    def test_string_gib_syntax(self):
        # 16 GiB = 16384 MiB available, "2 GiB" per job, default util
        # floor(16384 * 0.9 / 2048) = floor(7.2) = 7
        spec = {"package": "llvm", "mem_per_job": "2 GiB"}
        with _avail(16384):
            jobs = effective_jobs(16, spec)
        self.assertEqual(jobs, 7)

    def test_string_mb_syntax(self):
        spec = {"package": "pkg", "mem_per_job": "1024 MB"}
        with _avail(8192):
            jobs = effective_jobs(16, spec)
        # floor(8192 * 0.9 / 1024) = floor(7.2) = 7
        self.assertEqual(jobs, 7)

    # ── detection failure → passthrough ─────────────────────────────────────

    def test_detection_failure_returns_requested(self):
        spec = {"package": "llvm", "mem_per_job": 2048}
        with _avail(0):          # 0 means "unknown"
            jobs = effective_jobs(8, spec)
        self.assertEqual(jobs, 8)

    # ── invalid mem_per_job → passthrough with warning ───────────────────────

    def test_invalid_mem_per_job_returns_requested(self):
        spec = {"package": "pkg", "mem_per_job": "lots of memory"}
        with _avail(8192):
            jobs = effective_jobs(8, spec)
        self.assertEqual(jobs, 8)

    def test_zero_mem_per_job_returns_requested(self):
        spec = {"package": "pkg", "mem_per_job": 0}
        with _avail(8192):
            jobs = effective_jobs(8, spec)
        self.assertEqual(jobs, 8)

    # ── requested=1 is never lowered ─────────────────────────────────────────

    def test_single_job_never_changed(self):
        spec = {"package": "llvm", "mem_per_job": 65536}   # 64 GiB/job
        with _avail(1024):       # only 1 GiB available
            jobs = effective_jobs(1, spec)
        self.assertEqual(jobs, 1)


if __name__ == "__main__":
    unittest.main()
