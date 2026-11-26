# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""An HTTP store download writes its own temporary file, per process and thread:
with one fixed `<dest>.tmp` two fetches of one file wrote into the same file."""

import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import MagicMock

from bits_helpers import sync


class Response:
  status_code, headers = 200, {"content-length": "6"}

  def __init__(self, started, go):
    self.started, self.go = started, go

  def raise_for_status(self):
    pass

  def iter_content(self, chunk_size):
    yield b"abc"
    self.started.set()
    self.go.wait(5)                 # both are half-way when either finishes
    yield b"def"


class TmpNameTest(unittest.TestCase):
  def test_two_fetches_of_one_file(self):
    work = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, work)
    dest = os.path.join(work, "zlib.tar.gz")
    store = sync.HttpRemoteSync("https://store.example", "el9", work, False)
    go, seen, results = threading.Event(), [], []

    def fetch():
      started = threading.Event()
      session = MagicMock(get=lambda *a, **kw: Response(started, go))
      t = threading.Thread(target=lambda: results.append(store.getRetry("https://x/zlib.tar.gz", dest,
                                                                         session=session)))
      t.start()
      started.wait(5)
      seen.append(sorted(f for f in os.listdir(work) if f.endswith(".tmp")))
      return t
    threads = [fetch(), fetch()]
    go.set()
    for t in threads:
      t.join(5)
    self.assertEqual(len(seen[1]), 2)                       # two temporary files at once
    self.assertEqual(results, [True, True])
    with open(dest, "rb") as f:
      self.assertEqual(f.read(), b"abcdef")
    self.assertEqual(os.listdir(work), ["zlib.tar.gz"])


if __name__ == "__main__":
  unittest.main()
