# Relaxed CVMFS reuse + foreign-deployment importer (`bits import`), plus the `--initdotsh-from-modules` env-unification experiment

## Summary

This PR lands the first end-to-end implementation of **ADR-0001 (relaxed CVMFS
reuse)** — letting a developer build the top of a software stack on top of a
blessed, already-published CVMFS release (e.g. an LCG release) without
recompiling everything underneath — together with the **`bits import`** tool
that converts a foreign (non-bits) CVMFS deployment into a bits-consumable reuse
overlay. It also adds an opt-in, hashed **`--initdotsh-from-modules`** build mode
plus a diagnostic harness, as the first step toward making a package's modulefile
the single source of truth for both its runtime *and* its build/development
environment. A latent crash in the Boto3 upload path is fixed along the way.

Everything new is **default-off and hash-neutral**: with no new flags the simple
`aliBuild`-style build is bit-for-bit unchanged (identical hashes, no forced
rebuild, no schema break). New behaviour is reached only through explicit opt-in,
and any opt-in that changes build output folds into the package hash.

~2,500 lines added across 29 files; 1,096 tests pass (2 skipped).

## Motivation

Two long-standing pain points:

1. **Slow developer iteration on top of a release.** Strict content-addressed
   reuse only reuses a dependency when its hash matches the current recipe
   exactly. A developer who only wants to patch the top of the stack still pays
   for a full rebuild of the base. Relaxed reuse grafts the deployed packages of
   a blessed release matched on `(name, architecture, build_id)`, so only the
   packages the developer actually touches are built.

2. **Build env ≠ development env.** The runtime/development modulefile exposes the
   complete environment (`CMAKE_PREFIX_PATH`, `PYTHONPATH` site-packages,
   `<PKG>_ROOT`, include dirs), but the build-time `init.sh` exposes a subset —
   which is why hundreds of recipes hand-reconstruct those variables
   (`bits_pythonpath_from_deps`, `CMakeRecipe`'s `CMAKE_PREFIX_PATH` rebuild,
   inline `-DCMAKE_PREFIX_PATH`). The `--initdotsh-from-modules` work is an
   experiment toward closing that gap so the build inherits the same env a user
   develops in.

## What's included

### 1. ADR-0001 relaxed CVMFS reuse (Stages 0–1)

- **Provenance & coherence token** (`bits_helpers/provenance.py`): a deterministic,
  content-derived `build_id` (per-release coherence token) and a readable
  `abi_tag`, written additively into each package's `.meta.json`. Same `build_id`
  ⇒ the packages were built together ⇒ an ABI-consistent subset, so no closure
  walk is needed at reuse time.
- **`--reuse-policy {strict,relaxed}` / `--reuse-base <build_id>` / `--build-local`**
  (`bits_helpers/args.py`, `build.py`): `strict` (default) reuses only on exact
  hash and is publishable; `relaxed` additionally grafts a blessed release's
  deployed packages for fast local dev. A publish guard refuses to publish a
  relaxed (loose-provenance) result; provenance is **contagious upward** — anything
  built above a graft is marked `loose`.
- **Read-only matcher + frontier-cut** (`bits_helpers/cvmfs_reuse.py`,
  `utilities.getPackageList`): a deployed package that matches on
  `(name, arch, build_id)` is kept in the spec set but its subtree is pruned; the
  graft then adopts the deployed hash so the existing reuse path symlinks the
  deployed tree instead of building — no `doBuild` surgery.

### 2. `bits import` — foreign-deployment importer (Stage 2)

`bits_helpers/cvmfs_import.py` (+ thin CLI `cvmfs_import_cmd.py`) turns a non-bits
CVMFS release into a reuse overlay:

- Harvest each deployed modulefile's *resolved* operations (via `modulecmd
  display`), or read a manifest fallback; classify and prefix-factor them into a
  JSON corpus; capture dependency edges structurally.
- Closure-check the set, then stamp it with one deterministic `build_id`.
- Generate a per-`build_id` overlay: `MODULES/<build_id>/<arch>/<name>/<version>`
  build-sufficient modulefiles + hidden module-side `.meta.json` + one
  `.cvmfscatalog` nested catalog per `build_id`. A name-alias map reconciles
  foreign ↔ bits package names on both module ids and dependency edges.
- The generated modulefile is the **single environment artifact** (no synthesized
  `init.sh`): loading it yields a build-sufficient env, identical in mechanism to
  how bits-native packages are consumed.

CLI: `bits import --modulepath <dir> | --manifest <file> [--aliases <map>]
[--label LCG_109] [--out <dir>] [--force]`.

### 3. `--initdotsh-from-modules` build mode (experiment)

- A **hashed** opt-in flag: because it changes recipe build behaviour it folds
  into the package hash (published through the defaults-release env, so it flows
  into every package's hash). Off-state adds nothing, so existing hashes are
  byte-identical.
- When on, `generate_initdotsh` additionally emits the modulefile-equivalent
  dependency env the legacy `init.sh` omits — the package's own
  `<PKG>_INCLUDE_DIR` and its Python `site-packages` on `PYTHONPATH` — generated
  from the package root and guarded on directory existence. The
  defaults/build-config and `build_requires` layer is untouched (bits injects it
  as today).
- **Diagnostic harness** (`tools/initdotsh_modules_diff.py`): for each installed
  package, compares the on-disk `init.sh` environment against the modulefile-
  derived environment and reports, per package and in aggregate, which functional
  variables the modules env adds vs. is missing. Includes a `--dump-raw` mode.

### 4. Fixes and housekeeping

- **`fix(sync)`**: `Boto3RemoteSync` built the tarball name with
  `"...".format(architecture=arch, **spec)`, which raises
  `TypeError: got multiple values for keyword argument 'architecture'` whenever a
  spec carries an `architecture` key (`architecture: shared` packages, or any
  recipe that sets the field). Now passes fields explicitly and uses
  `effective_arch`, consistent with `build_template.sh`'s `$EFFECTIVE_ARCHITECTURE`
  and the other sync backends. Regression test added.
- **`fix(rc)`**: accept flat (header-less) `bits.rc`; restore `search_path` →
  `BITS_PATH` so single-package builds find the recipe path.
- **`ci/docs`**: `README.rst` → `README.md`, root `mkdocs.yml`, `check-readme`
  uses an absolute path; ADR + implementation plan + `REFERENCE.md` updated.

## Backward compatibility & hashing

- Default `strict` + no new flags ⇒ the simple build case is bit-for-bit
  unchanged; the pinned golden-hash test (`test_build.test_hashing`) is untouched
  and still passes.
- Any flag that changes build output (`--initdotsh-from-modules`) is a **hashed
  input** with a byte-identical off-state, so the two modes get distinct,
  reproducible identities (separate artifact trees) rather than colliding.
- New `.meta.json` fields are additive; no existing keys are dropped.

## Testing

- New suites: `test_provenance.py`, `test_cvmfs_reuse.py`, `test_reuse_resolver.py`,
  `test_cvmfs_import.py`, `test_cvmfs_import_cmd.py`, `test_initdotsh_diff.py`,
  plus additions to `test_build.py`, `test_args.py`, `test_sync.py`.
- Full suite: **1,096 passed, 2 skipped.**
- `bits import` verified end-to-end through the real launcher (manifest →
  overlay tree with `build_id`, alias-remapped modulefiles, `.cvmfscatalog`).

> **Not yet validated end-to-end:** a relaxed build against a real CVMFS
> deployment carrying a `build_id` (no such deployment available in CI), and a
> real build under `--initdotsh-from-modules`. Both routes are unit-tested and go
> through proven machinery, but want a build-host run before relying on them.

## Planned / follow-up

- **Relaxed reuse hardening (ADR-0001 Stage 3):** `base_build_id` lineage/ABI-compat
  checks, `abi_tag` verification at graft time, signed-manifest verification,
  `bits q --build-id`, and `bits reproduce` (strict rebuild from the `.meta.json`
  repro spec).
- **Wire the alias map into reuse resolution** so relaxed lookups translate
  bits ↔ foreign names (touches the Stage-1 resolver; held back for a careful,
  full-suite change).
- **`--initdotsh-from-modules`, remaining:**
  - `CMAKE_PREFIX_PATH` is currently left to `CMakeRecipe` (its `;`-separated `-D`
    form would corrupt a `:`-separated env var); unifying it needs a coordinated
    `CMakeRecipe` change.
  - Gate the now-redundant recipe helpers (`bits_pythonpath_from_deps`,
    `CMakeRecipe`'s `CMAKE_PREFIX_PATH` rebuild) on `$BITS_INITDOTSH_FROM_MODULES`
    in `bits-recipe-tools`, so recipes can drop the per-package reconstruction.
    (Editing `bits-recipe-tools` re-hashes it → a one-time full rebuild; to be done
    after a build-host validation confirms the generated env is sufficient.)
  - Longer term: converge `init.sh` and modulefile generation so the modulefile is
    the single source of truth for build *and* runtime env across all packages.

## Notes for reviewers

- The interesting, self-contained logic lives in `bits_helpers/cvmfs_import.py`
  (stdlib-only, fully unit-tested) and `bits_helpers/provenance.py`.
- Hot-path changes are small and guarded: `storeHashes`, `create_provenance_info`,
  the `getPackageList` frontier-cut, and the `generate_initdotsh` opt-in block.
- Suggested review order: `provenance.py` → `cvmfs_reuse.py` + the
  `getPackageList` hook → `cvmfs_import.py` → the `build.py` reuse/`from_modules`
  edits → `sync.py` fix.
