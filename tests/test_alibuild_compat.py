# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Phase-0 consolidation negative control: the minimal aliBuild wrapper contract.

The ``aliBuild`` wrapper (``bits/aliBuild``) sets a fixed set of environment
variables and ``exec``s ``bits "$@"`` — it never reads a config file. These tests
assert the Python argument layer still honours that contract for the essential
command set the community runs through the wrapper (``build``, ``doctor``,
``init``, ``deps``, ``version``), so any consolidation refactor that breaks
aliBuild compatibility fails here rather than in production.

The alidist recipe/hash path itself (legacy init.sh, byte-stable hashing) is
covered by test_hashing.py / test_legacy_initdotsh.py / test_initdotsh_diff.py;
this file is the CLI/env-contract half of the guardrail.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers.args import doParseArgs

# Exactly what bits/aliBuild exports before exec'ing bits.
_ALIBUILD_ENV = {
    "BITS_BRANDING":        "aliBuild",
    "BITS_COMMUNITY":       "ALICE",
    "BITS_PKG_PREFIX":      "VO_ALICE",
    "BITS_LEGACY_INITDOTSH": "1",
    "BITS_REPO_DIR":        "alidist",
}

# The minimal command set the wrapper must keep offering.
_MINIMAL_COMMANDS = {
    "version": ["version"],
    "build":   ["build", "--force-unknown-architecture", "zlib"],
    "doctor":  ["doctor", "zlib"],
    "init":    ["init", "zlib"],
    "deps":    ["deps", "zlib"],
}


def _parse(argv):
    sys.argv = ["aliBuild"] + argv
    args, _ = doParseArgs()
    return args


class AliBuildMinimalWrapperTest(unittest.TestCase):
    """Env contract the aliBuild wrapper relies on must keep working."""

    def setUp(self):
        self._env = patch.dict(os.environ, _ALIBUILD_ENV, clear=False)
        self._env.start()
        # These would otherwise mask the aliBuild defaults under test.
        for k in ("BITS_PATH", "BITS_PROVIDERS", "BITS_ORGANISATION"):
            os.environ.pop(k, None)

    def tearDown(self):
        self._env.stop()

    def test_minimal_command_set_parses_and_dispatches(self):
        for action, argv in _MINIMAL_COMMANDS.items():
            with self.subTest(command=action):
                self.assertEqual(_parse(argv).action, action)

    def test_build_uses_alidist_config_dir(self):
        # $BITS_REPO_DIR seeds the --config-dir default (classic alidist layout).
        args = _parse(["build", "--force-unknown-architecture", "zlib"])
        self.assertEqual(args.configDir, "alidist")

    def test_community_from_env(self):
        # $BITS_COMMUNITY selects the registry/provider home for build too.
        args = _parse(["build", "--force-unknown-architecture", "zlib"])
        self.assertEqual(getattr(args, "organisation", None), "ALICE")
        # Its former name still works; the new one wins.
        with patch.dict(os.environ, {"BITS_ORGANISATION": "LHCB"}):
            os.environ.pop("BITS_COMMUNITY")
            self.assertEqual(_parse(["build", "--force-unknown-architecture", "zlib"]).organisation,
                             "LHCB")
            os.environ["BITS_COMMUNITY"] = "SHIP"
            self.assertEqual(_parse(["build", "--force-unknown-architecture", "zlib"]).organisation,
                             "SHIP")
        self.assertEqual(_parse(["init", "--community", "lhcb"]).organisation, "lhcb")

    def test_alibuild_defaults_to_no_provider_bootstrap(self):
        # Legacy aliBuild path: recipes come from a local alidist checkout, so the
        # built-in bits-providers default is OFF (empty) unless BITS_PROVIDERS is set.
        args = _parse(["build", "--force-unknown-architecture", "zlib"])
        self.assertEqual(args.bits_providers, "")

    def test_explicit_providers_still_wins_under_alibuild(self):
        os.environ["BITS_PROVIDERS"] = "https://example.com/p"
        try:
            args = _parse(["build", "--force-unknown-architecture", "zlib"])
            self.assertEqual(args.bits_providers, "https://example.com/p")
        finally:
            os.environ.pop("BITS_PROVIDERS", None)


class AliBuildWorkDirVariablesTest(unittest.TestCase):
    """The wrapper passes aliBuild's ALIBUILD_WORK_DIR and ALIBUILD_BUILD_WORK_DIR on
    as bits' own variables, unless those are set."""

    def run_wrapper(self, env):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        shutil.copy(os.path.join(os.path.dirname(__file__), "..", "aliBuild"), d)
        with open(os.path.join(d, "bits"), "w") as stub:   # prints what bits would get
            stub.write('#!/bin/bash\necho "${BITS_WORK_DIR:-unset} ${BITS_BUILD_WORK_DIR:-unset}"\n')
        os.chmod(os.path.join(d, "bits"), 0o755)
        base = {k: v for k, v in os.environ.items()
                if not k.startswith(("BITS_", "ALIBUILD_", "ALICE_WORK_DIR"))}
        return subprocess.run(["bash", os.path.join(d, "aliBuild"), "version"],
                              env=dict(base, **env), capture_output=True, text=True,
                              check=True).stdout.split()

    def test_aliBuild_names_are_taken(self):
        self.assertEqual(self.run_wrapper({"ALIBUILD_WORK_DIR": "/w",
                                           "ALIBUILD_BUILD_WORK_DIR": "/fast"}), ["/w", "/fast"])

    def test_bits_names_win(self):
        self.assertEqual(self.run_wrapper({"ALIBUILD_WORK_DIR": "/w", "BITS_WORK_DIR": "/b",
                                           "ALIBUILD_BUILD_WORK_DIR": "/fast",
                                           "BITS_BUILD_WORK_DIR": "/bfast"}), ["/b", "/bfast"])

    def test_alice_work_dir_after_alibuild_work_dir(self):
        self.assertEqual(self.run_wrapper({"ALICE_WORK_DIR": "/a"})[0], "/a")
        self.assertEqual(self.run_wrapper({"ALICE_WORK_DIR": "/a", "ALIBUILD_WORK_DIR": "/w"})[0], "/w")

    def test_empty_counts_as_unset(self):
        self.assertEqual(self.run_wrapper({"BITS_WORK_DIR": "", "ALIBUILD_WORK_DIR": "/w",
                                           "BITS_BUILD_WORK_DIR": "", "ALIBUILD_BUILD_WORK_DIR": "/f"}),
                         ["/w", "/f"])

    def test_neither_set(self):
        self.assertEqual(self.run_wrapper({}), ["unset", "unset"])


if __name__ == "__main__":
    unittest.main()
