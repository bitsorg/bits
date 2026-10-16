# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""BUILD_EPOCH: the version of what bits itself puts into every build.

A package's hash covers its own inputs (recipe, sources, patches, dependencies),
not the bits code that runs the build. A change to that code that can alter
what a build produces (the build environment, the generated init.sh, what
relocation bakes in, the layout of INSTALLROOT) would otherwise leave every
hash unchanged, so packages built before and after it are silently mixed.
Bumping BUILD_EPOCH rebuilds every package once. At 0 it is not hashed, so
introducing it changed no hash.

tests/test_build_epoch.py checks the files in COVERED against COVERED_SHA256:
any change to them fails it until COVERED_SHA256 is updated, in the same
commit that decides whether BUILD_EPOCH is bumped as well. A bump also changes
the golden hashes in tests/test_build.py, which that commit updates.
"""

import hashlib
import os

BUILD_EPOCH = 0

# The bits files that shape what a build produces.
COVERED = (
  "bits_helpers/build_template.sh",
  "bits_helpers/initdotsh.py",
  "bits_helpers/relativize-configs.sh",
  "bits_helpers/relocate-me.sh",
  "bits_helpers/view-list.sh",
)
# Not covered, because it changes too often for a checksum: build.py, which
# fills in the build script (the template parameters and buildEnvironment) and
# the container settings. A change there that alters builds bumps BUILD_EPOCH
# by review, not by this check.

COVERED_SHA256 = "90af3c745eb6c4e2705ec63564eb1f1a4c0c7973147e416821a10f07a420ae34"


def covered_sha256(root=None):
  """sha256 over COVERED (each path and its content), from the bits checkout."""
  root = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
  h = hashlib.sha256()
  for rel in COVERED:
    with open(os.path.join(root, rel), "rb") as fh:
      h.update(rel.encode() + b"\0" + fh.read() + b"\0")
  return h.hexdigest()
