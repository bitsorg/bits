# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The macOS sandbox profile lets a build read the disk but not the user's
credentials (store token, ssh, AWS keys, keychains); denied after the read allow,
as SBPL lets a later rule win. The work dir itself is never denied."""

import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers.sandbox import CREDENTIAL_PATHS, make_sbpl_profile


class CredentialReadTest(unittest.TestCase):
  def profile(self, builddir):
    path = make_sbpl_profile(False, builddir)
    self.addCleanup(os.unlink, path)
    with open(path) as f:
      return f.read()

  def test_credentials_are_denied_after_reading_is_allowed(self):
    home = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, home)
    with patch.dict(os.environ, {"HOME": home}):
      text = self.profile(os.path.join(home, "sw"))
    allow = text.index("(allow file-read*)")
    for rel in CREDENTIAL_PATHS:
      rule = '(deny file-read* (subpath "%s"))' % os.path.join(home, rel)
      self.assertGreater(text.index(rule), allow, rel)

  def test_the_configured_key_and_token_files_too(self):
    keys = os.path.join(tempfile.gettempdir(), "elsewhere", "s3keys")
    token = os.path.join(tempfile.gettempdir(), "elsewhere", "store-token")
    gitlab = os.path.join(tempfile.gettempdir(), "elsewhere", "gitlab-token")
    with patch.dict(os.environ, {"BITS_AWS_KEYS_FILE": keys, "BITS_GITLAB_TOKEN_FILE": gitlab}), \
         patch("bits_helpers.source_store.TOKEN_FILE", token):
      text = self.profile(os.path.join(tempfile.gettempdir(), "sw"))
    for f in (keys, token, gitlab):
      self.assertIn('(deny file-read* (subpath "%s"))' % f, text)

  def test_a_work_dir_under_one_stays_readable(self):
    home = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, home)
    with patch.dict(os.environ, {"HOME": home}):
      text = self.profile(os.path.join(home, ".bits", "sw"))
    self.assertNotIn('"%s"' % os.path.join(home, ".bits"), text)
    self.assertIn('"%s"' % os.path.join(home, ".ssh"), text)

  def test_the_accounts_home_too_when_home_is_changed(self):
    import pwd
    with patch.dict(os.environ, {"HOME": tempfile.gettempdir()}):
      text = self.profile(os.path.join(tempfile.gettempdir(), "sw"))
    self.assertIn('"%s"' % os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".ssh"), text)


if __name__ == "__main__":
  unittest.main()
