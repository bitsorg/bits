# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""orch.sbom_licences: licence and dependency analysis of the scanned builds."""

import os
import shutil
import tempfile
import unittest

from console_backend.orch import db, sbom_licences as L, sbom_scan, vuln_triage


def comp(name, licence="MIT", version="1", runtime=(), build=(), **props):
    c = {"bom-ref": name + "@1", "name": name, "version": version,
         "properties": [{"name": "bits:" + k, "value": v} for k, v in props.items()]
         + [{"name": "bits:build_requires", "value": b} for b in build]}
    if licence:
        c["licenses"] = [{"expression": licence}]
    return c, {"ref": name + "@1", "dependsOn": [d + "@1" for d in runtime]}


def sbom(*parts, top=("app",)):
    return {"bomFormat": "CycloneDX", "components": [c for c, _ in parts],
            "metadata": {"component": {"bom-ref": "release", "name": "r"}},
            "dependencies": [{"ref": "release", "dependsOn": [t + "@1" for t in top]}]
            + [d for _, d in parts]}


STACK = (comp("app", "GPL-2.0-only", runtime=["libfoo", "CMake", "py"], build=["gcc"]),
         comp("libfoo", "Apache-2.0", runtime=["libgpl3"]),
         comp("libgpl3", "GPL-3.0-or-later"),
         comp("CMake", "BSD-3-Clause", runtime=["agpltool"]),     # a build tool: not followed
         comp("agpltool", "AGPL-3.0-only"),
         comp("py", "LicenseRef-bits-Python", redistributable="none"),
         comp("gcc", "GPL-3.0-or-later WITH GCC-exception-3.1"),  # build only
         comp("orphan", ""),
         comp("zlib", "", provided_by="system"))


class ClassTest(unittest.TestCase):
    def test_classes(self):
        cls = L.licence_class
        self.assertEqual([cls(x) for x in ("MIT", "GPL-2.0-only", "GPL-2.0", "GPL-2.0-or-later",
                                           "GPL-2.0+", "GPL-3.0", "AGPL-3.0", "LGPL-2.1-only",
                                           "MPL-2.0", "LicenseRef-x", "", "NOASSERTION")],
                         ["permissive", "gpl2only", "gpl2only", "gpl2plus", "gpl2plus", "gpl3", "agpl",
                          "lgpl", "weak", "custom", "unknown", "unknown"])
        for unreviewed in ("GNU GPL v2", "Proprietary", "GPL", "GPLv3", "MIT AND", "(MIT"):
            self.assertEqual(cls(unreviewed), "custom", unreviewed)
        self.assertEqual(cls("GPL-3.0-only OR MIT"), "permissive")
        self.assertEqual(cls("MIT AND GPL-3.0-only"), "gpl3")
        self.assertEqual(cls("(GPL-2.0-only)"), "gpl2only")
        self.assertEqual(cls("GPL-2.0-only AND (MIT OR Apache-2.0)"), "gpl2only")
        self.assertEqual(cls("(MIT OR Apache-2.0) AND GPL-3.0-only"), "gpl3")
        self.assertEqual(cls("GPL-3.0-or-later WITH GCC-exception-3.1"), "permissive")
        self.assertEqual(cls("GPL-2.0-only WITH Linux-syscall-note"), "gpl2only")

    def test_conflicts(self):
        self.assertTrue(L.conflict("GPL-2.0-only", "GPL-3.0-only"))
        self.assertTrue(L.conflict("GPL-3.0-only", "GPL-2.0"))
        self.assertTrue(L.conflict("GPL-2.0-only", "Apache-2.0"))
        self.assertTrue(L.conflict("GPL-2.0-only", "Apache-2.0 AND MIT"))
        self.assertIsNone(L.conflict("GPL-2.0-only", "Apache-2.0 OR MIT"))      # MIT chosen
        for dep in ("GPL-2.0-only OR GPL-3.0-only", "GPL-3.0-only OR GPL-2.0-only"):
            self.assertIsNone(L.conflict("GPL-3.0-only", dep), dep)              # either order
        self.assertIsNone(L.conflict("GPL-2.0-only", "LGPL-2.1-only OR Apache-2.0"))
        self.assertIsNone(L.conflict("GPL-2.0-only", "MIT AND (Apache-2.0 OR GPL-2.0-only)"))
        self.assertIsNone(L.conflict("GPL-2.0-only OR GPL-3.0-only", "Apache-2.0"))
        self.assertIsNone(L.conflict("GPL-2.0-only", "Apache-2.0 WITH LLVM-exception"))
        self.assertEqual([L.licence_class(x) for x in ("mit", "apache-2.0", "MPL-2.0+")],
                         ["permissive", "permissive", "weak"])
        self.assertIsNone(L.conflict("GPL-2.0-or-later", "Apache-2.0"))
        self.assertIsNone(L.conflict("MIT", "GPL-3.0-only"))
        self.assertTrue(L.restricted("none") and L.restricted("sources") and not L.restricted("all"))


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.dir = os.path.join(d, "scans")
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)
        self.n = 0

    def build(self, bid, doc, community="lcg", platform="x86_64-el9"):
        self.n += 1
        self.conn.execute("INSERT INTO pipelines (id, is_build, community, package, version, platform,"
                          " first_seen_ms) VALUES (?, 1, ?, 'LCG', '1', ?, 0)", (self.n, community, platform))
        self.conn.execute("INSERT INTO builds (pipeline_id, build_id, community, recorded_ms, published_at)"
                          " VALUES (?, ?, ?, ?, ?)", (self.n, bid, community, self.n, "2026-10-%02d" % self.n))
        self.conn.commit()
        sbom_scan._scanned(self.conn, self.dir, bid, "sha%d" % self.n, "db", {}, [], doc)

    def test_indexed(self):
        self.build("r-aaaaaaaaaaaa", sbom(*STACK))
        g = L.graph(self.conn, "r-aaaaaaaaaaaa")
        app = g["app@1"]
        self.assertEqual((app["license"], app["top"], app["runtime"]),
                         ("GPL-2.0-only", 1, ["CMake@1", "libfoo@1", "py@1"]))
        self.assertEqual(app["links"], ["CMake@1", "gcc@1", "libfoo@1", "py@1"])   # build links too
        self.assertEqual((g["py@1"]["redistributable"], g["libfoo@1"]["top"]), ("none", 0))

    def test_licences(self):
        self.build("r-aaaaaaaaaaaa", sbom(*STACK))
        rules = vuln_triage.Rules([])
        out = L.licences(L.graph(self.conn, "r-aaaaaaaaaaaa"), "lcg", rules)
        (con,) = [f for f in out["findings"] if f["kind"] == "conflict"]
        self.assertEqual((con["package"], con["more"]), ("app", 0))
        self.assertEqual([(u["package"], u["path"]) for u in con["using"]],
                         [("libfoo", ["app", "libfoo"]), ("libgpl3", ["app", "libfoo", "libgpl3"])])  # not agpltool
        self.assertEqual(sorted(f["package"] for f in out["findings"] if f["kind"] == "unknown"),
                         ["orphan", "py"])                                  # not the system zlib
        (reach,) = out["copyleft"]
        self.assertEqual((reach["top"], [c["package"] for c in reach["copyleft"]]), ("app", ["libgpl3"]))
        self.assertEqual(out["restricted"], ["py"])
        self.assertEqual(out["classes"]["gpl2only"], 1)

    def test_accepted_findings_and_the_report(self):
        self.build("r-aaaaaaaaaaaa", sbom(*STACK))
        vuln_triage.add(self.conn, "lcg", L.finding_id("conflict"), "will_not_fix", "dynamic only", "ann",
                        package="app")
        (line,) = L.licence_lines(self.conn)["releases"]
        self.assertEqual((line["open"], line["accepted"]), ({"conflict": 0, "unknown": 2}, 1))
        rel = L.licence_report(self.conn, "r-aaaaaaaaaaaa")
        self.assertTrue(rel["indexed"])
        acc = {(f["kind"], f["package"]): bool(f["rule"]) for f in rel["findings"]}
        self.assertEqual(acc, {("conflict", "app"): True, ("unknown", "orphan"): False,
                               ("unknown", "py"): False})
        self.assertEqual(L.licence_lines(self.conn, communities=set())["releases"], [])
        self.assertIsNone(L.licence_report(self.conn, "r-aaaaaaaaaaaa", communities={"lhcb"}))
        self.conn.execute("UPDATE sbom_scans SET index_version = 2")          # not indexed for it yet
        rel = L.licence_report(self.conn, "r-aaaaaaaaaaaa")
        self.assertEqual((rel["indexed"], "findings" in rel), (False, False))
        self.assertNotIn("open", L.licence_lines(self.conn)["releases"][0])

    def test_dependencies_and_diff(self):
        self.build("r-aaaaaaaaaaaa", sbom(*STACK))
        newer = [c for c in STACK if c[0]["name"] != "orphan"] + [comp("extra")]
        newer[1] = comp("libfoo", "MIT", version="2", runtime=["libgpl3"])
        self.build("r-bbbbbbbbbbbb", sbom(*newer))
        rep = L.dependency_report(self.conn, "r-bbbbbbbbbbbb")
        most = {r["package"]: (r["rebuilds"], r["runtime"]) for r in rep["most_depended"]}
        self.assertEqual(most["libgpl3"], (2, 2))                           # libfoo, app
        self.assertEqual(most["gcc"], (1, 0))                               # app builds with it
        self.assertEqual((rep["build_only"], rep["unneeded"]), (["gcc"], ["extra"]))
        self.assertEqual(rep["previous"], "r-aaaaaaaaaaaa")
        d = rep["diff"]
        self.assertEqual((d["added"], d["removed"]), ([{"package": "extra", "version": "1"}],
                                                      [{"package": "orphan", "version": "1"}]))
        self.assertEqual(d["version"], [{"package": "libfoo", "from": "1", "to": "2"}])
        self.assertEqual(d["licence"], [{"package": "libfoo", "from": "Apache-2.0", "to": "MIT"}])
        self.assertIsNone(L.dependency_report(self.conn, "r-aaaaaaaaaaaa"))  # not the newest

    def test_versions_across_lines(self):
        self.build("r-aaaaaaaaaaaa", sbom(*STACK))
        self.build("s-aaaaaaaaaaaa", sbom(*[comp("app", version="9"), comp("libfoo")]), community="lhcb")
        out = L.dependency_lines(self.conn)
        self.assertEqual([r["line"] for r in out["releases"]], ["r", "s"])
        (app,) = [v for v in out["versions"] if v["package"] == "app"]
        self.assertEqual(sorted(app["versions"]), ["1", "9"])


if __name__ == "__main__":
    unittest.main()
