# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The container engine bits runs its containers with: docker (default) or podman.

--container-engine sets $BITS_CONTAINER_ENGINE (so scripts bits starts see it too);
rootless podman needs no root daemon, and a recipe leaving its container has only the
user's own privileges.
"""

import os

ENGINES = ("docker", "podman")
VARIABLE = "BITS_CONTAINER_ENGINE"


def engine():
  """The engine's command: $BITS_CONTAINER_ENGINE, else docker."""
  name = os.environ.get(VARIABLE, "").strip() or "docker"
  if name not in ENGINES:
    raise SystemExit("%s=%r: the container engine is one of %s" % (VARIABLE, name, ", ".join(ENGINES)))
  return name


def choose(name):
  """Use *name* (--container-engine) from now on, here and in what bits starts."""
  if name:
    os.environ[VARIABLE] = name
  return engine()
