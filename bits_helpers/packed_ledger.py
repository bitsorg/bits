# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tarballs that builds in this work dir packed: the signed-reuse gate exempts only them.

When a build packs its tarball, build_template.sh writes its sha256 to
``PACKED/<the tarball's path in the work dir>.sha256``, before the tarball takes
its place (a --confine-work-dir build may write only its own entry). A tarball
found later is "packed here" only while it is a plain file with that sha256: one
left by a store download, an interrupted run or another bits process is not. A
tarball the gate refuses goes to ``QUARANTINE/``, kept but never unpacked.
"""

import hashlib
import os

LEDGER = "PACKED"
QUARANTINE = "QUARANTINE"


def _entry(work_dir, tarball):
  return os.path.join(work_dir, LEDGER, os.path.relpath(tarball, work_dir) + ".sha256")


def _sha256(path):
  h = hashlib.sha256()
  with open(path, "rb") as f:
    for block in iter(lambda: f.read(1 << 20), b""):
      h.update(block)
  return h.hexdigest()


def _plain_file(path):
  return os.path.isfile(path) and not os.path.islink(path)


def packed_here(work_dir, tarball):
  """Is *tarball*, as it is now, one a build here packed?"""
  try:
    with open(_entry(work_dir, tarball)) as f:
      want = f.read().strip()
  except OSError:
    return False
  return _plain_file(tarball) and want == _sha256(tarball)


def quarantine(work_dir, tarball):
  """Move a refused *tarball* (and its .sha256) under QUARANTINE/, never over one
  kept before (a numbered folder then); return where."""
  dest = os.path.join(work_dir, QUARANTINE, os.path.dirname(os.path.relpath(tarball, work_dir)))
  base, n = dest, 1
  while os.path.lexists(os.path.join(dest, os.path.basename(tarball))):
    dest, n = "%s.%d" % (base, n), n + 1
  os.makedirs(dest, exist_ok=True)
  for path in (tarball, tarball + ".sha256"):
    if os.path.lexists(path):
      os.replace(path, os.path.join(dest, os.path.basename(path)))
  return dest
