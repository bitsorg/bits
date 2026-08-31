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
import sys
import unittest
from unittest.mock import patch

from bits_helpers.args import doParseArgs

# Exactly what bits/aliBuild exports before exec'ing bits.
_ALIBUILD_ENV = {
    "BITS_BRANDING":        "aliBuild",
    "BITS_ORGANISATION":    "ALICE",
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
        for k in ("BITS_PATH", "BITS_PROVIDERS"):
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

    def test_organisation_from_env(self):
        # $BITS_ORGANISATION selects the registry/provider home for build too.
        args = _parse(["build", "--force-unknown-architecture", "zlib"])
        self.assertEqual(getattr(args, "organisation", None), "ALICE")

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


if __name__ == "__main__":
    unittest.main()
