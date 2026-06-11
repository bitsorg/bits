"""Tests for `bits publish --view` build_id derivation + view placement."""

import json
import os
import tempfile
import unittest
from types import SimpleNamespace

from bits_helpers import view_publish_cmd as vp


def _deployed(store, rel, build_id, files=("bin/x",), arch="el9", package=None):
    prefix = os.path.join(store, arch, rel)
    for f in files:
        p = os.path.join(prefix, f)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
    meta = {"build_id": build_id, "architecture": arch}
    if package:
        meta["package"] = package
    with open(os.path.join(prefix, ".meta.json"), "w") as fh:
        json.dump(meta, fh)
    return prefix


def _args(store, **kw):
    base = dict(publishView="rel", architecture="el9", workDir=store,
                cvmfsTarget=store, package=None)
    base.update(kw)
    return SimpleNamespace(**base)


class TestBuildIdDerivation(unittest.TestCase):

    def test_single_build_id_auto(self):
        with tempfile.TemporaryDirectory() as s:
            _deployed(s, "A/1", "L-1"); _deployed(s, "B/2", "L-1")
            self.assertEqual(vp._build_ids_in_area(s, "el9"), {"L-1"})
            self.assertEqual(vp._resolve_build_id(_args(s), s, "el9"), "L-1")

    def test_multiple_build_ids_refused(self):
        with tempfile.TemporaryDirectory() as s:
            _deployed(s, "A/1", "L-1"); _deployed(s, "B/2", "L-2")
            self.assertIsNone(vp._resolve_build_id(_args(s), s, "el9"))   # ambiguous

    def test_package_pins_build_id(self):
        with tempfile.TemporaryDirectory() as s:
            _deployed(s, "ROOT/1", "L-1", package="ROOT")
            _deployed(s, "Boost/2", "L-2", package="Boost")
            bid = vp._resolve_build_id(_args(s, package="ROOT"), s, "el9")
            self.assertEqual(bid, "L-1")


class TestDoPublishView(unittest.TestCase):

    def test_publishes_named_view(self):
        with tempfile.TemporaryDirectory() as s:
            _deployed(s, "ROOT/1", "L-1", files=("bin/root", "lib/libCore.so"))
            _deployed(s, "Boost/2", "L-1", files=("lib/libboost.so",))
            self.assertTrue(vp.doPublishView(_args(s), None))
            view = os.path.join(s, "Views", "rel-L-1", "el9")
            self.assertTrue(os.path.islink(os.path.join(view, "bin", "root")))
            self.assertTrue(os.path.islink(os.path.join(view, "lib", "libboost.so")))
            self.assertTrue(os.path.isfile(
                os.path.join(s, "Views", "rel-L-1", ".cvmfscatalog")))

    def test_honours_layout_views_dir(self):
        with tempfile.TemporaryDirectory() as s:
            prefix = _deployed(s, "ROOT/1", "L-1", files=("bin/root",))
            # inject a non-default views_dir into the recorded layout
            with open(os.path.join(prefix, ".meta.json")) as fh:
                meta = json.load(fh)
            meta["cvmfs_layout"] = {"views_dir": "release-views"}
            with open(os.path.join(prefix, ".meta.json"), "w") as fh:
                json.dump(meta, fh)
            self.assertTrue(vp.doPublishView(_args(s), None))
            self.assertTrue(os.path.isdir(
                os.path.join(s, "release-views", "rel-L-1", "el9")))
            self.assertFalse(os.path.exists(os.path.join(s, "Views")))

    def test_no_packages_fails(self):
        with tempfile.TemporaryDirectory() as s:
            os.makedirs(os.path.join(s, "el9"))
            self.assertFalse(vp.doPublishView(_args(s), None))


if __name__ == "__main__":
    unittest.main()
