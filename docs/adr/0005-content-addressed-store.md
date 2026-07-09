# ADR-0005: Content-addressed S3 store (drop the pointer objects)

Status: **Proposed** — design for review. Simplifies the store layout by removing
the version-named links and dist-closure symlinks, keeping only the hash-keyed
tarballs. Motivated by the recurring store-consistency bugs (see Context).
Date: 2026-07-09

## Context

The S3 store today holds three kinds of objects per architecture:

1. **Content tarballs** — `TARS/<arch>/store/<h2>/<hash>/<pkg>-<verrev>.<arch>.tar.gz`.
   Content-addressed by the package hash. The only thing a build actually needs.
2. **Version links** — `TARS/<arch>/<pkg>/<pkg>-<verrev>.<arch>.tar.gz`. A small
   object whose body points at the store path (version → hash).
3. **Dist closure** — `TARS/<arch>/dist{,-direct,-runtime}/<pkg>/<pkg>-<verrev>/…`.
   One small symlink object per dependency, i.e. the package's full closure.

Build reuse never consults (2) or (3): bits computes a package's `hash` from its
recipe + resolved dependency closure + defaults + architecture and fetches
`store/<hash>` directly (`spec["remote_hashes"]` → `fetch_tarball`). The pointer
layers exist for the CVMFS publish path and for reconstructing a local store
layout — both of which can be derived from the **build manifest**, which already
records `package → hash → tarball_sha256` for the whole closure.

Maintaining (2) and (3) as separate mutable objects, kept consistent with (1)
across concurrent builds and manual cleanups, is the source of essentially every
store bug we have hit:

- one-sided state: version link exists but the content object was deleted
  (`sync.py` self-heal, commit `b00477e`);
- partial dist sets aborting as "Conflicts detected" (commit `8de7ebb`);
- half-cleaned stores, `bitsStore --broken-links`, per-slot locks, orphan GC.

These are all *pointer-consistency* problems. Content-addressed objects have none
of them: an object either exists at its hash or it does not.

## Decision

**Store only the content tarballs, keyed by hash; reconstruct everything else on
the consuming node from the manifest.**

### Layout

```
store/<arch>/<h2>/<hash>.tar.gz               # the only per-package object
SOURCES/cache/<...>                            # unchanged
MANIFESTS/<...>                                # unchanged (BOMs + signed common)
```

- Single top-level `store/` root (replaces the old `TARS/<arch>/…` tree), with
  **architecture as the first level**: the package `hash` does NOT encode the
  architecture (that is exactly why same-hash/different-arch objects collided —
  ADR context / certify per-arch fix `484…`), so arch must stay a path component
  to keep the two arch trees separate. `store/<arch>/<h2>/<hash>.tar.gz`.
- Keep the `<h2>` two-hex-char shard under the arch: a single flat `<arch>/`
  prefix with tens of thousands of objects is slow to list, and GC / `bitsStore`
  list often.
- Filename is `<hash>.tar.gz` (drop the `<pkg>-<verrev>.<arch>` decoration). bits
  keys on hash + spec, not the filename — **to verify**: nothing parses pkg/verrev
  out of the tarball filename (see "Open questions").

### Upload contract (`sync.py`)

- `HEAD store/<arch>/<h2>/<hash>.tar.gz`; if present, **skip** (content-addressed
  dedup — same hash on the same arch is byte-identical, so re-upload is never
  needed).
- Else `PUT` the tarball. **No** version-link or dist-symlink objects are written.
- Removes: `upload_symlinks_and_tarball`'s link_path put, and the whole
  `dist{,-direct,-runtime}` loop — and with them the one-sided-state self-heal and
  the "Conflicts detected" guard, which no longer have anything to guard.

### Fetch contract (`sync.py`)

- Reuse fetches `store/<arch>/<h2>/<hash>.tar.gz` by hash (already the primary
  lookup); the `<pkg>/<verrev>` fallback is dropped.

### Revision assignment (CRITICAL — prerequisite for Phase 2)

The version links are **load-bearing for the reuse decision and revision
numbering**, not just for publish. On reuse, `fetch_symlinks` downloads a
package's version links from S3, and the revision counter (`build.py` ~2704–2842)
parses each link's *target* for `(hash, revision)` to:

- **reuse** — if an existing link's hash matches this build's hash, reuse that
  revision and skip the build (`"already found … Not building"`); else
- **assign** — pick the next free revision, skipping `busyRevisions`.

Dropping the version links therefore removes the `(version → revision → hash)`
index the counter reads. `reconstruct_local_layout` (Phase 1) does NOT help — it
only knows the current build's own revision, not the global history.

**Resolution (decided): manifest-primary + a race-free rev-index.**

The revision counter derives its candidate `(version → {revision → hash})` set
from two sources, unioned, instead of scanning `TARS/<arch>/<pkg>/`:

1. **Common manifest (primary, certified).** Already fetched for signed reuse
   (`trusted_reuse_index`); each entry carries `{package, version, revision,
   effective_architecture, hash}` (certify.py `_PKG_FIELDS`). Filter to
   `(package, version, arch)`.
2. **Rev-index (supplement, for uncertified rebuilds).** A **write-once marker per
   revision** — NOT a mutable object, NOT a symlink — so it reintroduces neither
   the pointer-consistency nor the read-modify-write race we are removing:
   ```
   MANIFESTS/rev-index/<arch>/<pkg>/<version>-<revision>   (body = hash)
   ```
   Producers `PUT` their own marker on upload (HEAD-skip, idempotent); concurrent
   builds write different markers, never the same object. The counter LISTs
   `rev-index/<arch>/<pkg>/`, parses `<version>-<revision>`, reads `hash` from the
   body. Markers are tiny (~64 B) and one-per-revision (far fewer than the dist
   symlinks). GC drops markers whose hash is no longer reachable.

Same reuse/assign logic as today (hash match → reuse revision; else next free
number), just fed from (1)∪(2) rather than the version links. Ship + test this
BEFORE the upload stops writing version links.

### Local reconstruction (build node)

- After the revision counter finalises a package's revision+hash, bits
  materialises the local version link from the graph (`create_version_link`,
  wired in `build.py` for every package on the non-makeflow path) and the
  `dist*` closure (`createDistLinks` in `doFinalSync`) — no S3 read. This makes
  local reconstruction the single source of the version link that used to come
  from the S3 version-link object (upload for built packages, `fetch_symlinks`
  for reused ones).

### CVMFS publish (`bits-console/.gitlab/cvmfs-*-publish.yml`) — UNCHANGED

- **Finding (during P2d):** the publish loop does not read S3. It runs in the
  same job as the build and reads the **local** per-package tarball
  `BITS_WORK_DIR/TARS/<arch>/<pkg>/<pkg>-<verrev>.<arch>.tar.gz` (a symlink into
  the local content store), driven by the build manifest it already parses.
- Because local reconstruction (above) recreates that local version link for
  BOTH built and reused packages, the publish YAML/shell needs **no change**.
  A missing local tarball is already handled (`[[ -f ]]` → SKIP), so a dangling
  link degrades to a skip, not a crash. This is why the original "rewrite the
  publish loop to fetch `store/<hash>` by hash" is unnecessary — keeping local
  reconstruction is smaller and lower-risk and still removes the S3 pointer
  objects (the actual bug source).
- **Relocation is mandatory and must be preserved.** Store tarballs are built
  relocatable — their contents embed a placeholder/build prefix and an
  `@@PKGREVISION@@` marker — so a fetched tarball CANNOT be posted to CVMFS as-is.
  The step is unchanged (today's template mode): `untar → run relocate-me.sh with
  INSTALL_BASE = the final CVMFS path → re-tar → POST to prepub` (plus the
  module-file second job). Because publish is left untouched (above), this simply
  keeps working. Skipping relocation would publish binaries with wrong paths.
- `INSTALL_BASE` and the CVMFS target path (prefix / version-revision /
  install-dir / family) come from the **manifest**, not from the tarball name or
  the old dist symlinks, so nothing is lost by dropping the pointer objects.
  (Confirm the fetched object still contains `relocate-me.sh`; it is part of the
  build output captured in the tarball, so hash-only storage keeps it.)

### GC + bitsStore  (renamed from `manageStore`; invoked as `bits store …`)

- GC (`gc.py`) already **is** hash-vs-manifest: it only ever deletes keys under
  `TARS/<arch>/store/<h2>/<hash>/…` sourced by hash from the *verified* signed
  manifest (`safe_store_key`, `_STORE_KEY_RE`). Dropping the pointer objects
  needs no GC change.
- `bitsStore`'s content-object operations (`ls`/`rm`/`verify`/`--orphans`) are
  unchanged — they already work against the content objects vs the manifests.
- The `--links` / `--broken-links` selectors are **kept**, but now only match the
  LEGACY pointer objects left by pre-ADR-0005 builds. They are the one-time
  migration tool: `bits store rm --links` (and `--broken-links`) purge the stale
  version/dist objects from an existing store. New builds never create them.

## Consequences

- Eliminates the pointer-consistency bug class outright (the four commits above
  become unnecessary; the code they touched is deleted, not maintained).
- Simpler mental model, simpler GC, simpler cleanup, no per-slot upload locks for
  the store side.
- Cost: wiring the local-reconstruction step (`create_version_link`) so both
  built and reused packages have their local version link; the CVMFS publish loop
  is **not** rewritten (it reads local files — see above). Plus a store
  **migration / flag day** for existing pointer objects.
- Loses in-bucket browsability by package/version and the in-bucket dependency
  closure. Both are recoverable from manifests; confirm no external consumer
  relies on the S3-side closure (see Open questions).
- Scope actually implemented (P2a–P2d): the content object KEEPS its current
  path/name `store/<h2>/<hash>/<pkg>-<verrev>.<arch>.tar.gz` — only the pointer
  objects (version links + dist symlinks) are dropped. The `<hash>.tar.gz` rename
  and the single-`store/<arch>/` root are a separate, optional later change (they
  touch `resolve_store_path`, the certify probe, and `bitsStore`); the
  bug-source removal does not depend on them.

## Implementation order (phased, each independently shippable + tested)

1. **Local reconstruction** from manifest (no behaviour change yet): make bits
   able to build the local `dist*`/version layout from the graph. Add tests.
2. **Revision index from manifests (PREREQUISITE — found during Phase 1 review).**
   Make the revision counter derive its `(version, revision, hash)` candidates from
   the fetched common manifest(s) when local version links are absent, so the reuse
   decision + revision numbering survive without the S3 version links. Ship + test
   this BEFORE the upload change below, and resolve the uncertified-store
   sub-question (see "Revision assignment"). Only then:

2b. **Upload**: skip writing link/dist objects; keep HEAD-skip on the hash object.
   Producers now write hash-only. Consumers fetch by hash and get revisions from
   the manifest index (step 2).
3. **Publish loop**: rewrite to manifest + `store/<arch>/<h2>/<hash>` fetch, while
   **keeping the relocate-me.sh step** (untar → relocate to the final CVMFS
   INSTALL_BASE → re-tar → POST). Test against a real manifest; verify the
   published tree has correct embedded paths (no build-prefix leakage).
4. **GC + bitsStore** — *done (rename + no-op adaptation)*: the tool moved into the
   bits repo as `bitsStore` and is invoked as `bits store …`. GC (`gc.py`) was
   already hash-vs-manifest, and `bitsStore`'s content-object operations
   (`ls`/`rm`/`verify`/`--orphans`) already operate on `store/…/<hash>/…` vs the
   manifests, so no logic change was needed. The `--links`/`--broken-links`
   selectors are retained as the migration tool (see Migration).
5. **Migration**: new builds write hash-only; the old link/dist objects in an
   existing store are simply no longer created. Purge the stale ones with
   `bits store rm --links` / `bits store rm --broken-links` (or just rebuild the
   store). There is no `migrate` subcommand — the content objects keep their path,
   so nothing needs moving; only the pointer objects are swept.

## Open questions (resolve before Phase 2)

1. **Filename** — *resolved*: versions come from the **recipe/spec**, never from a
   tarball filename. `ver_rev(spec)` returns `spec["version"]`/`spec["revision"]`
   (utilities.py:274); every tarball name is *constructed* from the spec
   (`store_integrity._tarball_name` :70, `sync.py` upload name :1117, the rsync
   find-glob :538) and the S3 fetch keys on `resolve_store_path(arch, hash)`.
   Nothing parses pkg/verrev out of a filename. Consequence: renaming the stored
   object to `<hash>.tar.gz` loses no information — only those constructors (and
   `resolve_store_path`) change to emit `store/<arch>/<h2>/<hash>.tar.gz`.
2. **External closure consumers** — *resolved*: `bitsStore` is the only external
   tool that reads the store; it is adapted in Phase 4. No other tool (and not the
   CVMFS-graft path) reads the S3 dist closure directly, so nothing else needs a
   reconstruct-from-manifest change.
3. **Certify store-probe** (not the manifest — the *check*): before signing a
   common manifest, `certify` verifies it against the real store — for every
   package it reads the tarball from S3, hashes the actual bytes, and refuses to
   sign if the object is missing or its sha256 doesn't match what the manifest
   claims (fail-closed: you cannot sign a manifest that lies about the store).
   That read is `make_s3_probe` (certify.py:331), which builds the object key from
   `resolve_store_path(arch, hash)` — the exact path this ADR changes. So when the
   layout changes, the probe must read `store/<arch>/<h2>/<hash>.tar.gz`. Because
   `resolve_store_path` is the single path builder (upload, fetch, AND probe all
   call it), changing it once covers the probe. One extra simplification: today
   the probe expects a per-hash *directory* containing a `<pkg>-<verrev>.<arch>`
   tarball and heads/lists inside it; the new layout is a single `<hash>.tar.gz`
   object, so that dir-listing/tarball-name fallback collapses to one head/get.
   The manifest *format* (hash → tarball_sha256) is unchanged.
