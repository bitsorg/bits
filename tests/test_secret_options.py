# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""A secret given on the command line is warned about: the process list and CI logs show it."""

import unittest
from unittest import mock

from bits_helpers import log


class SecretOptionsTest(unittest.TestCase):
  def said(self, argv):
    from bits_helpers import args
    with mock.patch("sys.argv", ["bits"] + argv), \
         mock.patch.object(log, "warning") as warning, \
         mock.patch.object(args, "finaliseArgs", side_effect=SystemExit(0)), \
         self.assertRaises(SystemExit):
      args.doParseArgs()
    return [c[0][1] for c in warning.call_args_list if "command line" in c[0][0]]

  def test_given_spelled_out_joined_or_abbreviated(self):
    self.assertEqual(self.said(["publish", "zlib", "--prepub-tok", "t"]), ["--prepub-token"])
    self.assertEqual(self.said(["build", "--s3-secret-key=k", "zlib"]), ["--s3-secret-key"])
    self.assertEqual(self.said(["compliance", "--gitlab-token", "t"]), ["--gitlab-token"])

  def test_not_given(self):
    self.assertEqual(self.said(["build", "zlib"]), [])

  def test_prepub_token_differing_from_the_environment(self):
    from bits_helpers import cvmfs_publish
    for env, argv, warned in (({}, ["--token", "t"], True),
                              ({"PREPUB_API_TOKEN": "t"}, ["--token", "t"], True),   # "$PREPUB_API_TOKEN"
                              ({"PREPUB_API_TOKEN": "t"}, [], False)):
      with mock.patch.dict("os.environ", env, clear=True), \
           mock.patch.object(log, "warning") as warning, \
           mock.patch.object(cvmfs_publish, "tree_fingerprint", return_value="FP"):
        self.assertEqual(cvmfs_publish.main(["--fingerprint", "."] + argv), 0)
      self.assertEqual(any("command line" in c[0][0] for c in warning.call_args_list), warned)

  def test_a_release_hands_its_token_on_without_the_command_line(self):
    from bits_helpers import cvmfs_publish
    with mock.patch.object(log, "warning") as warning, \
         mock.patch.object(cvmfs_publish, "tree_fingerprint", return_value="FP"):
      self.assertEqual(cvmfs_publish.main(["--fingerprint", "."], token="t"), 0)
    self.assertFalse(warning.called)


if __name__ == "__main__":
  unittest.main()
