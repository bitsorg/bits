# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Unit tests for the bits-native S3 store statistics (bits_helpers.store_stats)."""
import unittest

from bits_helpers import store_stats as S


class ParseTests(unittest.TestCase):
    def test_store_object_key(self):
        self.assertEqual(
            S.parse_arch_hash("TARS/x86_64-el9-opt/store/ab/abcd/ROOT-6.opt.tar.gz"),
            ("x86_64-el9-opt", "abcd"))

    def test_non_store_key_outside_prefix(self):
        self.assertEqual(S.parse_arch_hash("MANIFESTS/common.json"), (None, None))

    def test_arch_but_not_store_object(self):
        self.assertEqual(S.parse_arch_hash("TARS/x86_64-el9-opt/something"),
                         ("x86_64-el9-opt", None))


class MapTests(unittest.TestCase):
    def test_hash_to_build_first_build_wins(self):
        builds = [
            {"build_id": "build-B", "packages": [{"hash": "h1"}]},
            {"build_id": "build-A", "packages": [{"hash": "h1"}, {"hash": "h2"}]},
        ]
        m = S.hash_to_build_map(builds)
        self.assertEqual(m["h1"], "build-A")   # sorted -> A wins the shared hash
        self.assertEqual(m["h2"], "build-A")

    def test_signed_from_common_sources(self):
        commons = [{"sources": ["build-A", "build-C"]}, {"sources": ["build-A"]}]
        self.assertEqual(S.signed_builds_from_common(commons), {"build-A", "build-C"})


class SummariseTests(unittest.TestCase):
    def setUp(self):
        self.objs = [
            ("TARS/x86_64-el9-opt/store/ab/h1/ROOT.tar.gz", 100),
            ("TARS/x86_64-el9-opt/store/cd/h2/Boost.tar.gz", 50),
            ("TARS/x86_64-el10-opt/store/ef/h3/ROOT.tar.gz", 80),
            ("MANIFESTS/common.json", 5),
        ]
        self.h2b = {"h1": "build-A", "h2": "build-A", "h3": "build-B"}

    def test_totals_and_arch(self):
        s = S.summarise(self.objs, self.h2b, {"build-A"})
        self.assertEqual(s["total_bytes"], 235)
        self.assertEqual(s["total_objects"], 4)
        self.assertEqual(s["other"], {"bytes": 5, "objects": 1})
        el9 = next(a for a in s["arch"] if a["arch"] == "x86_64-el9-opt")
        self.assertEqual((el9["bytes"], el9["objects"]), (150, 2))

    def test_manifest_breakdown_and_signed(self):
        s = S.summarise(self.objs, self.h2b, {"build-A"})
        by = {(m["manifest"], m["arch"]): m for m in s["manifests"]}
        self.assertEqual(by[("build-A", "x86_64-el9-opt")]["bytes"], 150)
        self.assertTrue(by[("build-A", "x86_64-el9-opt")]["signed"])
        self.assertFalse(by[("build-B", "x86_64-el10-opt")]["signed"])

    def test_unknown_hash_is_uncertified_and_unsigned(self):
        s = S.summarise(self.objs, {}, set())     # no attribution
        m = next(m for m in s["manifests"] if m["arch"] == "x86_64-el9-opt")
        self.assertEqual(m["manifest"], S.UNCERTIFIED)
        self.assertFalse(m["signed"])

    def test_manifests_sorted_desc_by_bytes(self):
        s = S.summarise(self.objs, self.h2b, set())
        sizes = [m["bytes"] for m in s["manifests"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))


class PrometheusTests(unittest.TestCase):
    def test_exposition_has_arch_manifest_and_totals(self):
        s = S.summarise([("TARS/a/store/x/h1/p.tar.gz", 10)],
                        {"h1": "b1"}, {"b1"})
        text = S.to_prometheus(s)
        self.assertIn('bits_store_bytes{arch="a"} 10', text)
        self.assertIn('bits_store_manifest_bytes{manifest="b1",arch="a",signed="1"} 10', text)
        self.assertIn("bits_store_bytes_total 10", text)


if __name__ == "__main__":
    unittest.main()
