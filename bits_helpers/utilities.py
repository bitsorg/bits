#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

# Standard library
import hashlib
import os
import platform
import re
import sys
from datetime import datetime
from os.path import basename, isdir, islink, join

# Internal
from bits_helpers.git import git
from bits_helpers.log import dieOnError




def call_ignoring_oserrors(function, *args, **kwargs):
  try:
    return function(*args, **kwargs)
  except OSError:
    return None


def symlink(link_target, link_name):
  """Match the behaviour of `ln -nsf LINK_TARGET LINK_NAME`, without having to fork.

  Create a new symlink named LINK_NAME pointing to LINK_TARGET. If LINK_NAME
  is a directory, create a symlink named basename(LINK_TARGET) inside it.
  """
  # If link_name is a symlink pointing to a directory, isdir() will return True.
  if isdir(link_name) and not islink(link_name):
    link_name = join(link_name, basename(link_target))
  call_ignoring_oserrors(os.unlink, link_name)
  os.symlink(link_target, link_name)


asList = lambda x: x if isinstance(x, list) else [x]


def topological_sort(specs):
  """Topologically sort specs so that dependencies come before the packages that depend on them.

  This function returns a generator, yielding package names in order.

  The algorithm used here was adapted from:
  http://www.stoimen.com/blog/2012/10/01/computer-algorithms-topological-sort-of-a-graph/
  """
  edges = [(spec["package"], dep) for spec in specs.values() for dep in spec["requires"]]
  leaves = [spec["package"] for spec in specs.values() if not spec["requires"]]
  while leaves:
    current_package = leaves.pop(0)
    yield current_package
    # Find every package that depends on the current one.
    new_leaves = {pkg for pkg, dep in edges if dep == current_package}
    # Stop blocking packages that depend on the current one...
    edges = [(pkg, dep) for pkg, dep in edges if dep != current_package]
    # ...but keep blocking those that still depend on other stuff!
    leaves.extend(new_leaves - {pkg for pkg, _ in edges})
  # If we have any edges left, we have a cycle
  if edges:
    # Find a cycle by following dependencies
    cycle = []
    start = edges[0][0]  # Start with any remaining package
    current = start
    max_iter = 10000 # Prevent infinite loops
    while max_iter > 0:
      max_iter -= 1
      cycle.append(current)
      # Find what current depends on
      for pkg, dep in edges:
        if pkg == current:
          current = dep
          break
      if current in cycle:  # We found a cycle
        cycle = cycle[cycle.index(current):]  # Trim to just the cycle
        dieOnError(True, "Dependency cycle detected: " + " -> ".join(cycle + [cycle[0]]))
      if current == start:  # We've gone full circle
        raise RuntimeError("Internal error: cycle detection failed")
    assert False, "Unreachable error: cycle detection failed"




def pkg_to_shell_id(name: str) -> str:
  """Return a valid shell identifier derived from a package name.

  Replaces every character that is not alphanumeric or underscore with
  ``_``, then upper-cases the result.  This handles both the common
  dash-separated convention and less common names that contain dots or
  other punctuation::

      pkg_to_shell_id("GCC-Toolchain")  -> "GCC_TOOLCHAIN"
      pkg_to_shell_id("common.bits")    -> "COMMON_BITS"
      pkg_to_shell_id("o2.framework")   -> "O2_FRAMEWORK"

  The transformation is used wherever a package name must appear as part
  of a shell variable name, e.g. ``${COMMON_BITS_ROOT}``.  Filesystem
  paths (tarballs, install dirs, SPECS dirs) always use the original
  package name unchanged.
  """
  import re
  return re.sub(r'[^A-Za-z0-9_]', '_', name).upper()




def ver_rev(spec):
  """Return the version-revision directory segment for *spec*.

  Normally this is ``<version>-<revision>`` (e.g. ``8.5.0-1``).

  When a package has ``force_revision`` set via a ``defaults-*.sh``
  ``overrides:`` entry or a top-level ``force_revision:`` in the defaults
  file, the revision may be a fixed string *or* an empty string.  An empty
  string means the revision suffix is dropped entirely, yielding just
  ``<version>`` (e.g. ``CMSSW_13_0_0`` instead of ``CMSSW_13_0_0-1``).

  Every place in the codebase that previously wrote
  ``"{version}-{revision}".format(**spec)`` must call this helper instead so
  that the forced/dropped revision is honoured consistently across the install
  tree, tarballs, symlinks, init.sh, and dist trees.
  """
  rev = spec.get("revision", "")
  return "{}-{}".format(spec["version"], rev) if rev else spec["version"]


def human_bytes(n, units=("B", "KiB", "MiB", "GiB", "TiB"), sep=" "):
  """Bytes as a short human string. Default gives binary units with a space
  ('5.0 KiB', '0 B'); pass units=('B','K','M','G','T'), sep='' for the compact
  form ('1.8G', '0B')."""
  n = float(n or 0)
  for u in units:
    if n < 1024 or u == units[-1]:
      return ("%.0f%s%s" % (n, sep, u)) if u == "B" else ("%.1f%s%s" % (n, sep, u))
    n /= 1024.0


def resolve_store_path(architecture, spec_hash):
  """Return the path where a tarball with the given hash is to be stored.

  The returned path is relative to the working directory (normally sw/) or the
  root of the remote store.
  """
  return "/".join(("TARS", architecture, "store", spec_hash[:2], spec_hash))


def resolve_links_path(architecture, package):
  """Return the path where symlinks for the given package are to be stored.

  The returned path is relative to the working directory (normally sw/) or the
  root of the remote store.
  """
  return "/".join(("TARS", architecture, package))


def short_commit_hash(spec):
  """Shorten the spec's commit hash to make it more human-readable.

  The ``commit_hash`` property may hold a tag name rather than an actual git
  hash.  When the tag and the commit hash are the same, the value is returned
  as-is; otherwise only the first 10 characters (a typical git short-hash) are
  returned.
  """
  return (spec["commit_hash"]
          if spec["tag"] == spec["commit_hash"]
          else spec["commit_hash"][:10])


# Date fields available for tag/version substitution; zero-padded where needed.
# NOTE: captured once at module import time — they do not update during the run.
now = datetime.now()
nowKwds = {
  "year":  str(now.year),
  "month": str(now.month).zfill(2),
  "day":   str(now.day).zfill(2),
  "hour":  str(now.hour).zfill(2),
}

def resolve_spec_data(spec, data, defaults, branch_basename="", branch_stream="",
                      default_vars=None, strict=True):
  """Expand the data replacing the following keywords:

  - %(name)s      — package name (alias for %(package)s, preferred in source URLs)
  - %(package)s
  - %(commit_hash)s
  - %(short_hash)s
  - %(tag)s
  - %(branch_basename)s
  - %(branch_stream)s
  - %(tag_basename)s
  - %(defaults_upper)s
  - %(version)s
  - %(root_dir)s
  - %(year)s
  - %(month)s
  - %(day)s
  - %(hour)s

  with the calculated content.
  """
  defaults_upper = "" if defaults == ['release'] else "_".join(d.upper() for d in defaults)
  commit_hash = spec.get("commit_hash", "hash_unknown")
  tag = str(spec.get("tag", "tag_unknown"))
  package = spec.get("package")
  all_vars = {
    "name": package,       # short alias used in source URLs: %(name)s-%(version)s.tar.gz
    "package": package,
    "root_dir": "${%s_ROOT}" % pkg_to_shell_id(package),
    "commit_hash": commit_hash,
    "short_hash": commit_hash[0:10],
    "tag": tag,
    "branch_basename": branch_basename,
    "branch_stream": branch_stream or tag,
    "tag_basename": basename(tag),
    "defaults_upper": defaults_upper,
    "version": str(spec.get("version", "version_unknown")),
    "platform_machine": platform.machine(),
    "sys_platform": sys.platform,
    "os_name": os.name,
    **nowKwds,
  }
  # default_vars come from the active --defaults profile's `variables:` block and
  # are shared across recipes.  Apply them BEFORE the recipe's own `variables:`
  # so a recipe-local definition overrides a profile-wide one of the same name.
  for k, v in (default_vars or {}).items():
    all_vars[k] = v
  for k, v in spec.get("variables",{}).items():
    all_vars[k] = v

  if strict:
    # Opted-in expansion — version/tag/source/patches, or a recipe that sets
    # `variables:` / `expand_recipe: true`.  An unknown %(x)s is almost certainly
    # a typo, so it is fatal.  Uses %-formatting so the documented indirect form
    # `%%(%(v1)s_key)s` keeps working (%% collapses to % between passes).
    #   variables:
    #     v1: foo
    #     foo_key: bar
    #     final: %%(%(v1)s_key)s   # -> %(foo_key)s -> bar
    while re.search(r"\%\([a-zA-Z][a-zA-Z0-9_]*\)s", data):
      try:
        data = data % all_vars
      except KeyError as e:
        dieOnError(True,
          "Unknown variable %s referenced in recipe for '%s'.\n"
          "  Offending value: %r\n"
          "  Available variables: %s" % (
            e, package or "?", data, ", ".join(sorted(all_vars))))
        return data  # guard for mocked dieOnError in tests
    return data

  # Soft expansion — the recipe did NOT opt in and is only being expanded because
  # the defaults profile defines `variables:`.  Substitute only the variables we
  # actually know, leaving any other %(...)s — and bare `%` (shell parameter
  # expansion like ${v%suffix}, printf %d, etc.) — untouched, so incidental
  # occurrences in a recipe body are neither clobbered nor turned into a fatal
  # error.  Loops to support indirect %(%(v1)s_key)s nesting; the prev!=data
  # guard terminates once no further known substitutions are possible.
  _var_re = re.compile(r"\%\(([a-zA-Z][a-zA-Z0-9_]*)\)s")
  def _sub_known(m):
    return str(all_vars[m.group(1)]) if m.group(1) in all_vars else m.group(0)
  prev = None
  while prev != data and _var_re.search(data):
    prev = data
    data = _var_re.sub(_sub_known, data)
  return data

def resolve_version(spec, defaults, branch_basename, branch_stream):
    return resolve_spec_data(spec, spec["version"], defaults, branch_basename, branch_stream)

def resolve_tag(spec, default_vars=None):
  """Expand the tag, replacing the following keywords:
  - %(year)s
  - %(month)s
  - %(day)s
  - %(hour)s
  - any variable from the active defaults profile's `variables:` block, or the
    recipe's own `variables:`

  The variables blocks matter because `tag:` otherwise accepted a strictly
  smaller vocabulary than `version:` / `source:` / `patches:`, which go through
  resolve_spec_data. A profile defining `variables: {release: main}` and an
  override of `tag: "%(release)s"` therefore died with "Unknown variable" even
  though the very same expansion worked in every other field. Precedence
  mirrors resolve_spec_data: profile-wide first, then recipe-local.
  """
  all_vars = {**nowKwds, **spec,
              **(default_vars or {}),
              **(spec.get("variables") or {})}
  try:
    return spec["tag"] % all_vars
  except KeyError as e:
    dieOnError(True,
      "Unknown variable %s in tag field of recipe for '%s': %r\n"
      "  Available variables: %s" % (
        e, spec.get("package", "?"), spec.get("tag", ""),
        ", ".join(sorted(str(k) for k in all_vars))))
    return spec.get("tag", "")  # guard for mocked dieOnError in tests


def normalise_multiple_options(option, sep=","):
  return [x for x in ",".join(option).split(sep) if x]

def prunePaths(workDir):
  for x in ["PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"]:
    if x not in os.environ:
      continue
    workDirEscaped = re.escape("%s" % workDir) + "[^:]*:?"
    os.environ[x] = re.sub(workDirEscaped, "", os.environ[x])
  for x in list(os.environ.keys()):
    if x.endswith("_VERSION") and x != "BITS_VERSION":
      os.environ.pop(x)


# Use this to check if a given spec is compatible with the given default


# Cache of "<repository>@<commit>" identity labels keyed by recipe directory so
# the per-recipe origin trace does not shell out to git once per package.
_recipeSourceLabelCache = {}

def recipeSourceLabel(pkgdir, provider_dirs=None):
  """Return a short ``<repository>@<commit>`` identity for the directory a recipe
  was resolved from, for origin tracing in the build log.

  Provider checkouts already carry an authoritative ``(name, commit)`` pair in
  *provider_dirs*; for the primary config dir and other ``BITS_PATH`` entries the
  short git ``HEAD`` of the directory is used instead.  Results are cached per
  directory.  Never raises — degrades to the bare directory basename when the
  source is not a git checkout (e.g. a generated-package directory).
  """
  if pkgdir in _recipeSourceLabelCache:
    return _recipeSourceLabelCache[pkgdir]
  name = basename(pkgdir.rstrip("/")) or pkgdir
  commit = ""
  if provider_dirs and pkgdir in provider_dirs:
    prov_name, prov_hash = provider_dirs[pkgdir]
    name = prov_name or name
    commit = (prov_hash or "")[:10]
  if not commit:
    try:
      err, out = git(("rev-parse", "--short", "HEAD"), directory=pkgdir, check=False)
      if err == 0 and out.strip():
        commit = out.strip()
    except Exception:
      commit = ""
  label = "{}@{}".format(name, commit) if commit else name
  _recipeSourceLabelCache[pkgdir] = label
  return label



class Hasher:
  def __init__(self) -> None:
    # usedforsecurity=False suppresses the FIPS rejection of SHA-1 on
    # systems where SHA-1 is blocked for security use (Python ≥ 3.9 only).
    # Fall back gracefully on Python 3.8 and earlier where the parameter
    # does not exist.
    try:
      self.h = hashlib.sha1(usedforsecurity=False)
    except TypeError:
      self.h = hashlib.sha1()  # Python < 3.9
  def __call__(self, txt):
    if not isinstance(txt, bytes):
      txt = txt.encode('utf-8', 'ignore')
    self.h.update(txt)
  def hexdigest(self):
    return self.h.hexdigest()
  def copy(self):
    new_hasher = Hasher()
    new_hasher.h = self.h.copy()
    return new_hasher
