# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for bits_helpers/cvmfs_import (ADR-0001 Stage 2)."""

import json
import os
import tempfile
import unittest

from bits_helpers.cvmfs_import import (
    parse_module_display, factor_ops, summarize_options, build_corpus_entry,
    closure_check, compute_corpus_build_id, generate_modulefile,
    build_module_meta,
    AliasMap, corpus_from_manifest, _infer_base_prefix, write_overlay,
    import_release, rewrite_module_anchor,
    strip_base_dep, harvest_trusted, import_trusted_release, overlay_reuse_module,
)
import json as _json
import os as _os
import tempfile as _tempfile
import unittest as _unittest


# A deployed bits modulefile anchored on $::env(BASEDIR) (as pasted from CVMFS).
_DEPLOYED_BOOST = """\
#%Module1.0
set version 1.90.0-1
if ![ is-loaded 'BASE/1.0' ] { module load BASE/1.0 }
set PKG_ROOT $::env(BASEDIR)/Boost/$version
if {[file isdirectory $PKG_ROOT/lib]} { prepend-path LD_LIBRARY_PATH $PKG_ROOT/lib }
setenv BOOST_ROOT $PKG_ROOT
"""


class RewriteModuleAnchorTest(_unittest.TestCase):

    def test_basedir_replaced_with_absolute_base(self):
        out = rewrite_module_anchor(_DEPLOYED_BOOST, "/cvmfs/r/x86_64-el9-gcc15-opt/Packages/")
        # BASEDIR reference gone; PKG_ROOT now absolute; trailing slash trimmed.
        self.assertNotIn("BASEDIR", out)
        self.assertIn("set PKG_ROOT /cvmfs/r/x86_64-el9-gcc15-opt/Packages/Boost/$version", out)
        # Everything else (guard, $version, BASE load, deps) preserved.
        self.assertIn("module load BASE/1.0", out)
        self.assertIn("file isdirectory $PKG_ROOT/lib", out)

    def test_both_env_forms(self):
        self.assertEqual(rewrite_module_anchor("$env(BASEDIR)/a", "/b"), "/b/a")
        self.assertEqual(rewrite_module_anchor("$::env(BASEDIR)/a", "/b"), "/b/a")

    def test_no_basedir_is_noop(self):
        self.assertEqual(rewrite_module_anchor("prepend-path PATH /x/bin\n", "/b"),
                         "prepend-path PATH /x/bin\n")


def _mf(pkg, verrev, deps):
    """A deployed bits modulefile as really published: a multi-line proc block,
    a MULTI-LINE `if { ... }` BASE guard, single-line dep guards, BASEDIR anchor."""
    lines = ["#%%Module1.0",
             "proc ModulesHelp { } {", '  puts stderr "help"', "}",
             "set version %s" % verrev, "# Dependencies",
             "if ![ is-loaded 'BASE/1.0' ] {", " module load BASE/1.0", "}"]
    for d in deps:
        lines.append('if ![ is-loaded "%s" ] { module load %s }' % (d, d))
    lines += ["set PKG_ROOT $::env(BASEDIR)/%s/$version" % pkg,
              "prepend-path PATH $PKG_ROOT/bin",
              "setenv %s_ROOT $PKG_ROOT" % pkg.upper()]
    return "\n".join(lines) + "\n"


def _braces_balanced(text):
    return text.count("{") == text.count("}")


class StripBaseDepTest(_unittest.TestCase):

    def test_strips_only_base(self):
        text = _mf("Boost", "1.90.0-1", ["CMake/3.30.6-1", "Python/3.13.11-1"])
        self.assertTrue(_braces_balanced(text))
        out = strip_base_dep(text)
        # The whole multi-line BASE block is gone — no orphan '}' left behind
        # (regression: the deployment's guard spans `if {` / `module load` / `}`).
        self.assertNotIn("module load BASE", out)
        self.assertNotIn("is-loaded 'BASE", out)
        self.assertTrue(_braces_balanced(out), "unbalanced braces after strip:\n" + out)
        # Real deps and the proc block survive; BASEDIR ref stays (re-anchor removes it).
        self.assertIn("module load CMake/3.30.6-1", out)
        self.assertIn("module load Python/3.13.11-1", out)
        self.assertIn("proc ModulesHelp", out)
        self.assertIn("BASEDIR", out)

    def test_strips_single_line_base_guard(self):
        # The one-line form must still be handled (balanced { } on one line).
        text = "if ![ is-loaded 'BASE/1.0' ] { module load BASE/1.0 }\nset x 1\n"
        out = strip_base_dep(text)
        self.assertNotIn("BASE", out)
        self.assertIn("set x 1", out)
        self.assertTrue(_braces_balanced(out))

    def test_base_prefix_package_not_stripped(self):
        # A package whose id merely starts with "BASE" must be kept.
        text = ('if ![ is-loaded "BASECAMP/1.0" ] { module load BASECAMP/1.0 }\n'
                "if ![ is-loaded 'BASE/1.0' ] { module load BASE/1.0 }\n")
        out = strip_base_dep(text)
        self.assertIn("module load BASECAMP/1.0", out)
        self.assertNotIn("module load BASE/1.0", out)


class HarvestTrustedTest(_unittest.TestCase):

    def _deploy(self, root):
        arch = "x86_64-el9-gcc15-opt"
        mroot = _os.path.join(root, arch, "Modules", "modulefiles")
        proot = _os.path.join(root, arch, "Packages")
        pkgs = {"Boost": ("1.90.0-1", ["CMake/3.30.6-1", "Python/3.13.11-1"], "hB"),
                "CMake": ("3.30.6-1", [], "hC"),
                "Python": ("3.13.11-1", [], "hP")}
        for pkg, (verrev, deps, h) in pkgs.items():
            md = _os.path.join(mroot, pkg); _os.makedirs(md)
            with open(_os.path.join(md, verrev), "w") as fh:
                fh.write(_mf(pkg, verrev, deps))
            pd = _os.path.join(proot, pkg, verrev); _os.makedirs(pd)
            with open(_os.path.join(pd, ".meta.json"), "w") as fh:
                _json.dump({"build_id": "rel-XYZ",
                            "package": {"hash": h, "version": verrev.split("-")[0],
                                        "revision": "1"}}, fh)
        return mroot, proot, arch

    def test_reanchor_hash_and_build_id(self):
        with _tempfile.TemporaryDirectory() as root:
            mroot, proot, arch = self._deploy(root)
            corpus, hashes, build_id = harvest_trusted(mroot, proot)
            self.assertEqual(build_id, "rel-XYZ")
            self.assertEqual(hashes["Boost/1.90.0-1"], "hB")
            boost = corpus["Boost/1.90.0-1"]["rendered"]
            self.assertNotIn("BASEDIR", boost)        # re-anchored
            self.assertNotIn("BASE/1.0", boost)       # BASE dep stripped
            self.assertIn("set PKG_ROOT %s/Boost/$version" % proot, boost)
            self.assertEqual(corpus["Boost/1.90.0-1"]["deps"],
                             ["CMake/3.30.6-1", "Python/3.13.11-1"])

    def test_import_trusted_writes_overlay(self):
        with _tempfile.TemporaryDirectory() as root, \
             _tempfile.TemporaryDirectory() as out:
            mroot, proot, arch = self._deploy(root)
            res = import_trusted_release(mroot, proot, arch, out)
            self.assertEqual(res["build_id"], "rel-XYZ")
            self.assertEqual(res["dangling"], [])     # closure complete
            self.assertEqual(res["overlay_path"],
                             _os.path.join(out, "rel-XYZ", arch))
            mf = _os.path.join(out, "rel-XYZ", arch, "Boost", "1.90.0-1")
            with open(mf) as fh:
                self.assertIn("%s/Boost/$version" % proot, fh.read())
            with open(_os.path.join(out, "rel-XYZ", arch, "Boost",
                                    ".1.90.0-1.meta.json")) as fh:
                self.assertEqual(_json.load(fh)["hash"], "hB")


class OverlayReuseModuleTest(_unittest.TestCase):

    def _overlay(self, root):
        d = _os.path.join(root, "Boost"); _os.makedirs(d)
        open(_os.path.join(d, "1.90.0-1"), "w").close()
        with open(_os.path.join(d, ".1.90.0-1.meta.json"), "w") as fh:
            _json.dump({"hash": "hB"}, fh)
        return root

    def test_strict_hash_match(self):
        with _tempfile.TemporaryDirectory() as ov:
            self._overlay(ov)
            self.assertEqual(overlay_reuse_module(ov, "Boost", want_hash="hB"),
                             "Boost/1.90.0-1")
            self.assertIsNone(overlay_reuse_module(ov, "Boost", want_hash="other"))

    def test_relaxed_any_version(self):
        with _tempfile.TemporaryDirectory() as ov:
            self._overlay(ov)
            self.assertEqual(overlay_reuse_module(ov, "Boost", want_hash=None),
                             "Boost/1.90.0-1")

    def test_missing_package_or_overlay(self):
        with _tempfile.TemporaryDirectory() as ov:
            self._overlay(ov)
            self.assertIsNone(overlay_reuse_module(ov, "Nope", want_hash="hB"))
        self.assertIsNone(overlay_reuse_module(None, "Boost", want_hash="hB"))


class WriteOverlayRenderedTest(_unittest.TestCase):

    def test_rendered_modulefile_written_verbatim(self):
        rendered = rewrite_module_anchor(_DEPLOYED_BOOST, "/cvmfs/r/Packages")
        corpus = {"Boost/1.90.0-1": {"version": "1.90.0", "revision": "1",
                                     "deps": [], "rendered": rendered}}
        with _tempfile.TemporaryDirectory() as out:
            write_overlay(corpus, "bid-1", "x86_64-el9-gcc15-opt", out,
                          package_hashes={"Boost/1.90.0-1": "abc123"})
            mf = _os.path.join(out, "bid-1", "x86_64-el9-gcc15-opt", "Boost", "1.90.0-1")
            with open(mf) as fh:
                text = fh.read()
            # The deployment's own (re-anchored) modulefile, not a regenerated one.
            self.assertEqual(text, rendered)
            meta_path = _os.path.join(_os.path.dirname(mf), ".1.90.0-1.meta.json")
            with open(meta_path) as fh:
                meta = _json.load(fh)
            self.assertEqual(meta["hash"], "abc123")
            self.assertEqual(meta["build_id"], "bid-1")

PREFIX = "/cvmfs/x/ROOT/6.38.00"


# Representative `module show ROOT/6.38.00` output from environment-modules.
SAMPLE = """\
-------------------------------------------------------------------
/cvmfs/sft.cern.ch/lcg/modulefiles/ROOT/6.38.00:

module-whatis\t{ROOT data analysis framework}
conflict\tROOT
prereq\tPython/3.13.11
prereq\tBoost/1.90.0
prepend-path\tPATH /cvmfs/x/ROOT/6.38.00/bin
prepend-path\tLD_LIBRARY_PATH /cvmfs/x/ROOT/6.38.00/lib
prepend-path\t-d : PYTHONPATH /cvmfs/x/ROOT/6.38.00/lib/python
setenv\tROOTSYS /cvmfs/x/ROOT/6.38.00
module load gcc/13
something-weird custom args
-------------------------------------------------------------------
"""


class TestParseModuleDisplay(unittest.TestCase):

    def setUp(self):
        self.r = parse_module_display(SAMPLE)

    def test_env_ops_parsed(self):
        ops = self.r["ops"]
        self.assertIn(("prepend-path", "PATH", "/cvmfs/x/ROOT/6.38.00/bin"), ops)
        self.assertIn(("prepend-path", "LD_LIBRARY_PATH", "/cvmfs/x/ROOT/6.38.00/lib"), ops)
        self.assertIn(("setenv", "ROOTSYS", "/cvmfs/x/ROOT/6.38.00"), ops)

    def test_path_option_flags_stripped(self):
        # "-d :" delimiter flag dropped; VAR/VALUE isolated.
        self.assertIn(("prepend-path", "PYTHONPATH", "/cvmfs/x/ROOT/6.38.00/lib/python"),
                      self.r["ops"])

    def test_deps_from_prereq_and_module_load_deduped_ordered(self):
        self.assertEqual(self.r["deps"], ["Python/3.13.11", "Boost/1.90.0", "gcc/13"])

    def test_metadata_directives_ignored(self):
        flat = " ".join(self.r["verbatim"])
        self.assertNotIn("module-whatis", flat)
        self.assertNotIn("conflict", flat)

    def test_unknown_line_kept_verbatim(self):
        self.assertIn("something-weird custom args", self.r["verbatim"])

    def test_empty_input(self):
        self.assertEqual(parse_module_display(""),
                         {"ops": [], "deps": [], "verbatim": []})

    def test_dep_dedup_across_directives(self):
        r = parse_module_display("prereq Python/3.13.11\nmodule load Python/3.13.11\n")
        self.assertEqual(r["deps"], ["Python/3.13.11"])

    def test_depends_on_directive(self):
        r = parse_module_display("depends-on cmake/3.30 ninja/1.12\n")
        self.assertEqual(r["deps"], ["cmake/3.30", "ninja/1.12"])


class TestFactorAndSummarize(unittest.TestCase):

    def test_factor_ops_replaces_prefix_losslessly(self):
        ops = [("prepend-path", "PYTHONPATH", PREFIX + "/lib/python3.13/site-packages"),
               ("setenv", "ROOTSYS", PREFIX),
               ("prepend-path", "PATH", "/usr/local/bin")]   # outside prefix → unchanged
        self.assertEqual(factor_ops(ops, PREFIX), [
            ("prepend-path", "PYTHONPATH", "$PREFIX/lib/python3.13/site-packages"),
            ("setenv", "ROOTSYS", "$PREFIX"),
            ("prepend-path", "PATH", "/usr/local/bin"),
        ])

    def test_summarize_options(self):
        ops = [
            ("prepend-path", "PATH", PREFIX + "/bin"),
            ("prepend-path", "LD_LIBRARY_PATH", PREFIX + "/lib"),
            ("prepend-path", "CMAKE_PREFIX_PATH", PREFIX),        # dedups into lib
            ("prepend-path", "PYTHONPATH", PREFIX + "/lib/py/sp"),
            ("setenv", "ROOTSYS", PREFIX),                       # not a category
        ]
        self.assertEqual(summarize_options(ops, PREFIX), ["bin", "lib", "python"])


class TestBuildCorpusEntry(unittest.TestCase):

    def test_full_entry(self):
        e = build_corpus_entry(SAMPLE, PREFIX, version="6.38.00", revision="1")
        self.assertEqual(e["version"], "6.38.00")
        self.assertEqual(e["revision"], "1")
        self.assertEqual(e["base_prefix"], PREFIX)
        # env keeps the full factored ops (lossless), incl. the exact python path
        self.assertIn(("prepend-path", "PYTHONPATH", "$PREFIX/lib/python"), e["env"])
        self.assertIn(("setenv", "ROOTSYS", "$PREFIX"), e["env"])
        self.assertEqual(e["options"], ["bin", "lib", "python"])  # no pkgconfig in SAMPLE
        self.assertIn("something-weird custom args", e["verbatim"])
        self.assertEqual(e["deps"], ["Python/3.13.11", "Boost/1.90.0", "gcc/13"])


def _entry(deps=(), base="/cvmfs/x",
           env=(("prepend-path", "LD_LIBRARY_PATH", "$PREFIX/lib"),), verbatim=()):
    return {"base_prefix": base, "env": [tuple(t) for t in env],
            "verbatim": list(verbatim), "deps": list(deps)}


class TestClosureAndBuildId(unittest.TestCase):

    def test_closed_corpus_has_no_dangling(self):
        corpus = {
            "ROOT/6.38.00": _entry(deps=["Python/3.13.11"]),
            "Python/3.13.11": _entry(),
        }
        self.assertEqual(closure_check(corpus), [])

    def test_dangling_edges_reported_sorted(self):
        corpus = {
            "ROOT/6.38.00": _entry(deps=["Python/3.13.11", "Boost/1.90.0"]),
            "Python/3.13.11": _entry(),
        }
        self.assertEqual(closure_check(corpus), ["Boost/1.90.0"])

    def test_build_id_deterministic_and_labelled(self):
        corpus = {"ROOT/6.38.00": _entry(deps=["Python/3.13.11"]),
                  "Python/3.13.11": _entry()}
        a = compute_corpus_build_id(corpus, "LCG_109")
        b = compute_corpus_build_id(dict(reversed(list(corpus.items()))), "LCG_109")
        self.assertTrue(a.startswith("LCG_109-"))
        self.assertEqual(a, b)   # insertion order independent

    def test_build_id_changes_with_content(self):
        c1 = {"ROOT/6.38.00": _entry(env=[("prepend-path", "PATH", "$PREFIX/bin")])}
        c2 = {"ROOT/6.38.00": _entry(env=[("prepend-path", "PATH", "$PREFIX/sbin")])}
        self.assertNotEqual(compute_corpus_build_id(c1, "L"),
                            compute_corpus_build_id(c2, "L"))


class TestGenerateModulefile(unittest.TestCase):

    def test_regenerates_targeted_modulefile(self):
        entry = build_corpus_entry(SAMPLE, PREFIX, version="6.38.00", revision="1")
        text = generate_modulefile("ROOT/6.38.00", entry, "LCG_109-abc123def456")
        lines = text.splitlines()
        self.assertEqual(lines[0], "#%Module1.0")
        # build_id is queryable, not an env var
        self.assertIn('module-whatis "build_id: LCG_109-abc123def456"', lines)
        # deps become prereqs
        self.assertIn("prereq Python/3.13.11", lines)
        self.assertIn("prereq gcc/13", lines)
        # $PREFIX re-targeted to the deployed path (lossless python sub-path kept)
        self.assertIn("prepend-path PATH %s/bin" % PREFIX, lines)
        self.assertIn("prepend-path PYTHONPATH %s/lib/python" % PREFIX, lines)
        self.assertIn("setenv ROOTSYS %s" % PREFIX, lines)
        # verbatim oddity preserved
        self.assertIn("something-weird custom args", lines)

    def test_build_sufficient_hooks_in_modulefile(self):
        # SAMPLE sets no CMAKE_PREFIX_PATH/PKG_CONFIG_PATH/CPATH → all added,
        # the pkgconfig/include ones guarded on the deployed tree.
        entry = build_corpus_entry(SAMPLE, PREFIX)
        text = generate_modulefile("ROOT/6.38.00", entry, "bid")
        self.assertIn("prepend-path CMAKE_PREFIX_PATH %s" % PREFIX, text)
        self.assertIn('if {[file isdirectory "%s/lib/pkgconfig"]} {' % PREFIX, text)
        self.assertIn("prepend-path PKG_CONFIG_PATH %s/lib/pkgconfig" % PREFIX, text)
        self.assertIn('if {[file isdirectory "%s/include"]} {' % PREFIX, text)
        self.assertIn("setenv ROOT_ROOT %s" % PREFIX, text)

    def test_existing_cmake_prefix_not_redeclared(self):
        entry = build_corpus_entry("prepend-path CMAKE_PREFIX_PATH %s\n" % PREFIX, PREFIX)
        text = generate_modulefile("p/1", entry, "bid")
        self.assertEqual(text.count("prepend-path CMAKE_PREFIX_PATH"), 1)

    def test_prefix_override_relocates(self):
        entry = build_corpus_entry(SAMPLE, PREFIX)
        text = generate_modulefile("ROOT/6.38.00", entry, "bid", prefix="/opt/root")
        self.assertIn("prepend-path PATH /opt/root/bin", text.splitlines())
        self.assertNotIn(PREFIX, text)

    def test_no_build_id_omits_whatis(self):
        entry = build_corpus_entry("prepend-path PATH %s/bin\n" % PREFIX, PREFIX)
        text = generate_modulefile("p/1", entry, "")
        self.assertNotIn("module-whatis", text)


class TestModulefileNaming(unittest.TestCase):

    def test_dashed_name_sanitised_in_root_var(self):
        e = build_corpus_entry("setenv X 1\n", "/cvmfs/p")
        text = generate_modulefile("py-foo/1", e, "bid")
        self.assertIn("setenv PY_FOO_ROOT /cvmfs/p", text)


class TestBuildModuleMeta(unittest.TestCase):

    def test_carries_identity_and_build_id(self):
        entry = build_corpus_entry(SAMPLE, PREFIX, version="6.38.00", revision="1")
        m = build_module_meta("ROOT/6.38.00", entry, "LCG_109-abc", package_hash="h1",
                              abi_tag="x86-64-gcc15")
        self.assertEqual(m["package"], "ROOT")
        self.assertEqual(m["version"], "6.38.00")
        self.assertEqual(m["revision"], "1")
        self.assertEqual(m["build_id"], "LCG_109-abc")
        self.assertEqual(m["hash"], "h1")
        self.assertEqual(m["abi_tag"], "x86-64-gcc15")
        self.assertEqual(m["base_prefix"], PREFIX)
        self.assertEqual(m["deps"], ["Python/3.13.11", "Boost/1.90.0", "gcc/13"])
        self.assertTrue(m["imported"])


class TestAliasMap(unittest.TestCase):

    def test_bidirectional_with_identity_fallback(self):
        a = AliasMap({"ROOT": "root", "Boost": "boost"})
        self.assertEqual(a.to_bits("ROOT"), "root")
        self.assertEqual(a.to_foreign("root"), "ROOT")
        self.assertEqual(a.to_bits("Unknown"), "Unknown")   # identity passthrough
        self.assertEqual(a.to_foreign("unknown"), "unknown")

    def test_unmapped_reports_gaps(self):
        a = AliasMap({"ROOT": "root"})
        self.assertEqual(a.unmapped(["ROOT", "Boost", "Python", "Boost"]),
                         ["Boost", "Python"])

    def test_load_dict_and_list_forms(self):
        with tempfile.TemporaryDirectory() as d:
            p1 = os.path.join(d, "m1.json")
            with open(p1, "w") as fh:
                json.dump({"ROOT": "root"}, fh)
            self.assertEqual(AliasMap.load(p1).to_bits("ROOT"), "root")
            p2 = os.path.join(d, "m2.json")
            with open(p2, "w") as fh:
                json.dump({"aliases": [["Boost", "boost"]]}, fh)
            self.assertEqual(AliasMap.load(p2).to_bits("Boost"), "boost")
            self.assertEqual(AliasMap.load(os.path.join(d, "nope.json")).to_bits("x"), "x")


class TestManifestAndInference(unittest.TestCase):

    def test_corpus_from_manifest(self):
        man = {"packages": [{
            "module_id": "ROOT/6.38.00", "base_prefix": "/cvmfs/x/ROOT/6.38.00",
            "version": "6.38.00", "revision": "1",
            "env": [["prepend-path", "PATH", "$PREFIX/bin"]],
            "deps": ["Python/3.13.11"],
        }]}
        c = corpus_from_manifest(man)
        self.assertEqual(c["ROOT/6.38.00"]["env"], [("prepend-path", "PATH", "$PREFIX/bin")])
        self.assertEqual(c["ROOT/6.38.00"]["deps"], ["Python/3.13.11"])

    def test_infer_base_prefix_from_bin(self):
        ops = [("prepend-path", "PATH", "/cvmfs/x/ROOT/6.38.00/bin"),
               ("prepend-path", "LD_LIBRARY_PATH", "/cvmfs/x/ROOT/6.38.00/lib")]
        self.assertEqual(_infer_base_prefix(ops), "/cvmfs/x/ROOT/6.38.00")

    def test_infer_base_prefix_falls_back_to_lib(self):
        ops = [("prepend-path", "LD_LIBRARY_PATH", "/cvmfs/x/Q/1.0/lib")]
        self.assertEqual(_infer_base_prefix(ops), "/cvmfs/x/Q/1.0")


class TestWriteOverlay(unittest.TestCase):

    def _corpus(self):
        return {
            "ROOT/6.38.00": build_corpus_entry(SAMPLE, PREFIX, version="6.38.00", revision="1"),
            "Python/3.13.11": build_corpus_entry(
                "prepend-path PATH /cvmfs/x/Python/3.13.11/bin\n",
                "/cvmfs/x/Python/3.13.11", version="3.13.11", revision="1"),
            "Boost/1.90.0": build_corpus_entry(
                "prepend-path LD_LIBRARY_PATH /cvmfs/x/Boost/1.90.0/lib\n",
                "/cvmfs/x/Boost/1.90.0", version="1.90.0", revision="1"),
            "gcc/13": build_corpus_entry("setenv CC /cvmfs/x/gcc/13/bin/gcc\n",
                                         "/cvmfs/x/gcc/13", version="13", revision="1"),
        }

    def test_layout_and_alias_remap(self):
        alias = AliasMap({"ROOT": "root", "Python": "python", "Boost": "boost"})
        with tempfile.TemporaryDirectory() as out:
            res = import_release(self._corpus(), "LCG_109", "x86_64-el9-gcc13",
                                 out, alias=alias, abi_tag="x86-64-gcc13")
            bid = res["build_id"]
            self.assertTrue(bid.startswith("LCG_109-"))
            self.assertEqual(res["dangling"], [])
            self.assertIn("root/6.38.00", res["written"])   # remapped name
            arch_root = os.path.join(out, bid, "x86_64-el9-gcc13")
            # modulefile present at name/version
            mf = os.path.join(arch_root, "root", "6.38.00")
            self.assertTrue(os.path.isfile(mf))
            text = open(mf).read()
            self.assertIn('module-whatis "build_id: %s"' % bid, text)
            # dep edge remapped to bits name in the prereq
            self.assertIn("prereq python/3.13.11", text)
            # modulefile is the single env artifact — no init.sh sidecar
            self.assertFalse(os.path.exists(os.path.join(arch_root, "root", ".6.38.00.init.sh")))
            self.assertIn("setenv ROOT_ROOT /cvmfs/x/ROOT/6.38.00", text)
            meta = json.load(open(os.path.join(arch_root, "root", ".6.38.00.meta.json")))
            self.assertEqual(meta["build_id"], bid)
            self.assertEqual(meta["abi_tag"], "x86-64-gcc13")
            # one nested catalog per build_id
            self.assertTrue(os.path.isfile(os.path.join(out, bid, ".cvmfscatalog")))

    def test_path_traversal_module_id_is_refused(self):
        corpus = {
            "ROOT/6.38.00": build_corpus_entry(SAMPLE, PREFIX, version="6.38.00"),
            "../evil/1": build_corpus_entry("setenv X 1\n", "/cvmfs/p", version="1"),
            "ok/../../etc": build_corpus_entry("setenv Y 1\n", "/cvmfs/q", version="x"),
        }
        with tempfile.TemporaryDirectory() as out:
            res = import_release(corpus, "L", "arch", out, force=True)
            # the safe module is written; the traversal ones are dropped
            self.assertIn("ROOT/6.38.00", res["written"])
            self.assertNotIn("../evil/1", res["written"])
            # nothing was written outside the build_id overlay
            escaped = os.path.join(os.path.dirname(out), "evil")
            self.assertFalse(os.path.exists(escaped))
            self.assertFalse(os.path.exists(os.path.join(out, "..", "evil")))

    def test_non_closed_corpus_refused(self):
        corpus = {"ROOT/6.38.00": build_corpus_entry(SAMPLE, PREFIX)}  # deps dangle
        with tempfile.TemporaryDirectory() as out:
            res = import_release(corpus, "L", "arch", out)
            self.assertIsNone(res["build_id"])
            self.assertEqual(res["written"], [])
            self.assertTrue(res["dangling"])
            self.assertEqual(os.listdir(out), [])   # nothing written


if __name__ == "__main__":
    unittest.main()
