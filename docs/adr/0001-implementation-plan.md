# Implementation Plan — ADR-0001 (Relaxed CVMFS reuse, `build_id`, importer)

Companion to `0001-cvmfs-relaxed-reuse.md`. Sequenced per the ADR's Rollout
section. Each task lists the **anchor** (file:function as it exists today), the
**change**, and the **done-criteria / test**. Guiding rules: the `strict` path
stays byte-for-byte unchanged; every stage is independently shippable and behind
a default-off switch; run `pytest tests/` before each commit.

Verified anchors (current code):
- `create_provenance_info` (`build.py:727`) → `.meta.json` written at
  `build.py:2527`; already records `hash`, `bits_version`, `dist.commit`
  (= `BITS_DIST_HASH`, the recipe-repo commit), and dependency lists.
- `.build-hash` written at `build.py:2361`.
- Reuse decision: `build.py` ~2090–2360 (`remote_hashes`/`local_hashes`,
  `better_tarball`, revision/hash alignment).
- `getPackageList` (`utilities.py:1396`); the `prefer_system` hook +
  `prefer_system_replacement_specs` (`utilities.py` 1552–1607) — the model for
  "this dep is provided externally → prune/substitute".
- `CVMFSRemoteSync` (`sync.py:420`): read-only cvmfs:// store, reads deployed
  `.build-hash`, grafts via dummy tarball.
- Build env sources each dep's `etc/profile.d/init.sh` (`build.py` ~576–630,
  `_dep_init_path`). bits-native deployed packages ship this file.
- `BuildManifest` (`manifest.py:229`: `add_package`, `add_providers`, `_save`).
- Fast module enumeration: `cvmfs_catalog.py` / `bitsModules` (already present).

---

## Stage 0 — `build_id` + provenance metadata (foundation, ~1 small PR)

Goal: every build is labelled and provenance-aware. No behaviour change.

1. **Compute `abi_tag` and `build_id`.**
   - Anchor: new helper `bits_helpers/provenance.py` (`compute_abi_tag(args, defaults_env)`, `compute_build_id(specs, args)`).
   - `abi_tag` = stable hash/string over: compiler id+version, `_GLIBCXX_USE_CXX11_ABI`, `CXXSTD`, OS/glibc, qualified arch.
   - `build_id` = `<label>` (from `defaults` var `build_id:` if set, else `build_family`) + short hash of the sorted manifest `{name, version, ver_rev, hash}` for the whole build set. **Deterministic** (no timestamps/host).
   - Test: `tests/test_provenance.py` — determinism (same inputs → same id), arch/flag sensitivity.

2. **Stamp it into `.meta.json` and the manifest.**
   - Anchor: `create_provenance_info` (`build.py:727`) — add `build_id`, `abi_tag`, `reuse_policy` ("strict"|"relaxed"), `provenance` ("pure"|"loose"), and a `repro` block `{dist_commit, recipe_tools_version, defaults}` (dist_commit already present). For Stage 0 `reuse_policy="strict"`, `provenance="pure"` always.
   - Anchor: `BuildManifest.add_package` (`manifest.py:330`) — carry `build_id`/`abi_tag`.
   - Test: extend `tests/test_build_stats.py`/a new `tests/test_provenance.py` to assert fields land in the written JSON.

3. **Surface `build_id` in the modulefile — hash-neutrally.**
   *(DEFERRED to Stage 1/2.* There is no post-build modulefile step in
   `build.py` today, so this would mean a new install-tree-mutation step on every
   build — riskier than warranted while the resolver reads `build_id` from
   `.meta.json`, not the modulefile. Do it when generating overlay modulefiles,
   where modulefile content is already being written.)*
   - **Do NOT modify `bits-recipe-tools`/`MakeModule`**: that re-hashes every
     package (bits-recipe-tools is a universal `build_requires`) and would force
     a full rebuild for the simple aliBuild case — forbidden by the backward-
     compatibility constraint below.
   - Anchor instead: bits' own module finalization in `build.py` (the step that
     writes/relocates `etc/modulefiles/<pkg>` after `MakeModule`). Append
     `module-whatis "build_id: <id>"` / `"abi_tag: <tag>"` there, **only when a
     build_id is set**. This touches `build.py` (not hashed) and leaves the
     recipe, `MODULE_OPTIONS`, and `bits-recipe-tools` untouched → no rehash,
     no rebuild, aliBuild unaffected.
   - Test: build a trivial package; assert the whatis lines appear when build_id
     is set and are absent (and the package hash is unchanged vs. baseline) when
     it is not.

Done when: a normal build writes `build_id`/`abi_tag`/provenance to `.meta.json`
and the modulefile, deterministically, with the full suite green.

---

## Stage 1 — relaxed reuse of *bits-native* releases (~the core PR)

Goal: `bits build --reuse-policy relaxed --reuse-base <build_id>` grafts a
bits-published release and builds only the top. No importer yet.

1. **CLI + defaults plumbing.**
   - Anchor: `args.py` build parser — add `--reuse-policy {strict,relaxed}`
     (default `strict`), `--reuse-base <build_id>`, `--build-local <pkg,...>`.
   - Defaults: read `reuse_policy:` / `reuse_base:` from `defaults` (the
     `_read_bits_rc`/defaults plumbing already merges these).
   - Publish-guard: `bits publish` / write-store path refuses `provenance=="loose"`.

2. **Frontier-cut at resolution time.**
   - Anchor: `getPackageList` (`utilities.py:1396`), alongside the
     `prefer_system` block (1552–1607). Add a parallel "prefer_cvmfs" pass:
     for each spec, if policy is relaxed and a deployed package matches
     (name via alias map [Stage 2; identity map for native], qualified-arch,
     `--reuse-base` build_id) and the package is not in `--build-local`, mark it
     `from_cvmfs` and **prune its subtree** (same mechanism as system packages:
     they're removed from the build closure and their `requires` not followed).
   - Reuse `prefer_system_replacement_specs` semantics to substitute the spec
     with the deployed one (path + env source = the deployed tree).
   - Test: `tests/test_reuse_policy.py` — graph with a base + top; assert the
     base subtree is pruned and the top remains, under relaxed; assert nothing
     pruned under strict; assert `--build-local X` keeps X.

3. **Zero-copy graft (replace dummy-tarball round-trip).**
   - Anchor: `CVMFSRemoteSync.fetch_symlinks` (`sync.py:450`) + the reuse
     decision (`build.py` ~2090–2360) + the dormant `islink && isdir`
     short-circuit (referenced in `reference_cvmfs_reuse` notes).
   - For a `from_cvmfs` package: symlink `sw/<arch>/<pkg>/<ver_rev>` →
     `/cvmfs/.../<pkg>/<ver>` and `MODULES/<build_id>/<arch>/<pkg>` →
     deployed modulefile; skip build/unpack entirely. Guard all writes with
     `[ -w ]` (already the pattern).
   - Build env: the top's `_dep_init_path` (`build.py:630`) sources the grafted
     dep's `/cvmfs/.../etc/profile.d/init.sh` — **already build-sufficient for
     bits-native deps** (they ship init.sh). No new env work in Stage 1.
   - Test: integration-style test with a faked `/cvmfs` tree (init.sh present);
     assert the consumer build env resolves the dep paths into the fake cvmfs
     and no tarball is unpacked.

4. **Contagious provenance.**
   - Anchor: after the frontier-cut, any spec whose closure contains a
     `from_cvmfs` node (under relaxed, where the deployed hash ≠ recomputed
     hash) is `provenance="loose"`. Propagate up in the dependency walk and
     write to `.meta.json` (Stage 0 field).
   - Test: assert a top built on a relaxed base is `loose`; a sibling built
     purely locally on a `pure` base stays `pure`.

Done when: `--reuse-policy relaxed --reuse-base <id>` against a bits-published
release builds only the top, zero-copy, marks it `loose`, and `bits publish`
rejects it; strict path unchanged; suite green.

---

## Stage 2 — importer for foreign (LCG) deployments (~the big PR, standalone tool first)

Goal: turn a non-bits CVMFS release into a Stage-1-consumable overlay.

> **As-built note (Stage 2 done).** The command is **`bits import`** (not
> `cvmfs-import` — that read like a CernVM-FS-suite tool). There is **no
> synthesised `init.sh`**: the generated modulefile is made *build-sufficient*
> (it carries `CMAKE_PREFIX_PATH`/`PKG_CONFIG_PATH`/`CPATH`/`<Pkg>_ROOT`, the
> path ones guarded on the deployed tree), so a grafted dep's environment is set
> up by **loading its modulefile** (`module load` / `bits printenv`) — the same
> single mechanism for imported and bits-native packages. Logic lives in
> `bits_helpers/cvmfs_import.py` (stdlib-only) with the CLI in
> `bits_helpers/cvmfs_import_cmd.py`.

1. **Harvest → corpus.**
   - New subcommand `bits import` (`bits_helpers/cvmfs_import_cmd.py`).
   - Input modes: (a) modulefiles present → `MODULEPATH=<lcg> modulecmd sh
     display <pkg>/<ver>` per package, parse the *resolved* ops; (b)
     manifest-only → env from manifest paths + matched recipe template (fallback).
   - Emit one JSON **corpus**: per package `{version, base_prefix, cvmfs_path,
     env:{generic_options,[verbatim]}, deps:[…]}`; `module load`→structured
     `deps` edges; prefix factored out.
   - Test: fixture LCG modulefiles → assert classification (generic vs verbatim)
     and deps extraction.

2. **Closure-check + `build_id`.**
   - Verify every `deps` edge target is in the corpus (refuse on dangling).
   - Assign deterministic `build_id` (Stage 0 algorithm over the corpus).
   - Test: dangling-edge corpus → refused; complete corpus → stable id.

3. **Generate overlay (D6/D10).**
   - Per package, emit into `MODULES/<build_id>/<arch>/<pkg>/<ver>`: a
     **build-sufficient** bits modulefile (harvested env ops re-targeted to the
     deployed path + remapped deps as `prereq` + `module-whatis build_id` + the
     build hooks `CMAKE_PREFIX_PATH`/`PKG_CONFIG_PATH`/`CPATH`/`<Pkg>_ROOT`,
     guarded on the tree), plus a hidden module-side `.meta.json`. **No separate
     `init.sh`** — the modulefile is the single env artifact.
   - Validate generated paths exist in the CVMFS tree; per-package overrides for
     the awkward ones (ROOT/Geant4 data, etc.).
   - Drop a `.cvmfscatalog` per `build_id` dir (D9) → fast catalog enumeration.
   - Test: generated modulefile carries the build hooks (`CMAKE_PREFIX_PATH`,
     `*_ROOT`, guarded `PKG_CONFIG_PATH`/`CPATH`).

4. **Name-alias map (D8).**
   - `alias.yaml`: lcg.bits-name ↔ foreign-name; bootstrap from corpus+recipe
     names, hand-fix stragglers; consumed by the Stage-1 frontier-cut matcher.
   - Report unmatched packages.

5. **Wire the overlay into Stage 1.**
   - The Stage-1 resolver reads module-side `.meta.json`/`build_id` from the
     overlay (D6 out-of-tree read), uses the alias map for name matching, and
     grafts as in Stage 1 — the grafted dep's build env now comes from loading
     the *generated* build-sufficient modulefile (via `bits printenv`/module),
     not an `init.sh`. End-to-end test: import a fixture release → relaxed-build
     a top package against it.

Done when: `bits import <release>` produces a loadable, catalog-backed,
build-sufficient overlay that Stage 1 consumes to build a top package without
rebuilding the base.

---

## Stage 3 — hardening (post-MVP)

- `base_build_id` lineage field + `abi_tag` compatibility check for cross-build
  "work together" (owner-attested multi-reference, D10).
- Verify the `build_id` manifest signature (cvmfs-prepub transparency log)
  before adoption.
- `bits q --build-id <id>` filter and `bits avail` grouping by release.
- `build_id`-as-reproducibility: `bits reproduce <build_id>` that checks out the
  recorded `repro` inputs and rebuilds strict to confirm hash parity.

---

## Backward compatibility (HARD CONSTRAINT — gates every PR)

The simple case (a plain `bits build <pkg>` / aliBuild with a local alidist, no
CVMFS, no `--reuse-policy`) must be **bit-for-bit unaffected**: identical package
hashes, identical build graph, no forced rebuild, no schema break.

Invariants every task must hold:

1. **No rehash.** Nothing in the hashed path changes — recipes, `MODULE_OPTIONS`,
   and `bits-recipe-tools` are untouched. New metadata is produced by `build.py`
   / helpers (not hashed). (This is why Stage 0.3 moved out of `MakeModule`.)
2. **Defaults preserve today's behaviour.** `--reuse-policy` defaults `strict`;
   `--reuse-base`/`--build-local` absent ⇒ exact-hash reuse exactly as now.
3. **Metadata is additive and defensively read.** New `.meta.json` / manifest /
   corpus keys are optional; every reader tolerates their absence (the existing
   `fetch_symlinks` `jq .package.hash` keeps working). New provenance inputs use
   `os.environ.get(...)`/`getattr(...)` with fallbacks — never a new hard-required
   env var (contrast the existing `os.environ["BITS_DIST_HASH"]`, which we must
   not add more of).
4. **`build_id` is harmless when unused.** It is computed defensively even for a
   minimal aliBuild build (label from `build_family`), written only as additive
   `.meta.json`/whatis metadata; absence of a `defaults build_id:` never errors.
5. **No new runtime deps for the simple path.** The importer (`modulecmd`, `jq`,
   alias map) is a separate tool; a normal build never invokes it.

**Regression guard (add first, keep green):** `tests/test_backward_compat.py`
builds a representative package twice — once on `main`-equivalent behaviour and
once with the new code, both with no new flags — and asserts identical
`spec["hash"]`, identical resolved build graph, and that `.meta.json` differs
only by *added* keys. CI fails if the simple path's hash moves.

## Cross-cutting

- **Tests:** new `tests/test_provenance.py`, `tests/test_reuse_policy.py`,
  `tests/test_cvmfs_import.py`; extend `tests/test_sync.py` for zero-copy graft.
- **Docs:** `REFERENCE.md` §21/§26 — `--reuse-policy`, `build_id`, the importer,
  the overlay layout and nested-catalog requirement.
- **Defaults:** `reuse_policy:` and `reuse_base:` in `defaults-*.sh`.
- **Backwards-compat:** Stage 0 adds fields (additive); Stages 1–2 are behind
  `--reuse-policy relaxed` (default strict) → zero impact on existing builds.

## Risk register (from the ADR review)

1. **Build-sufficient env** — bits-native deps already ship `init.sh` (Stage 1
   safe); the risk is concentrated in Stage 2's generated build-sufficient
   *modulefile* (loaded via `bits printenv`/module — no init.sh). Mitigate
   with the validation in Stage 2.3.
2. **Strict cross-host hash reproducibility** — the `repro` block (Stage 0.2)
   records the inputs; `bits reproduce` (Stage 3) verifies. If parity fails in
   practice, that's a blocker for the *publish* reuse path too and must be
   chased independently.
3. **`build_id` stamped on an incoherent set** — closure-check (Stage 2.2);
   determinism (Stage 0.1).
4. **Overlay collisions** — namespacing by `build_id` (Stage 2.3 layout).
5. **Forced rebuild of the simple/aliBuild path** — *retired by design*: the
   whatis lines are injected in `build.py` (hash-neutral), `bits-recipe-tools`
   stays untouched, and the backward-compat regression guard fails CI if any
   simple-path hash moves.
