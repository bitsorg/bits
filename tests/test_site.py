# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The site configuration: NN-<name>.yaml files, merged in name order."""

import os
import shutil
import tempfile
import unittest

from bits_helpers import site


class SiteTest(unittest.TestCase):
  def setUp(self):
    self.tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.tmp, True)
    self.a, self.b = os.path.join(self.tmp, "a"), os.path.join(self.tmp, "b")
    os.makedirs(self.a)
    os.makedirs(self.b)
    saved = site.SEARCH
    site.SEARCH = [self.a, self.b]
    site.reset()

    def restore():
      site.SEARCH = saved
      site.reset()
    self.addCleanup(restore)

  def write(self, d, name, text):
    with open(os.path.join(d, name), "w") as f:
      f.write(text)
    site.reset()

  def test_name_order_last_wins_and_mappings_merge(self):
    self.write(self.a, "00-bits.yaml", "containers: {registry: r0, tag: latest}\nstore: {read: s0}\n")
    self.write(self.b, "10-site.yaml", "containers: {registry: r1}\n")
    self.write(self.a, "20-host.yaml", "store: {read: null}\n")
    self.write(self.a, "README.md", "not read")
    self.write(self.a, "1-bad-name.yaml", "store: {read: nope}\n")
    self.assertEqual(site.get("containers.registry"), "r1")
    self.assertEqual(site.get("containers.tag"), "latest")
    self.assertIsNone(site.get("store.read"))
    self.assertEqual(site.get("store.read", "x"), "x")
    self.assertEqual(site.origin("containers.registry"), os.path.join(self.b, "10-site.yaml"))
    self.assertEqual([os.path.basename(f) for f in site.files()],
                     ["00-bits.yaml", "10-site.yaml", "20-host.yaml"])

  def test_same_name_in_a_later_directory_replaces_and_empty_masks(self):
    self.write(self.a, "10-CERN.yaml", "store: {operations: cern}\n")
    self.write(self.b, "10-CERN.yaml", "")
    self.assertIsNone(site.get("store.operations"))
    self.assertEqual(site.files(), [os.path.join(self.b, "10-CERN.yaml")])

  def test_a_broken_file_stops_bits_with_its_name(self):
    self.write(self.a, "00-x.yaml", "store: [unclosed\n")
    with self.assertRaises(SystemExit) as cm:
      site.get("store.read")
    self.assertIn("00-x.yaml", str(cm.exception))
    self.write(self.a, "00-x.yaml", "- a list\n")
    with self.assertRaises(SystemExit):
      site.get("store.read")

  def test_read_store_by_architecture_then_default(self):
    self.write(self.a, "00-x.yaml", "store:\n  read: plain\n  read_by_arch: {slc9_x86-64: alice}\n")
    self.assertEqual(site.read_store("slc9_x86-64"), "alice")
    self.assertEqual(site.read_store("x86_64-el9"), "plain")


class ShippedTest(unittest.TestCase):
  def test_shipped_files_keep_the_previous_built_in_values(self):
    saved = site.SEARCH
    site.SEARCH = [site.SHIPPED]
    site.reset()
    self.addCleanup(setattr, site, "SEARCH", saved)
    self.addCleanup(site.reset)
    self.assertEqual([os.path.basename(f) for f in site.files()], ["00-bits.yaml", "10-CERN.yaml"])
    self.assertEqual(site.get("containers.registry"), "gitlab-registry.cern.ch/bits/containers")
    self.assertEqual(site.get("containers.tag"), "latest")
    self.assertEqual(site.get("store.operations"), "https://s3.cern.ch/lcgapp-bits-testing")
    self.assertEqual(site.read_store("slc9_x86-64"), "https://s3.cern.ch/swift/v1/alibuild-repo")
    self.assertEqual(site.read_store("x86_64-el9"), "")
    self.assertEqual(site.get("providers.url"), "https://github.com/bitsorg/bits-providers")
    self.assertEqual(site.get("sbom.namespace"), "https://bits.cern.ch/sbom")


if __name__ == "__main__":
  unittest.main()
