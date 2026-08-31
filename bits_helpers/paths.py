# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Config- and recipe-file path resolution: locate recipe/defaults ``.sh`` files
across the config dir and taps, and list the search paths. Split out of
utilities.py as a leaf (depends only on the logger), so the recipe and defaults
layers above it can share these without an import cycle."""

import os
from os.path import exists, join

from bits_helpers.log import dieOnError, error

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
    Used by repository-provider checkouts: a cloned provider under
    ``$BITS_WORK_DIR/REPOS/``, or a locally-shadowed provider under the
    config dir (see repo_provider._local_provider_dir).
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

def resolveFilename(taps, pkg, configDir, generatedPackages, ext=".sh", required_by=None):
  for d in getConfigPaths(configDir):
    if d in generatedPackages and pkg in generatedPackages[d]:
      meta = generatedPackages[d][pkg]
      return ("generate:{}@{}".format(pkg, meta["version"]), meta["pkgdir"])
    filename = checkForFilename(taps, pkg, d, ext=ext)
    if exists(filename):
      return (filename, d)
  # Name the recipe(s) that pulled this dependency in (with their origin), so the
  # operator sees WHO required a missing package, not just that it is missing.
  reqline = ""
  if required_by:
    reqline = "\nRequired by: " + ", ".join(sorted(required_by))
  dieOnError(True,
             "Package {pkg} not found on any loaded recipe path (searched "
             "BITS_PATH, primary config dir: {cfg}).{req}\n"
             "If {pkg} is provided by a repository that was not loaded, add "
             "`always_load: true` to that provider's recipe (alongside "
             "`provides_repository: true`) so it is cloned before resolution — or "
             "list it in BITS_PROVIDERS. A repository-provider is otherwise "
             "auto-loaded only when it appears as a dependency in the build graph, "
             "which a base recipe repository usually does not.".format(
               pkg=pkg, cfg=configDir, req=reqline))

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
