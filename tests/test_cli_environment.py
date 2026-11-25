# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""`-e NAME=VALUE` adds a binding; `-e NAME` alone is handed to the build script from
bits' environment, as `docker run -e NAME` does, never written on a command line."""

import unittest

from bits_helpers.utilities import cli_environment


class CliEnvironmentTest(unittest.TestCase):
  def test_bindings(self):
    self.assertEqual(cli_environment(["A=1", "B=", "C=x=y", "JALIEN_TOKEN_CERT", "X=given"]),
                     ([("A", "1"), ("B", ""), ("C", "x=y"), ("X", "given")], ["JALIEN_TOKEN_CERT"]))


if __name__ == "__main__":
  unittest.main()
