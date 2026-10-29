# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits cvmfs release: a release made from one build's BOMs and the store."""

import io
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import bits_helpers.cvmfs_release as rel
from bits_helpers.checksum import checksum_file
from bits_helpers.utilities import resolve_store_path

ARCH, TOOL = "x86_64-el9-gcc15-opt", "x86_64-el9-gcc15"
TM = {"packages": "{prefix}/{arch}/Packages/{pkg}/{tag}", "release": "LCG_110",
      "views": "{prefix}/views/LCG_110/{arch}", "prefix": "/cvmfs/r/lcg"}


def _pkg(name, h, arch=ARCH, requires=()):
    return {"package": name, "version": "1", "revision": "2", "hash": h,
            "effective_architecture": arch, "requires": list(requires),
            "tarball": "%s-1-2.%s.tar.gz" % (name, arch), "tarball_sha256": "sha256:x"}


def _bom(arch, packages, **kw):
    return dict({"build_id": "release_gcc15_opt-abc", "effective_architecture": arch,
                 "published_at": "2026-10-11T10:00:00Z", "source_pipeline_id": "4242",
                 "cvmfs_templates": TM, "requested_packages": ["LCG"],
                 "packages": packages}, **kw)


class _Body:
    def __init__(self, data):
        self._d = data

    def read(self):
        return self._d


class _S3:
    def __init__(self, objects):
        self.objects = {k: json.dumps(v).encode() for k, v in objects.items()}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": _Body(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, **kw):
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)]}


class FindAndMergeTest(unittest.TestCase):
    def test_by_pipeline_index(self):
        lcg = _bom(ARCH, [_pkg("LCG", "l1", requires=["ROOT"]), _pkg("ROOT", "r1")])
        gcc = _bom(TOOL, [_pkg("GCC", "g1", arch=TOOL)])
        s3 = _S3({"BUILDS/by-pipeline/4242/release_gcc15_opt-abc.json":
                  {"build_id": "release_gcc15_opt-abc", "keys": ["k1", "k2"]},
                  "k1": lcg, "k2": gcc})
        self.assertEqual(rel.find_boms(s3, "bkt", pipeline="4242"), [gcc, lcg])   # by arch
        with self.assertRaises(rel.ReleaseError):
            rel.find_boms(s3, "bkt", pipeline="9999")
        # A BOM the index lists but that cannot be read: the release would be incomplete.
        del s3.objects["k2"]
        with self.assertRaises(rel.ReleaseError):
            rel.find_boms(s3, "bkt", pipeline="4242")

    def test_a_pipeline_with_several_builds_needs_the_build_id(self):
        a = _bom(ARCH, [_pkg("ROOT", "r1")], build_id="b-opt")
        b = _bom("x86_64-el9-gcc15-dbg", [_pkg("ROOT", "r2", arch="x86_64-el9-gcc15-dbg")],
                 build_id="b-dbg")
        s3 = _S3({"BUILDS/by-pipeline/4242/b-opt.json": {"build_id": "b-opt", "keys": ["ka"]},
                  "BUILDS/by-pipeline/4242/b-dbg.json": {"build_id": "b-dbg", "keys": ["kb"]},
                  "ka": a, "kb": b})
        with self.assertRaises(rel.ReleaseError) as cm:
            rel.find_boms(s3, "bkt", pipeline="4242")
        self.assertIn("--build-id", str(cm.exception))
        self.assertEqual(rel.find_boms(s3, "bkt", pipeline="4242", build_id="b-dbg"), [b])

    def test_by_build_id_newest_per_arch(self):
        old = _bom(ARCH, [_pkg("ROOT", "r0")], published_at="2026-10-01T00:00:00Z")
        new = _bom(ARCH, [_pkg("ROOT", "r1")])
        other = _bom(ARCH, [_pkg("ROOT", "r2")], published_at="2026-10-12T00:00:00Z",
                     source_pipeline_id="7")
        s3 = _S3({"MANIFESTS/b/a.json": old, "MANIFESTS/b/b.json": new,
                  "MANIFESTS/b/c.json": other, "MANIFESTS/b/sbom.cdx.json": {"bomFormat": "x"},
                  # The SPDX SBOM beside the BOMs has a packages list too.
                  "MANIFESTS/b/sbom.spdx.json": {"packages": [{"SPDXID": "x", "name": "ROOT"}]}})
        self.assertEqual(rel.find_boms(s3, "bkt", build_id="b"), [other])
        self.assertEqual(rel.find_boms(s3, "bkt", pipeline="4242", build_id="b"), [new])

    def test_merge_orders_by_requires_and_takes_the_top_levels_arch(self):
        boms = [_bom(ARCH, [_pkg("LCG", "l1", requires=["ROOT", "GCC"]),
                            _pkg("ROOT", "r1", requires=["GCC"])]),
                _bom(TOOL, [_pkg("GCC", "g1", arch=TOOL)])]
        man, arch = rel.merge_boms(boms)
        self.assertEqual(arch, ARCH)
        self.assertEqual([p["package"] for p in man["packages"]], ["GCC", "ROOT", "LCG"])
        self.assertEqual((man["cvmfs_templates"], man["requested_packages"]), (TM, ["LCG"]))

    def test_a_noarch_top_level_takes_the_architecture_most_packages_have(self):
        boms = [_bom("share", [_pkg("LCG", "l1", arch="share", requires=["ROOT"])]),
                _bom(ARCH, [_pkg("ROOT", "r1"), _pkg("zlib", "z1")]),
                _bom(TOOL, [_pkg("GCC", "g1", arch=TOOL)])]
        self.assertEqual(rel.merge_boms(boms)[1], ARCH)
        self.assertEqual(rel.merge_boms(boms, TOOL)[1], TOOL)   # --arch decides

    def test_the_architecture_the_boms_record_wins(self):
        # A build of the toolchain alone: its BOM is under the toolchain's arch,
        # the build's own is the one recorded (publish then makes no release).
        boms = [_bom(TOOL, [_pkg("GCC", "g1", arch=TOOL)], architecture=ARCH,
                     requested_packages=["GCC"])]
        self.assertEqual(rel.merge_boms(boms)[1], ARCH)
        self.assertEqual(rel.merge_boms(boms, TOOL)[1], TOOL)   # --arch decides
        # The publishing host's arch (older BOMs) is not the build's: not trusted.
        lcg = [_bom(ARCH, [_pkg("LCG", "l1")], architecture="x86_64-el9")]
        self.assertIsNone(rel.build_architecture(lcg))
        self.assertEqual(rel.merge_boms(lcg)[1], ARCH)
        self.assertEqual(rel.build_architecture([_bom(ARCH, [_pkg("LCG", "l1")],
                                                       architecture=ARCH)]), ARCH)

    def test_merge_refuses_boms_without_templates_or_disagreeing(self):
        with self.assertRaises(rel.ReleaseError):
            rel.merge_boms([_bom(ARCH, [_pkg("ROOT", "r1")], cvmfs_templates=None)])
        with self.assertRaises(rel.ReleaseError):
            rel.merge_boms([_bom(ARCH, [_pkg("ROOT", "r1")]),
                            _bom(TOOL, [], cvmfs_templates=dict(TM, release="LCG_111"))])
        with self.assertRaises(rel.ReleaseError):
            rel.merge_boms([])

    def test_signed_check(self):
        pkgs = [_pkg("ROOT", "r1"), _pkg("GCC", "g1")]
        rel.check_signed(pkgs, {(ARCH, "r1"): "sha256:x", (ARCH, "g1"): "x"})
        with self.assertRaises(rel.ReleaseError) as cm:
            rel.check_signed(pkgs, {(ARCH, "r1"): "sha256:x", (ARCH, "g1"): "sha256:other"})
        self.assertIn("GCC", str(cm.exception))
        with self.assertRaises(rel.ReleaseError):
            rel.check_signed(pkgs, {(ARCH, "r1"): "sha256:x"})
        # The same hash certified for another architecture only does not count.
        with self.assertRaises(rel.ReleaseError) as cm:
            rel.check_signed(pkgs, {(ARCH, "r1"): "sha256:x", ("other-arch", "g1"): "sha256:x"})
        self.assertIn("GCC", str(cm.exception))


class FetchTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.work, True)
        self.store = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.store, True)
        self.tars = os.path.join(self.work, "TARS")

    def _reader(self, content):
        fetched = []

        class S3:
            def download_file(self, Bucket, Key, Filename):
                name = os.path.basename(Key).split("-")[0]
                if name not in content:
                    raise IOError("404 %s" % Key)
                fetched.append(Key)
                with open(Filename, "wb") as fh:
                    fh.write(content[name])
        return mock.Mock(s3=S3(), remoteStore="bkt", fetched=fetched)

    def test_fetches_only_what_is_missing_and_checks_sha256(self):
        root, gcc = _pkg("ROOT", "r1"), _pkg("GCC", "g1", arch=TOOL)
        reader = self._reader({"ROOT": b"root-bytes", "GCC": b"gcc-bytes"})
        path = os.path.join(self.store, "x")
        with open(path, "wb") as fh:
            fh.write(b"root-bytes")
        root["tarball_sha256"] = checksum_file(path)
        n = rel.fetch_missing(reader, [root, gcc], self.tars, lambda p: p["package"] == "GCC")
        self.assertEqual(n, 1)
        # Exactly the file the BOM names, under its own architecture's store path.
        self.assertEqual(reader.fetched, ["%s/ROOT-1-2.%s.tar.gz" % (resolve_store_path(ARCH, "r1"), ARCH)])
        link = os.path.join(self.tars, ARCH, "ROOT", "ROOT-1-2.%s.tar.gz" % ARCH)
        self.assertTrue(os.path.islink(link))
        # The store's bytes differ from the BOM's sha256: refused.
        gcc["tarball_sha256"] = "sha256:" + "0" * 64
        with self.assertRaises(rel.ReleaseError):
            rel.fetch_missing(reader, [gcc], self.tars, lambda p: False)

    def test_missing_from_the_store(self):
        with self.assertRaises(rel.ReleaseError):
            rel.fetch_missing(self._reader({}), [_pkg("ROOT", "r1")], self.tars, lambda p: False)


class MainTest(unittest.TestCase):
    def test_publishes_the_merged_manifest_with_the_release(self):
        boms = [_bom(ARCH, [_pkg("LCG", "l1", requires=["ROOT"]), _pkg("ROOT", "r1")])]
        reader = mock.Mock(s3=object(), remoteStore="bkt")
        got = {}

        def publish(argv):
            m = argv[argv.index("--manifest") + 1]
            with open(m) as fh:
                got["manifest"] = json.load(fh)
            got["argv"] = argv
            return 0
        with mock.patch("bits_helpers.sync.remote_from_url", return_value=reader), \
             mock.patch.object(rel, "find_boms", return_value=boms), \
             mock.patch.object(rel, "fetch_missing", return_value=0), \
             mock.patch.object(rel, "_published_check", return_value=lambda p: True), \
             mock.patch("bits_helpers.cvmfs_publish.main", publish), \
             mock.patch("sys.stderr", io.StringIO()):
            rc = rel.main(["--pipeline", "4242", "--repo", "r", "--remote-store", "b3://bkt",
                           "--publish-path", "staged"])
        self.assertEqual(rc, 0)
        argv = got["argv"]
        for flag in ("--release-view", "--wait", "--publish-path"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--arch") + 1], ARCH)
        self.assertEqual(argv[argv.index("--build-id") + 1], "4242")
        self.assertEqual([p["package"] for p in got["manifest"]["packages"]], ["ROOT", "LCG"])
        self.assertEqual(got["manifest"]["requested_packages"], ["LCG"])

    def test_refuses_a_non_s3_store_and_an_unsigned_build(self):
        with mock.patch("bits_helpers.sync.remote_from_url", return_value=object()), \
             mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(rel.main(["--pipeline", "1", "--repo", "r"]), 1)
        boms = [_bom(ARCH, [_pkg("ROOT", "r1")])]
        reader = mock.Mock(s3=object(), remoteStore="bkt")
        with mock.patch("bits_helpers.sync.remote_from_url", return_value=reader), \
             mock.patch.object(rel, "find_boms", return_value=boms), \
             mock.patch("bits_helpers.build.trusted_reuse_index", return_value={}), \
             mock.patch("bits_helpers.cvmfs_publish.main", side_effect=AssertionError), \
             mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(rel.main(["--pipeline", "1", "--repo", "r",
                                       "--trust-manifest", "https://x/m.json"]), 1)


if __name__ == "__main__":
    unittest.main()
