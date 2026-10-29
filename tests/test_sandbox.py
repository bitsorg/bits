# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Unit tests for bits_helpers.sandbox.

All tests mock external calls (podman, sandbox-exec, /proc, /.dockerenv) so
they run on any platform without requiring podman to be installed.
"""

from pathlib import Path
import os
import sys
import types
import unittest
from unittest.mock import patch, mock_open, MagicMock

from bits_helpers import sandbox
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
    @patch("bits_helpers.sandbox.sandbox_exec_refusal", return_value=None)
    @patch("bits_helpers.sandbox.sandbox_exec_available", return_value=True)
    def test_explicit_sandbox_exec_macos(self, _s, _r):
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
    @patch("bits_helpers.sandbox.sandbox_exec_refusal", return_value=None)
    @patch("bits_helpers.sandbox.sandbox_exec_available", return_value=True)
    def test_auto_macos_sandbox_exec(self, _s, _r):
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
        mock_profile.assert_called_once()
        self.assertEqual(mock_profile.call_args[0], (False, "/sw"))

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_profile_receives_allow_network_true_when_off(self, mock_profile, _r):
        wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network="off"), _opts(sandbox="sandbox-exec"),
            workdir="/sw",
        )
        mock_profile.assert_called_once()
        self.assertEqual(mock_profile.call_args[0], (True, "/sw"))

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_yaml_boolean_off_enables_network(self, mock_profile, _r):
        # Regression: YAML SafeLoader parses bare `sandbox_network: off` as the
        # Python bool False (not the string "off"). It must still enable network.
        wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network=False), _opts(sandbox="sandbox-exec"),
            workdir="/sw",
        )
        mock_profile.assert_called_once()
        self.assertEqual(mock_profile.call_args[0], (True, "/sw"))

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_yaml_boolean_on_blocks_network(self, mock_profile, _r):
        # `sandbox_network: on` -> Python bool True -> network blocked.
        wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network=True), _opts(sandbox="sandbox-exec"),
            workdir="/sw",
        )
        mock_profile.assert_called_once()
        self.assertEqual(mock_profile.call_args[0], (False, "/sw"))

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_recipe_system_block_is_honoured(self, mock_profile, _r):
        # `system: {sandbox_network: "off"}` in a recipe (the defaults-file form)
        # overrides a blocking global default.
        wrap_build_command(
            LOCAL_CMD, {"package": "P", "system": {"sandbox_network": "off"}},
            _opts(sandbox="sandbox-exec", sandbox_network="on"), workdir="/sw",
        )
        mock_profile.assert_called_once()
        self.assertEqual(mock_profile.call_args[0], (True, "/sw"))

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_top_level_field_beats_system_block(self, mock_profile, _r):
        wrap_build_command(
            LOCAL_CMD, {"package": "P", "sandbox_network": "on",
                        "system": {"sandbox_network": "off"}},
            _opts(sandbox="sandbox-exec", sandbox_network="off"), workdir="/sw",
        )
        mock_profile.assert_called_once()
        self.assertEqual(mock_profile.call_args[0], (False, "/sw"))

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_global_default_off_allows_when_recipe_silent(self, mock_profile, _r):
        # No per-recipe field -> fall back to global --sandbox-network/bits.rc.
        wrap_build_command(
            LOCAL_CMD, _spec(), _opts(sandbox="sandbox-exec", sandbox_network="off"),
            workdir="/sw",
        )
        mock_profile.assert_called_once()
        self.assertEqual(mock_profile.call_args[0], (True, "/sw"))

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_recipe_field_overrides_global_default(self, mock_profile, _r):
        # Global default off, but recipe explicitly asks for network blocked.
        wrap_build_command(
            LOCAL_CMD, _spec(sandbox_network="on"),
            _opts(sandbox="sandbox-exec", sandbox_network="off"),
            workdir="/sw",
        )
        mock_profile.assert_called_once()
        self.assertEqual(mock_profile.call_args[0], (False, "/sw"))

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    @patch("bits_helpers.sandbox.make_sbpl_profile", return_value="/tmp/p.sb")
    def test_global_default_on_blocks_when_recipe_silent(self, mock_profile, _r):
        wrap_build_command(
            LOCAL_CMD, _spec(), _opts(sandbox="sandbox-exec", sandbox_network="on"),
            workdir="/sw",
        )
        mock_profile.assert_called_once()
        self.assertEqual(mock_profile.call_args[0], (False, "/sw"))


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


# ---------------------------------------------------------------------------
# The macOS sandbox: one package's own files, nesting, the default
# ---------------------------------------------------------------------------

ENV = {"PKGHASH": "ab12cd", "PKGNAME": "zlib", "EFFECTIVE_ARCHITECTURE": "osx_arm64",
       "PKGVERSION": "v1.3", "PKGREVISION": "2", "COMMIT_HASH": "51b7f2a",
       "PKGFAMILY": "", "DEVEL_PREFIX": "", "BUILD_FAMILY": ""}


class PackageWritePathsTests(unittest.TestCase):

    def test_only_the_packages_own_files(self):
        with patch.dict(os.environ, {"CCACHE_DIR": "/cc"}):
            sub, lit, dirs = sandbox.package_write_paths(ENV, "/sw", "/bw", ["/extra"])
        self.assertEqual(sub[:7], [
            "/bw/BUILD/ab12cd", "/sw/INSTALLROOT/ab12cd", "/sw/TMP/ab12cd",
            "/sw/TARS/osx_arm64/store/ab/ab12cd", "/sw/TARS/osx_arm64/zlib",
            "/sw/osx_arm64/zlib/v1.3-2", "/sw/SOURCES/zlib/v1.3/51b7f2a"])
        self.assertIn("/cc", sub)
        self.assertIn("/extra", sub)
        self.assertIn("/sw/MODULES", sub)
        self.assertEqual(lit, ["/bw/BUILD/zlib-latest", "/sw/osx_arm64/zlib/latest"])
        for d in ("/sw", "/bw", "/sw/TARS/osx_arm64/store/ab", "/sw/osx_arm64/zlib",
                  "/sw/SOURCES/zlib/v1.3", "/sw/SPECS"):
            self.assertIn(d, dirs)
        self.assertNotIn("/sw/osx_arm64", sub)        # not every package's tree

    def test_family_devel_prefix_and_build_family(self):
        env = dict(ENV, PKGFAMILY="externals", PKGREVISION="", DEVEL_PREFIX="dev",
                   BUILD_FAMILY="gcc")
        sub, lit, _dirs = sandbox.package_write_paths(env, "/sw", "/sw")
        self.assertIn("/sw/osx_arm64/externals/zlib/v1.3", sub)
        self.assertIn("/sw/BUILD/zlib-latest-dev", lit)
        self.assertIn("/sw/osx_arm64/externals/zlib/latest-gcc", lit)

    def test_profile_kept_beside_build_sh_and_scoped(self):
        import tempfile
        d = tempfile.mkdtemp()
        write = sandbox.package_write_paths(ENV, "/sw", "/sw")
        path = make_sbpl_profile(False, "/sw", write=write, path=os.path.join(d, "sandbox.sb"),
                                 replay="sandbox-exec -f sandbox.sb /bin/bash -e -x build.sh")
        content = Path(path).read_text()
        self.assertTrue(content.startswith("; Written by bits"))
        self.assertIn('(allow file-write* (subpath "/sw/INSTALLROOT/ab12cd"))', content)
        self.assertNotIn('(subpath "/sw")', content)
        self.assertIn('file-write-create', content)
        self.assertIn('(subpath "/private/tmp")', content)

    def test_a_latest_link_is_not_resolved_to_its_target(self):
        import tempfile
        d = os.path.realpath(tempfile.mkdtemp())
        os.makedirs(os.path.join(d, "v1"))
        os.symlink("v1", os.path.join(d, "latest"))
        self.assertEqual(sandbox._both(os.path.join(d, "latest"), literal=True),
                         [os.path.join(d, "latest")])
        self.assertEqual(sandbox._both(os.path.join(d, "latest")),
                         [os.path.join(d, "latest"), os.path.join(d, "v1")])

    def test_a_quote_in_a_path_is_refused(self):
        with self.assertRaises(ValueError):
            make_sbpl_profile(False, "/sw", write=(['/a"b'], [], []))

    @patch("bits_helpers.sandbox.resolve_sandbox_mode", return_value="sandbox-exec")
    def test_wrap_writes_the_scoped_profile(self, _r):
        import tempfile
        d = tempfile.mkdtemp()
        opts = _opts(sandbox="sandbox-exec")
        opts.sandboxAllowWrite = ["/extra"]
        cmd = wrap_build_command(LOCAL_CMD, _spec(), opts, workdir="/sw", pkg_env=ENV,
                                 build_workdir="/sw", script_dir=d)
        self.assertIn(os.path.join(d, "sandbox.sb"), cmd)
        self.assertIn('(subpath "/extra")', Path(os.path.join(d, "sandbox.sb")).read_text())
        self.assertIn("grep 'Operation not permitted'", sandbox.failure_hint(cmd))
        self.assertEqual(sandbox.failure_hint(LOCAL_CMD), "")


class NestingTests(unittest.TestCase):

    def setUp(self):
        sandbox._REFUSAL.clear()
        self.addCleanup(sandbox._REFUSAL.clear)

    @patch("bits_helpers.sandbox.subprocess.run", return_value=MagicMock(returncode=1))
    def test_already_in_a_sandbox(self, _run):
        self.assertIn("do not nest", sandbox.sandbox_exec_refusal())

    @patch("bits_helpers.sandbox.subprocess.run", return_value=MagicMock(returncode=0))
    def test_usable_and_tried_once(self, run):
        self.assertIsNone(sandbox.sandbox_exec_refusal())
        self.assertIsNone(sandbox.sandbox_exec_refusal())
        run.assert_called_once()

    @patch("sys.platform", "darwin")
    @patch("bits_helpers.sandbox.sandbox_exec_available", return_value=True)
    @patch("bits_helpers.sandbox.sandbox_exec_refusal", return_value="nested")
    def test_explicit_refused_auto_off(self, _r, _a):
        with self.assertRaises(ValueError):
            resolve_sandbox_mode("sandbox-exec", False)
        self.assertEqual(resolve_sandbox_mode("auto", False), "off")


class DefaultModeTests(unittest.TestCase):

    @patch("sys.platform", "darwin")
    @patch("bits_helpers.sandbox.sandbox_exec_available", return_value=True)
    @patch("bits_helpers.sandbox.sandbox_exec_refusal", return_value=None)
    def test_env_or_defaults_turn_it_on_for_a_local_mac_build(self, _r, _a):
        self.assertEqual(sandbox.default_mode("1", None, False), "sandbox-exec")
        self.assertEqual(sandbox.default_mode(None, True, False), "sandbox-exec")
        self.assertEqual(sandbox.default_mode("", "on", False), "sandbox-exec")
        self.assertEqual(sandbox.default_mode("0", True, False), "off")   # env wins
        self.assertEqual(sandbox.default_mode("1", None, True), "off")    # --docker
        self.assertEqual(sandbox.default_mode(None, None, False), "off")

    @patch("sys.platform", "darwin")
    @patch("bits_helpers.sandbox.sandbox_exec_available", return_value=True)
    @patch("bits_helpers.sandbox.sandbox_exec_refusal", return_value="nested")
    def test_by_default_where_it_cannot_run_it_is_off(self, _r, _a):
        self.assertEqual(sandbox.default_mode("1", None, False), "off")

    @patch("sys.platform", "linux")
    def test_never_elsewhere(self):
        self.assertEqual(sandbox.default_mode("1", True, False), "off")


if __name__ == "__main__":
    unittest.main()
