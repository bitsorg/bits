import unittest
from unittest.mock import MagicMock, patch

from bits_helpers import build


class TestRemoteReuse(unittest.TestCase):
    def test_build_checks_remote_records_before_reusing_local_revision(self):
        class RevisionResolved(BaseException):
            pass

        arch = "x86_64-el9"
        spec = {"package": "fftw", "version": "3.3.10", "is_devel_pkg": False,
                "remote_hashes": ["bb"], "local_hashes": ["cc"]}
        spec.update(remote_revision_hash="bb", local_revision_hash="cc")
        ctx = MagicMock()
        ctx.specs = {"fftw": spec}
        ctx.workDir = ctx.args.workDir = "/sw"
        ctx.args.architecture = ctx.raw_architecture = arch
        ctx.args.defaults = ["release"]
        ctx.args.develPrefix = ctx.develPackageBranch = ""
        ctx.cfg.reuse_overlay = None
        ctx.cfg.symlink_packages_from = None
        ctx.syncHelper.writeStore = ""
        ctx.mainPackage = "fftw"
        link = "fftw-3.3.10-local1.%s.tar.gz" % arch
        target = "../../%s/store/cc/cc/%s" % (arch, link)
        for records, expected in [([("2", "bb")], ("2", "bb")),
                                  ([], ("local1", "cc"))]:
            with self.subTest(records=records), \
                 patch.object(build, "log_current_package"), \
                 patch.object(build, "storeHook"), \
                 patch.object(build, "storeHashes"), \
                 patch.object(build.os, "listdir", return_value=[link]), \
                 patch.object(build.os.path, "isfile", return_value=True), \
                 patch.object(build, "readlink", return_value=target), \
                 patch.object(build, "symlink"), \
                 patch.object(build, "_revision_index_records", return_value=records) as lookup, \
                 patch.object(build, "create_version_link", side_effect=RevisionResolved):
                with self.assertRaises(RevisionResolved):
                    build.build_one_package("fftw", ctx)
                lookup.assert_called_once()
                self.assertEqual((spec["revision"], spec["hash"]), expected)

