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

    def upload_file(self, local, bucket, key):
        with open(local) as fh:
            self.uploads.append((bucket, key, json.load(fh)))


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
                 "completed_at": "2026-01-01T00:00:00Z", "outcome": "built_from_source"},
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
        self.assertTrue(
            re.fullmatch(r"MANIFESTS/%s/[^/]+-\d{8}T\d{6}Z-[0-9a-f]+\.json" % re.escape(build_id), key),
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
        self.assertTrue(top["tarball"])
        self.assertTrue(top["tarball_sha256"].startswith("sha256:"))

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

    def test_trigger_certification_creates_pipeline(self):
        from types import SimpleNamespace
        import bits_helpers.forge as forge
        args = SimpleNamespace(
            manifestsRemote="ssh://git@gitlab.cern.ch:7999/buncic/bits-manifests.git",
            gitlabToken="tok", certifyRef="main")
        with patch.object(forge, "resolve_gitlab_token", return_value="tok"), \
             patch.object(forge, "gitlab_create_pipeline",
                          return_value={"id": 9, "web_url": "https://gl/p/9"}) as gp:
            publish._trigger_certification(args, _Parser())
        a, k = gp.call_args
        self.assertEqual(a[0], "https://gitlab.cern.ch/api/v4")   # derived API URL
        self.assertEqual(a[2], "buncic/bits-manifests")           # derived project
        self.assertEqual(k.get("ref"), "main")

    def test_trigger_certification_needs_token(self):
        from types import SimpleNamespace
        import bits_helpers.forge as forge
        args = SimpleNamespace(
            manifestsRemote="ssh://git@gitlab.cern.ch:7999/buncic/bits-manifests.git",
            gitlabToken=None, certifyRef="main")
        with patch.object(forge, "resolve_gitlab_token", return_value=None):
            with self.assertRaises(_Parser._Err):
                publish._trigger_certification(args, _Parser())

    def test_run_leaf_is_unique_per_call(self):
        # Distinct hosts/runs must not collide: the leaf carries host + UTC stamp.
        leaves = {publish._run_leaf() for _ in range(3)}
        for leaf in leaves:
            self.assertRegex(leaf, r"^[A-Za-z0-9._-]+-\d{8}T\d{6}Z-[0-9a-f]+\.json$")


if __name__ == "__main__":
    unittest.main()
