# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Integration test for CVMFSRemoteSync.fetch_symlinks tarball synthesis.

The reuse-unpack in build_template.sh does `mv $TMP/$PKGHASH/$PKGPATH ...`
where PKGPATH = <arch>/<pkg>/<verrev>, so the synthesized dummy tarball MUST
carry that version-revision directory level. Runs the real shell (needs
jq/tar/find), so it is skipped where those are absent.
"""

import json
import os
import shutil
import tarfile
import tempfile
import unittest

from bits_helpers.sync import CVMFSRemoteSync

_TOOLS = all(shutil.which(t) for t in ("jq", "tar", "find"))
ARCH = "x86_64-el9-gcc15-opt"  # the slc->el rewrite does not touch this name
HASH = "26681160ad2eec00361f7df05dd5a94f6cbdf9e6"


@unittest.skipUnless(_TOOLS, "needs jq/tar/find")
class FetchSymlinksLayoutTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.work = tempfile.mkdtemp()
        pkg = os.path.join(self.root, ARCH, "Packages", "CMake", "3.30.6-1")
        os.makedirs(os.path.join(pkg, "bin"))
        os.makedirs(os.path.join(pkg, "etc"))
        open(os.path.join(pkg, "bin", "cmake"), "w").close()
        with open(os.path.join(pkg, ".meta.json"), "w") as fh:
            json.dump({"package": {"hash": HASH, "version": "3.30.6",
                                   "revision": "1"}}, fh)

    def test_tarball_has_version_revision_level(self):
        sync = CVMFSRemoteSync("cvmfs://" + self.root, None, ARCH, self.work)
        sync.fetch_symlinks({"package": "CMake", "version": "3.30.6",
                             "revision": "1", "remote_hashes": [HASH],
                             "local_hashes": []})
        tb = os.path.join(self.work, "TARS", ARCH, "store", HASH[:2], HASH,
                          "CMake-3.30.6-1.%s.tar.gz" % ARCH)
        self.assertTrue(os.path.isfile(tb), "synthesized tarball missing")
        names = set(tarfile.open(tb).getnames())
        # PKGPATH tail that build_template.sh's `mv` will look for.
        verrev = "./%s/CMake/3.30.6-1" % ARCH
        self.assertIn(verrev, names)
        self.assertIn(verrev + "/bin", names)


if __name__ == "__main__":
    unittest.main()
