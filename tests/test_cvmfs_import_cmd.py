# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the `bits import` CLI driver (bits_helpers/cvmfs_import_cmd)."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from bits_helpers.cvmfs_import_cmd import doImport, _list_modules


def _args(out, **kw):
    base = dict(workDir=out, architecture="x86_64-el9-gcc13",
               importOut=out, importLabel="LCG_109",
               importAliases=None, importForce=False,
               importManifest=None, importModulepath=None)
    base.update(kw)
    return SimpleNamespace(**base)


CLOSED_MANIFEST = {"packages": [
    {"module_id": "ROOT/6.38.00", "base_prefix": "/cvmfs/x/ROOT/6.38.00",
     "version": "6.38.00", "revision": "1",
     "env": [["prepend-path", "PATH", "$PREFIX/bin"]],
     "deps": ["Python/3.13.11"]},
    {"module_id": "Python/3.13.11", "base_prefix": "/cvmfs/x/Python/3.13.11",
     "version": "3.13.11", "revision": "1",
     "env": [["prepend-path", "PATH", "$PREFIX/bin"]], "deps": []},
]}


class TestListModules(unittest.TestCase):

    def test_enumerates_name_version_skips_hidden(self):
        with tempfile.TemporaryDirectory() as mp:
            os.makedirs(os.path.join(mp, "ROOT"))
            open(os.path.join(mp, "ROOT", "6.38.00"), "w").close()
            open(os.path.join(mp, "ROOT", ".version"), "w").close()   # skipped
            os.makedirs(os.path.join(mp, "Python"))
            open(os.path.join(mp, "Python", "3.13.11"), "w").close()
            self.assertEqual(_list_modules(mp),
                             ["Python/3.13.11", "ROOT/6.38.00"])


class TestDoCvmfsImport(unittest.TestCase):

    def _manifest(self, d, data):
        p = os.path.join(d, "manifest.json")
        with open(p, "w") as fh:
            json.dump(data, fh)
        return p

    def test_closed_manifest_writes_overlay(self):
        with tempfile.TemporaryDirectory() as out:
            mf = self._manifest(out, CLOSED_MANIFEST)
            ok = doImport(_args(out, importManifest=mf), None)
            self.assertTrue(ok)
            # exactly one build_id dir written, with a catalog + modulefiles
            bids = [d for d in os.listdir(out) if d.startswith("LCG_109-")]
            self.assertEqual(len(bids), 1)
            arch_root = os.path.join(out, bids[0], "x86_64-el9-gcc13")
            self.assertTrue(os.path.isfile(os.path.join(arch_root, "ROOT", "6.38.00")))
            self.assertTrue(os.path.isfile(os.path.join(out, bids[0], ".cvmfscatalog")))

    def test_open_release_refused_without_force(self):
        man = {"packages": [CLOSED_MANIFEST["packages"][0]]}   # ROOT, dep dangles
        with tempfile.TemporaryDirectory() as out:
            mf = self._manifest(out, man)
            self.assertFalse(doImport(_args(out, importManifest=mf), None))
            self.assertEqual([d for d in os.listdir(out) if d.startswith("LCG_109")], [])

    def test_open_release_forced(self):
        man = {"packages": [CLOSED_MANIFEST["packages"][0]]}
        with tempfile.TemporaryDirectory() as out:
            mf = self._manifest(out, man)
            self.assertTrue(doImport(
                _args(out, importManifest=mf, importForce=True), None))

    def test_trusted_harvest_writes_overlay(self):
        with tempfile.TemporaryDirectory() as dep, tempfile.TemporaryDirectory() as out:
            arch = "x86_64-el9-gcc13"
            mroot = os.path.join(dep, "Modules", "modulefiles")
            proot = os.path.join(dep, "Packages")
            for pkg, ver, deps, h in [("ROOT", "6.38.00-1", [], "hR")]:
                os.makedirs(os.path.join(mroot, pkg))
                with open(os.path.join(mroot, pkg, ver), "w") as fh:
                    fh.write("#%%Module1.0\nset PKG_ROOT $::env(BASEDIR)/%s/%s\n"
                             "prepend-path PATH $PKG_ROOT/bin\n" % (pkg, ver))
                os.makedirs(os.path.join(proot, pkg, ver))
                with open(os.path.join(proot, pkg, ver, ".meta.json"), "w") as fh:
                    json.dump({"build_id": "rel-1",
                               "package": {"hash": h, "version": "6.38.00",
                                           "revision": "1"}}, fh)
            ok = doImport(_args(out, architecture=arch, importTrusted=True,
                                importModulepath=mroot, importInstallBase=proot), None)
            self.assertTrue(ok)
            mf = os.path.join(out, "rel-1", arch, "ROOT", "6.38.00-1")
            with open(mf) as fh:
                text = fh.read()
            self.assertIn("%s/ROOT/6.38.00-1" % proot, text)   # re-anchored absolute
            self.assertNotIn("BASEDIR", text)

    def test_trusted_needs_modulepath_and_install_base(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertFalse(doImport(_args(out, importTrusted=True), None))

    def test_no_source_errors(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertFalse(doImport(_args(out), None))

    def test_missing_manifest_errors(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertFalse(doImport(
                _args(out, importManifest=os.path.join(out, "nope.json")), None))


if __name__ == "__main__":
    unittest.main()
