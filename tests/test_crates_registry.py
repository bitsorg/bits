# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The site's crates registry (bits_helpers.crates_registry): cargo's configuration in
build containers, asked once per run, upstream when it does not answer."""

import os
import tempfile
import unittest
from unittest.mock import patch

from bits_helpers import crates_registry

URL = "https://store.example.org:8444/crates"


class CratesRegistryTest(unittest.TestCase):
  def setUp(self):
    crates_registry._answers.clear()
    self.addCleanup(crates_registry._answers.clear)
    self.work = tempfile.mkdtemp()
    self.addCleanup(__import__("shutil").rmtree, self.work, True)

  def test_configured(self):
    with patch.object(crates_registry.site, "get", lambda k, d=None: URL + "/"):
      env = {k: v for k, v in os.environ.items() if k != "BITS_CRATES_REGISTRY"}
      with patch.dict(os.environ, env, clear=True):
        self.assertEqual(crates_registry.configured(), URL)                  # the site's
      with patch.dict(os.environ, {"BITS_CRATES_REGISTRY": ""}):
        self.assertEqual(crates_registry.configured(), "")                   # set empty: none

  def test_the_container_reads_it_at_the_filesystem_root(self):
    with patch.dict(os.environ, {"BITS_CRATES_REGISTRY": URL}), \
         patch.object(crates_registry, "_up", lambda url: True):
      volume = crates_registry.container_volume(self.work)
    path, target = volume.split(":", 1)
    self.assertEqual(target, "/.cargo/config.toml:ro")
    with open(path) as f:
      text = f.read()
    self.assertIn('[source.crates-io]\nreplace-with = "bits-store"', text)
    self.assertIn('registry = "sparse+%s/index/"' % URL, text)

  def test_none_without_a_registry_or_when_it_does_not_answer(self):
    with patch.dict(os.environ, {"BITS_CRATES_REGISTRY": ""}):
      self.assertIsNone(crates_registry.container_volume(self.work))
    with patch.dict(os.environ, {"BITS_CRATES_REGISTRY": URL}), \
         patch.object(crates_registry.requests, "get", side_effect=crates_registry.requests.ConnectionError) as get:
      self.assertIsNone(crates_registry.container_volume(self.work))
      self.assertIsNone(crates_registry.container_volume(self.work))
    self.assertEqual(get.call_count, 1)                                     # asked once per run
    self.assertEqual(get.call_args[0][0], "https://store.example.org:8444/health")

  def test_cargo_takes_it(self):
    """A real cargo reads the configuration: its registry replaces crates.io."""
    import shutil, subprocess
    if not shutil.which("cargo"):
      self.skipTest("no cargo")
    home = os.path.join(self.work, "home")
    os.makedirs(home)
    with open(os.path.join(home, "config.toml"), "w") as f:
      f.write(crates_registry.cargo_config("http://127.0.0.1:9/crates"))
    out = subprocess.run(["cargo", "config", "get", "-Zunstable-options", "source"], capture_output=True, text=True,
                         cwd=self.work, env=dict(os.environ, CARGO_HOME=home, RUSTC_BOOTSTRAP="1"))
    if out.returncode:
      self.skipTest("cargo config get: %s" % out.stderr.strip()[:100])
    self.assertIn('source.crates-io.replace-with = "bits-store"', out.stdout)
    self.assertIn('sparse+http://127.0.0.1:9/crates/index/', out.stdout)


if __name__ == "__main__":
  unittest.main()
