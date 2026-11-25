# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A recipe's `$(...)` source entry (echoed by bash on the host) gets no credentials."""

import os
import unittest
from unittest.mock import patch

from bits_helpers.workarea import _resolve_source_entry


class SourceExpressionTest(unittest.TestCase):
  def test_no_credentials_but_the_architecture(self):
    env = {"CI_JOB_TOKEN": "t0k", "BITS_STORE_TOKEN": "s3cr", "AWS_SECRET_ACCESS_KEY": "k",
           "CI_REPOSITORY_URL": "https://gitlab-ci-token:t0k@gitlab.cern.ch/x.git", "LANG": "C"}
    expr = "$(${CI_JOB_TOKEN:-none}-${BITS_STORE_TOKEN:-none}-${AWS_SECRET_ACCESS_KEY:-none}" \
           "-${CI_REPOSITORY_URL:-none}-$LANG-$ARCHITECTURE)"
    with patch.dict(os.environ, env):
      self.assertEqual(_resolve_source_entry(expr, "el9_x86-64"),
                       ("none-none-none-none-C-el9_x86-64", True))


if __name__ == "__main__":
  unittest.main()
