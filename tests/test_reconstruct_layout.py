# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""ADR-0005 Phase 1: reconstruct the local version + dist* symlink layout from
the resolved dependency graph (no S3), matching what the build/reuse paths write.
"""

import os
import tempfile
import types
import unittest

from bits_helpers.build import (
    reconstruct_local_layout, create_version_link, createDistLinks,
)


def _specs():
    # GCC (leaf) <- fftw (depends on GCC).  Distinct hashes so the shard/dir differ.
    gcc = {"package": "GCC-Toolchain", "version": "v14.2.0-alice2", "revision": "1",
           "hash": "aa11" + "0" * 36, "requires": [],
           "full_requires": [], "full_runtime_requires": []}
    fftw = {"package": "fftw", "version": "3.3.10", "revision": "2",
            "hash": "bb22" + "0" * 36, "requires": ["GCC-Toolchain"],
            "full_requires": ["GCC-Toolchain"], "full_runtime_requires": ["GCC-Toolchain"]}
    return {"GCC-Toolchain": gcc, "fftw": fftw}


def _readlink(root, *parts):
    p = os.path.join(root, *parts)
    assert os.path.islink(p), "expected a symlink at %s" % p
    return os.readlink(p)


class TestReconstructLayout(unittest.TestCase):

    ARCH = "x86_64-el9"

    def test_version_link_target_and_location(self):
        specs = _specs()
        with tempfile.TemporaryDirectory() as d:
            create_version_link(specs["fftw"], self.ARCH, d)
            tgt = _readlink(d, "TARS", self.ARCH, "fftw", "fftw-3.3.10-2.x86_64-el9.tar.gz")
            self.assertEqual(
                tgt, "../../x86_64-el9/store/bb/bb22" + "0" * 36 + "/fftw-3.3.10-2.x86_64-el9.tar.gz")

    def test_dist_trees_contain_self_plus_closure(self):
        specs = _specs()
        with tempfile.TemporaryDirectory() as d:
            reconstruct_local_layout(specs["fftw"], specs, self.ARCH, d)
            base = os.path.join(d, "TARS", self.ARCH)
            for repo in ("dist", "dist-direct", "dist-runtime"):
                ddir = os.path.join(base, repo, "fftw", "fftw-3.3.10-2")
                names = sorted(os.listdir(ddir))
                # every dist* tree here holds the package itself + its one dep
                self.assertEqual(names, ["GCC-Toolchain-v14.2.0-alice2-1.x86_64-el9.tar.gz",
                                         "fftw-3.3.10-2.x86_64-el9.tar.gz"], repo)
                # targets point five levels up into the content store, by hash
                self.assertEqual(
                    os.readlink(os.path.join(ddir, "fftw-3.3.10-2.x86_64-el9.tar.gz")),
                    "../../../../../TARS/x86_64-el9/store/bb/bb22" + "0" * 36
                    + "/fftw-3.3.10-2.x86_64-el9.tar.gz")
                self.assertEqual(
                    os.readlink(os.path.join(ddir, "GCC-Toolchain-v14.2.0-alice2-1.x86_64-el9.tar.gz")),
                    "../../../../../TARS/x86_64-el9/store/aa/aa11" + "0" * 36
                    + "/GCC-Toolchain-v14.2.0-alice2-1.x86_64-el9.tar.gz")

    def test_matches_createDistLinks_exactly(self):
        # Equivalence / no-regression: the standalone reconstruction produces the
        # same dist trees as the existing createDistLinks(args, syncHelper, ...).
        specs = _specs()
        with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
            reconstruct_local_layout(specs["fftw"], specs, self.ARCH, d1)
            args = types.SimpleNamespace(architecture=self.ARCH, workDir=d2)
            for repo, req in (("dist", "full_requires"), ("dist-direct", "requires"),
                              ("dist-runtime", "full_runtime_requires")):
                createDistLinks(specs["fftw"], specs, args, None, repo, req)
            for repo in ("dist", "dist-direct", "dist-runtime"):
                rel = os.path.join("TARS", self.ARCH, repo, "fftw", "fftw-3.3.10-2")
                a = {n: os.readlink(os.path.join(d1, rel, n)) for n in os.listdir(os.path.join(d1, rel))}
                b = {n: os.readlink(os.path.join(d2, rel, n)) for n in os.listdir(os.path.join(d2, rel))}
                self.assertEqual(a, b, repo)

    def test_shared_noarch_uses_shared_arch(self):
        # A package with architecture: shared installs under TARS/shared/…
        from bits_helpers.utilities import SHARED_ARCH
        specs = _specs()
        specs["fftw"]["architecture"] = SHARED_ARCH
        with tempfile.TemporaryDirectory() as d:
            create_version_link(specs["fftw"], self.ARCH, d)
            tgt = _readlink(d, "TARS", SHARED_ARCH, "fftw", "fftw-3.3.10-2.shared.tar.gz")
            self.assertEqual(
                tgt, "../../shared/store/bb/bb22" + "0" * 36 + "/fftw-3.3.10-2.shared.tar.gz")

    def test_dropped_revision_omits_suffix(self):
        # force_revision="" (empty) drops the -rev suffix everywhere (ver_rev).
        specs = _specs()
        specs["fftw"]["revision"] = ""
        with tempfile.TemporaryDirectory() as d:
            create_version_link(specs["fftw"], self.ARCH, d)
            self.assertTrue(os.path.islink(os.path.join(
                d, "TARS", self.ARCH, "fftw", "fftw-3.3.10.x86_64-el9.tar.gz")))


if __name__ == "__main__":
    unittest.main()
