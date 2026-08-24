# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for bits_helpers.sandbox.

All tests mock external calls (podman, sandbox-exec, /proc, /.dockerenv) so
they run on any platform without requiring podman to be installed.
"""

import os
import sys
import types
import unittest
from unittest.mock import patch, mock_open, MagicMock

from bits_helpers.sandbox import (
    detect_dind,
    podman_available,
    sandbox_exec_available,
    resolve_sandbox_mode,
    make_sbpl_profile,
    wrap_build_command,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _opts(sandbox="auto", sandbox_image=None, sandbox_network="on"):
    """Return a minimal argparse-like namespace."""
    ns = types.SimpleNamespace()
    ns.sandbox = sandbox
    ns.sandboxImage = sandbox_image
    ns.sandboxNetwork = sandbox_network
    return ns


_UNSET = object()


def _spec(pkg="TestPkg", sandbox_network=_UNSET):
    spec = {"package": pkg}
    if sandbox_network is not _UNSET:
        spec["sandbox_network"] = sandbox_network
    return spec


LOCAL_CMD = "env FOO=bar bash -e -x /sw/slc8_x86-64/TestPkg/build.sh 2>&1"
DOCKER_CMD = (
    "docker run --rm --entrypoint= "
    "-v /sw:/container/bits/sw "
    "-e WORK_DIR_OVERRIDE=/container/bits/sw "
    "alisw/slc8-builder:latest bash -ex /build.sh"
)


# ---------------------------------------------------------------------------
# detect_dind
# ---------------------------------------------------------------------------

class DetectDindTests(unittest.TestCase):

    @patch("sys.platform", "darwin")
    def test_darwin_always_false(self):
        self.assertFalse(detect_dind())

    @patch("sys.platform", "linux")
    @patch("os.path.exists", return_value=True)
    def test_dockerenv_present(self, _exists):
        self.assertTrue(detect_dind())

    @patch("sys.platform", "linux")
    @patch("os.path.exists", return_value=False)
    def test_cgroup_docker_marker(self, _exists):
        cgroup_content = "12:devices:/docker/abc123\n0::/\n"
        with patch("builtins.open", mock_open(read_data=cgroup_content)):
            self.assertTrue(detect_dind())

    @patch("sys.platform", "linux")
    @patch("os.path.exists", return_value=False)
    def test_cgroup_kubepods_marker(self, _exists):
        cgroup_content = "1:cpuset:/kubepods/besteffort/pod123\n"
        with patch("builtins.open", mock_open(read_data=cgroup_content)):
            self.assertTrue(detect_dind())

    @patch("sys.platform", "linux")
    @patch("os.path.exists", return_value=False)
    def test_no_markers(self, _exists):
        cgroup_content = "12:devices:/user.slice\n0::/\n"
        with patch("builtins.open", mock_open(read_data=cgroup_content)):
            self.assertFalse(detect_dind())

    @patch("sys.platform", "linux")
    @patch("os.path.exists", return_value=False)
    def test_proc_unreadable(self, _exists):
        with patch("builtins.open", side_effect=OSError):
            self.assertFalse(detect_dind())


# ---------------------------------------------------------------------------
# podman_available / sandbox_exec_available
# ---------------------------------------------------------------------------

class PodmanAvailableTests(unittest.TestCase):

    @patch("shutil.which", return_value=None)
    def test_no_binary(self, _which):
        self.assertFalse(podman_available())

    @patch("shutil.which", return_value="/usr/bin/podman")
    @patch("subprocess.run")
    def test_binary_works(self, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=0)
        self.assertTrue(podman_available())

    @patch("shutil.which", return_value="/usr/bin/podman")
    @patch("subprocess.run")
    def test_binary_fails(self, mock_run, _which):
        mock_run.return_value = MagicMock(returncode=1)
        self.assertFalse(podman_available())

    @patch("shutil.which", return_value="/usr/bin/podman")
    @patch("subprocess.run", side_effect=Exception("timeout"))
    def test_exception(self, _run, _which):
        self.assertFalse(podman_available())


class SandboxExecAvailableTests(unittest.TestCase):

    @patch("sys.platform", "linux")
    def test_linux_always_false(self):
        self.assertFalse(sandbox_exec_available())

    @patch("sys.platform", "darwin")
    @patch("shutil.which", return_value="/usr/bin/sandbox-exec")
    def test_darwin_present(self, _which):
        self.assertTrue(sandbox_exec_available())

    @patch("sys.platform", "darwin")
    @patch("shutil.which", return_value=None)
    def test_darwin_missing(self, _which):
        self.assertFalse(sandbox_exec_available())


# ---------------------------------------------------------------------------
# resolve_sandbox_mode
# ---------------------------------------------------------------------------

class ResolveSandboxModeTests(unittest.TestCase):

    def test_off_always_off(self):
        self.assertEqual("off", resolve_sandbox_mode("off", False))
        self.assertEqual("off", resolve_sandbox_mode("off", True))

    @patch("bits_helpers.sandbox.podman_available", return_value=True)
    def test_explicit_podman_available(self, _p):
        self.assertEqual("podman", resolve_sandbox_mode("podman", False))

    @patch("bits_helpers.sandbox.podman_available", return_value=False)
    def test_explicit_podman_missing_raises(self, _p):
        with self.assertRaises(ValueError):
            resolve_sandbox_mode("podman", False)

    # explicit podman under --docker probes the image, not the host
    @patch("bits_helpers.sandbox.podman_in_image", return_value=False)
    def test_explicit_podman_docker_missing_in_image_raises(self, _p):
        with self.assertRaises(ValueError) as ctx:
            resolve_sandbox_mode("podman", True, image="img")
        self.assertIn("builder image", str(ctx.exception))

    @patch("sys.platform", "darwin")
    @patch("bits_helpers.sandbox.sandbox_exec_available", return_value=True)
    def test_explicit_sandbox_exec_macos(self, _s):
        self.assertEqual("sandbox-exec", resolve_sandbox_mode("sandbox-exec", False))

    @patch("sys.platform", "linux")
    def test_explicit_sandbox_exec_linux_raises(self):
        with self.assertRaises(ValueError):
            resolve_sandbox_mode("sandbox-exec", False)

    # auto, no docker, Linux: sandboxing is off and podman is NOT probed.
    # podman is only used inside --docker or when explicitly requested.
    @patch("sys.platform", "linux")
    @patch("bits_helpers.sandbox.podman_available", return_value=True)
    def test_auto_linux_no_docker_is_off(self, _p):
        self.assertEqual("off", resolve_sandbox_mode("auto", False))
        _p.assert_not_called()

    @patch("sys.platform", "linux")
    @patch("bits_helpers.sandbox.podman_available", return_value=False)
    def test_auto_linux_no_docker_off_without_probe(self, _p):
        self.assertEqual("off", resolve_sandbox_mode("auto", False))
        _p.assert_not_called()

    # auto, no docker, macOS
    @patch("sys.platform", "darwin")
    @patch("bits_helpers.sandbox.sandbox_exec_available", return_value=True)
    def test_auto_macos_sandbox_exec(self, _s):
        self.assertEqual("sandbox-exec", resolve_sandbox_mode("auto", False))

    @patch("sys.platform", "darwin")
    @patch("bits_helpers.sandbox.sandbox_exec_available", return_value=False)
    def test_auto_macos_no_sandbox_exec(self, _s):
        self.assertEqual("off", resolve_sandbox_mode("auto", False))

    # auto, with docker: podman is probed INSIDE the builder image, not the host
    @patch("bits_helpers.sandbox.podman_in_image", return_value=True)
    def test_auto_docker_podman_in_image(self, _p):
        self.assertEqual("podman", resolve_sandbox_mode("auto", True, image="img"))

    @patch("bits_helpers.sandbox.podman_in_image", return_value=False)
    def test_auto_docker_no_podman_in_image(self, _p):
        self.assertEqual("off", resolve_sandbox_mode("auto", True, image="img"))


# ---------------------------------------------------------------------------
# wrap_build_command — mode=off
# ---------------------------------------------------------------------------

class WrapOffTests(unittest.TestCase):

    def test_off_returns_unchanged(self):
        result = wrap_build_command(
            LOCAL_CMD, _spec(), _opts(sandbox="off"),
            workdir="/sw",
        )
        self.assertEqual(result, LOCAL_CMD)


# ---------------------------------------------------------------------------
# wrap_build_command — podman, local (no docker)
# ---------------------------------------------------------------------------

class WrapPodmanLocalTests(unittest.TestCase):

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="podman")
    def test_network_blocked_by_default(self, _r):
        result = wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network="on"),
            _opts(sandbox="podman", sandbox_image="myimage"),
            workdir="/sw",
        )
        self.assertIn("--network=none", result)
        self.assertIn("podman run", result)
        self.assertIn("-v /sw:/sw", result)

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="podman")
    def test_network_allowed_when_off(self, _r):
        result = wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network="off"),
            _opts(sandbox="podman", sandbox_image="myimage"),
            workdir="/sw",
        )
        self.assertNotIn("--network=none", result)
        self.assertIn("podman run", result)

    @patch("bits_helpers.sandbox.warning")
    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="podman")
    def test_no_image_explicit_podman_warns(self, _r, mock_warn):
        """--sandbox=podman with no image emits a warning and returns unchanged."""
        result = wrap_build_command(
            LOCAL_CMD, _spec(), _opts(sandbox="podman", sandbox_image=None),
            workdir="/sw",
        )
        self.assertEqual(result, LOCAL_CMD)
        self.assertTrue(mock_warn.called, "expected a warning for explicit podman + no image")

    @patch("bits_helpers.sandbox.warning")
    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="podman")
    def test_no_image_auto_mode_silent(self, _r, mock_warn):
        """sandbox=auto with no image falls back silently (no console warning)."""
        result = wrap_build_command(
            LOCAL_CMD, _spec(), _opts(sandbox="auto", sandbox_image=None),
            workdir="/sw",
        )
        self.assertEqual(result, LOCAL_CMD)
        self.assertFalse(mock_warn.called, "auto-detected podman with no image must not warn")

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="podman")
    def test_no_image_returns_unchanged(self, _r):
        """Without an image, sandbox falls back gracefully (legacy compat check)."""
        result = wrap_build_command(
            LOCAL_CMD, _spec(), _opts(sandbox="auto", sandbox_image=None),
            workdir="/sw",
        )
        self.assertEqual(result, LOCAL_CMD)

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="podman")
    def test_build_cmd_quoted_as_shell_arg(self, _r):
        """The original build_command must appear as a single quoted argument."""
        result = wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network="on"),
            _opts(sandbox="podman", sandbox_image="img"),
            workdir="/sw",
        )
        # shlex.quote wraps the original command in single quotes
        self.assertIn("/bin/bash -c", result)


# ---------------------------------------------------------------------------
# wrap_build_command — podman, nested inside Docker
# ---------------------------------------------------------------------------

class WrapPodmanDockerTests(unittest.TestCase):

    @patch("bits_helpers.sandbox.detect_dind", return_value=False)
    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="podman")
    def test_replaces_inner_entrypoint(self, _r, _d):
        result = wrap_build_command(
            DOCKER_CMD, _spec(sandbox_network="on"),
            _opts(sandbox="podman"),
            workdir="/sw",
            docker_active=True,
            container_workdir="/container/bits/sw",
            docker_image="alisw/slc8-builder:latest",
        )
        self.assertIn("podman run", result)
        self.assertIn("--network=none", result)
        # The original docker run prefix is preserved
        self.assertIn("docker run", result)
        # 'bash -ex /build.sh' appears only inside the nested podman call
        self.assertIn("bash -ex /build.sh", result)

    @patch("bits_helpers.sandbox.detect_dind", return_value=False)
    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="podman")
    def test_network_allowed_in_docker(self, _r, _d):
        result = wrap_build_command(
            DOCKER_CMD, _spec(sandbox_network="off"),
            _opts(sandbox="podman"),
            workdir="/sw",
            docker_active=True,
            container_workdir="/container/bits/sw",
            docker_image="alisw/slc8-builder:latest",
        )
        self.assertNotIn("--network=none", result)

    @patch("bits_helpers.sandbox.detect_dind", return_value=True)
    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="podman")
    def test_dind_warning_emitted(self, _r, _d):
        """DinD detection should trigger a warning (not an exception)."""
        warnings = []
        with patch("bits_helpers.sandbox.warning", side_effect=lambda *a, **kw: warnings.append(a)):
            wrap_build_command(
                DOCKER_CMD, _spec(sandbox_network="on"),
                _opts(sandbox="podman"),
                workdir="/sw",
                docker_active=True,
                container_workdir="/container/bits/sw",
                docker_image="alisw/slc8-builder:latest",
            )
        self.assertTrue(any("DinD" in str(w) or "already running inside" in str(w)
                            for w in warnings))

    @patch("bits_helpers.sandbox.detect_dind", return_value=False)
    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="podman")
    def test_missing_marker_returns_unchanged(self, _r, _d):
        bad_cmd = "docker run alisw/slc8-builder:latest /custom_entry.sh"
        result = wrap_build_command(
            bad_cmd, _spec(), _opts(sandbox="podman"),
            workdir="/sw",
            docker_active=True,
            container_workdir="/container/bits/sw",
            docker_image="alisw/slc8-builder:latest",
        )
        self.assertEqual(result, bad_cmd)


# ---------------------------------------------------------------------------
# wrap_build_command — sandbox-exec (macOS)
# ---------------------------------------------------------------------------

class WrapSandboxExecTests(unittest.TestCase):

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/bits-sandbox-abc.sb")
    def test_sandbox_exec_prefix(self, _profile, _r):
        result = wrap_build_command(
            LOCAL_CMD, _spec(), _opts(sandbox="sandbox-exec"),
            workdir="/sw",
        )
        self.assertTrue(result.startswith("sandbox-exec -f"))
        self.assertIn("/tmp/bits-sandbox-abc.sb", result)
        self.assertIn("/bin/bash -c", result)

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_profile_receives_allow_network_false_by_default(self, mock_profile, _r):
        wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network="on"), _opts(sandbox="sandbox-exec"),
            workdir="/sw",
        )
        # make_sbpl_profile should have been called with allow_network=False
        mock_profile.assert_called_once_with(False, "/sw")

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_profile_receives_allow_network_true_when_off(self, mock_profile, _r):
        wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network="off"), _opts(sandbox="sandbox-exec"),
            workdir="/sw",
        )
        mock_profile.assert_called_once_with(True, "/sw")

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_yaml_boolean_off_enables_network(self, mock_profile, _r):
        # Regression: YAML SafeLoader parses bare `sandbox_network: off` as the
        # Python bool False (not the string "off"). It must still enable network.
        wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network=False), _opts(sandbox="sandbox-exec"),
            workdir="/sw",
        )
        mock_profile.assert_called_once_with(True, "/sw")

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_yaml_boolean_on_blocks_network(self, mock_profile, _r):
        # `sandbox_network: on` -> Python bool True -> network blocked.
        wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network=True), _opts(sandbox="sandbox-exec"),
            workdir="/sw",
        )
        mock_profile.assert_called_once_with(False, "/sw")

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_global_default_off_allows_when_recipe_silent(self, mock_profile, _r):
        # No per-recipe field -> fall back to global --sandbox-network/bits.rc.
        wrap_build_command(
            LOCAL_CMD, _spec(), _opts(sandbox="sandbox-exec", sandbox_network="off"),
            workdir="/sw",
        )
        mock_profile.assert_called_once_with(True, "/sw")

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_recipe_field_overrides_global_default(self, mock_profile, _r):
        # Global default off, but recipe explicitly asks for network blocked.
        wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network="on"),
            _opts(sandbox="sandbox-exec", sandbox_network="off"),
            workdir="/sw",
        )
        mock_profile.assert_called_once_with(False, "/sw")

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_global_default_on_blocks_when_recipe_silent(self, mock_profile, _r):
        wrap_build_command(
            LOCAL_CMD, _spec(), _opts(sandbox="sandbox-exec", sandbox_network="on"),
            workdir="/sw",
        )
        mock_profile.assert_called_once_with(False, "/sw")


# ---------------------------------------------------------------------------
# make_sbpl_profile
# ---------------------------------------------------------------------------

class MakeSbplProfileTests(unittest.TestCase):

    def test_profile_written_to_file(self):
        path = make_sbpl_profile(allow_network=False, builddir="/sw/slc8")
        try:
            self.assertTrue(os.path.exists(path))
            with open(path) as fh:
                content = fh.read()
            self.assertIn("(deny default)", content)
            self.assertIn("/sw/slc8", content)
            self.assertNotIn("(allow network*)", content)
        finally:
            os.unlink(path)

    def test_network_rule_present_when_allowed(self):
        path = make_sbpl_profile(allow_network=True, builddir="/sw")
        try:
            with open(path) as fh:
                content = fh.read()
            self.assertIn("(allow network*)", content)
        finally:
            os.unlink(path)

    def test_canonical_temp_dirs_writable(self):
        # Regression: macOS resolves /tmp -> /private/tmp and
        # /var/folders -> /private/var/folders, and SBPL subpath matching uses
        # the resolved path. Without the /private/... rules the compiler's own
        # $TMPDIR temp files are denied ("C compiler cannot create executables").
        path = make_sbpl_profile(allow_network=False, builddir="/sw")
        try:
            with open(path) as fh:
                content = fh.read()
            for sub in ('(subpath "/private/tmp")',
                        '(subpath "/private/var/folders")',
                        '(subpath "/private/var/tmp")'):
                self.assertIn(sub, content)
        finally:
            os.unlink(path)

    def test_standard_char_devices_writable(self):
        # Regression: /dev/null is outside every allowed write subpath, so
        # without an explicit allow the default-deny breaks `> /dev/null` and
        # thus essentially every autotools configure on macOS.
        path = make_sbpl_profile(allow_network=False, builddir="/sw")
        try:
            with open(path) as fh:
                content = fh.read()
            for dev in ('"/dev/null"', '"/dev/zero"', '"/dev/urandom"',
                        '"/dev/tty"', '"/dev/ptmx"'):
                self.assertIn(dev, content)
            self.assertIn('(subpath "/dev/fd")', content)
            # Raw disk devices must stay denied: no rule should reference them
            # as a quoted SBPL pattern (the explanatory comment may mention them).
            self.assertNotIn('"/dev/disk', content)
            self.assertNotIn('"/dev/rdisk', content)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
