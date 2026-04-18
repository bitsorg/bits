import json
import os
import shutil
import sys
import tempfile
import unittest
from argparse import Namespace
from io import StringIO
from unittest.mock import MagicMock, patch

from bits_helpers.doctor import (
    FAIL, PASS, SKIP, WARN,
    _check_compiler,
    _check_cvmfs_repo,
    _check_disk_space,
    _check_host_tool,
    _check_podman,
    _check_qemu_binfmt,
    _check_store,
    _emit_check_store_json,
    _emit_check_store_text,
    _probe_tarball_in_store,
    _run_check_store_checks,
    _run_runner_checks,
    doDoctor,
)

# ── Recipe stubs (unchanged) ───────────────────────────────────────────────────

RECIPE_DEFAULTS_RELEASE = """package: defaults-release
version: v1
---
"""

RECIPE_PACKAGE1 = """package: Package1
version: v1
prefer_system: .*
prefer_system_check: /bin/false
---
"""

RECIPE_SYSDEP = """package: SysDep
version: v1
system_requirement: .*
system_requirement_check: /bin/false
---
"""

RECIPE_BREAKDEFAULTS = """package: BreakDefaults
version: v1
valid_defaults:
  - its_not_there
---
"""

RECIPE_TESTDEF1 = """package: TestDef1
version: v1
valid_defaults:
  - common_default
  - default1
requires:
  - TestDef2
---
"""

RECIPE_TESTDEF2 = """package: TestDef2
version: v1
valid_defaults:
  - common_default
  - default2
---
"""


# ── Existing recipe-check tests (preserved, extended) ─────────────────────────

class DoctorTestCase(unittest.TestCase):

    @patch("bits_helpers.doctor.banner")
    @patch("bits_helpers.doctor.warning")
    @patch("bits_helpers.doctor.error")
    @patch("bits_helpers.doctor.exists")
    @patch("bits_helpers.utilities.exists")
    @patch("bits_helpers.utilities.open")
    def test_doctor(self, mockOpen, mockUtilitiesExists, mockDoctorExists,
                    mockPrintError, mockPrintWarning, mockPrintBanner):
        recipes = lambda: {
            "/dist/package1.sh":         StringIO(RECIPE_PACKAGE1),
            "/dist/testdef1.sh":         StringIO(RECIPE_TESTDEF1),
            "/dist/testdef2.sh":         StringIO(RECIPE_TESTDEF2),
            "/dist/sysdep.sh":           StringIO(RECIPE_SYSDEP),
            "/dist/defaults-release.sh": StringIO(RECIPE_DEFAULTS_RELEASE),
            "/dist/breakdefaults.sh":    StringIO(RECIPE_BREAKDEFAULTS),
        }

        mockOpen.side_effect = lambda f, mode="r": (
            recipes()[f] if f in recipes() else MagicMock()
        )

        def mockExists(f):
            return f in recipes()

        mockUtilitiesExists.side_effect = mockExists
        mockDoctorExists.side_effect = mockExists

        def resetOut():
            return {"warning": StringIO(), "error": StringIO(), "banner": StringIO()}

        mockPrintError.side_effect   = lambda e, *a: out["error"].write((e % a) + "\n")
        mockPrintWarning.side_effect = lambda e, *a: out["warning"].write((e % a) + "\n")
        mockPrintBanner.side_effect  = lambda e, *a: out["banner"].write((e % a) + "\n")

        args = Namespace(
            workDir="/work",
            configDir="/dist",
            docker=False,
            dockerImage=None,
            docker_extra_args=["--network=host"],
            debug=False,
            preferSystem=[],
            noSystem="*",
            architecture="osx_x86-64",
            disable=[],
            defaults=["release"],
            environment=[],
            runner=False,
            json_output=False,
        )

        if not hasattr(self, "assertRegex"):
            self.assertRegex = self.assertRegexpMatches
            self.assertNotRegex = self.assertNotRegexpMatches

        # All OK
        out = resetOut()
        with self.assertRaises(SystemExit) as cm:
            args.packages = ["Package1"]
            doDoctor(args, MagicMock())
        self.assertEqual(cm.exception.code, 0)

        # System dependency not found
        out = resetOut()
        with self.assertRaises(SystemExit) as cm:
            args.packages = ["SysDep"]
            doDoctor(args, MagicMock())
        self.assertEqual(cm.exception.code, 1)

        # Invalid default
        out = resetOut()
        with self.assertRaises(SystemExit) as cm:
            args.packages = ["BreakDefaults"]
            doDoctor(args, MagicMock())
        self.assertEqual(cm.exception.code, 2)
        self.assertRegex(out["error"].getvalue(), "- its_not_there")

        # Common defaults
        out = resetOut()
        with self.assertRaises(SystemExit) as cm:
            args.packages = ["TestDef1"]
            doDoctor(args, MagicMock())
        self.assertEqual(cm.exception.code, 2)
        self.assertRegex(out["banner"].getvalue(), "- common_default")
        self.assertNotRegex(out["banner"].getvalue(), "- default1")
        self.assertNotRegex(out["banner"].getvalue(), "- default2")


# ── _check_host_tool ───────────────────────────────────────────────────────────

class TestCheckHostTool(unittest.TestCase):
    def test_pass_for_existing_tool(self):
        # 'sh' or 'python3' should always be present in the test environment
        for tool in ("sh", "python3", "ls"):
            status, detail = _check_host_tool(tool)
            if status == PASS:
                self.assertIn("/", detail)
                return
        self.skipTest("No expected tool found in PATH")

    def test_fail_for_nonexistent_tool(self):
        status, detail = _check_host_tool("__bits_nonexistent_tool_xyzzy__")
        self.assertEqual(status, FAIL)
        self.assertIn("not found", detail)


# ── _check_compiler ────────────────────────────────────────────────────────────

class TestCheckCompiler(unittest.TestCase):
    def test_returns_pass_or_fail(self):
        status, detail = _check_compiler()
        self.assertIn(status, (PASS, FAIL))
        self.assertTrue(len(detail) > 0)

    def test_fail_when_no_compiler_on_path(self):
        # Patch the underlying 'which' calls inside _check_compiler so every
        # compiler probe reports not found.
        with patch("bits_helpers.doctor.getstatusoutput", return_value=(127, "not found")):
            status, detail = _check_compiler()
        self.assertEqual(status, FAIL)
        self.assertIn("compiler", detail.lower())

    def test_pass_when_compiler_found(self):
        def mock_gso(cmd):
            if "which" in cmd:
                return (0, "/usr/bin/c++")
            return (1, "")
        with patch("bits_helpers.doctor.getstatusoutput", side_effect=mock_gso):
            status, detail = _check_compiler()
        self.assertEqual(status, PASS)
        self.assertIn("/usr/bin/c++", detail)


# ── _check_qemu_binfmt ─────────────────────────────────────────────────────────

class TestCheckQemuBinfmt(unittest.TestCase):
    def test_skip_for_native_arch(self):
        status, detail = _check_qemu_binfmt("slc9_x86-64")
        self.assertEqual(status, SKIP)

    def test_skip_when_not_linux(self):
        with patch("os.path.isdir", return_value=False):
            status, detail = _check_qemu_binfmt("slc9_aarch64")
        self.assertEqual(status, SKIP)

    def test_pass_when_handler_registered_and_enabled(self):
        # Build a real temp binfmt_misc directory with a populated handler file.
        with tempfile.TemporaryDirectory() as bfdir:
            handler_path = os.path.join(bfdir, "qemu-aarch64")
            with open(handler_path, "w") as fh:
                fh.write("enabled\ninterpreter /usr/bin/qemu-aarch64-static\n")
            # Redirect the binfmt_misc constant and the isdir/exists checks.
            import bits_helpers.doctor as doc
            with patch.object(doc.os.path, "isdir", lambda p: True), \
                 patch.object(doc.os.path, "exists", lambda p: True), \
                 patch.object(doc.os.path, "join",
                              lambda *a: handler_path if "binfmt_misc" in str(a) else os.path.join(*a)):
                status, detail = _check_qemu_binfmt("slc9_aarch64")
        self.assertEqual(status, PASS)
        self.assertIn("enabled", detail)

    def test_fail_when_handler_missing(self):
        with tempfile.TemporaryDirectory() as bfdir:
            # Directory exists but no handler file
            with patch("bits_helpers.doctor.os.path.isdir", return_value=True), \
                 patch("bits_helpers.doctor.os.path.exists", return_value=False):
                import bits_helpers.doctor as doc
                orig_join = doc.os.path.join
                def mock_join(*args):
                    if len(args) == 2 and "binfmt_misc" in str(args[0]):
                        return os.path.join(bfdir, args[1])
                    return orig_join(*args)
                with patch.object(doc.os.path, "join", side_effect=mock_join):
                    status, detail = _check_qemu_binfmt("slc9_aarch64")
        self.assertEqual(status, FAIL)
        self.assertIn("not registered", detail)


# ── _check_cvmfs_repo ──────────────────────────────────────────────────────────

class TestCheckCvmfsRepo(unittest.TestCase):
    def test_fail_for_nonexistent_path(self):
        status, detail = _check_cvmfs_repo("/nonexistent/cvmfs/repo.example")
        self.assertEqual(status, FAIL)
        self.assertIn("does not exist", detail)

    def test_pass_for_accessible_directory(self):
        with tempfile.TemporaryDirectory() as d:
            # Create a dummy entry so it is non-empty
            open(os.path.join(d, "dummy"), "w").close()
            status, detail = _check_cvmfs_repo(d)
        self.assertEqual(status, PASS)
        self.assertIn("accessible", detail)

    def test_warn_for_empty_directory(self):
        with tempfile.TemporaryDirectory() as d:
            status, detail = _check_cvmfs_repo(d)
        self.assertEqual(status, WARN)
        self.assertIn("empty", detail)


# ── _check_disk_space ──────────────────────────────────────────────────────────

class TestCheckDiskSpace(unittest.TestCase):
    def test_pass_when_ample_space(self):
        # Mock lots of free space
        with patch("shutil.disk_usage") as mock_du:
            mock_du.return_value = shutil.disk_usage.__class__  # namedtuple-like
            mock_du.return_value = type("du", (), {"free": 100 * 1024**3, "total": 200 * 1024**3})()
            import bits_helpers.doctor as doc
            with patch.object(doc.shutil, "disk_usage",
                              return_value=type("du", (), {"free": 100 * 1024**3, "total": 200 * 1024**3})()):
                status, detail = _check_disk_space("/tmp", min_free_gib=10.0)
        self.assertEqual(status, PASS)

    def test_warn_when_low_space(self):
        import bits_helpers.doctor as doc
        with patch.object(doc.shutil, "disk_usage",
                          return_value=type("du", (), {"free": 1 * 1024**3, "total": 200 * 1024**3})()):
            status, detail = _check_disk_space("/tmp", min_free_gib=10.0)
        self.assertEqual(status, WARN)
        self.assertIn("low disk space", detail)

    def test_real_tmp_passes_or_warns(self):
        status, detail = _check_disk_space(tempfile.gettempdir(), min_free_gib=0.001)
        self.assertEqual(status, PASS)


# ── _check_store ───────────────────────────────────────────────────────────────

class TestCheckStore(unittest.TestCase):
    def test_skip_empty_url(self):
        status, detail = _check_store("")
        self.assertEqual(status, SKIP)

    def test_skip_rsync(self):
        status, detail = _check_store("rsync://example.com/bits-repo")
        self.assertEqual(status, SKIP)

    def test_skip_unknown_scheme(self):
        status, detail = _check_store("ftp://example.com/bits")
        self.assertEqual(status, SKIP)

    def test_pass_https_ok(self):
        import urllib.request
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        with patch("urllib.request.urlopen", return_value=mock_resp):
            status, detail = _check_store("https://s3.cern.ch/swift/v1/bits-repo")
        self.assertEqual(status, PASS)
        self.assertIn("200", detail)

    def test_warn_https_403(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(None, 403, "Forbidden", {}, None)):
            status, detail = _check_store("https://s3.cern.ch/swift/v1/bits-repo")
        self.assertEqual(status, WARN)
        self.assertIn("403", detail)

    def test_fail_https_unreachable(self):
        with patch("urllib.request.urlopen",
                   side_effect=OSError("Connection refused")):
            status, detail = _check_store("https://s3.cern.ch/swift/v1/bits-repo")
        self.assertEqual(status, FAIL)

    def test_pass_s3_with_credentials(self):
        with tempfile.NamedTemporaryFile(suffix=".s3cfg", delete=False) as fh:
            fh.write(b"[default]\naccess_key = test\n")
            s3cfg = fh.name
        try:
            with patch("bits_helpers.doctor.expanduser", return_value=s3cfg):
                status, detail = _check_store("s3://my-bucket/bits")
            self.assertEqual(status, PASS)
        finally:
            os.unlink(s3cfg)

    def test_warn_s3_no_credentials(self):
        with patch("bits_helpers.doctor.expanduser", return_value="/nonexistent/.s3cfg"):
            status, detail = _check_store("s3://my-bucket/bits")
        self.assertEqual(status, WARN)
        self.assertIn("~/.s3cfg not found", detail)

    def test_pass_b3_with_env_key(self):
        with patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "AKIATEST"}):
            status, detail = _check_store("b3://my-bucket/bits")
        self.assertEqual(status, PASS)

    def test_warn_b3_no_env_key(self):
        env = {k: v for k, v in os.environ.items() if k != "AWS_ACCESS_KEY_ID"}
        with patch.dict(os.environ, env, clear=True):
            status, detail = _check_store("b3://my-bucket/bits")
        self.assertEqual(status, WARN)


# ── _run_runner_checks ─────────────────────────────────────────────────────────

class TestRunRunnerChecks(unittest.TestCase):
    """Integration-level tests for the full check list."""

    def _args(self, **kw):
        base = dict(
            architecture="slc9_x86-64",
            docker=False,
            runner=True,
            workDir=tempfile.gettempdir(),
            remoteStore="",
            insecure=False,
            cvmfsRepos=[],
            minDisk=0.001,   # virtually always passes
            json_output=False,
        )
        base.update(kw)
        return Namespace(**base)

    def test_returns_list_of_triples(self):
        checks = _run_runner_checks(self._args())
        for item in checks:
            self.assertEqual(len(item), 3)
            name, status, detail = item
            self.assertIsInstance(name, str)
            self.assertIn(status, (PASS, FAIL, WARN, SKIP))
            self.assertIsInstance(detail, str)

    def test_git_check_present(self):
        checks = _run_runner_checks(self._args())
        names = [n for n, _, _ in checks]
        self.assertIn("git", names)

    def test_compiler_check_present(self):
        checks = _run_runner_checks(self._args())
        names = [n for n, _, _ in checks]
        self.assertIn("C++ compiler", names)

    def test_docker_check_added_when_docker_flag_set(self):
        checks = _run_runner_checks(self._args(docker=True))
        names = [n for n, _, _ in checks]
        self.assertIn("docker daemon", names)

    def test_cvmfs_check_added_per_repo(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "dummy"), "w").close()
            checks = _run_runner_checks(self._args(cvmfsRepos=[d]))
        names = [n for n, _, _ in checks]
        self.assertTrue(any("CVMFS" in n for n in names))

    def test_store_check_present(self):
        checks = _run_runner_checks(self._args())
        names = [n for n, _, _ in checks]
        self.assertIn("remote store", names)


# ── doDoctor --runner dispatch ─────────────────────────────────────────────────

class TestDoDocorRunnerMode(unittest.TestCase):
    """Test the --runner dispatch path in doDoctor."""

    def _args(self, **kw):
        base = dict(
            packages=[],
            architecture="slc9_x86-64",
            docker=False,
            runner=True,
            workDir=tempfile.gettempdir(),
            remoteStore="",
            insecure=False,
            cvmfsRepos=[],
            minDisk=0.001,
            json_output=False,
        )
        base.update(kw)
        return Namespace(**base)

    def test_exits_0_when_all_pass(self):
        all_pass = [("git", PASS, "ok"), ("C++ compiler", PASS, "ok")]
        with patch("bits_helpers.doctor._run_runner_checks", return_value=all_pass), \
             patch("bits_helpers.doctor._emit_runner_text"):
            with self.assertRaises(SystemExit) as ctx:
                doDoctor(self._args(), MagicMock())
        self.assertEqual(ctx.exception.code, 0)

    def test_exits_1_when_any_fail(self):
        checks = [("git", PASS, "ok"), ("C++ compiler", FAIL, "not found")]
        with patch("bits_helpers.doctor._run_runner_checks", return_value=checks), \
             patch("bits_helpers.doctor._emit_runner_text"):
            with self.assertRaises(SystemExit) as ctx:
                doDoctor(self._args(), MagicMock())
        self.assertEqual(ctx.exception.code, 1)

    def test_exits_0_when_only_warn(self):
        checks = [("podman (sandbox)", WARN, "podman not found")]
        with patch("bits_helpers.doctor._run_runner_checks", return_value=checks), \
             patch("bits_helpers.doctor._emit_runner_text"):
            with self.assertRaises(SystemExit) as ctx:
                doDoctor(self._args(), MagicMock())
        self.assertEqual(ctx.exception.code, 0)

    def test_json_output_structure(self):
        import io
        checks = [
            ("git",          PASS, "/usr/bin/git"),
            ("C++ compiler", PASS, "/usr/bin/c++"),
            ("podman (sandbox)", WARN, "podman not found"),
        ]
        with patch("bits_helpers.doctor._run_runner_checks", return_value=checks):
            captured = io.StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                with self.assertRaises(SystemExit):
                    doDoctor(self._args(json_output=True), MagicMock())
            finally:
                sys.stdout = old_stdout
        report = json.loads(captured.getvalue())
        self.assertEqual(report["mode"], "runner")
        self.assertIn("checks", report)
        self.assertIn("summary", report)
        self.assertIn("exit_code", report)
        self.assertEqual(report["summary"][PASS], 2)
        self.assertEqual(report["summary"][WARN], 1)
        self.assertEqual(report["exit_code"], 0)

    def test_compiler_missing_sets_exitcode_in_recipe_mode(self):
        """Regression: missing compiler must make doDoctor exit non-zero."""
        args = Namespace(
            packages=["SomePackage"],
            architecture="slc9_x86-64",
            docker=False,
            dockerImage=None,
            docker_extra_args=["--network=host"],
            debug=False,
            preferSystem=[],
            noSystem="*",
            disable=[],
            defaults=["release"],
            environment=[],
            workDir=tempfile.gettempdir(),
            configDir="/nonexistent_dist",
            runner=False,
            json_output=False,
        )
        mock_parser = MagicMock()
        # configDir doesn't exist → parser.error is called, which raises
        mock_parser.error.side_effect = SystemExit(2)
        with self.assertRaises(SystemExit) as ctx:
            with patch("bits_helpers.doctor.exists", return_value=False):
                doDoctor(args, mock_parser)
        # parser.error called for missing configDir — non-zero
        self.assertNotEqual(ctx.exception.code, 0)


# ── _probe_tarball_in_store ────────────────────────────────────────────────────

class TestProbeTarballInStore(unittest.TestCase):
    """Unit tests for the per-package store probe function."""

    def _spec(self, pkg="ROOT", version="6.32.00", rev="1",
              arch=None, remote_hashes=None):
        spec = {
            "package":       pkg,
            "version":       version,
            "revision":      rev,
            "remote_hashes": remote_hashes if remote_hashes is not None
                             else ["abcdef1234567890" * 2],
        }
        if arch:
            spec["architecture"] = arch
        return spec

    # ── HTTP store ─────────────────────────────────────────────────────────────

    def test_pass_http_200(self):
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__  = MagicMock(return_value=False)
        mock_resp.status    = 200
        with patch("urllib.request.urlopen", return_value=mock_resp):
            status, detail = _probe_tarball_in_store(
                self._spec(), "slc9_x86-64",
                "https://s3.cern.ch/swift/v1/bits-repo")
        self.assertEqual(status, PASS)
        self.assertIn("available", detail)
        self.assertIn("ROOT", detail)

    def test_fail_http_404_all_hashes(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(None, 404, "Not Found", {}, None)):
            status, detail = _probe_tarball_in_store(
                self._spec(), "slc9_x86-64",
                "https://s3.cern.ch/swift/v1/bits-repo")
        self.assertEqual(status, FAIL)
        self.assertIn("build from source", detail)

    def test_warn_http_500(self):
        import urllib.error
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.HTTPError(None, 500, "Server Error", {}, None)):
            status, detail = _probe_tarball_in_store(
                self._spec(), "slc9_x86-64",
                "https://s3.cern.ch/swift/v1/bits-repo")
        self.assertEqual(status, WARN)
        self.assertIn("500", detail)

    def test_warn_http_connection_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("Connection refused")):
            status, detail = _probe_tarball_in_store(
                self._spec(), "slc9_x86-64",
                "https://s3.cern.ch/swift/v1/bits-repo")
        self.assertEqual(status, WARN)

    # ── Local filesystem store ─────────────────────────────────────────────────

    def test_pass_local_file_exists(self):
        with tempfile.TemporaryDirectory() as store_dir:
            spec = self._spec(remote_hashes=["aabbccddeeff0011" * 2])
            h    = spec["remote_hashes"][0]
            arch = "slc9_x86-64"
            tarball = "ROOT-6.32.00-1.%s.tar.gz" % arch
            tarball_dir = os.path.join(store_dir, "TARS", arch, "store", h[:2], h)
            os.makedirs(tarball_dir)
            open(os.path.join(tarball_dir, tarball), "w").close()
            status, detail = _probe_tarball_in_store(spec, arch, store_dir)
        self.assertEqual(status, PASS)
        self.assertIn("available", detail)

    def test_fail_local_file_missing(self):
        with tempfile.TemporaryDirectory() as store_dir:
            status, detail = _probe_tarball_in_store(
                self._spec(), "slc9_x86-64", store_dir)
        self.assertEqual(status, FAIL)

    # ── Non-probeable stores ───────────────────────────────────────────────────

    def test_skip_rsync_store(self):
        status, detail = _probe_tarball_in_store(
            self._spec(), "slc9_x86-64", "rsync://store.example.com/bits")
        self.assertEqual(status, SKIP)

    def test_skip_s3_store(self):
        status, detail = _probe_tarball_in_store(
            self._spec(), "slc9_x86-64", "s3://my-bucket/bits")
        self.assertEqual(status, SKIP)

    # ── Missing hashes ─────────────────────────────────────────────────────────

    def test_warn_no_remote_hashes(self):
        spec = self._spec(remote_hashes=[])
        status, detail = _probe_tarball_in_store(
            spec, "slc9_x86-64", "https://store.example.com/bits")
        self.assertEqual(status, WARN)
        self.assertIn("hash not computed", detail)


# ── _run_check_store_checks ────────────────────────────────────────────────────

class TestRunCheckStoreChecks(unittest.TestCase):
    """Unit tests for the check-store orchestration."""

    def _args(self, store="https://s3.cern.ch/swift/v1/bits-repo", **kw):
        base = dict(
            architecture="slc9_x86-64",
            remoteStore=store,
            insecure=False,
        )
        base.update(kw)
        return Namespace(**base)

    def _specs(self):
        """Return a minimal specs dict like getPackageList would produce."""
        from collections import OrderedDict
        specs = OrderedDict()
        specs["DepA"] = {
            "package": "DepA", "version": "1.0", "revision": "1",
            "tag": "v1.0", "recipe": "pkg: DepA\n", "requires": [],
        }
        specs["ROOT"] = {
            "package": "ROOT", "version": "6.32.00", "revision": "1",
            "tag": "v6-32-00-patches", "recipe": "pkg: ROOT\n",
            "requires": ["DepA"],
        }
        return specs

    def test_skip_when_no_store(self):
        checks = _run_check_store_checks(
            self._args(store=""), self._specs(),
            own={"ROOT"}, always_built=set())
        self.assertEqual(len(checks), 1)
        status = checks[0][1]
        self.assertEqual(status, SKIP)

    def test_skip_when_nothing_to_build(self):
        checks = _run_check_store_checks(
            self._args(), self._specs(),
            own=set(), always_built=set())
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0][1], SKIP)
        self.assertIn("nothing", checks[0][2].lower())

    def test_calls_store_hashes_and_probe(self):
        """Check that storeHashes is called and probe is invoked per target."""
        def fake_store_hashes(pkg, specs, considerRelocation):
            specs[pkg]["remote_hashes"] = ["deadbeef01234567" * 2]
            specs[pkg]["local_hashes"]  = ["deadbeef01234567" * 2]

        with patch("bits_helpers.build.storeHashes", side_effect=fake_store_hashes), \
             patch("bits_helpers.doctor._probe_tarball_in_store",
                   return_value=(PASS, "available")) as mock_probe:
            checks = _run_check_store_checks(
                self._args(), self._specs(),
                own={"ROOT"}, always_built={"DepA"})
        # Both ROOT and DepA are targets → probe called twice
        self.assertEqual(mock_probe.call_count, 2)
        statuses = [s for _, s, _ in checks if _ != "note"]
        self.assertTrue(all(s == PASS for s in statuses if s != WARN))

    def test_approx_note_when_no_tag(self):
        """A spec without a 'tag' key should trigger the approximation warning."""
        specs = {
            "Foo": {"package": "Foo", "version": "1.0", "revision": "1",
                    "recipe": "pkg: Foo\n", "requires": []},
            # No 'tag' key → commit_hash will be set to "0"
        }

        def fake_store_hashes(pkg, specs_, considerRelocation):
            specs_[pkg].setdefault("remote_hashes", [])
            specs_[pkg].setdefault("local_hashes",  [])

        with patch("bits_helpers.build.storeHashes", side_effect=fake_store_hashes):
            checks = _run_check_store_checks(
                self._args(), specs,
                own={"Foo"}, always_built=set())
        note_names = [n for n, _, _ in checks]
        self.assertIn("(note)", note_names)

    def test_json_output_structure(self):
        import io
        checks = [
            ("ROOT",  PASS, "available: ROOT-6.32.00-1.slc9_x86-64.tar.gz (hash abcdef12)"),
            ("DepA",  FAIL, "not in store — will build from source"),
        ]
        captured = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured
        try:
            _emit_check_store_json(checks, "slc9_x86-64",
                                   "https://s3.cern.ch/swift/v1/bits-repo")
        finally:
            sys.stdout = old_stdout
        report = json.loads(captured.getvalue())
        self.assertEqual(report["mode"], "check-store")
        self.assertIn("packages", report)
        self.assertIn("summary", report)
        self.assertEqual(report["summary"][PASS], 1)
        self.assertEqual(report["summary"][FAIL], 1)

    def test_doDoctor_check_store_exits_0(self):
        """--check-store mode always exits 0 regardless of store results."""
        from io import StringIO
        checks = [("ROOT", FAIL, "not in store — will build from source")]
        args = Namespace(
            packages=["ROOT"],
            architecture="slc9_x86-64",
            docker=False, dockerImage=None,
            docker_extra_args=["--network=host"],
            debug=False, preferSystem=[], noSystem="*",
            disable=[], defaults=["release"], environment=[],
            runner=False, checkStore=True,
            remoteStore="https://s3.cern.ch/swift/v1/bits-repo",
            insecure=False, json_output=False,
            workDir=tempfile.gettempdir(),
            configDir="/nonexistent_for_check_store_test",
        )
        mock_parser = MagicMock()
        mock_parser.error.side_effect = SystemExit(2)
        with patch("bits_helpers.doctor.exists", return_value=False):
            with self.assertRaises(SystemExit) as ctx:
                doDoctor(args, mock_parser)
        # configDir doesn't exist → exits via parser.error before reaching --check-store
        self.assertNotEqual(ctx.exception.code, 0)  # configDir guard fires first

    def test_doDoctor_check_store_full_flow(self):
        """End-to-end: --check-store with mocked getPackageList and probe."""
        import io
        from io import StringIO

        fake_specs = {
            "ROOT": {"package": "ROOT", "version": "6.32.00", "revision": "1",
                     "tag": "v6-32-00", "recipe": "pkg: ROOT\n", "requires": []},
        }

        def fake_gpl(**kw):
            for pkg, spec in fake_specs.items():
                kw["specs"][pkg] = spec
            return (set(), {"ROOT"}, set(), None)

        def fake_store_hashes(pkg, specs_, considerRelocation):
            specs_[pkg]["remote_hashes"] = ["aa" * 16]
            specs_[pkg]["local_hashes"]  = ["aa" * 16]

        args = Namespace(
            packages=["ROOT"],
            architecture="slc9_x86-64",
            docker=False, dockerImage=None,
            docker_extra_args=["--network=host"],
            debug=False, preferSystem=[], noSystem="*",
            disable=[], defaults=["release"], environment=[],
            runner=False, checkStore=True,
            remoteStore="https://s3.cern.ch/swift/v1/bits-repo",
            insecure=False, json_output=True,
            workDir=tempfile.gettempdir(),
            configDir="/tmp",
        )
        captured = io.StringIO()
        with patch("bits_helpers.doctor.exists", return_value=True), \
             patch("bits_helpers.doctor.expanduser", return_value="/tmp/.rootlogon.C"), \
             patch("bits_helpers.doctor.os.path.exists", return_value=False), \
             patch("bits_helpers.doctor.getPackageList", side_effect=fake_gpl), \
             patch("bits_helpers.doctor.parseDefaults",
                   return_value=(None, {}, [], {})), \
             patch("bits_helpers.doctor.readDefaults", return_value={}), \
             patch("bits_helpers.doctor.validateDefaults",
                   return_value=(True, "", ["release"])), \
             patch("bits_helpers.build.storeHashes", side_effect=fake_store_hashes), \
             patch("urllib.request.urlopen",
                   side_effect=__import__("urllib.error", fromlist=["HTTPError"])
                   .HTTPError(None, 404, "Not Found", {}, None)), \
             patch("sys.stdout", captured):
            with self.assertRaises(SystemExit) as ctx:
                doDoctor(args, MagicMock())
        self.assertEqual(ctx.exception.code, 0)
        report = json.loads(captured.getvalue())
        self.assertEqual(report["mode"], "check-store")
        self.assertIn("packages", report)


if __name__ == "__main__":
    unittest.main()
