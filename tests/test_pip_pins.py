# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""pip pins (bits_helpers/pip_pins.py): read from a bits-store, written once from
what PythonPipRecipe recorded, by a build writing through one."""

import json
import os
import tempfile
import unittest

from bits_helpers import pip_pins

SHA, OTHER = "ab" * 32, "cd" * 32


class Store:
  """A bits-store's MANIFESTS: create-only."""
  def __init__(self, writable=True):
    self.objects, self.writeStore = {}, "bits-store+https://store/store" if writable else ""
    self.puts = 0

  def get_key(self, k):
    return self.objects.get(k)

  def put_key(self, k, data):
    self.puts += 1
    if k in self.objects:
      return False
    self.objects[k] = data
    return True


class PipPinsTest(unittest.TestCase):
  def setUp(self):
    self.dir = tempfile.mkdtemp()
    self.addCleanup(lambda: __import__("shutil").rmtree(self.dir))
    self.spec = {"package": "contourpy", "version": "1.0.7", "recipe": ". $(bits-include PythonPipRecipe)\n"}
    self.specs = {"Python": {"version": "3.13.16"}}

  def recorded(self, file="contourpy-1.0.7.tar.gz", sha=SHA):
    with open(os.path.join(self.dir, pip_pins.SOURCE), "w") as f:
      json.dump({"url": "https://x/" + file, "file": file, "sha256": sha}, f)

  def test_key(self):
    self.assertEqual(pip_pins.key(self.spec, self.specs, "x86_64-el9-gcc14-opt"),
                     "MANIFESTS/pip-pins/x86_64-el9-gcc14-opt/contourpy/1.0.7/3.13-wheel.json")
    spec = dict(self.spec, recipe='PYPI_NAME="Py_Graphics.X"\n. $(bits-include PythonPipRecipe)\n',
                pip_build="source")
    self.assertEqual(pip_pins.key(spec, {}, "a"), "MANIFESTS/pip-pins/a/py-graphics-x/1.0.7/none-source.json")

  def test_first_build_pins_later_ones_read(self):
    store, k = Store(), "MANIFESTS/pip-pins/a/contourpy/1.0.7/3.13-wheel.json"
    self.assertIsNone(pip_pins.read(store, k))
    self.recorded()
    pip_pins.after_build(self.spec, store, k, self.dir)
    want = {"file": "contourpy-1.0.7.tar.gz", "sha256": SHA}
    self.assertEqual(self.spec["pip_source"], want)
    self.assertEqual(pip_pins.read(store, k), want)
    # a build with the pin writes nothing, and one that lost the race keeps the first
    self.recorded(sha=OTHER)
    pip_pins.after_build(dict(self.spec, pip_pin=want), store, k, self.dir)
    self.assertEqual(store.puts, 1)
    pip_pins.after_build(dict(self.spec), store, k, self.dir)
    self.assertEqual((store.puts, pip_pins.read(store, k)), (2, want))

  def test_no_pin_written(self):
    k = "MANIFESTS/pip-pins/a/contourpy/1.0.7/3.13-wheel.json"
    self.recorded()
    for store in (Store(writable=False), object()):          # read only; not a bits-store
      pip_pins.after_build(self.spec, store, k, self.dir)
      self.assertEqual(getattr(store, "objects", {}), {})
    self.assertIsNone(pip_pins.read(object(), k))
    store = Store()
    self.recorded(sha="not a sha")                              # nothing usable recorded
    pip_pins.after_build(self.spec, store, k, self.dir)
    self.assertEqual(store.objects, {})
    store.objects[k] = b"{not json"
    self.assertIsNone(pip_pins.read(store, k))


if __name__ == "__main__":
  unittest.main()
