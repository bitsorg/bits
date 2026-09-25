# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for `bits cvmfs-path` (bits_helpers/cvmfs_path.py).

The handler loads a defaults profile and expands the group's publish-path
templates. Here the defaults loading is stubbed (parseDefaults/readDefaults and
the configDir existence check) so the tests exercise only the path resolution +
admin/user root selection, with no recipe checkout required.
"""

import io
import os
import sys
import types
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers import cvmfs_path as CP
from bits_helpers import repo_provider as RP


_META = {"system": {
    "prefix": "/cvmfs/test.cvmfs.io",
    "cvmfs_releases_template": "{prefix}/releases/{platform}/Packages/{pkg}/{tag}",
    "cvmfs_modules_template": "{prefix}/{platform}/Modules/modulefiles/{pkg}",
    "cvmfs_shared_path_template": "{prefix}/noarch/{pkg}/{tag}",
    "cvmfs_user_prefix": "{prefix}/user",
}}


class _Args:
    def __init__(self, **kw):
        self.configDir = "/does/not/matter"
        self.defaults = ["release"]
        self.architecture = "el9_x86-64"
        self.disable = []
        self.package = "GENIE"
        self.version = "R-3_06_02"
        self.platform = "x86_64-el9"
        self.installDir = "el9-x86_64"
        self.kind = "releases"
        self.admin = False
        self.login = ""
        self.__dict__.update(kw)


class _Parser:
    def error(self, msg):
        raise SystemExit(msg)


class CvmfsPathHandlerTest(unittest.TestCase):
    def setUp(self):
        # Stub the defaults loading so no recipe checkout is needed. The configDir
        # existence check now lives in repo_provider.resolve_config_dir, so the
        # "exists" stub is applied there.
        self._orig = (RP.exists, CP.parseDefaults, CP.readDefaults)
        RP.exists = lambda p: True
        CP.readDefaults = lambda *a, **k: ({}, "")
        CP.parseDefaults = lambda *a, **k: ("", {}, {}, _META)

    def tearDown(self):
        RP.exists, CP.parseDefaults, CP.readDefaults = self._orig

    def _run(self, **kw):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = CP.doCvmfsPath(_Args(**kw), _Parser())
        self.assertTrue(rc)
        return buf.getvalue().strip()

    def test_admin_releases(self):
        self.assertEqual(
            self._run(admin=True),
            "/cvmfs/test.cvmfs.io/releases/x86_64-el9/Packages/GENIE/R-3_06_02")

    def test_user_releases(self):
        self.assertEqual(
            self._run(admin=False, login="alice"),
            "/cvmfs/test.cvmfs.io/user/alice/releases/x86_64-el9/Packages/GENIE/R-3_06_02")

    def test_admin_modules(self):
        self.assertEqual(
            self._run(admin=True, kind="modules"),
            "/cvmfs/test.cvmfs.io/x86_64-el9/Modules/modulefiles/GENIE")

    def test_admin_shared_ignores_platform(self):
        self.assertEqual(
            self._run(admin=True, kind="shared"),
            "/cvmfs/test.cvmfs.io/noarch/GENIE/R-3_06_02")

    def test_user_without_login_aborts(self):
        with self.assertRaises(SystemExit):
            self._run(admin=False, login="")

    def test_prefix_only_defaults(self):
        CP.parseDefaults = lambda *a, **k: ("", {}, {}, {"system": {"prefix": "/cvmfs/x.io"}})
        self.assertEqual(
            self._run(admin=True),
            "/cvmfs/x.io/x86_64-el9/Packages/GENIE/R-3_06_02")

    def test_no_prefix_aborts(self):
        CP.parseDefaults = lambda *a, **k: ("", {}, {}, {"system": {}})
        with self.assertRaises(SystemExit):
            self._run(admin=True)

    def test_release_segment_from_branch_or_none(self):
        # {release} comes from the config dir's branch; when git fails (not a
        # checkout) its error text must not leak into the path.
        CP.parseDefaults = lambda *a, **k: ("", {}, {}, {"system": {
            "prefix": "/cvmfs/g",
            "cvmfs_releases_template": "{prefix}/{release}/{pkg}/{tag}/{platform}"}})
        orig = CP.git
        try:
            CP.git = lambda *a, **k: (128, "fatal: not a git repository")
            self.assertEqual(self._run(admin=True),
                             "/cvmfs/g/GENIE/R-3_06_02/x86_64-el9")
            CP.git = lambda *a, **k: (0, "refs/heads/LCG_110")
            self.assertEqual(self._run(admin=True),
                             "/cvmfs/g/LCG_110/GENIE/R-3_06_02/x86_64-el9")
        finally:
            CP.git = orig

    def test_set_release_reaches_path(self):
        # --set release=... (as passed to the build) wins over the default main.
        CP.parseDefaults = lambda *a, **k: ("", {}, {}, {
            "variables": {"release": "main"},
            "system": {"prefix": "/cvmfs/g",
                       "cvmfs_releases_template": "{prefix}/{release}/{pkg}/{tag}/{platform}"}})
        self.assertEqual(self._run(admin=True, flavours={"release": "LCG_110"}),
                         "/cvmfs/g/LCG_110/GENIE/R-3_06_02/x86_64-el9")
        self.assertEqual(self._run(admin=True),
                         "/cvmfs/g/GENIE/R-3_06_02/x86_64-el9")

    def test_prefix_fallback_when_recipe_has_none(self):
        # Recipe declares no prefix; --prefix supplies it, templates default.
        CP.parseDefaults = lambda *a, **k: ("", {}, {}, {"system": {}})
        self.assertEqual(
            self._run(admin=True, prefix="/cvmfs/y.io/cms/releases"),
            "/cvmfs/y.io/cms/releases/x86_64-el9/Packages/GENIE/R-3_06_02")


if __name__ == "__main__":
    unittest.main()
