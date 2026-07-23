# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A defaults file can select legacy (pre-modules) init.sh via
system: legacy_initdotsh: true — build.apply_defaults_legacy_initdotsh."""

import unittest
from types import SimpleNamespace

from bits_helpers.build import apply_defaults_legacy_initdotsh as apply


class LegacyInitdotshFromDefaultsTestCase(unittest.TestCase):

    def test_flips_and_strips_marker(self):
        args = SimpleNamespace(initdotshFromModules=True)
        dm = {"system": {"legacy_initdotsh": True},
              "env": {"BITS_INITDOTSH_FROM_MODULES": "1", "CFLAGS": "-O2"}}
        self.assertTrue(apply(args, dm, explicit=False))
        self.assertIs(args.initdotshFromModules, False)
        self.assertNotIn("BITS_INITDOTSH_FROM_MODULES", dm["env"])
        self.assertEqual(dm["env"]["CFLAGS"], "-O2")   # other env untouched

    def test_explicit_user_choice_wins(self):
        args = SimpleNamespace(initdotshFromModules=True)
        dm = {"system": {"legacy_initdotsh": True},
              "env": {"BITS_INITDOTSH_FROM_MODULES": "1"}}
        self.assertFalse(apply(args, dm, explicit=True))
        self.assertIs(args.initdotshFromModules, True)
        self.assertIn("BITS_INITDOTSH_FROM_MODULES", dm["env"])

    def test_no_request_is_noop(self):
        args = SimpleNamespace(initdotshFromModules=True)
        self.assertFalse(apply(args, {"system": {}, "env": {}}, explicit=False))
        self.assertIs(args.initdotshFromModules, True)

    def test_already_legacy_noop(self):
        args = SimpleNamespace(initdotshFromModules=False)
        self.assertFalse(apply(args, {"system": {"legacy_initdotsh": "yes"}}, explicit=False))

    def test_top_level_key_honoured(self):
        args = SimpleNamespace(initdotshFromModules=True)
        dm = {"legacy_initdotsh": "true", "env": {"BITS_INITDOTSH_FROM_MODULES": "1"}}
        self.assertTrue(apply(args, dm, explicit=False))
        self.assertIs(args.initdotshFromModules, False)

    def test_falsey_values_do_not_flip(self):
        for v in ("false", "0", "no", "", "off"):
            args = SimpleNamespace(initdotshFromModules=True)
            self.assertFalse(apply(args, {"system": {"legacy_initdotsh": v}}, explicit=False), v)


if __name__ == "__main__":
    unittest.main()
