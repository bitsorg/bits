Covers `bits`, `lcg.bits` (recipes), and `bits-recipe-tools`. Entries tagged **[Feature]**, **[Fix]**, **[Improvement]**.

---
# Recent changes
	
## init.sh from-modules — build env derived from dependency modulefiles (now the default)
- **[Feature]** `34a672c` `--initdotsh-from-modules` as a **hashed** build input (foundation; off-state byte-identical).
- **[Feature]** `77a6103` from-modules adds the modulefile-equivalent dev env (`<PKG>_INCLUDE_DIR`, Python site-packages) to init.sh.
- **[Feature]** `e4e713d` make `--initdotsh-from-modules` the **default**; add `--legacy-initdotsh` (aliBuild stays legacy, hashes byte-identical).
- **[Feature]** `e2a6169` from-modules also exports `CMAKE_PREFIX_PATH` (`:`-separated, read natively by `find_package`).
- **[Improvement]** bits-recipe-tools `e429a87` gate `CMakeRecipe`/`BitsPython` env reconstruction off under from-modules (this not landing in the built v0.0.28 briefly broke CMP0144-old packages; fixed by v0.0.29).
- **[Improvement]** lcg.bits `d967f93` drop redundant dependency-env reconstruction; `cd609fc` drop redundant `-DCMAKE_PREFIX_PATH`; `93f997c` `torch_scatter`/`torch_sparse` drop manual PYTHONPATH loop.
- **[Improvement]** `768bc44` / `900ad31` / `607013f` env-diff harness comparing init.sh-derived vs modulefile-derived build env.

## --builders scheduler & resource management
- **[Feature]** `6d64434` unleash the final (sink) package to full `-j` (default on; memory cap still applies).
- **[Feature]** `6dd1e4e` history-driven **critical-path scheduling** (default on; `--no-critical-path-schedule`).
- **[Fix]** `dc2fc07` macOS available-memory was under-reported (subtracted reclaimable inactive-anon), throttling heavy builds (ROOT to `-j2` on 24 GB); prefer psutil, else reclaimable `vm_stat` buckets.
- **[Fix]** `70d3fca` failure logs → `LOGS/<arch>/` and `9e3d9cd` per-arch `bits_build_stats.json` — stop different platforms in one work area from clobbering each other; `b3a1e46`/`170ff5a` `bits stats` reads the relocated file + docs.
- **[Fix]** `882ad22` resolve `%(version)s` in the build-order banner.
- **[Improvement]** `3162234` use `threading.current_thread()` (clear deprecation warnings).

## Repository providers & init workflow (aliBuild vs native bits)
- **[Feature]** `2545a0c` aliBuild front-end defaults to the legacy path, native bits to the provider path.
- **[Feature]** `847e8db` `aliBuild init` (no PACKAGE) checks out the recipes (alidist) and exits; `d3bc83e` `bits init <group>.bits` checks out a recipe repo from the registry; `55fbd89` develop a package that lives in a required provider repo.
- **[Fix]** `73a145e` load the bootstrap repo's required providers (e.g. alice.bits → alidist.bits — fixed "gsl not found").
- **[Improvement]** `621dded` warn on provider version conflict; `4d92e95` point the "package not found" error at the provider mechanism; `ee78182` docs.

## CVMFS layout, merged views & relaxed reuse
- **[Feature]** `0632f28` / `b70abac` / `b1dfc1d` / `e8d1222` / `ea04075` merged symlink-farm view: one-entry-per-var env, opt-in `enter/setenv --view`, view-aware `load`/`printenv` + age-based GC, path remap (fixes PyROOT).
- **[Feature]** `58f13b6` / `c2d99be` / `f59a547` / `53e91d4` / `80b6a08` published per-`build_id` views on CVMFS, `bits publish --view`, per-tree pre-publish primitive, CVMFS layout recorded in `.meta.json`.
- **[Feature]** `8f193fa`→`c52f5d7`, `cf19966` ADR-0001 import pipeline: modulefile harvest → classify → closure/`build_id` → overlay → `bits import` (build-sufficient from modulefiles).
- **[Fix]** `fac7aef` review fixes (command injection, partial-view, republish, path traversal, build_id match).
- **[Improvement]** `44a7ee1` docs for `--reuse-policy`/`--reuse-base`/`--build-local`.

## Sync / remote store
- **[Fix]** `28c6989` upload freshly-built packages to `--write-store` (S3) when reading from a CVMFS remote (cross-backend `DualRemoteSync`; was silently dropped).
- **[Fix]** `8c21990` `--aggressive-cleanup` dropped the tarball the `--write-store` upload still needed.
- **[Fix]** `15f82e4` Boto3 tarball-name crash on specs carrying an `architecture` key.

## Recipe hashing
- **[Feature]** `5f4a15e` `untracked_requires` — link a dependency without folding it into the consumer's hash (edit a dep without rebuilding the stack above); `20e8ed6` cookbook example.

## CLI robustness & bits.rc
- **[Fix]** `fec3303` never exit non-zero without a message (silent-exit safety net for malformed defaults/recipes).
- **[Fix]** `f53c6ee` / `0e7abd5` restore `search_path` for single-package builds; `5dd6b22` use `python3`.
- **[Improvement]** `a7d0a2f` accept flat (header-less) `bits.rc`; `a7afc92` CI README-path fix.

## lcg.bits recipes (other) & bits-recipe-tools
- **[Fix]** `6f30d4f` ROOT: use external bits zstd (`-Dbuiltin_zstd=OFF`) so ROOT 6.40 finds `zdict.h`.
- **[Improvement]** `a11558a` reduce ROOT `mem_per_job` to 1250; `05a4eef` bump bits-recipe-tools version.
- **[Improvement]** bits-recipe-tools `a441d2b` `ModuleRecipe` guards `lib`/`lib64`/`pkgconfig`/`site-packages` path entries on existence.

# Summary of older changes

## Defaults & conditional configuration
- **[Feature]** Conditional variables and architecture-gated overrides: `pkg:matcher` requires/patches, version-gating (including on the *depending* package's own version), `(?VAR)` variable-conditional requires, `&&`/`||` matchers, `--flavour` build-wide variables, and defaults-profile variables expanded in recipe bodies.
- **[Improvement]** Defaults-chain hardening: deep-merge across `a::b::c`, `release` as the implicit base, `old::new` overrides, YAML `include` sharing, str→list normalisation, flat `name = value` overrides, and a non-hashed `system:` block for build-host policy.

## --builders: parallel scheduling & resource management
- **[Feature]** Bounded parallel builds under `--builders` with builder-aware `$JOBS` division; `--build-nice` priority ladder + straggler-renice watchdog (incl. inside Docker); overlap source downloads with compilation; `--auto-resources` measurement-driven scheduling; per-package resource monitoring + the `bits stats` report.
- **[Fix]** First macOS available-RAM under-report fix (throttled `-j`); memory cap + CPU-oversubscribe per-builder share.

## Patches & source handling
- **[Feature]** Auto-apply `patches:` after checkout; re-extract when the patch set changes; `%(name)s`/`%(version)s` substitution in source URLs; opt-out for automatic patching; local sources from a package repo dir.
- **[Fix]** Many extraction-correctness fixes (strip-components off-by-one, single-file archives, `--batch` to stop interactive reversal, stale sentinel, dirty-tree re-extraction).

## Repository providers, manifests & CVMFS layout/publish
- **[Feature]** Repository-provider mechanism + source-checksum features; bootstrap `alidist` by default when no recipe repo is given; templated CVMFS layout (`cvmfs_dir`/`install_dir`/`module_dir`); pipeline template-mode relocation publish; relocate text files carrying hard-coded build paths.
- **[Improvement]** Manifest schema v3 records patches, resolved variables, and the effective architecture per package.

## macOS support
- **[Feature]** `--brew` / `bits brew` (generate a Homebrew Brewfile from recipes); macOS sandbox (SBPL) profile + `sandbox_network` default; doctor Xcode-CLT/XQuartz checks; emit `DYLD_LIBRARY_PATH` on macOS (reconstructed from deps) and `LD_LIBRARY_PATH` on Linux (not both).

## Module listing, robustness & misc
- **[Feature]** `bits q` fast CVMFS module listing via the serving catalog; RECC support; build hooks; exclude recipe comments/blank lines from the build hash.
- **[Fix]** Broad robustness: clean error messages instead of raw crashes, propagate the real recipe exit code (no masking to 1), never hang at end-of-run on a raised exception, surface cmake find-failure detail, concise `--builders` failure summary; Python 3.8–3.14 compatibility.

## bits-recipe-tools — shared recipe framework (built out May–Jun 2026)
- **[Feature]** New helper hierarchy: `CMakeRecipe`, `PythonRecipe`/`PythonPipRecipe`, `MesonRecipe`, `AutoToolsRecipe`, `MakeRecipe`, `BinaryRecipe`, `MetaRecipe`, `ModuleRecipe`, `PreloadRecipe`, `HomebrewRecipe`; shared helpers `BitsArch`, `BitsPython`, `BitsMacOS`, `BitsPatch`; `SetBuildEnv`/`CMAKE_PREFIX_PATH` construction; per-target DYLD/LD handling.

## lcg.bits — recipe stack (628 commits)
- **[Feature]/[Improvement]** Build-out and maintenance of the LCG/Key4hep stack in bits-native form: hundreds of package recipes added and iterated, with recurring gcc15 / Python-3.13 / macOS build fixes, converging onto the bits-recipe-tools helpers and the from-modules build env.


	_Omitted: merge / auto-PR-fix commits (`0f93f16`, `c346ecc`, `90bfad4`, `38b0279`, `40239a9`)._
