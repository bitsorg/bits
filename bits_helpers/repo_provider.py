"""
Iterative discovery and fetching of repository-provider packages.

A *repository provider* is a normal bits recipe that carries the extra YAML
field::

    provides_repository: true

Its ``source`` URL points to a **recipe repository** (a directory that
contains ``*.sh`` recipe files, just like a regular ``*.bits`` checkout).
When bits encounters such a package while scanning for dependencies it:

1. Clones the package's git source into a local cache directory.
2. Adds the checkout to ``BITS_PATH`` (prepend or append, controlled by the
   optional ``repository_position`` field).
3. Restarts dependency scanning so that recipes in the newly-visible directory
   become reachable.

The process repeats until the dependency graph is stable (no new providers
discovered) or ``MAX_PROVIDER_ITERATIONS`` is reached, which makes the
scheme naturally handle *nested providers* (a provider whose own recipe
repository contains further providers).

Cache layout
------------
::

    $BITS_WORK_DIR/
      REPOS/
        <package-lower>/          ← one directory per provider package
          <short_commit_hash>/    ← the actual checkout  (cache key = hash)
            .bits_provider_ok     ← written after a successful checkout
            *.sh                  ← recipe files live here
          latest -> <hash>        ← symlink to the most-recently used entry

If ``.bits_provider_ok`` already exists for the resolved commit hash bits
reuses the checkout without any network access (cache hit).

Reproducibility
---------------
The commit hash of every provider whose recipes are used is stored in
``spec["recipe_provider"]`` / ``spec["recipe_provider_hash"]`` for each
package whose recipe came from that provider.  ``storeHashes`` in
``build.py`` folds the provider hash into the package's content-addressable
build hash so that upgrading a provider triggers a rebuild of all packages
sourced from it.
"""

import glob
import os
import shutil
from collections import OrderedDict
from os.path import join, exists, abspath

from bits_helpers.log import debug, info, warning, banner, dieOnError
from bits_helpers.git import Git
from bits_helpers.workarea import updateReferenceRepoSpec, logged_scm
from bits_helpers.utilities import (
    checkForFilename,
    getConfigPaths,
    getGeneratedPackages,
    getRecipeReader,
    parseRecipe,
    symlink,
)

# Maximum provider-discovery iterations (guards against run-away recursion)
MAX_PROVIDER_ITERATIONS = 20

# Sub-directory under the work dir where provider checkouts are cached
REPOS_CACHE_SUBDIR = "REPOS"

# Reserved package name for the BITS_PROVIDERS / bits.rc synthesised provider
BITS_PROVIDERS_PACKAGE = "bits-providers"


# ── Internal helpers ────────────────────────────────────────────────────────

def _provider_cache_root(work_dir: str, package: str) -> str:
    """Return the per-package cache root: ``<work_dir>/REPOS/<package>/``."""
    return join(abspath(work_dir), REPOS_CACHE_SUBDIR, package.lower())


def _check_for_shadows(
    incoming_dir: str,
    position: str,
    provider_name: str = "",
) -> None:
    """Warn when *incoming_dir* being added as *position* would shadow recipes
    already visible on ``BITS_PATH``.

    Shadowing can only happen when *position* is ``"prepend"`` — the new
    directory lands before every entry already present.  The function computes
    the set of recipe base-names (``*.sh`` files, lower-cased, extension
    stripped) for *incoming_dir* and compares it against all directories
    currently listed in ``BITS_PATH``.  Each collision produces an individual
    warning so that the operator can take corrective action via
    ``provider_policy``.

    The primary config dir (always position 0 in ``getConfigPaths``) is *not*
    stored in ``BITS_PATH`` and is therefore not included in this scan.  It
    is searched first unconditionally and cannot be shadowed by any provider
    regardless of position.
    """
    if position != "prepend":
        return  # append can never shadow entries that come before it

    current = os.environ.get("BITS_PATH", "")
    existing_dirs = [p for p in current.split(",") if p and os.path.isdir(p)]
    if not existing_dirs:
        return  # nothing on BITS_PATH yet — no shadowing possible

    incoming_recipes = {
        os.path.splitext(os.path.basename(f))[0].lower()
        for f in glob.glob(os.path.join(incoming_dir, "*.sh"))
    }
    if not incoming_recipes:
        return  # empty provider directory — nothing to shadow

    label = (
        "Provider %r" % provider_name
        if provider_name
        else "Directory %r" % incoming_dir
    )
    for existing_dir in existing_dirs:
        existing_recipes = {
            os.path.splitext(os.path.basename(f))[0].lower()
            for f in glob.glob(os.path.join(existing_dir, "*.sh"))
        }
        shadowed = incoming_recipes & existing_recipes
        if shadowed:
            warning(
                "%s is being prepended and will shadow %d recipe(s) already "
                "visible from %s: %s\n"
                "  To suppress this warning grant prepend explicitly in bits.rc:\n"
                "    provider_policy = %s:prepend\n"
                "  Or force the safe default:\n"
                "    provider_policy = %s:append",
                label, len(shadowed), existing_dir,
                ", ".join(sorted(shadowed)),
                provider_name or "?",
                provider_name or "?",
            )


def _add_to_bits_path(
    directory: str,
    recipe_position: str = "append",
    provider_name: str = "",
    policy: dict = None,
) -> None:
    """Extend the in-process ``BITS_PATH`` with *directory*.

    The change is written to ``os.environ`` so that every subsequent call to
    ``getConfigPaths`` (which reads ``BITS_PATH``) picks it up.

    Position resolution (highest priority first)
    --------------------------------------------
    1. **User policy** — if *provider_name* appears in *policy* the value
       there wins unconditionally.
    2. **Recipe default** — ``repository_position`` from the provider recipe,
       but only when it is ``"append"`` (the safe direction).  A recipe that
       asks for ``"prepend"`` is *downgraded* to ``"append"`` unless the user
       has explicitly granted prepend via *policy* (rule 1).
    3. **Built-in default** — ``"append"`` when nothing else applies.

    This means providers can never self-elevate to ``"prepend"`` without an
    explicit user opt-in, closing the class of recipe-controlled PATH-hijacking
    attacks described in the security analysis.
    """
    policy = policy or {}

    # Determine effective position
    if provider_name and provider_name in policy:
        position = policy[provider_name]
        if position != recipe_position:
            debug(
                "Provider %r: policy overrides recipe position %r → %r",
                provider_name, recipe_position, position,
            )
    elif recipe_position == "prepend" and provider_name:
        # Recipe asks for prepend but the user has not granted it — downgrade.
        warning(
            "Provider %r requested repository_position: prepend but no "
            "provider_policy entry grants it.  Falling back to append (safe "
            "default).  To allow prepend, add to bits.rc:\n"
            "  provider_policy = %s:prepend",
            provider_name, provider_name,
        )
        position = "append"
    else:
        position = recipe_position

    # Warn before mutating BITS_PATH (Solution B — shadow detection)
    _check_for_shadows(directory, position, provider_name)

    current = os.environ.get("BITS_PATH", "")
    parts = [p for p in current.split(",") if p]
    if directory in parts:
        debug("Provider dir already in BITS_PATH: %s", directory)
        return
    if position == "prepend":
        parts.insert(0, directory)
    else:
        parts.append(directory)
    os.environ["BITS_PATH"] = ",".join(parts)
    debug("BITS_PATH updated (%s): %s", position, os.environ["BITS_PATH"])


def _try_read_spec(pkg_lower: str, config_dir: str, taps: dict):
    """Try to locate and parse only the YAML header of *pkg_lower*.

    Returns an ``OrderedDict`` spec on success, ``None`` if the recipe is not
    found on the current ``BITS_PATH`` (without terminating the process).
    """
    generated = getGeneratedPackages(config_dir)
    for pkg_dir in getConfigPaths(config_dir):
        gen_pkgs_for_dir = generated.get(pkg_dir, {})
        if pkg_lower in gen_pkgs_for_dir:
            meta = gen_pkgs_for_dir[pkg_lower]
            filename = "generate:{}@{}".format(pkg_lower, meta["version"])
            gen_pkgs = gen_pkgs_for_dir
        else:
            filename = checkForFilename(taps, pkg_lower, pkg_dir)
            if not exists(filename):
                continue
            gen_pkgs = {}

        err, spec, _ = parseRecipe(getRecipeReader(filename, config_dir, gen_pkgs))
        if err or spec is None:
            continue
        return spec
    return None


# ── Clone / cache a provider repository ────────────────────────────────────

def clone_or_update_provider(
    spec: OrderedDict,
    work_dir: str,
    reference_sources: str,
    fetch_repos: bool,
) -> tuple:
    """Clone (or reuse a cached checkout of) the repository described by *spec*.

    Returns ``(checkout_dir, commit_hash)`` where *checkout_dir* is the local
    directory that should be added to ``BITS_PATH``.

    The function follows the same mirror-then-clone pattern used by the main
    build system:

    1. Create / update a bare *mirror* of the source repository under
       *reference_sources* (same directory that ``--reference-sources`` uses).
    2. Resolve ``tag`` to an actual commit hash via ``ls-remote``.
    3. If a checkout for that hash already exists (cache hit), reuse it.
    4. Otherwise clone from the mirror into the cache directory and check
       out the requested tag.
    """
    package = spec["package"]
    source = spec.get("source", "")
    tag = spec.get("tag", spec.get("version", "HEAD"))

    dieOnError(not source,
               "Repository provider '%s' has no 'source' URL." % package)

    scm = Git()
    cache_root = _provider_cache_root(work_dir, package)
    os.makedirs(cache_root, exist_ok=True)

    # ── 1. Update / create bare mirror ──────────────────────────────────
    # Always refresh the mirror when a cached checkout already exists so that
    # we can detect upstream changes on every run (the user may have tagged a
    # new version of the provider repository since the last build).  On the
    # very first clone there is no cache yet, so we respect the caller's
    # ``fetch_repos`` flag to avoid unnecessary network access.
    has_cached_checkout = exists(join(cache_root, "latest"))
    mirror_spec = OrderedDict(spec)
    mirror_spec["scm"] = scm
    mirror_spec["is_devel_pkg"] = False
    updateReferenceRepoSpec(
        reference_sources, package, mirror_spec,
        fetch=fetch_repos or has_cached_checkout,
        usePartialClone=True, allowGitPrompt=False,
    )
    mirror_dir = mirror_spec.get("reference")

    # ── 2. Resolve tag → commit hash ────────────────────────────────────
    repo_for_ls = mirror_dir or source
    try:
        refs_out = logged_scm(
            scm, package, reference_sources,
            scm.listRefsCmd(repo_for_ls),
            ".", prompt=False, logOutput=False,
        )
        scm_refs = scm.parseRefs(refs_out)
    except SystemExit:
        scm_refs = {}

    commit_hash = (
        scm_refs.get("refs/tags/" + tag)
        or scm_refs.get("refs/heads/" + tag)
        or tag  # fall-back: tag is already a raw commit hash
    )
    short_hash = commit_hash[:10] if len(commit_hash) > 10 else commit_hash
    # Safety: an empty hash would collapse checkout_dir to cache_root itself,
    # causing shutil.rmtree to wipe the entire package cache on the next step.
    dieOnError(not short_hash,
               "commit_hash resolved to empty string for provider '%s' tag '%s' — "
               "refusing to construct checkout_dir to prevent clobbering the "
               "package cache." % (package, tag))
    checkout_dir = join(cache_root, short_hash)

    # ── 3. Cache-hit check ───────────────────────────────────────────────
    # The marker file is written only after a successful checkout.  If it
    # exists for the hash we just resolved from the (freshly-updated) mirror,
    # the provider is up-to-date and we can reuse the cached directory.
    marker = join(checkout_dir, ".bits_provider_ok")
    if exists(marker):
        debug("Provider '%s' is up-to-date (cache hit @ %s)", package, short_hash)
        info("Reusing cached provider '%s' @ %s", package, short_hash)
        symlink(short_hash, join(cache_root, "latest"))
        return checkout_dir, commit_hash

    # ── 4. Clone + checkout ──────────────────────────────────────────────
    banner("Fetching repository provider '%s' @ %s", package, tag)
    # Safety: refuse to clone over an existing git repository — this would
    # destroy a different provider's checkout if two packages ever resolved to
    # the same (or an empty) hash subdirectory.
    dieOnError(exists(join(checkout_dir, ".git")),
               "checkout_dir '%s' already contains a .git repository; refusing "
               "to clone provider '%s' over it to prevent clobbering an existing "
               "checkout." % (checkout_dir, package))
    shutil.rmtree(checkout_dir, ignore_errors=True)

    err, out = scm.exec(
        scm.cloneSourceCmd(source, checkout_dir, mirror_dir, usePartialClone=True),
        directory=".", check=False,
    )
    dieOnError(err,
               "Failed to clone repository provider '%s' from %s:\n%s"
               % (package, source, out))

    err, out = scm.exec(
        scm.checkoutCmd(tag), directory=checkout_dir, check=False,
    )
    dieOnError(err,
               "Failed to check out tag '%s' for provider '%s':\n%s"
               % (tag, package, out))

    # Ensure the checkout directory exists (the actual git clone creates it,
    # but tests or edge-cases may not – makedirs is idempotent).
    os.makedirs(checkout_dir, exist_ok=True)

    # Write the completion marker so subsequent runs get a cache hit
    with open(marker, "w") as fh:
        fh.write(commit_hash + "\n")

    symlink(short_hash, join(cache_root, "latest"))
    info("Provider '%s' ready at %s", package, checkout_dir)
    return checkout_dir, commit_hash


# ── Always-on provider loading ───────────────────────────────────────────────

def _parse_provider_url(url_spec: str) -> tuple:
  """Parse a provider URL with an optional ``@tag`` suffix.

  Returns ``(url, tag)`` where *tag* defaults to ``"main"`` when not given::

      _parse_provider_url("https://github.com/org/repo.git")
      # → ("https://github.com/org/repo.git", "main")

      _parse_provider_url("https://github.com/org/repo.git@stable")
      # → ("https://github.com/org/repo.git", "stable")
  """
  url, sep, tag = url_spec.partition("@")
  return url.strip(), (tag.strip() or "main")


def _make_bits_providers_spec(url: str, tag: str) -> OrderedDict:
  """Synthesise the virtual ``bits-providers`` provider spec from a URL + tag.

  The returned spec matches the layout bits.rc users would write by hand::

      package: bits-providers
      version: "1"
      source: <url>
      tag: <tag>
      provides_repository: true
      always_load: true
      repository_position: prepend
  """
  return OrderedDict([
    ("package",               BITS_PROVIDERS_PACKAGE),
    ("version",               "1"),
    ("source",                url),
    ("tag",                   tag),
    ("provides_repository",   True),
    ("always_load",           True),
    ("repository_position",   "append"),
  ])


def load_always_on_providers(
  config_dir: str,
  work_dir: str,
  reference_sources: str,
  fetch_repos: bool,
  bits_providers: str = None,
  taps: dict = None,
  provider_policy: dict = None,
) -> dict:
  """Clone providers that must be loaded unconditionally before any
  dependency-graph traversal.

  Two sources of always-on providers are consulted in order:

  1. **``bits_providers`` / ``BITS_PROVIDERS``** — when *bits_providers* is
     non-empty a virtual :data:`BITS_PROVIDERS_PACKAGE` recipe is synthesised
     from the URL (with an optional ``@tag`` suffix, default ``main``) and
     cloned immediately.  This corresponds to the auto-constructed recipe::

         package: bits-providers
         version: "1"
         source: <url>
         tag: <tag>
         provides_repository: true
         always_load: true
         repository_position: prepend

  2. **``always_load: true`` recipes in the primary config dir** — every
     recipe file in *config_dir* that declares **both** ``provides_repository:
     true`` and ``always_load: true`` is cloned before ``getPackageList``
     runs.  A recipe named ``bits-providers`` is skipped here when source 1
     already handled it (avoiding a double-clone).

  Returns a ``{checkout_dir: (package_name, commit_hash)}`` dict in the same
  format as :func:`fetch_repo_providers_iteratively`, so its entries can be
  merged into the final ``provider_dirs`` for build-hash propagation.

  Failures in individual clones are logged as warnings and do not abort the
  build, so a temporarily unreachable provider repository does not block work
  on packages that do not depend on it.
  """
  provider_dirs: dict = {}
  taps = taps or {}
  policy = provider_policy or {}

  # ── 1. BITS_PROVIDERS / bits.rc ``providers`` ───────────────────────────
  if bits_providers:
    url, tag = _parse_provider_url(bits_providers)
    spec = _make_bits_providers_spec(url, tag)
    debug("Always-on provider from BITS_PROVIDERS: %s @ %s", url, tag)
    try:
      checkout_dir, commit_hash = clone_or_update_provider(
        spec, work_dir, reference_sources, fetch_repos,
      )
      _add_to_bits_path(
        checkout_dir,
        recipe_position=spec["repository_position"],
        provider_name=BITS_PROVIDERS_PACKAGE,
        policy=policy,
      )
      provider_dirs[checkout_dir] = (BITS_PROVIDERS_PACKAGE, commit_hash)
    except SystemExit:
      warning(
        "Failed to load BITS_PROVIDERS from %s — continuing without it.",
        bits_providers,
      )

  # ── 2. ``always_load: true`` recipes in the primary config dir ──────────
  for sh_path in sorted(glob.glob(os.path.join(abspath(config_dir), "*.sh"))):
    try:
      err, spec, _ = parseRecipe(getRecipeReader(sh_path))
    except Exception:
      continue
    if err or spec is None:
      continue
    if not (spec.get("always_load") and spec.get("provides_repository")):
      continue
    pkg = spec["package"]
    # Skip if BITS_PROVIDERS already loaded a recipe with the reserved name so
    # we do not clone the same (or a conflicting) repository twice.
    if pkg == BITS_PROVIDERS_PACKAGE and bits_providers:
      debug("Skipping always_load recipe '%s': already handled via BITS_PROVIDERS",
            pkg)
      continue
    debug("Always-loading provider '%s' from config dir", pkg)
    try:
      checkout_dir, commit_hash = clone_or_update_provider(
        spec, work_dir, reference_sources, fetch_repos,
      )
      _add_to_bits_path(
        checkout_dir,
        recipe_position=spec.get("repository_position", "append"),
        provider_name=pkg,
        policy=policy,
      )
      provider_dirs[checkout_dir] = (pkg, commit_hash)
    except SystemExit:
      warning(
        "Failed to always-load provider '%s' — continuing without it.", pkg,
      )

  if provider_dirs:
    banner(
      "Always-on providers loaded:\n%s",
      "\n".join(
        "  %-20s  %s  (commit %s)" % (name, checkout, commit[:10])
        for checkout, (name, commit) in provider_dirs.items()
      ),
    )

  return provider_dirs


# ── CWD recipe-directory detection ─────────────────────────────────────────

def cwd_is_recipe_dir() -> bool:
  """Return True if the current working directory looks like a bits recipe repo.

  The definitive marker of a bits recipe repository is the presence of a
  ``defaults-release.sh`` file.  Every community recipe repo ships one so
  that bits knows which default build profile to apply.  Checking for this
  specific file avoids false-positive matches on arbitrary directories that
  happen to contain ``*.sh`` files with YAML headers.

  This check is intentionally fast (a single ``os.path.exists`` call) so it
  can be called on every ``bits build`` invocation without measurable overhead.
  """
  return os.path.exists("defaults-release.sh")


# ── Backward-compat bootstrap ───────────────────────────────────────────────

def bootstrap_default_config(args, work_dir: str) -> str | None:
  """Bootstrap a default recipe repository when no config dir exists.

  Called when ``bits build <PKG>`` is run without a pre-existing recipe
  directory.  The lookup order for which community recipe repo to clone is:

  1. **``organisation`` from bits.rc / ``--organisation``** — if set to e.g.
     ``lhcb``, bits looks for ``lhcb.bits.sh`` in the bits-providers checkout.
  2. **``default.bits.sh``** — fallback for backward-compatibility with the
     original ALICE workflow when no organisation is configured.

  Procedure:

  1. Fetch **bits-providers** (using the URL from ``args.bits_providers``).
  2. Resolve the candidate recipe filename: ``<org>.bits.sh`` or
     ``default.bits.sh``.
  3. Parse that recipe and clone the ``source`` repository it points to.
  4. Return the local checkout path (caller assigns it to ``args.configDir``).

  Returns ``None`` when any step cannot proceed; the caller decides whether
  to die or show the normal "missing config dir" error.
  """
  bits_providers_url = getattr(args, "bits_providers", None)
  if not bits_providers_url:
    return None

  reference_sources = getattr(args, "referenceSources", "")
  fetch_repos = getattr(args, "fetchRepos", True)

  # ── 1. Clone / update bits-providers ──────────────────────────────────
  url, tag = _parse_provider_url(bits_providers_url)
  spec = _make_bits_providers_spec(url, tag)
  try:
    info("Bootstrapping: fetching bits-providers from %s …", url)
    providers_checkout, _ = clone_or_update_provider(
      spec, work_dir, reference_sources, fetch_repos,
    )
  except SystemExit:
    warning("Bootstrap failed: could not clone bits-providers from %s", url)
    return None

  # ── 2. Resolve candidate recipe file ──────────────────────────────────
  # Prefer <organisation>.bits.sh when an organisation is configured so that
  # "bits init --organisation lhcb && bits build PKG" just works without any
  # other arguments.  Fall back to default.bits.sh for ALICE backward compat.
  # Organisation is stored in uppercase in bits.rc (e.g. "ALICE", "LHCB") but
  # the bits-providers filenames are lowercase (alice.bits.sh, lhcb.bits.sh).
  organisation = (getattr(args, "organisation", None) or "").lower()
  candidates = []
  if organisation:
    candidates.append(("%s.bits.sh" % organisation, organisation))
  candidates.append(("default.bits.sh", "default"))

  chosen_sh = None
  chosen_label = None
  for filename, label in candidates:
    path = join(providers_checkout, filename)
    if exists(path):
      chosen_sh = path
      chosen_label = label
      break

  if chosen_sh is None:
    if organisation:
      debug(
        "Bootstrap: neither %s.bits.sh nor default.bits.sh found in "
        "bits-providers — cannot auto-configure",
        organisation,
      )
    else:
      debug("Bootstrap: no default.bits.sh in bits-providers — nothing to auto-configure")
    return None

  try:
    err, default_spec, _ = parseRecipe(getRecipeReader(chosen_sh))
  except Exception as exc:
    warning("Bootstrap: could not parse %s.bits.sh: %s", chosen_label, exc)
    return None
  if err or default_spec is None:
    warning("Bootstrap: parse error in %s.bits.sh: %s", chosen_label, err)
    return None

  default_source = default_spec.get("source", "")
  if not default_source:
    warning("Bootstrap: %s.bits.sh has no 'source' URL", chosen_label)
    return None

  # ── 3. Clone the config repository ────────────────────────────────────
  try:
    info(
      "Bootstrapping: cloning %s recipe repository from %s …",
      chosen_label, default_source,
    )
    checkout_dir, _ = clone_or_update_provider(
      default_spec, work_dir, reference_sources, fetch_repos,
    )
  except SystemExit:
    warning(
      "Bootstrap failed: could not clone %s config repository from %s",
      chosen_label, default_source,
    )
    return None

  info("Bootstrap complete: using recipe repository at %s", checkout_dir)
  return checkout_dir


# ── Iterative provider discovery ────────────────────────────────────────────

def fetch_repo_providers_iteratively(
    packages: list,
    config_dir: str,
    work_dir: str,
    reference_sources: str,
    fetch_repos: bool,
    taps: dict,
    provider_policy: dict = None,
) -> dict:
    """Discover, clone, and register all repository-provider packages
    reachable from the *packages* list.

    Returns a dict ``{checkout_dir: (package_name, commit_hash)}`` suitable
    for passing to ``getPackageList`` as *provider_dirs*.

    Algorithm
    ---------
    Each outer iteration does a depth-first walk of the dependency graph
    using whatever is currently on ``BITS_PATH``.  When a package with
    ``provides_repository: true`` is encountered for the first time, its
    repository is cloned and added to ``BITS_PATH``; the walk then restarts
    from scratch so that recipes newly visible on the extended path (including
    any providers *inside* the freshly-cloned repository) are discovered.
    The loop terminates when a full walk completes without finding any new
    providers (stable point) or after ``MAX_PROVIDER_ITERATIONS`` restarts.
    """
    # checkout_dir -> (pkg_name, commit_hash)
    provider_dirs: dict = {}
    policy = provider_policy or {}
    # package names already cloned (avoids re-cloning on every restart)
    cloned: set = set()
    # packages we have successfully read (cache to avoid re-parsing)
    resolved: dict = {}
    # packages that couldn't be found on the most recent full walk
    not_found: set = set()

    for iteration in range(MAX_PROVIDER_ITERATIONS):
        debug("Provider discovery: starting iteration %d", iteration + 1)

        found_new_provider = False
        # Packages to visit in this walk.  After a provider is cloned, we
        # also re-queue anything that was "not found" in previous walks
        # because it might now be reachable.
        queue = list(packages)
        visited: set = set()

        while queue:
            pkg = queue.pop(0)
            pkg_lower = pkg.lower()

            if pkg_lower in visited:
                continue
            visited.add(pkg_lower)

            # Use cached spec when available
            if pkg in resolved:
                spec = resolved[pkg]
            else:
                spec = _try_read_spec(pkg_lower, config_dir, taps)
                if spec is None:
                    not_found.add(pkg)
                    continue
                resolved[pkg] = spec
                not_found.discard(pkg)

            # ── New provider found ───────────────────────────────────────
            if spec.get("provides_repository") and pkg not in cloned:
                checkout_dir, commit_hash = clone_or_update_provider(
                    spec, work_dir, reference_sources, fetch_repos,
                )
                _add_to_bits_path(
                    checkout_dir,
                    recipe_position=spec.get("repository_position", "append"),
                    provider_name=pkg,
                    policy=policy,
                )
                provider_dirs[checkout_dir] = (pkg, commit_hash)
                cloned.add(pkg)

                # Invalidate the resolved-spec cache for packages that were
                # not previously findable; they may now be reachable via the
                # newly-added directory.
                for missed in list(not_found):
                    resolved.pop(missed, None)
                queue.extend(not_found)

                found_new_provider = True
                break  # restart the walk with the extended BITS_PATH

            # ── Enqueue transitive dependencies ─────────────────────────
            deps = (
                list(spec.get("requires", []))
                + list(spec.get("build_requires", []))
            )
            queue.extend(r for r in deps if r.lower() not in visited)

        if not found_new_provider:
            debug("Provider discovery stable after %d iteration(s).", iteration + 1)
            break
    else:
        warning(
            "Reached the maximum number of provider-discovery iterations (%d). "
            "Some repository providers may not have been loaded.",
            MAX_PROVIDER_ITERATIONS,
        )

    if provider_dirs:
        banner(
            "Repository providers loaded:\n%s",
            "\n".join(
                "  %s  ->  %s  (commit %s)" % (name, checkout, commit[:10])
                for checkout, (name, commit) in provider_dirs.items()
            ),
        )

    return provider_dirs
