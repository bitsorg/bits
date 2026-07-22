# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tar-slip and token-safety guards added in the pre-release security review."""

import io
import os
import shutil
import tarfile
import tempfile
import unittest

from bits_helpers.workarea import _assert_safe_archive_members


def _make_tar(path, members):
    """Create a .tar.gz with the given (name, data) members, unsanitised."""
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))


class TarSlipGuardTestCase(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_clean_archive_passes(self):
        p = os.path.join(self.d, "ok.tar.gz")
        _make_tar(p, [("pkg/README", b"x"), ("pkg/src/main.c", b"y")])
        self.assertIsNone(_assert_safe_archive_members(p))

    def test_traversing_member_rejected(self):
        p = os.path.join(self.d, "slip.tar.gz")
        _make_tar(p, [("pkg/ok", b"x"), ("../../evil", b"y")])
        with self.assertRaises(ValueError):
            _assert_safe_archive_members(p)

    def test_absolute_member_rejected(self):
        p = os.path.join(self.d, "abs.tar.gz")
        _make_tar(p, [("/etc/evil", b"y")])
        with self.assertRaises(ValueError):
            _assert_safe_archive_members(p)

    def test_absolute_symlink_target_rejected(self):
        p = os.path.join(self.d, "sym.tar.gz")
        with tarfile.open(p, "w:gz") as tf:
            ti = tarfile.TarInfo("pkg/link")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "/etc/passwd"
            tf.addfile(ti)
        with self.assertRaises(ValueError):
            _assert_safe_archive_members(p)

    def test_relative_symlink_allowed(self):
        # lib64 -> ../lib style links are common and legitimate.
        p = os.path.join(self.d, "rel.tar.gz")
        with tarfile.open(p, "w:gz") as tf:
            ti = tarfile.TarInfo("pkg/lib64")
            ti.type = tarfile.SYMTYPE
            ti.linkname = "../lib"
            tf.addfile(ti)
        self.assertIsNone(_assert_safe_archive_members(p))


class PrepubTokenTestCase(unittest.TestCase):

    def test_token_not_sent_over_unverified_tls(self):
        from bits_helpers.prepub import _make_session
        s = _make_session("secret-token", no_verify_tls=True)
        self.assertNotIn("Authorization", s.headers)
        self.assertFalse(s.verify)

    def test_token_sent_over_verified_tls(self):
        from bits_helpers.prepub import _make_session
        s = _make_session("secret-token", no_verify_tls=False)
        self.assertEqual(s.headers.get("Authorization"), "Bearer secret-token")
        self.assertTrue(s.verify)


if __name__ == "__main__":
    unittest.main()
