# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Site configuration: the defaults and assumptions an installation may change
(stores, container images, providers, ...).

Files named NN-<name>.yaml are read from, in this order: bits' own site.d, then
/etc/bits/site.d, ~/.config/bits/site.d ($XDG_CONFIG_HOME) and $BITS_SITE_DIR.
A file in a later directory replaces the one of the same name (an empty file
masks it); all are then merged in name order, the last one winning. Mappings
merge key by key; any other value replaces; null removes. The command line, the
environment and the community's defaults (system:) still come first.
"""

import os
import re
import sys
from os.path import abspath, dirname, expanduser, isfile, join

SHIPPED = join(dirname(abspath(__file__)), "site.d")
SYSTEM_DIR = "/etc/bits/site.d"
SEARCH = None          # the directories to read instead (tests)
_NAME = re.compile(r"^\d\d-[^/]+\.yaml$")
_loaded = None


def dirs():
  """The directories read, lowest priority first."""
  if SEARCH is not None:
    return list(SEARCH)
  if os.environ.get("BITS_SITE_ISOLATED", "").strip().lower() in ("1", "true", "yes", "on"):
    return [SHIPPED]          # bits' own files only (tests, reproducible runs)
  user = join(os.environ.get("XDG_CONFIG_HOME") or expanduser("~/.config"), "bits", "site.d")
  out = [SHIPPED, SYSTEM_DIR, user]
  if os.environ.get("BITS_SITE_DIR"):
    out.append(os.environ["BITS_SITE_DIR"])
  return out


def files():
  """The files read, in the order they are merged."""
  by_name = {}
  for d in dirs():
    try:
      names = os.listdir(d)
    except OSError:
      continue
    for n in names:
      if _NAME.match(n) and isfile(join(d, n)):
        by_name[n] = join(d, n)
  return [by_name[n] for n in sorted(by_name)]


def _merge(into, src, path, origin, origins):
  for key, value in src.items():
    name = path + str(key)
    if value is None:
      into.pop(key, None)
      for k in [k for k in origins if k == name or k.startswith(name + ".")]:
        del origins[k]
    elif isinstance(value, dict):
      if not isinstance(into.get(key), dict):
        into[key] = {}
      _merge(into[key], value, name + ".", origin, origins)
    else:
      into[key] = value
      origins[name] = origin


def load():
  """({merged values}, {dotted key: file it came from}); stops bits on a bad file."""
  import yaml
  data, origins = {}, {}
  for path in files():
    try:
      with open(path) as f:
        doc = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as exc:
      sys.exit("bits: site configuration %s: %s" % (path, exc))
    if doc is None:
      continue
    if not isinstance(doc, dict):
      sys.exit("bits: site configuration %s: not a mapping" % path)
    _merge(data, doc, "", path, origins)
  return data, origins


def _config():
  global _loaded
  if _loaded is None:
    _loaded = load()
  return _loaded


def reset():
  """Read the files again on the next lookup (tests)."""
  global _loaded
  _loaded = None


def get(key, default=None):
  """The value of a dotted key ("store.operations"), or *default*."""
  node = _config()[0]
  for part in key.split("."):
    if not isinstance(node, dict) or part not in node:
      return default
    node = node[part]
  return node


def origin(key):
  """The file a dotted key's value came from, or None."""
  return _config()[1].get(key)


def read_store(architecture, fallback=True):
  """The read store for *architecture* when none is given: store.read_by_arch
  (matched by distribution and machine), else store.read (unless not
  *fallback*), else ""."""
  from bits_helpers.arch import normalise_arch_key
  key = normalise_arch_key(architecture)
  for arch, url in (get("store.read_by_arch") or {}).items():
    if url and normalise_arch_key(str(arch)) == key:
      return str(url)
  return str(get("store.read") or "") if fallback else ""


def main(argv):
  """Print the values of dotted keys, one per line ("" when unset): for scripts."""
  for key in argv:
    value = get(key)
    print("" if value is None else value)


if __name__ == "__main__":
  main(sys.argv[1:])
