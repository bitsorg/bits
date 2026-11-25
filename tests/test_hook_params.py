# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A recipe's hook_params reach the hooks as written, and only as shell variables."""

import subprocess
import unittest

from bits_helpers.build import hook_params_exports


class HookParamsTest(unittest.TestCase):
  def test_values_as_written(self):
    params = {"AutoReq": "1", "Q": 'say "hi"', "D": "$HOME `id`", "S": "it's"}
    out = subprocess.run(["bash", "-ec", hook_params_exports("rocm", params)
                          + '\nprintf "%s|" "$AutoReq" "$Q" "$D" "$S"'],
                         capture_output=True, text=True, check=True).stdout
    self.assertEqual(out, '1|say "hi"|$HOME `id`|it\'s|')

  def test_a_name_that_is_not_a_variable_is_refused(self):
    for name in ("a b", "x;id", "1A", ""):
      with self.assertRaises(SystemExit):
        hook_params_exports("rocm", {name: "1"})

  def test_none(self):
    self.assertEqual(hook_params_exports("zlib", {}), "")


if __name__ == "__main__":
  unittest.main()
