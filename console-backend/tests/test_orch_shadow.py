# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator's M4, first half: certification in shadow."""

import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from bits_helpers import certify, trust
from console_backend import config, forge_ops, main
from console_backend.orch import certified, db, events, shadow, worker

SETTINGS = {"BITS_ORCH_MANIFESTS_PROJECT": "bits/bits-manifests",
            "BITS_ORCH_SHADOW_LOOKBACK_DAYS": "100000"}


def _pkg(name, h, arch="el9", sha=None):
    return {"package": name, "version": "1", "revision": "1", "hash": h,
            "tarball": "%s-1-1.%s.tar.gz" % (name, arch), "tarball_sha256": sha or ("s" + h),
            "effective_architecture": arch}


def _bom(build_id, arch, *pkgs):
    return {"build_id": build_id, "source_pipeline_id": 7,
            "packages": [_pkg(n, h, arch) for n, h in pkgs]}


def _archive(sha, files):
    """GitLab's archive of manifests/ at *sha*: one top directory."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path, doc in files.items():
            data = json.dumps(doc).encode() if not isinstance(doc, bytes) else doc
            info = tarfile.TarInfo("bits-manifests-%s-manifests/%s" % (sha, path))
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class FakeManifests:
    def __init__(self):
        self.trees, self.pipelines, self.jobs, self.artifacts, self.parents = {}, {}, {}, {}, {}
        self.fail = None

    def add(self, pid, sha, before, status="success", updated="2026-10-11T10:00:00Z",
            source="push", artifacts=None):
        self.pipelines[pid] = {"id": pid, "sha": sha, "before_sha": before, "ref": "main",
                               "source": source, "status": status, "updated_at": updated,
                               "created_at": "2026-10-11T09:00:00Z"}
        self.jobs[pid] = [{"id": pid * 10, "name": "certify-and-publish", "status": status},
                          {"id": pid * 10 - 1, "name": "certify-and-publish", "status": "failed"}]
        if artifacts is not None:
            self.artifacts[pid * 10] = artifacts

    def finished_pipelines(self, ref, per_page=50, scope="finished"):
        if scope != "finished":
            return [{"id": i} for i in getattr(self, "running", ())]
        return [dict(p) for p in sorted(self.pipelines.values(), key=lambda p: -p["id"])]

    def get_pipeline(self, pid):
        if self.fail:
            raise self.fail
        return dict(self.pipelines[pid])

    def pipeline_jobs(self, pid):
        return list(self.jobs[pid])

    def commit(self, sha):
        return {"parent_ids": self.parents.get(sha, [])}

    def repository_archive(self, sha, path=None):
        assert path == "manifests"
        return _archive(sha, self.trees[sha]) if sha in self.trees else None

    def job_artifacts(self, job_id, max_bytes=0):
        return self.artifacts.get(job_id)


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "orch.db")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.cache = os.path.join(self.dir, "shadow")
        self.settings = config.Settings(env=SETTINGS)
        self.gl = FakeManifests()
        self.store = {}               # (arch, hash) -> sha256 the store holds
        priv = Ed25519PrivateKey.generate()
        self.key = os.path.join(self.dir, "k.pem")
        with open(self.key, "wb") as fh:
            fh.write(priv.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
        os.chmod(self.key, 0o600)
        kid = trust.key_id(priv.public_key())
        self.trusted = {kid: priv.public_key()}
        # The shipped key policy is strict; this test key may certify any group.
        p = patch.object(trust, "load_key_policy", lambda dirs=None: {kid: ["*"], "default": []})
        p.start()
        self.addCleanup(p.stop)

    def probe(self, arch, h, tarball=None):
        return self.store.get((arch, h))

    def ci(self, sha, archs, tamper=None, key=None):
        """What the CI job signs for the tree at *sha*: the artifacts zip."""
        root = os.path.join(self.dir, "ci-" + sha)
        shutil.rmtree(root, True)
        for path, doc in self.gl.trees[sha].items():
            p = os.path.join(root, path)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(doc if isinstance(doc, bytes) else json.dumps(doc).encode())
        out = os.path.join(self.dir, "out-" + sha, "common-manifest.json")
        outs = certify.certify_by_arch(os.path.join(root, "manifests"), key or self.key, out,
                                       probe=self.probe, valid_days=30, source_commit=sha,
                                       only_archs=archs)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for op, sp, arch in outs:
                body = open(op, "rb").read()
                if tamper:
                    doc = json.loads(body)
                    tamper(arch, doc)
                    body = json.dumps(doc).encode()
                    sig = json.dumps(trust.sign_bytes(body, trust.load_private_key(key or self.key)))
                else:
                    sig = open(sp).read()
                z.writestr(os.path.basename(op), body)
                z.writestr(os.path.basename(op) + ".sig", sig)
        return buf.getvalue()

    def run_shadow(self, store=("s3", "bucket"), sync=True):
        shadow.discover(self.conn, self.gl, self.settings)
        if sync:
            certified.sync(self.conn, self.gl, self.cache)
        while shadow.compare_one(self.conn, self.gl, store, self.settings, self.cache,
                                 trusted=self.trusted, probe_factory=lambda c: self.probe):
            pass

    def cert(self, pid):
        return self.conn.execute("SELECT * FROM certifications WHERE pipeline_id = ?",
                                 (pid,)).fetchone()

    def run_rows(self, pid, mode="shadow"):
        return {r["arch"]: r for r in self.conn.execute(
            "SELECT * FROM sign_runs WHERE certification = ? AND mode = ?", (pid, mode))}

    def kinds(self):
        return [e["kind"] for e in events.since(self.conn, limit=1000)]


class ChangedArchsTest(_Base):
    def tree(self, files):
        root = os.path.join(tempfile.mkdtemp(dir=self.dir), "manifests")
        for path, doc in files.items():
            p = os.path.join(root, path)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w") as fh:
                fh.write(doc if isinstance(doc, str) else json.dumps(doc))
        return root

    def test_the_ci_jobs_reading(self):
        a, b = _bom("b1", "el9", ("p", "h1")), _bom("b2", "el10", ("q", "h2"))
        old = self.tree({"lcg/a.json": a, "lcg/b.json": b, "lcg/same.json": a})
        new = self.tree({"lcg/a.json": dict(a, extra=1), "lcg/same.json": a,
                         "alice/c.json": _bom("b3", "shared", ("r", "h3"))})
        changed, archs = shadow.changed_archs(new, old)
        self.assertEqual(changed, ["alice/c.json", "lcg/a.json", "lcg/b.json"])
        self.assertEqual(archs, ["el10", "el9", "shared"])     # b.json deleted: its old side
        self.assertEqual(shadow.changed_archs(new, new), ([], None))
        mixed = self.tree({"x/m.json": {"packages": [_pkg("p", "h1"), _pkg("q", "h2", "el10")]}})
        self.assertEqual(shadow.changed_archs(mixed, None)[1], None)   # every architecture
        broken = self.tree({"x/m.json": "{not json"})
        self.assertEqual(shadow.changed_archs(broken, None)[1], None)
        labelled = self.tree({"x/m.json": {"effective_architecture": "el8", "packages": []}})
        self.assertEqual(shadow.changed_archs(labelled, None)[1], ["el8"])

    def test_the_archive_is_extracted_safely(self):
        evil = io.BytesIO()
        with tarfile.open(fileobj=evil, mode="w:gz") as tar:
            for name in ("top/manifests/lcg/ok.json", "top/manifests/../../evil.json",
                         "/abs/manifests/x.json", "top/README.md"):
                info = tarfile.TarInfo(name)
                info.size = 2
                tar.addfile(info, io.BytesIO(b"{}"))
            link = tarfile.TarInfo("top/manifests/lcg/link.json")
            link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
            tar.addfile(link)

        class R:
            def repository_archive(self, sha, path=None):
                return evil.getvalue()
        root = shadow.tree(R(), "abcdef1", self.cache)
        found = sorted(os.path.relpath(os.path.join(d, f), root)
                       for d, _s, fs in os.walk(root) for f in fs)
        self.assertEqual(found, ["lcg/ok.json"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "evil.json")))
        self.assertEqual(shadow.tree(None, "abcdef1", self.cache), root)     # cached

    def test_trees_in_use_are_kept(self):
        import time
        for i, sha in enumerate(("a", "b", "c", "d", "e")):
            os.makedirs(os.path.join(self.cache, sha))
            t = time.time() - 100 + i
            os.utime(os.path.join(self.cache, sha), (t, t))
        shadow.prune(self.cache, keep=2, protect=("a",))
        self.assertEqual(sorted(os.listdir(self.cache)), ["a", "d", "e"])

    def test_an_unknown_commit_is_a_skip(self):
        with self.assertRaises(shadow.Skip):
            shadow.tree(FakeManifests(), "nope", self.cache)


class CompareTest(_Base):
    def setUp(self):
        super().setUp()
        self.gl.trees["aaaa000"] = {"manifests/lcg/a.json": _bom("b1", "el9", ("p", "h1"))}
        self.gl.trees["aaaa111"] = dict(self.gl.trees["aaaa000"], **{
            "manifests/lcg/b.json": _bom("b2", "el9", ("q", "h2")),
            "manifests/alice/c.json": _bom("b3", "el10", ("r", "h3"))})
        self.store = {("el9", "h1"): "sh1", ("el9", "h2"): "sh2", ("el10", "h3"): "sh3"}

    def test_identical_manifests_match(self):
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el10", "el9"]))
        self.run_shadow()
        c = self.cert(5)
        self.assertEqual((c["state"], json.loads(c["archs"]), c["job_id"]),
                         ("compared", ["el10", "el9"], 50))
        runs = self.run_rows(5)
        self.assertEqual({a: r["state"] for a, r in runs.items()}, {"el9": "match", "el10": "match"})
        self.assertEqual((runs["el9"]["packages"], runs["el9"]["source"]), (2, "artifacts"))
        self.assertEqual(runs["el9"]["manifest_sha256"], runs["el9"]["signed_sha256"])
        self.assertEqual(self.kinds().count("shadow.match"), 2)
        self.assertEqual(shadow.summary(self.conn), {"match": 2})
        self.run_shadow()                                   # nothing more to do
        self.assertEqual(self.kinds().count("shadow.match"), 2)

    def test_a_difference_is_a_mismatch_with_what_differs(self):
        def tamper(arch, doc):
            if arch == "el9":
                doc["packages"] = doc["packages"][:1] + [dict(_pkg("x", "h9"), group="lcg")]
                doc["sources"] = doc["sources"] + ["b-extra"]
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el10", "el9"], tamper=tamper))
        self.run_shadow()
        runs = self.run_rows(5)
        self.assertEqual(runs["el10"]["state"], "match")
        self.assertEqual(runs["el9"]["state"], "mismatch")
        d = json.loads(runs["el9"]["diff"])
        self.assertEqual(d["only_ours"]["count"], 1)
        self.assertEqual(d["only_signed"]["first"], ["x@1-1 h9"])
        self.assertEqual(d["sources"]["only_signed"], ["b-extra"])
        ev = [e for e in events.since(self.conn, limit=100) if e["kind"] == "shadow.mismatch"]
        self.assertEqual(ev[0]["payload"]["arch"], "el9")
        self.assertEqual(ev[0]["subject"], "certification:5")

    def test_a_different_scope_shows_in_sources(self):
        # CI certified every architecture (no scope), el8 too, which this push
        # did not touch: its manifests' sources cover the el8 build as well.
        for sha in ("aaaa000", "aaaa111"):
            self.gl.trees[sha]["manifests/lcg/old.json"] = _bom("b0", "el8", ("o", "h0"))
        self.store[("el8", "h0")] = "sh0"
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", None))
        self.run_shadow()
        runs = self.run_rows(5)
        self.assertEqual(runs["el9"]["state"], "mismatch")
        self.assertEqual(json.loads(runs["el9"]["diff"])["sources"]["only_signed"], ["b0"])
        self.assertIn("not in the worker", json.loads(runs["el8"]["diff"])["error"])

    def test_an_architecture_signed_but_not_computed_is_a_mismatch(self):
        buf = io.BytesIO(self.ci("aaaa111", ["el10", "el9"]))
        with zipfile.ZipFile(buf, "a") as z:
            z.writestr("common-manifest-el8.json", json.dumps({"packages": []}))
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=buf.getvalue())
        self.run_shadow()
        self.assertEqual(self.run_rows(5)["el8"]["state"], "mismatch")

    def test_an_untrusted_signature_is_unverified(self):
        other = os.path.join(self.dir, "other.pem")
        priv = Ed25519PrivateKey.generate()
        with open(other, "wb") as fh:
            fh.write(priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
        os.chmod(other, 0o600)
        okid = trust.key_id(priv.public_key())
        trust.load_key_policy = lambda dirs=None: {okid: ["*"]}   # undone by the patch
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el10", "el9"], key=other))
        self.run_shadow()
        self.assertEqual({r["state"] for r in self.run_rows(5).values()}, {"unverified"})
        self.assertNotIn("shadow.match", self.kinds())

    def test_the_worker_refusing_what_was_signed(self):
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el10", "el9"]))
        self.store[("el9", "h2")] = "other-bytes"           # the store now disagrees
        self.run_shadow()
        runs = self.run_rows(5)
        self.assertEqual(list(runs), ["*"])
        self.assertIn("refuses", json.loads(runs["*"]["diff"])["error"])

    def test_without_artifacts_the_stores_copy_while_it_is_current(self):
        art = self.ci("aaaa111", ["el10", "el9"])
        objects = {}
        with zipfile.ZipFile(io.BytesIO(art)) as z:
            for n in z.namelist():
                objects["MANIFESTS/" + n] = z.read(n)
        doc = json.loads(objects["MANIFESTS/common-manifest-el10.json"])
        doc["source_commit"] = "aaaa222"                          # a later certification replaced it
        objects["MANIFESTS/common-manifest-el10.json"] = json.dumps(doc).encode()
        self.gl.add(5, "aaaa111", "aaaa000")
        self.run_shadow(store=(FakeS3(objects), "bucket"))
        runs = self.run_rows(5)
        self.assertEqual((runs["el9"]["state"], runs["el9"]["source"]), ("match", "store"))
        self.assertEqual(runs["el10"]["state"], "superseded")

    def test_a_full_run_from_the_store_skips_architectures_it_did_not_sign(self):
        self.gl.trees["aaaa111"]["manifests/lcg/bad.json"] = b"{broken"      # scope: every arch
        art = self.ci("aaaa111", None)
        objects = {"MANIFESTS/common-manifest-el7.json": json.dumps(
                       {"source_commit": "s-old", "packages": []}).encode(),
                   "MANIFESTS/common-manifest-el7.json.sig": b"{}"}
        with zipfile.ZipFile(io.BytesIO(art)) as z:
            for n in z.namelist():
                objects["MANIFESTS/" + n] = z.read(n)
        self.gl.add(5, "aaaa111", "aaaa000")
        with self.assertLogs("bits", "WARNING"):              # bits skips the unreadable BOM
            self.run_shadow(store=(FakeS3(objects), "bucket"))
        self.assertEqual(json.loads(self.cert(5)["archs"]), [])
        self.assertEqual({a: r["state"] for a, r in self.run_rows(5).items()},
                         {"el9": "match", "el10": "match"})

    def test_failed_jobs_are_skipped_until_retried(self):
        self.gl.add(5, "aaaa111", "aaaa000", status="failed")
        self.run_shadow()
        self.assertEqual((self.cert(5)["state"], self.cert(5)["reason"]),
                         ("skipped", "certify-and-publish failed"))
        self.gl.add(5, "aaaa111", "aaaa000", updated="2026-10-11T11:00:00Z",
                    artifacts=self.ci("aaaa111", ["el10", "el9"]))
        self.run_shadow()
        self.assertEqual(self.cert(5)["state"], "compared")

    def test_what_is_not_a_certification(self):
        self.gl.add(5, "aaaa000", "aaaa000", artifacts=b"")           # nothing changed
        self.gl.add(6, "aaaa111", "aaaa000", source="merge_request_event")
        self.gl.jobs[5] = [{"id": 50, "name": "certify-and-publish", "status": "success"}]
        self.run_shadow()
        self.assertEqual(self.cert(5)["reason"], "no manifests changed")
        self.assertIsNone(self.cert(6))
        self.gl.add(7, "aaaa111", "aaaa000")
        self.gl.jobs[7] = [{"id": 70, "name": "validate-and-merge", "status": "success"}]
        self.run_shadow()
        self.assertEqual(self.cert(7)["reason"], "no certify-and-publish job")

    def test_a_first_push_diffs_against_the_parent(self):
        self.gl.parents["aaaa111"] = ["aaaa000"]
        self.gl.add(5, "aaaa111", "0" * 40, artifacts=self.ci("aaaa111", ["el10", "el9"]))
        self.run_shadow()
        self.assertEqual(self.cert(5)["before_sha"], "aaaa000")
        self.assertEqual({r["state"] for r in self.run_rows(5).values()}, {"match"})

    def test_an_outage_fails_the_step_without_counting(self):
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el10", "el9"]))
        self.gl.fail = forge_ops.ForgeError(502, "bad gateway")
        with self.assertRaises(forge_ops.ForgeError):
            self.run_shadow()
        self.assertEqual((self.cert(5)["state"], self.cert(5)["attempts"]), ("pending", 0))
        self.gl.fail = None
        err = Exception("timeout")
        err.response = {"Error": {"Code": "RequestTimeout"}}

        def probe(arch, h, tarball=None):
            if h == "h2":
                try:
                    raise err
                except Exception as exc:
                    raise certify.CertifyError("store check failed") from exc
            return self.store.get((arch, h))
        with self.assertRaises(certify.CertifyError):
            shadow.compare_one(self.conn, self.gl, ("s3", "b"), self.settings, self.cache,
                               trusted=self.trusted, probe_factory=lambda c: probe)
        self.assertEqual((self.cert(5)["state"], self.cert(5)["attempts"]), ("pending", 0))
        self.assertEqual(self.run_rows(5), {})

    def test_errors_back_off_then_fail(self):
        self.gl.add(5, "aaaa111", "aaaa000")
        self.gl.fail = forge_ops.ForgeError(403, "forbidden")
        with self.assertLogs("bits_orchestrator.shadow", "WARNING"):
            self.run_shadow()
        c = self.cert(5)
        self.assertEqual((c["state"], c["attempts"]), ("pending", 1))
        self.assertGreater(c["next_ms"], db.now_ms())
        with self.assertLogs("bits_orchestrator.shadow", "WARNING"):
            for _ in range(shadow.MAX_ATTEMPTS):
                self.conn.execute("UPDATE certifications SET next_ms = 0")
                self.run_shadow()
        self.assertEqual(self.cert(5)["state"], "failed")
        self.assertEqual(self.kinds().count("shadow.failed"), 1)

    def test_old_pipelines_are_not_compared(self):
        self.settings.orch_shadow_lookback_days = 1
        self.gl.add(5, "aaaa111", "aaaa000")
        self.gl.pipelines[5]["created_at"] = "2026-01-01T00:00:00Z"
        shadow.discover(self.conn, self.gl, self.settings)
        self.assertIsNone(self.cert(5))


class FakeS3:
    def __init__(self, objects):
        self.objects = dict(objects)
        self.gets = []

    def get_object(self, Bucket, Key):
        self.gets.append(Key)
        if Key not in self.objects:
            e = Exception("NoSuchKey")
            e.response = {"Error": {"Code": "NoSuchKey"}}
            raise e
        return {"Body": io.BytesIO(self.objects[Key])}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            e = Exception("404")
            e.response = {"Error": {"Code": "404"}}
            raise e
        return {"ETag": '"e-%d"' % len(self.objects[Key]), "ContentLength": len(self.objects[Key])}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        return {"Contents": [{"Key": k, "ETag": '"e-%d"' % len(v), "Size": len(v)}
                             for k, v in sorted(self.objects.items()) if k.startswith(Prefix)]}


class ProbeCacheTest(_Base):
    def test_only_new_objects_are_hashed(self):
        key = "TARS/el9/store/h1/h1abc/p-1-1.el9.tar.gz"
        s3 = FakeS3({key: b"bytes"})
        probe = certify.s3_probe(s3, "b", "el9", cache=shadow.DigestCache(self.conn))
        first = probe("el9", "h1abc", "p-1-1.el9.tar.gz")
        self.assertTrue(first.startswith("sha256:"))
        self.assertEqual(len(s3.gets), 1)
        self.assertEqual(probe("el9", "h1abc", "p-1-1.el9.tar.gz"), first)
        self.assertEqual(len(s3.gets), 1)                    # from store_digests
        s3.objects[key] = b"other bytes"                     # a changed object is hashed again
        self.assertNotEqual(probe("el9", "h1abc", "p-1-1.el9.tar.gz"), first)
        self.assertEqual(len(s3.gets), 2)
        self.assertEqual(probe("el9", "h1abc"), probe("el9", "h1abc", "p-1-1.el9.tar.gz"))
        self.assertIsNone(probe("el9", "nope", "x.tar.gz"))
        s3.head_object = lambda Bucket, Key: (_ for _ in ()).throw(TimeoutError("slow"))
        with self.assertRaises(certify.CertifyError):          # not "absent": the run fails
            probe("el9", "h1abc", "p-1-1.el9.tar.gz")


class RedirectTest(unittest.TestCase):
    def test_a_redirect_is_followed_without_the_token(self):
        seen = []

        class R:
            def __init__(self, status, location=None):
                self.status_code, self.headers = status, {"Location": location} if location else {}

            def iter_content(self, n):
                return [b"zip"]

            def close(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def get(url, headers, timeout, stream, allow_redirects):
            seen.append((url, dict(headers), allow_redirects))
            return R(302, "https://objects.example/bucket/a.zip?sig=x") if len(seen) == 1 else R(200)
        f = forge_ops.GitLabForge("https://gl/api/v4", "tok", "bits/bits-manifests")
        with patch.object(forge_ops.requests, "get", get):
            self.assertEqual(f.job_artifacts(5), b"zip")
        self.assertEqual(seen[0][1]["PRIVATE-TOKEN"], "tok")
        self.assertEqual(seen[1][0], "https://objects.example/bucket/a.zip?sig=x")
        self.assertNotIn("PRIVATE-TOKEN", seen[1][1])
        self.assertEqual({s[2] for s in seen}, {False})


class BuildByArchTest(unittest.TestCase):
    def test_it_is_what_certify_by_arch_signs(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        key = os.path.join(d, "k.pem")
        with open(key, "wb") as fh:
            fh.write(Ed25519PrivateKey.generate().private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()))
        os.chmod(key, 0o600)
        p = patch.object(trust, "load_key_policy", lambda dirs=None: None)
        p.start()
        self.addCleanup(p.stop)
        boms = [_bom("b1", "el9", ("p", "h1")), _bom("b2", "el10", ("q", "h2"))]
        probe = lambda a, h, t=None: "s" + h                 # noqa: E731
        built = dict(certify.build_by_arch(boms, probe=probe, source_commit="c"))
        signed = certify.certify_by_arch(boms, key, os.path.join(d, "cm.json"), probe=probe,
                                         source_commit="c")
        for op, _sp, arch in signed:
            self.assertEqual(shadow.digest(json.load(open(op))), shadow.digest(built[arch]))


class ApiTest(_Base):
    def setUp(self):
        super().setUp()
        main.settings = config.Settings(env={
            "GITLAB_API_URL": "https://gl/api/v4", "BITS_ORCH_DB": self.path,
            "BITS_ADMINS_POLICY": "testbed @alice\n* @carol"})
        p = patch.object(main.identity, "verify_gitlab_token", lambda a, t, *x, **k: t or None)
        p.start()
        self.addCleanup(p.stop)
        self.client = TestClient(main.app)
        self.gl.trees["aaaa000"] = {}
        self.gl.trees["aaaa111"] = {"manifests/lcg/b.json": _bom("b2", "el9", ("q", "h2"))}
        self.store = {("el9", "h2"): "sh2"}
        self.gl.add(5, "aaaa111", "aaaa000", artifacts=self.ci("aaaa111", ["el9"]))
        self.run_shadow()

    def _get(self, user, path):
        return self.client.get(path, headers={"Authorization": "Bearer %s" % user})

    def test_overall_admins_read_certifications(self):
        r = self._get("carol", "/ops/certifications")
        self.assertEqual(r.status_code, 200)
        c = r.json()["certifications"][0]
        self.assertEqual((c["pipeline_id"], {(x["mode"], x["state"]) for x in c["sign_runs"]}),
                         (5, {("shadow", "match"), ("set", "match"), ("replay", "missing")}))
        summary = self._get("carol", "/ops/certifications/summary").json()
        self.assertEqual((summary["sign_runs"], summary["replay"]), ({"match": 1}, {"missing": 1}))
        self.assertEqual(self._get("carol", "/ops/certifications?state=bogus").status_code, 400)
        self.assertEqual(self._get("alice", "/ops/certifications").status_code, 403)
        self.assertEqual(self.client.get("/ops/certifications").status_code, 401)


class WorkerTest(_Base):
    def test_the_shadow_runs_on_its_own_thread_only_when_configured(self):
        w = worker.Worker(self.conn, config.Settings(env={}), path=self.path)
        w.start()
        self.assertIsNone(w.shadow)
        w = worker.Worker(self.conn, self.settings, interval=0.05, path=self.path)
        w.deps.manifests_reader = lambda: self.gl
        w.deps.store = lambda: None
        self.gl.trees["aaaa000"], self.gl.trees["aaaa111"] = {}, {"manifests/l/b.json": _bom("b", "el9")}
        self.gl.add(5, "aaaa111", "aaaa000")
        w.start()
        self.addCleanup(w.stop)
        self.assertEqual(w.shadow.name, "shadow")
        import time

        def failed():
            return [e for e in events.since(self.conn, limit=1000)
                    if e["kind"] == "worker.step_failed" and e["payload"]["step"] == "shadow"]
        with self.assertLogs("bits_orchestrator.shadow", "ERROR"):
            for _ in range(100):
                if failed():
                    break
                time.sleep(0.05)
        w.stop()
        w.shadow.join(5)
        self.assertIn("BITS_ORCH_STORE", failed()[0]["payload"]["error"])
        self.assertEqual(len(failed()), 1)


if __name__ == "__main__":
    unittest.main()
