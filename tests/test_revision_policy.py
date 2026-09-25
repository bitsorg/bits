"""Hash revision labels reuse the existing forced-revision path."""

import os
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers.build import create_version_link, storeHashes
from bits_helpers.sync import RsyncRemoteSync
from bits_helpers.packages import getPackageList
from bits_helpers.utilities import ver_rev


class RevisionPolicyTest(unittest.TestCase):
    def specs(self, defaults=None, force=None, overrides=None):
        recipes = {
            "app": "package: app\nversion: '1'\n" + (force or "") + "---\necho build\n",
            "defaults-release": "package: defaults-release\nversion: '1'\n---\n",
        }
        specs = {}
        with patch("bits_helpers.packages.resolveFilename",
                   side_effect=lambda taps, pkg, cfg, gen: (pkg, "/recipes")), \
             patch("bits_helpers.packages.getRecipeReader",
                   side_effect=lambda path, *a, **kw: lambda: recipes[path]), \
             patch("bits_helpers.packages.getGeneratedPackages", return_value={"/recipes": {}}), \
             patch("bits_helpers.packages.load_for_spec", return_value={}):
            getPackageList(
                packages=["app"], specs=specs, configDir="/recipes",
                preferSystem=False, noSystem="*", architecture="slc9_x86-64",
                disable=[], defaults=["release"],
                performPreferCheck=lambda *a: (1, ""),
                performRequirementCheck=lambda *a: (0, ""),
                performValidateDefaults=lambda *a: (True, "", None),
                overrides=overrides or {}, taps={}, log=lambda *a: None,
                defaults_meta=defaults or {},
            )
        for name in ("defaults-release", "app"):
            specs[name].update(commit_hash="0", is_devel_pkg=False)
            storeHashes(name, specs, considerRelocation=False)
            specs[name]["hash"] = specs[name]["remote_revision_hash"]
        return specs

    def test_hash_policy_preserves_identity_and_uses_each_packages_hash(self):
        normal = self.specs()
        hashed = self.specs({"revision_policy": "hash"})
        for name, spec in hashed.items():
            self.assertNotIn("force_revision", normal[name])
            self.assertEqual(spec["remote_revision_hash"], normal[name]["remote_revision_hash"])
            self.assertEqual(spec["force_revision"], spec["remote_revision_hash"])
            self.assertNotEqual(spec["force_revision"], spec["local_revision_hash"])
            spec["revision"] = spec["force_revision"]
            self.assertEqual(ver_rev(spec), "1-" + spec["hash"])
            storeHashes(name, hashed, considerRelocation=False)
            self.assertEqual(spec["force_revision"], spec["hash"])
        self.assertNotEqual(hashed["app"]["force_revision"],
                            hashed["defaults-release"]["force_revision"])

    def test_explicit_revisions_keep_precedence(self):
        for value in ("", "rc1"):
            with self.subTest(value=value):
                spec = self.specs({"revision_policy": "hash"},
                                  force='force_revision: "' + value + '"\n')["app"]
                self.assertEqual(spec["force_revision"], value)
                spec = self.specs({"revision_policy": "hash"},
                                  overrides={"app": {"force_revision": value}})["app"]
                self.assertEqual(spec["force_revision"], value)
                spec = self.specs({"revision_policy": "hash", "force_revision": value})["app"]
                self.assertEqual(spec["force_revision"], value)

    def test_null_global_revision_allows_hash_policy(self):
        spec = self.specs({"revision_policy": "hash", "force_revision": None})["app"]
        self.assertEqual(spec["force_revision"], spec["hash"])

    def test_hash_revision_layout_matches_upload(self):
        spec = self.specs({"revision_policy": "hash"})["app"]
        spec["revision"] = spec["force_revision"]
        arch = "slc9_x86-64"
        filename = "app-1-{}.{}.tar.gz".format(spec["hash"], arch)
        with tempfile.TemporaryDirectory() as workdir:
            create_version_link(spec, arch, workdir)
            link = os.path.join(workdir, "TARS", arch, "app", filename)
            self.assertEqual(os.readlink(link), "../../{}/store/{}/{}/{}".format(
                arch, spec["hash"][:2], spec["hash"], filename))
            sync = RsyncRemoteSync("remote", "remote", arch, workdir)
            command = sync.upload_shell_command(spec)
            self.assertIn(filename, command)
            self.assertIn("store/{}/{}".format(spec["hash"][:2], spec["hash"]), command)
