Covers `bits`, `lcg.bits` (recipes), and `bits-recipe-tools`. Entries tagged **[Feature]**, **[Fix]**, **[Improvement]**.

---
# Unreleased

- **[Improvement]** A recipe repository's `cvmfs.yaml` is read with the first `--defaults` profile taken from that repository, beneath that profile's keys, and never overrides a more specific repository's file (the group's, the repository of the first profile after `release`, then `-c` and `BITS_PATH`): `--defaults atlas::gcc15` publishes with atlas.bits' layout, also from a stacks.bits branch that still has its layout in its defaults. A modules template `bits q` cannot use (e.g. with `{install_dir}`) no longer prints a note.
- **[Feature]** A defaults profile with `expand_recipe: true` expands every recipe body strictly (an unknown `%(name)s` is an error); `bits build` and `bits status` both honour it (from #125).
- **[Fix]** The `Python` recipe itself gets the `python_*` variables (its body still expanded softly); non-string recipe variables no longer crash (a float warns: quote versions such as `"3.10"`); `relocate-me.sh` no longer rewrites an install base that contains the build directory twice (from #125).
- **[Fix]** `bits cvmfs publish`: the release view adds only the links that are not published yet, instead of resending all of them (a shared root fails on existing ones).
- **[Feature]** `bits cvmfs publish --replace-on-conflict` replaces only content another build published: a package whose published hash differs, or a merged view whose fingerprint (now in its `.meta.json`) differs, is sent with `replace` and prepub deletes the old subtree first. Needs cvmfs-prepub with `replace_on_conflict`, checked before uploading. Modulefiles, release views and aliases are never replaced, and the package identity is now always sent (no more UNIQUE failures on existing modulefiles).
- **[Improvement]** The merged view's `setup.sh`/`setup.csh` put the view's man pages on `MANPATH` when it is set (unset, `man` already finds them from `PATH`); `share/man` stays a real directory in the view.
- **[Improvement]** `bits cvmfs publish`: with a packages and a modules template, modulefiles are no longer a commit each: the package carries its modulefile, and one job at the end links `<modules dir>/<pkg>/<ver-rev>` to it for every package that lacks one (about a quarter of a large publish).
- **[Improvement]** The tarball's sha256 is computed while it is packed (a `<tarball>.sha256` sidecar), so the build manifest and the store upload no longer read the tarball again; a legacy store object over 5 GB is now stamped with its sha256 (multipart copy) instead of being re-hashed on every publish.
- **[Improvement]** Faster build manifests (SBOMs): the tarball's sha256 is computed while it is packed (a `<tarball>.sha256` sidecar), so the manifest and the store upload do not read it again; a store object stamped with its sha256 by a server-side managed copy, which also works above 5 GB, so large legacy objects are no longer re-hashed on every publish.

# 0.6 — 2026-10-04

Merged into `main` as one squashed commit, `2f15ed8` (#122). The commit ids below are from the `consolidation` branch history.

## Since #122
- **[Feature]** `bits cvmfs publish --object-list --prewarm` (ingest with `--direct-s3`): prepub announces the stored objects so Stratum 1s pull them right after each commit.
- **[Improvement]** `38b02da` require Python 3.8.
- **[Fix]** `730574a` resource stats lookup is case-insensitive; overrides from always-on providers are applied.
- **[Fix]** `703fe79` `cvmfs-publish` skips non-redistributable binaries; `--dist BRANCH` honoured; `verify` uses each package's architecture; store upload accepts https stores.
- **[Fix]** `78689b6` console-backend: PyJWT 2.15.1 (security fixes).
- **[Improvement]** `d2b7497`, `004deb2` USERGUIDE and COOKBOOK checked against the code; `bits use` fixes; `cbd95e0` bits-console links point to GitLab.

## Relocation-independent packages
- **[Feature]** `9828005` / `c3e1cd5` emit relocation-independent pkg-config and CMake config files (`${pcfiledir}` / `${CMAKE_CURRENT_LIST_DIR}` anchors).
- **[Improvement]** `ffe8a31` extract relativization into a shared, portable helper (`relativize-configs.sh`); `db787e5` also relativize `bin/*-config` scripts.
- **[Fix]** `072188c` relativize after POST_INSTALL, just before packing — hooks were re-baking the absolute prefix, so every store tarball shipped absolute configs.
- **[Fix]** `377f619` / `e0f0147` put dependency include dirs on `CPATH` and lib dirs on `LIBRARY_PATH` (bare `-lfoo` links, e.g. Go/cgo in myschedd).
- **[Fix]** `ec55739` stop exporting dependency include dirs on `CPATH` again (it acted like `-I` and shadowed CMake's `-isystem` choice, e.g. protobuf in TritonCore); `LIBRARY_PATH` stays.
- **[Fix]** `e7024bf` write `.bits-relocate` after hooks and relativization, so files written by POST_INSTALL hooks are relocated too.

## Shared toolchains (own_hash)
- **[Feature]** `3e29ccb` / `b9ca24c` `own_hash`: defaults-independent identity for shared toolchains (+ container-fingerprint fold) — GCC built once, reused across build types.
- **[Feature]** `cb92544` build-type-neutral store arch for `own_hash` packages; `78b25d5` source the neutral-arch toolchain in init.sh and bridge the build arch on reuse.

## Store, signed reuse & certification
- **[Feature]** `23a9350` deterministic package tarballs (sorted members, zeroed owner, fixed mtime, pinned compressor); `3f84e76` require gnu-tar on macOS.
- **[Feature]** `545e3ba` / `1132894` / `781ec16` sign manifests via a signer (local or security proxy); `ad2b249` certify via the console-backend; `64eb7b8` TLS options for the signing service.
- **[Fix]** `834d70e` derive signed-reuse trust from the store's manifest listing, not guessed arch names (builds were rebuilding everything).
- **[Fix]** `63cce8e` rebuild on signed-reuse hash mismatch instead of aborting; `d7a10d5` never fail a reused package on a write-store HEAD.
- **[Fix]** `edc3f61` verify the write store before skipping upload; log release + bits branch at start.
- **[Fix]** `92ca5fb` keep virtual `defaults-release` out of store upload, reuse and CVMFS publish.
- **[Fix]** `32c3512` a `--write-store` alone is also the read store (it was silently dropped, so nothing was uploaded); `72c8ff6` accept CERN S3 path-style URLs (`https://s3.cern.ch/<bucket>`) and quiet absent guessed manifests.
- **[Improvement]** `9089e40` / `a1ee6ba` one-line signed-reuse and derived-trust summaries.
- **[Fix]** security review: `b1e2869` fail-closed `key-policy.json` (M4); `3aed61e` gate store upload on redistributable tag (H1); `51d180d` commit-SHA pin for provider recipes (M2); `910da44` don't crash on a re-certification conflict; `24fb9ea` resolve `%(name)s` in restricted source URLs for compliance purge.

## Console backend & signing approval
- **[Feature]** `ab11aeb`→`824bffa` console-backend service: community-admin authz, gated sign endpoint, CI ID-token signing, WebAuthn enrolment + digest-bound approval, approver PWA, cross-device (QR) CLI approval.
- **[Feature]** `d023db6`→`b2a98ba` passkey-only CLI approve, per-request nonce, multiple origins under one rp_id; `004091d` / `2427040` / `59e4b42` build/publish pre-approval.
- **[Feature]** `de0beb2`→`69c9706` OIDC login + backend session (24 h TTL); `971724d` / `4c0c946` / `1a228dd` ops hub: trigger, cancel, retry, delete pipelines and admin writes via backend token; `95b1ab3` admin policy from the `bits-admins` group tree; `6e1b56e` cached GitHub read proxy.
- **[Fix]** `705d97f` / `aba0dd7` / `1e149a6` QR signing and manifest-signing auth fixes; `bb0a509` CA certificates.
- **[Feature]** `afdddca` / `51351b6` / `3885b89` / `03ffaee` CLI build pre-approval: passkey (cross-device) approval bound to the build's packages and the MR author; deterministic build ids accepted as keys; `7c73d8c` read-only pre-approval lookup for the merge gate.
- **[Fix]** `35f23d3` serve pipeline variables for Re-run/Re-publish with the ops token; `0cb1a13` read the sign-proxy port and token from its agent socket (they rotate); `5819e62` list admin subgroups with `all_available`.

## CLI surface & configuration
- **[Change]** Python 3.8 is the minimum (`requires-python >=3.8`; `bits doctor` checks it); 3.7 is no longer supported or tested.
- **[Change]** `e7f09cb`→`3ce3f49` remove `--makeflow`/`--pipeline` (use `--builders`/`--parallel`); `025f0c2` retire usage analytics.
- **[Change]** `c244e21` `--remote-store` (`--store` deprecated); `8ab8b19` `--parallel` (`--builders` alias); `daf30b0` `--prefer-system`/`--force-overwrite`; `2460a81` `--search-path`; `43ed396` `--set` alias for `--flavour`.
- **[Change]** `2e4437b` / `f8eae7f` / `37a7f26` / `ba4bd0d` retire `bits.rc` in favour of trust-gated `bits use` profiles (`.bitsuse` local or `~/.bits/use`).
- **[Change]** `d346c5c` `bits publish` is CVMFS-only; single-package S3 writes move to `bits store upload`; `66d6d06` `--release-view` (`--view` deprecated); `802d6f2` / `fd49757` fold cvmfs-stage/publish into `bits cvmfs` and gc/store-stats into `bits store`; `f8d4f0b` `bits prune` (`cleanup` kept as deprecated alias).
- **[Improvement]** `acf491d` / `e2ab029` robust arch autodetect with multiple installed trees.
- **[Change]** `632aa0d` / `e425ff7` `bits certify` uploads what is missing, gets a passkey approval via bits-console and opens the MR; the old certify is `bits sign`; `bits publish` no longer opens MRs (`--approve` for pre-approval).
- **[Feature]** `23ca133` `bits build --dry-run` prints a per-package reuse plan (installed / local / remote / build); `bits status --check-store` uses the same listing.
- **[Feature]** `e55a85d` `bits doctor` without packages checks the machine (Python modules, git/compiler, docker or rootless podman, disk, store access); `9054398` `bits deps --outmake` Makefile export.
- **[Fix]** `faced22` / `bf7f2f8` rootless podman: `--userns=keep-id`, no SELinux relabel of bind mounts, skip cgroup limits the user cannot apply; `01c126b` start the monitor for sequential builds too and make push failures visible.

## Recipes, layout & views
- **[Feature]** `d735cd6` / `9cec1ce` / `d7b6924` `bits overlay lcg` — LCG release view over a built closure (`bits lcg-view` deprecated).
- **[Feature]** `1978efd` `version_from: <var>` versions a source-less package from a build variable; `e331303` `view: true` recipe flag for auto path-collapse; `0c4e712` `{day}` nightly CVMFS path token.
- **[Fix]** `c6e2b47` split recipe front-matter on a bare `---` line; `e7017eb` honour per-patch `strip=N`; `4169e83` relaxed overlay match must not substitute a different version; `7c3b535` name the modulefile after the version.
- **[Fix]** `acb90ce` / `7170f38` resolve defaults after provider discovery, iterated to a fixed point; `a2fc11a` use a locally checked-out provider instead of cloning.
- **[Improvement]** `2f3f770` shared-arch sentinel `shared`→`share`; `bf9d557` / `ef8fbb1` Brewfile from a recipe scan, recorded in `sw/<arch>`; `ad34eb8` / `25d148d` docker builder image derived from arch + registry.
- **[Improvement]** `84e2f22` provenance lists recursive deps in build order; `3878386` BOM records `source_pipeline_id`; `b3d5319` cvmfs-publish fails loud on post-relocate writes outside the package.
- **[Fix]** `b234b54` a missing defaults file fails with the searched paths; warn when a defaults repo's recipe is hidden by an earlier one; recipe `system: sandbox_network` honoured; noarch (`share`) packages publish with the shared template.
- **[Fix]** `2632ef6` `bits overlay lcg` finds packages installed under a family directory (e.g. MCGenerators were missing from `LCG_externals`) and skips `<pkg>/latest` links.
- **[Fix]** `b48eb8c` `bits cvmfs-path` accepts `--set` like `bits build`; `da1ea80` git errors outside a checkout no longer become the `{release}` segment; `04ced6e` fill `{release}` in the `--reuse-from cvmfs` modules path.

## CVMFS publish: packages once, releases as views
- **[Feature]** `507fdef` place the whole closure with the publishing build's templates (recorded in the manifest as `cvmfs_templates`); `{arch}` token; `cvmfs_repository` swaps only the repository.
- **[Feature]** `324fc1a` `cvmfs_packages_template`: each package published once per build arch and skipped when the same build hash is already there; `--release-view` publishes a release as relative symlinks, plus `BASE/1.0` and package aliases so modulefiles resolve.
- **[Feature]** `c3f08d4` LCG-style merged view per release and arch (`cvmfs_views_template`, `bin/ lib/ include/ …` + a self-locating `setup.sh`), shaped per recipe with `view:` (not hashed); `55a9a2d` link a directory whole when one package fills it.
- **[Fix]** `3dfbc6b` no `.cvmfscatalog` in the merged-view tar (ingest adds it; a second one failed the job).

## SBOM, manifests & checksums
- **[Feature]** `638bb01` manifest v4: dependency edges (incl. system-provided deps), `system_packages`, source and tag per package.
- **[Feature]** `6337c84` `bits sbom`: deterministic CycloneDX 1.6 and SPDX 2.3 export of a build manifest (credentials stripped from URLs); `75f2007` publish uploads both next to the release NOTICE.
- **[Feature]** `fe10ab6` `bits checksums`: hash every tarball and patch of a recipe repository, pin git tags (branches reported as moving), and with `--defaults` record what profile overrides add in the profile repo; `--write` never overwrites.
- **[Fix]** `460cf30` checksum files load after overrides, profile repositories' checksums merge over the recipe repo's, commit pins are keyed by tag (a legacy `tag:` pin applies only to the recipe's own tag); `--write-checksums` finds cached downloads and recipe patches and merges instead of overwriting; `f9cff23` it writes override entries to the profile that set them.

## Internal restructuring
- **[Improvement]** `54a50d9`→`891d51f` remove dead code, the legacy spool publish path and single-admin forge API; split `utilities` into `arch` / `matchers` / `paths` / `recipe` / `packages` / `defaults`; `RemoteSync` base class; one shared Prometheus push.
- **[Improvement]** `f8fa03b`, `af1a6c2`→`d84dd18` per-command argument registrars; `0f38c62`→`423107f` `BuildConfig` snapshot of resolved knobs; `7247ee8` extract `build_one_package`.
- **[Improvement]** `64537bb` aliBuild minimal-wrapper compat harness.
- **[Fix]** `1a7d73c` stub the S3 probe in the enforce re-certification test (it failed without boto3).

# Earlier changes
	
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
- **[Feature]** `--reuse-from <modules-path>|cvmfs` reuses deployed components via their published modulefiles/`init.sh` (sourced in place from `/cvmfs`); `--reuse-policy strict|relaxed`, `<src>::policy` sugar, per-dependency reuse, corrected pkg-config prefixes, and `cvmfs`-resolution from the `cvmfs_modules_template`.
- **[Change]** Removed the legacy `cvmfs://` `--remote-store` reuse: the `build_id` graft (`--reuse-base`/`--reuse-cvmfs`, `CVMFSRemoteSync`, `select_build_id`/`graftable_match`) is retired in favour of `--reuse-from`; a `cvmfs://` `--remote-store` now errors and points at `--reuse-from`.
- **[Feature]** `0632f28` / `b70abac` / `b1dfc1d` / `e8d1222` / `ea04075` merged symlink-farm view: one-entry-per-var env, opt-in `enter/setenv --view`, view-aware `load`/`printenv` + age-based GC, path remap (fixes PyROOT).
- **[Feature]** `58f13b6` / `c2d99be` / `f59a547` / `53e91d4` / `80b6a08` published per-`build_id` views on CVMFS, `bits publish --view`, per-tree pre-publish primitive, CVMFS layout recorded in `.meta.json`.
- **[Feature]** `8f193fa`→`c52f5d7`, `cf19966` CVMFS import pipeline: modulefile harvest → classify → closure/`build_id` → overlay → `bits import` (build-sufficient from modulefiles).
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
