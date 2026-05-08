"""Tests for the provider-repository staleness check.

Verifies that clone_or_update_provider() always refreshes the upstream
mirror when a cached checkout already exists, so that bits detects new
versions of a provider on every run — even when fetch_repos=False.
"""

import os
import shutil
import tempfile
import unittest
from collections import OrderedDict
from unittest.mock import MagicMock, call, patch

from bits_helpers.repo_provider import (
    _provider_cache_root,
    clone_or_update_provider,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _provider_spec(pkg="my-provider", tag="v1"):
    return OrderedDict({
        "package": pkg,
        "version": tag,
        "source": "https://github.com/test/%s.git" % pkg,
        "tag": tag,
        "provides_repository": True,
        "repository_position": "append",
    })


def _mock_scm(commit="abcdef1234567890"):
    scm = MagicMock()
    scm.listRefsCmd.return_value = ["ls-remote", "origin"]
    scm.parseRefs.return_value = {"refs/tags/v1": commit}
    scm.cloneSourceCmd.return_value = ["git", "clone", "url", "dest"]
    scm.checkoutCmd.return_value = ["git", "checkout", "v1"]
    scm.exec.return_value = (0, "")
    return scm


class TestProviderStalenessCheck(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work_dir = os.path.join(self.tmp, "sw")
        self.ref_dir = os.path.join(self.tmp, "mirror")
        os.makedirs(self.work_dir)
        os.makedirs(self.ref_dir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _pre_populate_cache(self, pkg, commit):
        """Create a cached checkout so the provider appears to have run before."""
        cache_root = _provider_cache_root(self.work_dir, pkg)
        short = commit[:10]
        checkout = os.path.join(cache_root, short)
        os.makedirs(checkout, exist_ok=True)
        with open(os.path.join(checkout, ".bits_provider_ok"), "w") as fh:
            fh.write(commit + "\n")
        # Create the 'latest' symlink that signals a prior run
        latest = os.path.join(cache_root, "latest")
        if os.path.islink(latest):
            os.unlink(latest)
        os.symlink(short, latest)
        return checkout

    # ── Core staleness-check behaviour ────────────────────────────────────

    @patch("bits_helpers.repo_provider.updateReferenceRepoSpec")
    @patch("bits_helpers.repo_provider.logged_scm")
    @patch("bits_helpers.repo_provider.Git")
    def test_mirror_always_refreshed_when_cache_exists(
            self, MockGit, mock_logged_scm, mock_update_ref):
        """When a 'latest' symlink exists, fetch=True regardless of fetch_repos."""
        commit = "abcdef1234567890"
        scm = _mock_scm(commit)
        MockGit.return_value = scm
        mock_logged_scm.return_value = "%s\trefs/tags/v1" % commit
        scm.parseRefs.return_value = {"refs/tags/v1": commit}

        spec = _provider_spec()
        self._pre_populate_cache(spec["package"], commit)

        # Deliberately pass fetch_repos=False — the mirror must still be updated
        clone_or_update_provider(spec, self.work_dir, self.ref_dir,
                                 fetch_repos=False)

        # updateReferenceRepoSpec must have been called with fetch=True
        mock_update_ref.assert_called_once()
        _, kwargs = mock_update_ref.call_args
        self.assertTrue(
            kwargs.get("fetch", False),
            "updateReferenceRepoSpec was NOT called with fetch=True despite "
            "an existing cached checkout",
        )

    @patch("bits_helpers.repo_provider.updateReferenceRepoSpec")
    @patch("bits_helpers.repo_provider.logged_scm")
    @patch("bits_helpers.repo_provider.Git")
    def test_no_cache_respects_fetch_repos_false(
            self, MockGit, mock_logged_scm, mock_update_ref):
        """On the first run (no cache) fetch_repos=False is respected."""
        commit = "abcdef1234567890"
        scm = _mock_scm(commit)
        MockGit.return_value = scm
        mock_logged_scm.return_value = "%s\trefs/tags/v1" % commit
        scm.parseRefs.return_value = {"refs/tags/v1": commit}

        # No pre-populated cache — 'latest' symlink does not exist
        spec = _provider_spec()
        clone_or_update_provider(spec, self.work_dir, self.ref_dir,
                                 fetch_repos=False)

        mock_update_ref.assert_called_once()
        _, kwargs = mock_update_ref.call_args
        self.assertFalse(
            kwargs.get("fetch", True),
            "updateReferenceRepoSpec should NOT fetch when fetch_repos=False "
            "and no cached checkout exists",
        )

    @patch("bits_helpers.repo_provider.updateReferenceRepoSpec")
    @patch("bits_helpers.repo_provider.logged_scm")
    @patch("bits_helpers.repo_provider.Git")
    def test_no_cache_with_fetch_repos_true_fetches(
            self, MockGit, mock_logged_scm, mock_update_ref):
        """fetch_repos=True always fetches, cache-or-not."""
        commit = "abcdef1234567890"
        scm = _mock_scm(commit)
        MockGit.return_value = scm
        mock_logged_scm.return_value = "%s\trefs/tags/v1" % commit
        scm.parseRefs.return_value = {"refs/tags/v1": commit}

        spec = _provider_spec()
        clone_or_update_provider(spec, self.work_dir, self.ref_dir,
                                 fetch_repos=True)

        mock_update_ref.assert_called_once()
        _, kwargs = mock_update_ref.call_args
        self.assertTrue(kwargs.get("fetch", False))

    # ── New-version detection ──────────────────────────────────────────────

    @patch("bits_helpers.repo_provider.updateReferenceRepoSpec")
    @patch("bits_helpers.repo_provider.logged_scm")
    @patch("bits_helpers.repo_provider.Git")
    def test_upstream_update_triggers_new_clone(
            self, MockGit, mock_logged_scm, mock_update_ref):
        """If the upstream tag moved to a new commit, a fresh clone is performed."""
        old_commit = "aaaaaaaaaa000000"
        new_commit = "bbbbbbbbbb111111"

        scm = _mock_scm(new_commit)
        MockGit.return_value = scm
        mock_logged_scm.return_value = "%s\trefs/tags/v1" % new_commit
        scm.parseRefs.return_value = {"refs/tags/v1": new_commit}

        spec = _provider_spec()
        # Cache has the OLD commit; upstream now reports the NEW commit
        self._pre_populate_cache(spec["package"], old_commit)

        checkout_dir, got_hash = clone_or_update_provider(
            spec, self.work_dir, self.ref_dir, fetch_repos=False)

        # A fresh clone must have been executed
        scm.exec.assert_any_call(
            scm.cloneSourceCmd.return_value,
            directory=".", check=False,
        )
        # The returned hash must be the new one
        self.assertEqual(got_hash, new_commit)
        # The marker for the new hash must exist
        marker = os.path.join(checkout_dir, ".bits_provider_ok")
        self.assertTrue(os.path.exists(marker))
        with open(marker) as fh:
            self.assertEqual(fh.read().strip(), new_commit)

    @patch("bits_helpers.repo_provider.updateReferenceRepoSpec")
    @patch("bits_helpers.repo_provider.logged_scm")
    @patch("bits_helpers.repo_provider.Git")
    def test_cache_hit_still_skips_clone_when_hash_unchanged(
            self, MockGit, mock_logged_scm, mock_update_ref):
        """When the upstream hash hasn't changed, no clone is performed."""
        commit = "abcdef1234567890"
        scm = _mock_scm(commit)
        MockGit.return_value = scm
        mock_logged_scm.return_value = "%s\trefs/tags/v1" % commit
        scm.parseRefs.return_value = {"refs/tags/v1": commit}

        spec = _provider_spec()
        self._pre_populate_cache(spec["package"], commit)

        checkout_dir, got_hash = clone_or_update_provider(
            spec, self.work_dir, self.ref_dir, fetch_repos=False)

        # No clone must have been attempted
        for c in scm.exec.call_args_list:
            args = c[0][0] if c[0] else []
            self.assertNotIn("clone", args,
                             "Git clone was called despite cache hit (unchanged hash)")
        self.assertEqual(got_hash, commit)


if __name__ == "__main__":
    unittest.main()
