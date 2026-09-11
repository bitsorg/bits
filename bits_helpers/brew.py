# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""`bits brew` — generate a Homebrew Brewfile from the recipes.

macOS is a developer platform for bits (it does not build/distribute CVMFS
packages there), so stable, low-level system libraries are sourced from Homebrew
rather than built. A recipe opts in by declaring, in its YAML header:

    homebrew_formula: readline          # one formula, or a list
    homebrew_taps:                      # optional, rarely needed
      - some/tap

This command collects every such declaration that applies to the target
architecture (its prefer_system must match, when present) and writes a Brewfile
— the executable manifest of the macOS "system layer".

Where the Brewfile lives
------------------------
The Brewfile is a *local, per-architecture build artifact*, not something
committed next to the recipes. It is written into the build work area at
``<work-dir>/<arch>/Brewfile`` (work-dir defaults to ``sw``), keyed to the
architecture and the recipes your current configuration resolves — exactly like
the per-package install trees under ``<work-dir>/<arch>/``. This mirrors the
Linux side, where the container images are the system layer; on macOS the
Brewfile at ``sw/<arch>/Brewfile`` plays that role.

``bits build`` is the authoritative emitter: after it resolves the dependency
graph for your configuration it (re)writes ``<work-dir>/<arch>/Brewfile`` from
the recipes it actually resolved, so the file always matches the closure of the
last build. This happens on dry runs too, so

    bits build --dry-run <target>

generates the exact closure Brewfile without building anything — the canonical
way to produce it for a multi-repo provider stack (e.g. atlas.bits + lcg.bits),
since it goes through real provider discovery and dependency resolution. With
``bits build --brew`` a pre-existing Brewfile is installed in one shot
(``brew bundle``) *before* resolution, so subsequent builds reuse it instead of
re-running each recipe's on-demand ``brew install``.

``bits brew`` is a lighter, no-resolution alternative: it scans the config dir
plus any provider repositories already cloned under ``<work-dir>/REPOS`` (from a
previous build) and writes the same ``<work-dir>/<arch>/Brewfile``. Use it for a
quick pass; use ``bits build --dry-run`` when you need the exact closure.
Install with ``brew bundle --file <work-dir>/<arch>/Brewfile``.
"""

import os
import sys
import glob

from bits_helpers.log import debug, error, info
from bits_helpers.recipe import parseRecipe, FileReader


def _as_list(value):
  """Normalise a scalar-or-list YAML value into a list of strings."""
  if value is None:
    return []
  if isinstance(value, (list, tuple)):
    return [str(v).strip() for v in value if str(v).strip()]
  return [str(value).strip()] if str(value).strip() else []


# Formulae the bits BUILD SYSTEM itself needs on macOS, independent of any recipe.
# gnu-tar (gtar): deterministic package tarballs — build_template.sh uses GNU tar's
# --sort/--mtime so packages are byte-identical across nodes (finding R1). Always
# emitted so it can never fall out of the Brewfile when recipes change.
BASE_FORMULAE = frozenset({"gnu-tar"})


def default_brewfile_path(work_dir, architecture):
  """Return the local per-arch Brewfile path ``<work-dir>/<arch>/Brewfile``.

  This is a build artifact keyed to the architecture, alongside the per-package
  install trees, not a file committed next to the recipes.
  """
  return os.path.join(os.path.abspath(work_dir), str(architecture), "Brewfile")


def _formula_from_spec(spec, architecture, formulae, taps, where):
  """Fold one spec's homebrew declaration into (formulae, taps) if it applies.

  A recipe contributes its ``homebrew_formula`` when it declares no
  ``prefer_system`` (author opted in unconditionally) or its ``prefer_system``
  regex matches ``architecture`` (so a Linux-only declaration is never emitted
  into a macOS Brewfile). ``where`` names the source for error messages.
  """
  import re
  declared = _as_list(spec.get("homebrew_formula"))
  if not declared:
    return
  pref = spec.get("prefer_system")
  if pref is not None:
    try:
      if not re.match(pref, architecture):
        return
    except re.error:
      error("brew: malformed prefer_system %r in %s", pref, where)
      return
  formulae.update(declared)
  taps.update(_as_list(spec.get("homebrew_taps")))


def collect_homebrew(configDirs, architecture):
  """Return (formulae, taps) declared by recipes for architecture.

  ``configDirs`` is a directory path or an iterable of directory paths; every
  ``*.sh`` under each is parsed as a recipe (non-recipes such as ``defaults-*.sh``
  are skipped). The Brewfile is a macOS artifact, so for a non-osx architecture
  nothing is emitted; on osx the build-system base formulae (BASE_FORMULAE) are
  always included.
  """
  formulae, taps = set(), set()
  if not str(architecture).startswith("osx"):
    return formulae, taps
  formulae |= BASE_FORMULAE
  if isinstance(configDirs, str):
    configDirs = [configDirs]
  seen = set()
  for configDir in configDirs:
    for path in sorted(glob.glob(os.path.join(configDir, "*.sh"))):
      real = os.path.realpath(path)
      if real in seen:
        continue
      seen.add(real)
      err, spec, _ = parseRecipe(FileReader(path))
      if err or not spec:
        # Not a parseable recipe (e.g. a defaults-*.sh helper). Skip quietly.
        continue
      _formula_from_spec(spec, architecture, formulae, taps, os.path.basename(path))
  return formulae, taps


def collect_homebrew_from_specs(specs, architecture):
  """Return (formulae, taps) for an already-resolved spec graph.

  ``specs`` is the dict of resolved package specs (as ``bits build`` produces
  after dependency resolution). This is the authoritative, config-accurate set:
  only the recipes the current configuration actually pulls in contribute, so the
  emitted Brewfile is exactly what this build needs. On a non-osx architecture
  nothing is emitted; on osx the build-system base formulae are always included.
  """
  formulae, taps = set(), set()
  if not str(architecture).startswith("osx"):
    return formulae, taps
  formulae |= BASE_FORMULAE
  for spec in specs.values():
    if not isinstance(spec, dict):
      continue
    _formula_from_spec(spec, architecture, formulae, taps,
                       spec.get("package", "<unknown>"))
  return formulae, taps


def _standalone_scan_dirs(configDir, work_dir):
  """Directories `bits brew` scans standalone: configDir + cloned providers.

  Provider repositories cloned by a previous build live under
  ``<work-dir>/REPOS``; including them makes a standalone ``bits brew`` closure-
  aware on a multi-repo provider stack without re-running provider discovery.
  Before the first build there are none, so only configDir is scanned.
  """
  dirs = [configDir]
  repos = os.path.join(os.path.abspath(work_dir), "REPOS")
  if os.path.isdir(repos):
    # Any directory under REPOS that contains recipes. Duplicates across
    # provider versions are harmless — formulae are collected into a set.
    found = set()
    for path in glob.glob(os.path.join(repos, "**", "*.sh"), recursive=True):
      found.add(os.path.dirname(path))
    dirs.extend(sorted(found))
  return dirs


def render_brewfile(formulae, taps, architecture):
  """Render a sorted, deterministic Brewfile string."""
  lines = [
    "# Generated by bits — do not edit by hand.",
    "# Local per-arch build artifact (source of truth: the homebrew_formula:",
    "# fields in the bits recipes, plus gnu-tar for reproducible tarballs).",
    "# Emitted by `bits build` and by `bits brew -a %s`." % architecture,
    "# Install with:  brew bundle --file <this file>",
    "",
  ]
  for tap in sorted(taps):
    lines.append('tap "%s"' % tap)
  if taps:
    lines.append("")
  for formula in sorted(formulae):
    lines.append('brew "%s"' % formula)
  return "\n".join(lines) + "\n"


def write_brewfile(path, formulae, taps, architecture):
  """Write the rendered Brewfile to *path*, creating parent dirs. Returns content."""
  content = render_brewfile(formulae, taps, architecture)
  out_dir = os.path.dirname(os.path.abspath(path))
  if out_dir and not os.path.isdir(out_dir):
    os.makedirs(out_dir, exist_ok=True)
  with open(path, "w") as fh:
    fh.write(content)
  return content


def doBrew(args, parser):
  """Entry point for `bits brew`."""
  if not str(args.architecture).startswith("osx"):
    info("brew: architecture %s is not macOS; the Brewfile will list only "
         "recipes whose prefer_system matches it (usually none).", args.architecture)

  if not os.path.isdir(args.configDir):
    parser.error("config directory not found: %s" % args.configDir)

  # The Brewfile is a local, per-arch build artifact: default it into the work
  # area at <work-dir>/<arch>/Brewfile, not next to the recipes.
  if args.output is None:
    args.output = default_brewfile_path(args.workDir, args.architecture)

  scan_dirs = _standalone_scan_dirs(args.configDir, args.workDir)
  formulae, taps = collect_homebrew(scan_dirs, args.architecture)
  content = render_brewfile(formulae, taps, args.architecture)

  if args.check:
    # Compare the existing file against what would be generated now.
    if args.output == "-":
      parser.error("--check needs a real --output file, not stdout")
    try:
      with open(args.output) as fh:
        current = fh.read()
    except OSError:
      error("brew: %s is missing; run 'bits brew -o %s' to create it.",
            args.output, args.output)
      return False
    if current != content:
      error("brew: %s is out of date; regenerate with 'bits brew -o %s'.",
            args.output, args.output)
      return False
    info("brew: %s is up to date (%d formulae).", args.output, len(formulae))
    return True

  if args.output == "-":
    sys.stdout.write(content)
  else:
    write_brewfile(args.output, formulae, taps, args.architecture)
    info("brew: wrote %d formulae%s to %s", len(formulae),
         (" and %d taps" % len(taps)) if taps else "", args.output)
  return True
