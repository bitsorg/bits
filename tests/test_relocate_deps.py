# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits cvmfs publish: paths to dependencies become their own CVMFS paths."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import bits_helpers
import bits_helpers.cvmfs_publish as cp

W, A, PH = "/w/sw", "el9-gcc14-opt", "abc"
PP = "%s/Athena/25.0.1-1" % A
TM = {"prefix": "/cvmfs/r/atlas",
      "packages": "{prefix}/{arch}/Packages/{family}{pkg}/{tag}",
      "shared": "{prefix}/noarch/{pkg}/{tag}"}
DEPS = [  # (name, family, effective_architecture, version, revision)
    ("ROOT", "", A, "6.36", "1"),
    ("Python", "lcg", A, "3.13", "1"),        # a family
    ("GCC", "", "el9", "14.3", "1"),          # own_hash toolchain arch
    ("cmake-modules", "", "share", "2.0", "1"),   # noarch
    ("zlib", "", A, "1.0", "1"),
    ("onlybuilt", "", A, "1", ""),            # no revision, never referenced
]
FILES = {
    "env_setup.sh":
        "export ROOTSYS=%(W)s/%(A)s/ROOT/6.36-1\n"
        "export PATH=%(W)s/%(PP)s/bin:%(W)s/%(A)s/lcg/Python/3.13-1/bin:$PATH\n"
        "export OLD=/cvmfs/r/atlas/%(A)s/Packages/ROOT/6.36-1/lib\n",
    "bin/tool": "#!%(W)s/%(A)s/lcg/Python/3.13-1/bin/python3\nprint(1)\n",
    "lib/cmake/AthenaConfig.cmake":
        'set(GCC_ROOT "%(W)s/el9/GCC/14.3-1")\n'
        "include(%(W)s/share/cmake-modules/2.0-1/x.cmake)\n",
    "lib/libAthena.la":
        "dependency_libs=' -L%(W)s/%(A)s/ROOT/6.36-1/lib "
        "%(W)s/INSTALLROOT/%(PH)s/%(PP)s/lib/libz.la'\n",
    "zlib.txt": "%(W)s/%(A)s/zlib/1.0-1/lib;%(W)s/%(A)s/zlib/1.0-10/lib\n",
}
OWN = "/cvmfs/r/atlas/%s/Packages/Athena/25.0.1-1" % A
CTX = {"repo": "r", "arch": A, "tmpl_prefix": "", "templates": TM,
       "prefix_fallback": "/cvmfs/r", "submit": False}


def _pkgroot(base):
    root = os.path.join(base, PP)
    os.makedirs(os.path.join(root, "etc", "profile.d"))
    for rel, text in FILES.items():
        os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
        with open(os.path.join(root, rel), "w") as fh:
            fh.write(text % {"W": W, "A": A, "PP": PP, "PH": PH})
    os.chmod(os.path.join(root, "bin", "tool"), 0o755)
    os.symlink("tool", os.path.join(root, "bin", "link"))
    with open(os.path.join(root, "etc", "profile.d", ".bits-pkginfo"), "w") as fh:
        fh.write('OP=%s\nPP=${PKGPATH:-%s}\nPH=%s\nPKG_DIR="%s"\n' % (PP, PP, PH, W))
    with open(os.path.join(root, "etc", "profile.d", ".bits-relocate"), "w") as fh:
        fh.write("\n".join(sorted(FILES)) + "\nbin/link\n")
    deps = [{"name": n, "pkg_family": f, "effective_architecture": a,
             "version": v, "revision": r} for n, f, a, v, r in DEPS]
    with open(os.path.join(root, ".meta.json"), "w") as fh:
        json.dump({"dependencies": {"recursive": {"build": deps, "runtime": deps[:2]}}}, fh)
    shutil.copy(os.path.join(os.path.dirname(bits_helpers.__file__), "relocate-me.sh"), root)
    return root


def _read(root, rel):
    with open(os.path.join(root, rel)) as fh:
        return fh.read()


class TestRelocateDependencies(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.t, True)
        self.root = _pkgroot(os.path.join(self.t, "w"))

    def _relocate(self, ctx=CTX):
        cp.relocate_dependencies(self.root, cp.dependency_targets(self.root, ctx))

    def test_each_dependency_gets_its_own_path(self):
        self._relocate()
        P = "/cvmfs/r/atlas/%s/Packages" % A
        self.assertEqual(_read(self.root, "env_setup.sh"),
                         "export ROOTSYS=%s/ROOT/6.36-1\n"
                         "export PATH=%s/bin:%s/lcg/Python/3.13-1/bin:$PATH\n"
                         "export OLD=%s/ROOT/6.36-1/lib\n" % (P, W, P, P))
        self.assertEqual(_read(self.root, "bin/tool").splitlines()[0],
                         "#!%s/lcg/Python/3.13-1/bin/python3" % P)
        self.assertEqual(_read(self.root, "lib/cmake/AthenaConfig.cmake"),
                         'set(GCC_ROOT "/cvmfs/r/atlas/el9/Packages/GCC/14.3-1")\n'
                         "include(/cvmfs/r/atlas/noarch/cmake-modules/2.0-1/x.cmake)\n")
        # The package's own paths are left to relocate-me.sh.
        self.assertIn("%s/INSTALLROOT/%s/%s/lib" % (W, PH, PP),
                      _read(self.root, "lib/libAthena.la"))
        self.assertIn("PATH=%s/bin:" % W, _read(self.root, "env_setup.sh"))
        # zlib/1.0-1 is the dependency, zlib/1.0-10 is not.
        self.assertEqual(_read(self.root, "zlib.txt"),
                         "%s/zlib/1.0-1/lib;%s/%s/zlib/1.0-10/lib\n" % (P, W, A))
        self.assertEqual(os.stat(os.path.join(self.root, "bin", "tool")).st_mode & 0o777, 0o755)
        self.assertTrue(os.path.islink(os.path.join(self.root, "bin", "link")))

    def test_a_referenced_dependency_without_a_path_stops_the_publish(self):
        ctx = dict(CTX, templates=dict(TM, packages="{prefix}/{release}/{pkg}/{tag}"))
        with self.assertRaisesRegex(SystemExit, r"refers to .* no CVMFS path: unresolved \{release\}"):
            self._relocate(ctx)

    def test_an_unreferenced_dependency_without_a_path_is_fine(self):
        targets = cp.dependency_targets(self.root, CTX)
        targets["%s/%s/onlybuilt/1" % (W, A)] = ValueError("unresolved {release}")
        cp.relocate_dependencies(self.root, targets)
        self.assertNotIn(W + "/" + A + "/ROOT", _read(self.root, "env_setup.sh"))

    def test_what_else_names_the_build_area_is_reported(self):
        with open(os.path.join(self.root, "lib", "cmake", "AthenaConfig.cmake"), "a") as fh:
            fh.write("# %s/BUILD/abc/Athena/x.cc\n" % W)
        self.assertEqual(cp.relocate_dependencies(self.root, cp.dependency_targets(self.root, CTX)),
                         ["lib/cmake/AthenaConfig.cmake", "zlib.txt"])

    def test_old_meta_and_odd_entries(self):
        with open(os.path.join(self.root, ".meta.json"), "w") as fh:
            json.dump({"dependencies": {"recursive": {"build": [
                {"name": "ROOT", "version": "6.36", "revision": 1}, {"version": "x"}]}}}, fh)
        self.assertEqual(cp.dependency_targets(self.root, CTX)["%s/%s/ROOT/6.36-1" % (W, A)],
                         "/cvmfs/r/atlas/%s/Packages/ROOT/6.36-1" % A)
        os.remove(os.path.join(self.root, ".meta.json"))
        self.assertEqual(cp.dependency_targets(self.root, CTX), {"%s/%s" % (W, PP): W})

    def test_files_outside_the_package_and_read_only_files(self):
        outside = os.path.join(self.t, "outside.txt")
        with open(outside, "w") as fh:
            fh.write("%s/%s/ROOT/6.36-1\n" % (W, A))
        with open(os.path.join(self.root, "etc", "profile.d", ".bits-relocate"), "a") as fh:
            fh.write("%s\n../../../../outside.txt\n" % outside)
        os.chmod(os.path.join(self.root, "env_setup.sh"), 0o444)
        self._relocate()
        self.assertEqual(_read(self.t, "outside.txt"), "%s/%s/ROOT/6.36-1\n" % (W, A))
        self.assertNotIn(W + "/" + A, _read(self.root, "env_setup.sh"))
        self.assertEqual(os.stat(os.path.join(self.root, "env_setup.sh")).st_mode & 0o777, 0o444)

    def test_a_cvmfs_path_containing_the_build_dir(self):
        # relocate-me.sh maps the package's own path in one pass; a dependency's
        # would be rewritten again by it, so that stops the publish.
        ctx = dict(CTX, templates=dict(TM, packages="{prefix}%s/{pkg}/{tag}" % W))
        with self.assertRaisesRegex(SystemExit, "contains the build dir"):
            self._relocate(ctx)

    def test_without_pkginfo_nothing_changes(self):
        os.remove(os.path.join(self.root, "etc", "profile.d", ".bits-pkginfo"))
        self.assertEqual(cp.dependency_targets(self.root, CTX), {})

    def test_publish_one_leaves_no_build_path(self):
        if "GNU tar" not in subprocess.run(["tar", "--version"], capture_output=True,
                                           text=True).stdout:
            self.skipTest("publish packs with GNU tar --hard-dereference (not bsdtar)")
        tar = os.path.join(self.t, "Athena.tar.gz")
        subprocess.run(["tar", "-czf", tar, "-C", os.path.join(self.t, "w"), A], check=True)
        out = os.path.join(self.t, "out")

        def publish(c, p, tarfn, label, **k):
            with tarfile.open(tarfn) as tf:
                tf.extractall(out)
            return "J"
        spec = {"package": "Athena", "version": "25.0.1", "revision": "1"}
        with mock.patch.object(cp, "tar_path", lambda s, r, a: tar), \
             mock.patch.object(cp, "_publish_tar", publish):
            cp.publish_one(spec, dict(CTX, tars_root=self.t, tmp_dir=self.t))
        for dp, _dn, fns in os.walk(out):
            for f in fns:
                p = os.path.join(dp, f)
                if not os.path.islink(p) and f != "zlib.txt":   # 1.0-10: not a dependency
                    text = Path(p).read_text(errors="replace")
                    self.assertNotIn(W + "/", text, p)
                    self.assertNotIn(OWN + "/" + A, text, p)
        self.assertIn("-L/cvmfs/r/atlas/%s/Packages/ROOT/6.36-1/lib %s/lib/libz.la" % (A, OWN),
                      _read(out, "lib/libAthena.la"))
        self.assertIn('PKG_DIR="%s"' % OWN, _read(out, "etc/profile.d/.bits-pkginfo"))


if __name__ == "__main__":
    unittest.main()
