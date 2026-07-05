# ADR-0001: Relaxed CVMFS package reuse via `build_id`, and importing foreign deployments

**Status:** Proposed
**Date:** 2026-06-10
**Deciders:** Predrag Buncic; bits maintainers; CVMFS/SFT release managers

## Context

bits reuses a published CVMFS package instead of recompiling it **only when the
full content hash matches** the hash bits computes for the current recipe
closure (recipe body — comments excluded — plus the `defaults` env, the
architecture string, and every transitive dependency's hash). This exact-hash
rule is correct and is what makes a bits artifact reproducible and safe to
publish: the hash certifies the *entire* input closure, so two packages that
share a hash are byte-for-byte interchangeable.

Two real needs are not served by exact-hash-only reuse:

1. **Fast local development on top of a blessed stack.** A developer wants to
   build and test the *top* of a stack (e.g. key4hep, or one generator) on top
   of an already-published, blessed release (an LCG release on
   `/cvmfs/sft.cern.ch/...`) *without recompiling the world*. The deployed base
   was built by a different pipeline/host, so its hashes will not match what
   this developer's bits would compute — exact-hash reuse therefore rebuilds
   everything, defeating the purpose.

2. **Adopting pre-existing, non-bits CVMFS deployments.** Established releases
   (LCG) are already deployed on CVMFS but lack bits-native metadata
   (`.meta.json`, `build_id`) and may even lack a usable modulefile layout. We
   want bits to *consume* such a deployment as a reuse source without rebuilding
   or republishing it, and without write access to the (read-only, not-ours)
   package tree.

What bits already has (verified, current code):

- `cvmfs://` remote store (`CVMFSRemoteSync`, `sync.py`): reads a deployed
  `Packages/<arch>/<pkg>/<ver>/.build-hash` and grafts the deployed files into
  the store so the normal reuse path consumes them. Read-only (no `writeStore`).
- Hash-gated reuse decision (`build.py` ~2090–2360) keyed on
  `remote_hashes`/`local_hashes`; builds write `.build-hash`/`.meta.json`.
- The `islink && isdir` short-circuit that would let `sw/<arch>/<pkg>/<ver>`
  point straight at `/cvmfs` (true zero-copy) exists but is not wired for reuse.
- `prefer_system` machinery (a CLI/defaults-driven "this dep is provided
  externally, prune it" hook in `getPackageList`).
- Qualified architecture strings that already encode the ABI-critical axes,
  e.g. `ubuntu2510_x86-64-gcc15-dbg` (OS/glibc, compiler, build type).

Forces: we must **not** weaken the integrity of the publish path (the
content-addressed store and the cvmfs-prepub transparency log depend on
hash purity), while making the dev path fast and able to adopt foreign releases.
**Hard constraint:** the simple case — a plain `bits build` / aliBuild with a
local alidist and no CVMFS — must remain bit-for-bit unaffected (identical
hashes, no forced rebuild, no schema break). Every decision below is therefore
opt-in and default-off, all new metadata is additive and defensively read, and
nothing in the hashed path (recipes, `MODULE_OPTIONS`, `bits-recipe-tools`)
changes. See the implementation plan's "Backward compatibility" section for the
invariants and the CI regression guard.

## Decision

Introduce an **opt-in, policy-gated relaxed reuse mode** plus a **`build_id`
coherence token** and an **importer** for foreign deployments. Concretely:

**D1 — `--reuse-policy strict|relaxed` (CLI flag and `reuse_policy:` defaults
variable; default `strict`).** It is *not* a recipe field, because the same
recipes serve standard builds; the relaxation is a property of *this invocation*,
not of the package. `strict` is today's behaviour (exact-hash reuse only;
artifacts are hash-pure and publish-eligible). `relaxed` enables D2–D4.

**D2 — Relaxed match key = (name, qualified-arch, `build_id`), not the hash.**
Because lcg.bits recipes do not pin dependency versions, "version" reduces to
"whatever the blessed set deploys": in `relaxed` mode **the base's versions
win**, overriding the recipe/`defaults` version intent. A package the top layer
genuinely needs built differently (newer version, a patch the base lacks) is
forced out of the graft with `--build-local <pkg>[,<pkg>…]` and built locally
above the frontier. The qualified arch supplies ABI safety (compiler + build
type + OS). `build_id` supplies *closure* safety: see D3.

**D3 — `build_id`: a per-release coherence token stamped on every package built
together.** Same `build_id` ⇒ the packages were built as one coherent set ⇒ any
subset is mutually ABI-consistent **by construction**, so the resolver does not
have to walk and verify the dependency closure. `build_id` is **content-derived
and deterministic** (a human-readable release label, e.g. `LCG_109`, plus a
hash of the manifest = the sorted set of `{name, version, path, deps}`), so
independent or repeated imports of the same release agree and the id is
verifiable. Stored in `.meta.json` (`package.build_id`) and surfaced in the
modulefile as a `module-whatis "build_id: …"` line (not a `setenv`, to avoid
leaking it into the loaded environment). A later `base_build_id` lineage field
extends this to "built *against* release X in a separate run" (see Open
Questions).

The `build_id` **manifest doubles as the strict-reproducibility spec.** Besides
the package set, it records the inputs that determine bits' content hashes —
the recipe-repo commits (`lcg.bits`/`stacks.bits`), the `bits-recipe-tools`
version, the `defaults`, and an explicit **`abi_tag`** (compiler, libstdc++ ABI
/ `_GLIBCXX_USE_CXX11_ABI`, `CXXSTD`, glibc/OS). So one object serves both
modes: the *coherence token* for `relaxed`, and the exact "what to check out to
get matching hashes" recipe for `strict`. This is what makes the publish-grade
path ("reuse a CVMFS package whose hash matches") dependable on a fresh host
rather than best-effort, and it lets cross-`build_id` (`base_build_id`)
compatibility be **checked** against `abi_tag` instead of assumed.

**D4 — Frontier-cut at resolution time + zero-copy graft.** Reusing the existing
`prefer_system` hook in `getPackageList`: for each `requires`, if a deployed/
overlay package matches (name via the alias map of D7, qualified-arch, and the
selected `build_id`), **prune that node and its entire subtree** from the build
graph, wire its modulefile environment into the consumers, and zero-copy
symlink `sw/<arch>/<pkg>/<ver>` and `MODULES/<arch>/<pkg>` at `/cvmfs` (wiring
the dormant `islink && isdir` short-circuit). Everything *above* the frontier is
built locally against the grafted deps.

**D5 — Provenance qualification, and it is contagious upward.** A `relaxed`
graft taints provenance. Crucially, taint **propagates up the closure**: any
package built above a relaxed graft is itself loose, because its inputs include
unverified binaries. Provenance is therefore "pure" only if the *entire* closure
is pure (hash-verified or locally built from pure inputs). Each artifact records
its provenance (`pure` | `loose`, plus the `build_id`(s) it borrowed) in
`.meta.json`. **The publish pipeline accepts only `pure` artifacts** and checks
the whole closure, not just the directly-grafted nodes.

**D6 — Out-of-tree module + metadata overlay.** bits learns to read package
metadata from a *module-adjacent* location, not only from inside the package
tree. Native published packages keep `.meta.json` **with the package** (the
artifact stays self-describing and signable). Foreign/imported deployments get a
separate, bits-owned overlay (generated modulefiles + module-side metadata) that
*points into* the read-only foreign tree via `BASEDIR`/symlinks. The policy
selects the source: `strict` reads the package-side `.meta.json` (the binary's
own hash); `relaxed` reads the module-side `build_id`.

**D7 — Importer (`bits import`).** Convert a foreign
deployment into a bits-consumable overlay:

1. **Harvest** the deployed modulefiles by *evaluating* them
   (`MODULEPATH=<lcg> modulecmd sh display <pkg>/<ver>`) rather than parsing raw
   Tcl, capturing the resolved operations into one JSON **corpus**. Fallback
   when no modulefiles exist (manifest-only deployments): synthesise the env
   from the manifest's path info plus the matched recipe template.
2. **Classify** each entry's ops into three buckets: env ops that fit the
   generic `BitsModule` template (`bin→PATH`, `lib/lib64→LD_LIBRARY_PATH`+
   `CMAKE_PREFIX_PATH`, `python→PYTHONPATH`, `pkgconfig→PKG_CONFIG_PATH`);
   **`module load`/`prereq`/`depends-on` → a structured `deps:[…]` list**
   (so names are remappable and so they form the release graph edges); and any
   leftover `setenv`/etc. carried **verbatim**. ~90% are pure generic+deps; the
   rest keep verbatim extras.
3. **Factor the base prefix** out of every path so generation is pure
   substitution over (prefix, version, `build_id`).
4. **Closure-check** the `deps` edge set — refuse to stamp one `build_id` on a
   set with dangling edges — then assign the deterministic `build_id` of D3.
5. **Generate** the bits overlay modulefiles (retargeted prefix + `build_id` +
   remapped deps) and the module-side metadata of D6, with **path validation**
   (the generated paths must exist in the CVMFS tree) and per-package overrides.
   The modulefile is made **build-sufficient** — it adds the build hooks
   (`CMAKE_PREFIX_PATH`/`PKG_CONFIG_PATH`/`CPATH`/`<Pkg>_ROOT`, guarded on the
   tree) so a grafted dep's build env comes from *loading the modulefile* (via
   `bits printenv`/module), the same mechanism as bits-native deps. No separate
   `init.sh` is synthesised.

The corpus is simultaneously the import source, the bits-native manifest for the
release, the template library, and the name corpus.

**D8 — Name-alias map is the only fuzzy human input.** lcg.bits vocabulary and
the foreign deployment's vocabulary (e.g. `ROOT` vs `root`, `-local2` vs
hash-revisions) are reconciled once via an alias table, used **only** at
reuse-resolution time to match a recipe's `requires` to an overlay module. Env,
deps, paths, and the closure check all come from the corpus — not the recipe.

**D9 — The published module-overlay directory is its own CVMFS nested
catalog.** When the generated modulefiles are published, the overlay root
carries a `.cvmfscatalog` marker so that subtree becomes an independent nested
catalog. This makes module enumeration use bits' **existing fast catalog path**
(`cvmfs_catalog.py` / `bitsModules`: read `user.catalog_counters`, fetch the
catalog, query SQLite — one HTTP round-trip instead of a per-file FUSE walk),
which is exactly how `bits q` already lists modules quickly on CVMFS. Both the
runtime listing (`bits q`/`avail`) and the reuse resolver scanning the overlay
for available `build_id` packages then enumerate in O(1 catalog fetch) rather
than walking the tree. The catalog boundary also lets the whole module set be
fetched/cached as a unit and snapshotted independently of the (much larger)
package payload. The importer emits the marker; native module publishing should
do the same for its `MODULES/<arch>/` root.

**D10 — Module sets are per-`build_id`; package payload is deduplicated;
multiple modulefiles may reference one package under owner attestation.** Every
package in a coherent release is published with its own modulefile, **even when
its content is unchanged** and CVMFS CAS deduplicates the underlying payload —
the modulefile is a tiny per-release entry, so a full per-release module set is
essentially free. The module overlay is therefore namespaced by `build_id`
(layout `MODULES/<build_id>/<arch>/…`, one nested catalog each per D9), which
also removes the collision when several releases share a `MODULEPATH` (two
`Boost/1.90` from different releases are distinct module *files* pointing at the
same deduplicated payload). A single CVMFS package may be referenced by several
`(version, build_id)` modulefiles — i.e. an already-published binary can be
adopted into a *new* coherent set without rebuild. Because that breaks the
"built-together ⇒ coherent" guarantee of D3 (the binary was built in a
*different* run), it is valid **only as an explicit act of the repository owner,
who attests the package works within the new set**; the attestation (who, when,
which `base_build_id`/sets) is recorded in the new `build_id` manifest and is the
provenance for that owner-asserted reuse. bits trusts the attestation; it does
not re-derive coherence for cross-set references.

## Options Considered

### Option A: Exact-hash reuse only (status quo)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low (already built) |
| Reproducibility | Perfect |
| Dev iteration on blessed stacks | Poor — rebuilds everything |
| Adopt foreign (LCG) releases | Not possible |

**Pros:** Strong integrity; nothing to build. **Cons:** Fails both target use cases.

### Option B: Loose match on (name, version) only, no `build_id`
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| ABI safety | **Unsafe** — can mix incompatible closures (diamond ABI) |
| Reproducibility | None |

**Pros:** Trivial. **Cons:** Silent ABI corruption when a consumer links a
grafted dep alongside a differently-built sub-dep. Rejected.

### Option C: Relaxed match on (name, qualified-arch, `build_id`) + frontier-cut (CHOSEN)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium |
| ABI safety | Safe *by construction* (same `build_id` ⇒ built together) |
| Dev iteration | Fast (build only the top) |
| Adopt foreign releases | Yes, via importer overlay |
| Reproducibility | Loose (explicitly gated out of publish) |

**Pros:** Safe, fast, adopts foreign sets, clean strict/relaxed split.
**Cons:** Loose artifacts not reproducible (by design); needs `build_id`,
importer, alias map.

### Option D: Regenerate modulefiles from lcg.bits recipes (rejected for env)
**Pros:** Recipe knows bespoke env. **Cons:** Depends on a matching recipe;
assumes the foreign layout matches the recipe's `MODULE_OPTIONS`; re-derives env
that already exists and is validated in the deployed modulefile. Rejected in
favour of harvesting the deployed modulefile (D7); the recipe is reduced to the
name-alias role (D8).

### Option E: Relocate `.meta.json` globally to the module side (rejected)
**Pros:** One place; convenient for the importer. **Cons:** Breaks the
self-contained, signable native artifact (binary separated from its provenance).
Rejected in favour of the role-split overlay (D6).

### Option F: Mutate the foreign package tree to inject bits metadata (rejected)
**Cons:** The foreign tree is read-only and not ours. Rejected; the overlay (D6)
adds compatibility without touching it.

## Trade-off Analysis

The core trade is **reproducibility for speed/adoptability**, made safe by two
disciplines that cost almost nothing because bits already has the hooks:

- **ABI safety is bought by the qualified arch + `build_id`, not by closure
  analysis.** This is the central bet: rather than verify that a grafted
  subtree is internally consistent, we *trust a token that certifies it was
  built together*. That collapses the hard part (graph consistency) into a
  string compare, but it means `build_id` must be assigned only to genuinely
  coherent sets (hence the closure-check in D7.4 and the determinism in D3).
- **Integrity is preserved by making loose-ness contagious and publish-blocked
  (D5).** The strict path is untouched; the only way a loose binary reaches the
  store is a pipeline bug, which the closure-wide provenance check guards.

The residual risk is *misplaced trust*: if a `build_id` is stamped on an
incoherent set, or the qualified arch fails to capture an ABI axis, relaxed
reuse can produce broken binaries. Both are dev-only blast radius (never
published) and both are mitigated (closure-check; arch discipline).

## Consequences

**Easier:** developer iteration on blessed stacks (build only the top, instant
zero-copy base); adopting existing LCG releases as reuse sources without rebuild
or republish; reasoning about reuse safety (one token, not a graph walk);
finally wiring the true zero-copy symlink path.

**Harder / new burden:** maintaining the name-alias map; keeping the importer's
env fidelity correct (harvest + validation + per-package overrides); ensuring
`build_id` is stamped only on closed/coherent sets; two metadata locations to
keep from drifting; the publish pipeline must enforce the closure-wide
provenance check.

**To revisit:** `base_build_id` lineage for cross-build "work together";
optionally tying `build_id` to the cvmfs-prepub signing/transparency log so a
blessed set's integrity is verifiable before adoption; GC/lifetime of relaxed
builds (they break if the blessed release is removed).

## Open Questions / Gaps surfaced on review

1. **Relaxed reuse presupposes "build against modules" — and the env must be
   *build*-sufficient, not just runtime.** To compile the top layer against
   grafted `/cvmfs` deps, the build environment must come from the deps'
   **modulefiles**, not from bits' usual `INSTALLROOT` layout. The catch: a
   harvested modulefile is runtime-oriented (`PATH`, `LD_LIBRARY_PATH`), whereas
   compilation needs `CMAKE_PREFIX_PATH`, include/lib dirs, `pkg-config`,
   `*_ROOT`. This is exactly the build-vs-runtime env drift that is already a
   recurring bug class. So (a) the "build env == runtime modulefile env"
   capability is a **prerequisite**, not an optional follow-on, for D4; and
   (b) the importer must **validate that each grafted module is build-sufficient**
   (or augment it) — this is the first place a relaxed build (e.g. key4hep on
   LCG) will fail, at compile/link time, not runtime.
2. **ABI tag beyond the arch string.** Within one `build_id` ABI is automatic.
   Across `build_id`s (the `base_build_id` lineage), compatibility must compare
   ABI-relevant config (compiler, libstdc++ ABI / `_GLIBCXX_USE_CXX11_ABI`,
   `CXXSTD`, glibc/OS). Record an explicit `abi_tag` with each `build_id` so
   cross-build compatibility is *checked*, not assumed.
3. **Importer input modes.** The original motivating case is a manifest with
   **no** modulefiles; `module show` harvest is impossible there. The importer
   needs the manifest-only fallback (env from manifest paths + recipe template),
   same corpus schema, different env source.
4. **Per-package "force local" escape.** A developer may need a package built
   the way *their* recipe specifies (a patch/feature the blessed build lacks)
   while grafting everything else. Support `relaxed` with an exclude list
   (`--build-local pkgA,pkgB`), CLI/defaults, not a recipe field.
5. **Frontier/release selection.** When several `build_id`s are deployed, which
   base to graft from must be explicit (`--reuse-base LCG_109`), with a sane
   default (newest satisfying the most deps).
6. **Discoverability.** `bits q`/`avail` could filter by `build_id` so a user
   can see "what the blessed set offers."
7. **Trust.** Relaxed reuse trusts the deployment; optional hardening is to
   verify the `build_id` manifest signature (cvmfs-prepub transparency log)
   before adoption.

## Rollout / Sequencing

The design couples two independently large efforts; decouple them to de-risk.

- **Stage 0 — `build_id` only.** Add `build_id` (+ `abi_tag` + reproducibility
  inputs, D3) to `.meta.json` and a `module-whatis` line. Small, immediately
  useful for provenance/discoverability, and the foundation everything keys on.
- **Stage 1 — relaxed reuse of *bits-native* releases.** `--reuse-policy`,
  frontier-cut, zero-copy graft, provenance propagation, **build-against-modules**
  — validated against a release bits itself published (no importer needed; the
  metadata already exists). This proves the reuse machinery and surfaces the
  build-sufficient-env problem on a controlled surface.
- **Stage 2 — import foreign deployments.** The importer (D7), name-alias map
  (D8), per-`build_id` overlay + nested catalog (D9/D10), manifest-only
  fallback. This is where LCG adoption lands, on top of proven machinery.
- **Stage 3 — hardening.** `base_build_id` lineage + `abi_tag` compatibility
  checks; signed-manifest verification; `bits q --build-id` filter.

## Action Items

1. [ ] Accept/iterate this ADR with release managers (esp. D5 publish-guard and D10 attestation).
2. [ ] Add `build_id` (+ `abi_tag`) to `.meta.json` and a `module-whatis` line via `MakeModule`; make it deterministic (D3).
3. [ ] Add `--reuse-policy strict|relaxed` (+ `reuse_policy:` defaults) and provenance fields (`pure|loose`, borrowed `build_id`s) in `.meta.json`.
4. [ ] Implement the resolution-time frontier-cut on the `prefer_system` hook; wire the zero-copy `islink && isdir` symlink path.
5. [ ] Implement closure-wide provenance propagation + the publish-pipeline `pure`-only guard.
6. [ ] Add out-of-tree overlay metadata reading (D6).
7. [ ] Build the importer (D7): `module show` harvest → corpus (prefix-factored, classified, deps as edges) → closure-check → deterministic `build_id` → generate overlay + validation + overrides; plus the manifest-only fallback.
8. [ ] Seed the lcg.bits ↔ foreign name-alias map (D8); report unmatched packages.
8a. [ ] Emit a `.cvmfscatalog` at the published module-overlay root (D9) so enumeration uses the fast catalog path; do the same for native `MODULES/<arch>/`.
9. [ ] **Prerequisite track:** "build against modules" (build env == runtime modulefile env) — required by D4.
10. [ ] Defer: `base_build_id` lineage; signed-manifest verification; `bits q --build-id` filter.
