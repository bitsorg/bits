# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Console progress must work in terminals, redirected logs and parallel builds."""

from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers import build
from bits_helpers.build_progress import BuildProgress, package_info, package_label, set_package_progress
from bits_helpers.cmd import execute
from bits_helpers.download import download
from bits_helpers.workarea import checkout_sources


class BuildProgressTest(unittest.TestCase):
    def test_plan_numbers_include_reuse_and_exclude_defaults_and_unselected(self):
        specs = {p: {"package": p, "version": "1"} for p in ("defaults-release", "A", "B", "Main")}
        set_package_progress(specs, ["defaults-release", "A", "B"])
        self.assertEqual(package_label(specs["A"]), "[1/2] A@1")
        self.assertEqual(package_label(specs["B"], "dev"), "[2/2] B@dev")
        self.assertNotIn("_build_progress", specs["defaults-release"])
        self.assertNotIn("_build_progress", specs["Main"])

    @patch("bits_helpers.build_progress.sys.stdout.isatty", return_value=False)
    def test_redirected_command_reports_stages_without_compiler_noise(self, _tty):
        messages = []
        progress = BuildProgress("[3/16] Python@3.13.11", "Building", emit=messages.append)
        command = ["/bin/bash", "-ex", "-c",
                   'echo bits:stage:prepare; echo bits:stage:recipe; '
                   'Configure() { :; }; Make() { echo "[3/5] compiler output"; }; '
                   'MakeInstall() { :; }; Configure; Make; MakeInstall; '
                   'echo bits:stage:package; echo bits:stage:relocate']
        self.assertEqual(execute(command, printer=progress), 0)
        progress.end()
        self.assertEqual(messages, [
            "==> [3/16] Python@3.13.11: Building",
            "==> [3/16] Python@3.13.11: Preparing build environment",
            "==> [3/16] Python@3.13.11: Running build recipe",
            "==> [3/16] Python@3.13.11: Configuring",
            "==> [3/16] Python@3.13.11: Compiling",
            "==> [3/16] Python@3.13.11: Installing",
            "==> [3/16] Python@3.13.11: done",
        ])

    @patch("bits_helpers.log.logger.isEnabledFor", return_value=False)
    @patch("bits_helpers.build_progress.sys.stdout.isatty", return_value=True)
    def test_terminal_stage_changes_reset_compiler_percentage(self, _tty, _debug):
        output = StringIO()
        with patch("bits_helpers.log.sys.stderr", output):
            progress = BuildProgress("[1/2] A@1", "Compiling")
            progress("%s", "bits:stage:recipe")
            progress.display.lasttime -= 1
            progress("%s", "[1/2] Building CXX object")
            self.assertEqual(progress.display.percent, 50)
            progress("%s", "bits:stage:package")
            self.assertEqual(progress.display.percent, -1)
            progress.end()
        self.assertIn("Running build recipe", output.getvalue())
        self.assertIn("50%", output.getvalue())
        self.assertIn("Installing", output.getvalue())
        # the line ends cleared: no spaces left where the spinner was
        self.assertIn(": \033[32mdone\033[m\033[K\n", output.getvalue())

    @patch("bits_helpers.build_progress.sys.stdout.isatty", return_value=True)
    def test_parallel_output_is_labeled_and_does_not_use_spinners(self, _tty):
        messages = []

        def report(i):
            progress = BuildProgress("[{}/8] P{}@1".format(i, i), "Compiling",
                                     emit=messages.append, interactive=False)
            progress("%s", "bits:stage:recipe")
            progress.end()

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(report, range(1, 9)))
        self.assertEqual(len(messages), 24)
        for i in range(1, 9):
            self.assertEqual(len([m for m in messages if m.startswith("==> [{}/8]".format(i))]), 3)
        self.assertFalse(any("\033" in m for m in messages))

    @patch("bits_helpers.build_progress.logger.isEnabledFor", return_value=True)
    @patch("bits_helpers.build_progress.sys.stdout.isatty", return_value=True)
    @patch("bits_helpers.build_progress.debug")
    def test_debug_retains_build_output_and_ignores_traced_markers(self, debug, _tty, _enabled):
        messages = []
        progress = BuildProgress("A@1", "Compiling", emit=messages.append)
        progress("%s", "+ echo bits:stage:package")
        progress("%s", "compiler details")
        progress("%s", "bits:stage:unknown")
        self.assertEqual(len(messages), 1)
        self.assertEqual(debug.call_count, 3)
        progress("%s", "bits:stage:package")
        self.assertEqual(messages[-1], "==> A@1: Installing")

    @patch("bits_helpers.build_progress.sys.stdout.isatty", return_value=False)
    def test_failure_is_visible_even_without_subprocess_output(self, _tty):
        messages = []
        progress = BuildProgress("[2/3] A@1", "Compiling", emit=messages.append)
        self.assertEqual(execute(["/bin/bash", "-c", "exit 7"], printer=progress), 7)
        progress.end(error=True)
        self.assertEqual(messages, ["==> [2/3] A@1: Compiling", "==> [2/3] A@1: failed"])

    @patch("bits_helpers.build_progress.sys.stdout.isatty", return_value=False)
    def test_run_build_reports_cached_archive_and_store_sync(self, _tty):
        spec = {"package": "A", "version": "1", "revision": "1", "hash": "a" * 40,
                "is_devel_pkg": False, "requires": [], "_build_progress": (2, 3)}
        args = Namespace(builders=1, debug=False, resourceMonitoring=False,
                         workDir="/sw", architecture="slc7_x86-64")
        helper = Namespace(remoteStore="b3://cache", writeStore="b3://cache")
        messages = []
        with patch.object(build, "info", messages.append), \
             patch.object(build, "execute", side_effect=lambda cmd, printer: execute(cmd, printer=printer)), \
             patch.object(build, "doFinalSync") as sync, \
             patch.object(build, "detectArch", return_value="slc7_x86-64"), \
             patch.dict("os.environ", {"BITS_DIST_HASH": "0" * 40}), \
             patch("bits_helpers.monitor.note_build"), \
             patch("bits_helpers.progress.tick"):
            build.runBuildCommand(None, "A", {"A": spec}, args,
                                  ["/bin/bash", "-c", "echo bits:stage:unpack"],
                                  "/cache/A.tar.gz", "/specs/A", "/sw", helper)
        sync.assert_called_once()
        self.assertEqual(messages, [
            "==> [2/3] A@1: Unpacking [from store]",
            "==> [2/3] A@1: Unpacking cached archive",
            "==> [2/3] A@1: Syncing package to store",
            "==> [2/3] A@1: done",
        ])

    def test_common_build_commands_report_their_stage(self):
        commands = {
            "configure": [
                "++ ./configure --prefix='/path with spaces'",
                "+ /src/Configure linux-x86_64", "+ perl ./Configure",
                "+ cmake -S /src -B build", "+ env CC=gcc cmake ..",
                "+ meson setup build",
            ],
            "compile": [
                "+ make -j8", "+ gmake -C build", "+ ninja -C build",
                "+ cmake --build build --parallel 8", "+ meson compile -C build",
                "+ python3 -m build", "+ python3 setup.py build_ext",
            ],
            "install": [
                "+ make -j 8 install", "+ make -j install", "+ gmake install-strip",
                "+ ninja -C build install", "+ cmake --install build",
                "+ cmake --build build --target install", "+ meson install -C build",
                "+ python3.13 -m pip install .", "+ python setup.py install",
                "+ install -m 755 program /dest/bin",
            ],
        }
        for stage, traces in commands.items():
            for trace in traces:
                with self.subTest(trace=trace):
                    progress = BuildProgress("A@1", "Building", emit=lambda text: None, interactive=False)
                    progress("bits:stage:recipe")
                    progress(trace)
                    self.assertEqual(progress.stage, {"configure": "Configuring",
                                                      "compile": "Compiling", "install": "Installing"}[stage])

    def test_trace_detection_ignores_output_queries_and_commands_outside_recipe(self):
        messages = []
        progress = BuildProgress("A@1", "Building", emit=messages.append, interactive=False)
        progress("+ cmake ..")
        self.assertEqual(progress.stage, "Building")
        progress("bits:stage:recipe")
        for line in ("make install", "+ echo make install", "+ echo bits:stage:install",
                     "+ cmake --version", "+ cmake -E copy a b", "+ cmake -P helper.cmake",
                     "+ make --version", "+ make clean", "+ ninja -t targets",
                     "+ ninja -n install", "+ make -C install", "+ echo 'unterminated"):
            progress(line)
        # -C install names a directory; it is still a compile command.
        self.assertEqual(messages, ["==> A@1: Building", "==> A@1: Running build recipe", "==> A@1: Compiling"])
        progress("bits:stage:post-install")
        progress("+ cmake ..")
        self.assertEqual(progress.stage, "Installing")

    def test_explicit_recipe_markers_support_custom_build_tools(self):
        messages = []
        progress = BuildProgress("A@1", "Building", emit=messages.append, interactive=False)
        self.assertEqual(execute(["/bin/bash", "-ex", "-c",
                                 "echo bits:stage:configure; echo bits:stage:compile; "
                                 "echo bits:stage:install"], printer=progress), 0)
        self.assertEqual(messages, ["==> A@1: Building", "==> A@1: Configuring", "==> A@1: Compiling",
                                    "==> A@1: Installing"])


class PackageLineTest(unittest.TestCase):
    def test_package_lines_align_with_the_stage_lines(self):
        with patch("bits_helpers.build_progress.info") as log:
            package_info("%s: Fetching sources from %s", "[3/16] demo@1", "a.tar.gz")
        log.assert_called_once_with("==> %s: Fetching sources from %s", "[3/16] demo@1", "a.tar.gz")


class SourceProgressTest(unittest.TestCase):
    def test_archive_origin_reports_upstream_store_fallback_and_local_cache(self):
        url = "https://example.com/demo-1.tar.gz"
        messages = []
        helper = Namespace(remoteStore="b3://cache", fetch_source=lambda *args: False,
                           upload_source=lambda *args: None)

        def fetch(source, directory, work_dir):
            Path(directory, "demo-1.tar.gz").write_bytes(b"source archive")

        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict("bits_helpers.download.downloadHandlers", {"https": fetch}):
            download(url, None, tmp, sync_helper=helper, progress=messages.append)
            download(url, None, tmp, sync_helper=helper, progress=messages.append)
            self.assertEqual(messages[:2], ["b3://cache (source: {})".format(url), url])
            self.assertTrue(messages[2].startswith("local cache " + tmp))
            self.assertTrue(messages[2].endswith("(source: {})".format(url)))

    def test_store_hit_does_not_report_upstream_download(self):
        url = "https://example.com/demo-1.tar.gz"
        messages = []

        def fetch(url_hash, filename, directory):
            Path(directory, filename).write_bytes(b"cached source archive")

        helper = Namespace(remoteStore="https://cache.example.com", fetch_source=fetch)
        with tempfile.TemporaryDirectory() as tmp:
            download(url, None, tmp, sync_helper=helper, progress=messages.append)
        self.assertEqual(messages, ["https://cache.example.com (source: {})".format(url)])

    def test_git_checkout_reports_package_url_and_ref(self):
        from bits_helpers.git import Git
        spec = {"package": "demo", "version": "1", "source": "https://example.com/demo.git",
                "scm": Git(), "tag": "v1", "commit_hash": "v1", "is_devel_pkg": False,
                "_build_progress": (3, 16)}
        with tempfile.TemporaryDirectory() as tmp, \
             patch("bits_helpers.workarea.logged_scm"), \
             patch("bits_helpers.workarea.package_info") as log:
            checkout_sources(spec, tmp, tmp, False)
        log.assert_called_once_with("%s: Fetching sources from %s", "[3/16] demo@1",
                                    "https://example.com/demo.git (ref: v1)")

    def test_resolved_archive_url_has_package_label(self):
        spec = {"package": "demo", "version": "1", "sources": [
                    "(osx.*)https://example.com/mac.tar.gz",
                    "(slc.*)https://example.com/%(name)s-%(version)s.tar.gz,sha256:abc"],
                "scm": None, "tag": "v1", "commit_hash": "v1", "_build_progress": (3, 16)}

        def fetch(url, directory, work_dir, **kwargs):
            kwargs["progress"](url)

        with tempfile.TemporaryDirectory() as tmp, \
             patch("bits_helpers.workarea.download", side_effect=fetch), \
             patch("bits_helpers.workarea.package_info") as log:
            checkout_sources(spec, tmp, tmp, False, architecture="slc7_x86-64")
        log.assert_called_once_with("%s: Fetching sources from %s", "[3/16] demo@1",
                                    "https://example.com/demo-1.tar.gz")


if __name__ == "__main__":
    unittest.main()
