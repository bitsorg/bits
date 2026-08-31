# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers/status.py — bits status command."""

import json
import os
import re
import tempfile
import unittest
from collections import OrderedDict
from io import StringIO
from unittest.mock import MagicMock, patch

from bits_helpers.status import (
    ALREADY_INSTALLED,
    BUILD_FROM_SOURCE,
    FROM_REMOTE_STORE,
    FROM_STORE,
    HASH_UNKNOWN,
    LOCAL_CHECKOUT,
    LOCAL_CHECKOUT_UNCHANGED,
    _classify,
    _emit_json,
    _emit_table,
    _is_already_installed,
    _resolve_commit_hash,
    _scan_local_tars,
    _try_populate_refs,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_spec(pkg="mylib", version="1.0", tag="v1.0",
               is_devel=False, source="https://github.com/ex/mylib.git",
               remote_hashes=None, local_hashes=None,
               remote_revision_hash=None, local_revision_hash=None,
               devel_hash="", deps_hash=""):
    """Build a minimal spec dict for testing."""
    rh = remote_revision_hash or "aabbcc" + "0" * 34
    lh = local_revision_hash or "ddeeff" + "0" * 34
    spec = OrderedDict({
        "package":               pkg,
        "version":               version,
        "tag":                   tag,
        "source":                source,
        "recipe":                "#!/bin/bash\nmake",
        "requires":              [],
        "build_requires":        [],
        "runtime_requires":      [],
        "hash":                  rh,
        "remote_revision_hash":  rh,
        "local_revision_hash":   lh,
        "remote_hashes":         remote_hashes or [rh],
        "local_hashes":          local_hashes or [lh],
        "is_devel_pkg":          is_devel,
        "devel_hash":            devel_hash,
        "deps_hash":             deps_hash,
        "scm_refs":              {},
        "commit_hash":           "0",
        "revision":              "1",
    })
    if is_devel:
        spec["devel_hash"] = devel_hash
    return spec


def _write_build_hash(work_dir: str, arch: str, pkg: str, version: str,
                      revision: str, hash_value: str) -> str:
    """Write a .build-hash sentinel file under INSTALLROOT and return its path."""
    install_path = os.path.join(work_dir, arch, pkg, "{}-{}".format(version, revision))
    os.makedirs(install_path, exist_ok=True)
    hash_file = os.path.join(install_path, ".build-hash")
    with open(hash_file, "w") as fh:
        fh.write(hash_value)
    return hash_file


def _write_tars_symlink(work_dir: str, arch: str, pkg: str, version: str,
                        revision: str, hash_value: str) -> None:
    """Create the TARS symlink tree for a package tarball."""
    tarball_name = "{}-{}-{}.{}.tar.gz".format(pkg, version, revision, arch)
    store_dir = os.path.join(work_dir, "TARS", arch, "store",
                             hash_value[:2], hash_value)
    os.makedirs(store_dir, exist_ok=True)
    tarball_path = os.path.join(store_dir, tarball_name)
    with open(tarball_path, "wb") as fh:
        fh.write(b"fake tarball")
    symlink_dir = os.path.join(work_dir, "TARS", arch, pkg)
    os.makedirs(symlink_dir, exist_ok=True)
    symlink_path = os.path.join(symlink_dir, tarball_name)
    rel_target = os.path.join(
        "../..", arch, "store", hash_value[:2], hash_value, tarball_name
    )
    if not os.path.exists(symlink_path):
        os.symlink(rel_target, symlink_path)


# ── _try_populate_refs ─────────────────────────────────────────────────────────

class TestTryPopulateRefs(unittest.TestCase):
    def test_no_mirror_sets_empty_refs(self):
        spec = _make_spec()
        with tempfile.TemporaryDirectory() as tmpdir:
            _try_populate_refs(spec, tmpdir, "mylib")
        self.assertEqual(spec["scm_refs"], {})

    def test_existing_mirror_populated_via_git(self):
        """When a mirror exists, refs are read from it via listRefsCmd."""
        spec = _make_spec()
        fake_refs = {"refs/heads/main": "abc123", "refs/tags/v1.0": "def456"}
        scm_mock = MagicMock()
        scm_mock.exec.return_value = (0, "abc123 refs/heads/main\ndef456 refs/tags/v1.0\n")
        scm_mock.listRefsCmd.return_value = ["git", "ls-remote", "."]
        scm_mock.parseRefs.return_value = fake_refs
        spec["scm"] = scm_mock
        with tempfile.TemporaryDirectory() as tmpdir:
            mirror = os.path.join(tmpdir, "mylib")
            os.makedirs(mirror)
            _try_populate_refs(spec, tmpdir, "mylib")
        self.assertEqual(spec["scm_refs"], fake_refs)

    def test_git_failure_falls_back_to_empty(self):
        """If the git command fails, scm_refs stays empty (no crash)."""
        spec = _make_spec()
        scm_mock = MagicMock()
        scm_mock.exec.side_effect = RuntimeError("git exploded")
        scm_mock.listRefsCmd.return_value = ["git", "ls-remote", "."]
        spec["scm"] = scm_mock
        with tempfile.TemporaryDirectory() as tmpdir:
            mirror = os.path.join(tmpdir, "mylib")
            os.makedirs(mirror)
            _try_populate_refs(spec, tmpdir, "mylib")
        self.assertEqual(spec.get("scm_refs"), {})


# ── _resolve_commit_hash ───────────────────────────────────────────────────────

class TestResolveCommitHash(unittest.TestCase):
    def test_branch_ref_resolved(self):
        spec = _make_spec(tag="main")
        spec["scm_refs"] = {"refs/heads/main": "deadbeef"}
        _resolve_commit_hash(spec)
        self.assertEqual(spec["commit_hash"], "deadbeef")

    def test_tag_falls_back_to_tag_string(self):
        spec = _make_spec(tag="v1.0")
        spec["scm_refs"] = {}
        _resolve_commit_hash(spec)
        self.assertEqual(spec["commit_hash"], "v1.0")

    def test_no_source_sets_zero(self):
        spec = _make_spec()
        del spec["source"]
        spec["scm_refs"] = {}
        _resolve_commit_hash(spec)
        self.assertEqual(spec["commit_hash"], "0")

    def test_date_tag_expanded(self):
        spec = _make_spec(tag="v%(year)s-1")
        spec["scm_refs"] = {}
        _resolve_commit_hash(spec)
        # The tag should be expanded (year substituted)
        self.assertNotIn("%(year)s", spec["tag"])


# ── _is_already_installed ──────────────────────────────────────────────────────

class TestIsAlreadyInstalled(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.arch = "slc9_x86-64"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matching_hash_returns_true(self):
        spec = _make_spec(pkg="zlib", version="1.2.13", remote_revision_hash="aabb" + "0" * 36)
        spec["hash"] = spec["remote_revision_hash"]
        spec["revision"] = "1"
        _write_build_hash(self.tmp, self.arch, "zlib", "1.2.13", "1", spec["hash"])
        self.assertTrue(_is_already_installed(spec, self.tmp, self.arch))

    def test_mismatched_hash_returns_false(self):
        spec = _make_spec(pkg="zlib", version="1.2.13", remote_revision_hash="aabb" + "0" * 36)
        spec["hash"] = spec["remote_revision_hash"]
        spec["revision"] = "1"
        _write_build_hash(self.tmp, self.arch, "zlib", "1.2.13", "1", "differenthash")
        self.assertFalse(_is_already_installed(spec, self.tmp, self.arch))

    def test_no_hash_file_returns_false(self):
        spec = _make_spec(pkg="boost", version="1.83.0", remote_revision_hash="ccdd" + "0" * 36)
        spec["hash"] = spec["remote_revision_hash"]
        spec["revision"] = "1"
        self.assertFalse(_is_already_installed(spec, self.tmp, self.arch))

    def test_cvmfs_symlink_returns_true(self):
        """A symlink that resolves to an existing directory → CVMFS, always installed."""
        spec = _make_spec(pkg="root", version="6.32.0", remote_revision_hash="eeff" + "0" * 36)
        spec["hash"] = spec["remote_revision_hash"]
        spec["revision"] = "1"
        real_dir = os.path.join(self.tmp, "cvmfs_real", "root")
        os.makedirs(real_dir)
        install_parent = os.path.join(self.tmp, self.arch, "root")
        os.makedirs(install_parent, exist_ok=True)
        link_path = os.path.join(install_parent, "6.32.0-1")
        os.symlink(real_dir, link_path)
        self.assertTrue(_is_already_installed(spec, self.tmp, self.arch))


# ── _scan_local_tars ──────────────────────────────────────────────────────────

class TestScanLocalTars(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.arch = "slc9_x86-64"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_matching_remote_hash_returns_true(self):
        rh = "aabbccddeeff" + "0" * 28
        spec = _make_spec(pkg="mylib", version="1.0",
                          remote_revision_hash=rh, remote_hashes=[rh])
        spec["hash"] = rh
        spec["revision"] = "1"
        _write_tars_symlink(self.tmp, self.arch, "mylib", "1.0", "1", rh)
        self.assertTrue(_scan_local_tars(spec, self.tmp, self.arch))

    def test_no_tarball_returns_false(self):
        rh = "aabbccddeeff" + "0" * 28
        spec = _make_spec(pkg="mylib", version="1.0",
                          remote_revision_hash=rh, remote_hashes=[rh])
        spec["hash"] = rh
        spec["revision"] = "1"
        self.assertFalse(_scan_local_tars(spec, self.tmp, self.arch))

    def test_wrong_hash_returns_false(self):
        rh_good = "aabbccddeeff" + "0" * 28
        rh_bad  = "112233445566" + "0" * 28
        spec = _make_spec(pkg="mylib", version="1.0",
                          remote_revision_hash=rh_good, remote_hashes=[rh_good])
        spec["hash"] = rh_good
        spec["revision"] = "1"
        _write_tars_symlink(self.tmp, self.arch, "mylib", "1.0", "1", rh_bad)
        self.assertFalse(_scan_local_tars(spec, self.tmp, self.arch))

    def test_local_hash_matches_local_symlink(self):
        rh = "aabbccddeeff" + "0" * 28
        lh = "ddeeff112233" + "0" * 28
        spec = _make_spec(pkg="mylib", version="1.0",
                          remote_revision_hash=rh, local_revision_hash=lh,
                          remote_hashes=[rh], local_hashes=[lh])
        spec["hash"] = lh
        spec["revision"] = "local1"
        # Write a "local" revision tarball
        tarball_name = "mylib-1.0-local1.{}.tar.gz".format(self.arch)
        store_dir = os.path.join(self.tmp, "TARS", self.arch, "store", lh[:2], lh)
        os.makedirs(store_dir, exist_ok=True)
        with open(os.path.join(store_dir, tarball_name), "wb") as fh:
            fh.write(b"data")
        symlink_dir = os.path.join(self.tmp, "TARS", self.arch, "mylib")
        os.makedirs(symlink_dir, exist_ok=True)
        rel = os.path.join("../..", self.arch, "store", lh[:2], lh, tarball_name)
        os.symlink(rel, os.path.join(symlink_dir, tarball_name))
        self.assertTrue(_scan_local_tars(spec, self.tmp, self.arch))

    def test_dangling_symlink_ignored(self):
        rh = "aabbccddeeff" + "0" * 28
        spec = _make_spec(pkg="mylib", version="1.0",
                          remote_revision_hash=rh, remote_hashes=[rh])
        spec["hash"] = rh
        spec["revision"] = "1"
        # Create the symlink dir but no real tarball file
        symlink_dir = os.path.join(self.tmp, "TARS", self.arch, "mylib")
        os.makedirs(symlink_dir, exist_ok=True)
        tarball_name = "mylib-1.0-1.{}.tar.gz".format(self.arch)
        rel = os.path.join("../..", self.arch, "store", rh[:2], rh, tarball_name)
        os.symlink(rel, os.path.join(symlink_dir, tarball_name))
        # Symlink points nowhere (no actual tarball created)
        self.assertFalse(_scan_local_tars(spec, self.tmp, self.arch))


# ── _classify ─────────────────────────────────────────────────────────────────

class TestClassify(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.arch = "slc9_x86-64"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _spec(self, **kwargs):
        return _make_spec(**kwargs)

    def test_already_installed(self):
        rh = "aabb" + "0" * 36
        spec = self._spec(pkg="zlib", version="1.2.13",
                          remote_revision_hash=rh, remote_hashes=[rh])
        spec["hash"] = rh
        spec["revision"] = "1"
        _write_build_hash(self.tmp, self.arch, "zlib", "1.2.13", "1", rh)
        self.assertEqual(_classify(spec, self.tmp, self.arch), ALREADY_INSTALLED)

    def test_from_store(self):
        rh = "ccdd" + "0" * 36
        spec = self._spec(pkg="boost", version="1.83.0",
                          remote_revision_hash=rh, remote_hashes=[rh])
        spec["hash"] = rh
        spec["revision"] = "1"
        _write_tars_symlink(self.tmp, self.arch, "boost", "1.83.0", "1", rh)
        self.assertEqual(_classify(spec, self.tmp, self.arch), FROM_STORE)

    def test_build_from_source(self):
        rh = "eeff" + "0" * 36
        spec = self._spec(pkg="ROOT", version="6.32.06",
                          remote_revision_hash=rh, remote_hashes=[rh])
        spec["hash"] = rh
        spec["revision"] = "1"
        self.assertEqual(_classify(spec, self.tmp, self.arch), BUILD_FROM_SOURCE)

    def test_local_checkout_will_rebuild(self):
        spec = self._spec(pkg="MyAnalysis", version="dev", is_devel=True,
                          devel_hash="newhash", deps_hash="")
        spec["hash"] = "anything"
        spec["revision"] = "1"
        # old_devel_hash = "0" (no file) → mismatch → LOCAL_CHECKOUT
        self.assertEqual(_classify(spec, self.tmp, self.arch), LOCAL_CHECKOUT)

    def test_local_checkout_unchanged(self):
        rh = "ffgg" + "0" * 36
        spec = self._spec(pkg="O2Physics", version="dev", is_devel=True,
                          devel_hash="stable", deps_hash="")
        spec["hash"] = rh
        spec["revision"] = "1"
        # Write the sentinel file with matching combined hash
        sentinel_dir = os.path.join(self.tmp, "BUILD", rh, "O2Physics")
        os.makedirs(sentinel_dir, exist_ok=True)
        with open(os.path.join(sentinel_dir, ".build_succeeded"), "w") as fh:
            fh.write("stable")   # devel_hash + deps_hash
        self.assertEqual(_classify(spec, self.tmp, self.arch), LOCAL_CHECKOUT_UNCHANGED)

    def test_already_installed_takes_priority_over_from_store(self):
        """If both installed and tarball exist, already_installed wins."""
        rh = "aabb" + "0" * 36
        spec = self._spec(pkg="cmake", version="3.27.0",
                          remote_revision_hash=rh, remote_hashes=[rh])
        spec["hash"] = rh
        spec["revision"] = "1"
        _write_build_hash(self.tmp, self.arch, "cmake", "3.27.0", "1", rh)
        _write_tars_symlink(self.tmp, self.arch, "cmake", "3.27.0", "1", rh)
        self.assertEqual(_classify(spec, self.tmp, self.arch), ALREADY_INSTALLED)

    def test_remote_store_detection(self):
        """With a sync_helper mock that returns a tarball, state is from_remote_store."""
        rh = "9900" + "0" * 36
        spec = self._spec(pkg="geant4", version="11.2.0",
                          remote_revision_hash=rh, remote_hashes=[rh])
        spec["hash"] = rh
        spec["revision"] = "1"
        # Create a fake tarball in the TARS store path after fetch_tarball is called
        def fake_fetch(s):
            tar_dir = os.path.join(
                self.tmp, "TARS", self.arch, "store", rh[:2], rh
            )
            os.makedirs(tar_dir, exist_ok=True)
            with open(os.path.join(tar_dir, "geant4-11.2.0-1.{}.tar.gz".format(self.arch)), "wb") as f:
                f.write(b"data")
        sync_mock = MagicMock()
        sync_mock.fetch_tarball.side_effect = fake_fetch
        result = _classify(spec, self.tmp, self.arch, sync_helper=sync_mock)
        self.assertEqual(result, FROM_REMOTE_STORE)


# ── Output formatters ──────────────────────────────────────────────────────────

class TestEmitJson(unittest.TestCase):
    def _rows(self):
        return [
            {"package": "zlib", "version": "1.2.13", "hash": "aabbcc",
             "state": ALREADY_INSTALLED, "state_label": "already installed"},
            {"package": "boost", "version": "1.83.0", "hash": "ddeeff",
             "state": FROM_STORE, "state_label": "from store (local tarball)"},
            {"package": "ROOT", "version": "6.32.06", "hash": "112233",
             "state": BUILD_FROM_SOURCE, "state_label": "build from source"},
        ]

    def test_json_structure(self):
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            _emit_json(self._rows(), "slc9_x86-64")
            data = json.loads(mock_out.getvalue())
        self.assertEqual(data["architecture"], "slc9_x86-64")
        self.assertEqual(len(data["packages"]), 3)
        self.assertEqual(data["packages"][0]["package"], "zlib")
        self.assertEqual(data["packages"][0]["state"], ALREADY_INSTALLED)
        self.assertEqual(data["packages"][2]["state"], BUILD_FROM_SOURCE)

    def test_all_fields_present(self):
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            _emit_json(self._rows(), "slc9_x86-64")
            data = json.loads(mock_out.getvalue())
        for pkg in data["packages"]:
            for field in ("package", "version", "hash", "state"):
                self.assertIn(field, pkg)


class TestEmitTable(unittest.TestCase):
    def _rows(self):
        return [
            {"package": "zlib", "version": "1.2.13", "hash": "aabbcc",
             "state": ALREADY_INSTALLED, "state_label": "already installed"},
            {"package": "ROOT", "version": "6.32.06", "hash": "112233",
             "state": BUILD_FROM_SOURCE, "state_label": "build from source"},
            {"package": "MyPkg", "version": "dev (dev)", "hash": "",
             "state": LOCAL_CHECKOUT, "state_label": "local checkout   (will rebuild)"},
        ]

    def test_table_contains_package_names(self):
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            _emit_table(self._rows(), "slc9_x86-64")
            output = mock_out.getvalue()
        self.assertIn("zlib", output)
        self.assertIn("ROOT", output)
        self.assertIn("MyPkg", output)

    def test_table_contains_states(self):
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            _emit_table(self._rows(), "slc9_x86-64")
            output = mock_out.getvalue()
        self.assertIn("already installed", output)
        self.assertIn("build from source", output)

    def test_empty_rows_handled(self):
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            _emit_table([], "slc9_x86-64")
            output = mock_out.getvalue()
        self.assertIn("No packages", output)

    def test_summary_line_present(self):
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            _emit_table(self._rows(), "slc9_x86-64")
            output = mock_out.getvalue()
        self.assertIn("Summary:", output)

    def test_architecture_in_header(self):
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            _emit_table(self._rows(), "slc9_x86-64")
            output = mock_out.getvalue()
        self.assertIn("slc9_x86-64", output)


# ── doStatus integration ───────────────────────────────────────────────────────

class TestDoStatus(unittest.TestCase):
    """Integration-level tests for doStatus using a minimal fake recipe repo."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.arch = "slc9_x86-64"
        self.work_dir = os.path.join(self.tmp, "sw")
        os.makedirs(self.work_dir)
        # Create the recipes dir so doStatus's configDir existence guard passes
        # in tests that mock getPackageList rather than populating real recipes.
        os.makedirs(os.path.join(self.tmp, "recipes"), exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_args(self, pkgname, json_output=False, check_store=False,
                   force_tracked=True, no_devel=None, disable=None,
                   force_rebuild=None, fetch_repos=False):
        import argparse
        args = argparse.Namespace(
            pkgname        = pkgname,
            defaults       = ["release"],
            architecture   = self.arch,
            workDir        = self.work_dir,
            configDir      = os.path.join(self.tmp, "recipes"),
            chdir          = ".",
            referenceSources = os.path.join(self.work_dir, "MIRROR"),
            noDevel        = no_devel or [],
            forceTracked   = force_tracked,
            disable        = disable or [],
            force_rebuild  = force_rebuild or [],
            fetchRepos     = fetch_repos,
            remoteStore    = "",
            no_remote_store = False,
            checkStore     = check_store,
            json_output    = json_output,
        )
        return args

    def _make_recipe_dir(self, pkg, version="1.0", tag=None, requires=None):
        """Write a minimal .sh recipe for *pkg* into the fake recipe repo."""
        config_dir = os.path.join(self.tmp, "recipes")
        bits_dir = os.path.join(config_dir, "common.bits")
        os.makedirs(bits_dir, exist_ok=True)
        tag = tag or "v{}".format(version)
        deps_yaml = ""
        if requires:
            deps_yaml = "\nrequires:\n" + "".join(
                "  - {}\n".format(r) for r in requires
            )
        recipe_text = (
            "package: {pkg}\n"
            "version: \"{version}\"\n"
            "source: https://github.com/example/{pkg}.git\n"
            "tag: {tag}\n"
            "{deps}"
            "---\n"
            "make -j${{JOBS:-1}}\n"
        ).format(pkg=pkg, version=version, tag=tag, deps=deps_yaml)
        recipe_file = os.path.join(bits_dir, "{}.sh".format(pkg))
        with open(recipe_file, "w") as fh:
            fh.write(recipe_text)
        # Write a minimal defaults-release.sh
        defaults_file = os.path.join(bits_dir, "defaults-release.sh")
        if not os.path.exists(defaults_file):
            with open(defaults_file, "w") as fh:
                fh.write("package: defaults-release\nversion: \"1\"\n---\n")
        return config_dir

    @patch("bits_helpers.status.getPackageList")
    @patch("bits_helpers.status.parseDefaults")
    @patch("bits_helpers.status.readDefaults")
    @patch("bits_helpers.hashing.storeHashes")
    @patch("bits_helpers.build.storeHook")
    def test_build_from_source_reported(self, mock_hook, mock_store_hashes,
                                        mock_read_defaults, mock_parse_defaults,
                                        mock_get_package_list):
        """A package with no installed hash and no tarball → build_from_source."""
        rh = "aabbcc" + "0" * 34
        mock_parse_defaults.return_value = (None, {}, {}, {})
        mock_read_defaults.return_value = None

        spec = _make_spec(pkg="mylib", version="1.0",
                          remote_revision_hash=rh, remote_hashes=[rh])
        mock_get_package_list.return_value = ([], ["mylib"], set(), None)

        import bits_helpers.status as status_mod
        # Inject spec into the specs dict passed to getPackageList
        def fake_get_pkg_list(*args, **kwargs):
            kwargs["specs"]["mylib"] = spec
            return ([], ["mylib"], set(), None)
        mock_get_package_list.side_effect = fake_get_pkg_list

        def fake_store_hashes(p, specs, considerRelocation):
            specs[p]["remote_revision_hash"] = rh
            specs[p]["local_revision_hash"]  = "local" + rh
            specs[p]["remote_hashes"] = [rh]
            specs[p]["local_hashes"]  = ["local" + rh]
            specs[p]["deps_hash"] = ""

        mock_store_hashes.side_effect = fake_store_hashes

        args = self._make_args(["mylib"], json_output=True)
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            with patch("bits_helpers.status.topological_sort", return_value=["mylib"]):
                with patch("bits_helpers.status.compute_combined_arch", return_value=self.arch):
                    with patch("bits_helpers.status.prunePaths"):
                        import argparse
                        parser = argparse.ArgumentParser()
                        parser.error = lambda msg: (_ for _ in ()).throw(SystemExit(msg))
                        status_mod.doStatus(args, parser)
            data = json.loads(mock_out.getvalue())
        self.assertEqual(data["packages"][0]["state"], BUILD_FROM_SOURCE)

    @patch("bits_helpers.status.getPackageList")
    @patch("bits_helpers.status.parseDefaults")
    @patch("bits_helpers.status.readDefaults")
    @patch("bits_helpers.hashing.storeHashes")
    @patch("bits_helpers.build.storeHook")
    def test_already_installed_reported(self, mock_hook, mock_store_hashes,
                                        mock_read_defaults, mock_parse_defaults,
                                        mock_get_package_list):
        """A package with matching .build-hash → already_installed."""
        rh = "ccddee" + "0" * 34
        mock_parse_defaults.return_value = (None, {}, {}, {})
        mock_read_defaults.return_value = None

        spec = _make_spec(pkg="zlib", version="1.2.13",
                          remote_revision_hash=rh, remote_hashes=[rh])

        def fake_get_pkg_list(*args, **kwargs):
            kwargs["specs"]["zlib"] = spec
            return ([], ["zlib"], set(), None)
        mock_get_package_list.side_effect = fake_get_pkg_list

        def fake_store_hashes(p, specs, considerRelocation):
            specs[p]["remote_revision_hash"] = rh
            specs[p]["local_revision_hash"]  = "local" + rh
            specs[p]["remote_hashes"] = [rh]
            specs[p]["local_hashes"]  = ["local" + rh]
            specs[p]["hash"] = rh
            specs[p]["deps_hash"] = ""

        mock_store_hashes.side_effect = fake_store_hashes

        # Write a matching .build-hash file
        _write_build_hash(self.work_dir, self.arch, "zlib", "1.2.13", "1", rh)

        args = self._make_args(["zlib"], json_output=True)
        import bits_helpers.status as status_mod
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            with patch("bits_helpers.status.topological_sort", return_value=["zlib"]):
                with patch("bits_helpers.status.compute_combined_arch", return_value=self.arch):
                    with patch("bits_helpers.status.prunePaths"):
                        import argparse
                        parser = argparse.ArgumentParser()
                        parser.error = lambda msg: (_ for _ in ()).throw(SystemExit(msg))
                        status_mod.doStatus(args, parser)
            data = json.loads(mock_out.getvalue())
        self.assertEqual(data["packages"][0]["state"], ALREADY_INSTALLED)

    @patch("bits_helpers.status.getPackageList")
    @patch("bits_helpers.status.parseDefaults")
    @patch("bits_helpers.status.readDefaults")
    @patch("bits_helpers.hashing.storeHashes")
    @patch("bits_helpers.build.storeHook")
    def test_json_output_structure(self, mock_hook, mock_store_hashes,
                                   mock_read_defaults, mock_parse_defaults,
                                   mock_get_package_list):
        """JSON output has architecture + packages array with required fields."""
        rh = "ffeedd" + "0" * 34
        mock_parse_defaults.return_value = (None, {}, {}, {})
        mock_read_defaults.return_value = None
        spec = _make_spec(pkg="cmake", version="3.27.0",
                          remote_revision_hash=rh, remote_hashes=[rh])

        def fake_get_pkg_list(*args, **kwargs):
            kwargs["specs"]["cmake"] = spec
            return ([], ["cmake"], set(), None)
        mock_get_package_list.side_effect = fake_get_pkg_list

        def fake_store_hashes(p, specs, considerRelocation):
            specs[p]["remote_revision_hash"] = rh
            specs[p]["local_revision_hash"]  = "local" + rh
            specs[p]["remote_hashes"] = [rh]
            specs[p]["local_hashes"]  = ["local" + rh]
            specs[p]["deps_hash"] = ""

        mock_store_hashes.side_effect = fake_store_hashes

        import bits_helpers.status as status_mod
        args = self._make_args(["cmake"], json_output=True)
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            with patch("bits_helpers.status.topological_sort", return_value=["cmake"]):
                with patch("bits_helpers.status.compute_combined_arch", return_value=self.arch):
                    with patch("bits_helpers.status.prunePaths"):
                        import argparse
                        parser = argparse.ArgumentParser()
                        parser.error = lambda msg: (_ for _ in ()).throw(SystemExit(msg))
                        status_mod.doStatus(args, parser)
            data = json.loads(mock_out.getvalue())
        self.assertIn("architecture", data)
        self.assertIn("packages", data)
        pkg = data["packages"][0]
        for field in ("package", "version", "hash", "state"):
            self.assertIn(field, pkg)

    @patch("bits_helpers.status.getPackageList")
    @patch("bits_helpers.status.parseDefaults")
    @patch("bits_helpers.status.readDefaults")
    @patch("bits_helpers.hashing.storeHashes")
    @patch("bits_helpers.build.storeHook")
    def test_hash_unknown_on_storeHashes_failure(self, mock_hook, mock_store_hashes,
                                                  mock_read_defaults, mock_parse_defaults,
                                                  mock_get_package_list):
        """If storeHashes raises, the package is reported as hash_unknown."""
        mock_parse_defaults.return_value = (None, {}, {}, {})
        mock_read_defaults.return_value = None
        spec = _make_spec(pkg="brokenlib", version="0.1")

        def fake_get_pkg_list(*args, **kwargs):
            kwargs["specs"]["brokenlib"] = spec
            return ([], ["brokenlib"], set(), None)
        mock_get_package_list.side_effect = fake_get_pkg_list
        mock_store_hashes.side_effect = RuntimeError("cannot compute hash")

        import bits_helpers.status as status_mod
        args = self._make_args(["brokenlib"], json_output=True)
        with patch("sys.stdout", new_callable=StringIO) as mock_out:
            with patch("bits_helpers.status.topological_sort", return_value=["brokenlib"]):
                with patch("bits_helpers.status.compute_combined_arch", return_value=self.arch):
                    with patch("bits_helpers.status.prunePaths"):
                        with patch("bits_helpers.status.warning"):
                            import argparse
                            parser = argparse.ArgumentParser()
                            parser.error = lambda msg: (_ for _ in ()).throw(SystemExit(msg))
                            status_mod.doStatus(args, parser)
            data = json.loads(mock_out.getvalue())
        self.assertEqual(data["packages"][0]["state"], HASH_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
