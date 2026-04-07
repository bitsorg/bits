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


# ── Internal helpers ────────────────────────────────────────────────────────

def _provider_cache_root(work_dir: str, package: str) -> str:
    """Return the per-package cache root: ``<work_dir>/REPOS/<package>/``."""
    return join(abspath(work_dir), REPOS_CACHE_SUBDIR, package.lower())


def _add_to_bits_path(directory: str, position: str = "append") -> None:
    """Extend the in-process ``BITS_PATH`` with *directory*.

    The change is written to ``os.environ`` so that every subsequent call to
    ``getConfigPaths`` (which reads ``BITS_PATH``) picks it up.
    """
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
    mirror_spec = OrderedDict(spec)
    mirror_spec["scm"] = scm
    mirror_spec["is_devel_pkg"] = False
    updateReferenceRepoSpec(
        reference_sources, package, mirror_spec,
        fetch=fetch_repos, usePartialClone=True, allowGitPrompt=False,
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
    checkout_dir = join(cache_root, short_hash)

    # ── 3. Cache-hit check ───────────────────────────────────────────────
    marker = join(checkout_dir, ".bits_provider_ok")
    if exists(marker):
        info("Reusing cached provider '%s' @ %s", package, short_hash)
        symlink(short_hash, join(cache_root, "latest"))
        return checkout_dir, commit_hash

    # ── 4. Clone + checkout ──────────────────────────────────────────────
    banner("Fetching repository provider '%s' @ %s", package, tag)
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


# ── Iterative provider discovery ────────────────────────────────────────────

def fetch_repo_providers_iteratively(
    packages: list,
    config_dir: str,
    work_dir: str,
    reference_sources: str,
    fetch_repos: bool,
    taps: dict,
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
                position = spec.get("repository_position", "append")
                _add_to_bits_path(checkout_dir, position)
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
