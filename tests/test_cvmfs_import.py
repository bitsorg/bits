"""Tests for bits_helpers/cvmfs_import.parse_module_display (ADR-0001 Stage 2)."""

import unittest

from bits_helpers.cvmfs_import import parse_module_display


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


if __name__ == "__main__":
    unittest.main()
