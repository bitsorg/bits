# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

from os import getcwd
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import call, patch, MagicMock  # In Python 3, mock is built-in
from collections import OrderedDict

from bits_helpers.workarea import updateReferenceRepoSpec, _apply_patches, _extract_source_archives
from bits_helpers.git import Git


class ExtractSourceArchivesTest(unittest.TestCase):
    """A corrupt/wrong-format source archive must fail cleanly (SystemExit via
    dieOnError), not crash the whole run with a CalledProcessError traceback."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_corrupt_tarball_fails_cleanly(self):
        # A file named like a tarball but containing non-archive bytes (e.g. a
        # 404 HTML page saved as the tarball).
        bogus = os.path.join(self.tmp, "pkg-1.0.tar.gz")
        with open(bogus, "w") as fh:
            fh.write("<html>404 Not Found</html>\n")
        with self.assertRaises(SystemExit):
            _extract_source_archives(self.tmp)


MOCK_SPEC = OrderedDict((
    ("package", "AliRoot"),
    ("source", "https://github.com/alisw/AliRoot"),
    ("scm", Git()),
    ("is_devel_pkg", False),
))


@patch("bits_helpers.workarea.debug", new=MagicMock())
@patch("bits_helpers.git.clone_speedup_options",
       new=MagicMock(return_value=["--filter=blob:none"]))
class WorkareaTestCase(unittest.TestCase):

    @patch("os.path.exists")
    @patch("os.makedirs")
    @patch("bits_helpers.git")
    @patch("bits_helpers.workarea.is_writeable", new=MagicMock(return_value=False))
    def test_reference_sources_reused(self, mock_git, mock_makedirs, mock_exists):
        """Check mirrors are reused when pre-existing, but not writable.

        In this case, make sure nothing is fetched, even when requested.
        """
        mock_exists.return_value = True
        spec = MOCK_SPEC.copy()
        updateReferenceRepoSpec(referenceSources="sw/MIRROR", p="AliRoot",
                                spec=spec, fetch=True)
        mock_exists.assert_called_with("%s/sw/MIRROR/aliroot" % getcwd())
        mock_makedirs.assert_called_with("%s/sw/MIRROR" % getcwd(), exist_ok=True)
        mock_git.assert_not_called()
        self.assertEqual(spec.get("reference"), "%s/sw/MIRROR/aliroot" % getcwd())

    @patch("os.path.exists")
    @patch("os.makedirs")
    @patch("codecs.open")
    @patch("bits_helpers.git.git")
    @patch("bits_helpers.workarea.is_writeable", new=MagicMock(return_value=True))
    def test_reference_sources_updated(self, mock_git, mock_open, mock_makedirs, mock_exists):
        """Check mirrors are updated when possible and git output is logged."""
        mock_exists.return_value = True
        mock_git.return_value = 0, "sentinel output"
        mock_open.return_value = MagicMock(
            __enter__=lambda *args, **kw: MagicMock(
                write=lambda output: self.assertEqual(output, "sentinel output")))
        spec = MOCK_SPEC.copy()
        updateReferenceRepoSpec(referenceSources="sw/MIRROR", p="AliRoot",
                                spec=spec, fetch=True)
        mock_exists.assert_called_with("%s/sw/MIRROR/aliroot" % getcwd())
        mock_exists.assert_has_calls([])
        mock_makedirs.assert_called_with("%s/sw/MIRROR" % getcwd(), exist_ok=True)
        mock_git.assert_called_once_with([
            "fetch", "-f", "--prune", "--filter=blob:none", spec["source"], "+refs/tags/*:refs/tags/*", "+refs/heads/*:refs/heads/*",
        ], directory="%s/sw/MIRROR/aliroot" % getcwd(), check=False, prompt=True)
        self.assertEqual(spec.get("reference"), "%s/sw/MIRROR/aliroot" % getcwd())

    @patch("os.path.exists")
    @patch("os.makedirs")
    @patch("codecs.open")
    @patch("bits_helpers.git.git")
    @patch("bits_helpers.workarea.is_writeable", new=MagicMock(return_value=True))
    def test_reference_sources_updated_custom_refspec(self, mock_git, mock_open, mock_makedirs, mock_exists):
        """Check mirrors are updated with custom refspec when provided."""
        mock_exists.return_value = True
        mock_git.return_value = 0, "sentinel output"
        mock_open.return_value = MagicMock(
            __enter__=lambda *args, **kw: MagicMock(
                write=lambda output: self.assertEqual(output, "sentinel output")))
        spec = MOCK_SPEC.copy()
        spec["ref_match_rule"] = ["+refs/heads/master:refs/heads/master"]
        updateReferenceRepoSpec(referenceSources="sw/MIRROR", p="AliRoot",
                                spec=spec, fetch=True)
        mock_exists.assert_called_with("%s/sw/MIRROR/aliroot" % getcwd())
        mock_makedirs.assert_called_with("%s/sw/MIRROR" % getcwd(), exist_ok=True)
        mock_git.assert_called_once_with([
            "fetch", "-f", "--prune", "--filter=blob:none", spec["source"], "+refs/heads/master:refs/heads/master",
        ], directory="%s/sw/MIRROR/aliroot" % getcwd(), check=False, prompt=True)
        self.assertEqual(spec.get("reference"), "%s/sw/MIRROR/aliroot" % getcwd())

    @patch("os.path.exists")
    @patch("os.makedirs")
    @patch("bits_helpers.git")
    @patch("bits_helpers.workarea.is_writeable", new=MagicMock(return_value=False))
    def test_reference_sources_not_writable(self, mock_git, mock_makedirs, mock_exists):
        """Check nothing is fetched when mirror directory isn't writable."""
        mock_exists.side_effect = lambda path: not path.endswith("/aliroot")
        spec = MOCK_SPEC.copy()
        updateReferenceRepoSpec(referenceSources="sw/MIRROR", p="AliRoot",
                                spec=spec, fetch=True)
        mock_exists.assert_called_with("%s/sw/MIRROR/aliroot" % getcwd())
        mock_makedirs.assert_called_with("%s/sw/MIRROR" % getcwd(), exist_ok=True)
        mock_git.assert_not_called()
        self.assertNotIn("reference", spec,
                         "should delete spec['reference'], as no mirror exists")

    @patch("os.path.exists")
    @patch("os.makedirs")
    @patch("bits_helpers.git.git")
    @patch("bits_helpers.workarea.is_writeable", new=MagicMock(return_value=True))
    def test_reference_sources_created(self, mock_git, mock_makedirs, mock_exists):
        """Check the mirror directory is created when possible."""
        mock_git.return_value = 0, ""
        mock_exists.side_effect = lambda path: not path.endswith("/aliroot")
        spec = MOCK_SPEC.copy()
        updateReferenceRepoSpec(referenceSources="sw/MIRROR", p="AliRoot",
                                spec=spec, fetch=True)
        mock_exists.assert_called_with("%s/sw/MIRROR/aliroot" % getcwd())
        mock_makedirs.assert_called_with("%s/sw/MIRROR" % getcwd(), exist_ok=True)
        mock_git.assert_called_once_with([
            "clone", "--bare", spec["source"],
            "%s/sw/MIRROR/aliroot" % getcwd(), "--filter=blob:none",
        ], directory=".", check=False, prompt=True)
        self.assertEqual(spec.get("reference"), "%s/sw/MIRROR/aliroot" % getcwd())


@patch("bits_helpers.workarea.debug", new=MagicMock())
class ApplyPatchesTest(unittest.TestCase):
    """Tests for _apply_patches() — automatic patch application in checkout_sources."""

    def setUp(self):
        self.source_dir = tempfile.mkdtemp()
        # Place dummy patch files in the source dir (as bits does via shutil.copyfile)
        for name in ("fix-a.patch", "fix-b.patch"):
            open(os.path.join(self.source_dir, name), "w").close()

    def tearDown(self):
        shutil.rmtree(self.source_dir, ignore_errors=True)

    def _spec(self, patches=None, auto_patch=None):
        """Build a minimal spec dict."""
        s = OrderedDict()
        if patches is not None:
            s["patches"] = patches
        if auto_patch is not None:
            s["auto_patch"] = auto_patch
        return s

    # ------------------------------------------------------------------
    # No-op cases
    # ------------------------------------------------------------------

    @staticmethod
    def _patch_cmd(call_obj):
        """Return the command list (first positional arg) of a subprocess.run call."""
        return call_obj[0][0]

    @patch("subprocess.run")
    def test_no_patches_key_is_noop(self, mock_run):
        """spec without 'patches' key must not invoke patch(1) at all."""
        _apply_patches(self._spec(), self.source_dir)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_empty_patches_list_is_noop(self, mock_run):
        """spec with patches:[] must not invoke patch(1) at all."""
        _apply_patches(self._spec(patches=[]), self.source_dir)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_sentinel_skips_reapplication(self, mock_run):
        """If .bits_patched already exists, patches must not be reapplied."""
        open(os.path.join(self.source_dir, ".bits_patched"), "w").close()
        _apply_patches(self._spec(patches=["fix-a.patch"]), self.source_dir)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_auto_patch_false_skips_application(self, mock_run):
        """auto_patch=False must not invoke patch(1) (recipe applies its own)."""
        _apply_patches(self._spec(patches=["fix-a.patch", "fix-b.patch"], auto_patch=False),
                       self.source_dir)
        mock_run.assert_not_called()

    @patch("subprocess.run")
    def test_auto_patch_false_writes_no_sentinel(self, mock_run):
        """When auto-patching is off, the .bits_patched sentinel must NOT be written
        (the recipe owns idempotency)."""
        _apply_patches(self._spec(patches=["fix-a.patch"], auto_patch=False), self.source_dir)
        self.assertFalse(os.path.exists(os.path.join(self.source_dir, ".bits_patched")))

    @patch("subprocess.run")
    def test_auto_patch_true_default_still_applies(self, mock_run):
        """auto_patch=True (and the absent-key default) must apply patches as before."""
        _apply_patches(self._spec(patches=["fix-a.patch"], auto_patch=True), self.source_dir)
        mock_run.assert_called_once()

    # ------------------------------------------------------------------
    # Happy-path: correct invocations and sentinel creation
    # ------------------------------------------------------------------

    @patch("subprocess.run")
    def test_single_patch_invocation(self, mock_run):
        """A single patch must be applied with patch -p1 --batch --input <path> in source_dir."""
        _apply_patches(self._spec(patches=["fix-a.patch"]), self.source_dir)
        expected_patch_path = os.path.join(self.source_dir, "fix-a.patch")
        mock_run.assert_called_once()
        self.assertEqual(self._patch_cmd(mock_run.call_args),
                         ["patch", "-p1", "--batch", "--input", expected_patch_path])
        self.assertEqual(mock_run.call_args[1].get("cwd"), self.source_dir)

    @patch("subprocess.run")
    def test_multiple_patches_applied_in_order(self, mock_run):
        """Multiple patches must be applied in declaration order."""
        _apply_patches(
            self._spec(patches=["fix-a.patch", "fix-b.patch"]),
            self.source_dir,
        )
        expected_a = os.path.join(self.source_dir, "fix-a.patch")
        expected_b = os.path.join(self.source_dir, "fix-b.patch")
        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(self._patch_cmd(mock_run.call_args_list[0]),
                         ["patch", "-p1", "--batch", "--input", expected_a])
        self.assertEqual(self._patch_cmd(mock_run.call_args_list[1]),
                         ["patch", "-p1", "--batch", "--input", expected_b])
        self.assertEqual(mock_run.call_args_list[0][1].get("cwd"), self.source_dir)
        self.assertEqual(mock_run.call_args_list[1][1].get("cwd"), self.source_dir)

    @patch("subprocess.run")
    def test_sentinel_written_on_success(self, mock_run):
        """After successful application .bits_patched sentinel must be created."""
        sentinel = os.path.join(self.source_dir, ".bits_patched")
        self.assertFalse(os.path.exists(sentinel))
        _apply_patches(self._spec(patches=["fix-a.patch"]), self.source_dir)
        self.assertTrue(os.path.exists(sentinel),
                        ".bits_patched sentinel must exist after successful patch application")

    @patch("subprocess.run")
    def test_inline_checksum_suffix_stripped(self, mock_run):
        """Patch entries may carry a ',algo:digest' suffix; only the filename is used."""
        _apply_patches(
            self._spec(patches=["fix-a.patch,sha256:deadbeef"]),
            self.source_dir,
        )
        expected_path = os.path.join(self.source_dir, "fix-a.patch")
        mock_run.assert_called_once()
        self.assertEqual(self._patch_cmd(mock_run.call_args),
                         ["patch", "-p1", "--batch", "--input", expected_path])
        self.assertEqual(mock_run.call_args[1].get("cwd"), self.source_dir)

    # ------------------------------------------------------------------
    # Failure path: no sentinel on error
    # ------------------------------------------------------------------

    @patch("bits_helpers.workarea.dieOnError")
    @patch("subprocess.run",
           side_effect=subprocess.CalledProcessError(1, "patch"))
    def test_patch_failure_calls_dieOnError(self, mock_run, mock_die):
        """A failing patch(1) call must invoke dieOnError with a descriptive message."""
        _apply_patches(self._spec(patches=["fix-a.patch"]), self.source_dir)
        mock_die.assert_called_once()
        args = mock_die.call_args[0]
        self.assertTrue(args[0], "dieOnError must be called with truthy error flag")
        self.assertIn("fix-a.patch", args[1], "error message must name the failing patch file")

    @patch("bits_helpers.workarea.dieOnError")
    @patch("subprocess.run",
           side_effect=subprocess.CalledProcessError(1, "patch"))
    def test_no_sentinel_on_failure(self, mock_run, mock_die):
        """If patch(1) fails, .bits_patched must NOT be created."""
        sentinel = os.path.join(self.source_dir, ".bits_patched")
        _apply_patches(self._spec(patches=["fix-a.patch"]), self.source_dir)
        self.assertFalse(os.path.exists(sentinel),
                         ".bits_patched must not exist after a failed patch application")


if __name__ == '__main__':
    unittest.main()
