# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""`bits lcg-view --build-view`: manifest + native merged view + collapsed setup.sh."""

from pathlib import Path
import json
import os
import tempfile
import unittest

from bits_helpers.lcg_view import main


def _pkg(work, arch, name, ver, files, deps=None):
    d = os.path.join(work, arch, name, ver)
    os.makedirs(d, exist_ok=True)
    for rel in files:
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        Path(p).write_text(rel)
    meta = {"package": {"name": name, "version": ver, "hash": "h" + name, "revision": 1},
            "dependencies": {"direct": {"runtime": deps or []}}}
    Path(os.path.join(d, ".meta.json")).write_text(json.dumps(meta))


class TestBuildView(unittest.TestCase):
    ARCH = "x86_64-el9-gcc14-opt"

    def _closure(self, work):
        _pkg(work, self.ARCH, "ROOT", "v6-36-04",
             ["bin/root", "lib/libCore.so", "include/TObject.h",
              "lib/python3.11/site-packages/ROOT/__init__.py"])
        _pkg(work, self.ARCH, "Boost", "1.89.0",
             ["include/boost/version.hpp", "lib/libboost.so", "lib/pkgconfig/boost.pc"],
             deps=[{"name": "ROOT", "version": "v6-36-04"}])

    def test_manifest_and_view_and_setup(self):
        with tempfile.TemporaryDirectory() as d:
            work = os.path.join(d, "sw")
            out = os.path.join(d, "out")
            self._closure(work)
            view = os.path.join(out, "LCG_110", self.ARCH)
            rc = main(["-a", self.ARCH, "-w", work, "--platform", self.ARCH,
                       "--version-number", "110", "--out", out, "--build-view", view])
            self.assertEqual(rc, 0)
            # manifest
            self.assertTrue(os.path.isfile(
                os.path.join(out, "LCG_110", "LCG_externals_%s.txt" % self.ARCH)))
            # view symlink farm — relative links that resolve
            root_bin = os.path.join(view, "bin", "root")
            self.assertTrue(os.path.islink(root_bin))
            self.assertFalse(os.path.isabs(os.readlink(root_bin)))
            self.assertTrue(os.path.exists(root_bin))
            # setup.sh collapses the path vars onto the single view dir
            setup = Path(os.path.join(view, "setup.sh")).read_text()
            self.assertIn("export PATH='%s/bin" % os.path.abspath(view), setup)
            self.assertIn("export CMAKE_PREFIX_PATH='%s" % os.path.abspath(view), setup)
            self.assertIn("python3.11/site-packages", setup)  # PYTHONPATH minor detected


if __name__ == "__main__":
    unittest.main()
