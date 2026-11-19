# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pins of pip packages (PythonPipRecipe): the file the first build installed, by
its sha256, under MANIFESTS/pip-pins/<arch>/<project>/<version>/<python>-<mode>.json
in bits-store. A later build of the same installs that file (BITS_PIP_PIN), never
one uploaded since. Written create-only, by a build writing through bits-store.
"""

import json
import os
import re

from bits_helpers.log import debug, info, warning

_SHA256 = re.compile(r"[0-9a-f]{64}")
_FILE = re.compile(r"[A-Za-z0-9._+!-]+\.(whl|tar\.gz|zip)")
_PYPI_NAME = re.compile(r"""^\s*(?:export\s+)?PYPI_NAME=["']?([A-Za-z0-9._-]+)["']?\s*(?:#.*)?$""", re.M)
SOURCE = ".pip-source.json"   # what PythonPipRecipe writes in INSTALLROOT


def key(spec, specs, arch):
  """The pin's key: the project as PEP 503 names it, Python's major.minor, the mode."""
  m = _PYPI_NAME.search(str(spec.get("recipe") or ""))
  project = re.sub(r"[-_.]+", "-", m.group(1) if m else spec["package"]).lower()
  python = ".".join(str((specs.get("Python") or {}).get("version") or "none").split(".")[:2])
  return "MANIFESTS/pip-pins/%s/%s/%s/%s-%s.json" % (
    arch, project, spec["version"], python, spec.get("pip_build") or "wheel")


def _pin(data):
  """{"file", "sha256"} from a pin's or .pip-source.json's bytes, else None."""
  try:
    d = json.loads(data)
    pin = {"file": d["file"], "sha256": d["sha256"]}
  except (TypeError, ValueError, KeyError):
    return None
  ok = isinstance(pin["file"], str) and _FILE.fullmatch(pin["file"]) and \
       isinstance(pin["sha256"], str) and _SHA256.fullmatch(pin["sha256"])
  return pin if ok else None


def read(sync, k):
  """The pin at *k* in a bits-store, or None (none, or no bits-store to ask)."""
  if not hasattr(sync, "get_key"):
    return None
  try:
    return _pin(sync.get_key(k))
  except Exception as exc:   # pylint: disable=broad-except  -- a pin is an aid, never a failure
    debug("pip pin %s: %s", k, exc)
    return None


def after_build(spec, sync, k, install_dir, built=True):
  """What pip installed (.pip-source.json) recorded on the spec (pip_source, for the
  build manifest); pinned when *built* here, unpinned, and writing through a bits-store."""
  try:
    with open(os.path.join(install_dir, SOURCE)) as f:
      text = f.read()
  except OSError:
    return
  got = _pin(text)
  if not got:
    return
  url = json.loads(text).get("url")
  if isinstance(url, str):   # without credentials (an index URL may carry them)
    got = dict(got, url=re.sub(r"^([A-Za-z][A-Za-z0-9+.-]*://)[^/?#]*@", r"\1", url))
  spec["pip_source"] = got
  if not built or spec.get("pip_pin") or not (getattr(sync, "writeStore", "") and hasattr(sync, "put_key")):
    return
  pin = {"file": got["file"], "sha256": got["sha256"]}
  data = json.dumps(pin, sort_keys=True).encode()
  try:
    if sync.put_key(k, data):
      info("%s: pinned %s (sha256 %s)", spec["package"], got["file"], got["sha256"][:12])
      return
    taken = read(sync, k)
    if taken and taken != pin:
      warning("%s: another build pinned %s first; builds from now on install that",
              spec["package"], taken["file"])
  except Exception as exc:   # pylint: disable=broad-except
    warning("%s: not pinned (%s)", spec["package"], exc)
