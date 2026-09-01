# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Recipe reading and parsing: locate a recipe (file / git / generated), load its
YAML front-matter (with ``!include`` support), and turn it into a validated spec
dict. Also the spec-level exception, spec validation, and the override merge
policy applied during parsing. Imports the file-path helpers from
bits_helpers.paths; utilities imports back the three entry points its defaults
and package-list code calls."""

import json
import os
import re
import sys
from collections import OrderedDict
from glob import glob
from os.path import join
from typing import Any, IO

import yaml

from bits_helpers.cmd import getoutput
from bits_helpers.git import git
from bits_helpers.log import dieOnError
from bits_helpers.paths import getConfigPaths, resolveFilename

class SpecError(Exception):
  pass


def validateSpec(spec):
  if not spec:
    raise SpecError("Empty recipe.")
  if type(spec) != OrderedDict:
    raise SpecError("Not a YAML key / value.")
  if "package" not in spec:
    raise SpecError("Missing package field in header.")


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

def yamlDump(s):
  # Ordered-map YAML dumper. Kept for external recipe generators (e.g. cms.bits)
  # that import yamlLoad/yamlDump from bits_helpers to re-emit a recipe header.
  class YamlOrderedDumper(yaml.SafeDumper):
    pass
  def represent_ordereddict(dumper, data):
    rep = []
    for k, v in data.items():
      k = dumper.represent_data(k)
      v = dumper.represent_data(v)
      rep.append((k, v))
    return yaml.nodes.MappingNode('tag:yaml.org,2002:map', rep)
  YamlOrderedDumper.add_representer(OrderedDict, represent_ordereddict)
  return yaml.dump(s, Dumper=YamlOrderedDumper)


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

# Whole-line recipe-body include directive:
#     #!include <repo/qualified/path.sh>   (resolved under the recipes repo root)
#     #!include "local/path.sh"            (resolved relative to the recipe's dir)
#
# The marker is `#!include`, NOT plain `#include`: recipe bodies routinely embed
# literal C `#include <string.h>` lines inside heredocs that generate test
# programs (e.g. lcg.bits/gcc-toolchain.sh), so a plain `#include` directive would
# collide with them and try to splice a system header. `#!include` cannot appear
# in C or ordinary shell; it stays `#`-prefixed (so it is an inert comment if the
# preprocessor never runs) and echoes the existing header `!include` YAML tag.
# Only a full line of exactly this shape matches, so C includes, shell `#`
# comments, shebangs, and `# include …` prose are all left untouched.
INCLUDE_RE = re.compile(
  r'^[ \t]*#!include[ \t]+(?:<([^>\n]+)>|"([^"\n]+)")[ \t]*$',
  re.MULTILINE,
)
MAX_INCLUDE_DEPTH = 32


def resolveIncludes(body, recipe_url, repo_dir=None, _visited=None, _depth=0):
  """Splice ``#!include`` directives in a recipe *body* with the referenced file.

  This is a deliberately narrow, bits-owned preprocessor — NOT a full C
  preprocessor (running shell through ``cpp`` mangles ``#`` comments, ``//`` in
  URLs / ``${x//a/b}``, and predefined macros like ``linux``).  It touches only
  whole-line ``#!include <...>`` / ``#!include "..."`` directives and leaves every
  other byte verbatim — crucially including the literal ``#include <header>``
  lines that recipe heredocs use to generate C test programs.

  Resolution mirrors the existing ``from:`` mechanism and C's two include forms:
  ``<path>`` resolves under the recipes repo root (``$BITS_REPO_DIR``), ``"path"``
  relative to the including recipe's own directory.  Inclusion is recursive with
  cycle detection and a depth cap; a path that escapes its base (``..`` or an
  absolute path) is rejected.

  The spliced text is returned verbatim, BEFORE variable substitution and hashing
  run downstream — so an included file's content is expanded in the consumer's
  context (``%(compiler)s`` etc.) and folds into the consumer package's hash,
  exactly as if it had been written inline.
  """
  if body is None or "#!include" not in body:
    return body  # fast path: nothing to do
  if _depth > MAX_INCLUDE_DEPTH:
    raise RuntimeError("#!include: nesting too deep (>%d) at %s" % (MAX_INCLUDE_DEPTH, recipe_url or "?"))
  if _visited is None:
    _visited = []
  if repo_dir is None:
    repo_dir = os.environ.get("BITS_REPO_DIR") or os.path.dirname(recipe_url or "") or "."
  recipe_dir = os.path.dirname(recipe_url or "") or "."

  def _splice(m):
    angle, quoted = m.group(1), m.group(2)
    rel = (angle if angle is not None else quoted).strip()
    base = repo_dir if angle is not None else recipe_dir
    base_abs = os.path.abspath(base)
    path_abs = os.path.abspath(os.path.join(base_abs, rel))
    # Path safety: reject absolute references and any `..` escape outside base.
    if os.path.isabs(rel) or not (path_abs == base_abs or path_abs.startswith(base_abs + os.sep)):
      raise RuntimeError("#!include: unsafe path %r in %s" % (rel, recipe_url or "?"))
    if path_abs in _visited:
      raise RuntimeError("#!include: cyclic include: %s" % " -> ".join(_visited + [path_abs]))
    try:
      with open(path_abs) as f:
        content = f.read()
    except OSError as e:
      raise RuntimeError("#!include: cannot open %r referenced in %s: %s" % (rel, recipe_url or "?", e))
    # Recurse so an included file may itself include (cycle-guarded by _visited).
    return resolveIncludes(content, path_abs, repo_dir, _visited + [path_abs], _depth + 1)

  return INCLUDE_RE.sub(_splice, body)


def parseRecipe(reader, generatePackages=None, visited=None):
  assert(reader.__call__)
  err, spec, recipe = (None, None, None)
  try:
    d = reader()
    header,recipe = d.split("---", 1)
    # Splice any `#!include` directives in the body before anything else sees it,
    # so the included text is variable-expanded and hashed as if written inline.
    recipe = resolveIncludes(recipe, getattr(reader, "url", "") or "")
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
    # Free-text metadata (description, acknowledgment, license, url, homepage,
    # source_url) is prose that routinely contains ": ", parentheses or other
    # characters YAML forbids in an unquoted (plain) scalar — e.g.
    #   description: Foo: the bar
    # trips "mapping values are not allowed here". Auto-quote the single-line value
    # of these keys so authors need not remember YAML quoting, mirroring the
    # %(…)s list-item quoting above. Values that are already quoted, a block scalar
    # (|/>), an anchor/alias/tag, or an inline comment are left alone, and the
    # transform is idempotent. For these prose keys a mid-value '#' is kept as text.
    header = re.sub(
      r'^(\s*(?:description|acknowledge?ment|license|url|homepage|source_url)\s*:[ \t]+)'
      r'(?![|>&*!#"\'])(.*\S)[ \t]*$',
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
