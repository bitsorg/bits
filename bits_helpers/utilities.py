#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

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
from os.path import basename, exists, isdir, islink, join

# Internal
from bits_helpers.git import git
from bits_helpers.log import banner, debug, dieOnError
from bits_helpers.paths import resolveDefaultsFilename
from bits_helpers.recipe import getRecipeReader, parseRecipe




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

def incompatibleFlavorDefaults(validDefaults, defaults, defaults_meta=None):
  """Evaluate the valid_defaults gate for a chained-defaults build, ignoring
  structural/overlay layers.

  Packages declare ``valid_defaults`` to gate on build *flavors* (e.g. ``o2``,
  ``o2-epn``). Structural layers are not flavors and must be ignored: the
  always-present ``release`` base, and any default whose file declares
  ``valid_defaults_exempt: true`` (e.g. the ``alidist`` variant). Their names
  are collected by :func:`readDefaults` into ``defaults_meta['_valid_defaults_exempt']``.

  Returns ``(bad, missing)``:

  * ``bad``     – chosen flavor defaults the packages do not accept;
  * ``missing`` – ``True`` when the packages require a flavor
    (``validDefaults`` is non-empty) but only structural layers were selected.

  When *validDefaults* is falsy (no package restricts defaults, e.g. a plain
  LCG build) the build is always compatible and ``([], False)`` is returned.
  """
  if not validDefaults:
    return ([], False)
  exempt = set((defaults_meta or {}).get("_valid_defaults_exempt", ()))
  exempt.add("release")
  flavors = [d for d in defaults if d not in exempt]
  bad = [d for d in flavors if d not in validDefaults]
  return (bad, not flavors)




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
  valid_defaults_exempt = []   # structural/overlay defaults, in chain order

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
      # A structural/overlay default (e.g. the 'alidist' variant) is not a
      # build flavor: packages must not gate their valid_defaults on it. Read
      # and strip the marker before the merge so it does not leak into the
      # merged metadata (see incompatibleFlavorDefaults).
      if xMeta.pop("valid_defaults_exempt", False):
        valid_defaults_exempt.append(xdefaults)
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

  # The 'release' base is auto-injected into every chain and is never a build
  # flavor, so it is always structural (exempt from the valid_defaults gate).
  if "release" in defaults and "release" not in valid_defaults_exempt:
    valid_defaults_exempt.append("release")
  defaultsMeta["_valid_defaults_exempt"] = valid_defaults_exempt

  debug("Merged Defaults: %s ",json.dumps(defaultsMeta,indent = 4))

  return (defaultsMeta, defaultsBody)



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
        dieOnError(err, err)   # was dieOnError(err, None, None): 3 args + a None message
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
