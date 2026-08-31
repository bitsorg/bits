# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Architecture domain: platform detection, arch templates/tokens, and the
architecture-derived variables. Split out of utilities.py; pure logic plus the
distro/platform probes used by detectArch. No other bits_helpers module imports
from here except utilities (one-way), so this stays a leaf."""

import platform
import re

from bits_helpers.cmd import getoutput

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
