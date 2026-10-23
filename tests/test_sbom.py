# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""bits_helpers.sbom — CycloneDX 1.6 / SPDX 2.3 export of a build manifest."""

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from bits_helpers import sbom

H = "a" * 40


def _manifest(schema=4):
    pkgs = [
        {"package": "defaults-release", "version": "v1", "hash": "d0"},
        {"package": "lcg.bits", "version": "1", "hash": "l0", "provides_repository": True},
        {"package": "CMake", "version": "3.30.6", "revision": "3", "hash": "c" * 40,
         "effective_architecture": "x86_64-el9-gcc14-opt", "outcome": "from_store",
         "tarball_sha256": "sha256:" + "1" * 64, "license": "BSD-3-Clause",
         "redistributable": "all", "requires": [], "build_requires": [],
         "source_checksums": [{"url": "https://cmake.org/files/cmake-3.30.6.tar.gz",
                               "checksum": "sha256:" + "2" * 64}]},
        {"package": "ROOT", "version": "v6-40-02", "revision": "5", "hash": "r" * 40,
         "effective_architecture": "x86_64-el9-gcc14-opt", "outcome": "built_from_source",
         "tarball_sha256": "sha256:" + "3" * 64, "license": "LGPL-2.1-or-later",
         "redistributable": "all", "commit_hash": H,
         "source": "https://github.com/root-project/root.git", "tag": "v6-40-02",
         "requires": ["zlib", "defaults-release"], "build_requires": ["CMake"],
         "patches": [{"name": "fix.patch", "checksum": "sha256:" + "4" * 64}]},
        {"package": "kkmcee", "version": "5.01.00", "revision": "1", "hash": "k" * 40,
         "license": "LicenseRef-KKMCee", "redistributable": "none",
         "requires": ["ROOT"], "build_requires": []},
    ]
    if schema < 4:
        for p in pkgs:
            for k in ("requires", "build_requires", "source", "tag"):
                p.pop(k, None)
    return {"schema_version": schema, "created_at": "2026-09-30T07:17:42.123456+00:00",
            "system_packages": ["zlib"] if schema >= 4 else [],
            "updated_at": "2026-09-30T07:18:18Z", "architecture": "x86_64-el9-gcc14-opt",
            "defaults": ["release", "key4hep"], "requested_packages": ["kkmcee"],
            "packages": pkgs}


def _odd_manifest():
    """Recipe data as found in the wild: free-text licences, odd names, no hashes."""
    lic = ["LGPLv3", "ApacheV2+BSD2C+CC0", "PSF License Version 2", "LicenseRef-HYDJET++",
           "BSD-2-Clause AND Public Domain", "GPL-2.0", "", None]
    pkgs = [{"package": "p_%d" % i, "version": "x" * 20 + str(i), "license": l,
             "tarball_sha256": "sha256:abc", "commit_hash": "0", "source": "/home/u/src",
             "source_checksums": [{"url": "https://u:p@h/x.tgz?token=1", "checksum": "md5:zz"}]}
            for i, l in enumerate(lic)]
    pkgs.append({"package": "p-0", "version": "1"})
    return {"schema_version": 4, "updated_at": "2026-09-30T09:00:00+02:00", "packages": pkgs}


class TestComponents(unittest.TestCase):
    def test_model(self):
        comps = {c["name"]: c for c in sbom.components(_manifest())}
        # defaults-* and recipe-repository packages are not components.
        self.assertEqual(sorted(comps), ["CMake", "ROOT", "kkmcee", "zlib"])
        self.assertTrue(comps["zlib"]["system"])              # a dependency not built by bits
        self.assertEqual(comps["ROOT"]["deps"], ["zlib@system"])
        self.assertEqual(comps["ROOT"]["build_deps"], ["CMake@" + "c" * 16])
        self.assertEqual(comps["kkmcee"]["deps"], ["ROOT@" + "r" * 16])

    def test_purl_and_download(self):
        root = _manifest()["packages"][3]
        self.assertEqual(sbom._purl(root), "pkg:github/root-project/root@" + H)
        self.assertEqual(sbom._download(root), "git+https://github.com/root-project/root.git@" + H)
        cmake = _manifest()["packages"][2]
        self.assertEqual(sbom._purl(cmake), "pkg:generic/CMake@3.30.6")
        self.assertEqual(sbom._download(cmake), "https://cmake.org/files/cmake-3.30.6.tar.gz")


    def test_missing_dependencies_that_are_not_system_are_dropped(self):
        m = _manifest()
        m["system_packages"] = []            # e.g. a failed build: zlib never recorded
        comps = {c["name"]: c for c in sbom.components(m)}
        self.assertNotIn("zlib", comps)
        self.assertEqual(comps["ROOT"]["deps"], [])

    def test_refs_are_unique(self):
        m = {"packages": [{"package": "a_b", "version": "1"}, {"package": "a-b", "version": "1"},
                          {"package": "p", "version": "x" * 20 + "1"},
                          {"package": "p", "version": "x" * 20 + "2"}]}
        refs = [c["ref"] for c in sbom.components(m)]
        self.assertEqual(len(refs), len(set(refs)))
        ids = [p["SPDXID"] for p in sbom.to_spdx(m)["packages"]]
        self.assertEqual(len(ids), len(set(ids)))       # a_b and a-b sanitise alike

    def test_clean_url(self):
        self.assertEqual(sbom.clean_url("https://u:p@ss@host/a.tgz?private_token=x&v=1"),
                         "https://host/a.tgz?v=1")
        self.assertEqual(sbom.clean_url("/home/u/src/root"), "")   # a development checkout
        self.assertEqual(sbom.clean_url("git@github.com:a/b.git"), "")
        e = {"package": "p", "source": "/home/u/src/p",
             "source_checksums": [{"url": "https://t:s@x.org/p.tgz", "checksum": "sha256:abc"}]}
        self.assertEqual(sbom._download(e), "https://x.org/p.tgz")
        comp = sbom.to_cyclonedx({"packages": [e]})["components"][0]
        self.assertEqual(comp["externalReferences"], [{"type": "distribution", "url": "https://x.org/p.tgz"}])

    def test_clean_url_keeps_clean_urls_verbatim(self):
        for u in ("https://h.org/f?download", "https://h.org/a%20b.tgz?x=1&y=2"):
            self.assertEqual(sbom.clean_url(u), u)
        self.assertEqual(sbom.clean_url("https://h.org/f?api_key=s&x=1"), "https://h.org/f?x=1")
        self.assertEqual(sbom.clean_url("https://h.org?x=a@b"), "https://h.org?x=a@b")

    def test_purl_github_is_anchored(self):
        for u in ("https://mygithub.com/a/b.git", "https://api.github.com/repos/a/b"):
            self.assertTrue(sbom._purl({"package": "p", "version": "1", "source": u})
                            .startswith("pkg:generic/"), u)

    def test_timestamps_are_utc(self):
        self.assertEqual(sbom._when({"updated_at": "2026-09-30T09:00:00+02:00"}), "2026-09-30T07:00:00Z")
        self.assertEqual(sbom._when({"published_at": "2026-09-30T07:21:49Z"}), "2026-09-30T07:21:49Z")


class TestLicences(unittest.TestCase):
    def test_spdx_expression(self):
        ok = {"mit": "MIT", "GPL-2.0-or-later WITH Classpath-exception-2.0":
              "GPL-2.0-or-later WITH Classpath-exception-2.0",
              "(apache-2.0 or bsd-3-clause) and zlib": "(Apache-2.0 OR BSD-3-Clause) AND Zlib",
              "LicenseRef-KKMCee": "LicenseRef-KKMCee", "GPL-2.0+": "GPL-2.0+"}
        for text, canon in ok.items():
            self.assertEqual(sbom.spdx_expression(text), canon, text)
        for bad in ("LGPLv3", "Apache v2", "BSD-2-Clause AND Public Domain", "LicenseRef-HYDJET++",
                    "DocumentRef-x:LicenseRef-y", "MIT AND", "(MIT", "", "GPL-2.0 WITH Foo"):
            self.assertIsNone(sbom.spdx_expression(bad), bad)

    def test_invalid_licences_stay_valid_documents(self):
        m = {"packages": [{"package": "a", "version": "1", "license": "LGPLv3"},
                          {"package": "b", "version": "1", "license": "LGPL v3"},
                          {"package": "c", "version": "1", "license": "LicenseRef-bits-LGPLv3"}]}
        comps = sbom.to_cyclonedx(m)["components"]
        self.assertEqual(comps[0]["licenses"], [{"license": {"name": "LGPLv3"}}])
        doc = sbom.to_spdx(m)
        decl = [p["licenseDeclared"] for p in doc["packages"]]
        # c's own LicenseRef keeps its name; a's wrapped text steps aside.
        self.assertEqual(decl[2], "LicenseRef-bits-LGPLv3")
        self.assertTrue(decl[0].startswith("LicenseRef-bits-LGPLv3-"))
        self.assertEqual(decl[1], "LicenseRef-bits-LGPL-v3")
        self.assertEqual(len(set(decl)), 3)              # no two texts share a ref
        texts = {x["licenseId"]: x["extractedText"] for x in doc["hasExtractedLicensingInfos"]}
        self.assertEqual(texts[decl[0]], "LGPLv3")
        self.assertEqual(texts[decl[1]], "LGPL v3")
        self.assertEqual(set(texts), set(decl))


    def test_noassertion_and_none(self):
        m = {"packages": [{"package": "a", "version": "1", "license": "NOASSERTION"},
                          {"package": "b", "version": "1", "license": "none"}]}
        self.assertNotIn("licenses", sbom.to_cyclonedx(m)["components"][0])
        self.assertEqual([p["licenseDeclared"] for p in sbom.to_spdx(m)["packages"]],
                         ["NOASSERTION", "NONE"])
        self.assertNotIn("hasExtractedLicensingInfos", sbom.to_spdx(m))


class TestCycloneDX(unittest.TestCase):
    def test_document(self):
        bom = sbom.to_cyclonedx(_manifest(), "rel-1")
        self.assertEqual((bom["bomFormat"], bom["specVersion"]), ("CycloneDX", "1.6"))
        self.assertEqual(bom["metadata"]["timestamp"], "2026-09-30T07:18:18Z")
        comps = {c["name"]: c for c in bom["components"]}
        root = comps["ROOT"]
        self.assertEqual(root["version"], "v6-40-02")           # upstream, as scanners compare
        self.assertIn({"name": "bits:revision", "value": "5"}, root["properties"])
        self.assertEqual(root["hashes"], [{"alg": "SHA-256", "content": "3" * 64}])
        self.assertEqual(root["licenses"], [{"expression": "LGPL-2.1-or-later"}])
        props = {(p["name"], p["value"]) for p in root["properties"]}
        self.assertIn(("bits:build_requires", "CMake"), props)
        self.assertIn(("bits:hash", "r" * 40), props)
        self.assertIn(("bits:patch", "fix.patch sha256:" + "4" * 64), props)
        self.assertEqual(comps["CMake"]["externalReferences"][0]["hashes"],
                         [{"alg": "SHA-256", "content": "2" * 64}])
        self.assertEqual(comps["zlib"]["properties"], [{"name": "bits:provided_by", "value": "system"}])
        deps = {d["ref"]: d["dependsOn"] for d in bom["dependencies"]}
        self.assertEqual(deps["release"], ["kkmcee@" + "k" * 16])
        self.assertEqual(deps["ROOT@" + "r" * 16], ["zlib@system"])
        refs = {c["bom-ref"] for c in bom["components"]} | {"release"}
        self.assertTrue(all(r in refs for d in bom["dependencies"] for r in [d["ref"]] + d["dependsOn"]))

    def test_deterministic(self):
        a = sbom.render(_manifest(), "cyclonedx", "rel-1")
        self.assertEqual(a, sbom.render(_manifest(), "cyclonedx", "rel-1"))
        m = _manifest()
        m["packages"][3]["hash"] = "x" * 40            # other content, other serial
        self.assertNotEqual(json.loads(a)["serialNumber"],
                            json.loads(sbom.render(m, "cyclonedx", "rel-1"))["serialNumber"])


def _with_references():
    m = _manifest()
    m["packages"][3]["references"] = ["cpe:2.3:a:cern:root:6.40.02:*:*:*:*:*:*:*",
                                      "cpe:2.3:a:root-project:root:6.40.02:*:*:*:*:*:*:*"]
    m["packages"][2]["references"] = ["pkg:pypi/cmake@3.30.6"]
    m["packages"][4]["references"] = ["cpe:2.3:a:kkmcee:kkmcee:5.01.00\\+cpu:*:*:*:*:*:*:*"]
    return m


class TestScannerIdentity(unittest.TestCase):
    """A recipe's references (CPEs, purls) name the package for vulnerability
    scanners; without them a package keeps its pkg:github / pkg:generic purl."""

    def _refs(self, refs):
        m = _manifest()
        m["packages"][3]["references"] = refs                  # ROOT
        return m

    def test_cyclonedx_carries_the_first_cpe_and_the_recipes_purl(self):
        m = self._refs(["cpe:2.3:a:cern:root:6.40.02:*:*:*:*:*:*:*", "pkg:generic/root@6.40.02",
                        "cpe:2.3:a:root-project:root:6.40.02:*:*:*:*:*:*:*"])
        root = {c["name"]: c for c in sbom.to_cyclonedx(m)["components"]}["ROOT"]
        self.assertEqual(root["cpe"], "cpe:2.3:a:cern:root:6.40.02:*:*:*:*:*:*:*")
        self.assertEqual(root["purl"], "pkg:generic/root@6.40.02")
        self.assertIn({"name": "syft:cpe23", "value": "cpe:2.3:a:root-project:root:6.40.02:*:*:*:*:*:*:*"},
                      root["properties"])

    def test_without_references_nothing_changes(self):
        root = {c["name"]: c for c in sbom.to_cyclonedx(_manifest())["components"]}["ROOT"]
        self.assertNotIn("cpe", root)
        self.assertTrue(root["purl"].startswith("pkg:github/root-project/root@"))

    def test_spdx_lists_every_cpe_as_a_security_reference(self):
        m = self._refs(["cpe:2.3:a:cern:root:6.40.02:*:*:*:*:*:*:*",
                        "cpe:2.3:a:root-project:root:6.40.02:*:*:*:*:*:*:*"])
        pk = {p["name"]: p for p in sbom.to_spdx(m)["packages"]}
        self.assertEqual([r["referenceLocator"] for r in pk["ROOT"]["externalRefs"]
                          if r["referenceType"] == "cpe23Type"],
                         ["cpe:2.3:a:cern:root:6.40.02:*:*:*:*:*:*:*",
                          "cpe:2.3:a:root-project:root:6.40.02:*:*:*:*:*:*:*"])
        self.assertEqual(pk["ROOT"]["versionInfo"], "v6-40-02")


class TestSPDX(unittest.TestCase):
    def test_document(self):
        doc = sbom.to_spdx(_manifest(), "rel-1")
        self.assertEqual(doc["spdxVersion"], "SPDX-2.3")
        pk = {p["name"]: p for p in doc["packages"]}
        self.assertEqual(pk["ROOT"]["licenseDeclared"], "LGPL-2.1-or-later")
        self.assertEqual(pk["ROOT"]["checksums"], [{"algorithm": "SHA256", "checksumValue": "3" * 64}])
        self.assertEqual(pk["zlib"]["downloadLocation"], "NOASSERTION")
        self.assertIn("Binaries not redistributed", pk["kkmcee"]["comment"])
        # A LicenseRef used by a package is declared in the document.
        self.assertEqual([x["licenseId"] for x in doc["hasExtractedLicensingInfos"]],
                         ["LicenseRef-KKMCee"])
        rel = {(r["spdxElementId"], r["relationshipType"], r["relatedSpdxElement"])
               for r in doc["relationships"]}
        rid = pk["ROOT"]["SPDXID"]
        self.assertIn(("SPDXRef-DOCUMENT", "DESCRIBES", pk["kkmcee"]["SPDXID"]), rel)
        self.assertIn((rid, "DEPENDS_ON", pk["zlib"]["SPDXID"]), rel)
        self.assertIn((pk["CMake"]["SPDXID"], "BUILD_DEPENDENCY_OF", rid), rel)
        ids = {p["SPDXID"] for p in doc["packages"]} | {"SPDXRef-DOCUMENT"}
        self.assertTrue(all(r[0] in ids and r[2] in ids for r in rel))
        self.assertTrue(all(__import__("re").fullmatch(r"SPDXRef-[A-Za-z0-9.-]+", i) for i in ids))

    def test_v3_manifest_has_no_edges(self):
        doc = sbom.to_spdx(_manifest(schema=3), "rel-1")
        self.assertEqual({r["relationshipType"] for r in doc["relationships"]}, {"DESCRIBES"})
        self.assertNotIn("zlib", {p["name"] for p in doc["packages"]})


class TestCLI(unittest.TestCase):
    def test_published_bom_and_errors(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        bom = os.path.join(d, "bom.json")
        with open(bom, "w") as fh:
            json.dump({"build_id": "release-e126", "published_at": "2026-09-30T07:21:49Z",
                       "packages": _manifest()["packages"]}, fh)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            sbom.doSbom(SimpleNamespace(manifest=bom, format="cyclonedx", outDir="-", buildId=None), None)
        doc = json.loads(buf.getvalue())
        self.assertEqual(doc["metadata"]["component"]["name"], "release-e126")
        self.assertEqual(doc["metadata"]["timestamp"], "2026-09-30T07:21:49Z")

        class Parser:
            def error(self, msg):
                raise SystemExit(msg)
        for body in ([1, 2], {"packages": []}):
            with open(bom, "w") as fh:
                json.dump(body, fh)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                sbom.doSbom(SimpleNamespace(manifest=bom, format="spdx", outDir="-", buildId=None),
                            Parser())

    def test_writes_both_files(self):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "m.json")
        with open(path, "w") as fh:
            json.dump(_manifest(schema=3), fh)
        out = os.path.join(d, "out")
        args = SimpleNamespace(manifest=path, format="both", outDir=out, buildId="rel-1")
        with self.assertLogs(level="WARNING") as logs:
            self.assertEqual(sbom.doSbom(args, None), 0)
        self.assertIn("no dependency edges", "\n".join(logs.output))
        self.assertEqual(sorted(os.listdir(out)), ["sbom.cdx.json", "sbom.spdx.json"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sbom.doSbom(SimpleNamespace(manifest=path, format="spdx", outDir="-", buildId="rel-1"), None)
        self.assertEqual(json.loads(buf.getvalue())["name"], "rel-1")


class TestSchemaValidation(unittest.TestCase):
    """Against the official schemas, where the validators are installed."""

    def test_cyclonedx_schema(self):
        try:
            from cyclonedx.schema import SchemaVersion
            from cyclonedx.validation.json import JsonStrictValidator
        except ImportError:
            self.skipTest("cyclonedx-python-lib not installed")
        for m in (_manifest(), _odd_manifest(), _with_references()):
            err = JsonStrictValidator(SchemaVersion.V1_6).validate_str(
                sbom.render(m, "cyclonedx", "rel-1"))
            self.assertIsNone(err, err)

    def test_spdx_validation(self):
        try:
            from spdx_tools.spdx.parser.parse_anything import parse_file
            from spdx_tools.spdx.validation.document_validator import validate_full_spdx_document
        except ImportError:
            self.skipTest("spdx-tools not installed")
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "sbom.spdx.json")
        for m in (_manifest(), _odd_manifest(), _with_references()):
            with open(path, "w") as fh:
                fh.write(sbom.render(m, "spdx", "rel-1"))
            self.assertEqual(validate_full_spdx_document(parse_file(path)), [])


if __name__ == "__main__":
    unittest.main()
