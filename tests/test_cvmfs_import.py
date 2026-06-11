"""Tests for bits_helpers/cvmfs_import.parse_module_display (ADR-0001 Stage 2)."""

import unittest

from bits_helpers.cvmfs_import import (
    parse_module_display, factor_ops, summarize_options, build_corpus_entry,
    closure_check, compute_corpus_build_id, generate_modulefile,
    generate_init_sh, build_module_meta,
)

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

    def test_prefix_override_relocates(self):
        entry = build_corpus_entry(SAMPLE, PREFIX)
        text = generate_modulefile("ROOT/6.38.00", entry, "bid", prefix="/opt/root")
        self.assertIn("prepend-path PATH /opt/root/bin", text.splitlines())
        self.assertNotIn(PREFIX, text)

    def test_no_build_id_omits_whatis(self):
        entry = build_corpus_entry("prepend-path PATH %s/bin\n" % PREFIX, PREFIX)
        text = generate_modulefile("p/1", entry, "")
        self.assertNotIn("module-whatis", text)


class TestGenerateInitSh(unittest.TestCase):

    def setUp(self):
        self.entry = build_corpus_entry(SAMPLE, PREFIX, version="6.38.00", revision="1")
        self.sh = generate_init_sh("ROOT/6.38.00", self.entry)

    def test_replays_path_ops_as_prepend_exports(self):
        self.assertIn('export PATH="%s/bin${PATH:+:$PATH}"' % PREFIX, self.sh)
        self.assertIn('export ROOTSYS="%s"' % PREFIX, self.sh)

    def test_build_sufficient_extras(self):
        # SAMPLE has no CMAKE_PREFIX_PATH / PKG_CONFIG_PATH → must be added
        self.assertIn("CMAKE_PREFIX_PATH=\"$P", self.sh)
        self.assertIn("$P/lib/pkgconfig", self.sh)
        self.assertIn('[ -d "$P/include" ]', self.sh)
        self.assertIn('export ROOT_ROOT="$P"', self.sh)

    def test_existing_cmake_prefix_not_duplicated(self):
        e = build_corpus_entry("prepend-path CMAKE_PREFIX_PATH %s\n" % PREFIX, PREFIX)
        sh = generate_init_sh("p/1", e)
        self.assertEqual(sh.count("export CMAKE_PREFIX_PATH="), 1)

    def test_prefix_override(self):
        sh = generate_init_sh("ROOT/6.38.00", self.entry, prefix="/opt/r")
        self.assertIn('P="/opt/r"', sh)
        self.assertNotIn(PREFIX, sh)

    def test_dashed_name_sanitised(self):
        e = build_corpus_entry("setenv X 1\n", "/cvmfs/p")
        sh = generate_init_sh("py-foo/1", e)
        self.assertIn("export PY_FOO_ROOT=", sh)


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


if __name__ == "__main__":
    unittest.main()
