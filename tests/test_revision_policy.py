"""Hash revision labels reuse the existing forced-revision path."""

import os
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers.build import create_version_link, storeHashes
from bits_helpers.sync import RsyncRemoteSync
from bits_helpers.sync import HttpRemoteSync
from bits_helpers.packages import getPackageList
from bits_helpers.utilities import resolve_store_path, ver_rev
from bits_helpers.rev_index import marker_key, revision_from_tarball, revision_of
from bits_helpers.status import _scan_local_tars


class RevisionPolicyTest(unittest.TestCase):
    def specs(self, defaults=None, force=None, overrides=None, local_policy=False):
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
            specs[name]["revision_policy_local"] = local_policy
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
            if name != "defaults-release":   # #121: its local hash is its remote hash
                self.assertNotEqual(spec["force_revision"], spec["local_revision_hash"])
            spec["revision"] = spec["force_revision"]
            self.assertEqual(ver_rev(spec), "1-" + spec["hash"])
            storeHashes(name, hashed, considerRelocation=False)
            self.assertEqual(spec["force_revision"], spec["hash"])
        self.assertNotEqual(hashed["app"]["force_revision"],
                            hashed["defaults-release"]["force_revision"])

    def test_hash_policy_uses_local_hash_when_revision_would_be_local(self):
        local = self.specs({"revision_policy": "hash"}, local_policy=True)
        for spec in local.values():
            self.assertEqual(spec["force_revision"], spec["local_revision_hash"])
            self.assertNotEqual(spec["force_revision"], spec["remote_revision_hash"])

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

    def test_hash_revision_round_trips_through_marker_and_local_caches(self):
        spec = self.specs({"revision_policy": "hash"})["app"]
        spec["revision"] = spec["force_revision"]
        arch = "slc9_x86-64"
        filename = "app-1-{}.{}.tar.gz".format(spec["hash"], arch)
        marker = marker_key(arch, "app", "1", spec["revision"])
        self.assertEqual(revision_from_tarball(filename, "app", "1", arch),
                         spec["revision"])
        self.assertEqual(revision_of(marker, arch, "app", "1"),
                         spec["revision"])

        with tempfile.TemporaryDirectory() as workdir:
            create_version_link(spec, arch, workdir)
            store_dir = os.path.join(workdir, resolve_store_path(arch, spec["hash"]))
            os.makedirs(store_dir)
            open(os.path.join(store_dir, filename), "wb").close()
            spec["remote_hashes"] = [spec["hash"]]
            spec["local_hashes"] = []
            self.assertTrue(_scan_local_tars(spec, workdir, arch))

            # The HTTP backend's early local-cache path must recognize the
            # uploaded content-object basename and avoid a remote request.
            sync = HttpRemoteSync("https://example.invalid", arch, workdir, False)
            with patch("bits_helpers.sync.requests.get",
                       side_effect=AssertionError("unexpected network fetch")):
                sync.fetch_tarball(spec)


class RevisionPolicyReviewTest(unittest.TestCase):
    """Follow-ups from the review of revision_policy: hash."""

    ARCH = "slc9_x86-64"
    HASH = "ab" + "c0" * 19   # a hex label that starts with letters lstrip("local") ate

    def test_pick_revision_accepts_a_hash_label(self):
        from bits_helpers.plan import pick_revision
        spec = {"package": "app", "version": "1"}
        names = ["app-1-%s.%s.tar.gz" % (self.HASH, self.ARCH)]
        self.assertEqual(pick_revision(names, spec, self.ARCH, forced=self.HASH), self.HASH)
        names += ["app-1-2.%s.tar.gz" % self.ARCH, "app-1-10.%s.tar.gz" % self.ARCH]
        self.assertEqual(pick_revision(names, spec, self.ARCH), "2")
        self.assertEqual(pick_revision(["app-1-local3.%s.tar.gz" % self.ARCH,
                                        "app-1-local12.%s.tar.gz" % self.ARCH],
                                       spec, self.ARCH, local=True), "local3")

    def test_devel_package_keeps_counter_revisions(self):
        from bits_helpers.hashing import _apply_revision_policy
        spec = {"revision_policy": "hash", "remote_revision_hash": self.HASH,
                "is_devel_pkg": True}
        _apply_revision_policy(spec)
        self.assertNotIn("force_revision", spec)
        spec["is_devel_pkg"] = False
        _apply_revision_policy(spec)
        self.assertEqual(spec["force_revision"], self.HASH)

    def test_untracked_label_fatal_only_under_hash_policy(self):
        from bits_helpers import build
        specs = {"u": {}, "h": {"revision_policy": "hash"},
                 "ok": {"revision_policy": "hash", "force_revision": ""}}
        with patch.object(build, "warning") as warn, \
             patch.object(build, "dieOnError") as die:
            build.check_untracked_labels(specs, ["u", "ok"])
            self.assertEqual(warn.call_count, 1)
            self.assertFalse(any(c.args[0] for c in die.call_args_list))
        with patch.object(build, "warning"), \
             patch.object(build, "dieOnError",
                          side_effect=lambda cond, msg: (_ for _ in ()).throw(SystemExit(msg)) if cond else None):
            with self.assertRaises(SystemExit):
                build.check_untracked_labels(specs, ["h"])

    def test_build_link_scan_parses_hash_labels(self):
        import re
        from bits_helpers.build import tarball_link_regex, tarball_target_regex
        spec = {"package": "app", "version": "1"}
        for rev in (self.HASH, "3", "local2", None):
            name = "app-1%s.%s.tar.gz" % ("-" + rev if rev else "", self.ARCH)
            with self.subTest(rev=rev):
                self.assertTrue(tarball_link_regex(spec, self.ARCH).fullmatch(name))
                target = "../../%s/store/ab/%s/%s" % (self.ARCH, "ab" * 20, name)
                self.assertEqual(re.match(tarball_target_regex(spec, self.ARCH), target).groups(),
                                 ("ab" * 20, rev))
        self.assertIsNone(tarball_link_regex(spec, self.ARCH).fullmatch(
            "app-1-v2.%s.tar.gz" % self.ARCH))   # a sibling version, not a revision
