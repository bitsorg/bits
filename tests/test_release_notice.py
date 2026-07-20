# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for per-RELEASE NOTICE / source-offer generation (bits_helpers/notice.py).

Complements tests/test_notice.py, which covers the per-PACKAGE NOTICE the build
script writes into each $INSTALLROOT.
"""

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock

from bits_helpers import notice


def _e(pkg, lic, version="1", redistributable=None, sources=None, commit=None):
    e = {"package": pkg, "version": version, "license": lic}
    if redistributable is not None:
        e["redistributable"] = redistributable
    if sources:
        e["source_checksums"] = [{"url": u, "store_path": p} for u, p in sources]
    if commit:
        e["commit_hash"] = commit
    return e


ENTRIES = [
    _e("ROOT", "LGPL-2.1-or-later",
       sources=[("https://x/root.tar.gz", "SOURCES/cache/ab/abcd/root.tar.gz")]),
    _e("cfitsio", "CFITSIO"),
    _e("zlib", "Zlib"),
    _e("qgraf", "all-rights-reserved", redistributable="none"),
    _e("runtimeonly", "LicenseRef-Vendor", redistributable="binaries"),
    _e("gitpkg", "GPL-3.0-only", commit="deadbeef"),
    _e("defaults-release", ""),                       # pseudo-package: ignored
]


class ReleaseNoticeTestCase(unittest.TestCase):

    def test_notice_contents(self):
        txt = notice.generate_notice(ENTRIES, "release-x")
        # Attribution appears because cfitsio is present; Geant4's does not.
        self.assertIn("NASA/HEASARC", txt)
        self.assertNotIn("Geant4 Collaboration", txt)
        # Distributed packages listed with their SPDX ids.
        self.assertIn("ROOT", txt)
        self.assertIn("LGPL-2.1-or-later", txt)
        self.assertIn("runtimeonly", txt)             # binaries are distributed
        # Excluded section names the licence-forbidden package…
        self.assertIn("qgraf", txt)
        self.assertIn("Not included in this distribution", txt)
        # …and pseudo-packages never appear.
        self.assertNotIn("defaults-release", txt)

    def test_source_offer_copyleft_only(self):
        txt = notice.generate_source_offer(ENTRIES, "b3://bkt", "release-x")
        # Copyleft with an archived source: the store path is the offer.
        self.assertIn("SOURCES/cache/ab/abcd/root.tar.gz", txt)
        # Copyleft from git: the commit is the corresponding source.
        self.assertIn("deadbeef", txt)
        # Permissive and excluded packages are not offered.
        self.assertNotIn("zlib", txt)
        self.assertNotIn("qgraf", txt)
        self.assertIn("until at least", txt)

    def test_upload_and_write_best_effort(self):
        s3 = MagicMock()
        self.assertTrue(notice.upload_release_compliance(s3, "bkt", "b1", ENTRIES))
        keys = [c.kwargs["Key"] for c in s3.put_object.call_args_list]
        self.assertEqual(keys, ["MANIFESTS/b1/NOTICE",
                                "MANIFESTS/b1/LICENSE-SOURCE-OFFER.txt"])
        s3.put_object.side_effect = RuntimeError("boom")
        self.assertFalse(notice.upload_release_compliance(s3, "bkt", "b1", ENTRIES))

        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.assertTrue(notice.write_release_compliance(d, ENTRIES, "b1"))
        self.assertTrue(os.path.isfile(os.path.join(d, "NOTICE")))
        self.assertTrue(os.path.isfile(os.path.join(d, "LICENSE-SOURCE-OFFER.txt")))


if __name__ == "__main__":
    unittest.main()
