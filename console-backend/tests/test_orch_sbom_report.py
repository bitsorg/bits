# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""orch.sbom_report: a CycloneDX SBOM as an HTML report (CycloneDX Sunshine)."""

import subprocess
import threading
import unittest
from unittest.mock import patch

from bits_helpers import sbom
from console_backend.orch import sbom_report


def cdx():
    """A CycloneDX document as bits_helpers.sbom writes it: two packages, one edge."""
    pkgs = [
        {"package": "CMake", "version": "3.30.6", "revision": "3", "hash": "c" * 40,
         "tarball_sha256": "sha256:" + "1" * 64, "license": "BSD-3-Clause",
         "redistributable": "all", "requires": [], "build_requires": []},
        {"package": "ROOT", "version": "v6-40-02", "revision": "5", "hash": "r" * 40,
         "tarball_sha256": "sha256:" + "3" * 64, "license": "LGPL-2.1-or-later",
         "redistributable": "all", "requires": ["CMake"], "build_requires": []},
    ]
    doc = {"schema_version": 4, "updated_at": "2026-10-23T10:00:00Z",
           "architecture": "x86_64-el9-gcc15-opt", "packages": pkgs}
    return sbom.render(doc, "cyclonedx", "atlas-1").encode()


class SbomReportTest(unittest.TestCase):
    def setUp(self):
        sbom_report._cache.clear()

    def test_a_bits_sbom_becomes_a_report_naming_its_build(self):
        page = sbom_report.render(cdx(), "atlas-1").decode()
        self.assertIn("<html", page.lower())
        self.assertIn("LGPL-2.1-or-later", page)
        self.assertIn("atlas-1.cdx.json", page)

    def test_not_a_cyclonedx_document_is_no_report(self):
        with self.assertRaises(sbom_report.Failed):
            sbom_report.render(b"not json", "x")

    def test_a_report_is_drawn_once_per_sbom_and_name(self):
        body = cdx()
        sbom_report.render(body, "atlas-1")
        with patch.object(subprocess, "run", side_effect=AssertionError("ran again")):
            sbom_report.render(body, "atlas-1")          # from the cache
            with self.assertRaises(AssertionError):      # another name: drawn again
                sbom_report.render(body, "atlas-2")

    def test_a_slow_report_fails_and_frees_its_slot(self):
        slow = subprocess.TimeoutExpired("sunshine", sbom_report.TIMEOUT_S)
        with patch.object(sbom_report, "_slots", threading.BoundedSemaphore(1)), \
                patch.object(sbom_report, "WAIT_S", 0.01):
            with patch.object(subprocess, "run", side_effect=slow):
                with self.assertRaisesRegex(sbom_report.Failed, "longer than"):
                    sbom_report.render(cdx(), "x")
            self.assertIn("atlas-1", sbom_report.render(cdx(), "atlas-1").decode())

    def test_a_failing_sunshine_is_no_report_even_with_a_file(self):
        def half(cmd, **kw):
            with open(cmd[cmd.index("-o") + 1], "w") as f:
                f.write("<html>")
            return subprocess.CompletedProcess(cmd, 1, "", "UnicodeEncodeError: x")
        with patch.object(subprocess, "run", side_effect=half):
            with self.assertRaisesRegex(sbom_report.Failed, "UnicodeEncodeError"):
                sbom_report.render(cdx(), "x")
        self.assertEqual(len(sbom_report._cache), 0)

    def test_the_cache_keeps_the_latest_reports(self):
        ok = lambda cmd, **kw: (open(cmd[cmd.index("-o") + 1], "w").write("r"),
                                subprocess.CompletedProcess(cmd, 0, "", ""))[1]
        with patch.object(subprocess, "run", side_effect=ok), \
                patch.object(sbom_report, "KEEP", 2):
            for n in ("a", "b", "c"):
                sbom_report.render(b"{}", n)
        self.assertEqual([k[1] for k in sbom_report._cache], ["b", "c"])

    def test_every_slot_busy_is_no_report(self):
        with patch.object(sbom_report, "_slots", threading.BoundedSemaphore(1)) as slots, \
                patch.object(sbom_report, "WAIT_S", 0.01):
            slots.acquire()
            with self.assertRaisesRegex(sbom_report.Busy, "busy"):
                sbom_report.render(cdx(), "x")

    def test_the_shown_file_name_is_path_safe(self):
        self.assertEqual(sbom_report._file_name("../a b"), ".._a_b.cdx.json")
        self.assertEqual(sbom_report._file_name(""), "sbom.cdx.json")


if __name__ == "__main__":
    unittest.main()
