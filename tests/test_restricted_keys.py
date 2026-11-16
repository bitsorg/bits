# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The shipped key for bits-store's /restricted builds: one of those, never a manifest's."""

import os
import unittest

from bits_helpers import trust

_KEYS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "keys")


class ShippedRestrictedKeyTest(unittest.TestCase):
    def test_for_restricted_builds_only(self):
        self.assertIn("512d56da4163ae97", trust.load_restricted_keys([_KEYS_DIR]))
        self.assertNotIn("512d56da4163ae97", trust.load_trusted_keys([_KEYS_DIR]))


if __name__ == "__main__":
    unittest.main()
