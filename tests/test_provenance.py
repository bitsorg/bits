# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Tests for bits_helpers/provenance.py (build_id / abi_tag) and the additive
provenance fields in create_provenance_info().

Doubles as the ADR-0001 Stage-0 backward-compatibility guard: the new fields
must be *added* to .meta.json, never replace or drop the pre-existing keys, and
the helpers must never raise on minimal input.
"""

import json
import os
import unittest
from types import SimpleNamespace

from bits_helpers import provenance as pv
from bits_helpers.build import create_provenance_info


def _spec(name, **kw):
    base = {
        "package": name, "version": "1.0", "revision": "1", "hash": "h" + name,
        "tag": None, "source": None,
        "build_requires": [], "runtime_requires": [],
        "full_build_requires": [], "full_runtime_requires": [],
    }
    base.update(kw)
    return base


class TestProvenanceHelpers(unittest.TestCase):

    def test_build_id_deterministic(self):
        specs = {"a": _spec("a"), "b": _spec("b")}
        args = SimpleNamespace(defaults=["release", "gcc15"],
                               architecture="ubuntu2510_x86-64-gcc15-dbg")
        self.assertEqual(pv.compute_build_id(specs, args),
                         pv.compute_build_id(dict(specs), args))

    def test_build_id_sensitive_to_member_hash(self):
        args = SimpleNamespace(defaults=["release"], architecture="x")
        self.assertNotEqual(
            pv.compute_build_id({"a": _spec("a", hash="h1")}, args),
            pv.compute_build_id({"a": _spec("a", hash="h2")}, args))

    def test_build_id_has_readable_label(self):
        args = SimpleNamespace(defaults=["release", "gcc15"], architecture="x")
        self.assertTrue(
            pv.compute_build_id({"a": _spec("a")}, args).startswith("release_gcc15-"))

    def test_build_id_minimal_does_not_crash(self):
        args = SimpleNamespace(defaults=[], architecture="")
        self.assertTrue(pv.compute_build_id({}, args).startswith("local-"))
        # specs lacking a hash are excluded, not fatal
        self.assertTrue(pv.compute_build_id({"x": {"package": "x"}}, args))

    def test_abi_tag_from_arch(self):
        args = SimpleNamespace(architecture="ubuntu2510_x86-64-gcc15-dbg")
        self.assertEqual(pv.compute_abi_tag(args), "ubuntu2510_x86-64-gcc15-dbg")

    def test_abi_tag_appends_cxxstd(self):
        args = SimpleNamespace(architecture="arch")
        os.environ["CXXSTD"] = "23"
        try:
            self.assertEqual(pv.compute_abi_tag(args), "arch+c++23")
        finally:
            os.environ.pop("CXXSTD", None)

    def test_abi_tag_empty_env(self):
        self.assertEqual(pv.compute_abi_tag(SimpleNamespace(architecture="")), "")

    def test_recipe_tools_ref(self):
        self.assertEqual(pv.recipe_tools_ref({}), "")
        self.assertEqual(
            pv.recipe_tools_ref({"bits-recipe-tools": {"version": "0.0.28",
                                                       "hash": "abcdef1234"}}),
            "0.0.28-abcdef12")


class TestProvenanceRecord(unittest.TestCase):
    """create_provenance_info(): new keys are additive, old keys preserved."""

    OLD_KEYS = ("comment", "bits_version", "dist", "architecture",
                "defaults", "package", "dependencies")
    NEW_KEYS = ("build_id", "abi_tag", "reuse_policy", "provenance", "repro",
                "cvmfs_layout")

    def _record(self, args):
        specs = {"a": _spec("a")}
        os.environ["BITS_DIST_HASH"] = "deadbeef"
        try:
            return json.loads(create_provenance_info("a", specs, args))
        finally:
            os.environ.pop("BITS_DIST_HASH", None)

    def test_old_keys_preserved_new_keys_added(self):
        rec = self._record(SimpleNamespace(annotate={}, architecture="arch",
                                           defaults=["release"], reusePolicy="strict"))
        for k in self.OLD_KEYS:
            self.assertIn(k, rec, "pre-existing key %r dropped" % k)
        for k in self.NEW_KEYS:
            self.assertIn(k, rec, "new key %r missing" % k)
        self.assertEqual(rec["reuse_policy"], "strict")
        self.assertEqual(rec["provenance"], "pure")
        self.assertEqual(rec["package"]["hash"], "ha")
        self.assertIsNone(rec["cvmfs_layout"])   # None when args has no layout

    def test_cvmfs_layout_recorded_when_present(self):
        layout = {"cvmfs_dir": "/cvmfs/x", "install_dir": "arch",
                  "module_dir": "arch/modules", "views_dir": "Views",
                  "install_path": "/cvmfs/x/arch", "module_path": "/cvmfs/x/arch/modules",
                  "views_path": "/cvmfs/x/Views"}
        rec = self._record(SimpleNamespace(annotate={}, architecture="arch",
                                           defaults=["release"], cvmfsLayout=layout))
        self.assertEqual(rec["cvmfs_layout"]["views_dir"], "Views")
        self.assertEqual(rec["cvmfs_layout"]["views_path"], "/cvmfs/x/Views")

    def test_reuse_policy_defaults_to_strict_when_arg_absent(self):
        # args without a reuse_policy attribute (the aliBuild simple case)
        rec = self._record(SimpleNamespace(annotate={}, architecture="arch",
                                           defaults=["release"]))
        self.assertEqual(rec["reuse_policy"], "strict")

    def _record_specs(self, specs):
        args = SimpleNamespace(annotate={}, architecture="arch", defaults=["release"])
        os.environ["BITS_DIST_HASH"] = "x"
        try:
            return json.loads(create_provenance_info("a", specs, args))
        finally:
            os.environ.pop("BITS_DIST_HASH", None)

    def test_provenance_loose_when_closure_has_graft(self):
        specs = {
            "a": _spec("a", full_runtime_requires=["dep"], full_build_requires=[]),
            "dep": _spec("dep", from_cvmfs=True),
        }
        self.assertEqual(self._record_specs(specs)["provenance"], "loose")

    def test_provenance_pure_when_no_graft_in_closure(self):
        specs = {
            "a": _spec("a", full_runtime_requires=["dep"], full_build_requires=[]),
            "dep": _spec("dep"),   # locally built, not grafted
        }
        self.assertEqual(self._record_specs(specs)["provenance"], "pure")


if __name__ == "__main__":
    unittest.main()
