# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Defaults profile handling: read and merge the ``defaults-*.sh`` chain, resolve
per-package family/override policy, and validate a package against the selected
defaults. Sits above the recipe layer (it reads defaults recipes) and imports the
list-coercion primitive from utilities."""

import fnmatch
import json
import os
import sys
from collections import OrderedDict
from os.path import exists

from bits_helpers.log import banner, debug, dieOnError
from bits_helpers.recipe import getRecipeReader, parseRecipe
from bits_helpers.paths import resolveDefaultsFilename
from bits_helpers.utilities import asList

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
