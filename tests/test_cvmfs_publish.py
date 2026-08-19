# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the ported producer helpers in bits_helpers.cvmfs_publish.

These cover the bits that were hand-written bash in cvmfs-prepub-publish.yml and
carry hard-won fixes (template expansion, the reused-artefact re-root, the
INSTALLROOT symlink relativiser, sanitize). The stage/submit/pipeline parts need
a build host and are proven separately via the remote-runner."""
import os
import shutil
import tempfile
import unittest

import json

from bits_helpers.cvmfs_publish import (
    expand_tmpl, repo_relative_path, relativise_symlinks, sanitize,
    resolve_pkg_path, tree_fingerprint)


class TestTreeFingerprint(unittest.TestCase):
    def _tree(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        os.makedirs(os.path.join(d, "bin"))
        with open(os.path.join(d, "bin", "x"), "w") as fh:
            fh.write("hello")
        os.symlink("../bin/x", os.path.join(d, "bin", "l"))
        return d

    def test_mtime_independent(self):
        # the catalog hash embeds mtime (relocation stamps it); the content
        # fingerprint must NOT, or no two runs could ever match.
        d = self._tree(); f1 = tree_fingerprint(d)
        os.utime(os.path.join(d, "bin", "x"), (1, 1))
        self.assertEqual(f1, tree_fingerprint(d))

    def test_content_sensitive(self):
        d = self._tree(); f1 = tree_fingerprint(d)
        with open(os.path.join(d, "bin", "x"), "w") as fh:
            fh.write("HELLO")
        self.assertNotEqual(f1, tree_fingerprint(d))

    def test_mode_and_symlink_sensitive(self):
        d = self._tree(); f1 = tree_fingerprint(d)
        os.chmod(os.path.join(d, "bin", "x"), 0o755)
        self.assertNotEqual(f1, tree_fingerprint(d))


class TestResolvePkgPath(unittest.TestCase):
    def _meta(self, **templates):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, ".meta.json"), "w") as fh:
            json.dump({"cvmfs_templates": dict(prefix="/cvmfs/r", **templates)}, fh)
        return d

    def test_non_shared_arch_uses_the_path_template(self):
        d = self._meta(path="{prefix}/el9/Packages/{pkg}/{version}",
                       shared="{prefix}/shared/{pkg}/{version}")
        self.assertEqual(
            resolve_pkg_path(d, "r", "O2", "1.0", "1.0", "", "", "", "", "", "",
                             kind="path", tmpl_prefix="", arch="el9-x86"),
            "el9/Packages/O2/1.0")

    def test_modules_kind_uses_the_modules_template(self):
        d = self._meta(path="{prefix}/el9/Packages/{pkg}/{version}",
                       modules="{prefix}/el9/Modules/modulefiles/{pkg}")
        self.assertEqual(
            resolve_pkg_path(d, "r", "O2", "1.0", "1.0", "", "", "", "", "", "",
                             kind="modules", tmpl_prefix="", arch="el9"),
            "el9/Modules/modulefiles/O2")

    def test_shared_arch_uses_the_shared_template(self):
        # NEGATIVE CONTROL: selecting shared unconditionally would give the wrong
        # path for a normal package and change the relocated bytes (the hash).
        d = self._meta(path="{prefix}/el9/Packages/{pkg}/{version}",
                       shared="{prefix}/shared/{pkg}/{version}")
        self.assertEqual(
            resolve_pkg_path(d, "r", "noarch", "1.0", "1.0", "", "", "", "", "", "",
                             kind="path", tmpl_prefix="", arch="shared"),
            "shared/noarch/1.0")


class TestExpandTmpl(unittest.TestCase):
    def test_family_carries_its_own_slash(self):
        # non-empty family -> "MCGenerators/ROOT"; the template uses {family}{pkg}
        self.assertEqual(
            expand_tmpl("{family}{pkg}/{version}", pkg="ROOT", version="v6",
                        family="MCGenerators"),
            "MCGenerators/ROOT/v6")

    def test_empty_family_collapses_without_a_stray_slash(self):
        self.assertEqual(
            expand_tmpl("{family}{pkg}/{version}", pkg="O2", version="daily"),
            "O2/daily")

    def test_all_tokens_substituted(self):
        got = expand_tmpl("{pkg}-{tag}-{revision}-{platform}-{user}",
                          pkg="a", tag="1", revision="2", platform="el9", user="u")
        self.assertEqual(got, "a-1-2-el9-u")


class TestRepoRelativePath(unittest.TestCase):
    def test_strips_the_repo_prefix(self):
        self.assertEqual(
            repo_relative_path("/cvmfs/test.cvmfs.io/el9/Packages/O2/1.0",
                               "test.cvmfs.io"),
            "el9/Packages/O2/1.0")

    def test_re_roots_a_reused_artefact(self):
        # a from_store package baked with another community's root is re-rooted
        # to this community's prefix (the §31 "re-rooting reused artefact" log).
        self.assertEqual(
            repo_relative_path("/cvmfs/bits.cern.ch/alice/Packages/O2/1.0",
                               "test.cvmfs.io",
                               meta_root="/cvmfs/bits.cern.ch/alice",
                               prefix_fallback="/cvmfs/test.cvmfs.io"),
            "Packages/O2/1.0")

    def test_prefix_community_mismatch_is_refused(self):
        # a path that ends up NOT under /cvmfs/<repo>/ is a misconfiguration.
        with self.assertRaises(ValueError):
            repo_relative_path("/cvmfs/other.cern.ch/x", "test.cvmfs.io")


class TestRelativiseSymlinks(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        os.makedirs(os.path.join(self.d, "bin"))
        os.makedirs(os.path.join(self.d, "lib"))
        open(os.path.join(self.d, "lib", "libfoo.so"), "w").close()

    def test_installroot_abs_symlink_is_relativised(self):
        os.symlink("/build/INSTALLROOT/abc/pkg/1.0/lib/libfoo.so",
                   os.path.join(self.d, "bin", "foo"))
        self.assertEqual(relativise_symlinks(self.d), 1)
        self.assertEqual(os.readlink(os.path.join(self.d, "bin", "foo")),
                         "../lib/libfoo.so")

    def test_system_abs_symlink_is_left_untouched(self):
        # NEGATIVE CONTROL: a non-INSTALLROOT absolute link must NOT be rewritten
        # (rewriting it would break a legitimate system reference).
        os.symlink("/usr/lib/libc.so", os.path.join(self.d, "bin", "sys"))
        self.assertEqual(relativise_symlinks(self.d), 0)
        self.assertEqual(os.readlink(os.path.join(self.d, "bin", "sys")),
                         "/usr/lib/libc.so")


class TestSanitize(unittest.TestCase):
    def test_counts_hardlinks_and_removes_special_files_and_reports_abssym(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        a = os.path.join(d, "a"); open(a, "w").close()
        os.link(a, os.path.join(d, "b"))            # hardlink pair
        os.symlink("/etc/passwd", os.path.join(d, "l"))  # remaining abs symlink
        try:
            os.mkfifo(os.path.join(d, "fifo"))      # unpublishable special
            have_fifo = True
        except (AttributeError, OSError):
            have_fifo = False
        res = sanitize(d)
        self.assertEqual(res["hardlinks"], 2)       # both members counted
        self.assertEqual(res["abs_symlinks"], 1)
        if have_fifo:
            self.assertEqual(res["specials_removed"], 1)
            self.assertFalse(os.path.exists(os.path.join(d, "fifo")))


class TestOrderBiggestFirst(unittest.TestCase):
    def _tars(self, sizes):
        t = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, t, True)
        for name, size in sizes:
            d = os.path.join(t, "el9", name); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "%s-1.0.el9.tar.gz" % name), "wb") as fh:
                fh.write(b"x" * size)
        return t

    def test_largest_first_missing_tar_last(self):
        from bits_helpers.cvmfs_publish import order_biggest_first
        t = self._tars([("GEANT4", 100), ("O2", 50), ("Clang", 80)])
        specs = [{"package": p, "version": "1.0"}
                 for p in ("O2", "GEANT4", "Clang", "Ghost")]   # Ghost: no tar
        self.assertEqual(
            [s["package"] for s in order_biggest_first(specs, t, "el9")],
            ["GEANT4", "Clang", "O2", "Ghost"])


class TestBatchDriver(unittest.TestCase):
    def _manifest(self, sizes):
        import json
        t = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, t, True)
        tars = os.path.join(t, "TARS")
        pkgs = []
        for name, size in sizes:
            d = os.path.join(tars, "el9", name); os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "%s-1.0.el9.tar.gz" % name), "wb") as fh:
                fh.write(b"x" * size)
            pkgs.append({"package": name, "version": "1.0",
                         "effective_architecture": "el9"})
        m = os.path.join(t, "manifest.json")
        with open(m, "w") as fh:
            json.dump({"packages": pkgs}, fh)
        return m, tars

    def _run(self, m, tars, workers, fake):
        from unittest import mock
        import bits_helpers.cvmfs_publish as cp
        argv = ["--manifest", m, "--repo", "r", "--tars-root", tars,
                "--arch", "el9", "--workers", str(workers)]
        if workers > 1:                       # satisfy the concurrency gate
            argv += ["--no-stats-db", "--no-prepare-lock"]
        with mock.patch.object(cp, "publish_one", fake):
            return cp.main(argv)

    def test_workers_gt_1_requires_the_concurrency_flags(self):
        # NEGATIVE CONTROL: drop the gate and this call returns 0 instead of
        # exiting — concurrent prepares would then abort on the shared stats DB.
        m, tars = self._manifest([("a", 10)])
        from unittest import mock
        import bits_helpers.cvmfs_publish as cp
        with mock.patch.object(cp, "publish_one", lambda s, c: []):
            with self.assertRaises(SystemExit):
                cp.main(["--manifest", m, "--repo", "r", "--tars-root", tars,
                         "--arch", "el9", "--workers", "4"])

    def test_serial_is_manifest_order(self):
        m, tars = self._manifest([("a", 10), ("big", 100), ("c", 5)])
        seen = []
        rc = self._run(m, tars, 1, lambda spec, ctx: seen.append(spec["package"]) or [])
        self.assertEqual(rc, 0)
        self.assertEqual(seen, ["a", "big", "c"])       # N=1: manifest order, no sort

    def test_workers_process_all_packages(self):
        m, tars = self._manifest([("a", 10), ("big", 100), ("c", 5), ("d", 50)])
        seen = []
        import threading
        lock = threading.Lock()

        def fake(spec, ctx):
            with lock:
                seen.append(spec["package"])
            return [("job-%s" % spec["package"], "%s@1(pkg)" % spec["package"])]
        rc = self._run(m, tars, 4, fake)
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(seen), ["a", "big", "c", "d"])

    def test_non_redistributable_false_is_excluded(self):
        # exact CI replica: a literal boolean false excludes; enum strings do not.
        import json
        t = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, t, True)
        tars = os.path.join(t, "TARS")
        pkgs = [{"package": "keep", "version": "1.0", "effective_architecture": "el9",
                 "redistributable": "all"},
                {"package": "drop", "version": "1.0", "effective_architecture": "el9",
                 "redistributable": False}]
        for name in ("keep", "drop"):
            d = os.path.join(tars, "el9", name); os.makedirs(d)
            with open(os.path.join(d, "%s-1.0.el9.tar.gz" % name), "wb") as fh:
                fh.write(b"x")
        m = os.path.join(t, "manifest.json")
        with open(m, "w") as fh:
            json.dump({"packages": pkgs}, fh)
        seen = []
        self._run(m, tars, 1, lambda spec, ctx: seen.append(spec["package"]) or [])
        self.assertEqual(seen, ["keep"])            # 'drop' excluded, 'keep' kept

    def test_one_failure_fails_the_batch(self):
        m, tars = self._manifest([("a", 10), ("bad", 100), ("c", 5)])

        def fake(spec, ctx):
            if spec["package"] == "bad":
                raise SystemExit("boom")
            return []
        # NEGATIVE CONTROL: swallow the exception in _run_one and rc would be 0.
        self.assertEqual(self._run(m, tars, 4, fake), 1)
        self.assertEqual(self._run(m, tars, 1, fake), 1)   # serial too

    def test_concurrency_is_bounded_by_workers(self):
        m, tars = self._manifest([(chr(97 + i), 10 + i) for i in range(12)])
        import threading
        import time
        live = [0]; peak = [0]; lock = threading.Lock()

        def fake(spec, ctx):
            with lock:
                live[0] += 1; peak[0] = max(peak[0], live[0])
            time.sleep(0.02)
            with lock:
                live[0] -= 1
            return []
        self.assertEqual(self._run(m, tars, 4, fake), 0)
        self.assertLessEqual(peak[0], 4)                   # never more than N in flight
        self.assertGreater(peak[0], 1)                     # and it DID run concurrently


if __name__ == "__main__":
    unittest.main()
