#!/usr/bin/env python3
# Standard library
import fnmatch
import hashlib
import json
import os
import platform
import re
import sys
from collections import OrderedDict
from datetime import datetime
from glob import glob
from os.path import basename, exists, isdir, islink, join
from shlex import quote
from typing import Any, IO

# Third-party
import yaml

# Internal
from bits_helpers.checksum_store import load_for_spec, merge_into_spec
from bits_helpers.cmd import getoutput
from bits_helpers.git import git
from bits_helpers.log import banner, debug, dieOnError, error, warning

from bits_helpers.cmd import getoutput
from bits_helpers.git import git

from bits_helpers.log import error, warning, dieOnError, debug, banner
from bits_helpers.checksum_store import load_for_spec, merge_into_spec

class SpecError(Exception):
  pass


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


SHARED_ARCH = "shared"
"""Sentinel value used in all paths for architecture-independent packages.

When a recipe sets ``architecture: shared``, bits substitutes this string for
the real build architecture in every path component (install dir, tarball name,
TARS store, SPECS dir, ``$PKGPATH``).  The result is that the package is
installed under ``sw/shared/<pkg>/<version>-<revision>/`` and its tarball is
stored under ``TARS/shared/store/…``, making it reusable by any architecture
without rebuilding.

Recipes that do **not** define ``architecture: shared`` are completely unaffected
— ``effective_arch()`` returns the real build architecture for them.
"""


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


# Mapping from bits architecture substrings to Docker --platform values.
# Matched by substring so that compound strings like "slc9_aarch64" or
# "ubuntu2204_x86-64" resolve correctly.
_BITS_ARCH_TO_DOCKER_PLATFORM = {
    "x86-64":  "linux/amd64",
    "x86_64":  "linux/amd64",
    "aarch64": "linux/arm64",
    "arm64":   "linux/arm64",
    "ppc64le": "linux/ppc64le",
    "s390x":   "linux/s390x",
    "riscv64": "linux/riscv64",
}


def docker_platform_for_arch(bits_arch: str):
    """Return the Docker ``--platform`` value for a bits architecture string.

    Examples::

        docker_platform_for_arch("slc9_aarch64")  -> "linux/arm64"
        docker_platform_for_arch("slc9_x86-64")   -> "linux/amd64"
        docker_platform_for_arch("osx_arm64")      -> "linux/arm64"
        docker_platform_for_arch("unknown")        -> None

    Returns ``None`` when the architecture substring is not recognised, which
    lets callers decide whether to fall back to the Docker daemon default.
    """
    for key, plat in _BITS_ARCH_TO_DOCKER_PLATFORM.items():
        if key in bits_arch:
            return plat
    return None


def effective_arch(spec: dict, build_arch: str) -> str:
  """Return the architecture string to use in paths and tarball names.

  If the recipe declares ``architecture: shared`` the function returns
  :data:`SHARED_ARCH` (``"shared"``), so that the package is installed in a
  location that every build platform can read.

  For all other recipes (including those that omit the field entirely) the
  function returns *build_arch* unchanged, preserving full backward
  compatibility.
  """
  if spec.get("architecture") == SHARED_ARCH:
    return SHARED_ARCH
  return build_arch


def compute_combined_arch(defaults_meta: dict, defaults_list: list, raw_arch: str) -> str:
  """Return the effective architecture string for install paths.

  **Per-default ``append_arch`` (new mechanism)**

  When one or more loaded defaults files set ``append_arch: <value>``, only
  those explicit values are appended to *raw_arch*, regardless of the
  ``qualify_arch`` flag::

      # defaults-gcc13.sh has append_arch: -gcc13
      # defaults-release.sh has no append_arch
      # result for --default release::gcc13:
      compute_combined_arch({"_append_arch_qualifiers": ["-gcc13"]},
                            ["release", "gcc13"], "slc7_x86-64")
      # → "slc7_x86-64-gcc13"

  This lets recipe authors opt individual defaults files into architecture
  qualification while keeping the others transparent.  The values from
  ``append_arch`` are appended **verbatim**, in the same order as the defaults
  chain (``--default a::b::c``); no separator is assumed.  Each value must
  carry its own separator if one is wanted (and may also be glued on with
  none)::

      append_arch: -gcc15-dbg # -> "<raw>-gcc15-dbg"
      append_arch: _gcc15     # -> "<raw>_gcc15"
      append_arch: dbg        # -> "<raw>dbg"      (no separator, glued on)

  **Legacy ``qualify_arch`` (backward-compatible fallback)**

  When no defaults file uses ``append_arch``, the old behaviour applies: if
  any loaded defaults file sets ``qualify_arch: true``, the install directory
  is qualified with every non-``release`` default name joined by ``-``::

      <raw_arch>-<d1>-<d2>-...

  When ``qualify_arch`` is absent or false and no ``append_arch`` values were
  collected, *raw_arch* is returned unchanged.

  Examples::

      compute_combined_arch({}, ["release"], "slc7_x86-64")
      # → "slc7_x86-64"  (neither mechanism active)

      compute_combined_arch({"qualify_arch": True}, ["dev", "gcc13"], "slc7_x86-64")
      # → "slc7_x86-64-dev-gcc13"  (legacy qualify_arch)

      compute_combined_arch({"qualify_arch": True}, ["release"], "slc7_x86-64")
      # → "slc7_x86-64"  (legacy, release-only → no suffix)

      compute_combined_arch({"_append_arch_qualifiers": ["-gcc13"]},
                            ["release", "gcc13"], "slc7_x86-64")
      # → "slc7_x86-64-gcc13"  (per-default append_arch, separator in value)
  """
  # --- New mechanism: per-default append_arch values -------------------------
  per_default = defaults_meta.get("_append_arch_qualifiers")
  if per_default:
    # Append each value verbatim: the separator (if any) lives in the value, so
    # callers can join with "-", "_", or nothing at all.
    return raw_arch + "".join(q for q in per_default if q)

  # --- Legacy mechanism: global qualify_arch flag ----------------------------
  if not defaults_meta.get("qualify_arch", False):
    return raw_arch
  qualifiers = [d for d in defaults_list if d != "release"]
  if not qualifiers:
    return raw_arch
  return raw_arch + "-" + "-".join(qualifiers)


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

def resolve_tag(spec):
  """Expand the tag, replacing the following keywords:
  - %(year)s
  - %(month)s
  - %(day)s
  - %(hour)s
  """
  try:
    return spec["tag"] % {**nowKwds, **spec}
  except KeyError as e:
    dieOnError(True,
      "Unknown variable %s in tag field of recipe for '%s': %r" % (
        e, spec.get("package", "?"), spec.get("tag", "")))
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

def validateSpec(spec):
  if not spec:
    raise SpecError("Empty recipe.")
  if type(spec) != OrderedDict:
    raise SpecError("Not a YAML key / value.")
  if "package" not in spec:
    raise SpecError("Missing package field in header.")

# Use this to check if a given spec is compatible with the given default
def validateDefaults(finalPkgSpec, defaults):
  if "valid_defaults" not in finalPkgSpec:
    return (True, "", [])
  validDefaults = asList(finalPkgSpec["valid_defaults"])
  nonStringDefaults = [x for x in validDefaults if not isinstance(x, str)]
  if nonStringDefaults:
    return (False, "valid_defaults needs to be a string or a list of strings. Found %s." % nonStringDefaults, [])
  defaultsList = asList(defaults)
  invalidDefaults = [d for d in defaultsList if d not in validDefaults]
  if not invalidDefaults:
    return (True, "", validDefaults)
  return (False, "Cannot compile %s with `%s' default. Valid defaults are\n%s" %
                  (finalPkgSpec["package"],
                   ", ".join(invalidDefaults),
                   "\n".join([" - " + x for x in validDefaults])), validDefaults)


# Built-in architecture layout, used when no `architecture:` template is set in
# the defaults.  Expressed with the same %(...)s substitution syntax bits uses
# elsewhere (sources, tags).  Available keys: see arch_components().
DEFAULT_ARCH_TEMPLATE = "%(os)s_%(machine)s"


def arch_components(hasOsRelease, osReleaseLines, platformTuple, platformSystem, platformProcessor):
  """Return the substitution dict from which the architecture string is built.

  Keys:
    os        -- distro+version token, e.g. "ubuntu2510" (or "osx")
    machine   -- bits-canonical dashed CPU form, e.g. "x86-64" (or "arm64")
    _machine  -- uname/underscore CPU form, e.g. "x86_64"

  doDetectArch() assembles the default layout via DEFAULT_ARCH_TEMPLATE; a
  defaults file may instead supply its own `architecture:` template referencing
  these keys (e.g. "%(os)s_%(_machine)s" for ubuntu2510_x86_64, or
  "%(_machine)s-%(os)s" for x86_64-ubuntu2510).
  """
  if platformSystem == "Darwin":
    processor = platformProcessor
    if not processor:
      processor = "x86-64" if platform.machine() == "x86_64" else "arm64"
    os_token = "osx"
  else:
    distribution, version, flavour = platformTuple
    distribution = distribution.lower()
    # If platform.dist does not return something sensible,
    # let's try with /etc/os-release
    if distribution not in ["ubuntu", "red hat enterprise linux", "redhat", "centos", "almalinux", "rocky linux"] and hasOsRelease:
      for x in osReleaseLines:
        key, is_prop, val = x.partition("=")
        if not is_prop:
          continue
        val = val.strip("\n \"")
        if key == "ID":
          distribution = val.lower()
        if key == "VERSION_ID":
          version = val

    if distribution == "ubuntu":
      major, _, minor = version.partition(".")
      version = major + minor
    elif distribution == "debian":
      # http://askubuntu.com/questions/445487/which-ubuntu-version-is-equivalent-to-debian-squeeze
      debian_ubuntu = {"7": "1204", "8": "1404", "9": "1604", "10": "1804", "11": "2004"}
      if version in debian_ubuntu:
        distribution = "ubuntu"
        version = debian_ubuntu[version]
    elif distribution in ["redhat", "red hat enterprise linux", "centos", "almalinux", "rocky linux"]:
      distribution = "slc"

    processor = platformProcessor
    if not processor:
      # Sometimes platform.processor returns an empty string
      processor = getoutput(("uname", "-m")).strip()

    os_token = "{distro}{version}".format(distro=distribution, version=version.split(".")[0])

  return {
    "os": os_token,
    "machine": processor.replace("_", "-"),
    "_machine": processor.replace("-", "_"),
  }


def apply_arch_template(template, components):
  """Render an architecture *template* (``%(os)s``/``%(machine)s``/...) against
  *components*.  A literal string with no placeholders is returned unchanged."""
  try:
    return template % components
  except (KeyError, ValueError) as exc:
    raise ValueError("invalid architecture template %r: %s" % (template, exc))


def doDetectArch(hasOsRelease, osReleaseLines, platformTuple, platformSystem, platformProcessor):
  return apply_arch_template(
    DEFAULT_ARCH_TEMPLATE,
    arch_components(hasOsRelease, osReleaseLines, platformTuple, platformSystem, platformProcessor))


# ── architecture token matching (order- and separator-independent) ──────────
# Used so that custom layouts (ubuntu2510_x86_64, x86_64-ubuntu2510, ...) are
# recognised without --force-unknown-architecture, and so docker-image / S3
# lookups match by content rather than by string position.
_ARCH_DISTRO_RE = re.compile(
  r"(slc[0-9]+|ubuntu[0-9]*|ubt[0-9]*|osx|fedora[0-9]*|alma(?:linux)?[0-9]*"
  r"|centos[0-9]*|rocky[0-9]*|rhel[0-9]*|el[0-9]+|debian[0-9]*)")
_ARCH_MACHINE_RE = re.compile(r"(x86[-_]64|aarch64|arm64|ppc64le|ppc64)")


def arch_distro_token(architecture):
  """Return the distro token (e.g. 'ubuntu2510') found anywhere in *architecture*."""
  m = _ARCH_DISTRO_RE.search(architecture or "")
  return m.group(0) if m else None


def arch_machine_token(architecture):
  """Return the CPU token (e.g. 'x86-64'/'x86_64') found anywhere in *architecture*."""
  m = _ARCH_MACHINE_RE.search(architecture or "")
  return m.group(0) if m else None


def normalise_arch_key(architecture):
  """(distro, dashed-machine) key for order/separator-independent comparison."""
  mac = arch_machine_token(architecture)
  return (arch_distro_token(architecture), mac.replace("_", "-") if mac else None)

# Try to guess a good platform. This does not try to cover all the
# possibly compatible linux distributions, but tries to get right the
# common one, obvious one. If you use a Unknownbuntu which is compatible
# with Ubuntu 15.10 you will still have to give an explicit platform
# string.
#
# FIXME: we should have a fallback for lsb_release, since platform.dist
# is going away.
def detectArch():
  try:
    with open("/etc/os-release") as osr:
      osReleaseLines = osr.readlines()
    hasOsRelease = True
  except OSError:
    osReleaseLines = []
    hasOsRelease = False
  try:
    if platform.system() == "Darwin":
      if platform.machine() == "x86_64":
        return "osx_x86-64"
      else:
        return "osx_arm64"
  except Exception:
    pass
  try:
    import distro
    platformTuple = distro.linux_distribution()
    platformSystem = platform.system()
    platformProcessor = platform.processor()
    if not platformProcessor or " " in platformProcessor:
      platformProcessor = platform.machine()
    return doDetectArch(hasOsRelease, osReleaseLines, platformTuple, platformSystem, platformProcessor)
  except Exception:
    return doDetectArch(hasOsRelease, osReleaseLines, ["unknown", "", ""], "", "")


def detectArchComponents():
  """Like detectArch(), but returns the {os, machine, _machine} substitution
  dict (see arch_components) so a defaults `architecture:` template can be
  rendered against the locally detected platform."""
  try:
    with open("/etc/os-release") as osr:
      osReleaseLines = osr.readlines()
    hasOsRelease = True
  except OSError:
    osReleaseLines = []
    hasOsRelease = False
  if platform.system() == "Darwin":
    machine = "x86-64" if platform.machine() == "x86_64" else platform.machine()
    return {"os": "osx", "machine": machine.replace("_", "-"), "_machine": machine.replace("-", "_")}
  try:
    import distro
    platformProcessor = platform.processor()
    if not platformProcessor or " " in platformProcessor:
      platformProcessor = platform.machine()
    return arch_components(hasOsRelease, osReleaseLines, distro.linux_distribution(),
                           platform.system(), platformProcessor)
  except Exception:
    return arch_components(hasOsRelease, osReleaseLines, ["unknown", "", ""], "", "")

def _parse_req_matcher(r):
  """Split a requirement string into ``(name, matcher, version_pin)`` triple.

  Supported syntaxes::

      name                          plain dependency
      name:matcher                  architecture/defaults-conditional dependency
      name = version                dependency with explicit version pin
      name = version:matcher        version pin + arch/defaults condition

  *matcher* is an architecture regex or ``defaults=<regex>``, exactly as for
  the two-field form.  *version_pin* is ``None`` when no ``= version`` clause
  is present.

  The ``=`` must appear **before** the ``:`` (if any) so that version strings
  containing ``:`` are not ambiguous with matchers.  In practice version
  strings do not contain ``:``, so this is not a real constraint.
  """
  # Locate = and : positions.  Only treat = as a version separator when it
  # appears before the first : (or when there is no :).
  eq_pos = r.find("=")
  colon_pos = r.find(":")
  if eq_pos != -1 and (colon_pos == -1 or eq_pos < colon_pos):
    name = r[:eq_pos].strip()
    rest = r[eq_pos + 1:].strip()
    if ":" in rest:
      pin, matcher = rest.split(":", 1)
      return name, matcher, pin.strip()
    return name, ".*", rest
  if ":" in r:
    name, matcher = r.split(":", 1)
    return name, matcher, None
  return r, ".*", None


def _defaults_active(matcher, defaults):
  """Return True if a ``defaults=<regex>`` *matcher* matches the active defaults.

  ``defaults`` is what bits threads through from ``args.defaults``, which is a
  *list* of profile names (``--defaults dev4::cuda`` -> ``["dev4", "cuda"]``);
  older callers/tests may pass a bare string.  The conditional is active when the
  regex matches ANY active profile, so a recipe can require a dependency only
  under a given profile, e.g. ``- "cuda:defaults=cuda"`` (enabled by
  defaults-cuda.sh).  Matching per-element also makes this safe: the previous
  code passed the whole list to ``re.match`` and would raise TypeError.
  """
  rx = matcher[len("defaults="):]
  defs = defaults if isinstance(defaults, (list, tuple)) else [defaults]
  return any(re.match(rx, d) for d in defs)


# A variable-reference matcher is spelled "(?NAME)" -- an identifier in the same
# parenthesised form as a regex group, but one that is NOT a legal regex (e.g.
# "(?cuda)" raises re.error: "unknown extension ?c").  This lets a recipe gate a
# dependency on a defaults *variable* rather than on the architecture string:
#   - "cuda:(?cuda)"      # require cuda only when variable `cuda` is truthy
# It is deliberately distinct from arch regexes such as "(?!osx)" (a valid
# negative-lookahead, kept as an arch match) -- we only treat "(?NAME)" as a
# variable reference when it fails to compile as a regex, so real regexes
# (including inline-flag groups like "(?i)") are never misinterpreted.
_VAR_MATCHER_RE = re.compile(r"\(\?([A-Za-z_][A-Za-z0-9_]*)\)\Z")


def _var_matcher_name(matcher):
  """Return the variable NAME if *matcher* is a "(?NAME)" variable reference,
  else None (in which case it is an arch regex / defaults= matcher)."""
  m = _VAR_MATCHER_RE.match(matcher or "")
  if not m:
    return None
  try:
    re.compile(matcher)
  except re.error:
    return m.group(1)   # not a valid regex -> it's a variable reference
  return None            # valid regex (e.g. "(?i)") -> treat as arch match


def _var_truthy(default_vars, name):
  """True when defaults variable *name* is defined and not a false-ish string."""
  v = (default_vars or {}).get(name)
  return v is not None and str(v).strip().lower() not in ("", "0", "false", "off", "no")


def _loose_version_key(v):
  """A natural-order sort key for version strings, à la ``sort -V``.

  Splits the string into runs of digits and non-digits; digit runs compare
  numerically (so v40r2 < v40r10) and non-digit runs lexicographically. Each
  element is a (type, value) tuple so int and str runs never compare directly.
  Handles the schemes bits sees: v40r2, v01-19-06, 01.07, 1.2.3, 0.1.0pre17.

  Separator characters ``-``, ``.`` and ``_`` are treated as equivalent and do
  not themselves contribute to the ordering, so dash- and dot-form tags compare
  equal (``v6-40-00`` == ``v6.40.00``). Without this, the raw separator runs
  sort lexicographically ('-' 0x2d < '.' 0x2e), which made ``v6-40-00`` rank
  below ``v6.36.99`` and silently broke ``version>=`` gating for ROOT-style
  dash tags.
  """
  key = []
  for p in re.findall(r"\d+|\D+", str(v)):
    if p.isdigit():
      key.append((0, int(p)))
    else:
      s = re.sub(r"[-._]+", "", p)   # drop separators; keep alpha (v, r, pre…)
      if s:
        key.append((1, s))
  return key


def _version_compare(a, b):
  """Return -1/0/1 comparing version strings *a* and *b* in natural order."""
  ka, kb = _loose_version_key(a), _loose_version_key(b)
  return (ka > kb) - (ka < kb)


# version<op><value>: e.g. "version=v40r2", "version<v40r4", "version>=v40r2".
_VERSION_OP_RE = re.compile(r"version\s*(>=|<=|==|!=|=|>|<)\s*(.+)\Z", re.DOTALL)
_VERSION_OPS = {
    "=":  lambda c: c == 0, "==": lambda c: c == 0, "!=": lambda c: c != 0,
    "<":  lambda c: c < 0,  "<=": lambda c: c <= 0,
    ">":  lambda c: c > 0,  ">=": lambda c: c >= 0,
}


def _matcher_atom_active(matcher, arch, defaults, default_vars=None, version=None):
  """Evaluate a single (non-compound) matcher atom. See _matcher_active."""
  if matcher.startswith("defaults="):
    return _defaults_active(matcher, defaults)
  vm = _VERSION_OP_RE.match(matcher)
  if vm:
    return version is not None and _VERSION_OPS[vm.group(1)](_version_compare(version, vm.group(2).strip()))
  var = _var_matcher_name(matcher)
  if var is not None:
    return _var_truthy(default_vars, var)
  return bool(re.match(matcher, arch))


def _matcher_active(matcher, arch, defaults, default_vars=None, version=None):
  """Whether a *matcher* is active for the current build.

  Atoms:
    * ``defaults=<regex>``       -> active when the regex matches an active profile;
    * ``version<op><value>``     -> active when the package version satisfies the
                                    comparison (op is one of = == != < <= > >=),
                                    e.g. ``foo.patch:version=v40r2`` or
                                    ``foo.patch:version<v40r4``. Versions compare
                                    in natural order (sort -V semantics);
    * ``(?VAR)``                 -> active when defaults variable VAR is truthy;
    * anything else              -> a regex matched against the architecture string.

  Atoms may be combined with ``&&`` (all) and ``||`` (any); ``||`` has the lower
  precedence, e.g. ``(?!osx) && version>=v40r2 || (?cuda)`` is
  ``((?!osx) AND version>=v40r2) OR (?cuda)``. (Note: a single ``|`` inside an
  arch regex is still ordinary alternation — only the doubled ``||`` combines.)

  *version* is the resolved package version (after overrides / pins); it is only
  consulted by the ``version`` kind and may be ``None`` for callers that never
  use it (e.g. requires filtering).
  """
  matcher = matcher.strip()
  if "||" in matcher:
    parts = [p for p in (s.strip() for s in matcher.split("||")) if p]
    return any(_matcher_active(p, arch, defaults, default_vars, version) for p in parts)
  if "&&" in matcher:
    parts = [p for p in (s.strip() for s in matcher.split("&&")) if p]
    return all(_matcher_active(p, arch, defaults, default_vars, version) for p in parts)
  return _matcher_atom_active(matcher, arch, defaults, default_vars, version)


def predefined_arch_vars(architecture):
  """Predefined, architecture-derived boolean variables (truthy ones only).

  These let a recipe or a defaults ``variables:`` gate test the platform with
  the same ``(?NAME)`` spelling used for flavours, e.g. a package requirement
  ``pkg:(?osx)`` or a variable gated ``when: "(?openloops) && (?!osx)"``. Only
  the *true* members are returned (an unset variable is already falsy via
  :func:`_var_truthy`, so ``(?osx)`` is correctly false off macOS). On
  ``osx_arm64`` this is ``{'osx': 'true', 'arm64': 'true', 'aarch64': 'true'}``.
  """
  a = str(architecture or "")
  is_osx = a.startswith("osx")
  is_arm = ("arm64" in a) or ("aarch64" in a)
  is_x86 = ("x86-64" in a) or ("x86_64" in a)
  cand = {"osx": is_osx, "linux": not is_osx,
          "arm64": is_arm, "aarch64": is_arm, "x86_64": is_x86}
  return {k: "true" for k, v in cand.items() if v}


def resolve_variables(variables, flavours, architecture, defaults):
  """Resolve a defaults ``variables:`` block into a flat ``{name: value}`` dict.

  Entries may be plain (``name: value`` -- always defined) or *gated*
  (``name: {value: V, when: MATCHER}`` -- defined to ``V`` only when ``MATCHER``
  is active for this build). ``MATCHER`` uses the requires-matcher grammar
  (``(?flavour)``, an architecture regex such as ``osx`` / ``(?!osx)``,
  ``defaults=<regex>``, combined with ``&&`` / ``||``) and is evaluated against
  the variables resolved *so far*, so a gate may reference CLI flavours, the
  predefined architecture variables, and any earlier entry ("a previously
  defined variable"). A gated entry with no explicit ``value`` defaults to
  ``True`` when active.

  Precedence (low -> high): predefined arch vars < CLI flavours < defaults-file
  entries, except that a CLI flavour always wins over a defaults entry of the
  same name (an explicit override) while remaining visible to every gate.
  """
  flavours = flavours or {}
  resolved = OrderedDict()
  resolved.update(predefined_arch_vars(architecture))
  resolved.update(flavours)                        # visible to the gates below
  for name, entry in (variables or {}).items():
    if name in flavours:
      continue                                     # CLI flavour overrides defaults
    if isinstance(entry, dict) and "when" in entry:
      if _matcher_active(str(entry["when"]), architecture, defaults, resolved):
        resolved[name] = entry.get("value", True)
      # inactive -> leave undefined (falsy)
    else:
      resolved[name] = entry
  return resolved


def filterByArchitectureDefaults(arch, defaults, requires, default_vars=None, version=None):
  """Yield requirements from *requires* that are satisfied by *arch*/*defaults*.

  *version* is the depending package's own resolved version; pass it so a
  requirement can be gated on it, e.g. ``- "curl:version>=v6.40.00"``.
  """
  for r in requires:
    require, matcher, _pin = _parse_req_matcher(r)
    if _matcher_active(matcher, arch, defaults, default_vars, version):
      yield require

def disabledByArchitectureDefaults(arch, defaults, requires, default_vars=None, version=None):
  """Yield requirements from *requires* that are *not* satisfied by *arch*/*defaults*."""
  for r in requires:
    require, matcher, _pin = _parse_req_matcher(r)
    if not _matcher_active(matcher, arch, defaults, default_vars, version):
      yield require


def _parse_patch_entry(entry):
  """Split a ``patches:`` entry into ``(name, matcher_or_None, checksum_suffix)``.

  Entry form: ``name[:matcher][,algo:digest]``. The optional inline checksum
  (which itself contains ``:``) is separated first on the first ``,``; a ``:``
  in the remaining head then introduces a conditional matcher, e.g.
  ``foo.patch:version<v40r4`` or ``foo.patch:(?cuda),sha256:abc``.
  """
  head, sep, tail = entry.partition(",")
  checksum = (sep + tail) if sep else ""
  name, csep, matcher = head.partition(":")
  return name.strip(), (matcher.strip() if csep else None), checksum


def filterPatches(patches, arch, defaults, default_vars, version):
  """Return the ``patches:`` entries active for this build, with any ``:matcher``
  stripped so downstream (checksum lookup, copy to $SOURCEDIR, ``patch``) sees a
  plain ``name[,algo:digest]``. Entries without a matcher are always kept."""
  out = []
  for entry in patches or []:
    name, matcher, checksum = _parse_patch_entry(entry)
    if matcher is None or _matcher_active(matcher, arch, defaults, default_vars, version=version):
      out.append(name + checksum)
  return out


def _collect_version_pins(arch, defaults, raw_requires, owner, version_pins, specs,
                          default_vars=None, version=None):
  """Extract version pins from *raw_requires* and merge into *version_pins*.

  Called while processing *owner*'s spec (before the requires list has been
  reduced to plain names).  Any ``name = version`` clause that is active for
  the current *arch*/*defaults* pair is registered in *version_pins*.

  Raises a :exc:`SystemExit` (via :func:`dieOnError`) when:

  * Two different packages pin the same dependency to **different** versions.
  * A version pin is declared for a dependency that was already resolved with
    a different version (i.e. ``name in specs`` with a conflicting version).
    This happens when the pinned package appeared in the build queue before the
    package that declares the pin, making the pin arrive too late.
  """
  for r in raw_requires:
    name, matcher, pin = _parse_req_matcher(r)
    if pin is None:
      continue
    # Check whether this entry is active for the current architecture/defaults.
    if not _matcher_active(matcher, arch, defaults, default_vars, version):
      continue
    if name in version_pins:
      if version_pins[name] != pin:
        dieOnError(True,
          "Conflicting version pin for '%s': '%s' (from an earlier spec) vs "
          "'%s' (from '%s'). Only one version pin per dependency is allowed."
          % (name, version_pins[name], pin, owner))
      # Same pin value from multiple packages — harmless, nothing to do.
      continue
    if name in specs:
      actual = specs[name].get("version", "")
      if actual != pin:
        dieOnError(True,
          "Version pin '%s = %s' declared by '%s' cannot be applied: '%s' was "
          "already resolved with version '%s'. Move the pinning package earlier "
          "in the build list, or remove the conflicting pin."
          % (name, pin, owner, name, actual))
      # Already resolved with the same version — no action needed.
      continue
    version_pins[name] = pin
    debug("Version pin registered: %s = %s  (from %s)", name, pin, owner)

def merge_dicts(dict1, dict2, skip_keys=None) -> OrderedDict:
    """
    Merge two ordered dictionaries where dict2's keys updates dict1's keys recursively.
    
    Args:
        dict1: First dictionary (base)
        dict2: Second dictionary (updates)
        skip_keys: Set of keys to skip during merge (won't be updated from dict2)
    
    Returns:
        OrderedDict with merged values
    """
    if dict2 is None:
      return dict1.copy()
    if skip_keys is None:
        skip_keys = set()
    
    # Add all keys from dict1 first
    merged = dict1.copy()
    
    # Overwrite with dict2's values and add new keys
    for key, value in dict2.items():
        # Skip keys that are in the skip list
        if key in skip_keys:
            continue
            
        if key not in merged:
            # Add new key from dict2
            merged[key] = value
        elif isinstance(merged[key], dict) and isinstance(value, dict):
            # Recursively merge nested ordered dictionaries
            merged[key] = merge_dicts(merged[key], value, skip_keys)
        elif isinstance(merged[key], list) and isinstance(value, list):
            # Merge lists, such as for "disabled"
            merged[key].extend(value)
        else:
            # Overwrite existing key
            merged[key] = value
    
    return merged

def resolve_pkg_family(defaults_meta: dict, package_name: str) -> str:
  """Return the package family for *package_name* from the defaults metadata.

  The ``package_family`` key in a defaults recipe is a mapping of the form::

      package_family:
        default: cms          # fallback when no pattern matches
        lcg:
          - ROOT
          - SCRAMV1
        cms:
          - data-*
          - coral

  Pattern matching uses :func:`fnmatch.fnmatch` (case-sensitive, ``*`` and
  ``?`` wildcards supported).  Families are tried in definition order; the
  first match wins.  If no pattern matches, the ``default`` family is
  returned.  If ``package_family`` is absent entirely, an empty string is
  returned so that the install path collapses to the legacy layout
  ``<arch>/<pkg>/<version>-<revision>``.

  **Defaults packages** (``defaults-*``) are always excluded from family
  assignment regardless of the ``package_family`` configuration, including the
  ``default:`` fallback.  These pseudo-packages carry configuration rather than
  installed software; assigning them to a family would corrupt their install
  path and break the ``init.sh`` sourcing chain for every downstream package.
  """
  # Defaults packages are special pseudo-packages and must never receive a
  # family.  The default: fallback in package_family would otherwise silently
  # pull them in, causing their SPECS/ and install paths to include a family
  # directory that nothing expects.
  if package_name.startswith("defaults-"):
    return ""
  family_cfg = defaults_meta.get("package_family")
  if not family_cfg or not isinstance(family_cfg, dict):
    return ""
  default_family = family_cfg.get("default", "")
  for family, patterns in family_cfg.items():
    if family == "default":
      continue
    if not isinstance(patterns, list):
      continue
    for pat in patterns:
      if fnmatch.fnmatch(package_name, str(pat)):
        return family
  return default_family


def readDefaults(configDir, defaults, error, architecture):
  defaultsMeta = {}
  defaultsBody = ""
  append_arch_qualifiers = []  # per-default append_arch values, in chain order

  for xdefaults in defaults:
    xDefaults = resolveDefaultsFilename(xdefaults, configDir, failOnError=False)
    xMeta = {}
    if xDefaults is not None and exists(xDefaults):
      err, xMeta, xBody = parseRecipe(getRecipeReader(xDefaults))
      if xBody.strip() != "":
        defaultsBody += "\n" + xBody.strip()
      if err:
        error(err)
        sys.exit(1)
      # Collect append_arch value before merging (merge_dicts would flatten it
      # into a single scalar and we need the ordered per-default list).
      if "append_arch" in xMeta:
        append_arch_qualifiers.append(xMeta["append_arch"])
      # Normalise this profile's overrides to dict-form *before* the chain merge
      # so that defaults chained as a::b::c deep-merge: the union of all entries,
      # last profile wins on a per-package key. Without this, merge_dicts sees a
      # list-form block ("- pkg = ver") and a dict-form block ("pkg:\n  ...") as
      # incompatible types and the later one REPLACES the earlier wholesale,
      # silently dropping the other profile's pins. asDict turns both shapes into
      # an OrderedDict, which merge_dicts then merges recursively (key-by-key,
      # last wins).
      if "overrides" in xMeta:
        xMeta["overrides"] = asDict(xMeta["overrides"])
      defaultsMeta = merge_dicts(defaultsMeta, xMeta)

  # Store the collected per-default qualifiers so compute_combined_arch can
  # use them instead of appending every default name to the architecture.
  if append_arch_qualifiers:
    defaultsMeta["_append_arch_qualifiers"] = append_arch_qualifiers

  debug("Merged Defaults: %s ",json.dumps(defaultsMeta,indent = 4))

  return (defaultsMeta, defaultsBody)

def getRecipeReader(url: str, dist=None, genPackages={}):
  m = re.search(r'^(dist|generate):(.*)@([^@]+)$', url)
  if m and m.group(1) == "generate":
    pkg, version = m.group(2), m.group(3)
    # search across all generated dirs
    if pkg in genPackages and genPackages[pkg]["version"] == version:
      return GeneratedPackage(genPackages[pkg])
    raise ValueError(f"Generated package {pkg}@{version} not found")
  elif m and dist:
    return GitReader(url, dist)
  else:
    return FileReader(url)

# Generate a recipe of package
class GeneratedPackage:
  def __init__(self, obj) -> None:
    self.command = obj["command"]
    self.url = obj["url"]
  def __call__(self):
    return  getoutput(self.command).strip()

# Read a recipe from a file
class FileReader:
  def __init__(self, url) -> None:
    self.url = url
  def __call__(self):
    with open(self.url) as f:
      return f.read()
      
# Read a recipe from a git repository using git show.
class GitReader:
  def __init__(self, url, configDir) -> None:
    self.url, self.configDir = url, configDir
  def __call__(self):
    m = re.search(r'^dist:(.*)@([^@]+)$', self.url)
    fn, gh = m.groups()
    err, d = git(("show", f"{gh}:{fn.lower()}.sh"),
                 directory=self.configDir)
    if err:
      raise RuntimeError("Cannot read recipe {fn} from reference {gh}.\n"
                         "Make sure you run first (this will not alter your recipes):\n"
                         "  cd {dist} && git remote update -p && git fetch --tags"
                         .format(dist=self.configDir, gh=gh, fn=fn))
    return d

def yamlLoad(s):
  class YamlSafeOrderedLoader(yaml.SafeLoader):
    """YAML Loader with `!include` constructor."""
    
    def __init__(self, stream: IO) -> None:
      """Initialise Loader."""
      try:
        self._root = os.path.split(stream.name)[0]
      except AttributeError:
        self._root = os.path.curdir
      super().__init__(stream)

  def construct_include(loader: YamlSafeOrderedLoader, node: yaml.Node) -> Any:
    """Include file referenced at node."""
    filename = os.path.abspath(os.path.join(loader._root, loader.construct_scalar(node)))
    extension = os.path.splitext(filename)[1].lstrip('.')
    try:
      with open(filename) as f:
        if extension in ('yaml', 'yml'):
          try:
            return yaml.load(f, YamlSafeOrderedLoader)
          except (yaml.scanner.ScannerError, yaml.parser.ParserError) as e:
            raise yaml.constructor.ConstructorError(
              None, None,
              "!include: failed to parse YAML file %r: %s" % (filename, e),
              node.start_mark)
        elif extension in ('json', ):
          try:
            return json.load(f)
          except ValueError as e:
            raise yaml.constructor.ConstructorError(
              None, None,
              "!include: failed to parse JSON file %r: %s" % (filename, e),
              node.start_mark)
        else:
          return ''.join(f.readlines())
    except OSError as e:
      raise yaml.constructor.ConstructorError(
        None, None,
        "!include: cannot open file %r: %s" % (filename, e),
        node.start_mark)

  def construct_mapping(loader, node):
    loader.flatten_mapping(node)
    return OrderedDict(loader.construct_pairs(node))

  YamlSafeOrderedLoader.add_constructor('!include', construct_include)
  YamlSafeOrderedLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                                        construct_mapping)
  return yaml.load(s, YamlSafeOrderedLoader)

def yamlDump(s):
  class YamlOrderedDumper(yaml.SafeDumper):
    pass
  def represent_ordereddict(dumper, data):
    rep = []
    for k,v in data.items():
      k = dumper.represent_data(k)
      v = dumper.represent_data(v)
      rep.append((k, v))
    return yaml.nodes.MappingNode('tag:yaml.org,2002:map', rep)
  YamlOrderedDumper.add_representer(OrderedDict, represent_ordereddict)
  return yaml.dump(s, Dumper=YamlOrderedDumper)

def parseRecipe(reader, generatePackages=None, visited=None):
  assert(reader.__call__)
  err, spec, recipe = (None, None, None)
  try:
    d = reader()
    header,recipe = d.split("---", 1)
    # YAML forbids '%' as the first character of a plain (unquoted) scalar because
    # it is reserved for directives (e.g. %YAML, %TAG).  Recipe authors may want
    # to write  "- %(name)s-%(version)s.patch"  in patches: (and similar lists)
    # for the same variable substitution that sources: already supports.  Auto-
    # quoting those list items here lets them write the bare %(…)s form without
    # needing to remember YAML quoting rules.
    header = re.sub(
      r'^(\s*-\s+)(%[^\n\'"#\[\{].*)$',
      lambda m: m.group(1) + '"' + m.group(2).replace('\\', '\\\\').replace('"', '\\"') + '"',
      header,
      flags=re.MULTILINE,
    )
    spec = yamlLoad(header)
    if spec and "from" in spec:
      basename = os.path.basename(getattr(reader, "url", "") or "")
      filename = basename[:-3] if basename.endswith(".sh") else basename
      repoDir = os.environ.get("BITS_REPO_DIR")
      if visited is None:
        visited = []
      if spec["from"] in visited:
        raise RuntimeError(f" Cyclic Dependency: {' -> '.join(list(visited) + [spec['from']])}")
      visited.append(spec["from"])
      parent_dir = os.path.join(repoDir, spec["from"])
      base_filename, pkgdir = resolveFilename({}, filename, parent_dir, generatePackages)
      base_reader = getRecipeReader(base_filename, repoDir, generatePackages[parent_dir])
      err, base_spec, base_recipe = parseRecipe(base_reader, generatePackages, visited)
      spec, recipe_append = handleMergePolicy(spec, base_spec)
      recipe = recipe + base_recipe if recipe_append else recipe
    validateSpec(spec)
  except RuntimeError as e:
    err = str(e)
  except OSError as e:
    err = str(e)
  except SpecError as e:
    err = "Malformed header for {}\n{}".format(reader.url, str(e))
  except yaml.YAMLError as e:
    err = "Unable to parse {}\n{}".format(reader.url, str(e))
  except ValueError:
    err = "Unable to parse %s. Header missing." % reader.url
  except Exception as e:
    err = "Unknown Exception in parseRecipe {}.\n{}".format(reader.url, e)
  return err, spec, recipe


def asDict(overrides_array):
    """
    Collapse an array of override dictionaries into a single OrderedDict.
    
    Args:
        overrides_array: A list containing dictionaries and/or lists of dictionaries
                        to be merged, with later elements taking precedence.
    Returns:
        OrderedDict: A single merged OrderedDict
    """
    debug("asDict: %s ",json.dumps(overrides_array,indent = 4))

    if not overrides_array:
        return OrderedDict()
     
    if isinstance(overrides_array, OrderedDict):
        return overrides_array
      
    # Start with an empty OrderedDict
    result = OrderedDict()

    def _string_override(s):
        """Support the "name = value" version-pin shorthand in overrides:, the
        same syntax used for requires: pins. Returns {name: {version, tag}} so
        that tarball URLs (%(version)s) and git checkouts (tag) both use the
        pinned value, or None when the string is not a "name = value" pin."""
        name, sep, value = s.partition("=")
        name, value = name.strip(), value.strip()
        if not (sep and name and value):
            return None
        return OrderedDict([(name, OrderedDict([("version", value), ("tag", value)]))])

    for item in overrides_array:
        if isinstance(item, str):
            # e.g. "acts = 44.4.0" — previously silently ignored, which made a
            # list-of-strings overrides: block a no-op.
            d = _string_override(item)
            if d is not None:
                result = merge_dicts(result, d)
        elif isinstance(item, list):
            # Handle nested lists - recursively process each element
            for subitem in item:
                if isinstance(subitem, dict):
                    result = merge_dicts(result, subitem)
                elif isinstance(subitem, str):
                    d = _string_override(subitem)
                    if d is not None:
                        result = merge_dicts(result, d)
        elif isinstance(item, dict):
            result = merge_dicts(result, item)

    debug("asDict (result): %s ",json.dumps(result))
    return result

# (Almost pure part of the defaults parsing)
# Override defaultsGetter for unit tests.
def parseDefaults(disable, defaultsGetter, log, architecture=None, configDir=None):
  defaultsMeta, defaultsBody = defaultsGetter()
  if architecture and configDir:
    archDefaults = resolveDefaultsFilename(architecture, configDir, failOnError=False)
    if archDefaults is not None and os.path.exists(archDefaults):
      defaultsArchMeta = {}
      err, defaultsArchMeta, archBody = parseRecipe(getRecipeReader(archDefaults, configDir))
      if err:
        dieOnError (err, None, None)
      banner("Using defaults-%s file found in %s", architecture, configDir)
      debug("Architecture-specific defaults mentioned in: %s ", archDefaults)
      defaultsMeta = merge_dicts(defaultsMeta, defaultsArchMeta, skip_keys={"package"})

  # Defaults are actually special packages. They can override metadata
  # of any other package and they can disable other packages. For
  # example they could decide to switch from ROOT 5 to ROOT 6 and they
  # could disable alien for O2. For this reason we need to parse their
  # metadata early and extract the override and disable data.

  defaultsDisable = asList(defaultsMeta.get("disable", []))
   
  for x in defaultsDisable:
    log("Package %s has been disabled by current default.", x)
  disable.extend(defaultsDisable)

  defaultsMeta["overrides"] = asDict(defaultsMeta.get("overrides", OrderedDict()))

  if type(defaultsMeta.get("overrides", OrderedDict())) != OrderedDict:
    return ("overrides should be a dictionary", None, None, {})

  overrides, taps = OrderedDict(), {}
  commonEnv = {"env": defaultsMeta["env"]} if "env" in defaultsMeta else {}
  overrides["defaults-release"] = commonEnv
  for k, v in defaultsMeta.get("overrides", {}).items():
    f = k.split("@", 1)[0].lower()
    if "@" in k:
      taps[f] = "dist:"+k
    overrides[f] = dict(**(v or {}))
  return (None, overrides, taps, defaultsMeta)

def checkForFilename(taps, pkg, d, ext=".sh"):
  filename = taps.get(pkg, "{}/{}{}".format(d, pkg, ext))
  if not exists(filename):
    if "/" in pkg:
      filename = taps.get(pkg, "{}/{}".format(d, pkg))
    else:
      filename = taps.get(pkg, "{}/{}/latest".format(d, pkg))
  return filename

def resolveLocalPath(configDir, s):
  """
  Resolves a local path if it is a file://filename.
  If the path is not a file://filename, it returns the string `s` as is.
  Args:
    configDir: The configuration directory.
    s: The path to resolve.
  Returns:
    The resolved path.
  """
  if s.startswith("file://"):
    return f"file:/" + os.path.abspath(resolveFilename({}, s.removeprefix("file://"), configDir, {}, ext="")[0])
  else:
    return s

def getConfigPaths(configDir):
  """Return the ordered list of directories to search for recipe files.

  Each entry in the ``BITS_PATH`` environment variable is interpreted as:

  * An **absolute path** – used directly (no ``.bits`` suffix appended).
    This is used by repository-provider checkouts, which are stored at
    absolute paths under ``$BITS_WORK_DIR/REPOS/``.
  * A **relative name** – resolved as ``<configDir>/<name>.bits`` (the
    original behaviour for named recipe repositories).
  """
  configPath = os.environ.get("BITS_PATH")
  pkgDirs = [configDir]
  if configPath:
    for r in [x for x in configPath.split(",") if x]:
      if os.path.isabs(r):
        d = r          # provider checkout – absolute path used directly
      else:
        d = join(configDir, "%s.bits" % r)
      if exists(d):
        pkgDirs.append(d)
  return pkgDirs

def resolveFilename(taps, pkg, configDir, generatedPackages, ext=".sh"):
  for d in getConfigPaths(configDir):
    if d in generatedPackages and pkg in generatedPackages[d]:
      meta = generatedPackages[d][pkg]
      return ("generate:{}@{}".format(pkg, meta["version"]), meta["pkgdir"])
    filename = checkForFilename(taps, pkg, d, ext=ext)
    if exists(filename):
      return (filename, d)
  dieOnError(True, "Package {} not found in {}".format(pkg, configDir))

def resolveDefaultsFilename(defaults, configDir, failOnError=True):
  """Return the path of ``defaults-<defaults>.sh`` searched across all config paths.

  Uses :func:`getConfigPaths` to build the search list so that BITS_PATH
  provider checkouts are honoured consistently with :func:`resolveFilename`.
  """
  filename = None
  for d in getConfigPaths(configDir):
    candidate = "{}/defaults-{}.sh".format(d, defaults)
    if exists(candidate):
      return candidate
    filename = candidate  # keep last candidate for the error message

  if failOnError:
    error("Default `%s' does not exist.\n" % (defaults or "<no defaults specified>"))

def getPackageList(packages, specs, configDir, preferSystem, noSystem,
                   architecture, disable, defaults, performPreferCheck, performRequirementCheck,
                   performValidateDefaults, overrides, taps, log, force_rebuild=(),
                   provider_dirs=None, defaults_meta=None):
  """Resolve the full set of packages required by *packages*.

  *provider_dirs* is an optional ``dict`` returned by
  ``repo_provider.fetch_repo_providers_iteratively``, mapping each provider
  checkout directory to a ``(package_name, commit_hash)`` tuple.  When a
  recipe is found inside one of these directories the corresponding spec
  gains two extra keys:

  ``spec["recipe_provider"]``
      The name of the provider package whose checkout contains this recipe.

  ``spec["recipe_provider_hash"]``
      The git commit hash of that provider checkout.  ``storeHashes`` folds
      this value into the package's content-addressable build hash so that
      upgrading a provider triggers a rebuild of all packages sourced from it.
  """
  systemPackages = set()
  ownPackages = set()
  failedRequirements = set()
  testCache = {}
  requirementsCache = {}
  trackingEnvCache = {}
  packages = packages[:]
  generatedPackages = getGeneratedPackages(configDir)
  validDefaults = []  # empty list: all OK; None: no valid default; non-empty list: list of valid ones
  if provider_dirs is None:
    provider_dirs = {}
  _disable_set = set(disable)
  # version_pins accumulates ``name -> version`` entries declared via the
  # ``name = version`` syntax in any spec's requires / build_requires lists.
  # Pins are applied to the dependency spec just before it is stored in
  # *specs*, overriding the version stated in the recipe and any defaults-file
  # override.  Conflicts (two different pins for the same name, or a pin that
  # arrives after the dependency was already resolved) are fatal errors.
  _version_pins = {}
  while packages:
    p = packages.pop(0)
    if p in specs:
      continue
    # A package already known to be disabled (prefer_system or system_requirement
    # passed on a prior iteration) should not be re-processed.  Without this
    # guard the package is re-evaluated once per occurrence in the queue —
    # i.e. once per dependent — and disable.append() fires each time, producing
    # hundreds of duplicate --disable=GCC-Toolchain entries in the argument log.
    if p in _disable_set:
      continue
    skip = False
    for d in defaults:
      if p == "defaults-release" and ("defaults-" + d) in specs:
        skip = True
        break
      else:
        pkg_filename = ("defaults-" + d) if p == "defaults-release" else p.lower()
    if skip:
      continue

    # We rewrite all defaults to "defaults-release", so load the correct
    # defaults package here.
    # The reason for this rewriting is (I assume) so that packages that are
    # not overridden by some defaults can be shared with other defaults, since
    # they will end up with the same hash. The defaults must be called
    # "defaults-release" for this to work, since the defaults are a dependency
    # and all dependencies' names go into a package's hash.
    filename,pkgdir = resolveFilename(taps, pkg_filename, configDir, generatedPackages)

    dieOnError(not filename, "Package {} not found in {}".format(p, configDir))
    assert(filename is not None)

    err, spec, recipe = parseRecipe(getRecipeReader(filename, configDir, generatedPackages[pkgdir]), generatedPackages)
    dieOnError(err, err)
    # Unless there was an error, both spec and recipe should be valid.
    # otherwise the error should have been caught above.
    assert(spec is not None)
    assert(recipe is not None)
    dieOnError(spec["package"].lower() != pkg_filename,
               "{}.sh has different package field: {}".format(p, spec["package"]))
    spec["pkgdir"] = pkgdir

    # Load the optional external checksum store (checksums/<pkg>.checksum)
    # and merge source/patch checksums + commit pin into the spec.
    merge_into_spec(spec, load_for_spec(spec))

    # Track which repository provider supplied this recipe so that
    # storeHashes can fold the provider's commit hash into the build hash.
    if pkgdir in provider_dirs:
      prov_name, prov_hash = provider_dirs[pkgdir]
      spec["recipe_provider"] = prov_name
      spec["recipe_provider_hash"] = prov_hash
      log("Recipe for '%s' comes from provider '%s' @ %s",
          p, prov_name, prov_hash[:10])

    if p == "defaults-release":
      # Re-rewrite the defaults' name to "defaults-release". Everything auto-
      # depends on "defaults-release", so we need something with that name.
      spec["package"] = "defaults-release"

      # Never run the defaults' recipe, to match previous behaviour.
      # Warn if a non-trivial recipe is found (i.e., one with any non-comment lines).
      for line in map(str.strip, recipe.splitlines()):
        if line and not line.startswith("#"):
          warning("%s.sh contains a recipe, which will be ignored", pkg_filename)
      recipe = ""

      # Strip top-level ``requires`` / ``build_requires`` from the defaults
      # spec before the dependency-following step below.  These fields are
      # consumed earlier, in the Phase 2 provider scan (before getPackageList
      # is called), to seed ``fetch_repo_providers_iteratively``.  If they
      # were left here, every package listed in defaults ``requires`` would
      # auto-receive a ``defaults-release`` build dependency (line 1037), which
      # creates an unresolvable cycle:
      #
      #   defaults-release → provider-pkg → defaults-release
      #
      # Clearing them here is safe: the provider repos they reference are
      # already loaded and their recipes are on BITS_PATH.
      spec.pop("requires", None)
      spec.pop("build_requires", None)

    dieOnError(spec["package"] != p,
               "{} should be spelt {}.".format(p, spec["package"]))

    # If an override fully matches a package, we apply it. This means
    # you can have multiple overrides being applied for a given package.
    # An override key may carry an optional ":matcher" suffix (same syntax as
    # requires/patches: arch regex, defaults=, version<op>, (?VAR), &&/||) to
    # gate it, e.g. "ROOT:osx" applies only on macOS architectures. Package
    # names never contain ":", so splitting on the first ":" is unambiguous.
    _ovr_vars = (defaults_meta or {}).get("variables")
    for override in overrides:
      # We downcase the regex in parseDefaults(), so downcase the package name
      # as well. FIXME: This is probably a bad idea; we should use
      # re.IGNORECASE instead or just match case-sensitively.
      pkg_re, sep, matcher = override.partition(":")
      if not re.fullmatch(pkg_re, p.lower()):
        continue
      if sep and not _matcher_active(matcher, architecture, defaults, _ovr_vars,
                                     spec.get("version")):
        continue
      log("Overrides for package %s: %s", spec["package"], overrides[override])
      spec.update(overrides.get(override, {}) or {})

    # Apply global force_revision from the top-level defaults field as a
    # fallback.  Per-package overrides (set via spec.update() above) take
    # precedence because they ran first.  A value of "" means "drop the
    # revision suffix entirely"; None means "not set, do not apply".
    if "force_revision" not in spec \
            and defaults_meta is not None \
            and "force_revision" in defaults_meta:
      raw = defaults_meta.get("force_revision")
      if raw is not None:
        spec["force_revision"] = "" if raw == "" else str(raw)

    # If --always-prefer-system is passed or if prefer_system is set to true
    # inside the recipe, use the script specified in the prefer_system_check
    # stanza to see if we can use the system version of the package.
    systemRE = spec.get("prefer_system", "(?!.*)")
    try:
      systemREMatches = re.match(systemRE, architecture)
    except TypeError:
      dieOnError(True, "Malformed entry prefer_system: {} in {}".format(systemRE, spec["package"]))

    noSystemList = []
    if noSystem == "*":
      noSystemList = [spec["package"]]
    elif noSystem is not None:
      noSystemList = noSystem.split(",")
    systemExcluded = (spec["package"] in noSystemList)
    allowSystemPackageUpload = spec.get("allow_system_package_upload", False)
    # Fill the track env with the actual result from executing the script.
    for env, trackingCode in spec.get("track_env", {}).items():
      key = spec["package"] + env
      if key not in trackingEnvCache:
        status, out = performPreferCheck(spec, trackingCode)
        dieOnError(status, f"Error while executing track_env for {key}: {trackingCode} => {out}")
        trackingEnvCache[key] = out
      spec["track_env"][env] = trackingEnvCache[key]

    if (not systemExcluded or allowSystemPackageUpload) and  (preferSystem or systemREMatches):
      requested_version = resolve_version(spec, defaults, "unavailable", "unavailable")
      cmd = "REQUESTED_VERSION={version}\n{check}".format(
        version=quote(requested_version),
        check=spec.get("prefer_system_check", "false"),
      ).strip()
      if spec["package"] not in testCache:
        testCache[spec["package"]] = performPreferCheck(spec, cmd)
      err, output = testCache[spec["package"]]
      if err:
        # prefer_system_check errored; this means we must build the package ourselves.
        ownPackages.add(spec["package"])
      else:
        # prefer_system_check succeeded; this means we should use the system package.
        match = re.search(r"^bits_system_replace:(?P<key>.*)$", output, re.MULTILINE)
        if not match and systemExcluded:
          # No replacement spec name given. Fall back to old system package
          # behaviour and just disable the package.
          ownPackages.add(spec["package"])
        elif not match and not systemExcluded:
          # No replacement spec name given. Fall back to old system package
          # behaviour and just disable the package.
          systemPackages.add(spec["package"])
          if spec["package"] not in _disable_set:
            disable.append(spec["package"])
            _disable_set.add(spec["package"])
        elif match:
          # The check printed the name of a replacement; use it.
          key = match.group("key").strip()
          replacement = None
          for replacement_matcher in spec["prefer_system_replacement_specs"]:
            if re.match(replacement_matcher, key):
              replacement = spec["prefer_system_replacement_specs"][replacement_matcher]
              break
          if replacement:
            # We must keep the package name the same, since it is used to
            # specify dependencies.
            replacement["package"] = spec["package"]
            # The version is required for all specs. What we put there will
            # influence the package's hash, so allow the user to override it.
            replacement.setdefault("version", requested_version)
            # Carry over structural keys set on the original spec earlier in
            # getPackageList that build.py needs and that are NOT recomputed for
            # the replacement. pkgdir (the recipe directory, used for PKGDIR) is
            # mandatory — without it doBuild raises KeyError: 'pkgdir' when it
            # builds the replacement (e.g. a HomebrewRecipe shim).
            for _carry in ("pkgdir", "recipe_provider", "recipe_provider_hash",
                           "force_revision"):
              if _carry in spec and _carry not in replacement:
                replacement[_carry] = spec[_carry]
            spec = replacement
            # Allows generalising the version based on the actual key provided
            spec["version"] = spec["version"].replace("%(key)s", key)
            # We need the key to inject the version into the replacement recipe later.
            spec["key"] = key 
            recipe = replacement.get("recipe", "")
            # If there's an explicitly-specified recipe, we're still building
            # the package. If not, Bits will still "build" it, but it's
            # basically instantaneous, so report to the user that we're taking
            # it from the system.
            if recipe:
              ownPackages.add(spec["package"])
            else:
              systemPackages.add(spec["package"])
          else:
            warning(f"Could not find named replacement spec for {spec['package']}: {key}, "
                    "falling back to building the package ourselves.")

    dieOnError(("system_requirement" in spec) and recipe.strip("\n\t "),
               "System requirements %s cannot have a recipe" % spec["package"])
    if re.match(spec.get("system_requirement", "(?!.*)"), architecture):
      cmd = spec.get("system_requirement_check", "false")
      if spec["package"] not in requirementsCache:
        requirementsCache[spec["package"]] = performRequirementCheck(spec, cmd.strip())

      err, output = requirementsCache[spec["package"]]
      if err:
        failedRequirements.update([spec["package"]])
        spec["version"] = "failed"
      else:
        if spec["package"] not in _disable_set:
          disable.append(spec["package"])
          _disable_set.add(spec["package"])

    spec["disabled"] = list(disable)
    if spec["package"] in disable:
      continue

    # Check whether the package is compatible with the specified defaults
    if validDefaults is not None:
      (ok,msg,valid) = performValidateDefaults(spec)
      if valid:
        validDefaults = [ v for v in validDefaults if v in valid ] if validDefaults else valid[:]
        if not validDefaults:
          validDefaults = None  # no valid default works for all current packages

    # Collect version pins declared by this spec's requires / build_requires
    # *before* the lists are reduced to plain package names by the filter step
    # below.  We pass the raw YAML lists so that _collect_version_pins can see
    # the full "name = version[:matcher]" strings.
    # Variables declared in the active --defaults profile(s) (`variables:` block)
    # gate "(?VAR)" conditional requires, e.g. "- cuda:(?cuda)".
    _default_vars = (defaults_meta or {}).get("variables")
    # The depending package's own version, so a requirement can be gated on it
    # via "name:version>=X" (matched in sort -V order). Use the recipe/defaults
    # value resolved so far (dependent-declared pins are applied later and do
    # not affect a package's own requires gating).
    _own_version = spec.get("version")
    _collect_version_pins(
      architecture, defaults,
      list(spec.get("requires", [])) + list(spec.get("build_requires", [])),
      spec["package"], _version_pins, specs,
      default_vars=_default_vars, version=_own_version,
    )

    # For the moment we treat build_requires just as requires.
    fn = lambda what: disabledByArchitectureDefaults(architecture, defaults, spec.get(what, []), _default_vars, _own_version)
    spec["disabled"] += [x for x in fn("requires")]
    spec["disabled"] += [x for x in fn("build_requires")]
    fn = lambda what: filterByArchitectureDefaults(architecture, defaults, spec.get(what, []), _default_vars, _own_version)
    spec["requires"] = [x for x in fn("requires") if x not in disable]
    spec["build_requires"] = [x for x in fn("build_requires") if x not in disable]
    if spec["package"] != "defaults-release":
      spec["build_requires"].append("defaults-release")
    spec["runtime_requires"] = spec["requires"]
    spec["requires"] = spec["runtime_requires"] + spec["build_requires"]
    # Check that version is a string
    dieOnError(not isinstance(spec["version"], str),
               "In recipe \"%s\": version must be a string" % p)
    spec["tag"] = spec.get("tag", spec["version"])
    # Apply any version pin registered for this package.  The pin is set by a
    # dependent that declared "- depname = version" in its requires list.  We
    # apply it here — after recipe defaults and defaults-*.sh overrides — so
    # that the pin takes the highest precedence.  Both "version" and "tag" are
    # updated so that tarball URLs (%(version)s) and git checkouts (tag) both
    # see the pinned value.
    if spec["package"] in _version_pins:
      _pin = _version_pins[spec["package"]]
      debug("Applying version pin to %s: %s -> %s", spec["package"],
            spec.get("version"), _pin)
      spec["version"] = _pin
      spec["tag"] = _pin
    spec["version"] = spec["version"].replace("/", "_")
    # Resolve version-/arch-/defaults-conditional patches now that the version is
    # final (after overrides + pins). filterPatches drops inactive entries and
    # strips the :matcher, so the hash, checkout copy, $PATCHn env and patch
    # application all see the same plain name[,checksum] list.
    if "patches" in spec:
      spec["patches"] = filterPatches(spec.get("patches"), architecture, defaults,
                                      _default_vars, spec["version"])
    spec["recipe"] = recipe.strip("\n")
    if spec["package"] in force_rebuild:
      spec["force_rebuild"] = True
    # Resolve optional package family (e.g. "cms", "lcg") from defaults metadata.
    # Falls back to "" when no package_family mapping is configured, preserving
    # the legacy install layout <arch>/<pkg>/<version>-<revision>.
    spec["pkg_family"] = resolve_pkg_family(defaults_meta or {}, spec["package"])
    specs[spec["package"]] = spec
    packages += spec["requires"]
  return (systemPackages, ownPackages, failedRequirements, validDefaults)

def getGeneratedPackages(configDir):
  all_pkgs = {}
  pkgDirs = getConfigPaths(configDir)
  for pkgdir in pkgDirs:
    dir_pkgs = {}
    for vp in [x.split(os.sep)[-2] for x in glob(join(pkgdir, "*", "packages.py"))]:
      packages_py = join(pkgdir, vp, "packages.py")
      sys.path.insert(0, join(pkgdir, vp))
      try:
        pkg = __import__("packages")
      except (ImportError, SyntaxError) as e:
        sys.path.pop(0)
        dieOnError(True, "Failed to import generated-packages script %r: %s" % (packages_py, e))
        continue
      try:
        pkg.getPackages(dir_pkgs, pkgdir)
      except Exception as e:
        dieOnError(True, "Error running getPackages() in %r: %s" % (packages_py, e))
      sys.modules.pop("packages")
      sys.path.pop(0)
    all_pkgs[pkgdir] = dir_pkgs
  return all_pkgs


def _coerce_to_list(val):
  """Return *val* as a list.

  If *val* is a comma-separated string (spaces stripped), split it.
  If it is already a list, return it unchanged.
  """
  if isinstance(val, str):
    return val.replace(" ", "").split(",")
  return val

def handleMergePolicy(override_spec, final_base):
  mergePolicy = override_spec.get("merge_policy", {})
  remove_keys  = _coerce_to_list(mergePolicy.get("remove", []))
  force_inherit = _coerce_to_list(mergePolicy.get("inherit", []))
  merge_keys   = _coerce_to_list(mergePolicy.get("merge", []))
  recipe_append = "recipe" not in remove_keys
  for k in remove_keys:
    if k in final_base:
      final_base.pop(k, None)
  for key in force_inherit:
    if key in final_base:
      override_spec[key] = final_base[key]
  override_spec.pop("merge_policy", None)
  override_spec.pop("from", None)
  for key in merge_keys:
    if key not in override_spec:
      raise ValueError(f"Merge key {key} not found in override spec")
    if key not in final_base:
      final_base[key] = override_spec[key]
    else:
      if isinstance(final_base[key], OrderedDict) and isinstance(
        override_spec[key], OrderedDict
      ):
        merged = final_base[key].copy()
        merged.update(override_spec[key])
        final_base[key] = merged
      elif isinstance(final_base[key], list) and isinstance(
        override_spec[key], list
      ):
        for x in override_spec[key]:
          if x not in final_base[key]:
            final_base[key].append(x)
      else:
        raise ValueError(
          f"Merge key not allowed for {key} as it's of type {type(final_base.get(key, 'unknown'))}"
        )
    override_spec.pop(key)
  for k, v in override_spec.items():
    final_base[k] = override_spec[k]
  return final_base, recipe_append

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
