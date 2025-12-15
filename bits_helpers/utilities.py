#!/usr/bin/env python3
import os
import yaml
import json
from typing import Any, IO


from os.path import exists
import hashlib
from glob import glob
from os.path import basename, join, isdir, islink
import sys
import os
import re
import platform

from datetime import datetime
from collections import OrderedDict
from shlex import quote

from bits_helpers.cmd import getoutput
from bits_helpers.git import git

from bits_helpers.log import error, warning, dieOnError, debug

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


asList = lambda x : x if type(x) == list else [x]


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

  This is complicated by the fact that the commit_hash property is not
  necessarily a commit hash, but might be a tag name. If it is a tag name,
  return it as-is, else assume it is actually a commit hash and shorten it.
  """
  if spec["tag"] == spec["commit_hash"]:
    return spec["commit_hash"]
  return spec["commit_hash"][:10]


# Date fields to substitute: they are zero-padded
now = datetime.now()
nowKwds = { "year": str(now.year),
            "month": str(now.month).zfill(2),
            "day": str(now.day).zfill(2),
            "hour": str(now.hour).zfill(2) }

def resolve_spec_data(spec, data, defaults, branch_basename="", branch_stream=""):
  """Expand the data replacing the following keywords:

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
    "package": package,
    "root_dir": "${%s_ROOT}" % package.upper().replace("-","_"),
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
  for k, v in spec.get("variables",{}).items():
    all_vars[k] = v

  # Support for indirect variable expansion e.g. with
  # variables:
  #   v1: foo
  #   foo_key: bar
  #   final: %%(%(v1)s_key)s
  # "final" will have the value "bar" (first expanded to "%(foo_key)s" and
  # then to value of "foo_key" i.e. "bar")
  while re.search(r"\%\([a-zA-Z][a-zA-Z0-9_]*\)s", data):
    data = data % all_vars
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
  return spec["tag"] % {**nowKwds, **spec}


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
  nonStringDefaults = [x for x in validDefaults if not type(x) == str]
  if nonStringDefaults:
    return (False, "valid_defaults needs to be a string or a list of strings. Found %s." % nonStringDefaults, [])
  if defaults in validDefaults:
    return (True, "", validDefaults)
  return (False, "Cannot compile %s with `%s' default. Valid defaults are\n%s" % 
                  (finalPkgSpec["package"],
                   defaults,
                   "\n".join([" - " + x for x in validDefaults])), validDefaults)


def doDetectArch(hasOsRelease, osReleaseLines, platformTuple, platformSystem, platformProcessor):
  if platformSystem == "Darwin":
    processor = platformProcessor
    if not processor:
      if platform.machine() == "x86_64":
        processor = "x86-64"
      else:
        processor = "arm64"
    return "osx_%s" % processor.replace("_", "-")
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

  return "{distro}{version}_{machine}".format(
    distro=distribution, version=version.split(".")[0],
    machine=processor.replace("_", "-"))

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

def filterByArchitectureDefaults(arch, defaults, requires):
  for r in requires:
    require, matcher = ":" in r and r.split(":", 1) or (r, ".*")
    if matcher.startswith("defaults="):
      wanted = matcher[len("defaults="):]
      if re.match(wanted, defaults):
        yield require
    if re.match(matcher, arch):
      yield require

def disabledByArchitectureDefaults(arch, defaults, requires):
  for r in requires:
    require, matcher = ":" in r and r.split(":", 1) or (r, ".*")
    if matcher.startswith("defaults="):
      wanted = matcher[len("defaults="):]
      if not re.match(wanted, defaults):
        yield require
    elif not re.match(matcher, arch):
      yield require

def merge_dicts(dict1, dict2):
  """
  Merge two ordered dictionaries where dict2's keys updates dict1's keys recursively.
  """
  # Add all keys from dict1 first
  merged = dict1.copy()
  # Overwrite with dict2's values and add new keys
  for key, value in dict2.items():
    if key not in merged:
      merged[key] = value
      continue
    elif isinstance(merged[key], dict) and isinstance(value, dict):
      # Recursively merge nested ordered dictionaries
      merged[key] = merge_dicts(merged[key], value)
    elif isinstance(merged[key], list) and isinstance(value, list):
      # merge lists, such as for "disabled"
      merged[key].extend(value)
    else:
      # Overwrite existing key or add new key
      merged[key] = value
  return merged

def readDefaults(configDir, defaults, error, architecture):
  defaultsMeta = {}
  defaultsBody = ""

  for xdefaults in defaults:
    xDefaults = resolveDefaultsFilename(xdefaults,configDir)
    xMeta = {}
    if exists(xDefaults):
      err, xMeta, xBody = parseRecipe(getRecipeReader(xDefaults))
      if xBody.strip() != "":
        defaultsBody += "\n" + xBody.strip()
      if err:
        error(err)
        sys.exit(1)
      defaultsMeta = merge_dicts(defaultsMeta, xMeta)

  archDefaults = "{}/defaults-{}.sh".format(configDir, architecture)
  archMeta = {}
  archBody = ""
  if exists(archDefaults):
    err, archMeta, archBody = parseRecipe(getRecipeReader(defaultsFilename))
    if err:
      error(err)
      sys.exit(1)
    for x in ["env", "disable", "overrides"]:
      defaultsMeta.setdefault(x, {}).update(archMeta.get(x, {}))
    defaultsBody += "\n# Architecture defaults\n" + archBody

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
    return open(self.url).read()

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
    with open(filename) as f:
      if extension in ('yaml', 'yml'):
        return yaml.load(f, YamlSafeOrderedLoader)
      elif extension in ('json', ):
        return json.load(f)
      else:
        return ''.join(f.readlines())

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
  except yaml.scanner.ScannerError as e:
    err = "Unable to parse {}\n{}".format(reader.url, str(e))
  except yaml.parser.ParserError as e:
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
     
    if type(overrides_array) == OrderedDict:
        return overrides_array
      
    # Start with an empty OrderedDict
    result = OrderedDict()
    
    for item in overrides_array:
        if isinstance(item, list):
            # Handle nested lists - recursively process each element
            for subitem in item:
                if isinstance(subitem, dict):
                    result = merge_dicts(result, subitem)
        elif isinstance(item, dict):
            result = merge_dicts(result, item)

    debug("asDict (result): %s ",json.dumps(result))
    return result

# (Almost pure part of the defaults parsing)
# Override defaultsGetter for unit tests.
def parseDefaults(disable, defaultsGetter, log):
  defaultsMeta, defaultsBody = defaultsGetter()
  # Defaults are actually special packages. They can override metadata
  # of any other package and they can disable other packages. For
  # example they could decide to switch from ROOT 5 to ROOT 6 and they
  # could disable alien for O2. For this reason we need to parse their
  # metadata early and extract the override and disable data.

  # defaultsMeta["disable"] = asDict(defaultsMeta.get("disable", OrderedDict()))

  defaultsDisable = asList(defaultsMeta.get("disable", []))
   
  for x in defaultsDisable:
    log("Package %s has been disabled by current default.", x)
  disable.extend(defaultsDisable)

  defaultsMeta["overrides"] = asDict(defaultsMeta.get("overrides", OrderedDict()))

  if type(defaultsMeta.get("overrides", OrderedDict())) != OrderedDict:
    return ("overrides should be a dictionary", None, None)

  overrides, taps = OrderedDict(), {}
  commonEnv = {"env": defaultsMeta["env"]} if "env" in defaultsMeta else {}
  overrides["defaults-release"] = commonEnv
  for k, v in defaultsMeta.get("overrides", {}).items():
    f = k.split("@", 1)[0].lower()
    if "@" in k:
      taps[f] = "dist:"+k
    overrides[f] = dict(**(v or {}))
  return (None, overrides, taps)

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
  configPath = os.environ.get("BITS_PATH")
  pkgDirs = [configDir]
  if configPath:
    for d in [join(configDir, "%s.bits" % r) for r in configPath.split(",") if r]:
      if exists(d):
        pkgDirs.append(d)
  return pkgDirs

def resolveFilename(taps, pkg, configDir, generatedPackages, ext=".sh"):
  for d in getConfigPaths(configDir):
    if d in generatedPackages and pkg in generatedPackages[d]:
      meta = generatedPackages[d][pkg]
      return ("generate:{}@{}".format(pkg, meta["version"]), meta["pkgdir"])
    filename = checkForFilename(taps, pkg, d, ext=".sh")
    if exists(filename):
      return (filename, d)
  dieOnError(True, "Package {} not found in {}".format(pkg, configDir))

def resolveDefaultsFilename(defaults, configDir):
  configPath = os.environ.get("BITS_PATH")
  cfgDir = configDir
  pkgDirs = [cfgDir]

  if configPath:
    for d in configPath.split(","):
      pkgDirs.append(cfgDir + "/" + d + ".bits")

  for d in pkgDirs:
    filename = "{}/defaults-{}.sh".format(d, defaults)
    if exists(filename):
      return(filename)

  error("Default `%s' does not exists.\n" % (filename or "<no defaults specified>"))

  '''
  error("Default `%s' does not exists. Viable options:\n%s" %
          (defaults or "<no defaults specified>",
           "\n".join("- " + basename(x).replace("defaults-", "").replace(".sh", "")
                     for x in glob(join(configDir, "defaults-*.sh")))))
  '''

def getPackageList(packages, specs, configDir, preferSystem, noSystem,
                   architecture, disable, defaults, performPreferCheck, performRequirementCheck,
                   performValidateDefaults, overrides, taps, log, force_rebuild=()):
  systemPackages = set()
  ownPackages = set()
  failedRequirements = set()
  testCache = {}
  requirementsCache = {}
  trackingEnvCache = {}
  packages = packages[:]
  generatedPackages = getGeneratedPackages(configDir)
  validDefaults = []  # empty list: all OK; None: no valid default; non-empty list: list of valid ones
  while packages:
    p = packages.pop(0)
    if p in specs:
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

    dieOnError(spec["package"] != p,
               "{} should be spelt {}.".format(p, spec["package"]))

    # If an override fully matches a package, we apply it. This means
    # you can have multiple overrides being applied for a given package.
    for override in overrides:
      # We downcase the regex in parseDefaults(), so downcase the package name
      # as well. FIXME: This is probably a bad idea; we should use
      # re.IGNORECASE instead or just match case-sensitively.
      if not re.fullmatch(override, p.lower()):
        continue
      log("Overrides for package %s: %s", spec["package"], overrides[override])
      spec.update(overrides.get(override, {}) or {})

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
          disable.append(spec["package"])
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
        disable.append(spec["package"])

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

    # For the moment we treat build_requires just as requires.
    fn = lambda what: disabledByArchitectureDefaults(architecture, defaults, spec.get(what, []))
    spec["disabled"] += [x for x in fn("requires")]
    spec["disabled"] += [x for x in fn("build_requires")]
    fn = lambda what: filterByArchitectureDefaults(architecture, defaults, spec.get(what, []))
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
    spec["version"] = spec["version"].replace("/", "_")
    spec["recipe"] = recipe.strip("\n")
    if spec["package"] in force_rebuild:
      spec["force_rebuild"] = True
    specs[spec["package"]] = spec
    packages += spec["requires"]
  return (systemPackages, ownPackages, failedRequirements, validDefaults)

def getGeneratedPackages(configDir):
  all_pkgs = {}
  pkgDirs = getConfigPaths(configDir)
  for pkgdir in pkgDirs:
    dir_pkgs = {}
    for vp in [x.split(os.sep)[-2] for x in glob(join(pkgdir, "*", "packages.py"))]:
      sys.path.insert(0, join(pkgdir, vp))
      pkg = __import__("packages")
      pkg.getPackages(dir_pkgs, pkgdir)
      sys.modules.pop("packages")
      sys.path.pop(0)
    all_pkgs[pkgdir] = dir_pkgs
  return all_pkgs


def handleMergePolicy(override_spec, final_base):
  mergePolicy = override_spec.get("merge_policy", {})
  remove_keys = mergePolicy.get("remove", [])
  force_inherit = mergePolicy.get("inherit", [])
  if isinstance(remove_keys, str):
    remove_keys = remove_keys.replace(" ", "").split(",")
  recipe_append = "recipe" not in remove_keys
  for k in remove_keys:
    if k in final_base:
      final_base.pop(k, None)
  if isinstance(force_inherit, str):
    force_inherit = force_inherit.replace(" ", "").split(",")
  for key in force_inherit:
    if key in final_base:
      override_spec[key] = final_base[key]
  merge_keys = mergePolicy.get("merge", [])
  if isinstance(merge_keys, str):
    merge_keys = merge_keys.replace(" ", "").split(",")
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
    self.h = hashlib.sha1()
  def __call__(self, txt):
    if not type(txt) == bytes:
      txt = txt.encode('utf-8', 'ignore')
    self.h.update(txt)
  def hexdigest(self):
    return self.h.hexdigest()
  def copy(self):
    new_hasher = Hasher()
    new_hasher.h = self.h.copy()
    return new_hasher
