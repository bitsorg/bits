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
import subprocess
import sys
from copy import deepcopy
from unittest.mock import patch
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
        self.assertEqual(list(rec)[-1], "dependency_graph")
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

    def test_pkg_family_defaults_to_empty_string(self):
        specs = {
            "a": _spec("a", pkg_family="apps", runtime_requires=["dep", "plain"],
                       full_runtime_requires=["dep", "plain"]),
            "dep": _spec("dep", pkg_family="libs"),
            "plain": _spec("plain"),
        }
        rec = self._record_specs(specs)
        self.assertEqual(rec["package"]["pkg_family"], "apps")
        for kind in ("direct", "recursive"):
            deps = rec["dependencies"][kind]["runtime"]
            self.assertEqual(deps[0]["pkg_family"], "libs")
            self.assertEqual(deps[1]["pkg_family"], "")
        del specs["a"]["pkg_family"]
        self.assertEqual(self._record_specs(specs)["package"]["pkg_family"], "")

    def test_release_view_keeps_shared_and_own_hash_packages(self):
        # `bits publish --release-view` keeps packages whose top-level architecture
        # is the build arch; noarch and toolchain packages must not drop out.
        import tempfile
        from bits_helpers.view import collect_build_id_roots
        specs = {
            "a": _spec("a", runtime_requires=["s", "t"], full_runtime_requires=["s", "t"]),
            "s": _spec("s", architecture="share"),
            "t": _spec("t", _own_hash_arch="neutral-arch"),
        }
        args = SimpleNamespace(annotate={}, architecture="arch", defaults=["release"])
        os.environ["BITS_DIST_HASH"] = "x"
        try:
            with tempfile.TemporaryDirectory() as root:
                for name in specs:
                    os.makedirs(os.path.join(root, name))
                    with open(os.path.join(root, name, ".meta.json"), "w") as fh:
                        fh.write(create_provenance_info(name, specs, args))
                bid = json.loads(create_provenance_info("a", specs, args))["build_id"]
                roots = collect_build_id_roots(root, bid, architecture="arch")
                self.assertEqual(sorted(os.path.basename(r) for r in roots), ["a", "s", "t"])
        finally:
            os.environ.pop("BITS_DIST_HASH", None)

    def test_effective_architecture_recorded_for_packages_and_dependencies(self):
        for overrides, expected in (({}, "arch"), ({"architecture": "share"}, "share"),
                                    ({"_own_hash_arch": "neutral-arch"}, "neutral-arch")):
            with self.subTest(overrides=overrides):
                specs = {
                    "a": _spec("a", build_requires=["shared", "native"],
                               runtime_requires=["shared", "native"],
                               full_build_requires=["shared", "native"],
                               full_runtime_requires=["shared", "native"], **overrides),
                    "shared": _spec("shared", architecture="share"),
                    "native": _spec("native"),
                }
                original = deepcopy(specs)
                rec = self._record_specs(specs)
                # Top level stays the build arch: release views filter on it.
                self.assertEqual(rec["architecture"], "arch")
                self.assertEqual(rec["effective_architecture"], expected)
                self.assertEqual(rec["package"]["effective_architecture"], expected)
                for kind in ("direct", "recursive"):
                    for scope in ("build", "runtime"):
                        self.assertEqual(
                            {dep["name"]: dep["effective_architecture"]
                             for dep in rec["dependencies"][kind][scope]},
                            {"shared": "share", "native": "arch"})
                self.assertEqual(specs, original)

    def test_dependency_graph_runtime_closure_without_defaults(self):
        specs = {
            "a": _spec("a", runtime_requires=["beta", "alpha", "alpha", "defaults-release"],
                       build_requires=["compiler"], untracked_requires=["plugin"]),
            "alpha": _spec("alpha", runtime_requires=["base"]),
            "beta": _spec("beta", runtime_requires=["base"]),
            "base": _spec("base", runtime_requires=["defaults-release"]),
            "plugin": _spec("plugin", runtime_requires=["base"]),
            "compiler": _spec("compiler"),
            "defaults-release": _spec("defaults-release", runtime_requires=["provider"]),
            "provider": _spec("provider"),
        }
        original = deepcopy(specs)
        expected = {"a": ["alpha", "beta", "plugin"], "alpha": ["base"],
                    "base": [], "beta": ["base"], "plugin": ["base"]}
        with patch("bits_helpers.build.topological_sort", side_effect=AssertionError("must not sort")), \
             patch("bits_helpers.deps._makefile_order", side_effect=AssertionError("must not sort")):
            graph = self._record_specs(specs)["dependency_graph"]
        self.assertEqual(json.dumps(graph), json.dumps(expected))
        self.assertEqual(specs, original)
        specs["unrelated"] = _spec("unrelated", runtime_requires=["beta"])
        self.assertEqual(json.dumps(self._record_specs(specs)["dependency_graph"]), json.dumps(expected))
        self.assertEqual(self._record_specs({"a": _spec("a")})["dependency_graph"], {"a": []})

    def test_dependency_metadata_stable_across_processes(self):
        code = """
import json
from types import SimpleNamespace
from tests.test_provenance import _spec
from bits_helpers.build import create_provenance_info
edges = {"a": ["z", "b"], "z": ["base"], "b": ["base"], "base": []}
specs = {p: _spec(p, runtime_requires=set(edges[p])) for p in set(edges)}
specs["a"]["runtime_requires"] = ["z", "b"]
specs["a"]["full_runtime_requires"] = {"z", "base", "b"}
specs["a"]["full_build_requires"] = {"tool2", "tool1"}
specs.update({p: _spec(p) for p in ("tool2", "tool1")})
args = SimpleNamespace(annotate={}, architecture="arch", defaults=["release"])
for extra in (False, True):
    if extra:
        specs["unrelated"] = _spec("unrelated", runtime_requires=["z"])
    rec = json.loads(create_provenance_info("a", specs, args))
    print(json.dumps([rec["dependency_graph"], rec["dependencies"]]))
"""
        outputs = []
        for seed in ("1", "3", "42"):
            output = subprocess.check_output(
                [sys.executable, "-c", code], text=True,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                env=dict(os.environ, PYTHONHASHSEED=seed, BITS_DIST_HASH="x"))
            lines = output.splitlines()
            self.assertEqual(lines[0], lines[1])
            outputs.append(lines[0])
        self.assertEqual(len(set(outputs)), 1)
        graph, deps = json.loads(outputs[0])
        self.assertEqual(list(graph), ["a", "b", "base", "z"])
        self.assertEqual([d["name"] for d in deps["direct"]["runtime"]], ["z", "b"])
        self.assertEqual([d["name"] for d in deps["recursive"]["runtime"]], ["b", "base", "z"])
        self.assertEqual([d["name"] for d in deps["recursive"]["build"]], ["tool1", "tool2"])

    def test_provenance_pure_when_closure_is_clean(self):
        # No untracked_requires anywhere in the closure -> pure. (The legacy
        # cvmfs:// graft that also produced "loose" was removed in Step 5.)
        specs = {
            "a": _spec("a", full_runtime_requires=["dep"], full_build_requires=[]),
            "dep": _spec("dep"),   # locally built
        }
        self.assertEqual(self._record_specs(specs)["provenance"], "pure")

    def test_provenance_loose_when_closure_has_untracked(self):
        # untracked_requires decouples a dependency from the hash -> loose.
        specs = {
            "a": _spec("a", untracked_requires=["dep"]),
            "dep": _spec("dep"),
        }
        self.assertEqual(self._record_specs(specs)["provenance"], "loose")


class TestBuildIdFromManifest(unittest.TestCase):
    """build_id_from_manifest reconstructs the canonical id from a manifest dict."""

    def _manifest(self, defaults, pkgs):
        return {"defaults": defaults, "packages": pkgs}

    def test_matches_compute_build_id(self):
        pkgs = [
            {"package": "a", "version": "1", "revision": "1", "hash": "h1"},
            {"package": "b", "version": "2", "revision": "3", "hash": "h2"},
        ]
        m = self._manifest(["release", "gcc15"], pkgs)
        specs = {p["package"]: p for p in pkgs}
        args = SimpleNamespace(defaults=["release", "gcc15"], architecture="x")
        self.assertEqual(pv.build_id_from_manifest(m), pv.compute_build_id(specs, args))

    def test_order_independent(self):
        pkgs = [
            {"package": "a", "version": "1", "revision": "1", "hash": "h1"},
            {"package": "b", "version": "2", "revision": "3", "hash": "h2"},
        ]
        a = pv.build_id_from_manifest(self._manifest(["release"], pkgs))
        b = pv.build_id_from_manifest(self._manifest(["release"], list(reversed(pkgs))))
        self.assertEqual(a, b)

    def test_label_from_defaults(self):
        m = self._manifest(["release", "gcc15"],
                           [{"package": "a", "hash": "h1"}])
        self.assertTrue(pv.build_id_from_manifest(m).startswith("release_gcc15-"))

    def test_hashless_packages_excluded(self):
        with_sys = self._manifest(["release"], [
            {"package": "a", "hash": "h1"},
            {"package": "sys", "hash": ""},      # system pkg, no content hash
        ])
        without = self._manifest(["release"], [{"package": "a", "hash": "h1"}])
        self.assertEqual(pv.build_id_from_manifest(with_sys),
                         pv.build_id_from_manifest(without))

    def test_unusable_input_never_raises(self):
        self.assertEqual(pv.build_id_from_manifest("not a dict"), "")
        self.assertTrue(pv.build_id_from_manifest({}).startswith("local-"))


if __name__ == "__main__":
    unittest.main()
