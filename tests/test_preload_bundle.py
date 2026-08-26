# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers/preload_bundle — CVMFS filebundle spec generation."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bits_helpers import preload_bundle as P


# A synthetic release: package root (arch-base-relative) -> repo-relative CVMFS
# path. 2-level (<pkg>/<verrev>) and grouped 3-level (<family>/<pkg>/<verrev>).
RELEASE = {
    "xrootd/5.9.1-1":            "el9/Packages/xrootd/5.9.1",
    "Boost/1.90.0-1":            "el9/Packages/Boost/1.90.0",
    "gcc/GCC/14.2.0-1":          "el9/Packages/GCC/14.2.0",   # grouped: <family>/<pkg>/<verrev>
}
_meta_exists = lambda prefix: prefix in RELEASE
_resolve = RELEASE.get


class ParseSidecarTest(unittest.TestCase):
    def _write(self, text):
        fd, p = tempfile.mkstemp()
        os.write(fd, text.encode()); os.close(fd)
        self.addCleanup(os.remove, p)
        return p

    def test_trigger_then_files_dedup_comments(self):
        p = self._write(
            "# a preload sidecar\n"
            "xrootd/5.9.1-1/bin/xrdcp\n"
            "Boost/1.90.0-1/lib/libboost.so\n"
            "\n"
            "Boost/1.90.0-1/lib/libboost.so\n"       # dup dropped
            "xrootd/5.9.1-1/lib/libXrdCl.so\n")
        trig, files = P.parse_sidecar(p)
        self.assertEqual(trig, "xrootd/5.9.1-1/bin/xrdcp")
        self.assertEqual(files, ["Boost/1.90.0-1/lib/libboost.so",
                                 "xrootd/5.9.1-1/lib/libXrdCl.so"])

    def test_missing_file_is_safe(self):
        self.assertEqual(P.parse_sidecar("/no/such/sidecar.paths"), (None, []))


class OwningPkgRootTest(unittest.TestCase):
    def test_two_level(self):
        self.assertEqual(
            P._owning_pkg_root("Boost/1.90.0-1/lib/libboost.so", _meta_exists),
            ("Boost/1.90.0-1", "lib/libboost.so"))

    def test_grouped_three_level_longest_wins(self):
        self.assertEqual(
            P._owning_pkg_root("gcc/GCC/14.2.0-1/lib64/libstdc++.so", _meta_exists),
            ("gcc/GCC/14.2.0-1", "lib64/libstdc++.so"))

    def test_unknown_owner(self):
        self.assertEqual(P._owning_pkg_root("Nope/1.0/lib/x.so", _meta_exists),
                         (None, None))


class BuildDependenciesTest(unittest.TestCase):
    def test_maps_sorts_dedups_and_skips_trigger(self):
        files = [
            "xrootd/5.9.1-1/bin/xrdcp",                 # trigger -> skipped
            "Boost/1.90.0-1/lib/libboost.so",
            "xrootd/5.9.1-1/lib/libXrdCl.so",
            "gcc/GCC/14.2.0-1/lib64/libstdc++.so",
        ]
        deps = P.build_dependencies(files, _resolve, _meta_exists,
                                    skip={"xrootd/5.9.1-1/bin/xrdcp"})
        self.assertEqual(deps, [
            "/el9/Packages/Boost/1.90.0/lib/libboost.so",
            "/el9/Packages/GCC/14.2.0/lib64/libstdc++.so",
            "/el9/Packages/xrootd/5.9.1/lib/libXrdCl.so",
        ])

    def test_unsafe_and_unresolvable_dropped(self):
        files = ["/abs/evil", "../escape/x", "Unknown/1.0/lib/x.so",
                 "Boost/1.90.0-1/lib/ok.so"]
        deps = P.build_dependencies(files, _resolve, _meta_exists)
        self.assertEqual(deps, ["/el9/Packages/Boost/1.90.0/lib/ok.so"])


class RenderAndPathTest(unittest.TestCase):
    def test_render_spec_exact_envelope(self):
        spec = P.render_spec(["/a", "/b"])
        self.assertEqual(spec, {"name": "CVMFS_BUNDLE", "version": "1.0.0",
                                "encoding": "UTF-8", "dependencies": ["/a", "/b"]})

    def test_bundle_path_for(self):
        self.assertEqual(P.bundle_path_for("bin/xrdcp"), "bin/.cvmfsbundle-xrdcp")
        self.assertEqual(P.bundle_path_for("root"), ".cvmfsbundle-root")


class GenerateForPackageTest(unittest.TestCase):
    def setUp(self):
        self.pkgroot = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.pkgroot, True)
        os.makedirs(os.path.join(self.pkgroot, P.SIDECAR_DIR))

    def _sidecar(self, name, lines):
        with open(os.path.join(self.pkgroot, P.SIDECAR_DIR, name), "w") as fh:
            fh.write("\n".join(lines) + "\n")

    def test_writes_bundle_next_to_trigger_and_removes_sidecar(self):
        self._sidecar("xrdcp.paths", [
            "xrootd/5.9.1-1/bin/xrdcp",                 # trigger
            "Boost/1.90.0-1/lib/libboost.so",
            "xrootd/5.9.1-1/lib/libXrdCl.so",
        ])
        written = P.generate_for_package(self.pkgroot, _resolve, _meta_exists)
        self.assertEqual(written, ["bin/.cvmfsbundle-xrdcp"])
        dest = os.path.join(self.pkgroot, "bin", ".cvmfsbundle-xrdcp")
        with open(dest) as fh:
            doc = json.load(fh)
        self.assertEqual(doc["name"], "CVMFS_BUNDLE")
        self.assertEqual(doc["dependencies"], [
            "/el9/Packages/Boost/1.90.0/lib/libboost.so",
            "/el9/Packages/xrootd/5.9.1/lib/libXrdCl.so",   # trigger's own file kept; only the exe excluded
        ])
        # sidecar dir removed so it never reaches CVMFS
        self.assertFalse(os.path.exists(os.path.join(self.pkgroot, P.SIDECAR_DIR)))

    def test_empty_deps_sidecar_writes_no_bundle(self):
        self._sidecar("xrdcp.paths", ["xrootd/5.9.1-1/bin/xrdcp"])  # only the trigger
        written = P.generate_for_package(self.pkgroot, _resolve, _meta_exists)
        self.assertEqual(written, [])
        self.assertFalse(os.path.exists(os.path.join(self.pkgroot, "bin")))

    def test_one_bad_sidecar_does_not_abort_and_dir_always_removed(self):
        # Inject a write-time failure: a plain file where one bundle's directory
        # needs to be created, so os.makedirs raises for that sidecar only.
        self._sidecar("good.paths", ["xrootd/5.9.1-1/bin/xrdcp",
                                      "Boost/1.90.0-1/lib/libboost.so"])
        open(os.path.join(self.pkgroot, "clash"), "w").close()          # not a dir
        self._sidecar("clash.paths", ["xrootd/5.9.1-1/clash/x",
                                      "Boost/1.90.0-1/lib/libboost.so"])
        written = P.generate_for_package(self.pkgroot, _resolve, _meta_exists)
        # the good sidecar still produced its bundle; clash was skipped, not fatal
        self.assertEqual(written, ["bin/.cvmfsbundle-xrdcp"])
        # cleanup ran despite the failure
        self.assertFalse(os.path.exists(os.path.join(self.pkgroot, P.SIDECAR_DIR)))

    def test_nul_in_path_is_rejected(self):
        self.assertFalse(P._is_safe_rel("Boost/1.90.0-1/lib/\x00evil"))


if __name__ == "__main__":
    unittest.main()
