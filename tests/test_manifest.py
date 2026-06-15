# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers.manifest — incremental build manifest.

Coverage:
    BuildManifest.__init__      — file created, schema correct, status in_progress
    add_providers               — provider entries recorded with remote_url
    add_package                 — all three outcomes; tarball checksum captured
    complete / fail             — status transitions; fail records package + reason
    _save (atomic write)        — os.replace is called
    load classmethod            — round-trip JSON load
    latest symlink              — updated after each save
    incremental (partial build) — manifest useful even if complete() never called
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

# Ensure the repo root is on sys.path.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bits_helpers.manifest import BuildManifest, _git_remote_url, _now_iso


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_spec(pkg="MyPkg", version="1.0", revision="1",
               pkg_hash="abcd1234" * 5, commit_hash="deadbeef" * 5,
               sources=None):
    spec = {
        "package":     pkg,
        "version":     version,
        "revision":    revision,
        "hash":        pkg_hash,
        "commit_hash": commit_hash,
    }
    if sources is not None:
        spec["sources"] = sources
    return spec


def _write_file(path: str, content: bytes = b"fake tarball content") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(content)
    return path


def _sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _make_manifest(tmp):
    """Return a BuildManifest initialised in *tmp*."""
    return BuildManifest(
        work_dir           = tmp,
        requested_packages = ["ROOT"],
        architecture       = "slc7_x86-64",
        defaults           = ["release"],
        config_dir         = tmp,
        config_commit      = "abc123",
    )


# ── __init__ ──────────────────────────────────────────────────────────────────

class TestManifestInit(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, m):
        with open(m.path) as fh:
            return json.load(fh)

    def test_file_created(self):
        m = _make_manifest(self.tmp)
        self.assertTrue(os.path.isfile(m.path))

    def test_schema_version(self):
        m = _make_manifest(self.tmp)
        data = self._load(m)
        self.assertEqual(data["schema_version"], BuildManifest.SCHEMA_VERSION)

    def test_status_in_progress(self):
        m = _make_manifest(self.tmp)
        data = self._load(m)
        self.assertEqual(data["status"], "in_progress")

    def test_requested_packages(self):
        m = _make_manifest(self.tmp)
        data = self._load(m)
        self.assertEqual(data["requested_packages"], ["ROOT"])

    def test_architecture(self):
        m = _make_manifest(self.tmp)
        data = self._load(m)
        self.assertEqual(data["architecture"], "slc7_x86-64")

    def test_empty_providers_and_packages(self):
        m = _make_manifest(self.tmp)
        data = self._load(m)
        self.assertEqual(data["providers"], [])
        self.assertEqual(data["packages"], [])

    def test_path_in_work_dir(self):
        m = _make_manifest(self.tmp)
        self.assertTrue(m.path.startswith(self.tmp))
        self.assertIn("bits-manifest-", os.path.basename(m.path))

    def test_filename_contains_target(self):
        """The manifest filename must embed the build target name."""
        m = BuildManifest(
            work_dir=self.tmp,
            requested_packages=["ROOT"],
            architecture="slc7_x86-64",
            defaults=["release"],
            config_dir=self.tmp,
            config_commit="abc123",
            target="ROOT",
        )
        self.assertIn("ROOT", os.path.basename(m.path))

    def test_filename_without_target(self):
        """When no target is given the filename is still valid (no double-dash)."""
        m = BuildManifest(
            work_dir=self.tmp,
            requested_packages=["ROOT"],
            architecture="slc7_x86-64",
            defaults=["release"],
            config_dir=self.tmp,
            config_commit="abc123",
            target="",
        )
        basename = os.path.basename(m.path)
        self.assertTrue(basename.startswith("bits-manifest-"))
        # No double-dash should appear (e.g. "bits-manifest--20260411...")
        self.assertNotIn("--", basename)

    def test_filename_target_sanitised(self):
        """Characters unsafe for filenames are replaced with underscores."""
        m = BuildManifest(
            work_dir=self.tmp,
            requested_packages=["pkg/bad name!"],
            architecture="slc7_x86-64",
            defaults=["release"],
            config_dir=self.tmp,
            config_commit="abc123",
            target="pkg/bad name!",
        )
        basename = os.path.basename(m.path)
        self.assertNotIn("/", basename)
        self.assertNotIn(" ", basename)
        self.assertNotIn("!", basename)
        self.assertIn("pkg_bad_name_", basename)

    def test_latest_symlink_created(self):
        m = _make_manifest(self.tmp)
        latest = os.path.join(m.manifest_dir, BuildManifest._LATEST_SYMLINK)
        self.assertTrue(os.path.islink(latest))
        self.assertEqual(os.readlink(latest), os.path.basename(m.path))

    def test_atomic_write_used(self):
        real_replace = os.replace
        with patch("bits_helpers.manifest.os.replace") as mock_replace:
            mock_replace.side_effect = real_replace
            m = _make_manifest(self.tmp)
            self.assertTrue(mock_replace.called)


# ── add_providers ─────────────────────────────────────────────────────────────

class TestAddProviders(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, m):
        with open(m.path) as fh:
            return json.load(fh)

    def test_empty_providers_dict_is_noop(self):
        m = _make_manifest(self.tmp)
        m.add_providers({})
        data = self._load(m)
        self.assertEqual(data["providers"], [])

    def test_provider_recorded(self):
        m = _make_manifest(self.tmp)
        checkout = os.path.join(self.tmp, "prov1")
        os.makedirs(checkout, exist_ok=True)
        with patch("bits_helpers.manifest._git_remote_url", return_value="https://example.com/repo"):
            m.add_providers({checkout: ("my-provider", "deadbeef" * 5)})
        data = self._load(m)
        self.assertEqual(len(data["providers"]), 1)
        p = data["providers"][0]
        self.assertEqual(p["name"], "my-provider")
        self.assertEqual(p["commit"], "deadbeef" * 5)
        self.assertEqual(p["remote_url"], "https://example.com/repo")

    def test_multiple_providers_recorded(self):
        m = _make_manifest(self.tmp)
        dirs = {}
        for i in range(3):
            d = os.path.join(self.tmp, "prov%d" % i)
            os.makedirs(d, exist_ok=True)
            dirs[d] = ("provider-%d" % i, "aa%02d" % i * 20)
        with patch("bits_helpers.manifest._git_remote_url", return_value=None):
            m.add_providers(dirs)
        data = self._load(m)
        self.assertEqual(len(data["providers"]), 3)

    def test_remote_url_none_on_failure(self):
        m = _make_manifest(self.tmp)
        checkout = os.path.join(self.tmp, "prov_norepo")
        os.makedirs(checkout, exist_ok=True)
        # No git repo → _git_remote_url should return None.
        m.add_providers({checkout: ("norepo", "0" * 40)})
        data = self._load(m)
        self.assertIsNone(data["providers"][0]["remote_url"])


# ── add_package ───────────────────────────────────────────────────────────────

class TestAddPackage(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.spec = _make_spec()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, m):
        with open(m.path) as fh:
            return json.load(fh)

    def test_already_installed(self):
        m = _make_manifest(self.tmp)
        m.add_package(self.spec, "already_installed")
        data = self._load(m)
        self.assertEqual(len(data["packages"]), 1)
        pkg = data["packages"][0]
        self.assertEqual(pkg["outcome"], "already_installed")
        self.assertEqual(pkg["package"], "MyPkg")
        self.assertIsNone(pkg["tarball"])
        self.assertIsNone(pkg["tarball_sha256"])

    def test_from_store_without_tarball(self):
        m = _make_manifest(self.tmp)
        m.add_package(self.spec, "from_store")
        data = self._load(m)
        pkg = data["packages"][0]
        self.assertEqual(pkg["outcome"], "from_store")
        self.assertIsNone(pkg["tarball_sha256"])

    def test_from_store_with_tarball(self):
        content = b"store tarball bytes"
        tar_path = os.path.join(self.tmp, "MyPkg-1.0-1.slc7_x86-64.tar.gz")
        _write_file(tar_path, content)
        m = _make_manifest(self.tmp)
        m.add_package(self.spec, "from_store", tar_path)
        data = self._load(m)
        pkg = data["packages"][0]
        self.assertEqual(pkg["tarball"], "MyPkg-1.0-1.slc7_x86-64.tar.gz")
        self.assertEqual(pkg["tarball_sha256"], _sha256(content))

    def test_built_from_source(self):
        content = b"fresh build bytes"
        tar_path = os.path.join(self.tmp, "MyPkg-1.0-1.slc7_x86-64.tar.gz")
        _write_file(tar_path, content)
        m = _make_manifest(self.tmp)
        m.add_package(self.spec, "built_from_source", tar_path)
        data = self._load(m)
        pkg = data["packages"][0]
        self.assertEqual(pkg["outcome"], "built_from_source")
        self.assertEqual(pkg["tarball_sha256"], _sha256(content))

    def test_patches_and_variables_recorded(self):
        # v3: patches (name + recorded checksum) and resolved variables.
        spec = _make_spec()
        spec["patches"] = ["fix-a.patch", "fix-b.patch"]
        spec["patch_checksums"] = {"fix-a.patch": "sha256:aaa"}  # b has no checksum
        spec["variables"] = {"foo": "bar", "ver": "1.2"}
        m = _make_manifest(self.tmp)
        m.add_package(spec, "built_from_source")
        pkg = self._load(m)["packages"][0]
        self.assertEqual(pkg["patches"], [
            {"name": "fix-a.patch", "checksum": "sha256:aaa"},
            {"name": "fix-b.patch", "checksum": None},
        ])
        self.assertEqual(pkg["variables"], {"foo": "bar", "ver": "1.2"})

    def test_patches_and_variables_default_empty(self):
        # A package with no patches/variables gets empty list / empty dict.
        m = _make_manifest(self.tmp)
        m.add_package(self.spec, "already_installed")
        pkg = self._load(m)["packages"][0]
        self.assertEqual(pkg["patches"], [])
        self.assertEqual(pkg["variables"], {})

    def test_schema_version_is_3(self):
        m = _make_manifest(self.tmp)
        self.assertEqual(self._load(m)["schema_version"], 3)

    def test_multiple_packages_recorded(self):
        m = _make_manifest(self.tmp)
        for i in range(5):
            m.add_package(_make_spec(pkg="Pkg%d" % i), "already_installed")
        data = self._load(m)
        self.assertEqual(len(data["packages"]), 5)
        names = [p["package"] for p in data["packages"]]
        self.assertEqual(names, ["Pkg%d" % i for i in range(5)])

    def test_incremental_save_after_each_package(self):
        """Each add_package call must persist to disk immediately."""
        m = _make_manifest(self.tmp)
        for i in range(3):
            m.add_package(_make_spec(pkg="Pkg%d" % i), "already_installed")
            data = self._load(m)
            # After the (i+1)th call, i+1 packages should be on disk.
            self.assertEqual(len(data["packages"]), i + 1)

    def test_missing_tarball_path_gives_null_sha256(self):
        m = _make_manifest(self.tmp)
        m.add_package(self.spec, "from_store", "/nonexistent/path.tar.gz")
        data = self._load(m)
        self.assertIsNone(data["packages"][0]["tarball_sha256"])


# ── complete / fail ───────────────────────────────────────────────────────────

class TestLifecycle(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, m):
        with open(m.path) as fh:
            return json.load(fh)

    def test_complete_sets_status(self):
        m = _make_manifest(self.tmp)
        m.complete()
        data = self._load(m)
        self.assertEqual(data["status"], "complete")

    def test_fail_sets_status(self):
        m = _make_manifest(self.tmp)
        m.fail("BadPkg", "build script exited 1")
        data = self._load(m)
        self.assertEqual(data["status"], "failed")

    def test_fail_records_package_name(self):
        m = _make_manifest(self.tmp)
        m.fail("BadPkg")
        data = self._load(m)
        self.assertEqual(data["failed_package"], "BadPkg")

    def test_fail_records_reason(self):
        m = _make_manifest(self.tmp)
        m.fail("BadPkg", "build script exited 1")
        data = self._load(m)
        self.assertEqual(data["failure_reason"], "build script exited 1")

    def test_fail_without_package_name(self):
        m = _make_manifest(self.tmp)
        m.fail()
        data = self._load(m)
        self.assertEqual(data["status"], "failed")
        self.assertNotIn("failed_package", data)

    def test_partial_build_readable(self):
        """Manifest should be readable and useful even without complete()."""
        m = _make_manifest(self.tmp)
        m.add_package(_make_spec("PkgA"), "from_store")
        # Do NOT call complete() — simulate a crash mid-build.
        data = self._load(m)
        self.assertEqual(data["status"], "in_progress")
        self.assertEqual(len(data["packages"]), 1)

    def test_updated_at_advances(self):
        m = _make_manifest(self.tmp)
        data_before = self._load(m)
        m.add_package(_make_spec(), "already_installed")
        data_after = self._load(m)
        # updated_at should be >= created_at (may be equal in fast tests).
        self.assertGreaterEqual(data_after["updated_at"], data_before["created_at"])


# ── load classmethod ──────────────────────────────────────────────────────────

class TestLoad(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_round_trip(self):
        m = _make_manifest(self.tmp)
        m.add_package(_make_spec("ZLib"), "from_store")
        m.complete()
        loaded = BuildManifest.load(m.path)
        self.assertEqual(loaded["status"], "complete")
        self.assertEqual(loaded["requested_packages"], ["ROOT"])
        self.assertEqual(loaded["packages"][0]["package"], "ZLib")

    def test_load_returns_dict(self):
        m = _make_manifest(self.tmp)
        result = BuildManifest.load(m.path)
        self.assertIsInstance(result, dict)


# ── latest symlink ────────────────────────────────────────────────────────────

class TestLatestSymlink(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_latest_points_to_manifest(self):
        m = _make_manifest(self.tmp)
        latest = os.path.join(m.manifest_dir, BuildManifest._LATEST_SYMLINK)
        target = os.readlink(latest)
        self.assertEqual(target, os.path.basename(m.path))

    def test_latest_updated_after_add_package(self):
        m = _make_manifest(self.tmp)
        m.add_package(_make_spec(), "already_installed")
        latest = os.path.join(m.manifest_dir, BuildManifest._LATEST_SYMLINK)
        self.assertTrue(os.path.islink(latest))
        # Reading via the symlink must work and match the real manifest.
        with open(latest) as fh:
            loaded = json.load(fh)
        self.assertEqual(len(loaded["packages"]), 1)

    def test_two_manifests_latest_points_to_second(self):
        """If two BuildManifest objects are created in the same dir (rare but
        possible in tests), the symlink should point to the most recently
        written one."""
        m1 = _make_manifest(self.tmp)
        import time; time.sleep(0.01)  # ensure distinct timestamps
        m2 = _make_manifest(self.tmp)
        latest = os.path.join(m2.manifest_dir, BuildManifest._LATEST_SYMLINK)
        self.assertEqual(os.readlink(latest), os.path.basename(m2.path))


# ── _git_remote_url ───────────────────────────────────────────────────────────

class TestGitRemoteUrl(unittest.TestCase):

    def test_returns_none_for_non_git_dir(self):
        with tempfile.TemporaryDirectory() as td:
            url = _git_remote_url(td)
            self.assertIsNone(url)

    def test_returns_url_for_git_repo(self):
        """Create a minimal git repo with a remote and verify URL retrieval."""
        with tempfile.TemporaryDirectory() as td:
            subprocess.run(["git", "init"], cwd=td, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                ["git", "remote", "add", "origin", "https://example.com/test.git"],
                cwd=td, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            url = _git_remote_url(td)
            self.assertEqual(url, "https://example.com/test.git")


# ── source_checksums ──────────────────────────────────────────────────────────

class TestSourceChecksums(unittest.TestCase):
    """Verify that source archive checksums are embedded in PackageEntry."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _load(self, m):
        with open(m.path) as fh:
            return json.load(fh)

    def test_no_sources_gives_empty_list(self):
        """A spec without a sources field must produce an empty source_checksums list."""
        m = _make_manifest(self.tmp)
        m.add_package(_make_spec(), "built_from_source")
        pkg = self._load(m)["packages"][0]
        self.assertEqual(pkg["source_checksums"], [])

    def test_source_with_checksum_recorded(self):
        """A sources entry with a declared checksum is split correctly."""
        spec = _make_spec(sources=[
            "https://example.com/libfoo-1.2.tar.gz,sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        ])
        m = _make_manifest(self.tmp)
        m.add_package(spec, "built_from_source")
        pkg = self._load(m)["packages"][0]
        self.assertEqual(len(pkg["source_checksums"]), 1)
        entry = pkg["source_checksums"][0]
        self.assertEqual(entry["url"], "https://example.com/libfoo-1.2.tar.gz")
        self.assertEqual(
            entry["checksum"],
            "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )

    def test_source_without_checksum_gives_null(self):
        """A sources entry with no declared checksum must record checksum=null."""
        spec = _make_spec(sources=["https://example.com/libbar-3.1.tar.xz"])
        m = _make_manifest(self.tmp)
        m.add_package(spec, "built_from_source")
        pkg = self._load(m)["packages"][0]
        entry = pkg["source_checksums"][0]
        self.assertEqual(entry["url"], "https://example.com/libbar-3.1.tar.xz")
        self.assertIsNone(entry["checksum"])

    def test_multiple_sources_all_recorded(self):
        """All entries in the sources list are captured in order."""
        spec = _make_spec(sources=[
            "https://example.com/main-1.0.tar.gz,sha256:aaaa" + "0" * 60,
            "https://example.com/extra.tar.gz",
            "fix-build.patch,sha256:bbbb" + "0" * 60,
        ])
        m = _make_manifest(self.tmp)
        m.add_package(spec, "built_from_source")
        pkg = self._load(m)["packages"][0]
        sc = pkg["source_checksums"]
        self.assertEqual(len(sc), 3)
        self.assertIsNotNone(sc[0]["checksum"])
        self.assertIsNone(sc[1]["checksum"])
        self.assertIsNotNone(sc[2]["checksum"])

    def test_url_with_comma_in_query_string(self):
        """A URL containing a comma but no checksum suffix must not be mangled."""
        url = "https://example.com/download?a=1,2&b=3"
        spec = _make_spec(sources=[url])
        m = _make_manifest(self.tmp)
        m.add_package(spec, "built_from_source")
        pkg = self._load(m)["packages"][0]
        entry = pkg["source_checksums"][0]
        self.assertEqual(entry["url"], url)
        self.assertIsNone(entry["checksum"])

    def test_schema_version_current(self):
        """Schema version reflects the latest additions (v3: patches + variables)."""
        m = _make_manifest(self.tmp)
        data = self._load(m)
        self.assertEqual(data["schema_version"], 3)


if __name__ == "__main__":
    unittest.main()
