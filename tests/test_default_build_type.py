# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A chain that qualifies the architecture but names no build type builds -opt:
the same binaries land under one qualified architecture, never also the bare one."""

import os
import shutil
import tempfile
import unittest

from bits_helpers.arch import compute_combined_arch, compute_own_hash_arch
from bits_helpers.defaults import readDefaults

PROFILES = {
    "release": "",
    "key4hep": "",
    "gcc15": "append_arch: -gcc15\n",
    "opt": "append_arch: -opt\nown_hash_neutral: true\nenv:\n  CMAKE_BUILD_TYPE: RELWITHDEBINFO\n",
    "dbg": "append_arch: -dbg\nown_hash_neutral: true\nenv:\n  CMAKE_BUILD_TYPE: DEBUG\n",
    "overlay": "valid_defaults_exempt: true\n",
}


class DefaultBuildTypeTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name, body in PROFILES.items():
            self.profile(name, body)

    def profile(self, name, body):
        with open(os.path.join(self.dir, "defaults-%s.sh" % name), "w") as fh:
            fh.write("package: defaults-%s\nversion: v1\n%s---\n" % (name, body))

    def read(self, chain):
        meta, _body = readDefaults(self.dir, chain, self.fail, "x86_64-el9")
        return meta, compute_combined_arch(meta, chain, "x86_64-el9")

    def test_no_build_type_builds_opt(self):
        chain = ["release", "key4hep", "gcc15"]
        meta, arch = self.read(chain)
        self.assertEqual(chain, ["release", "key4hep", "gcc15", "opt"])    # what is built
        self.assertEqual(arch, "x86_64-el9-gcc15-opt")
        self.assertEqual(meta["env"]["CMAKE_BUILD_TYPE"], "RELWITHDEBINFO")
        self.assertIn("opt", meta["_valid_defaults_exempt"])                # implied, not gated
        # The toolchain still drops the build type from its own arch.
        self.assertEqual(compute_own_hash_arch(meta, chain, "x86_64-el9"), "x86_64-el9-gcc15")

    def test_same_as_naming_it(self):
        named = ["release", "key4hep", "gcc15", "opt"]
        implied = ["release", "key4hep", "gcc15"]
        a, b = self.read(named)[0], self.read(implied)[0]
        b["_valid_defaults_exempt"].remove("opt")
        self.assertEqual(a, b)
        self.assertEqual(named, implied)

    def test_a_named_build_type_is_kept(self):
        chain = ["release", "key4hep", "gcc15", "dbg"]
        _meta, arch = self.read(chain)
        self.assertEqual((chain[-1], arch), ("dbg", "x86_64-el9-gcc15-dbg"))

    def test_overlays_stay_last(self):
        chain = ["release", "key4hep", "gcc15", "overlay"]
        self.read(chain)
        self.assertEqual(chain, ["release", "key4hep", "gcc15", "opt", "overlay"])

    def test_left_alone_without_a_qualified_arch_or_an_opt_profile(self):
        chain = ["release", "key4hep"]
        self.assertEqual(self.read(chain)[1], "x86_64-el9")
        self.assertEqual(chain, ["release", "key4hep"])
        os.unlink(os.path.join(self.dir, "defaults-opt.sh"))
        chain = ["release", "key4hep", "gcc15"]
        self.assertEqual(self.read(chain)[1], "x86_64-el9-gcc15")
        self.assertEqual(chain, ["release", "key4hep", "gcc15"])


if __name__ == "__main__":
    unittest.main()
