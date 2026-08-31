# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers/verify.py — bits verify command."""

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from bits_helpers.verify import (
    FAIL, MISS, PASS, SKIP,
    _find_tarball, _git_head, _store_rel,
    verify_package, verify_provider,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _write_file(path: str, content: bytes = b"fake tarball") -> str:
    """Write *content* to *path* (creating parent dirs) and return the path."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


def _sha256(content: bytes) -> str:
    """Return the checksum in the same ``sha256:<hex>`` format used by the manifest."""
    return "sha256:" + hashlib.sha256(content).hexdigest()


# ── _store_rel ─────────────────────────────────────────────────────────────────

class TestStoreRel(unittest.TestCase):
    def test_path_structure(self):
        rel = _store_rel("abcdef1234", "Foo-1.0.tar.gz", "slc9_x86-64")
        self.assertEqual(
            rel,
            os.path.join("TARS", "slc9_x86-64", "store", "ab", "abcdef1234", "Foo-1.0.tar.gz"),
        )

    def test_uses_first_two_chars_of_hash(self):
        rel = _store_rel("deadbeef", "pkg.tar.gz", "arch")
        self.assertTrue(rel.startswith(os.path.join("TARS", "arch", "store", "de", "deadbeef")))


# ── _find_tarball ──────────────────────────────────────────────────────────────

class TestFindTarball(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_returns_none_when_no_roots(self):
        result = _find_tarball("Foo-1.0.tar.gz", "aabbcc", "slc9_x86-64", [])
        self.assertIsNone(result)

    def test_returns_none_when_not_found(self):
        result = _find_tarball("Foo-1.0.tar.gz", "aabbcc", "slc9_x86-64", [self.tmp])
        self.assertIsNone(result)

    def test_finds_tarball_in_first_root(self):
        pkg_hash = "aabbcc0011"
        tarball = "Foo-1.0-1.slc9_x86-64.tar.gz"
        arch = "slc9_x86-64"
        rel = _store_rel(pkg_hash, tarball, arch)
        full = _write_file(os.path.join(self.tmp, rel))
        result = _find_tarball(tarball, pkg_hash, arch, [self.tmp, "/nonexistent"])
        self.assertEqual(result, full)

    def test_falls_back_to_second_root(self):
        tmp2 = tempfile.mkdtemp()
        try:
            pkg_hash = "deadbeef01"
            tarball = "Bar-2.0-1.slc9_x86-64.tar.gz"
            arch = "slc9_x86-64"
            rel = _store_rel(pkg_hash, tarball, arch)
            full = _write_file(os.path.join(tmp2, rel))
            result = _find_tarball(tarball, pkg_hash, arch, [self.tmp, tmp2])
            self.assertEqual(result, full)
        finally:
            import shutil
            shutil.rmtree(tmp2, ignore_errors=True)


# ── _git_head ──────────────────────────────────────────────────────────────────

class TestGitHead(unittest.TestCase):
    def test_returns_none_for_nonexistent_dir(self):
        result = _git_head("/nonexistent/path/no/git/here")
        self.assertIsNone(result)

    def test_returns_none_for_non_git_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = _git_head(d)
            self.assertIsNone(result)

    def test_returns_commit_for_real_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            # Initialise a minimal git repo with one commit
            subprocess.run(["git", "init", d], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", d, "config", "user.email", "test@test"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", d, "config", "user.name", "Test"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            open(os.path.join(d, "f"), "w").close()
            subprocess.run(["git", "-C", d, "add", "."], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", d, "commit", "-m", "init"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            head = _git_head(d)
            self.assertIsNotNone(head)
            self.assertEqual(len(head), 40)
            self.assertTrue(all(c in "0123456789abcdef" for c in head))


# ── verify_package ─────────────────────────────────────────────────────────────

class TestVerifyPackage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _entry(self, **kw):
        base = {
            "package": "Foo",
            "version": "1.0",
            "revision": "1",
            "hash": "aabb001122",
            "tarball": "Foo-1.0-1.slc9_x86-64.tar.gz",
            "tarball_sha256": "",
            "outcome": "built",
        }
        base.update(kw)
        return base

    def _plant_tarball(self, content: bytes, entry: dict, arch: str) -> str:
        rel = _store_rel(entry["hash"], entry["tarball"], arch)
        return _write_file(os.path.join(self.tmp, rel), content)

    def test_skip_when_no_tarball_key(self):
        entry = self._entry(tarball=None, tarball_sha256=None, outcome="")
        status, detail = verify_package(entry, "slc9_x86-64", [self.tmp])
        self.assertEqual(status, SKIP)

    def test_skip_when_already_installed_no_tarball(self):
        entry = self._entry(tarball=None, tarball_sha256=None, outcome="already_installed")
        status, detail = verify_package(entry, "slc9_x86-64", [self.tmp])
        self.assertEqual(status, SKIP)
        self.assertIn("already_installed", detail)

    def test_miss_when_tarball_not_found(self):
        entry = self._entry(tarball_sha256="abc123")
        status, detail = verify_package(entry, "slc9_x86-64", [self.tmp])
        self.assertEqual(status, MISS)
        self.assertIn("not found", detail)

    def test_pass_when_sha256_matches(self):
        content = b"binary tarball content"
        entry = self._entry(tarball_sha256=_sha256(content))
        self._plant_tarball(content, entry, "slc9_x86-64")
        status, detail = verify_package(entry, "slc9_x86-64", [self.tmp])
        self.assertEqual(status, PASS)
        self.assertIn("OK", detail)

    def test_fail_when_sha256_mismatch(self):
        content = b"binary tarball content"
        entry = self._entry(tarball_sha256="0" * 64)   # wrong hash
        self._plant_tarball(content, entry, "slc9_x86-64")
        status, detail = verify_package(entry, "slc9_x86-64", [self.tmp])
        self.assertEqual(status, FAIL)
        self.assertIn("mismatch", detail)

    def test_searched_paths_shown_in_miss(self):
        entry = self._entry(tarball_sha256="deadbeef" * 8)
        status, detail = verify_package(entry, "slc9_x86-64", [self.tmp, "/other/root"])
        self.assertEqual(status, MISS)
        self.assertIn(self.tmp, detail)


# ── verify_provider ────────────────────────────────────────────────────────────

class TestVerifyProvider(unittest.TestCase):
    def _entry(self, **kw):
        base = {
            "name": "alidist",
            "checkout_dir": "/nonexistent",
            "commit": "a" * 40,
        }
        base.update(kw)
        return base

    def test_skip_when_checkout_missing(self):
        entry = self._entry(checkout_dir="/nonexistent/path")
        status, detail = verify_provider(entry)
        self.assertEqual(status, SKIP)
        self.assertIn("not present", detail)

    def test_skip_when_no_checkout_dir_key(self):
        entry = self._entry(checkout_dir="")
        status, detail = verify_provider(entry)
        self.assertEqual(status, SKIP)

    def test_pass_when_commit_matches(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", d], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", d, "config", "user.name", "T"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            open(os.path.join(d, "f"), "w").close()
            subprocess.run(["git", "-C", d, "add", "."], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", d, "commit", "-m", "init"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            commit = _git_head(d)
            entry = self._entry(checkout_dir=d, commit=commit)
            status, detail = verify_provider(entry)
            self.assertEqual(status, PASS)
            self.assertIn("OK", detail)

    def test_fail_when_commit_mismatch(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", d], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", d, "config", "user.email", "t@t"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", d, "config", "user.name", "T"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            open(os.path.join(d, "f"), "w").close()
            subprocess.run(["git", "-C", d, "add", "."], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "-C", d, "commit", "-m", "init"], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            entry = self._entry(checkout_dir=d, commit="0" * 40)
            status, detail = verify_provider(entry)
            self.assertEqual(status, FAIL)
            self.assertIn("mismatch", detail)

    def test_miss_when_git_head_fails(self):
        with tempfile.TemporaryDirectory() as d:
            # Directory exists but is not a git repo → _git_head returns None
            entry = self._entry(checkout_dir=d, commit="a" * 40)
            status, detail = verify_provider(entry)
            self.assertEqual(status, MISS)


# ── doVerify (integration-level) ──────────────────────────────────────────────

class TestDoVerify(unittest.TestCase):
    """Light integration tests exercising doVerify() end-to-end."""

    # Use the host architecture so the arch-check PASS/FAIL doesn't interfere
    # with exit-code assertions.  detectArch() is also patched to return this
    # same string so the two always agree.
    ARCH = "slc9_x86-64"

    def setUp(self):
        # Patch detectArch so the architecture check always matches ARCH.
        # doVerify imports from bits_helpers.utilities so patch it there.
        patcher = patch("bits_helpers.arch.detectArch", return_value=self.ARCH)
        self.mock_detectArch = patcher.start()
        self.addCleanup(patcher.stop)

    def _make_manifest(self, packages=None, providers=None, arch=None) -> dict:
        return {
            "schema_version": 2,
            "created_at": "2026-01-01T00:00:00Z",
            "status": "success",
            "architecture": arch or self.ARCH,
            "requested_packages": [],
            "packages": packages or [],
            "providers": providers or [],
        }

    def _args(self, manifest_path, *, cvmfs_root=None, work_dir="sw",
              no_providers=False, json_output=False):
        args = MagicMock()
        args.fromManifest = manifest_path
        args.cvmfsRoot = cvmfs_root
        args.workDir = work_dir
        args.noProviders = no_providers
        args.json_output = json_output
        return args

    def test_exits_3_for_missing_manifest(self):
        from bits_helpers.verify import doVerify
        args = self._args("/nonexistent/manifest.json")
        with self.assertRaises(SystemExit) as ctx:
            doVerify(args, None)
        self.assertEqual(ctx.exception.code, 3)

    def test_exits_3_for_malformed_manifest(self):
        from bits_helpers.verify import doVerify
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            fh.write("not json {{")
            path = fh.name
        try:
            args = self._args(path)
            with self.assertRaises(SystemExit) as ctx:
                doVerify(args, None)
            self.assertEqual(ctx.exception.code, 3)
        finally:
            os.unlink(path)

    def test_exits_0_when_all_skip(self):
        """A manifest with no tarballs (already_installed) should exit 0."""
        from bits_helpers.verify import doVerify
        manifest = self._make_manifest(packages=[{
            "package": "Foo", "version": "1.0", "revision": "1",
            "hash": "aabb", "tarball": None, "tarball_sha256": None,
            "outcome": "already_installed",
        }])
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "manifest.json")
            with open(manifest_path, "w") as fh:
                json.dump(manifest, fh)
            args = self._args(manifest_path, work_dir=tmp, no_providers=True)
            with self.assertRaises(SystemExit) as ctx:
                doVerify(args, None)
            self.assertEqual(ctx.exception.code, 0)

    def test_exits_1_on_sha256_mismatch(self):
        from bits_helpers.verify import doVerify
        pkg_hash = "deadbeef01"
        tarball = "Foo-1.0-1.slc9_x86-64.tar.gz"
        content = b"real content"
        wrong_sha = "0" * 64
        with tempfile.TemporaryDirectory() as tmp:
            # Plant the tarball
            rel = _store_rel(pkg_hash, tarball, self.ARCH)
            _write_file(os.path.join(tmp, rel), content)
            manifest = self._make_manifest(packages=[{
                "package": "Foo", "version": "1.0", "revision": "1",
                "hash": pkg_hash, "tarball": tarball,
                "tarball_sha256": wrong_sha,
                "outcome": "built",
            }])
            manifest_path = os.path.join(tmp, "manifest.json")
            with open(manifest_path, "w") as fh:
                json.dump(manifest, fh)
            args = self._args(manifest_path, work_dir=tmp, no_providers=True)
            with self.assertRaises(SystemExit) as ctx:
                doVerify(args, None)
            self.assertEqual(ctx.exception.code, 1)

    def test_exits_0_on_correct_sha256(self):
        from bits_helpers.verify import doVerify
        pkg_hash = "cafebabe01"
        tarball = "Foo-1.0-1.slc9_x86-64.tar.gz"
        content = b"correct content"
        correct_sha = _sha256(content)
        with tempfile.TemporaryDirectory() as tmp:
            rel = _store_rel(pkg_hash, tarball, self.ARCH)
            _write_file(os.path.join(tmp, rel), content)
            manifest = self._make_manifest(packages=[{
                "package": "Foo", "version": "1.0", "revision": "1",
                "hash": pkg_hash, "tarball": tarball,
                "tarball_sha256": correct_sha,
                "outcome": "built",
            }])
            manifest_path = os.path.join(tmp, "manifest.json")
            with open(manifest_path, "w") as fh:
                json.dump(manifest, fh)
            args = self._args(manifest_path, work_dir=tmp, no_providers=True)
            with self.assertRaises(SystemExit) as ctx:
                doVerify(args, None)
            self.assertEqual(ctx.exception.code, 0)

    def test_exits_2_on_missing_tarball(self):
        from bits_helpers.verify import doVerify
        manifest = self._make_manifest(packages=[{
            "package": "Ghost", "version": "0.1", "revision": "1",
            "hash": "000111222", "tarball": "Ghost-0.1-1.slc9_x86-64.tar.gz",
            "tarball_sha256": "a" * 64,
            "outcome": "built",
        }])
        with tempfile.TemporaryDirectory() as tmp:
            manifest_path = os.path.join(tmp, "manifest.json")
            with open(manifest_path, "w") as fh:
                json.dump(manifest, fh)
            args = self._args(manifest_path, work_dir=tmp, no_providers=True)
            with self.assertRaises(SystemExit) as ctx:
                doVerify(args, None)
            self.assertEqual(ctx.exception.code, 2)

    def test_json_output_structure(self):
        """--json flag emits parseable JSON with expected keys."""
        from bits_helpers.verify import doVerify
        import io
        content = b"data"
        pkg_hash = "aabbccdd11"
        tarball = "Pkg-1.0-1.slc9_x86-64.tar.gz"
        with tempfile.TemporaryDirectory() as tmp:
            rel = _store_rel(pkg_hash, tarball, self.ARCH)
            _write_file(os.path.join(tmp, rel), content)
            manifest = self._make_manifest(packages=[{
                "package": "Pkg", "version": "1.0", "revision": "1",
                "hash": pkg_hash, "tarball": tarball,
                "tarball_sha256": _sha256(content),
                "outcome": "built",
            }])
            manifest_path = os.path.join(tmp, "manifest.json")
            with open(manifest_path, "w") as fh:
                json.dump(manifest, fh)
            args = self._args(manifest_path, work_dir=tmp, no_providers=True,
                              json_output=True)
            captured = io.StringIO()
            import sys
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                with self.assertRaises(SystemExit):
                    doVerify(args, None)
            finally:
                sys.stdout = old_stdout
            report = json.loads(captured.getvalue())
        self.assertIn("packages", report)
        self.assertIn("summary", report)
        self.assertIn("exit_code", report)
        self.assertIn("architecture", report)
        self.assertEqual(report["packages"][0]["status"], PASS)


if __name__ == "__main__":
    unittest.main()
