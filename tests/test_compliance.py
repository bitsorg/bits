# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for `bits compliance` (bits_helpers/compliance.py)."""

import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bits_helpers import compliance


def _recipe(d, name, header):
    with open(os.path.join(d, name), "w") as fh:
        fh.write(header + "\n---\necho build\n# license: NotInHeader\n")


class _Parser:
    class _Err(Exception):
        pass

    def error(self, msg):
        raise self._Err(msg)


class ScanRecipesTestCase(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        _recipe(self.d, "good.sh", "package: Good\nversion: \"1\"\nlicense: MIT")
        _recipe(self.d, "quoted.sh",
                'package: Quoted\nlicense: "Apache-2.0 WITH LLVM-exception"')
        _recipe(self.d, "restricted.sh",
                "package: Secret\nlicense: LicenseRef-Secret\nredistributable: none")
        # Legacy boolean must keep working (false == none).
        _recipe(self.d, "legacy.sh",
                "package: Legacy\nlicense: LicenseRef-L\nredistributable: false")
        # Split forms: binaries-only (vendor runtime), sources-only.
        _recipe(self.d, "runtime.sh",
                "package: Runtime\nlicense: LicenseRef-R\nredistributable: binaries")
        _recipe(self.d, "srconly.sh",
                "package: SrcOnly\nlicense: MIT\nredistributable: sources")
        _recipe(self.d, "shim.sh", "package: shim\nlicense: NOASSERTION")
        _recipe(self.d, "nolicense.sh", "package: Bare\nversion: \"2\"")
        _recipe(self.d, "defaults-x.sh", "package: defaults-x")  # pseudo: exempt
        with open(os.path.join(self.d, "helper.sh"), "w") as fh:
            fh.write("#!/bin/bash\necho not-a-recipe\n")   # no package: -> skipped

    def test_scan(self):
        rec = compliance.scan_recipes(self.d)
        self.assertEqual(rec["total"], 9)
        self.assertEqual(rec["missing_license"], ["nolicense.sh"])
        self.assertIn("restricted.sh (LicenseRef-Secret)", rec["licenseref"])
        self.assertEqual(rec["noassertion"], ["shim.sh"])
        # binaries restricted: none + legacy false + sources-only.
        self.assertEqual(sorted(rec["restricted"]),
                         ["Legacy", "Secret", "SrcOnly"])
        # sources restricted: none + legacy false + binaries-only.
        self.assertEqual(sorted(rec["restricted_sources"]),
                         ["Legacy", "Runtime", "Secret"])
        # Quoted values are unquoted; body 'license:' lines never leak in.
        self.assertEqual(rec["by_package"]["quoted"]["license"],
                         "Apache-2.0 WITH LLVM-exception")
        self.assertEqual(rec["by_package"]["good"]["license"], "MIT")

    def test_do_compliance_recipes_only(self):
        args = SimpleNamespace(recipesDir=self.d, noStoreCheck=True, workDir="sw")
        # nolicense.sh is an issue -> exit 1.
        self.assertEqual(compliance.doCompliance(args, _Parser()), 1)
        os.remove(os.path.join(self.d, "nolicense.sh"))
        self.assertEqual(compliance.doCompliance(args, _Parser()), 0)

    def test_store_findings_classified(self):
        # Restricted package in a BOM -> 'stored'; in a signed manifest ->
        # 'certified'; matching is against the CURRENT recipe flags.
        boms = {
            "MANIFESTS/b1/x.el9.json": {"packages": [
                {"package": "Secret", "version": "1", "revision": "1",
                 "effective_architecture": "el9"},
                {"package": "Good", "version": "1", "revision": "1",
                 "effective_architecture": "el9"}]},
            "MANIFESTS/common-manifest-el9.json": {"packages": [
                {"package": "Secret", "version": "1", "revision": "1",
                 "effective_architecture": "el9"}]},
            "MANIFESTS/rev-index/el9/Secret/1-1": "not-json",
        }

        class _S3:
            def get_paginator(self, _m):
                return SimpleNamespace(paginate=lambda **kw: [
                    {"Contents": [{"Key": k} for k in boms]}])

            def get_object(self, Bucket, Key):
                import io, json as _json
                return {"Body": io.BytesIO(_json.dumps(boms[Key]).encode())}

        with patch.object(compliance, "_anonymous_store_access", return_value=True), \
             patch.object(compliance, "_s3_client",
                          return_value=(_S3(), "bkt", "https://s3.example")):
            st = compliance.audit_store("b3://bkt", {"secret"}, "/tmp")
        self.assertTrue(st["public"])
        self.assertEqual(st["boms"], 1)
        self.assertEqual(st["signed"], 1)
        self.assertEqual(st["stored"], ["Secret 1-1 el9"])
        self.assertEqual(st["certified"], ["Secret 1-1 el9"])


class _NoSuchKey(Exception):
    """What boto3 raises for an absent object, as far as the code looks."""

    def __init__(self, key):
        super().__init__(key)
        self.response = {"Error": {"Code": "NoSuchKey"}}


class EnforceTestCase(unittest.TestCase):
    """--enforce: purge offending objects, rewrite BOMs, delete emptied ones."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        _recipe(self.d, "secret.sh",
                "package: Secret\nversion: \"1\"\nlicense: LicenseRef-Secret\n"
                "redistributable: false\n"
                "source: https://example.org/secret-%(version)s.tar.gz")
        _recipe(self.d, "good.sh", "package: Good\nlicense: MIT")
        self.rec = compliance.scan_recipes(self.d)

        from bits_helpers.utilities import resolve_store_path
        from bits_helpers.download import getUrlChecksum
        sp = resolve_store_path("el9", "beef01")
        src_h = getUrlChecksum("https://example.org/secret-1.tar.gz")
        self.objects = {
            "MANIFESTS/b1/x.el9.json": json.dumps({"packages": [
                {"package": "Secret", "version": "1", "hash": "beef01",
                 "effective_architecture": "el9"},
                {"package": "Good", "hash": "aa11",
                 "effective_architecture": "el9"}]}),
            "MANIFESTS/b2/y.el9.json": json.dumps({"packages": [
                {"package": "Secret", "version": "1", "hash": "beef01",
                 "effective_architecture": "el9"}]}),
            sp + "/Secret-1-1.el9.tar.gz": "bytes",
            "MANIFESTS/rev-index/el9/Secret/1-1": "beef01",
            "SOURCES/cache/%s/%s/secret-1.tar.gz" % (src_h[:2], src_h): "src",
        }
        self.deleted, self.put = [], {}
        objects = self.objects
        outer = self

        class _S3:
            def get_paginator(self, _m):
                def paginate(Bucket, Prefix=""):
                    keys = [k for k in sorted(objects) if k.startswith(Prefix)
                            and k not in outer.deleted]
                    return [{"Contents": [{"Key": k} for k in keys]}]
                return SimpleNamespace(paginate=paginate)

            def get_object(self, Bucket, Key):
                import io
                if Key not in outer.put and Key not in objects:
                    raise _NoSuchKey(Key)
                body = outer.put.get(Key, objects.get(Key))
                return {"Body": io.BytesIO(body.encode())}

            def delete_object(self, Bucket, Key):
                outer.deleted.append(Key)

            def put_object(self, Bucket, Key, Body):
                outer.put[Key] = Body.decode()

        self.s3 = _S3()
        self._c = patch.object(compliance, "_s3_client",
                               return_value=(self.s3, "bkt", "https://s3.x"))
        self._c.start()
        self.addCleanup(self._c.stop)
        os.environ["AWS_ACCESS_KEY_ID"] = "k"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "s"
        self.addCleanup(os.environ.pop, "AWS_ACCESS_KEY_ID", None)
        self.addCleanup(os.environ.pop, "AWS_SECRET_ACCESS_KEY", None)

    def test_dry_run_touches_nothing(self):
        rc = compliance.enforce_store("b3://bkt", self.rec, self.d, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.deleted, [])
        self.assertEqual(self.put, {})

    def test_enforce_purges_and_rewrites(self):
        rc = compliance.enforce_store("b3://bkt", self.rec, self.d)
        self.assertEqual(rc, 0)
        from bits_helpers.utilities import resolve_store_path
        sp = resolve_store_path("el9", "beef01")
        # Tarball, rev-index marker and source archive deleted; b2's BOM
        # (emptied) deleted; b1's BOM rewritten with only Good left.
        self.assertIn(sp + "/Secret-1-1.el9.tar.gz", self.deleted)
        self.assertIn("MANIFESTS/rev-index/el9/Secret/1-1", self.deleted)
        self.assertTrue(any(k.startswith("SOURCES/cache/") for k in self.deleted))
        self.assertIn("MANIFESTS/b2/y.el9.json", self.deleted)
        kept = json.loads(self.put["MANIFESTS/b1/x.el9.json"])
        self.assertEqual([p["package"] for p in kept["packages"]], ["Good"])

    def test_a_bom_naming_another_builds_hash_deletes_nothing_of_it(self):
        from bits_helpers.utilities import resolve_store_path
        good = resolve_store_path("el9", "aa11") + "/Good-1-1.el9.tar.gz"
        self.objects[good] = "bytes"
        self.objects["MANIFESTS/b3/p77-run.el9.json"] = json.dumps({"packages": [
            {"package": "Secret", "version": "1", "hash": "aa11", "effective_architecture": "el9"}]})
        compliance.enforce_store("b3://bkt", self.rec, self.d)
        self.assertNotIn(good, self.deleted)                       # Good's build stays
        self.assertIn(resolve_store_path("el9", "beef01") + "/Secret-1-1.el9.tar.gz", self.deleted)

    def test_a_package_whose_name_starts_like_it_is_not_held(self):
        from bits_helpers.utilities import resolve_store_path
        other = resolve_store_path("el9", "cc33") + "/Secret-tools-1-1.el9.tar.gz"
        self.objects[other] = "bytes"
        self.objects["MANIFESTS/rev-index/el9/Secret-tools/1-1"] = "cc33"   # known to the store
        for version in ("1", "tools-1", "tools"):                  # crafted versions too
            self.objects["MANIFESTS/b4/x.el9.json"] = json.dumps({"packages": [
                {"package": "Secret", "version": version, "hash": "cc33",
                 "effective_architecture": "el9"}]})
            compliance.enforce_store("b3://bkt", self.rec, self.d)
            self.assertNotIn(other, self.deleted, version)         # Secret-tools stays
        _recipe(self.d, "secret-tools.sh", "package: Secret-tools\nlicense: MIT")
        del self.objects["MANIFESTS/rev-index/el9/Secret-tools/1-1"]
        compliance.enforce_store("b3://bkt", compliance.scan_recipes(self.d), self.d)
        self.assertNotIn(other, self.deleted)                      # or to the recipes
        self.objects["MANIFESTS/b5/y.el9.json"] = json.dumps({"packages": [
            {"package": "Secret-tools", "version": "1", "hash": "cc33"}]})
        compliance.enforce_store("b3://bkt", self.rec, self.d)
        self.assertNotIn(other, self.deleted)                      # or to a BOM

    def signed_store(self, signer=None, policy=None, extra=(), trust_signer=False, **fields):
        """A signed manifest for el9 (Secret, Good and *extra*) and the key: (pem, uploads)."""
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from bits_helpers import trust
        priv = Ed25519PrivateKey.generate()
        pem = os.path.join(self.d, "key.pem")
        with open(pem, "wb") as fh:
            fh.write(priv.private_bytes(serialization.Encoding.PEM,
                                        serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()))
        self.kid = trust.key_id(priv.public_key())
        data = json.dumps(dict(fields, packages=[{"package": "Secret", "hash": "beef01"},
                                                 {"package": "Good", "hash": "aa11"}] + list(extra)))
        sig = json.dumps(trust.sign_bytes(data.encode(), signer or priv))
        for key in ("MANIFESTS/common-manifest-el9.json", "MANIFESTS/x/common-manifest-el9.json"):
            self.objects[key], self.objects[key + ".sig"] = data, sig   # only the first is read
        self.objects["MANIFESTS/common-manifest-el8.json"] = json.dumps({"packages": []})
        self.objects["MANIFESTS/b1/x.el9.json"] = json.dumps({"packages": [   # not approved
            {"package": "Secret", "version": "1", "hash": "beef01", "effective_architecture": "el9"},
            {"package": "Other", "hash": "cc22", "effective_architecture": "el9",
             "tarball_sha256": "sha256:" + "1" * 64}]})
        uploads = {}

        def upload_file(src, bucket, key):
            with open(src) as fh:
                uploads[key] = fh.read()
        self.s3.upload_file = upload_file
        trusted = {trust.key_id(k.public_key()): k.public_key()
                   for k in [priv] + ([signer] if trust_signer else [])}
        for name, value in (("load_trusted_keys", trusted), ("load_key_policy", policy)):
            p = patch.object(trust, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        return pem, uploads

    def enforce(self, pem, dry_run=False):
        return compliance.enforce_store("b3://bkt", self.rec, self.d, key_pem=pem, dry_run=dry_run)

    def test_with_a_key_the_signed_manifest_loses_the_restricted_entries_only(self):
        from bits_helpers import trust
        pem, uploads = self.signed_store()
        self.assertEqual(self.enforce(pem, dry_run=True), 0)
        self.assertEqual(uploads, {})
        self.assertEqual(self.enforce(pem), 0)
        key = "MANIFESTS/common-manifest-el9.json"
        self.assertEqual(sorted(uploads), [key, key + ".sig"])
        # what it certified, less Secret; the BOMs (unapproved ones too) add nothing
        self.assertEqual(json.loads(uploads[key])["packages"], [{"package": "Good", "hash": "aa11"}])
        self.assertTrue(trust.verify_bytes(uploads[key].encode(), json.loads(uploads[key + ".sig"]),
                                           trust.load_trusted_keys()))

    def test_only_entries_its_signer_could_certify_stay(self):
        pem, uploads = self.signed_store(extra=[{"package": "Alien", "hash": "dd", "group": "lhcb"}],
                                         policy={"default": {"common"}})
        self.assertEqual(self.enforce(pem), 0)
        self.assertEqual([e["package"] for e in json.loads(
            uploads["MANIFESTS/common-manifest-el9.json"])["packages"]], ["Good"])

    def test_what_cannot_be_signed_again_is_reported(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from bits_helpers.utilities import resolve_store_path
        from bits_helpers import trust
        other = Ed25519PrivateKey.generate()
        for kw in ({"signer": Ed25519PrivateKey.generate()},          # does not verify
                   {"signer": other, "trust_signer": True,             # the key may not sign it
                    "policy": {trust.key_id(other.public_key()): {"*"}, "default": set()}},
                   {"sequence": 7, "issuer": "bits-orchestrator/x"}):  # the orchestrator's
            with self.subTest(kw=kw):
                pem, uploads = self.signed_store(**kw)
                self.assertEqual(self.enforce(pem), 1)
                self.assertEqual(uploads, {})
        self.assertIn(resolve_store_path("el9", "beef01") + "/Secret-1-1.el9.tar.gz", self.deleted)

    def test_an_unusable_key_is_refused_before_anything_is_removed(self):
        with open(os.path.join(self.d, "bad.pem"), "w") as fh:
            fh.write("not a key")
        self.assertEqual(self.enforce(os.path.join(self.d, "bad.pem")), 1)
        self.assertEqual((self.deleted, self.put), ([], {}))


class RecipeSourcePrefixNameTest(unittest.TestCase):
    """_recipe_source_prefixes must resolve %(name)s (the package name) in a
    source URL, so restricted packages whose URL templates the name (qgraf,
    kkmcee, starlight, …) have their SOURCES/cache/ archives purged too."""

    def test_name_substitution_resolves_prefix(self):
        from bits_helpers.download import getUrlChecksum
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        base = "https://example.cern.ch/src"
        _recipe(d, "qgraf.sh",
                'package: qgraf\nversion: "3.1.4"\nredistributable: none\n'
                'sources:\n  - %s/%%(name)s-%%(version)s.tgz' % base)
        rec = compliance.scan_recipes(d)
        prefixes, unresolved = compliance._recipe_source_prefixes(d, rec, {"qgraf"})
        self.assertEqual(unresolved, [])           # %(name)s no longer unresolved
        h = getUrlChecksum("%s/qgraf-3.1.4.tgz" % base)
        self.assertIn("SOURCES/cache/%s/%s/" % (h[:2], h),
                      [p for p, _why in prefixes])


if __name__ == "__main__":
    unittest.main()
