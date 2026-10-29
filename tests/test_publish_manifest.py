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
        marker = patch.object(publish, "_orchestrator_marker", return_value=None)
        marker.start()                         # the stores here have no orchestrator
        self.addCleanup(marker.stop)
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

    def test_bom_records_the_builds_architecture_not_the_hosts(self):
        self.writer = _FakeWriter()
        with patch.object(sync, "remote_from_url", return_value=self.writer):
            publish._publish_from_manifest(
                "x86_64-el9", self.work, "https://s3.example/mybucket", _Parser(),
                manifest=self.man_path)
        self.assertEqual(self.writer.s3.uploads[-1][2]["architecture"], ARCH)

    def test_bom_carries_what_a_release_needs(self):
        """A release is made later from the BOM alone: the build's templates,
        what it was asked to build, per package its view rules and requires;
        and the pipeline that made it finds it through an index."""
        self.manifest["cvmfs_templates"] = {"packages": "{prefix}/{arch}/Packages/{pkg}/{tag}",
                                            "release": "LCG_110"}
        self.manifest["requested_packages"] = ["Top"]
        self.manifest["packages"][0].update(view={"exclude": ["share/doc"]}, requires=["zlib"])
        with open(self.man_path, "w") as fh:
            json.dump(self.manifest, fh)
        with patch.dict(os.environ, {"CI_PIPELINE_ID": "4242"}):
            w = self._run()
        _, key, bom = w.s3.uploads[-1]
        self.assertEqual(bom["cvmfs_templates"]["release"], "LCG_110")
        self.assertEqual(bom["requested_packages"], ["Top"])
        self.assertEqual(bom["source_pipeline_id"], "4242")
        top = bom["packages"][0]
        self.assertEqual((top["view"], top["requires"]), ({"exclude": ["share/doc"]}, ["zlib"]))
        index = json.loads(w.s3.texts["BUILDS/by-pipeline/4242/%s.json" % bom["build_id"]])
        self.assertEqual(index, {"build_id": bom["build_id"], "keys": [key]})

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

    def test_sboms_uploaded_with_the_stored_sha(self):
        # The store kept an object whose sha differs from the local build's:
        # the SBOM, like the BOM, must describe the stored bytes.
        self.manifest["packages"][0]["tarball_sha256"] = "sha256:" + "1" * 64
        with open(self.man_path, "w") as fh:
            json.dump(self.manifest, fh)
        stored = "sha256:" + "2" * 64

        class Writer(_FakeWriter):
            def upload_symlinks_and_tarball(self, spec):
                super().upload_symlinks_and_tarball(spec)
                spec["store_tarball_sha256"] = stored
        self.writer = w = Writer()
        with patch.object(sync, "remote_from_url", return_value=w):
            publish._publish_from_manifest(ARCH, self.work, "https://s3.example/mybucket",
                                           _Parser(), manifest=self.man_path)
        build_id = build_id_from_manifest(self.manifest)
        cdx = json.loads(w.s3.texts["MANIFESTS/%s/sbom.cdx.json" % build_id])
        spdx = json.loads(w.s3.texts["MANIFESTS/%s/sbom.spdx.json" % build_id])
        top = [c for c in cdx["components"] if c["name"] == "Top"][0]
        self.assertEqual(top["hashes"], [{"alg": "SHA-256", "content": "2" * 64}])
        self.assertEqual(cdx["metadata"]["component"]["name"], build_id)
        self.assertEqual(spdx["name"], build_id)
        # Published + licence-excluded (marked); NoTar never reached the store.
        self.assertEqual(sorted(c["name"] for c in cdx["components"]), ["Secret", "Top"])

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
                    publishStore="https://s3.example/mybucket")
        base.update(over)
        return SimpleNamespace(**base)

    def _certify_args(self, **over):
        from types import SimpleNamespace
        base = dict(fromManifest="latest", dryRun=False, workDir=self.work, architecture=ARCH,
                    publishStore="https://s3.example/mybucket", certifyGroup=None,
                    manifestsRemote=None, certifyRef=None, gitlabToken=None,
                    approval="passkey", console=None, consoleCafile=None,
                    consoleInsecure=False, certifier=None)
        base.update(over)
        return SimpleNamespace(**base)

    def test_publish_only_uploads_even_with_certify_defaults(self):
        # `bits publish` never opens an MR, whatever the defaults configure.
        system = {"certify_group": "ship", "manifests_remote": "ssh://h/g/p.git"}
        with patch.object(publish, "_publish_from_manifest",
                          return_value=("bid", [], system)) as up, \
             patch.object(publish, "_submit_certification_mr") as sub:
            publish.doPublish(self._dopublish_args(), _Parser())
        up.assert_called_once()
        sub.assert_not_called()

    def test_certify_uses_system_defaults_and_approves_before_mr(self):
        calls = []
        bom = [("x86_64-el9", {"build_id": "bid", "packages": []})]
        system = {"certify_group": "ship", "manifests_remote": "ssh://h/g/p.git",
                  "console_url": "https://c"}
        with patch.object(publish, "_publish_from_manifest", return_value=("bid", bom, system)), \
             patch.object(publish, "_submit_certification_mr",
                          side_effect=lambda *a: calls.append("mr")), \
             patch("bits_helpers.forge.resolve_gitlab_token", return_value="tok"), \
             patch("bits_helpers.sign_console.preapprove_via_console",
                   side_effect=lambda *a, **k: calls.append(("pre", a))):
            args = self._certify_args()               # bare `bits certify`
            publish.doCertify(args, _Parser())
        self.assertEqual([c if c == "mr" else c[0] for c in calls], ["pre", "mr"])
        self.assertEqual(calls[0][1][:4], ("https://c", "bid", ["ship"], [bom[0][1]]))
        self.assertEqual((args.certifyGroup, args.manifestsRemote), ("ship", "ssh://h/g/p.git"))

    def test_certify_cli_overrides_defaults(self):
        system = {"certify_group": "ship", "manifests_remote": "ssh://h/g/p.git"}
        with patch.object(publish, "_publish_from_manifest", return_value=("bid", [], system)), \
             patch.object(publish, "_submit_certification_mr") as sub, \
             patch("bits_helpers.forge.resolve_gitlab_token", return_value="tok"):
            args = self._certify_args(certifyGroup="lcg", approval="none", certifier="alice")
            publish.doCertify(args, _Parser())
        sub.assert_called_once()
        self.assertEqual(args.certifyGroup, "lcg")

    def test_certify_approval_none_skips_the_console(self):
        with patch.object(publish, "_publish_from_manifest",
                          return_value=("bid", [], {"certify_group": "g", "manifests_remote": "r"})), \
             patch.object(publish, "_submit_certification_mr") as sub, \
             patch("bits_helpers.forge.resolve_gitlab_token", return_value="tok"), \
             patch("bits_helpers.sign_console.preapprove_via_console") as pre:
            publish.doCertify(self._certify_args(approval="none", certifier="alice"), _Parser())
        pre.assert_not_called()
        sub.assert_called_once()

    def test_certify_approval_none_needs_certifier(self):
        with patch.object(publish, "_publish_from_manifest") as up:
            with self.assertRaises(_Parser._Err):
                publish.doCertify(self._certify_args(approval="none"), _Parser())
        up.assert_not_called()

    def test_certify_nothing_published_is_an_error(self):
        with patch.object(publish, "_publish_from_manifest", return_value=None), \
             patch("bits_helpers.forge.resolve_gitlab_token", return_value="tok"):
            with self.assertRaises(_Parser._Err):
                publish.doCertify(self._certify_args(), _Parser())

    def test_certify_needs_token_before_upload(self):
        with patch.object(publish, "_publish_from_manifest") as up, \
             patch("bits_helpers.forge.resolve_gitlab_token", return_value=None):
            with self.assertRaises(_Parser._Err):
                publish.doCertify(self._certify_args(), _Parser())
        up.assert_not_called()

    def test_certify_passkey_needs_console_and_target(self):
        with patch.object(publish, "_publish_from_manifest", return_value=("bid", [], {})), \
             patch.object(publish, "_submit_certification_mr") as sub, \
             patch("bits_helpers.forge.resolve_gitlab_token", return_value="tok"), \
             patch("bits_helpers.sign_console.preapprove_via_console") as pre, \
             patch("bits_helpers.site.get", return_value=None), \
             patch.dict("os.environ", {"BITS_CONSOLE_URL": ""}):
            with self.assertRaises(_Parser._Err):          # no group / manifests repo
                publish.doCertify(self._certify_args(console="https://c"), _Parser())
            with self.assertRaises(_Parser._Err):          # target set, no console URL
                publish.doCertify(self._certify_args(certifyGroup="lcg",
                                                     manifestsRemote="ssh://h/g/p.git"), _Parser())
        pre.assert_not_called()
        sub.assert_not_called()

    def test_the_console_url_defaults_to_the_site_s(self):
        from bits_helpers import orchestrated
        with patch.dict("os.environ", {"BITS_CONSOLE_URL": ""}):
            self.assertEqual(orchestrated.console_url(), "https://bits.cern.ch")   # 10-CERN.yaml
            self.assertEqual(orchestrated.console_url(system={"console_url": "https://c"}),
                             "https://c")
            with patch("bits_helpers.site.get", return_value=None):
                self.assertIsNone(orchestrated.console_url())

    def test_certify_dry_run_touches_nothing(self):
        with patch.object(publish, "_publish_from_manifest", return_value=None), \
             patch.object(publish, "_submit_certification_mr") as sub, \
             patch("bits_helpers.forge.resolve_gitlab_token", return_value=None):
            publish.doCertify(self._certify_args(dryRun=True), _Parser())
        sub.assert_not_called()

    def test_run_leaf_is_unique_per_call(self):
        # Distinct hosts/runs must not collide: the leaf carries host + UTC stamp.
        leaves = {publish._run_leaf() for _ in range(3)}
        for leaf in leaves:
            self.assertRegex(leaf, r"^[A-Za-z0-9._-]+-\d{8}T\d{6}Z-[0-9a-f]+\.json$")


class _StoreS3:
    """An S3 stand-in that keeps what is written and serves it back."""

    def __init__(self):
        self.objects = {}

    def upload_file(self, local, bucket, key):
        with open(local, "rb") as fh:
            self.objects[key] = fh.read()

    def put_object(self, Bucket, Key, Body):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.encode()

    def get_object(self, Bucket, Key):
        import io
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}


class _StoreWriter:
    def __init__(self, s3, delay=None):
        import threading
        self.s3, self.writeStore, self.tarballs = s3, "mybucket", []
        self.delay, self.lock = delay or {}, threading.Lock()
        self.active = self.peak = 0

    def upload_symlinks_and_tarball(self, spec):
        import time
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(self.delay.get(spec["hash"], 0.01))
        with self.lock:
            self.active -= 1
            self.tarballs.append(spec["hash"])
        spec["store_tarball_sha256"] = "sha256:" + spec["hash"]


class TestPublishOnceAndConcurrently(unittest.TestCase):
    """The per-package store checks run a few at a time, and `bits certify` in
    the publish job uses the BOM the job's BOM step already published."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.work = os.path.join(self.tmp, "sw")
        os.makedirs(os.path.join(self.work, "MANIFESTS"))
        self.hashes = ["%02x%s" % (i, "ab" * 3) for i in range(12)]
        self.manifest = {"defaults": ["release"], "architecture": ARCH, "packages": [
            {"package": "P%d" % i, "version": "1", "revision": "1", "hash": h,
             "effective_architecture": ARCH, "view": {"include": ["bin"]}}
            for i, h in enumerate(self.hashes)]}
        self.man_path = os.path.join(self.work, "MANIFESTS", "bits-manifest-P-20260101T000000Z.json")
        with open(self.man_path, "w") as fh:
            json.dump(self.manifest, fh)
        for i, h in enumerate(self.hashes):
            _write_store_tarball(self.work, ARCH, h, "P%d-1-1.%s.tar.gz" % (i, ARCH))
        self.s3 = _StoreS3()
        env = patch.dict(os.environ, {"CI_PIPELINE_ID": "4242"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _publish(self, writer, **kw):
        with patch.object(sync, "remote_from_url", return_value=writer), \
             patch("bits_helpers.notice.upload_release_compliance"):
            return publish._publish_from_manifest(ARCH, self.work, "https://s3.example/mybucket",
                                                  _Parser(), manifest=self.man_path, **kw)

    def _boms(self):
        return [json.loads(v) for k, v in sorted(self.s3.objects.items())
                if k.startswith("MANIFESTS/") and k.endswith(".json")]

    def test_concurrent_checks_keep_the_bom_in_manifest_order(self):
        # The first packages finish last: the BOM still follows the manifest.
        w = _StoreWriter(self.s3, {h: 0.05 * (12 - i) for i, h in enumerate(self.hashes)})
        with patch.dict(os.environ, {"BITS_PUBLISH_JOBS": "4"}):
            build_id, bom, _system = self._publish(w)
        self.assertEqual(w.peak, 4)
        self.assertEqual(sorted(w.tarballs), sorted(self.hashes))
        self.assertNotEqual(w.tarballs, self.hashes)                  # finished out of order
        self.assertEqual([p["hash"] for p in bom[0][1]["packages"]], self.hashes)
        self.assertEqual(bom[0][1]["packages"][0]["tarball_sha256"], "sha256:" + self.hashes[0])

    def test_certify_uses_the_bom_the_publish_step_wrote(self):
        first = self._publish(_StoreWriter(self.s3))
        boms = len(self._boms())
        again = _StoreWriter(self.s3)
        build_id, bom, _system = self._publish(again, reuse_pipeline_bom=True)
        self.assertEqual(again.tarballs, [])                          # nothing checked again
        self.assertEqual(len(self._boms()), boms)                     # no second BOM
        self.assertEqual((build_id, bom), (first[0], first[1]))

    def test_a_bom_that_is_not_this_publish_is_not_reused(self):
        def tamper(doc):
            doc["packages"][0]["view"] = {"include": ["everything"]}
        def drop(doc):
            doc["packages"].pop()
        def twice(doc):
            doc["packages"][-1] = dict(doc["packages"][0])
        def other_pipeline(doc):
            doc["source_pipeline_id"] = "1"
        def extra_group(doc):
            doc["packages"][0]["group"] = "common"
        def other_layout(doc):
            doc["cvmfs_templates"] = {"package": "/elsewhere"}
        def other_tarball(doc):
            doc["packages"][0]["tarball"] = "P0-1-2.%s.tar.gz" % ARCH
        def other_label(doc):
            doc["effective_architecture"] = "x86_64-el9"
        for change in (tamper, drop, twice, other_pipeline, extra_group, other_layout,
                       other_tarball, other_label):
            with self.subTest(change=change.__name__):
                self.s3.objects.clear()
                self._publish(_StoreWriter(self.s3))
                key = [k for k in self.s3.objects if k.startswith("MANIFESTS/")
                       and k.endswith(ARCH + ".json")][0]
                doc = json.loads(self.s3.objects[key])
                change(doc)
                self.s3.objects[key] = json.dumps(doc).encode()
                again = _StoreWriter(self.s3)
                self._publish(again, reuse_pipeline_bom=True)
                self.assertEqual(sorted(again.tarballs), sorted(self.hashes))   # published afresh

    def test_a_stored_sha256_is_what_the_reused_bom_carries(self):
        # The one thing taken from the store: its sha256 of each tarball.
        self._publish(_StoreWriter(self.s3))
        build_id, bom, _system = self._publish(_StoreWriter(self.s3), reuse_pipeline_bom=True)
        self.assertEqual([p["tarball_sha256"] for p in bom[0][1]["packages"]],
                         ["sha256:" + h for h in self.hashes])

    def test_an_interrupt_stops_the_queue(self):
        class Interrupting(_StoreWriter):
            def upload_symlinks_and_tarball(self, spec):
                if spec["hash"] == self_hashes[0]:
                    raise KeyboardInterrupt
                super().upload_symlinks_and_tarball(spec)
        self_hashes = self.hashes
        w = Interrupting(self.s3, {h: 0.2 for h in self.hashes})
        with patch.dict(os.environ, {"BITS_PUBLISH_JOBS": "2"}):
            with self.assertRaises(KeyboardInterrupt):
                self._publish(w)
        self.assertLess(len(w.tarballs), 4)                           # not all twelve
        self.assertEqual([k for k in self.s3.objects if k.startswith("MANIFESTS/")], [])

    def test_no_reuse_outside_a_pipeline_or_for_bits_publish(self):
        self._publish(_StoreWriter(self.s3))
        plain = _StoreWriter(self.s3)
        self._publish(plain)                                          # bits publish: always
        self.assertEqual(len(plain.tarballs), 12)
        with patch.dict(os.environ, {"CI_PIPELINE_ID": ""}):
            cli = _StoreWriter(self.s3)
            self._publish(cli, reuse_pipeline_bom=True)
        self.assertEqual(len(cli.tarballs), 12)


class TestStoreUploadRedistributable(unittest.TestCase):
    """H1 mitigation: `bits store upload` (_publish_s3) must refuse a package whose
    recipe forbids binary redistribution — mirroring the build-time and bulk gates
    so no upload path leaks a restricted binary into a world-readable store."""

    def _run(self, entry, store="b3://bucket"):
        writer = _FakeWriter()
        with patch.object(publish, "_load_manifest_spec", return_value=entry), \
             patch.object(sync, "remote_from_url", return_value=writer) as rfu, \
             patch.dict(os.environ, {}, clear=False):
            publish._publish_s3(entry["package"], entry.get("version"), ARCH,
                                "/wd", store, _Parser())
            from bits_helpers.sync import bucket_endpoint     # the store's own
            self.endpoint = bucket_endpoint(rfu.call_args[0][0][5:]) if rfu.call_args else None
        self.store_used = rfu.call_args[0][0] if rfu.call_args else None
        return writer

    def test_https_store_is_normalised_like_bits_publish(self):
        # The default store is an https:// URL; without normalisation the upload
        # fell through to the rsync backend.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BITS_S3_ALIBUILD_REPO_ENDPOINT_URL", None)
            w = self._run({"package": "ROOT", "version": "6.30", "revision": "1", "hash": "h3"},
                          store="https://s3.cern.ch/swift/v1/alibuild-repo")
        self.assertEqual(self.store_used, "b3://alibuild-repo")
        self.assertEqual(self.endpoint, "https://s3.cern.ch")
        self.assertEqual(w.tarballs, ["h3"])

    def test_refuses_non_redistributable(self):
        w = self._run({"package": "QGRAF", "version": "3.6", "revision": "1",
                       "hash": "h1", "redistributable": "none"})
        self.assertEqual(w.tarballs, [])

    def test_refuses_unrecognised_fail_closed(self):
        w = self._run({"package": "X", "version": "1", "revision": "1",
                       "hash": "h9", "redistributable": "weird-typo"})
        self.assertEqual(w.tarballs, [])

    def test_uploads_shareable_default(self):
        # No redistributable key -> default 'all' -> uploaded.
        w = self._run({"package": "ROOT", "version": "6.30", "revision": "1", "hash": "h2"})
        self.assertEqual(w.tarballs, ["h2"])


if __name__ == "__main__":
    unittest.main()
