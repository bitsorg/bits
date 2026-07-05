# ADR-0001 (stage): Provenance survives store → restore (default-weak)

**Status:** Proposed
**Date:** 2026-06-10
**Deciders:** Predrag Buncic; bits maintainers
**Extends:** [ADR-0001](0001-cvmfs-relaxed-reuse.md) (relaxed reuse / build_id / contagious provenance).
**Prompted by:** cross-backend stores (recall from CVMFS, upload freshly-built to S3 — `DualRemoteSync`). Raises the question: once an artifact leaves the machine that built it, how is its provenance preserved on the way back?

## Context

ADR-0001 gives every package a provenance stamp computed **at build time** from
the live dependency resolution:

- `create_provenance_info` (`build.py`) writes `provenance`, `reuse_policy`,
  `build_id` into the package's `.meta.json`.
- A package is `"loose"` iff its closure contains a package grafted from CVMFS
  (`from_cvmfs`, adopted by `build_id`/ABI, **not** hash-verified) — `"pure"`
  otherwise. `from_cvmfs` is set only under `--reuse-policy relaxed`
  (`utilities.py`); strict, hash-matched recall is always `"pure"`.
- Loose artifacts are currently **forbidden from any write store** (`build.py`):
  `--reuse-policy relaxed` + `--write-store`/`--pipeline` dies. So today only
  `"pure"` artifacts can reach a store.

Two facts make this safe *today* but fragile *tomorrow*:

1. `.meta.json` (with the provenance stamp) lives inside the package tree and
   therefore inside the tarball — provenance **already travels with the
   artifact**.
2. But provenance is only ever **computed**, never **re-read**. On recall, bits
   downloads the tarball and trusts the resolver's live `from_cvmfs` flags; it
   does not consult the restored artifact's `.meta.json`. So if a loose artifact
   were ever restored and used as a dependency, its consumer would **not** be
   marked loose — contagion breaks across the store→restore boundary.

As long as loose artifacts can't enter a store (the guard above) this gap is
latent. The moment we want loose artifacts in a (segregated) store — the natural
next step after cross-backend publishing — the gap becomes a
provenance-laundering hole.

## Decision

Make provenance a property of the **artifact**, re-established on restore, and
**weak by default**:

1. **Re-read on recall.** When a tarball is recalled from any store, read its
   `.meta.json`. If `provenance != "pure"` — or the field/`build_id` is missing,
   or the closure can't be confirmed — tag the in-memory spec loose
   (`from_cvmfs`-equivalent) so contagion continues into anything built on top.
2. **Default-weak.** Strong (`"pure"`) is *earned*, never assumed: only an
   explicit `provenance: "pure"` with a verifiable `build_id`/closure yields a
   strong spec. Absence or ambiguity ⇒ loose.
3. **Segregate stores.** Loose artifacts go to their own store path/prefix
   (never the strong-blessed path), so a strong consumer never silently picks one
   up.
4. **Strong publish refuses loose inputs.** A publish into a strong store fails
   if any input's restored provenance is loose. `build_id` + `reuse_policy`
   already make the check cheap.

This keeps the simple aliBuild case unchanged (everything is `"pure"`, nothing is
re-tagged) and is purely additive — provenance still never enters a package hash.

## Options Considered

### Option A: Default-weak, re-read on restore (this decision)
| Dimension | Assessment |
|-----------|------------|
| Safety | High — fail-safe; no laundering |
| Complexity | Medium — recall must parse `.meta.json` + propagate |
| Compatibility | Full — pure stays pure |

**Pros:** correct under partial/legacy metadata; contagion survives transport.
**Cons:** a recall path now does a small metadata read + closure check.

### Option B: Trust the build-time stamp, do not re-read
**Pros:** zero new work on recall. **Cons:** contagion silently breaks across
store→restore; a loose artifact can be laundered into a strong consumer. Rejected.

### Option C: Keep loose artifacts out of all stores forever (status quo)
**Pros:** simplest; the existing guard already does it. **Cons:** blocks the
useful "build against CVMFS, publish to S3" workflows for relaxed reuse; doesn't
scale to imported/grafted corpora. Acceptable as the interim until A ships.

## Trade-off Analysis

The only real cost of A is the recall-time metadata read and the discipline of a
segregated loose store. That buys a provenance model that is sound across
machines and time, rather than only within a single build process. Defaulting to
weak (rather than strong) is the safe direction: a false "loose" merely forces a
rebuild or an explicit re-bless; a false "pure" ships unverified binaries as
trusted.

## Consequences

- **Easier:** relaxed-reuse artifacts can eventually be cached/published safely;
  the cross-backend `DualRemoteSync` path can later be extended past strict-only.
- **Harder / to revisit:** recall gains a metadata-read step; stores must be
  partitioned by provenance; publish grows an input-provenance gate.
- **Until this ships:** keep the relaxed-reuse write-store guard and the
  strict-only `DualRemoteSync` upload gate (freshly-built `"pure"` packages
  only). That is exactly today's behaviour — this stage removes the need for the
  blanket ban, not the ban itself, and only once 1–4 are in place.

## Action Items

1. [ ] Recall reads restored `.meta.json`; sets spec loose unless `provenance ==
   "pure"` with a verifiable `build_id`/closure (default-weak).
2. [ ] Segregate loose artifacts into their own store prefix; teach recall/lookup
   the split.
3. [ ] Publish into a strong store refuses any loose-provenance input.
4. [ ] Only then: allow `--reuse-policy relaxed` to write to a (loose) store —
   relax the current ban to "loose store only".
5. [ ] Tests: round-trip a loose artifact through a store and assert a consumer
   built on it is marked loose; assert a strong publish rejects it.
