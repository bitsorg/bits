# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""orch.vuln_triage: the admins' triage of vulnerability findings (VEX)."""

import os
import shutil
import tempfile
import unittest

from console_backend.orch import db, events, vuln_triage

DAY = 86400 * 1000


class TriageTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.conn = db.connect(os.path.join(d, "orch.db"))
        self.addCleanup(self.conn.close)
        db.migrate(self.conn)

    def add(self, vuln="CVE-2024-1", state="false_positive", **kw):
        return vuln_triage.add(self.conn, kw.pop("community", "ATLAS"), vuln, state,
                               kw.pop("reason", "checked"), kw.pop("by", "ann"), **kw)

    def test_a_rule_is_an_event_and_revoked_once(self):
        rid = self.add(package="libxml2", version="2.9.13", days=30, now=1000)
        r = vuln_triage.get(self.conn, rid)
        self.assertEqual((r["community"], r["expires_ms"], r["author"]), ("atlas", 1000 + 30 * DAY, "ann"))
        self.assertTrue(vuln_triage.revoke(self.conn, rid, "bob"))
        self.assertFalse(vuln_triage.revoke(self.conn, rid, "bob"))
        ev = [(e["kind"], e["actor"]["id"]) for e in events.since(self.conn)]
        self.assertEqual(ev, [("sbom.triaged", "ann"), ("sbom.triage_revoked", "bob")])
        self.assertEqual(vuln_triage.listing(self.conn), [])
        self.assertEqual(len(vuln_triage.listing(self.conn, revoked=True)), 1)

    def test_bad_rules_are_refused(self):
        for kw in ({"vuln": "x"}, {"vuln": "CVE 1"}, {"state": "fixed"}, {"state": "not_affected"},
                   {"state": "not_affected", "justification": "because"}, {"version": "1.0"},
                   {"reason": " "}, {"days": 0.5}, {"days": 1000}, {"days": "x"},
                   {"package": "a\nb"}, {"community": ""}):
            with self.assertRaises(vuln_triage.Invalid, msg=kw):
                self.add(**kw)
        self.assertEqual(vuln_triage.listing(self.conn, revoked=True), [])
        rid = self.add(state="not_affected", justification="code_not_reachable")
        self.assertEqual(vuln_triage.get(self.conn, rid)["justification"], "code_not_reachable")

    def test_the_most_specific_rule_covers_a_finding_until_it_expires(self):
        everything = self.add()
        package = self.add(package="LibXML2")
        exact = self.add(package="libxml2", version="2.9.13", days=1, now=0)
        self.add(vuln="CVE-2024-2", package="libxml2")
        rules = vuln_triage.Rules.load(self.conn, now=1)
        rule = lambda name, version, alias="": (rules.rule("atlas", "CVE-2024-1", name, version, alias)
                                                or {}).get("id")
        self.assertEqual(rule("libxml2", "2.9.13"), exact)
        self.assertEqual(rule("libxml2", "2.9.14"), package)
        self.assertEqual(rule("zlib1g", "1.3", alias="libxml2"), package)    # its bits name
        self.assertEqual(rule("zlib", "1.3"), everything)
        self.assertIsNone(rules.rule("lhcb", "CVE-2024-1", "zlib", "1.3"))
        later = vuln_triage.Rules.load(self.conn, now=2 * DAY)
        self.assertEqual(later.rule("atlas", "CVE-2024-1", "libxml2", "2.9.13")["id"], package)

    def test_the_served_sbom_carries_the_rules_as_analysis(self):
        self.add(package="zlib", state="not_affected", justification="code_not_present",
                 reason="we never parse gzip")
        self.add(vuln="CVE-2024-2", state="will_not_fix", reason="accepted")
        comps = {"a": {"name": "libxml2", "version": "2.9.13"},
                 "z": {"name": "zlib1g", "version": "1.3",
                       "properties": [{"name": "bits:package", "value": "zlib"}]}}
        vulns = [{"bom-ref": "vuln-CVE-2024-1", "id": "CVE-2024-1", "affects": [{"ref": "a"}, {"ref": "z"}]},
                 {"bom-ref": "vuln-CVE-2024-2", "id": "CVE-2024-2", "affects": [{"ref": "a"}]},
                 {"bom-ref": "vuln-CVE-3", "id": "CVE-3", "affects": [{"ref": "a"}]}]
        out = vuln_triage.annotate(vulns, comps, vuln_triage.Rules.load(self.conn), "atlas")
        self.assertEqual([(v["bom-ref"], [a["ref"] for a in v["affects"]], v.get("analysis", {}).get("state"))
                          for v in out], [
            ("vuln-CVE-2024-1", ["a"], None),
            ("vuln-CVE-2024-1-triage-1", ["z"], "not_affected"),
            ("vuln-CVE-2024-2-triage-2", ["a"], "exploitable"),
            ("vuln-CVE-3", ["a"], None)])
        self.assertEqual(out[1]["analysis"], {"state": "not_affected", "justification": "code_not_present",
                                              "detail": "we never parse gzip (ann)"})
        self.assertEqual(out[2]["analysis"]["response"], ["will_not_fix"])
        self.assertEqual({p["name"]: p["value"] for p in out[1]["properties"]}["bits:triage:package"], "zlib")
        self.assertNotIn("properties", out[0])
        self.assertEqual(vuln_triage.annotate(vulns, comps, vuln_triage.Rules([]), "atlas"), vulns)


if __name__ == "__main__":
    unittest.main()
