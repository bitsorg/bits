# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""getPackageList — resolve a set of requested packages into the full, ordered
build list: read each recipe, evaluate architecture/variable gates, apply system
(prefer/require) checks via caller callbacks, register version pins and patches,
and fold in checksum-store data. The top of the dependency graph; imports from
every lower layer (recipe, matchers, paths) plus the utilities primitives."""

import re
from collections import OrderedDict
from shlex import quote

from bits_helpers.checksum_store import load_for_spec, merge_into_spec
from bits_helpers.log import banner, debug, dieOnError, warning
from bits_helpers.matchers import (_collect_version_pins, _matcher_active,
                                   disabledByArchitectureDefaults,
                                   filterByArchitectureDefaults, filterPatches)
from bits_helpers.recipe import getRecipeReader, parseRecipe, getGeneratedPackages
from bits_helpers.paths import resolveFilename
from bits_helpers.utilities import recipeSourceLabel, resolve_version
from bits_helpers.defaults import resolve_pkg_family

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
  recipe_sources = {}   # package name -> "<repository>@<commit>" origin label
  required_by = {}      # dep name (bare, lowercased) -> set of "requirer (source)"
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

    # Per-recipe origin trace: record which repository@commit actually supplied
    # this recipe — for every package, not only provider-sourced ones.  This is
    # the first thing to consult when a recipe resolves to an unexpected (e.g.
    # stale) version: if the commit here predates an upstream change, the source
    # checkout was out of date.
    spec["recipe_source"] = recipeSourceLabel(pkgdir, provider_dirs)
    recipe_sources[spec["package"]] = spec["recipe_source"]
    debug("Recipe '%s' resolved from %s  (dir: %s)",
          spec["package"], spec["recipe_source"], pkgdir)

    # Load the optional external checksum store (checksums/<pkg>.checksum)
    # and merge source/patch checksums + commit pin into the spec.
    merge_into_spec(spec, load_for_spec(spec))

    # Track which repository provider supplied this recipe so that
    # storeHashes can fold the provider's commit hash into the build hash.
    if pkgdir in provider_dirs:
      prov_name, prov_hash = provider_dirs[pkgdir]
      spec["recipe_provider"] = prov_name
      spec["recipe_provider_hash"] = prov_hash

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
                           "recipe_source", "force_revision"):
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
    spec["disabled"] += [x for x in fn("untracked_requires")]
    fn = lambda what: filterByArchitectureDefaults(architecture, defaults, spec.get(what, []), _default_vars, _own_version)
    spec["requires"] = [x for x in fn("requires") if x not in disable]
    spec["build_requires"] = [x for x in fn("build_requires") if x not in disable]
    # untracked_requires: real, runtime-linked dependencies that are deliberately
    # NOT folded into this package's identity hash (see storeHashes), so editing
    # one does not invalidate/rebuild its consumers. They still take part in the
    # dependency graph, build ordering and environment via `requires`.
    spec["untracked_requires"] = [x for x in fn("untracked_requires") if x not in disable]
    if spec["package"] != "defaults-release":
      spec["build_requires"].append("defaults-release")
    spec["runtime_requires"] = spec["requires"]
    spec["requires"] = spec["runtime_requires"] + spec["build_requires"] + spec["untracked_requires"]
    # Reverse-dependency trace: remember who pulled in each dependency so a later
    # "package not found" can name the requiring recipe(s) and their origin
    # instead of only the missing name.  Keyed by the bare dep name (version /
    # arch qualifiers stripped) lowercased, matching how pkg_filename is derived
    # when the dep is later resolved.
    _req_label = "{} ({})".format(spec["package"], spec.get("recipe_source", "?"))
    for _dep in spec["requires"]:
      _dk = re.split(r"[:=]", _dep, 1)[0].strip().lower()
      if _dk:
        required_by.setdefault(_dk, set()).add(_req_label)
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

  # ── Recipe-origin summary (package@repository:commit) ───────────────────────
  # One compact, always-on block grouping every resolved recipe by the
  # repository@commit it was loaded from.  Complements the per-recipe debug lines
  # above with an at-a-glance map of which source supplied which packages — the
  # authoritative trace for diagnosing stale or unexpected recipe resolution.
  if recipe_sources:
    by_source = OrderedDict()
    for _pkg, _src in sorted(recipe_sources.items()):
      by_source.setdefault(_src, []).append(_pkg)
    banner("Recipe origins: %d package(s) from %d source(s)",
           len(recipe_sources), len(by_source))
    for _src, _pkgs in by_source.items():
      log("  %s  ←  %s", _src, ", ".join(_pkgs))

  return (systemPackages, ownPackages, failedRequirements, validDefaults)
