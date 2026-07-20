# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for `bits compliance` (bits_helpers/compliance.py)."""

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


if __name__ == "__main__":
    unittest.main()
