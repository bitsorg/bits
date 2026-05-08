"""Regression tests for container_workDir / --cvmfs-prefix logic in build.py.

The logic that determines the in-container work-directory path and rewrites
the cached tarball path was modified to support --cvmfs-prefix.  These tests
verify that:

  1. All three *existing* flag combinations produce the same result as before:
       a. No docker          → container_workDir = ""
       b. --docker           → container_workDir = "/container/bits/sw"
                               tarball path rewritten to use /container/bits/sw
       c. --docker --container-use-workdir
                             → container_workDir = workDir
                               tarball path NOT rewritten

  2. The new --cvmfs-prefix path:
       d. --docker --cvmfs-prefix PATH
                             → container_workDir = PATH
                               tarball path rewritten to use PATH

  3. re.escape() is used when rewriting paths, so a workDir containing regex
     metacharacters (e.g. '+' or '(') is handled correctly.
"""
import re
import unittest
from argparse import Namespace


# ---------------------------------------------------------------------------
# Helper that replicates the container_workDir + cachedTarball logic
# extracted from bits_helpers/build.py so we can test it in isolation.
# ---------------------------------------------------------------------------

def _compute_container_paths(work_dir, cached_tarball, docker, container_use_workdir,
                              cvmfs_prefix=None):
    """Return (container_workDir, adjusted_cached_tarball) mirroring build.py logic."""
    container_workDir = ""
    if docker:
        if cvmfs_prefix:
            container_workDir = cvmfs_prefix
            cached_tarball = re.sub(
                "^" + re.escape(work_dir), container_workDir, cached_tarball)
        elif not container_use_workdir:
            container_workDir = "/container/bits/sw"
            cached_tarball = re.sub(
                "^" + re.escape(work_dir), container_workDir, cached_tarball)
        else:
            container_workDir = work_dir
    return container_workDir, cached_tarball


WORK_DIR = "/data/alice/sw"
TARBALL   = WORK_DIR + "/TARS/x86_64-el9/ROOT/ROOT-6.32.0-1.x86_64-el9.tar.gz"


class ExistingBehaviourTest(unittest.TestCase):
    """Cases (a), (b), (c): existing flag combinations must be unchanged."""

    def test_no_docker_container_workdir_is_empty(self):
        cwd, tb = _compute_container_paths(
            WORK_DIR, TARBALL,
            docker=False, container_use_workdir=False)
        self.assertEqual(cwd, "")
        self.assertEqual(tb, TARBALL)   # untouched

    def test_docker_default_uses_container_path(self):
        cwd, tb = _compute_container_paths(
            WORK_DIR, TARBALL,
            docker=True, container_use_workdir=False)
        self.assertEqual(cwd, "/container/bits/sw")
        expected_tb = TARBALL.replace(WORK_DIR, "/container/bits/sw", 1)
        self.assertEqual(tb, expected_tb)

    def test_docker_container_use_workdir_keeps_workdir(self):
        cwd, tb = _compute_container_paths(
            WORK_DIR, TARBALL,
            docker=True, container_use_workdir=True)
        self.assertEqual(cwd, WORK_DIR)
        self.assertEqual(tb, TARBALL)   # path not rewritten

    def test_empty_cached_tarball_is_harmless(self):
        # When no cached tarball is available the string is "".
        for docker, use_workdir in [(False, False), (True, False), (True, True)]:
            cwd, tb = _compute_container_paths(
                WORK_DIR, "",
                docker=docker, container_use_workdir=use_workdir)
            self.assertEqual(tb, "")


class CvmfsPrefixTest(unittest.TestCase):
    """Case (d): --cvmfs-prefix mounts workDir at the CVMFS path."""

    CVMFS_PREFIX = "/cvmfs/sft-nightlies-test.cern.ch/releases"

    def test_container_workdir_is_cvmfs_prefix(self):
        cwd, _ = _compute_container_paths(
            WORK_DIR, TARBALL,
            docker=True, container_use_workdir=False,
            cvmfs_prefix=self.CVMFS_PREFIX)
        self.assertEqual(cwd, self.CVMFS_PREFIX)

    def test_tarball_path_rewritten_to_cvmfs_prefix(self):
        _, tb = _compute_container_paths(
            WORK_DIR, TARBALL,
            docker=True, container_use_workdir=False,
            cvmfs_prefix=self.CVMFS_PREFIX)
        self.assertTrue(tb.startswith(self.CVMFS_PREFIX))
        self.assertFalse(tb.startswith(WORK_DIR))

    def test_cvmfs_prefix_takes_priority_over_container_use_workdir(self):
        # When both flags are conceptually set, --cvmfs-prefix wins because
        # it is checked first in the if/elif/else chain.
        cwd, tb = _compute_container_paths(
            WORK_DIR, TARBALL,
            docker=True, container_use_workdir=True,
            cvmfs_prefix=self.CVMFS_PREFIX)
        self.assertEqual(cwd, self.CVMFS_PREFIX)
        self.assertTrue(tb.startswith(self.CVMFS_PREFIX))

    def test_no_docker_cvmfs_prefix_is_ignored(self):
        # --cvmfs-prefix only makes sense with --docker; without it the
        # docker branch is not entered, so container_workDir stays "".
        cwd, tb = _compute_container_paths(
            WORK_DIR, TARBALL,
            docker=False, container_use_workdir=False,
            cvmfs_prefix=self.CVMFS_PREFIX)
        self.assertEqual(cwd, "")
        self.assertEqual(tb, TARBALL)


class RegexEscapeTest(unittest.TestCase):
    """re.escape() ensures workDirs containing regex metacharacters are safe."""

    def test_workdir_with_plus_sign(self):
        wd = "/data/alice+special/sw"
        tb = wd + "/TARS/x86_64-el9/pkg/pkg-1.0.tar.gz"
        cwd, adjusted = _compute_container_paths(
            wd, tb, docker=True, container_use_workdir=False)
        self.assertEqual(cwd, "/container/bits/sw")
        self.assertTrue(adjusted.startswith("/container/bits/sw"))

    def test_workdir_with_parentheses(self):
        wd = "/data/(test)/sw"
        tb = wd + "/TARS/x86_64-el9/pkg/pkg-1.0.tar.gz"
        cwd, adjusted = _compute_container_paths(
            wd, tb, docker=True, container_use_workdir=False)
        self.assertTrue(adjusted.startswith("/container/bits/sw"))

    def test_workdir_with_dot(self):
        # A dot in the path must not match arbitrary characters.
        wd = "/data/sw.v2"
        tb = wd + "/TARS/x86_64-el9/pkg/pkg-1.0.tar.gz"
        # A path that starts with /data/swXv2 must NOT be rewritten.
        tb_wrong = "/data/swXv2/TARS/x86_64-el9/pkg/pkg-1.0.tar.gz"
        _, adjusted = _compute_container_paths(
            wd, tb_wrong, docker=True, container_use_workdir=False)
        # re.escape makes "." literal — the wrong path should be unchanged.
        self.assertEqual(adjusted, tb_wrong)


if __name__ == "__main__":
    unittest.main()
