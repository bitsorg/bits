# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the S3 BOM-manifest publish path (bits_helpers.publish).

Covers the P0 concurrency/identity guarantees:
  * the manifest is keyed MANIFESTS/<build_id>/<host>-<UTC>.json — a folder per
    (deterministic) release plus a unique per-run leaf, so concurrent publishes
    never overwrite each other and one build is findable in the namespace;
  * config pseudo-packages (defaults-*) and packages with no local store tarball
    are excluded;
  * the uploaded BOM carries who/when/where provenance.
"""

import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import bits_helpers.publish as publish
import bits_helpers.sync as sync
from bits_helpers.provenance import build_id_from_manifest
from bits_helpers.utilities import resolve_store_path


class _Parser:
    class _Err(Exception):
        pass

    def error(self, msg):
        raise self._Err(msg)


class _FakeS3:
    def __init__(self):
        self.uploads = []
        self.texts = {}          # key -> str (NOTICE / source-offer uploads)

    def upload_file(self, local, bucket, key):
        with open(local) as fh:
            self.uploads.append((bucket, key, json.load(fh)))

    def put_object(self, Bucket, Key, Body):
        self.texts[Key] = Body.decode("utf-8")


class _FakeWriter:
    """Stands in for the boto3 remote sync: records tarball + manifest uploads."""

    def __init__(self):
        self.s3 = _FakeS3()
        self.writeStore = "mybucket"
        self.tarballs = []

    def upload_symlinks_and_tarball(self, spec):
        self.tarballs.append(spec["hash"])


ARCH = "slc7_x86-64"


def _write_store_tarball(work_dir, arch, h, name):
    d = os.path.join(work_dir, resolve_store_path(arch, h))
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name)
    with open(p, "wb") as fh:
        fh.write(b"tarball-" + h.encode())
    return p


class TestPublishFromManifest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work = os.path.join(self.tmp, "sw")
        man_dir = os.path.join(self.work, "MANIFESTS")
        os.makedirs(man_dir)
        # Top: freshly built, has a store tarball -> published.
        # defaults-release: config pseudo-package -> excluded even with a hash.
        # NoTar: real package but no local store tarball -> skipped.
        self.manifest = {
            "defaults": ["release", "gcc15"],
            "architecture": ARCH,
            "packages": [
                {"package": "Top", "version": "1", "revision": "local1",
                 "hash": "aa11", "effective_architecture": ARCH,
                 "commit_hash": "c0ffee", "built_by": "me@host",
                 "completed_at": "2026-01-01T00:00:00Z", "outcome": "built_from_source",
                 "license": "MIT"},
                # Restricted: has a store tarball, but redistributable: false —
                # must be skipped from the upload AND from the BOM.
                {"package": "Secret", "version": "3", "revision": "local1",
                 "hash": "cc44", "effective_architecture": ARCH,
                 "license": "LicenseRef-Secret", "redistributable": False},
                {"package": "defaults-release", "version": "1", "revision": "1",
                 "hash": "dd22", "effective_architecture": ARCH},
                {"package": "NoTar", "version": "9", "revision": "local1",
                 "hash": "bb33", "effective_architecture": ARCH},
            ],
        }
        self.man_path = os.path.join(man_dir, "bits-manifest-Top-20260101T000000Z.json")
        with open(self.man_path, "w") as fh:
            json.dump(self.manifest, fh)
        _write_store_tarball(self.work, ARCH, "aa11", "Top-1-local1.slc7_x86-64.tar.gz")
        _write_store_tarball(self.work, ARCH, "cc44", "Secret-3-local1.slc7_x86-64.tar.gz")
        # defaults-release also has a tarball on disk, to prove exclusion is by
        # name, not by tarball absence.
        _write_store_tarball(self.work, ARCH, "dd22", "defaults-release-1-1.tar.gz")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self):
        self.writer = _FakeWriter()
        with patch.object(sync, "remote_from_url", return_value=self.writer):
            publish._publish_from_manifest(
                ARCH, self.work, "https://s3.example/mybucket", _Parser(),
                manifest=self.man_path)
        return self.writer

    def test_only_real_packages_with_tarballs_uploaded(self):
        w = self._run()
        # Only Top's tarball is uploaded (defaults excluded, NoTar skipped).
        self.assertEqual(w.tarballs, ["aa11"])

    def test_manifest_key_namespaced_by_build_id_and_unique_leaf(self):
        w = self._run()
        bucket, key, _ = w.s3.uploads[-1]
        self.assertEqual(bucket, "mybucket")
        build_id = build_id_from_manifest(self.manifest)
        self.assertTrue(build_id.startswith("release_gcc15-"))
        # The leaf carries the BOM's effective architecture: BOMs are
        # per-platform so certification can be scoped by platform.
        self.assertTrue(
            re.fullmatch(r"MANIFESTS/%s/[^/]+-\d{8}T\d{6}Z-[0-9a-f]+\.[^/]+\.json"
                         % re.escape(build_id), key),
            "unexpected key: %s" % key)

    def test_bom_has_provenance_and_excludes_config(self):
        w = self._run()
        _, _, bom = w.s3.uploads[-1]
        self.assertEqual(bom["build_id"], build_id_from_manifest(self.manifest))
        self.assertIn("@", bom["published_by"])
        self.assertRegex(bom["published_at"], r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
        names = [p["package"] for p in bom["packages"]]
        self.assertEqual(names, ["Top"])
        top = bom["packages"][0]
        self.assertEqual(top["built_by"], "me@host")
        self.assertEqual(top["completed_at"], "2026-01-01T00:00:00Z")
        # Compliance metadata travels with the BOM (license feeds NOTICE).
        self.assertEqual(top["license"], "MIT")
        self.assertTrue(top["tarball"])
        self.assertTrue(top["tarball_sha256"].startswith("sha256:"))

    def test_release_compliance_files_uploaded(self):
        # NOTICE + LICENSE-SOURCE-OFFER.txt land next to the release's BOMs;
        # the NOTICE lists the distributed package and the excluded one.
        w = self._run()
        build_id = build_id_from_manifest(self.manifest)
        notice_txt = w.s3.texts.get("MANIFESTS/%s/NOTICE" % build_id, "")
        self.assertIn("Top", notice_txt)
        self.assertIn("MIT", notice_txt)
        self.assertIn("Secret", notice_txt)          # excluded-section entry
        self.assertIn("Not included in this distribution", notice_txt)
        self.assertIn("MANIFESTS/%s/LICENSE-SOURCE-OFFER.txt" % build_id,
                      w.s3.texts)

    def test_non_redistributable_never_uploaded_nor_in_bom(self):
        # redistributable: false — the binary must not reach the (possibly
        # world-readable) store, and what is not in the store cannot be in the
        # BOM: Secret has a real store tarball on disk, yet neither its hash is
        # uploaded nor does any BOM entry mention it.
        w = self._run()
        self.assertEqual(w.tarballs, ["aa11"])            # no cc44
        _, _, bom = w.s3.uploads[-1]
        self.assertEqual([p["package"] for p in bom["packages"]], ["Top"])

    def test_cvmfs_publish_refuses_non_redistributable(self):
        # The enforcement point for `redistributable: false`: a per-package
        # CVMFS publish must SKIP such a package (QGRAF, Oracle client) before
        # touching its install tree — it stays in the private store only.
        from types import SimpleNamespace
        args = SimpleNamespace(
            publishView=None, fromManifest=None, package="Secret", version=None,
            workDir=self.work, architecture=ARCH, cvmfsTarget="/cvmfs/x",
            scratchDir=None,
            prepubUrl="https://prepub.example.org", prepubToken=None, prepubRepo=None, prepubPath=None,
            prepubWebhook=None, prepubPollInterval=10, prepubTimeout=1800,
            prepubNoVerifyTls=False, dryRun=False,
        )
        with patch.object(publish, "_find_installroot",
                          side_effect=AssertionError("must gate BEFORE locating "
                                                     "the install tree")):
            publish.doPublish(args, _Parser())   # returns without publishing

    def test_partial_publish_writes_no_bom(self):
        # If any package fails to upload, no BOM manifest is emitted (a partial
        # publish must not look like a complete, certifiable build).
        self.writer = _FakeWriter()

        def _boom(spec):
            raise RuntimeError("upload failed")

        self.writer.upload_symlinks_and_tarball = _boom
        with patch.object(sync, "remote_from_url", return_value=self.writer):
            with self.assertRaises(SystemExit):
                publish._publish_from_manifest(
                    ARCH, self.work, "https://s3.example/mybucket", _Parser(),
                    manifest=self.man_path)
        self.assertEqual(self.writer.s3.uploads, [])   # no BOM on partial publish

    def _cert_args(self, **over):
        from types import SimpleNamespace
        base = dict(manifestsRemote="ssh://git@gitlab.cern.ch:7999/buncic/bits-manifests.git",
                    gitlabToken="tok", certifyRef="main", certifyGroup="ship")
        base.update(over)
        return SimpleNamespace(**base)

    def test_submit_certification_mr_opens_mr(self):
        # One MR / one commit carrying one per-platform BOM file per arch, each
        # file named with its architecture (per-platform certification scoping).
        import bits_helpers.forge as forge
        bom = [("x86_64-el9", {"build_id": "release_gcc15-abc", "packages": []}),
               ("shared", {"build_id": "release_gcc15-abc", "packages": []})]
        with patch.object(forge, "resolve_gitlab_token", return_value="tok"), \
             patch.object(forge, "gitlab_create_commit", return_value={"id": "c1"}) as cc, \
             patch.object(forge, "gitlab_create_merge_request",
                          return_value={"iid": 7, "web_url": "https://gl/mr/7"}) as mr:
            publish._submit_certification_mr(self._cert_args(), _Parser(),
                                             "release_gcc15-abc", bom)
        ca, _ = cc.call_args
        # (api_url, token, project, branch, start_branch, files, content, message)
        self.assertEqual(ca[0], "https://gitlab.cern.ch/api/v4")
        self.assertEqual(ca[2], "buncic/bits-manifests")
        paths = [p for p, _c in ca[5]]
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].startswith(
            "manifests/ship/release_gcc15-abc.x86_64-el9."))
        self.assertTrue(paths[1].startswith(
            "manifests/ship/release_gcc15-abc.shared."))
        ma, mk = mr.call_args
        self.assertEqual(ma[4], "main")                 # target branch

    def test_submit_certification_needs_group(self):
        import bits_helpers.forge as forge
        with patch.object(forge, "resolve_gitlab_token", return_value="tok"):
            with self.assertRaises(_Parser._Err):
                publish._submit_certification_mr(self._cert_args(certifyGroup=None),
                                                 _Parser(), "b", {"packages": []})

    def test_submit_certification_needs_token(self):
        import bits_helpers.forge as forge
        with patch.object(forge, "resolve_gitlab_token", return_value=None):
            with self.assertRaises(_Parser._Err):
                publish._submit_certification_mr(self._cert_args(gitlabToken=None),
                                                 _Parser(), "b", {"packages": []})

    def test_submit_certification_records_certifier(self):
        # A bot opens the MR on behalf of a human whose authority was verified
        # upstream (bits-console): --certifier stamps certified_by into every
        # committed per-arch manifest (audit trail), without mutating the
        # caller's bom.
        import json as _json, bits_helpers.forge as forge
        doc = {"build_id": "b1", "packages": []}
        bom = [("x86_64-el9", doc)]
        with patch.object(forge, "resolve_gitlab_token", return_value="tok"), \
             patch.object(forge, "gitlab_create_commit", return_value={"id": "c1"}) as cc, \
             patch.object(forge, "gitlab_create_merge_request",
                          return_value={"iid": 1, "web_url": "u"}):
            publish._submit_certification_mr(self._cert_args(certifier="alice"),
                                             _Parser(), "b1", bom)
        committed = _json.loads(cc.call_args[0][5][0][1])   # first file's content
        self.assertEqual(committed["certified_by"], ["alice"])
        self.assertNotIn("certified_by", doc)             # caller's dict untouched

    def _dopublish_args(self, **over):
        from types import SimpleNamespace
        base = dict(publishView=None, fromManifest="latest", package=None,
                    dryRun=False, workDir=self.work, architecture=ARCH,
                    publishStore="https://s3.example/mybucket", certify=False,
                    certifyGroup=None, manifestsRemote=None, certifyRef=None,
                    noCertify=False)
        base.update(over)
        return SimpleNamespace(**base)

    def test_system_defaults_imply_certify(self):
        system = {"certify_group": "ship",
                  "manifests_remote": "ssh://git@gitlab.cern.ch:7999/buncic/bits-manifests.git"}
        with patch.object(publish, "_publish_from_manifest",
                          return_value=("bid", {"packages": []}, system)), \
             patch.object(publish, "_submit_certification_mr") as sub:
            args = self._dopublish_args()          # bare publish, nothing on CLI
            publish.doPublish(args, _Parser())
        sub.assert_called_once()
        self.assertEqual(args.certifyGroup, "ship")            # from system:
        self.assertEqual(args.manifestsRemote, system["manifests_remote"])

    def test_no_certify_opts_out_of_configured_certify(self):
        system = {"certify_group": "ship", "manifests_remote": "ssh://h/g/p.git"}
        with patch.object(publish, "_publish_from_manifest",
                          return_value=("bid", {"packages": []}, system)), \
             patch.object(publish, "_submit_certification_mr") as sub:
            publish.doPublish(self._dopublish_args(noCertify=True), _Parser())
        sub.assert_not_called()

    def test_cli_group_implies_certify_without_flag(self):
        with patch.object(publish, "_publish_from_manifest",
                          return_value=("bid", {"packages": []}, {})), \
             patch.object(publish, "_submit_certification_mr") as sub:
            publish.doPublish(self._dopublish_args(certifyGroup="lcg",
                                                   manifestsRemote="ssh://h/g/p.git"),
                              _Parser())
        sub.assert_called_once()

    def test_run_leaf_is_unique_per_call(self):
        # Distinct hosts/runs must not collide: the leaf carries host + UTC stamp.
        leaves = {publish._run_leaf() for _ in range(3)}
        for leaf in leaves:
            self.assertRegex(leaf, r"^[A-Za-z0-9._-]+-\d{8}T\d{6}Z-[0-9a-f]+\.json$")


if __name__ == "__main__":
    unittest.main()
