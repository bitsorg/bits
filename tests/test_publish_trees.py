# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the per-tree cvmfs-prepub publish primitive (bits_helpers/publish)."""

import os
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers.publish import _PrepubConfig, _publish_tree, _resolve_repo_subpath


class _Parser:
    class _Err(Exception):
        pass

    def error(self, msg):
        raise self._Err(msg)


def _cfg():
    return _PrepubConfig(url="https://prepub", token="tok", webhook=None,
                         no_verify_tls=False, poll_interval=1, timeout=10)


class TestResolveRepoSubpath(unittest.TestCase):

    def test_explicit_repo_and_path(self):
        self.assertEqual(
            _resolve_repo_subpath("sft.cern.ch", "/lcg/releases/x", None, _Parser()),
            ("sft.cern.ch", "lcg/releases/x"))

    def test_derived_from_cvmfs_target(self):
        self.assertEqual(
            _resolve_repo_subpath(None, None,
                                  "/cvmfs/sft.cern.ch/lcg/releases/ROOT/v6", _Parser()),
            ("sft.cern.ch", "lcg/releases/ROOT/v6"))

    def test_half_specified_is_error(self):
        with self.assertRaises(_Parser._Err):
            _resolve_repo_subpath("repo", None, None, _Parser())


class TestPublishTree(unittest.TestCase):

    def test_tars_subtree_and_submits_to_its_path(self):
        captured = {}

        def fake_submit(prepub_url, token, repo, path, tar_path, webhook_url,
                        no_verify_tls, bearer_auth=False):
            # the tar really contains the tree's files
            with tarfile.open(tar_path) as tf:
                captured["names"] = sorted(n.lstrip("./") for n in tf.getnames()
                                           if n not in (".", "./"))
            captured["repo"] = repo
            captured["path"] = path
            captured["url"] = prepub_url
            return "job-1"

        polled = {}

        def fake_poll(prepub_url, token, job_id, poll_interval, timeout, no_verify_tls,
                      bearer_auth=False):
            polled["job"] = job_id

        with tempfile.TemporaryDirectory() as d:
            tree = os.path.join(d, "tree")
            os.makedirs(os.path.join(tree, "bin"))
            open(os.path.join(tree, "bin", "tool"), "w").close()
            with patch("bits_helpers.prepub.submit_job", new=fake_submit), \
                 patch("bits_helpers.prepub.poll_job", new=fake_poll):
                _publish_tree(tree, "myrepo", "modules/ROOT/v6", _cfg(), "modulefiles")

        self.assertIn("bin/tool", captured["names"])
        self.assertEqual(captured["repo"], "myrepo")
        self.assertEqual(captured["path"], "modules/ROOT/v6")   # its OWN path
        self.assertEqual(polled["job"], "job-1")

    def test_tar_is_cleaned_up_even_on_submit_error(self):
        seen = {}

        def boom(**kw):
            seen["tar"] = kw["tar_path"]
            raise RuntimeError("submit failed")

        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "t"))
            with patch("bits_helpers.prepub.submit_job", new=boom), \
                 patch("bits_helpers.prepub.poll_job", new=lambda **k: None):
                with self.assertRaises(RuntimeError):
                    _publish_tree(os.path.join(d, "t"), "r", "p", _cfg(), "x")
        self.assertFalse(os.path.exists(seen["tar"]))   # temp tar removed


if __name__ == "__main__":
    unittest.main()
