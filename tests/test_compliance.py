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
                "package: Secret\nlicense: LicenseRef-Secret\nredistributable: false")
        _recipe(self.d, "shim.sh", "package: shim\nlicense: NOASSERTION")
        _recipe(self.d, "nolicense.sh", "package: Bare\nversion: \"2\"")
        _recipe(self.d, "defaults-x.sh", "package: defaults-x")  # pseudo: exempt
        with open(os.path.join(self.d, "helper.sh"), "w") as fh:
            fh.write("#!/bin/bash\necho not-a-recipe\n")   # no package: -> skipped

    def test_scan(self):
        rec = compliance.scan_recipes(self.d)
        self.assertEqual(rec["total"], 6)
        self.assertEqual(rec["missing_license"], ["nolicense.sh"])
        self.assertEqual(rec["licenseref"], ["restricted.sh (LicenseRef-Secret)"])
        self.assertEqual(rec["noassertion"], ["shim.sh"])
        self.assertEqual(rec["restricted"], ["Secret"])
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
                {"package": "Secret", "hash": "beef01",
                 "effective_architecture": "el9"},
                {"package": "Good", "hash": "aa11",
                 "effective_architecture": "el9"}]}),
            "MANIFESTS/b2/y.el9.json": json.dumps({"packages": [
                {"package": "Secret", "hash": "beef01",
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
                body = outer.put.get(Key, objects[Key])
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


if __name__ == "__main__":
    unittest.main()
