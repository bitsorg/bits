# Bits — Reference Manual

> **See also:** [User Guide](USERGUIDE.md) · [Cookbook](COOKBOOK.md) · [Workflows](WORKFLOWS.md) · [Roadmap](ROADMAP.md)

## Table of Contents

> **Note:** Sections §§1–8 (Introduction through Publishing, Trust and Release Tasks) are in [USERGUIDE.md](USERGUIDE.md). This document covers developer and technical reference material starting from §9.

**Where to look:**

| You are… | Start with |
|----------|------------|
| a user running builds | [§16 Command-Line Reference](#16-command-line-reference), [§20 Environment Variables](#20-environment-variables) |
| a recipe author | [§12 Writing Recipes](#12-writing-recipes), [§17 Recipe Format](#17-recipe-format-reference), [§18 Defaults Profiles](#18-defaults-profiles), [§19 Shared Packages](#19-architecture-independent-shared-packages), [§13 Repository Providers](#13-repository-provider-feature) |
| a community admin or CI operator | [Trust-tiered reuse, publishing and signing](#artifact-resolution-order-trust-tiered-reuse), [§21 Binary Stores](#21-remote-binary-store-backends), [§22 Docker](#22-docker-support), [§23 Verification](#23-bits-verify--deployment-verification), [§25 Build Manifest](#25-build-manifest) |
| a bits developer | [§9 Architecture](#9-architecture-overview), [§10 Development Environment](#10-setting-up-a-development-environment), [§11 Source Files](#11-key-source-files), [§14 Tests](#14-writing-and-running-tests), [§15 Contributing](#15-contributing) |

### Part I — Developer Guide
9. [Architecture Overview](#9-architecture-overview)
10. [Setting Up a Development Environment](#10-setting-up-a-development-environment)
11. [Key Source Files](#11-key-source-files)
12. [Writing Recipes](#12-writing-recipes)
13. [Repository Provider Feature](#13-repository-provider-feature)
14. [Writing and Running Tests](#14-writing-and-running-tests)
15. [Contributing](#15-contributing)

### Part II — Technical Reference
16. [Command-Line Reference](#16-command-line-reference)
    - [Work Directory Layout](#work-directory-layout)
17. [Recipe Format Reference](#17-recipe-format-reference)
18. [Defaults Profiles](#18-defaults-profiles)
    - [Forcing or Dropping the Revision Suffix](#forcing-or-dropping-the-revision-suffix-force_revision)
19. [Architecture-Independent (Shared) Packages](#19-architecture-independent-shared-packages)
20. [Environment Variables](#20-environment-variables)
21. [Remote Binary Store Backends](#21-remote-binary-store-backends)
22. [Docker Support](#22-docker-support)
    - [22.1 Recipe Sandbox](#221-recipe-sandbox)
    - [22.2 Cross-compilation via QEMU](#222-cross-compilation-via-qemu)
23. [bits verify — Deployment Verification](#23-bits-verify--deployment-verification)
24. [Design Principles & Limitations](#24-design-principles--limitations)
25. [Build Manifest](#25-build-manifest)
26. [CVMFS Publishing Pipeline](#26-cvmfs-publishing-pipeline)

---
# Part I — Developer Guide

## 9. Architecture Overview

Bits is structured as a thin Bash entry point (`bits`) that delegates to a Python backend (`bitsBuild`) for all build-related work. The Python code lives in the `bits_helpers/` package.

```
bits  (Bash)
  │
  ├─ environment sub-commands (enter, load, unload, printenv, setenv, q, list, avail)
  │    └─ handled directly via modulecmd calls
  │
  ├─ stand-alone groups (store, cvmfs, use, overlay, preload)
  │    └─ dispatched to bitsStore or a bits_helpers module, before any work-dir setup
  │
  └─ build sub-commands (build, clean, deps, doctor, init, publish, certify, sign, version …)
       └─ bitsBuild  (Python entry point)
            └─ bits_helpers/
                 ├─ args.py           argument parsing
                 ├─ build.py          main orchestration loop
                 ├─ recipe.py         recipe parsing (front-matter, includes)
                 ├─ packages.py       dependency resolution
                 ├─ hashing.py        content-addressable package hashes
                 ├─ repo_provider.py  dynamic recipe-repository loading
                 ├─ scheduler.py      parallel build scheduler
                 ├─ sync.py           remote binary store backends
                 ├─ workarea.py       source checkout management
                 ├─ git.py / sl.py    SCM wrappers
                 └─ ...
```

### Architecture string and the `architecture:` template

The architecture string (e.g. `ubuntu2510_x86-64`) names install dirs, tarballs,
store paths and Docker images. By default it is auto-detected
as `%(os)s_%(machine)s`. A defaults file (typically `defaults-release.sh`) may
override the *layout* with an `architecture:` field — either a literal string or
a template using these `%(...)s` keys (same substitution syntax as recipe
sources):

| key          | example     | notes                                  |
| ------------ | ----------- | -------------------------------------- |
| `%(os)s`     | `ubuntu2510`| distro + version (or `osx`)            |
| `%(machine)s`| `x86-64`    | bits-canonical dashed CPU form         |
| `%(_machine)s`| `x86_64`   | uname/underscore CPU form              |

```yaml
# defaults-release.sh
architecture: %(os)s_%(_machine)s     # -> ubuntu2510_x86_64
# architecture: %(_machine)s-%(os)s   # -> x86_64-ubuntu2510
# architecture: ubuntu2510_x86-64     # literal, no substitution
```

Precedence (for `bits build` and `bits clean`): an explicit `--architecture`
always wins and the template is ignored; otherwise a template set by any defaults
file in the chain is rendered against the detected platform; with neither, the
auto-detected default stands. When bits checks whether an architecture is
supported, picks the Docker builder image, or decides whether the default public
store applies, it looks only at the distro and CPU tokens — whatever their order
and whether the CPU is spelled `x86-64` or `x86_64` — so custom layouts work
without `--force-unknown-architecture`.

### CVMFS layout

A defaults profile (typically `defaults-release.sh`) may declare where a build's
packages and modulefiles live on CVMFS, so the build / publish / reuse paths are
derived from one place instead of repeated CLI flags. Five optional, templated
top-level fields (templates may use `%(architecture)s`, the effective combined
architecture):

```yaml
cvmfs_dir:   /cvmfs/sft.cern.ch/lcg/releases   # CVMFS root
install_dir: %(architecture)s/Packages         # relative to cvmfs_dir
module_dir:  %(architecture)s/modules          # relative to cvmfs_dir
shared_dir:  noarch                           # architecture-independent packages
views_dir:   Views                            # merged release views
```

Fields left out take defaults (`install_dir`: `%(architecture)s`, `module_dir`:
`%(architecture)s/modules`, `shared_dir`: `noarch`, `views_dir`: `Views`), so
setting `cvmfs_dir` alone is enough. bits joins each with `cvmfs_dir` and uses
the result for:

- **provenance:** the resolved paths are recorded in each package's `.meta.json`,
  so publishing and the release-view tools find the target trees without
  re-reading the defaults profile. Docker builds do *not* take `--cvmfs-prefix`
  from the layout: they build relocatably and are relocated on publish; pass
  `--cvmfs-prefix` explicitly to compile at the final CVMFS path;
- **reuse:** `--reuse-from cvmfs` resolves the deployed modules tree from the
  same layout (or, if none is declared, from the group's
  `cvmfs_modules_template`), so already-deployed components are set up from
  their published modulefiles. `--remote-store` stays the tarball store; a
  `cvmfs://` store URL is rejected.

Builds that don't set any of these fields are unaffected.

### Build pipeline

```
load repository providers      ← clone any repository-provider packages,
                                 extend BITS_PATH, repeat until stable
        │
resolve packages               ← parse all recipes, resolve the full
                                 dependency graph, sort it (dependencies first)
        │
update source mirrors          ← refresh the reference mirror of each source repo
        │
        ├─ prefetch in the background: source archives and pre-built tarballs
        │  (best effort; anything missing is fetched when needed)
        │
        └─ for each package, dependencies first:
               compute its content hash (includes its dependencies' hashes)
               reuse a matching existing build if there is one (see below);
               otherwise:
                 check out the sources into the build area
                 run the recipe's build script, then pack the install root
                 upload the tarball to the write store (if configured)
```

### Artifact resolution order (trust-tiered reuse)

Before compiling a package, bits looks for an existing, trusted build of the
*exact same content hash* and reuses it. Tiers are consulted in order — first hit
wins — and each has its own root of trust:

1. **Local store on the build node** (`$WORK_DIR/TARS`, already-unpacked
   `INSTALLROOT`) — artifacts this node built or fetched earlier. Ultimately
   trusted (produced here) and cheapest, so it is consulted first.
2. **CVMFS**, when reusing a deployed release (`--reuse-from`) — the published
   read-only tree. Trusted by CVMFS itself: the repository is signed at Stratum-0
   and the client verifies it against the repo key in `/etc/cvmfs/keys`. No extra
   bits-level attestation is needed for these artifacts.
3. **Remote archive (S3/HTTP), verified against a signed manifest** — content-
   addressed tarballs. Integrity comes from the content hash + `tarball_sha256`;
   *authenticity* comes from a signature-verified release manifest: a tarball is
   reused only if its hash appears in a trusted signed manifest **and** its
   sha256 matches. This is what makes a public archive safe to reuse from.
4. **Build from source** — the fallback; the source archive is integrity-pinned
   by the recipe's `source_checksums`. Trusted by construction.

By default, reuse at any tier requires an exact content-hash match. The hash
covers the recipe, its sources and patches, and the hashes of all its
dependencies (including the defaults package); the store keys each artifact by
architecture *and* hash. A hit is therefore the artifact bits would otherwise
have built. The one exception is `--reuse-policy relaxed` (CVMFS tier only, for
local development): it also reuses a deployed package of the same version
without a hash match, and the publish path refuses the result.

`--no-remote-store` disables the S3/HTTP archive tier (a `--write-store`, if
given, is still read from). CVMFS reuse is controlled separately, by
`--reuse-from`. The local store and build-from-source always remain.

#### Signing and verifying the archive tier

Reuse from the remote archive (tier 3) is controlled by these `bits build` options:

- `--sign-manifest KEY.pem` — after a successful build, sign the build manifest
  (`bits-manifest-latest.json`) with an Ed25519 private key. The detached
  signature is written next to the manifest (`.sig`). Run this on the release/CI
  host that produced the archive; the private key never ships.
- `--trust-manifest URL|PATH` — the signed release manifest a consumer trusts as
  the authority for archive reuse. Its signature is verified against the public
  keys shipped in `bits/keys/` (plus `$BITS_TRUST_KEYS` and
  `~/.config/bits/keys`).
- `--require-signed-reuse` (the default) — fail closed: a tarball fetched from
  the remote store is reused only when a trusted signed manifest lists its hash
  **and** its sha256 matches. Unlisted → discard and rebuild; sha256 mismatch → fatal (tampering).
  Local build-node and CVMFS artifacts are unaffected.
  Maximal reuse is the default: with no `--trust-manifest`, a build trusts
  every signed manifest present in the store (content hashes do not include the
  architecture, so a hash vouched for by any trusted manifest counts, whichever
  platform or community certified it); signature and expiry are the only checks. Scope it
  with `--trust-groups`, or turn verification off with `--no-require-signed-reuse`.
- `--trust-groups G1,G2,…` — scope reuse by group. The signed common manifest may
  tag each entry with a `group`; with `--trust-groups` a consumer trusts only
  those groups plus the always-trusted `common` base (untagged entries count as
  base). Omit it to trust every signed entry. Produce group tags at certification
  time with `bits sign --group GROUP`.
- `--reuse-beacon URL` (or `$BITS_REUSE_BEACON`) — report the hashes this build
  reused from the store to `<URL>/api/reuse` (best effort, in the background; it
  never blocks or fails the build). Only small references are sent, never
  artifact data. This lets the console see which stored artifacts are in use.

#### Publishing and certifying a build — `bits publish`, `bits certify`

Three commands, one per step:

| Command | What it does | Credentials |
|---|---|---|
| `bits publish` | uploads the latest build (or `--manifest M`) to the S3 store, with its per-architecture BOMs | `~/.bits/s3keys` |
| `bits certify` | makes that build trusted: uploads whatever is still missing, gets a passkey approval, opens the certification MR | `~/.bits/s3keys`, `~/.bits/gitlab-token`, a passkey |
| `bits sign` | merges the BOMs, validates them against the store and signs — run by the manifests-repo CI | CI identity |

`bits certify` on its own:

1. uploads anything not yet in the store (idempotent, so it is harmless after
   `bits publish`);
2. asks bits-console to pre-approve the build and prints a QR code, the approve
   link, and a short code. Open the link on your phone, check that the code
   matches, and approve with your passkey. The approval binds the build's exact
   package hashes;
3. opens a merge request in the manifests repo adding the BOMs under
   `manifests/<group>/`, via the GitLab REST API with your token (works with SSH
   push; only host + path are taken from the remote URL). The token comes from
   `--gitlab-token`, `$BITS_CERTIFIER_TOKEN`/`$GITLAB_TOKEN` or
   `~/.bits/gitlab-token` (chmod 600).

The manifests CI then checks the MR author and the approval, and signs (`bits sign`).
If the approval is refused or times out, no MR is opened.

`--group`, `--manifests-remote`, `--ref` and `--console` default from the active
defaults' `system:` block, so a configured community just runs `bits certify`:

```yaml
system:
  certify_group:    ship
  manifests_remote: https://gitlab.cern.ch/buncic/bits-manifests.git
  console_url:      https://bits.cern.ch
```

These live under `system:` because they are publish policy, not part of any
package hash. `--approval none` opens the MR without asking for an approval, for
builds approved elsewhere (a bits-console build is pre-approved in the browser and
its MR is opened by the console's bot with `--certifier`). `--console-cafile PEM`
trusts a private CA; `--console-insecure` allows a plain-http console on a trusted
testbed.

#### Signing — `bits sign`

`bits sign <manifests…> --key <ed25519.pem> -o common-manifest.json` merges
published build manifests into one signed common manifest (the trust unit), after
validating every hash against the S3 store (`--remote-store`; `--store` is the
deprecated spelling). Instead of a local `--key`, CI can sign through a signing
proxy (`--sign-via-proxy`) or the bits-console signing service
(`--sign-via-service`). `--group` tags entries with a group; `--no-store-check`
does an offline dry merge. In the manifests-repo CI,
`--require-approval --admins ADMINS` refuses to sign unless the certifier is an
authorised admin. The identity is established in one of three ways (in order):
`--certifier USERNAME` (default `$GITLAB_USER_LOGIN` — the pipeline initiator
GitLab already authenticated; no API call); `--certifier-token PAT` (identify via
`GET /user`); or, failing both, reading who approved the merge request. See the
`bits-manifests` repo for the pipeline scaffolding.

Offline freshness: `--valid-days N` stamps an `expires` timestamp and
`--source-commit SHA` (default `$CI_COMMIT_SHA`) records the certified commit. A
consumer rejects a signed manifest whose `expires`
has passed — fail-closed, so a stale manifest cannot be replayed offline. A
manifest without `expires` never expires (backward compatible). See `keys/README.md`
for key rotation using the multi-key trust anchor.

Certifier identity and authority:

- `--admins FILE` is an overall/per-group admin policy. Lines `@handle` or
  `* @handle` are **overall** admins (can approve/override any group; mirrors
  bits-console `bits_admins`); `<group> @handle` lines are that group's admins
  (mirrors per-community `admins`). A `&group-path` token resolves to that GitLab
  **group's live members** via the API at certify time (so the list never needs
  manual syncing), while explicit `@handle` entries remain as a manual override.
  A group ref that can't be resolved (API/permission failure) is skipped with a
  warning, so literal admins keep working. `--changed-groups G1,G2` scopes the
  check to the groups changed in the MR (else every group present).
- Identity: with `--certifier-token PAT` (or `$BITS_CERTIFIER_TOKEN`) the
  initiating admin's own GitLab PAT authenticates them via `GET /user` — an
  unforgeable identity — which must be an authorised admin and is recorded as
  `certified_by` in the signed manifest. Without a certifier token, the gate
  falls back to reading who approved the merge request (read-only bot token).
  Either way the certifier identity travels with the signature.
- Per-key group binding: a `keys/key-policy.json` mapping `key_id -> [groups]`
  restricts which groups each signing key may certify (`"*"` = overall key).
  Enforced both when signing and when a consumer verifies a manifest; with no
  policy file there is no restriction. See `keys/README.md`.

Per-platform certification: the store is content-addressed **per
architecture** — object identity is `(effective_architecture, hash)` — so
entries from different platforms can never conflict and certification is
scoped by platform. `bits publish` emits **one BOM per effective
architecture** (`share` — noarch — is just another platform; the arch is in
the BOM file name), and `bits sign --architectures A1,A2` merges,
store-validates and signs only those platforms' BOMs, leaving the other
platforms' signed manifests untouched. A scoped platform whose BOMs are all
gone is re-signed **empty** — deleting a platform's BOMs revokes its entries.
The manifests-repo CI derives the changed architectures from the merge diff,
so validation cost scales with the change, not with the whole store, and one
platform's problem never blocks another's certification. Without
`--architectures` every architecture present is re-derived (full run).

Store objects are **authoritative** for their checksum. Package tarballs are
packed deterministically (see [Build lifecycle with a store](#build-lifecycle-with-a-store)),
but an object packed by an older bits or with a different tar/compressor
toolchain is not byte-identical, so the same content hash re-packed by a later
build can be a different file — expected and benign. An upload that finds an object already
at its designated path keeps it and records **its** sha256 in the build
manifest and BOM (read from the checksum stored with the object; for older
objects without one, bits hashes the object once and records it), so every
manifest converges on the
one stable object that certification verifies. Store objects are never
overwritten.

A BOM can still outlive the bytes it describes: the store was wiped and the
same hashes rebuilt, or two nodes raced to upload one missing object. When two
BOMs disagree on an `(arch, hash)`, certification asks the store and keeps the
entry matching the stored object (one summary warning); it fails closed when
the store confirms none or more than one of the claims. `bits store ls
--stale-boms` lists BOMs no entry of which matches the store any more and at
least one of which it contradicts; `rm --stale-boms` removes them and `verify
--stale-boms` exits 1 while any exist. `--manifests-dir DIR` checks the files of
a local bits-manifests checkout — the copy certify reads, so removing them there
(commit + MR) is what clears the conflict — instead of the store's `MANIFESTS/`
copies. A BOM is never selected when any of its entries still matches the store
or cannot be checked, or when its objects are merely absent.

#### Store garbage collection — `bits store gc`

`bits store gc --trust-manifest <signed-common-manifest>` sweeps unreferenced objects
from the shared S3 store. The roots are every content hash in the *verified*
signed common manifest; any store object whose hash is not a root and is older
than `--grace-days` (default 7) is removed. Each run sweeps one architecture's
store tree (`-a`, default: the detected architecture). It is deliberately
conservative:

- **Fail-closed** — if the manifest does not verify, or verifies to zero roots,
  nothing is swept (an unverifiable manifest never becomes "delete everything");
  only `--allow-empty` permits a sweep with zero roots.
- **Bounded namespace** — only keys matching `TARS/<arch>/store/<shard>/<hash>/<file>`
  with `shard == hash[:2]` and no whitespace/control characters are eligible;
  everything else is skipped.
- **Object-by-object** — deletes individual keys (re-validated at delete time),
  never a prefix/directory delete.

Use `-n/--dry-run` to see what would be swept. Because objects are shared and
keyed by content, dropping one build's roots never deletes an object another
certified build still references.

#### Uploads and certification

Reads need no credentials. Uploading to the S3 store is governed by possession of
S3 credentials (see the `b3://` backend below and the `~/.bits/s3keys` file) — any
user or CI job with write keys can upload artifacts and the build manifest.

Certification (signing) is a separate, deliberate step: a **group admin**
approves the build (in bits-console, or with a passkey via `bits certify`), and
the manifests-repo CI then signs the manifest with the release signing key. By
default, consumers reuse only artifacts listed in a verified signed manifest
(see `--require-signed-reuse` above).

#### Licence compliance and redistribution policy

Recipes carry licence metadata in the YAML front-matter. All of it is
**hash-excluded** (like comments): editing it never changes a package hash, so
licence corrections cost zero rebuilds.

```yaml
license: GPL-2.0-or-later        # SPDX id (LicenseRef-* for custom licences)
acknowledgment: "This product includes …"   # attribution text, if required
redistributable: none            # all | binaries | sources | none
```

`redistributable:` declares which **forms** of the package may be
redistributed — a "no redistribution" licence clause covers both the source
code and the binaries unless it says otherwise:

| Value      | Binaries → store/CVMFS | Source archives → store mirror |
|------------|------------------------|--------------------------------|
| `all` (default) | yes | yes |
| `binaries` | yes | no |
| `sources`  | no  | yes |
| `none`     | no  | no  |

Legacy booleans parse as `all`/`none`; an unrecognised value fails **closed**
(`none`, with a warning) — a typo must never publish a restricted package.
Enforcement is at every boundary: the end-of-build upload and the bulk
`bits publish` skip restricted binaries (they never enter the BOM either — what
is not in the store cannot be certified or reused), the per-package CVMFS
publish refuses them, and the source-archive mirror (`SOURCES/cache/`) drops
restricted sources while still allowing *fetches* from the mirror. Restricted
packages are still built and usable locally; they just never leave the host.

Attribution and the GPL source obligation are discharged mechanically: the
build script writes a per-package `NOTICE` into `$INSTALLROOT` when one is
needed (the recipe declares an `acknowledgment:` or a copyleft `license:`),
built from those fields and the source location, and `bits publish`
generates the per-release aggregation — `NOTICE` (required attributions,
every distributed package with its SPDX id, and the licence-excluded list)
plus `LICENSE-SOURCE-OFFER.txt` (where the corresponding sources of every
copyleft component are archived, and for how long) and the release's SBOMs
(`sbom.cdx.json`, CycloneDX 1.6; `sbom.spdx.json`, SPDX 2.3; see `bits sbom`) —
uploaded next to the release's BOMs under `MANIFESTS/<build_id>/` and placed at
the root of a published release view.

`bits compliance` audits it all (see the command reference), and
`bits compliance --enforce` is the admin tool to purge non-compliant packages
from the store and its manifests and re-certify the affected platforms.

---

## 10. Setting Up a Development Environment

```bash
git clone https://github.com/bitsorg/bits.git
cd bits

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# Install in editable mode with development extras
pip install -e '.[test,docs]'   # quotes needed in zsh
```

CI lints with ruff in check-only mode (`ruff check .`), configured under
`[tool.ruff]` in `pyproject.toml` and limited to real-bug rules (E9, F63, F7,
F82). Run the same check before submitting a patch:

```bash
pip install ruff
ruff check .
```

Do not run `ruff format` or `ruff check --fix` on the tree: the hand-aligned
2-space style is kept on purpose, and `--fix` would strip deliberate
import-availability probes. `.flake8` and `.pylintrc` are still in the repository
but are not enforced by CI.

---

## 11. Key Source Files

| Path | Purpose |
|------|---------|
| `bits` | Bash entry point; handles environment sub-commands, delegates build to `bitsBuild` |
| `bitsBuild` | Python entry point; dispatches all build sub-commands |
| `bitsDeps` | Thin wrapper calling `bitsBuild deps` |
| `bitsDoctor` | Thin wrapper calling `bitsBuild doctor` |
| `bitsStore` | `bits store` — S3 store inspection, verification, deletion, `gc`, `stats`, `upload` |
| `bitsModules` | Module listing on CVMFS through the serving catalog (used by `bits` with `BITS_CATALOG_LISTING=1`) |
| `aliBuild` | Backward-compatible wrapper: sets the ALICE defaults and execs `bits` |
| `pb` | Thin wrapper calling `bitsBuild` |
| `bitsenv` | Legacy environment manager |
| `bits_helpers/args.py` | Argument parsing for all sub-commands |
| `bits_helpers/build.py` | Core build orchestration (~4 000 lines); `doBuild` |
| `bits_helpers/hashing.py` | Build-hash computation (`storeHashes`) and the hash-excluded recipe keys |
| `bits_helpers/recipe.py`, `packages.py`, `defaults.py` | Recipe parsing (`parseRecipe`), dependency resolution (`getPackageList`), defaults loading and merging |
| `bits_helpers/arch.py`, `matchers.py`, `paths.py` | Architecture detection and effective arch; conditional `requires`/`patches:` matchers; recipe/defaults file lookup (`getConfigPaths`) |
| `bits_helpers/utilities.py` | Small shared helpers (topological sort, version/tag resolution, `version_from`); re-exports `yamlLoad`/`yamlDump` for external generators |
| `bits_helpers/repo_provider.py` | Iterative repository-provider discovery and caching |
| `bits_helpers/deps.py` | DOT/PDF dependency graph generation via Graphviz |
| `bits_helpers/init.py` | `bits init` — writable development checkouts |
| `bits_helpers/doctor.py` | `bits doctor` — system-requirements checking |
| `bits_helpers/clean.py` | `bits clean` — stale artifact removal from temporary build area |
| `bits_helpers/cleanup.py` | `bits prune` (was `bits cleanup`) — LRU + disk-pressure eviction from persistent workDir; sentinel management |
| `bits_helpers/publish.py` | `bits publish` — copy, relocate and hand a package to cvmfs-prepub; bulk manifest upload to the S3 store; `bits certify` |
| `bits_helpers/certify.py` | `bits sign` — merge BOMs, validate against the store, sign the common manifest |
| `bits_helpers/trust.py` | Manifest signing and verification, trust-key directories, key policy (`trusted_index`) |
| `bits_helpers/manifest.py` | Build manifest (`MANIFESTS/bits-manifest-*.json`) |
| `bits_helpers/gc.py` | `bits store gc` — reachability garbage collection of the S3 store |
| `bits_helpers/compliance.py`, `sbom.py`, `notice.py` | `bits compliance`, `bits sbom` (CycloneDX / SPDX), NOTICE and source-offer generation |
| `bits_helpers/verify.py` | `bits verify` — check a deployment against a build manifest |
| `bits_helpers/cvmfs_inspect.py` | `bits cvmfs` group — `platforms`/`show`/`summary`; dispatches `stage`/`publish` |
| `bits_helpers/cvmfs_publish.py` | `bits cvmfs publish` — place a build's packages, release view and merged view on CVMFS |
| `bits_helpers/plan.py` | `bits build --dry-run` per-package reuse plan |
| `bits_helpers/brew.py` | `bits brew` — macOS Brewfile from the recipes |
| `bits_helpers/overlay/lcg.py` | `bits overlay lcg` — LCG release view over a built closure |
| `bits_helpers/bits_use.py` | `bits use` — per-directory saved-argument profiles |
| `bits_helpers/scheduler.py` | Multi-threaded parallel build scheduler |
| `bits_helpers/sync.py` | Remote binary store backends (HTTP, S3, Boto3, rsync; `cvmfs://` is rejected) |
| `bits_helpers/git.py` | Git SCM wrapper |
| `bits_helpers/sl.py` | Sapling (`sl`) SCM wrapper |
| `bits_helpers/workarea.py` | Source-checkout and reference-mirror management |
| `bits_helpers/download.py` | Tarball download helpers |
| `bits_helpers/log.py` | Logging and progress output |
| `bits_helpers/cmd.py` | Subprocess execution helpers; `DockerRunner` |
| `bits_helpers/resource_manager.py` | Resource-aware build scheduling |
| `templates/` | Example Jinja2 templates for the templating plugin (`bits build … --plugin templating < template`); the generated build script itself comes from `bits_helpers/build_template.sh` |
| `tests/` | Full test suite |
| `docs/` | MkDocs documentation source |

---

## 12. Writing Recipes

A recipe is a file named after its package in lower case (`<package>.sh`, e.g. `python.sh` for `Python`), placed in a recipe repository (by convention a `*.bits` directory). It has two sections separated by a line containing only `---`:

1. A **YAML header** — package metadata, dependencies, and environment.
2. A **Bash build script** — the actual build steps.

### Minimal recipe

```yaml
package: zlib
version: "1.2.13"
source: https://github.com/madler/zlib.git
tag: v1.2.13
---
# The script runs in $BUILDDIR; build from a copy, never inside $SOURCEDIR
rsync -a --delete --exclude '**/.git' "$SOURCEDIR"/ ./
./configure --prefix="$INSTALLROOT"
make -j${JOBS:-1}
make install
```

### CMake-based package

```yaml
package: opencv
version: "4.5.3"
source: https://github.com/opencv/opencv.git
tag: "4.5.3"
requires:
  - zlib
  - jpeg
build_requires:
  - cmake
  - ninja
---
cmake -S "$SOURCEDIR" -B "$BUILDDIR" \
      -DCMAKE_INSTALL_PREFIX="$INSTALLROOT" \
      -DCMAKE_BUILD_TYPE=Release
cmake --build "$BUILDDIR" --parallel ${JOBS:-1}
cmake --install "$BUILDDIR"
```

### Annotated Boost recipe (showing environment fields)

```yaml
package: boost
version: "1.82.0"
source: https://github.com/boostorg/boost.git
tag: boost-1.82.0
requires:
  - zlib
  - bzip2
build_requires:
  - Python
env:
  BOOST_INCLUDEDIR: "$BOOST_ROOT/include"
prepend_path:
  ROOT_INCLUDE_PATH: "$BOOST_ROOT/include"
---
rsync -a --delete --exclude '**/.git' "$SOURCEDIR"/ ./
./bootstrap.sh --prefix="$INSTALLROOT" --with-python=$(which python3)
./b2 -j${JOBS:-1} \
     --build-dir="$BUILDDIR" \
     --prefix="$INSTALLROOT" \
     variant=release link=shared install
```

`env`, `prepend_path` and `append_path` values are written into the package's `etc/profile.d/init.sh` and evaluated when that file is sourced (e.g. while a dependent package builds). Refer to the install location as `$<PACKAGE>_ROOT` (here `$BOOST_ROOT`, which bits exports automatically), not `$INSTALLROOT`, which is only valid while the package itself builds. bits already prepends the package's `bin`, `lib`/`lib64` and `lib*/pkgconfig` directories to `PATH`, `LD_LIBRARY_PATH` (`DYLD_LIBRARY_PATH` on macOS), `LIBRARY_PATH` and `PKG_CONFIG_PATH`, so these need not be listed.

For the complete list of YAML header fields and build-time environment variables see [§17 Recipe Format Reference](#17-recipe-format-reference).

### Function-based recipes with bits-recipe-tools

The optional [`bits-recipe-tools`](https://github.com/bitsorg/bits-recipe-tools) package provides a higher-level authoring style using reusable shell function hooks (`CMakeRecipe`, `AutoToolsRecipe`, etc.). Instead of writing a flat Bash build script, you override only the lifecycle hooks that differ from the defaults (`Prepare`, `Configure`, `Make`, `MakeInstall`, `PostInstall`). See [Writing Recipes with bits-recipe-tools](COOKBOOK.md#writing-recipes-with-bits-recipe-tools) in the Cookbook for worked examples.

---

## 13. Repository Provider Feature

A **repository provider** is a recipe that, instead of describing a software package to build, describes *another recipe repository* to load dynamically at dependency-resolution time.

### Why it exists

Normally the set of recipe repositories searched is fixed at startup: the config directory plus whatever `BITS_PATH` (or `--search-path`) names. The repository provider feature lets a recipe itself pull in an additional recipe repository from git, enabling modular recipe sets and nested providers.

### Defining a repository provider

Add these fields to any recipe's YAML header:

```yaml
package: my-extra-recipes
version: "1.0"
source: https://github.com/myorg/my-extra-recipes.git
tag: v1.0

# Mark this recipe as a repository provider
provides_repository: true

# Where to insert the checkout in BITS_PATH: append (default) or prepend.
# prepend takes effect only when granted with --provider-policy (see Provider policy).
repository_position: append

# Optional integrity pin: the build fails if `tag` no longer resolves to this commit
# (full hash or a prefix of at least 7 characters).
# commit: 1a2b3c4d
```

The `source` URL must point to a git repository whose top-level directory contains `*.sh` recipe files (the same layout as any other `*.bits` directory). `tag` selects the branch, tag or commit to check out (default: `version`). A provider repository is loaded once per build; a `requires` entry that asks for a different version of an already-loaded provider is ignored, with a warning.

### Always-on providers (`always_load: true`)

A provider recipe can be marked to load unconditionally — before the dependency graph is even traversed — by setting `always_load: true` alongside `provides_repository: true`:

```yaml
package: shared-recipes
version: "1"
source: https://github.com/myorg/shared-recipes.git
tag: stable
provides_repository: true
always_load: true
repository_position: prepend   # honoured only with --provider-policy shared-recipes:prepend
```

Any recipe file in the primary config directory (`-c`/`--config-dir`) that has both flags set is cloned and added to `BITS_PATH` at startup, making its recipes visible to all subsequent dependency resolution without any package needing to declare an explicit dependency on it. This is the recommended way to distribute a curated set of approved recipes across a team.

### The `bits-providers` standard repository

Bits ships a **built-in default provider** pointing at the official `bitsorg/bits-providers` repository on GitHub. This repository is the provider registry: it maps each community name to its recipe repository (e.g. `alice.bits.sh`, `lhcb.bits.sh`). Native `bits` loads it automatically on every build; under the `aliBuild` wrapper it is off unless `BITS_PROVIDERS` is set:

```
BITS_PROVIDERS=https://github.com/bitsorg/bits-providers  (default)
```

**Overriding the default** (an empty value falls back to the default; native `bits` cannot switch the registry off):

```bash
# Use a private provider repository instead
export BITS_PROVIDERS=https://github.com/myorg/my-recipes.git@main

# Or for one run only
BITS_PROVIDERS=https://github.com/myorg/my-recipes.git@stable bits build ROOT

# Pin to a specific tag
export BITS_PROVIDERS=https://github.com/bitsorg/bits-providers@v2.0
```

The `@tag` suffix is optional; when omitted, `main` is used. Everything after the first `@` is taken as the tag, so use an `https://` URL here; an SSH URL such as `git@github.com:org/repo` is not supported.

### Front-end choice: native `bits` (provider path) vs `aliBuild` (legacy path)

Which path is used is chosen by the front-end:

- **Native `bits`** uses the **provider path**: `$BITS_PROVIDERS` defaults to the official `bitsorg/bits-providers` registry, so the registry is loaded on every build and a missing recipe directory can be bootstrapped from it (see below).
- **The `aliBuild` wrapper** (it exports `BITS_BRANDING=aliBuild`, `BITS_COMMUNITY=ALICE` and `BITS_REPO_DIR=alidist`) emulates **legacy aliBuild**: the providers default is *empty*, so no registry is loaded and recipes come from a local `alidist` checkout instead. `aliBuild init` clones `alisw/alidist`, `aliBuild build <PKG>` uses it directly, and the legacy build-time `init.sh` is kept (`BITS_LEGACY_INITDOTSH=1`, alidist-compatible hashes; `--legacy-initdotsh` selects it explicitly).

An explicit `$BITS_PROVIDERS` overrides the default in either mode.

### Bootstrapping a recipe repository from the registry

When `-c`/`--config-dir` names a recipe directory that does not exist, native `bits build` bootstraps one through the registry (the default config dir is `.`, which always exists, so this applies only to an explicit `-c`): it follows the `<community>.bits.sh` pointer (`$BITS_COMMUNITY`) in `bits-providers` and clones the recipe repo it names under the work directory; with no community set nothing is bootstrapped. That pointer recipe's own `requires` are then seeded into provider discovery, so a base provider it depends on (e.g. `alice.bits` `requires: [alidist.bits]`) is loaded too — even though it is not a dependency of the package being built.

To check out a recipe repository explicitly for development, name it on `bits init` (the `.bits` convention):

```bash
bits init alice.bits           # resolve alice.bits in the registry, clone it into ./alice.bits
bits init -c alice.bits ROOT   # develop a package beside it (incl. one from a required provider repo)
bits build -c alice.bits ROOT
```

`bits init -c <group> <pkg>` loads the provider chain and seeds it with the checked-out group's registry `requires`, so a package whose recipe lives in a required provider repository (e.g. `ROOT` in `alidist.bits`) is found and checked out side by side.

### Auto-synthesised `bits-providers` package

When `BITS_PROVIDERS` is set (explicitly or via the built-in default), bits automatically synthesises and loads a virtual package named **`bits-providers`** equivalent to writing the following recipe by hand:

```yaml
package: bits-providers
version: "1"
source: <BITS_PROVIDERS URL>
tag: <BITS_PROVIDERS tag>          # defaults to "main"
provides_repository: true
always_load: true
repository_position: append        # prepend only with --provider-policy bits-providers:prepend
```

It is loaded first, before the dependency-driven scan, so its recipes are visible from the very first dependency-resolution pass. The name `bits-providers` is reserved: while `BITS_PROVIDERS` is in effect, an always-load recipe of that name in the config directory is skipped, so the registry is not cloned twice.

### Provider configuration

The active provider set is configured through the environment, not a config
file. Set `$BITS_PROVIDERS` to replace the built-in default (an empty value falls back
to it; the registry is off only under the `aliBuild` wrapper). `bits init
--providers` only points you at the variable; no command takes a per-run
`--providers`.

```bash
export BITS_PROVIDERS=https://github.com/myorg/my-recipes.git@stable
```

### Provider policy

By default every repository-provider's checkout is **appended** to `BITS_PATH`, regardless of what its `repository_position` field declares.  This is the safe default: an appended provider can only add new recipes, never silently replace an existing one.

A provider that needs to appear *before* other directories — for example to shadow a recipe in the default repository with a patched version — must be explicitly granted `prepend` access by whoever runs the build, with `--provider-policy`.  Provider recipes cannot self-elevate.

#### Configuration

Grant prepend access with the `--provider-policy` flag (format
`name:prepend|append`, entries comma-separated):

```bash
# Grant one provider prepend access; keep all others at the safe default.
bits build --provider-policy bits-providers:prepend MyPackage

# Multiple entries are comma-separated.
bits build --provider-policy bits-providers:prepend,myorg-extras:append MyPackage
```

Persist it for the current directory with `bits use build --provider-policy …`.
Only `bits build` takes this flag.

#### How position is resolved

For each provider, bits evaluates the policy in this order:

| Priority | Source | Effect |
|----------|--------|--------|
| 1 (highest) | `provider_policy` entry for this provider | Exact position used, overrides recipe |
| 2 | Recipe's `repository_position` field, **only if `append`** | Respected as-is |
| 3 (default) | Recipe's `repository_position: prepend` **without policy** | Downgraded to `append`; a warning names the required `--provider-policy` entry |
| 4 | No field in recipe | `append` |

When a provider is about to be prepended (which needs a policy grant), bits compares its recipes with those in the directories already on `BITS_PATH` and warns, listing every recipe it will shadow. The warning is informational and is shown even when the prepend was granted.  The primary config directory (passed via `-c / --config-dir`) is always position 0 in the search order and **cannot** be shadowed by any provider.

#### Example: patching a default recipe

Suppose `myorg-patches` contains a modified `zlib.sh` that you want to take precedence over the version in the upstream provider:

```bash
bits build --provider-policy myorg-patches:prepend ROOT
# WARNING: Provider 'myorg-patches' is being prepended and will shadow 1 recipe(s)
#   already visible from /path/to/bits-providers: zlib
# (expected and intended — the warning is informational)
```

### Precedence for `BITS_PROVIDERS`

| Priority | Source | Example |
|----------|--------|---------|
| 1 (highest) | `BITS_PROVIDERS` environment variable (non-empty) | `export BITS_PROVIDERS=…` |
| 2 (default) | Built-in default (none under the `aliBuild` wrapper) | `https://github.com/bitsorg/bits-providers` |

### How providers are discovered (two-phase)

`bits build` loads providers in two phases before it resolves the full dependency graph:

**Phase 1 — always-on providers:**

1. If `BITS_PROVIDERS` is in effect, clone the registry as the `bits-providers` package and add it to `BITS_PATH` (appended unless `--provider-policy bits-providers:prepend` is given).
2. Clone every recipe in the config directory that has both `provides_repository: true` and `always_load: true` (skipping `bits-providers` if already handled).

**Phase 2 — iterative dependency-driven scan:**

The scan is seeded with the union of:
- the user-requested packages, and
- any top-level `requires` / `build_requires` declared in the active defaults file(s), and
- when the recipe directory was just bootstrapped from the registry, the `requires`/`build_requires` of its registry pointer recipe.

This second seed is what allows a defaults file to trigger provider loading (see [Triggering providers from a defaults file](#triggering-providers-from-a-defaults-file) below).

1. Walk the dependency graph from the seeded list.
2. When a package with `provides_repository: true` is encountered for the first time, clone its source repository into the cache and add the checkout to `BITS_PATH`.
3. Restart the walk — recipes newly visible on the extended path (including further providers) are now reachable.
4. Repeat until stable (no new providers found), for at most 20 restarts.

This naturally handles **nested providers**: a provider whose own recipe repository contains a further provider recipe.

During the walk, an `overrides:` entry for a provider in the active defaults (`source` and/or `tag`, with `%(variable)s` expansion) is applied before cloning, so a defaults profile can point a provider at a fork or branch. Because defaults files can themselves live in provider repositories, bits then re-reads the defaults with the providers on the search path and repeats the scan until the set of provider commits stops changing.

**Local checkout shadowing.** Before cloning a declared provider `<pkg>` (in either phase, except the registry itself), bits looks for `<config_dir>/<pkg>/`. If that directory contains `*.sh` recipes it is used instead of a clone, with its git `HEAD` recorded as provenance (plus `-dirty` if it has uncommitted changes). So a provider you are editing (e.g. `lcg.bits/` next to `lcg.bits.sh`) is picked up as-is, just as a local package checkout shadows its `source`. Only declared providers are shadowed; bits never picks up undeclared `*.bits/` directories this way. `--force-tracked` disables this (and all local-checkout pickup), forcing the remote clone. For an *undeclared* local sub-repo, put it on `BITS_PATH` with `--search-path NAMES`.

### Triggering providers from a defaults file

A defaults file can load a repository provider for all builds that use it by declaring the provider in a top-level `requires` or `build_requires` field:

```yaml
package: defaults-gcc13
version: "1"

# Pull in the organisation's recipe repository on every build that uses
# defaults-gcc13, even if no individual package lists it as a dependency.
requires:
  - myorg-recipes      # must have provides_repository: true in its .sh file
```

The provider's recipe (`myorg-recipes.sh`) must be findable on the search path: in the config directory, in an always-on provider, or in a provider loaded earlier in the scan. Once cloned, its recipes are visible to all subsequent dependency resolution.

> **Important — provider packages only.** A defaults file's top-level `requires` / `build_requires` only seed the Phase 2 provider scan; they do **not** become build dependencies. Every package already depends on `defaults-release`, so letting the defaults depend on packages would create a cycle (`defaults-release → provider-pkg → defaults-release`); bits therefore drops these fields from the defaults before resolving the build graph. The provider repositories are already on `BITS_PATH` by then, so nothing is lost.

This is subtly different from `always_load: true` on the provider recipe itself:

| Mechanism | When it fires | Scope |
|-----------|--------------|-------|
| `always_load: true` on the provider | Every build, unconditionally | Global — applies regardless of which defaults are active |
| `requires: [provider]` in a defaults file | Only when that defaults profile is active | Per-defaults — different profiles can load different providers |

Both mechanisms are fully backward-compatible: existing defaults files without a top-level `requires` are unaffected.

### Cache layout and staleness

Provider checkouts are cached under the work directory so that identical commits are never re-cloned:

```
$BITS_WORK_DIR/
  REPOS/
    <package-lower>/          one directory per provider package
      <short_commit_hash>/    the actual checkout  (cache key = commit hash)
        .bits_provider_ok     written only after a successful checkout
        *.sh                  recipe files live here
      latest -> <hash>        symlink to the most-recently used entry
```

A checkout is reused (cache hit) when `.bits_provider_ok` already exists for the resolved commit hash. If the recipe's `tag` resolves to a new commit, a fresh checkout is made alongside the old one; no stale data is ever overwritten.

**Staleness detection:** Once a provider has a cached checkout, bits refreshes its git mirror on every run (even with `--no-fetch-repos`) so that tag advances in the upstream repository are always detected. This ensures that a team-wide recipe update published as a new tag is picked up on the next build without any manual cache purge.

### Effect on build hashes

A provider's commit does **not** enter the build hash. Each package is hashed from its own inputs (recipe text, sources, patches and its dependencies' hashes), so a new provider commit rebuilds only the packages whose recipes changed (and their dependents), not every package from that repository. The provider name and commit used for each package are still recorded in the build manifest for provenance.

---

## 14. Writing and Running Tests

Tests live in the `tests/` directory and use Python's built-in `unittest` framework.

```bash
# Run the full suite
python -m unittest discover -s tests -p "test_*.py" -v

# Run a single test file
python -m unittest tests/test_repo_provider.py -v

# Run a single test class or method
python -m unittest tests.test_build.BuildTestCase.test_hashing -v
```

If `pytest` is available:

```bash
pytest tests/ -v
tox -e py312   # one Python version on Linux (envlist: py38–py314)
tox -e darwin  # the macOS environment
# tox also runs bitsBuild integration commands that clone alidist from GitHub (needs network)
```

### Test file overview

A selection of the main test files (`tests/` holds over 100):

| Test file | What it covers |
|-----------|---------------|
| `test_args.py` | CLI argument parsing (legacy tests) |
| `test_new_args.py` | New CLI arguments: `bits prune`/`cleanup` subparser, `--cvmfs-prefix`, `--no-relocate`; store/publish group renames; backward-compatibility assertions |
| `test_cleanup.py` | `bits_helpers/cleanup.py`: sentinel paths, LRU eviction, age-based eviction, disk-pressure mode, flock concurrency safety |
| `test_container_workdir.py` | `container_workDir` / `cachedTarball` path rewriting logic in `build.py`; all four flag combinations; `re.escape()` correctness for paths with regex metacharacters |
| `test_always_on_providers.py` | `_parse_provider_url`, `_make_bits_providers_spec`, `load_always_on_providers` (BITS_PROVIDERS path, `always_load` scan, double-clone prevention, failure isolation) |
| `test_defaults_requires_provider.py` | `parseDefaults` propagating top-level `requires`; defaults-provider seed construction; provider discovery seeded from defaults requires; backward compatibility |
| `test_build.py` | `doBuild` integration, hash computation, build script generation |
| `test_clean.py` | Stale-artifact detection and removal |
| `test_cmd.py` | `DockerRunner` and subprocess helpers |
| `test_deps.py` | Dependency graph generation |
| `test_git.py` | Git SCM wrapper |
| `test_pkg_to_shell_id.py` | `pkg_to_shell_id` sanitisation (dots, dashes, `@`, `+`); `generate_initdotsh` export correctness for dot-in-package-name |
| `test_provider_staleness.py` | Mirror always refreshed when cache exists; upstream tag advances detected; `fetch_repos=False` respected on first run |
| `test_qualify_arch.py` | `compute_combined_arch`: legacy `qualify_arch` and new per-default `append_arch`; end-to-end through `effective_arch`, install path, and `init.sh` generation |
| `test_repo_provider.py` | Repository provider: `getConfigPaths` absolute paths, `_add_to_bits_path`, `clone_or_update_provider` caching, iterative discovery, nested providers, provider commit recorded on specs but kept out of the build hash |
| `test_sync.py` | Remote store backends (requires `botocore` for S3 tests) |

### Guidelines for new tests

- Mock all network and filesystem side-effects; tests must pass offline.
- Place provider/SCM fixtures in `tempfile.mkdtemp()` directories cleaned up in `tearDown`.
- Use `unittest.mock.patch.object` to replace module-level functions (not `assertLogs` when the bits `LogFormatter` is active — patch `warning` directly instead).

---

## 15. Contributing

### Workflow

- Open an issue at `https://github.com/bitsorg/bits/issues` before starting non-trivial work so effort isn't duplicated.
- Fork the repository, create a feature branch from `main`, and open a pull request when ready.
- All tests must pass (`tox -e py3XX` on Linux, `tox -e darwin` on macOS) before a PR is merged.
- The main development branch is `main`; do not target `stable` or release branches directly.

### Code style

- Follow the code style enforced by `.flake8` and `.pylintrc`; run both before pushing.
- Write docstrings for all new public functions and classes.
- Keep the code Python 3.8 compatible (e.g. no `str.removeprefix`, no `ast.unparse`).
- Prefer small, focused commits; each commit should leave the test suite green.

### Which document to update

| What changed | Update |
|---|---|
| Installation, quick start, configuration, `bits enter/load/clean` usage | `docs/USERGUIDE.md` |
| Practical how-to examples for common tasks | `docs/COOKBOOK.md` |
| CLI flags, recipe YAML fields, environment variables, architecture/store/Docker internals | `docs/REFERENCE.md` (this file) |
| End-to-end development-to-CVMFS workflow | `docs/WORKFLOWS.md` |
| Planned features, design decisions, known limitations | `docs/ROADMAP.md` |

When a change affects the public CLI (new flag, renamed option, changed default), also update the relevant entry in [§16 Command-Line Reference](#16-command-line-reference) and the short description in README.md.

### License

The project is licensed under the terms in `LICENSE.md`.

---

# Part II — Technical Reference

## 16. Command-Line Reference

All sub-commands are accessed through the unified `bits` entry point:

```
bits [--debug|-d] [--dry-run|-n] <subcommand> [options]
```

| Global option | Description |
|---------------|-------------|
| `-d`, `--debug` | Enable verbose debug output |
| `-n`, `--dry-run` | Print what would happen without executing. For `bits build`: a per-package plan (installed, local tarball, from the remote store, from the reuse overlay, or build) and a summary. Nothing is downloaded: the remote store is only listed, which is possible for `http(s)://` and `b3://` stores. |
| `-a ARCH`, `-w DIR`, `--no-refresh` | Module commands (`enter`, `load`, `q`, …) only: architecture, work directory, and skip refreshing the modules directory. |

Saved arguments from a `bits use` profile are inserted right after the sub-command, before your own (see [bits init](#bits-init)); `bits.rc` is no longer read.

---

### bits build

Build one or more packages and all their dependencies.

```bash
bits build [options] PACKAGE [PACKAGE ...]
```

| Option | Description |
|--------|-------------|
| `--defaults PROFILE` | Defaults profile(s); use `::` to combine (e.g. `release::myproject`). Default: `release`. |
| `--flavour NAME[=VALUE]` (aliases `--flavor`, `--set`) | Set a build-wide flavour variable (repeatable, comma-separated). `NAME`→`true`, `NAME=VALUE`→`VALUE`, `!NAME`→`false`. Gates `(?NAME)` conditional requires/sources/patches and is exported into the build environment; overrides a defaults `variables:` entry of the same name. See [Flavours](#flavours). |
| `--reuse-from PATH\|cvmfs` | Reuse components already deployed on CVMFS, found through their published modulefiles at this absolute modules-tree path (unlike `--remote-store`, which is a tarball store). The literal `cvmfs` takes the location from the defaults `system:` layout (`module_dir` under `cvmfs_dir`) or, failing that, from `cvmfs_modules_template`. A trailing `::relaxed` or `::strict` also sets the reuse policy (e.g. `cvmfs::relaxed`); it must agree with `--reuse-policy` if both are given. Defaults to `$BITS_REUSE_FROM`. See [Reusing deployed components](#relaxed-cvmfs-reuse). |
| `--reuse-policy {strict,relaxed}` | How a `--reuse-from` component is matched. `strict` (default): only on an exact content-hash match; the result is publishable. `relaxed`: any deployed build of the **same version** (revision and hash may differ), for fast local development on top of e.g. an LCG release, so only the top of the stack is built. Relaxed builds have *loose* provenance and cannot be published: combining them with `--write-store` is refused. Falls back to the defaults `reuse_policy:` value. |
| `--build-local PKG[,PKG…]` | Packages to always build locally even when they could be reused (e.g. one you need patched), instead of taking them from `--reuse-from`. |
| `-a ARCH`, `--architecture ARCH` | Target architecture. Default: auto-detected, or the `architecture:` template from defaults (see [The `architecture:` template](#architecture-string-and-the-architecture-template)). An explicit value here overrides the template. |
| `--force-unknown-architecture` | Proceed even if architecture is unrecognised. |
| `--day DAY` | Value of the `{day}` nightly CVMFS path slot (e.g. `Fri`). Default: the UTC weekday, filled in only when a template uses `{day}`; `''` collapses the slot. Layout only — never hashed. See [bits store / bits cvmfs](#bits-store--bits-cvmfs-admin--ci-groups). |
| `-j N`, `--jobs N` | Parallel compilation jobs per package. Default: CPU count. |
| `--no-auto-patch` | Do not apply recipe `patches:` automatically for any package in this build. The patch files are still staged in `$SOURCEDIR` and exported as `$PATCH0..$PATCH_COUNT`; each recipe must then apply them itself (e.g. with the `bits_apply_patches` helper). A single recipe or a defaults profile can also opt out with `auto_patch: false`. See [Controlling patch application](#controlling-patch-application). |
| `--parallel [N]` | Number of packages to build at the same time. Bare `--parallel` uses 4; without it the build is serial (the default). With N>1 each build's `$JOBS` is divided across the builders so that together they do not oversubscribe the machine (see [Memory- and load-aware parallelism](#memory-aware-parallelism)). `--builders` is a kept alias. |
| `--oversubscribe FACTOR` | CPU oversubscription factor (≥ 1.0) for the per-builder `-j` share: each package gets `ceil(jobs × FACTOR ÷ parallel)`, still clamped to `-j` and to the (unscaled) memory cap. Falls back to `build_oversubscribe:` in the active defaults, then 1.0. |
| `--unleash-final` / `--no-unleash-final` | The final (top-level) package depends on all the others, so it always builds last and alone. Unleashing (on by default) gives it the full `-j` instead of the per-builder share; the `mem_per_job` cap still applies. `--no-unleash-final` keeps it on the per-builder share. Falls back to `build_unleash_final:` in the defaults `system:` block. Only affects `--parallel` > 1. |
| `--legacy-initdotsh` / `--initdotsh-from-modules` | How each build's **dependency environment** is set up. Default (`--initdotsh-from-modules`): derived from the dependencies' modulefiles, the same environment used at run time, so recipes need not rebuild `PYTHONPATH`, include paths etc. by hand. `--legacy-initdotsh`: the old build-time `init.sh`. The choice **changes package hashes**: legacy mode gives the same hashes as before, so bits can still reuse **alidist** tarballs. Legacy mode can also be selected with `BITS_LEGACY_INITDOTSH=1` (the aliBuild compatibility wrapper sets it). |
| `--critical-path-schedule` / `--no-critical-path-schedule` | Start ready `--parallel` jobs in order of their **critical path**: the longest chain, weighted by recorded build times, from the job to the final target, so the slowest chain starts as early as possible. Build times come from a previous run's `bits_build_stats.json`; without history, depth in the dependency graph is used. **On by default**; `--no-critical-path-schedule` starts jobs in the order they were queued. Falls back to `build_critical_path_schedule:` in the defaults `system:` block. Changes only the order, never what is built or any hash. |
| `--build-nice` / `--no-build-nice` | Give concurrent `--parallel` builds staggered OS priorities so CPU contention degrades gracefully: one build runs at full priority and the others are progressively lowered; when a build finishes, the next one moves up. Native builds run under `nice -n N`; `--docker`/podman builds get `docker run --cpu-shares=W`. Memory is still capped separately by `mem_per_job`. **Off by default**; only affects `--parallel` > 1. |
| `--build-nice-step N` | Nice increment between concurrent build slots when `--build-nice` is set: slot *k* → nice `min(k×N, 19)`. `N=1` gives a gentle `0,1,2,3` ladder; larger values separate the slots more aggressively. Default: 5. |
| `--build-nice-boost-after SECONDS` | With `--build-nice`, a watchdog raises the priority of one long-running straggler at a time, so a single heavy compile does not drag out the end of the build. Native builds: the longest-running lowered build is reniced towards 0 (needs root or `CAP_SYS_NICE`; otherwise a logged no-op). `--docker`/podman builds: a compiler process (e.g. `cc1plus`, `f951`) running longer than this is reniced as root inside the container; this **needs `ps` (package `procps`) in the build image**, see the note below. `0` disables. Default: 600. |
| `--prefetch-workers N` | Spawn *N* background threads to fetch remote tarballs and source archives ahead of the main build loop, so downloads overlap with compilation instead of blocking the serial preparation pass. Default: `-1` (auto — scales with `--parallel`, capped at 4); `0` disables. No effect without `--remote-store`. |
| `--parallel-downloads N` | Maximum concurrent source/tarball downloads the `--parallel` scheduler runs as standalone download tasks (so a checkout overlaps the previous package's build). Default: 2. |
| `--auto-resources` | Opt-in for `--parallel` > 1: load the per-package CPU and memory statistics recorded by a previous run, and turn on monitoring to refresh them, so a new build starts only while the machine still has CPU and memory to spare. Off by default (concurrency is then limited only by `--parallel`); explicit `--resources` / `--resource-monitoring` still apply. |
| `--brew` | **macOS only.** Let a recipe that sources a system library from Homebrew run `brew install <formula>` on demand (during dependency resolution) when the formula is missing. Without it, such a recipe fails with a message naming the formula to install. Exported to recipe `prefer_system_check` scripts as `BITS_BREW=1`. See [macOS Homebrew system layer](#macos-homebrew-system-layer). |
| `--parallel-sources N` | Download up to *N* `sources:` URLs concurrently within a single package checkout. Default: 1 (sequential). |
| `-e KEY=VALUE` | Extra environment variable binding (repeatable). |
| `-z PREFIX`, `--devel-prefix PREFIX` | Version prefix for development packages. |
| `-u`, `--fetch-repos` | Fetch updates into the source mirrors before building, so a branch (`tag: main`) builds its current commit. The default; a mirror that cannot be fetched (offline) is used as it is, with a warning. |
| `--no-fetch-repos` | Do not fetch: a branch builds the commit its mirror already has. Missing mirrors are still cloned. |
| `--no-local PACKAGE` | Do not use a local checkout for PACKAGE (repeatable). |
| `--force-tracked` | Do not pick up any package from a local checkout. |
| `--force-rebuild PACKAGE` | Always rebuild PACKAGE from scratch (repeatable; same as `force_rebuild: true` in its recipe). |
| `--only-deps` | Build only the dependencies, not the requested package (e.g. to warm a cache). |
| `--annotate PACKAGE=COMMENT` | Store COMMENT in PACKAGE's build metadata when it is built or downloaded in this run. |
| `--plugin PLUGIN` | Plugin that performs the actual build. |
| `-C DIR`, `--chdir DIR` | Change to DIR first (or set `$BITS_CHDIR`). |
| `-w DIR`, `--work-dir DIR` | Work/output directory. Default: `sw`. |
| `-c DIR`, `--config-dir DIR` | Directory containing recipe files. Default: `.`. |
| `--search-path NAMES` | Comma-separated recipe sub-repos to search besides the config dir. A relative NAME resolves to `<config-dir>/NAME.bits`; absolute paths are used as-is. Seeds `BITS_PATH` (an explicit `$BITS_PATH` wins). |
| `--reference-sources DIR` | Directory for the local mirrors of git repositories. Default: `<work-dir>/MIRROR`. |
| `--remote-store URL` | Binary store to fetch pre-built tarballs from. Append `::rw` to also upload to it (cannot be combined with `--write-store`). On supported architectures the public CERN store is the default; a `remote_store:` entry in the defaults `system:` block replaces that default. |
| `--write-store URL` | Binary store to upload built tarballs to. Given alone (no `--remote-store`), it is also the read store, except where a default remote store applies (then use `--remote-store URL::rw`). |
| `--no-remote-store` | Disable the default remote store; a `--write-store` is still read from. |
| `--insecure` | Do not validate TLS certificates of an `https://` store. |
| `--sign-manifest KEY.pem`, `--trust-manifest URL\|PATH`, `--require-signed-reuse` / `--no-require-signed-reuse`, `--trust-groups G1,G2` | Signed reuse of remote tarballs: on by default (fail-closed), trust derived from the store's signed manifests. See [Signing and verifying the archive tier](#signing-and-verifying-the-archive-tier). |
| `--reuse-beacon URL` | Report reused-from-store hashes to `<URL>/api/reuse` (best-effort; falls back to `$BITS_REUSE_BEACON`). |
| `--monitor` / `--no-monitor`, `--monitor-url URL`, `--monitor-interval SECS`, `--monitor-disk-interval SECS`, `--monitor-instance NAME` | Best-effort background sampler of the build host (load, memory, filesystem, `sw/` size) and the building packages, pushed to a VictoriaMetrics/Prometheus URL (`<URL>/api/v1/import/prometheus`; falls back to `$METRICS_URL`, then the system `monitor_url`). Off by default (bits-console enables it); never blocks or fails the build. Intervals default to 15 s and 60 s. |
| `--s3-endpoint URL` | S3 endpoint for `b3://` stores. Overrides `$S3_ENDPOINT_URL` / `$AWS_ENDPOINT_URL_S3`; default `https://s3.cern.ch`. Set for a **non-CERN** bucket (AWS, MinIO, Ceph RGW). |
| `--s3-access-key KEY` | S3 access key id. Overrides `$AWS_ACCESS_KEY_ID` (prefer the env var — a CI/CD variable or gitlab-runner `environment` entry — so the secret is not on the command line). |
| `--s3-secret-key KEY` | S3 secret access key. Overrides `$AWS_SECRET_ACCESS_KEY`. |
| `--s3-region REGION` | S3 region. Overrides `$AWS_DEFAULT_REGION`. |
| `--s3-addressing-style {auto,path,virtual}` | S3 addressing style for `b3://` stores. MinIO usually needs `path`. Overrides `$S3_ADDRESSING_STYLE`. |
| `--disable PACKAGE` | Do not build PACKAGE nor the dependencies only it needs (repeatable or comma-separated). |
| `--prefer-system` | Always prefer system packages where supported. (`--always-prefer-system` is a kept alias.) |
| `--no-system [PACKAGES]` | Never use system packages for the comma-separated PACKAGES; bare, for any package. |
| `--docker` | Build inside a Docker container. |
| `--docker-image IMAGE` | Docker image to use. Implies `--docker`. Default: derived from the architecture and registry (see [§22](#22-docker-support)). |
| `--docker-extra-args ARGS` | Extra arguments for `docker run`. Implies `--docker`. bits always adds `--network=host` and, unless given, `--cpuset-cpus=<host CPUs>`. |
| `-v VOLUME` | Additional volume to mount in the container (repeatable; passed to `docker run`). |
| `--cvmfs-prefix PATH` | Bind-mount the work directory at `PATH` inside the container so packages compile with their final CVMFS paths embedded and need no relocation at publish time. Only used with `--docker`. See [No-relocation builds](#no-relocation-builds-with---cvmfs-prefix). |
| `--container-use-workdir` | Mount the work directory at the same absolute path inside the container (without this flag it is mounted at `/container/bits/sw`). Ignored when `--cvmfs-prefix` is set. |
| `--docker-platform PLATFORM` | Docker `--platform` for cross-compilation (e.g. `linux/arm64`). Inferred automatically from `--architecture`; pass `native` to suppress. Requires QEMU binfmt handlers. See [§22.2 Cross-compilation via QEMU](#222-cross-compilation-via-qemu). |
| `--sandbox MODE` | Sandbox recipe builds: `off` (default), `auto`, `podman`, or `sandbox-exec` (macOS). See [§22.1 Recipe Sandbox](#221-recipe-sandbox). |
| `--sandbox-image IMAGE` | Container image for `--sandbox=podman` when not using `--docker`. Implies `--sandbox=podman`. |
| `--sandbox-network MODE` | Default network restriction inside the sandbox: `on` blocks network access, `off` allows it. A recipe's `sandbox_network:` always wins; falls back to `sandbox_network:` in the active defaults, then `on`. No effect where sandboxing is off. |
| `--no-auto-cleanup` | Keep the build directories after a successful build. |
| `--aggressive-cleanup` | Delete as much build data as possible when cleaning up. |
| `--resource-monitoring` | Enable per-package CPU/memory monitoring. **Default: on when `--parallel` > 1**, off for serial builds. |
| `--no-resource-monitoring` | Disable per-package monitoring even when `--parallel` > 1. |
| `--resources FILE` | JSON resource-utilisation file for scheduling. |
| `--check-checksums` | Warn on source/patch checksum mismatch; continue the build. |
| `--enforce-checksums` | Abort on source/patch checksum mismatch or missing checksum. |
| `--print-checksums` | Print checksums for all sources/patches in YAML format after the build. |
| `--write-checksums` | Add the fetched sources', patches' and git tags' checksums to `checksums/<package>.checksum` after the build. For a whole repository, use `bits checksums`. |
| `--store-integrity` | Record the SHA-256 of each uploaded tarball and verify it every time the tarball is fetched again; a mismatch is fatal. Persist it with `bits use build --store-integrity`. See [§21 Store integrity verification](#store-integrity-verification). |
| `--provider-policy POLICY` | Control where each repository provider is inserted into `BITS_PATH`. Format: comma-separated `name:prepend\|append` pairs; every provider defaults to `append`, and this flag is the only way to grant `prepend`. See [§13 Provider policy](#provider-policy). |
| `--from-manifest FILE` | Rebuild the packages a manifest JSON file requested (`requested_packages`); versions and tarballs are resolved afresh, not pinned or checked against the manifest. `PACKAGE` is then optional: without it, the manifest's requested packages are built. See [§25 Build Manifest](#25-build-manifest). |

The three `--*-checksums` flags are mutually exclusive. Precedence (highest → lowest): the command-line flag > per-recipe `enforce_checksums: true` > `checksum_mode:` in the defaults profile > `off`. `--write-checksums` is independent, can be combined with any of them, and can also be turned on with `write_checksums: true` in the defaults profile. See [§18 — Checksum policy in defaults profiles](#checksum-policy-in-defaults-profiles).

**Build-image requirement: `procps`.** With `--build-nice` under `--docker`/podman, the straggler boost finds the compiler process by running `ps` inside the build container, so build images for `bits build --docker` should include the **`procps`** package (`procps-ng` on RPM distros). Without `ps`, bits warns once and skips in-container boosting; the build itself is unaffected. Native builds use `psutil` on the host and need no `ps` in any image.

#### S3 store: common CI/CD config with per-runner overrides

The store URL and its S3 connection can be configured entirely through the
environment, so that a bucket is defined once as a **common GitLab CI/CD
variable** and a specific `gitlab-runner` can **override** it locally. Because
GitLab makes a CI/CD variable win over a same-named runner `environment` entry,
the per-host override uses a distinct `BITS_`-prefixed name that bits consults
first. Precedence for every setting (highest first):

1. the command-line flag (`--remote-store` / `--write-store` / `--s3-*`);
2. `BITS_<NAME>` — the per-host override, set in the gitlab-runner `config.toml`
   `environment`;
3. `<NAME>` — the common value, set as a GitLab CI/CD variable;
4. for the store URL only, `remote_store:` in the active defaults' `system:` block;
5. the built-in default (CERN S3, public read store on supported architectures).

S3 credentials, endpoint and region not set by 1–3 are also read from
`~/.bits/s3keys` (see [S3-compatible via boto3](#s3-compatible-via-boto3-b3)).

| Setting | CLI flag | Common var (CI/CD) | Per-runner override (config.toml) |
|---------|----------|--------------------|-----------------------------------|
| Read store / bucket | `--remote-store` | `REMOTE_STORE` | `BITS_REMOTE_STORE` |
| Write store | `--write-store` | `WRITE_STORE` | `BITS_WRITE_STORE` |
| Endpoint | `--s3-endpoint` | `S3_ENDPOINT_URL` | `BITS_S3_ENDPOINT_URL` |
| Access key | `--s3-access-key` | `AWS_ACCESS_KEY_ID` | `BITS_AWS_ACCESS_KEY_ID` |
| Secret key | `--s3-secret-key` | `AWS_SECRET_ACCESS_KEY` | `BITS_AWS_SECRET_ACCESS_KEY` |
| Region | `--s3-region` | `AWS_DEFAULT_REGION` | `BITS_AWS_DEFAULT_REGION` |
| Addressing style | `--s3-addressing-style` | `S3_ADDRESSING_STYLE` | `BITS_S3_ADDRESSING_STYLE` |

A store set as `REMOTE_STORE=b3://mybucket::rw` (or the flag) reads from and
uploads to the same bucket. With no store and no env vars, behaviour is
unchanged: the public CERN read store on supported architectures, no upload.

**Client requirement — install on the build host, not in the container.** bits
runs the remote-store sync in its own host-side Python process (the system
`python3` that the `bits` wrapper invokes), even for `bits build --docker`; the
container only runs the per-package compile steps. So the S3 client must be
installed on each build (gitlab-runner) host:

- `b3://` stores need **boto3**:
  - Ubuntu/Debian: `sudo apt install python3-boto3`
  - AlmaLinux/RHEL/Fedora: `sudo dnf install python3-boto3` (from EPEL: `sudo dnf install epel-release` first)
  - or, respecting PEP 668: `pip3 install --break-system-packages boto3`
- `s3://` stores need the **s3cmd** binary and an `~/.s3cfg` instead.

The bits-console runner installer (`runner_installer.sh`, part of bits-console,
not of this repository) installs `python3-boto3` automatically on both Ubuntu
and AlmaLinux hosts.

---

### bits deps

Generate a visual dependency graph for a package (requires Graphviz), and/or a
Makefile listing its dependency tree. The virtual `defaults-release` package and
its edges are excluded from dependency output.

```bash
bits deps [options] PACKAGE
```

| Option | Description |
|--------|-------------|
| `--outgraph FILE` | Output PDF file. |
| `--outmake FILE` | Output the package's dependency tree as Makefile rules (`pkg: dep1 dep2`), one per package, dependencies first (alphabetical among equals), with no recipes; the target package is the last rule, so name it when running `make`. Does not require Graphviz. At least one of `--outgraph`/`--outmake` is required. |
| `--runtime-only` | With `--outmake`, follow only `requires` (runtime) dependencies plus `untracked_requires`, and skip `build_requires`, so build-only packages are left out. |
| `--defaults PROFILE` | Defaults profile(s); use `::` to combine (e.g. `release::myproject`). Default: `release`. |
| `-a ARCH` | Architecture for dependency resolution. |
| `--disable PACKAGE` | Assume PACKAGE, and the dependencies only it needs, are not built (repeatable or comma-separated). |
| `--prefer-system` | Resolve as if system packages are used when compatible. (`--always-prefer-system` is a kept alias.) |
| `--no-system [PACKAGES]` | Never use system packages for PACKAGES (bare: any). |
| `--neat` | Graph with transitive reduction. |
| `--outdot FILE` | Keep the intermediate Graphviz dot file. |
| `-e KEY=VALUE` | Extra environment binding (repeatable). |
| `--docker`, `--docker-image IMAGE`, `--docker-extra-args ARGS` | Check for system packages inside a Docker container, as `bits build --docker` would. |
| `-c DIR`, `--config-dir DIR`, `--search-path NAMES` | Recipe directory and extra recipe sub-repos, as for `bits build`. |

Colour coding in the generated graph: **gold** = requested top-level package; **green-yellow** = runtime-only dependency; **plum** = build-only dependency; **tomato** = both runtime and build dependency. Build-dependency edges are grey, runtime edges blue.

**Recorded at build time.** Each package's `.meta.json` also contains
`dependency_graph`: for every package in its runtime closure (itself included),
the list of its direct runtime dependencies, `untracked_requires` included and
build-only dependencies and `defaults-release` left out. Names and lists are
sorted alphabetically, so the graph does not depend on build order; installation
order can be derived from the edges. (Other recursive dependency lists in the
metadata are sorted too; direct lists keep the recipe's order.) Each entry also
records `pkg_family` and `effective_architecture` (`share` for noarch recipes,
otherwise the architecture the package was built for), while the top-level
`architecture` stays the build architecture. None of this enters the package
hash, but it does change the bytes of newly built tarballs, so existing signed
checksums do not cover rebuilt tarballs.

---

### bits doctor

Check that the system satisfies all requirements for the requested packages, validate the full build-runner environment with `--runner`, or probe the remote binary store with `--check-store`.

```bash
bits doctor [options]                        # is this machine set up to run bits?
bits doctor [options] PACKAGE ...            # recipe system-requirement check
bits doctor --runner [options]               # runner environment validation
bits doctor --check-store PACKAGE ...        # pre-build store availability report
```

**Setup mode** (no `PACKAGE`) checks the machine itself: the Python bits runs with and its modules (boto3 is required when a `b3://` store is given), running as root, git, a C++ compiler, the container engine behind `docker` (for rootless podman: delegated cgroup controllers, `/etc/subuid`/`/etc/subgid` ranges, storage not on AFS/NFS, SELinux), disk space, and each `--remote-store`/`--write-store` (reachability; for `b3://`, credentials and bucket access; for `s3://`, s3cmd and `~/.s3cfg`). Each check is PASS / WARN / FAIL / SKIP with a one-line fix; any FAIL gives exit code 1. `--json` is supported.

**Recipe-check mode** (with `PACKAGE`) evaluates each package's `system_requirement` and `prefer_system` snippets in the dependency tree and reports which packages can be satisfied by the host and which will be built by bits. The `PACKAGE` positional argument is required in this mode.

**`--runner` mode** skips the recipe scan and instead runs a structured checklist of the build-runner environment. Each check returns PASS / FAIL / WARN / SKIP. WARN is advisory; only FAIL affects the exit code. It is meant for CI build hosts: a missing Docker daemon is a FAIL, and it does not check Python modules or the write store, so on a workstation use setup mode instead.

| Check performed | When included |
|-----------------|---------------|
| `git` on PATH | always |
| C++ compiler (`c++`, `g++`, or `clang++`) | always |
| Docker daemon reachable | always |
| QEMU binfmt handler for the target architecture (`-a`) | always |
| Xcode Command Line Tools, XQuartz, Homebrew Brewfile | macOS only |
| podman availability and user-namespace support | always |
| CVMFS repository path(s) accessible and non-empty | `--cvmfs-repos` / `$BITS_CVMFS_REPOS` |
| Free disk space in `--work-dir` ≥ `--min-disk` GiB | always |
| Remote store reachable (`https://`: HEAD request; `s3://`: `~/.s3cfg` present; `b3://`: `AWS_ACCESS_KEY_ID` set) | when a remote store is configured (including the default store) |
| cvmfs-prepub service healthy (`GET <URL>/api/v1/health`) | `--prepub-url` |

**`--check-store` mode** runs the standard dependency-tree resolution (same as recipe-check mode), computes the expected tarball hash for each package bits would need to build, and probes the remote store to report which are pre-built. The report is informational: exit code is always 0. Use it to estimate how much of a build will compile vs. be downloaded.

Hash computation notes:

- For tagged releases (the common CI case) the hash is exact — the tag string deterministically identifies the commit.
- doctor does not fetch, so for branch builds the commit hash is approximated with "0". If the store probe shows FAIL for all packages in a branch build, re-run via `bits status --check-store` for accurate hashes.
- The store tarball path for an `https://` store follows the pattern: `{store}/TARS/{arch}/store/{hash[:2]}/{hash}/{pkg}-{ver}-{rev}.{arch}.tar.gz`.

`--check-store` output example (text):
```
bits doctor --check-store  —  architecture: slc9_x86-64
  Store: https://s3.cern.ch/swift/v1/alibuild-repo

  package                              status  detail
  ──────────────────────────────────────────────────────────────────────────────
  zlib                                 PASS    available: zlib-1.3.1-1.slc9_x86-64.tar.gz (hash 3f2c8d...)
  GSL                                  PASS    available: GSL-2.7.1-1.slc9_x86-64.tar.gz  (hash a1b2c3...)
  ROOT                                 FAIL    not in store — will build from source: ROOT-6.32.00-1.slc9_x86-64.tar.gz

  2 of 3 package(s) available in store; 1 will build from source.
```

`--check-store` JSON output (abbreviated):
```json
{
  "mode": "check-store",
  "architecture": "slc9_x86-64",
  "store": "https://s3.cern.ch/swift/v1/alibuild-repo",
  "packages": [
    {"package": "zlib", "status": "PASS", "detail": "available: ..."},
    {"package": "ROOT", "status": "FAIL", "detail": "not in store — will build from source: ..."}
  ],
  "summary": {"PASS": 2, "FAIL": 1, "WARN": 0, "SKIP": 0},
  "notes": []
}
```

| Option | Default | Description |
|--------|---------|-------------|
| `--check-store` | off | Probe the remote store for each package bits would build. Needs a remote store (`--remote-store` or the architecture's default). Only `http(s)://` and local-directory stores can be probed; others are reported as SKIP. Always exits 0. |
| `--runner` | off | Validate the full build-runner environment instead of checking package recipes. |
| `--json` | off | Emit a machine-readable JSON report (setup mode, `--runner` and `--check-store`). |
| `--cvmfs-repos PATH` | _(none)_ | CVMFS mount path to check (repeatable, `--runner` mode only). Can also be set as `$BITS_CVMFS_REPOS=/cvmfs/a,/cvmfs/b`. |
| `--min-disk GIB` | `10.0` | Minimum free disk in `--work-dir` (setup and `--runner` modes). Less triggers WARN, not FAIL. |
| `-a ARCH`, `--architecture ARCH` | auto-detected | Target architecture. |
| `--defaults PROFILE` | `release` | Defaults profile for dependency resolution. |
| `-w DIR`, `--work-dir DIR` | `sw` | Work directory checked for disk space (setup and `--runner` modes). |
| `--docker` | off | Run recipe checks inside a Docker container, as `bits build --docker` would. |
| `--remote-store URL` | _(none)_ | Remote binary store URL; checked for reachability in setup and `--runner` mode and probed per-package in `--check-store` mode. |
| `--write-store URL` | _(none)_ | Write store; checked in setup mode like `--remote-store`. |
| `--no-remote-store` | off | Disable the default remote store. |
| `--insecure` | off | Skip TLS certificate validation when probing an `https://` store. |
| `--s3-endpoint`, `--s3-access-key`, `--s3-secret-key`, `--s3-region`, `--s3-addressing-style` | — | S3 connection for `b3://` stores, as for `bits build`. |
| `--prepub-url URL` | _(none)_ | `--runner` mode: probe the cvmfs-prepub service health. |
| `--disable PACKAGE`, `-e KEY=VALUE`, `--prefer-system`, `--no-system [PACKAGES]` | — | As for `bits build` (recipe-check mode). |
| `--docker-image IMAGE`, `--docker-extra-args ARGS` | — | Image and `docker run` arguments for `--docker`. |
| `-c DIR`, `--config-dir DIR`, `--search-path NAMES`, `-C DIR` | `.` | Recipe directory, extra recipe sub-repos, and directory to change to first. |

**Exit codes (recipe-check mode):** 0 = all requirements satisfied; 1 = missing system packages or compiler/git absent; 2 = no valid defaults combination; 3 = no valid defaults for the package set at all.

**Exit codes (`--runner` mode):** 0 = all checks PASS or WARN; 1 = one or more checks FAIL.

**Exit codes (`--check-store` mode):** always 0 (informational).

**Example — pre-build system check:**
```bash
bits doctor O2Physics
```

**Example — store availability report before a long build:**
```bash
bits doctor --check-store \
    --remote-store https://s3.cern.ch/swift/v1/alibuild-repo \
    -a slc9_x86-64 -c lcg.bits ROOT
```

**Example — store report (JSON output):**
```bash
bits doctor --check-store --json \
    --remote-store https://s3.cern.ch/swift/v1/alibuild-repo \
    -a slc9_x86-64 -c lcg.bits ROOT Geant4
```

**Example — runner health check (human-readable):**
```bash
bits doctor --runner -a slc9_x86-64 \
    --remote-store https://s3.cern.ch/swift/v1/alibuild-repo \
    --cvmfs-repos /cvmfs/alice.cern.ch
```

**Example — runner health check (JSON):**
```bash
bits doctor --runner --json \
    --cvmfs-repos /cvmfs/alice.cern.ch \
    --remote-store https://s3.cern.ch/swift/v1/alibuild-repo
```

**Environment variables relevant to `bits doctor`:**

| Variable | Description |
|----------|-------------|
| `$BITS_PREREQUISITES_URL` | URL shown when the C++ compiler or git is missing. Defaults to the ALICE prerequisite guide. |
| `$BITS_CVMFS_REPOS` | Comma-separated list of CVMFS paths checked in `--runner` mode (e.g. `/cvmfs/alice.cern.ch,/cvmfs/sft.cern.ch`). |

---

### bits status

Show what `bits build` would do for each package in the dependency tree, without building anything. Each package is classified into one of the states below. The mirrors are fetched first, as `bits build` does, so a branch resolves to the commit a build would use; with `--no-fetch-repos` git refs are read from the local mirror cache only, and packages whose refs have not been cached yet are reported as `hash_unknown`.

```bash
bits status [options] PACKAGE [PACKAGE...]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--defaults PROFILE` | `release` | Defaults profile(s); use `::` to combine. |
| `-a ARCH`, `--architecture ARCH` | detected | Target architecture. |
| `-w DIR`, `--work-dir DIR` | `sw` | bits work directory to inspect. |
| `-c DIR`, `--config-dir DIR` | `.` | Recipe directory. |
| `--search-path NAMES` | _(none)_ | Extra recipe sub-repos, as for `bits build`. |
| `-C DIR`, `--chdir DIR` | `.` | Change to DIR first. |
| `--reference-sources DIR` | `<workDir>/MIRROR` | Mirror directory for git ref cache. |
| `--no-local PACKAGE` | _(none)_ | Exclude a package from local-checkout detection. May be repeated. |
| `--force-tracked` | off | Ignore all local checkouts. |
| `--disable PACKAGE` | _(none)_ | Exclude a package from the dependency tree. |
| `--force-rebuild PACKAGE` | _(none)_ | Report the named package as needing a rebuild regardless of its hash. |
| `-u`, `--fetch-repos` | on | Clone / fetch reference repos to populate the git ref cache before computing hashes. Requires network access. |
| `--no-fetch-repos` | off | Use only the refs already cached (offline). |
| `--remote-store URL` | _(none)_ | Remote binary store URL. Only consulted when `--check-store` is given. |
| `--no-remote-store` | off | Disable any remote store, even a configured default. |
| `--check-store` | off | Probe the remote store to detect tarballs not mirrored locally. Adds a network round-trip per uncached package. |
| `--json` | off | Emit a machine-readable JSON report. |

**Package states:**

| State | Meaning |
|-------|---------|
| `already_installed` | Hash matches the installed package; nothing to do. |
| `from_store` | Matching tarball found in the local TARS store; will be unpacked. |
| `from_remote_store` | Tarball only in remote store; will be downloaded then unpacked. Detected only with `--check-store`. |
| `local_checkout` | A directory matching the package name exists in cwd; will be compiled from local sources. |
| `local_checkout_unchanged` | Devel package whose content hash has not changed; rebuild would be skipped. |
| `build_from_source` | No cached result found; will be compiled from scratch. |
| `hash_unknown` | Git refs unavailable (mirror not yet populated, e.g. with `--no-fetch-repos`). |

**JSON output** (`--json`):

```json
{
  "architecture": "slc9_x86-64",
  "packages": [
    { "package": "zlib", "version": "1.2.13", "hash": "abc...", "state": "already_installed" },
    { "package": "boost", "version": "1.83.0", "hash": "def...", "state": "from_store" },
    { "package": "ROOT",  "version": "6.32.06", "hash": "123...", "state": "build_from_source" }
  ]
}
```

---

### bits verify

Check that a live deployment matches the build manifest written by `bits build`. See [§23 bits verify — Deployment Verification](#23-bits-verify--deployment-verification) for full details.

```bash
bits verify --from-manifest FILE [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--from-manifest FILE` | _(required)_ | Path to the bits build manifest JSON file. |
| `--cvmfs-root PATH` | _(none)_ | Root of a CVMFS tarball store to search first (e.g. `/cvmfs/alice.cern.ch`). |
| `-w DIR`, `--work-dir DIR` | `$BITS_WORK_DIR`, else `sw` | Local bits work directory containing the `TARS/` store. |
| `--no-providers` | off | Skip provider checkout commit verification. |
| `--json` | off | Emit a machine-readable JSON report. |

**Exit codes:** 0 = consistent; 1 = FAIL (hash/commit mismatch); 2 = MISS (tarball not found); 3 = manifest unreadable.

---

### bits sbom

Export a build manifest as a Software Bill of Materials: CycloneDX 1.6 JSON
(`sbom.cdx.json`) and/or SPDX 2.3 JSON (`sbom.spdx.json`). `bits publish`
writes both automatically next to the release NOTICE; this command produces
them on demand from any manifest.

```bash
bits sbom MANIFEST [--format cyclonedx|spdx|both] [-o DIR|-] [--build-id ID]
```

| Option | Default | Description |
|--------|---------|-------------|
| `MANIFEST` | _(required)_ | A bits build manifest (`MANIFESTS/bits-manifest-*.json`). |
| `--format` | `both` | `cyclonedx`, `spdx` or `both`. |
| `-o DIR`, `--output-dir DIR` | `.` | Where to write the files; `-` prints one format to stdout. |
| `--build-id ID` | the manifest's build id | Release name in the SBOM. |

Each built package is a component: version `<version>-<revision>`, the tarball
SHA-256, the SPDX licence, a purl (`pkg:github/…@<commit>` for GitHub code,
else `pkg:generic/…`), source archives with their checksums, the git origin,
and bits properties (build hash, architecture, patches, redistributable).
Dependencies come from the manifest (schema v4 and later): runtime and
untracked `requires` become dependency edges (CycloneDX `dependencies`, SPDX
`DEPENDS_ON`); `build_requires` become a `bits:build_requires` property in
CycloneDX and `BUILD_DEPENDENCY_OF` in SPDX. Packages taken from
the system (`system_packages`) appear as components marked
`bits:provided_by = system`. Older (v3) manifests have no dependency edges:
their SBOM lists components only. A recipe `license:` that is not a valid SPDX
expression is kept verbatim (a licence name in CycloneDX, a declared
`LicenseRef-bits-…` in SPDX), so both files stay valid. URLs lose any credentials, and a local (non-URL) source
is not exported. The output is deterministic: the same manifest and bits
version give byte-identical files. The SBOMs `bits publish` uploads carry the
stored tarballs' sha256, as the BOM does.

---

### bits checksums

Compute, check and record the checksums of every recipe in a recipe
repository, without building. Writes the [external checksum
files](#external-checksum-files).

```bash
bits checksums [PACKAGE ...] [-c DIR] [--recipes DIR ...] [--defaults all|P1,P2]
               [--write] [-w WORKDIR] [-a ARCH] [--remote-store URL] [--fresh] [-j N]
```

| Option | Default | Description |
|--------|---------|-------------|
| `PACKAGE ...` | all | Only these packages. |
| `-c DIR`, `--config-dir DIR` | `.` | The recipe repository to check and write (`DIR/checksums/`). |
| `--recipes DIR` | — | Another repository, searched after `-c`, for the recipes the profiles override (repeatable). |
| `--defaults PROFILES` | — | Also check the overrides of `DIR`'s `defaults-*.sh`: `all` or a comma list. |
| `--write` | off | Record new entries. Without it the command only reports. |
| `-w WORKDIR` | `sw` | Holds the download cache (`SOURCES/cache/`), shared with builds. |
| `-a ARCH` | detected | Architecture for `$(...)` sources. |
| `--remote-store URL` | upstream only | Store whose source mirror is tried before upstream (read only). |
| `--fresh` | off | Download every source again from upstream into a private cache, ignoring the download cache and the remote store. |
| `-j N`, `--jobs N` | 8 | Parallel downloads and git queries. |

Sources and tags are resolved as a build resolves them (`%(version)s` etc.;
the group's `source_mode` picks between a recipe's git and tarball sources).
Each tarball is downloaded through the download cache and hashed (SHA-256),
every `(arch)url` variant included; each patch is hashed from the recipe's
`patches/`; each git tag is resolved to its commit with `git ls-remote` (a
branch is reported as moving and never pinned). A profile's entries are those
its overrides add over the release profile, or, for `defaults-release`, over
the recipe as written; they belong to the profile's repository:

```bash
bits checksums -c lcg.bits --write                                     # the recipes' own
bits checksums -c stacks.bits --write --defaults all --recipes lcg.bits  # the overrides
```

Each result is compared with the existing file (including a legacy `tag:`
pin, for the recipe's own tag) and with inline `url,algo:hex` suffixes and
printed as `new`, `ok`, `MISMATCH`, `failed`, `moving` or
`skipped` (e.g. a `file://` source), or `unverified` when the recorded
checksum uses another algorithm. A disagreeing entry is never overwritten;
new ones are added to their section, keeping the file's comments.
A profile override that cannot be resolved is reported as `failed`, as the
build would stop there too. Exit status 1 on any `MISMATCH` or `failed`.

---

### bits stats

Summarise the resource data recorded when a build ran with `--resource-monitoring`
(on by default for `--parallel > 1`). Reads `<work-dir>/LOGS/<arch>/bits_build_stats.json`
(per-package peaks; one file per architecture) and the per-package traces
under `SPECS/` (for average CPU and thread counts). When the architecture isn't
specified, `bits stats` reads the most recent `LOGS/*/bits_build_stats.json`.

**CPU-utilisation tuning hint.** At the end of a `--parallel` run, bits estimates
the whole-run CPU utilisation (useful core-seconds ÷ cores × wall-clock) and the
average number of builders busy at once, and writes them under a `"tuning"` key
in `bits_build_stats.json` together with a `recommendation`. When there is
headroom (utilisation below ~90%) the recommendation is also printed at the end
of the build: if the builder slots were mostly full it suggests a higher
`--oversubscribe` (which raises each builder's `-j` without changing the memory
budget); if the slots were often empty it points at the dependency graph and
suggests more `--parallel` and/or reusing prebuilt tarballs.

```bash
bits stats [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-w DIR`, `--work-dir DIR` | `sw` | Build work area to read stats from. |
| `--package NAME` | _(none)_ | Show the resource timeline detail for one package instead of the summary. |
| `--top N` | `10` | Number of packages in the table. |
| `--sort time\|rss\|cpu` | `time` | Sort the table by wall time, peak memory, or peak CPU. |
| `--json` | off | Emit machine-readable JSON instead of the text report. |

The report leads with a headline (machine size, serial build time, peak memory,
longest build), then a top-N table (time / peak RSS / peak & average CPU /
threads / memory-per-thread), then **flags** that each point at a concrete fix.
`MEM/THR` is the worst-case peak RSS ÷ thread count: when it is high, the
recipe's `-j` parallelism multiplies it into a large footprint, so cap `JOBS`
or set `mem_per_job`. The flags:

- **Under-threaded heavy build** — a long build using few cores on average →
  the recipe probably isn't running parallel make; add `${JOBS:+-j$JOBS}`.
- **OOM risk** — a package whose peak RSS is a large fraction of RAM → set
  `mem_per_job` on the recipe so the `--parallel` scheduler reserves for it.

---

### bits compliance

Audit recipe licence metadata and the binary store; optionally enforce.
Read-only by default; exit `0` = clean, `1` = issues found, so it can gate CI.

```bash
bits compliance [--recipes DIR] [--remote-store URL] [--no-store-check]
bits compliance [-c DIR] [--defaults P] [-a ARCH] PACKAGE ...   # group mode
bits compliance --enforce [--dry-run] [--key PEM]     # admin
```

| Option | Default | Description |
|--------|---------|-------------|
| `PACKAGE ...` | _(none)_ | Group mode: audit exactly the resolved dependency closure of these roots (e.g. the group's meta-packages), discovering recipe repositories as `bits build` does (config dir, defaults profile, repository providers). |
| `-c DIR`, `--search-path NAMES`, `-a ARCH`, `--defaults PROFILE`, `--disable PACKAGE` | as `bits build` | Group mode: how the closure is resolved. |
| `--recipes DIR` | `.` | Without `PACKAGE`: recipe repository to audit (a directory of `*.sh` recipes). |
| `--remote-store URL` | `https://s3.cern.ch/lcgapp-bits-testing` | S3 store to audit against the recipe flags (`https`, `b3://`, `s3://`). `$BITS_S3_STORE` changes the default. `--store` is the deprecated spelling. |
| `--no-store-check` | off | Audit the recipes only. |
| `--enforce` | off | ADMIN: purge non-compliant packages from the store (see below). |
| `--dry-run` | off | With `--enforce`: print every action, touch nothing. |
| `--key PEM` | _(none)_ | With `--enforce`: re-sign the affected platforms after the purge. |
| `-w DIR`, `--work-dir DIR` | `sw` | Scratch for the store client. |

The **audit** reports: recipes missing a `license:` field, unverified
`LicenseRef-*` ids, `NOASSERTION` system shims, and the binaries-restricted /
sources-restricted sets (`redistributable:`, see [Licence compliance and redistribution policy](#licence-compliance-and-redistribution-policy)). The **store walk**
probes whether the bucket answers *unauthenticated* requests (anonymous access
working at all is a finding: a restricted object in a world-readable bucket is
public redistribution regardless of any CVMFS gate), then checks every
per-build BOM and signed manifest for packages whose **current** recipe forbids
redistribution — the recipes are the source of truth, the store is audited
against them. Works without S3 credentials (degrades to an unsigned client).

`--enforce` removes what the audit found: deletes the offending packages'
store objects (all files under their `TARS/<arch>/store/…` prefixes), their
rev-index markers and their `SOURCES/cache/` archives (resolved from the
recipes; unresolvable URLs are reported, never guessed), rewrites the
per-build BOMs without the offending entries (an emptied BOM is deleted), and
with `--key` re-certifies the affected architectures from the rewritten BOMs
(per-platform scoping — an arch left empty gets an empty signed manifest,
i.e. revocation). Without `--key` the next CI certification self-heals.
It prints the matching `bits-manifests` repo files to prune, since CI
re-derives the signed manifests from the repo. Requires S3 write credentials
(`--dry-run` does not). Always dry-run first.

---

### bits init

`bits init` has two modes, selected by whether a PACKAGE name is given. (A name ending in `.bits` instead checks out that recipe repository from the provider registry — see [Bootstrapping a recipe repository from the registry](#bootstrapping-a-recipe-repository-from-the-registry).)

#### Clone mode — create a writable source checkout

```bash
bits init [options] PACKAGE[@VERSION][,PACKAGE[@VERSION]...]
```

Clones the upstream source repository for each named package into a writable local directory. After `bits init`, the created directory is automatically used as the source for subsequent `bits build` invocations of that package.

| Option | Description |
|--------|-------------|
| `--dist [USER/REPO@]BRANCH` | Recipe repository cloned into the config dir if that does not exist yet (`[user/repo@]branch` or `[url@]branch`; a bare `branch` is a branch of `alisw/alidist`). Default: `alisw/alidist` on its default branch. |
| `-z PREFIX`, `--devel-prefix PREFIX` | Directory for development checkouts. Default: `.`. |
| `-c DIR`, `--config-dir DIR` | Recipe directory. Default: `<devel-prefix>/alidist`. |
| `--reference-sources DIR` | Mirror directory to speed up cloning. Default: `<work-dir>/MIRROR`. |
| `-a ARCH`, `--architecture ARCH` | Architecture used to read the defaults. Default: detected. |
| `--defaults PROFILE` | Defaults profile(s); use `::` to combine (e.g. `release::myproject`). Default: `release`. |

#### Config mode — record persistent settings with `bits use`

`bits use build …` (below) is the general way to record persistent settings. As a shortcut, when **no PACKAGE** is given, `bits init` saves only the options you name on its command line as a `bits use` profile (`./.bitsuse`, or a record under `~/.bits/use/` when the directory is not writeable) and exits, so you do not repeat them on every build. `--architecture` goes to the `[common]` section (used by every command that takes an architecture), the rest to `[build]`; each section written replaces its previous content, as with `bits use`. Flags you give explicitly on a later command still win. Under the `aliBuild` wrapper, `aliBuild init` with no PACKAGE instead clones the recipe repository (`--dist`) into the config dir, as classic aliBuild did.

```bash
# Persist a remote binary store for the current project
bits init --remote-store https://store.example.com/store

# Persist both a read store and a write store
bits init --remote-store https://store.example.com/store \
          --write-store b3://mybucket/store

# Preview what would be saved without touching the profile
bits init --dry-run --remote-store https://store.example.com/store
```

| Config option | Saved to | Description |
|---------------|----------|-------------|
| `--remote-store URL` | `[build]` | Binary store to fetch pre-built tarballs from. |
| `--write-store URL` | `[build]` | Binary store to upload newly-built tarballs to. |
| `-a ARCH`, `--architecture ARCH` | `[common]` | Default target architecture. |
| `--defaults PROFILE` | `[build]` | Default profile(s), `::` separated. |
| `-c DIR`, `--config-dir DIR` | `[build]` | Default recipe directory. |
| `-w DIR`, `--work-dir DIR` | `[build]` | Default work/output directory. |
| `--reference-sources DIR` | `[build]` | Default mirror directory. |
| `--community NAME` | env only | No build-time flag; set `$BITS_COMMUNITY` (the `aliBuild` wrapper sets it). `--organisation` is its former name. |
| `--providers URL` | env only | No build-time flag; set `$BITS_PROVIDERS`. |

> **`bits.rc` has been retired.** Earlier versions read a `bits.rc` / `.bitsrc` / `~/.bitsrc` file; it is no longer read. Per-directory settings now live in a `bits use` profile (above); global settings come from environment variables: `$BITS_WORK_DIR`, `$BITS_REPO_DIR` (config dir), `$BITS_COMMUNITY`, `$BITS_PROVIDERS`, `$BITS_PATH` (recipe search path — see `--search-path`), `$BITS_S3_STORE`, `$BITS_PREREQUISITES_URL`, `$BITS_CVMFS_REPOS`. Display prefix and branding (`$BITS_PKG_PREFIX`, `$BITS_BRANDING`) are set by the `aliBuild` wrapper.

**Saving frequently-used CLI args (`bits use`).** `bits use` records raw command-line arguments per directory so you don't retype them. `bits use --architecture X` saves to the `[common]` section (applied to every arch-aware command); `bits use build --docker --sandbox off` saves to `[build]` (that command only). Saved args are injected right after the action and before your own args, so an explicit flag still wins. Saving a section replaces it, so give all of its args in one call. `bits use` (no args) shows the active profile and its source; `bits use --clear [SECTION]` clears it; `bits use --help` prints the forms (a `-h`/`--help` is never saved). Storage is two-tier: a local `./.bitsuse` when the directory is writeable and owned by you, otherwise a per-directory record under `~/.bits/use/` — so a choice persists even in a read-only checkout. A local `.bitsuse` is used only when you own it; otherwise it is ignored and the `~/.bits/use/` record applies. `.bitscmd`, the file's previous name, is still read as a fallback.

---

### bits clean

Delete build leftovers: `TMP/`, `INSTALLROOT/`, `BUILD/` trees that no `<package>-latest` link points to, and installed package versions (for the architecture and `share/`) that no `latest` link points to.

```bash
bits clean [options]
```

| Option | Description |
|--------|-------------|
| `-w DIR`, `--work-dir DIR` | Work directory to clean. Default: `sw`. |
| `-a ARCH`, `--architecture ARCH` | Architecture whose installed packages and tarballs are considered. Default: detected. |
| `-C DIR`, `--chdir DIR` | Change to DIR first. |
| `--aggressive-cleanup` | Also delete `SOURCES/` and the tarball store (`TARS/<arch>/store`, `TARS/share/store`; by-name links are kept). Git mirrors are not touched. |
| `-n`, `--dry-run` | Show what would be removed without deleting. |

---

### bits prune

Evict packages from a **persistent workDir** based on last-use age and/or available disk space. Intended for shared CI build caches where packages accumulate over time. (Formerly `bits cleanup`, which still works as a deprecated alias that warns.) See [User Guide §7 — bits prune](USERGUIDE.md#bits-prune--evict-packages-from-a-persistent-workdir) for full details.

```bash
bits prune [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-w DIR`, `--work-dir DIR` | `sw` | workDir to manage. |
| `-a ARCH`, `--architecture ARCH` | auto-detected | Architecture to evict packages for. |
| `--max-age DAYS` | `7.0` | Evict packages not touched in more than `DAYS` days. Set to `0` to disable age-based eviction. |
| `--min-free GIB` | _(none)_ | Evict LRU packages until `GIB` GiB are free on the workDir filesystem. |
| `--disk-pressure-only` | — | Run only the disk-pressure pass; skip age-based eviction. |
| `--retain` | off | Retention sweep over **all** architectures. Keeps the packages of the newest `--keep-builds` local build manifests per arch, and certified packages not yet on CVMFS. Evicts what is safe upstream (in the store, in the verified signed manifest **and** recorded as published to CVMFS), plus superseded attempts, orphan store tarballs, `BUILD/` dirs and dangling links. An arch whose signed manifest cannot be verified is skipped entirely. |
| `--keep-builds N` | `2` | With `--retain`: build manifests to keep per architecture. |
| `--remote-store URL` | _(none)_ | With `--retain`: store to derive the signed common manifests from, one per architecture on disk (`--store` is deprecated). |
| `--trust-manifest PATH\|URL` | _(none)_ | With `--retain`: explicit signed common manifest(s), in addition to or instead of the store (repeatable). |
| `--mark-published-from PATH\|URL` | _(none)_ | With `--retain`: backfill CVMFS publish markers (`.published/`) from a `cvmfs-status.json` record first, so released content becomes evictable. |
| `--grace-days DAYS` | `1.0` | With `--retain`: never evict anything modified more recently. |
| `-n`, `--dry-run` | — | Show what would be evicted without removing anything. |

---

### bits enter

Spawn a new interactive sub-shell with one or more modules loaded. Exit the sub-shell with `exit` to return to the original environment.

```bash
bits enter [-q] [--shellrc] [--dev] [--view] MODULE1[,MODULE2,...]
```

| Option | Description |
|--------|-------------|
| `--shellrc` | Source the user's shell startup file (`.bashrc`, `.zshrc`, etc.) in the new shell. Suppressed by default to avoid environment conflicts. |
| `--dev` | Source `etc/profile.d/init.sh` from each package directly instead of using `modulecmd`. Development use only. Appends `(dev)` to the shell prompt. |
| `-q` | Silence the module-load messages. |
| `--view` | Collapse `PATH`, `LD_LIBRARY_PATH`, `CMAKE_PREFIX_PATH`, `PKG_CONFIG_PATH`, `PYTHONPATH` and `ROOT_INCLUDE_PATH` onto a merged view of the loaded closure (cached under `$WORK_DIR/VIEWS/<arch>`), so the environment stays small on big stacks. `<PKG>_ROOT` and other `setenv`s are untouched. Turned on automatically when a loaded package's recipe sets `view: true`. |

The shell type is auto-detected from the parent process (`bash`, `zsh`, `ksh`, `csh`/`tcsh`, `sh`). Override with the `MODULES_SHELL` environment variable. The prompt is set to `[MODULE_LIST] \w $>` (or the zsh/ksh equivalent) for the duration of the session. Nesting `bits enter` inside another bits environment is blocked.

All module commands (`enter`, `load`, `unload`, `setenv`, `q`, `avail`, `modulecmd`) also accept `-w DIR` (default: `$BITS_WORK_DIR`, else `./sw` or `../sw`), `-a ARCH` (default: the installed architecture, preferring the host's when several are present) and `--no-refresh` (do not refresh the modules directory).

---

### bits load / printenv

Print the shell commands to load one or more modules. Must be `eval`'d to take effect, or used via `bits shell-helper`.

```bash
eval "$(bits load [-q] [--view] MODULE1[,MODULE2,...])"
```

`-q` suppresses the informational message on stderr; `--view` collapses the path variables onto a merged view as for `bits enter` (pass it explicitly). `printenv` is an alias for `load`. The modules directory is refreshed and the module is verified to exist before printing. `--dev` mode prints manual `source` commands to stderr instead (eval of dev mode is unsupported).

---

### bits unload

Print the shell commands to unload one or more modules. Must be `eval`'d to take effect.

```bash
eval "$(bits unload [-q] MODULE1[,MODULE2,...])"
```

The version may be omitted; `modulecmd` will unload whichever version is currently loaded. `-q` suppresses stderr output. Override the shell with `MODULES_SHELL`.

---

### bits setenv

Load modules into the current process and `exec` a command. No new shell is spawned; the exit code of the command is preserved.

```bash
bits setenv [-q] [--view] MODULE1[,MODULE2,...] -c COMMAND [ARGS...]
```

Everything after `-c` is executed as-is. The modules directory is refreshed and modules are verified before execution. `--view` works as for `bits enter` and is likewise turned on by a `view: true` package.

```bash
bits setenv ROOT/v6-30 -c root -b
```

---

### bits query / list / avail

```bash
bits q [REGEXP]    # list available modules, optionally filtered by regex
bits list          # show currently loaded modules
bits avail         # raw modulecmd avail output
```

`bits q` lists modules in the native `PKG/VERSION` form. When a display prefix is set in the environment (`BITS_PKG_PREFIX`, e.g. via the `aliBuild` wrapper) the output is reformatted to `PREFIX@PKG::VERSION` (so `aliBuild q` prints `VO_ALICE@zstd::1.5.7-local1`). The optional `REGEXP` is a case-insensitive extended regular expression. `bits q` lists the installed packages straight from the install tree, without refreshing the modules directory or running `modulecmd`, so it stays fast even with hundreds of packages. `bits avail` refreshes the modules directory and runs `modulecmd avail`.

**Listing on CVMFS.** `bits q` and the module refresh list a tree on CVMFS with a
directory walk: once the CVMFS client has the tree's catalogs in its cache, that
takes milliseconds. The first time it downloads them, one per nested catalog, so a
modules directory should be a single catalog. With `BITS_CATALOG_LISTING=1` they
first try the `bitsModules` helper, which reads the listing from the tree's CVMFS
catalog in one HTTP fetch; it applies only when the tree is the root of its own
catalog with no nested catalogs below it, and otherwise bits walks the directory,
with the same result.

**A community's modules on CVMFS.** With `BITS_CVMFS_PREFIX` set to a community's
CVMFS prefix (the bits entry point on CVMFS sets it), `bits q`, `enter`, `load`,
`printenv`, `unload` and `setenv` also use the modules the community published there,
after the local ones. bits uses the trees `<prefix>/<arch>/Modules/modulefiles` whose
`<arch>` is `ARCHITECTURE` but for a trailing `-opt`/`-dbg`, in this order: the exact
one, the one without build type (the toolchain), `-opt`, `-dbg`. So
`-a x86_64-el9-gcc14-opt` uses `x86_64-el9-gcc14-opt`, `x86_64-el9-gcc14` and
`x86_64-el9-gcc14-dbg`, never another compiler or OS. A module comes from the first
tree that has it, local builds first; `q` lists each name once. Each tree's modules
are loaded against that tree alone, with its own `BASEDIR`, so modules from several
trees can be loaded together; `unload` uses the tree each module was loaded from. The
CVMFS trees are not put on `MODULEPATH`: inside `bits enter`, a `module load` by hand
sees only the local modules. `--dev` works with local modules only.

Without `BITS_CVMFS_PREFIX` (unset; set but empty turns CVMFS modules off), a recipe
repository's [`cvmfs.yaml`](#the-cvmfs-layout-file-cvmfsyaml) gives the trees: in the
current directory, or beside the work directory (`sw/`). They are the directory of its
`cvmfs_modules_template` (`{prefix}/{arch}/Modules/modulefiles/{pkg}`, say), with the
`<arch>` order above. Without `-a` and with no tree for the local architecture,
`<arch>` is the one published for this host's CPU and OS (`el<N>` or `ubuntu<NNNN>`, as the CVMFS entry point picks its view) when there
is exactly one compiler for it; otherwise bits uses none and says which there are, so
`bits -a <arch> q` picks one.

---

### bits modulecmd

Pass arguments directly to the underlying `modulecmd` binary, after refreshing the module directory. Useful for operations not covered by the higher-level commands or for targeting a specific shell:

```bash
bits modulecmd zsh load ROOT/v6-30
# Consult man modulecmd for the full argument list.
```

---

### bits shell-helper

Emit a shell function definition to be `eval`'d in a shell rc file. Once active, `bits load` and `bits unload` modify the current shell's environment directly without requiring an explicit `eval`.

```bash
# Add to ~/.bashrc, ~/.zshrc, or ~/.kshrc:
BITS_WORK_DIR=/path/to/sw
eval "$(bits shell-helper)"
```

All other `bits` sub-commands pass through to the `bits` binary unchanged.

---

### bits version / architecture

```bash
bits --version      # e.g. bits 0.5-199-g648dfe8 (tag 0.5 +199, commit 648dfe8, 2026-10-02)
bits version        # the same line plus the detected architecture
bits architecture   # print only the architecture string (e.g. ubuntu2204_x86-64)
```

From a git checkout the version is `git describe --tags` (the plain tag when bits
runs exactly from it, `-dirty` with local changes) with the commit date; an
installed package reports its setuptools_scm version. The same string is recorded
as `bits_version` in build manifests and SBOMs.

---

### bits publish

Publish one built package to CVMFS through the cvmfs-prepub service — copy the
immutable install tree, relocate it to `--cvmfs-target`, tar it and submit it to
`--prepub-url` — or, with no `PACKAGE`, bulk-upload the latest build manifest to
the S3 store (see [Publishing an existing local build to S3](#publishing-an-existing-local-build-to-s3-bits-publish)).
A single package's S3-store write is `bits store upload`.

```bash
bits publish PACKAGE [VERSION] --cvmfs-target PATH --prepub-url URL [options]
bits publish --release-view NAME --cvmfs-target ROOT --prepub-url URL [PACKAGE]
bits publish [--manifest [FILE]] [--remote-store URL]       # bulk S3 upload
```

| Option | Description |
|--------|-------------|
| `PACKAGE [VERSION]` | Package to publish; `VERSION` (optionally with revision) defaults to the latest build. |
| `--cvmfs-target PATH` | Absolute CVMFS path the package will occupy. With `--release-view`, the CVMFS root the `Views/` tree lives under. |
| `--module-target PATH` | CVMFS path of the separate modules tree; the package's `etc/modulefiles` are published there as an independent job (also with `--no-relocate`). |
| `--release-view NAME` | Publish the merged view of a release to `<cvmfs-target>/Views/NAME-<build_id>/<arch>/` instead of a package; the build id is read from the packages' `.meta.json`. (`--view` is the deprecated spelling.) |
| `--no-relocate` | Skip relocation — for packages built at their final path with `bits build --cvmfs-prefix`. |
| `--scratch-dir DIR` | Directory for the temporary CVMFS working copy (default: a system temp dir). |
| `-w DIR`, `-a ARCH` | Work directory (default `sw`) and architecture. |
| `--manifest [FILE]`, `--from-manifest [FILE]` | Bulk-upload every package of a build manifest to the S3 store (`latest` by default). This is the mode when no `PACKAGE` is given. |
| `--remote-store URL` | S3 store for the bulk upload (default: `$BITS_S3_STORE`, else the built-in store). `--store` is the deprecated spelling. |
| `--prepub-url URL` | cvmfs-prepub API base URL. Required for a CVMFS publish. |
| `--prepub-token TOKEN` | API token (default `$PREPUB_API_TOKEN`). Each request is HMAC-signed and the secret never leaves the host; `--prepub-bearer-auth` sends it as an `Authorization: Bearer` header instead (only for a prepub running `auth_mode=bearer`). |
| `--prepub-repo REPO`, `--prepub-path SUBPATH` | Repository and lease sub-path; derived from `--cvmfs-target` when omitted. |
| `--prepub-webhook URL` | URL prepub POSTs to when the job completes. |
| `--prepub-poll-interval SEC`, `--prepub-timeout SEC` | Status polling interval (default 10 s) and total wait (default 1800 s). |
| `--prepub-no-verify-tls` | Skip TLS certificate verification (self-signed / dev only). |

Certification merge requests are opened by `bits certify`, not
`bits publish` (see [Publishing and certifying a build](#publishing-and-certifying-a-build--bits-publish-bits-certify)).

---

### bits certify / bits sign

Make a build's binaries trusted for reuse: `bits certify` uploads what the store is missing, gets a passkey approval through bits-console and opens the merge request; `bits sign` signs a build manifest (the former `bits certify`). Both are described, with their options, under [Publishing and certifying a build](#publishing-and-certifying-a-build--bits-publish-bits-certify) and [Signing — `bits sign`](#signing--bits-sign); `bits certify --help` and `bits sign --help` list every option.

---

### bits brew

Meant for macOS (`osx*` architectures; for any other architecture the Brewfile
normally lists nothing). Scan the recipes (config dir and provider repositories) for
`homebrew_formula:` and write a Brewfile listing the formulae the stack expects
(see [macOS Homebrew system layer](#macos-homebrew-system-layer)).

```bash
bits brew [-a ARCH] [--defaults PROFILE] [-o FILE|-] [--check] [-c DIR] [-w DIR]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-o FILE`, `--output FILE` | `<work-dir>/<arch>/Brewfile` | Where to write; `-` prints to stdout. A local per-arch build artifact, not committed next to the recipes. |
| `--check` | off | Do not write; exit non-zero if the file is missing or differs from what would be generated now. |
| `-a ARCH`, `--architecture ARCH` | detected | Only recipes whose `prefer_system` matches this architecture are included. |
| `--defaults PROFILE` | `release` | Defaults profile. |
| `-c DIR`, `--config-dir DIR` | `.` | Recipe directory. |
| `-w DIR`, `--work-dir DIR` | `sw` | Work area: the Brewfile is written under it and the providers cloned under `<work-dir>/REPOS` are scanned. |
| `-C DIR`, `--chdir DIR` | `.` | Change to DIR first. |

On macOS `gnu-tar` is always listed: package tarballs are packed with GNU tar so that
they are byte-reproducible (see [Build lifecycle with a store](#build-lifecycle-with-a-store)).

---

### bits overlay lcg

Emit an lcgcmake-style LCG release view over a built closure, so that a
consumer's `find_package(LCG <n> EXACT)` (e.g. ATLAS's AtlasLCG) resolves
against the bits install tree instead of an lcgcmake release. It runs after the
build: it scans the installed packages under `<work-dir>/<arch>` (including
package-family directories; `<pkg>/latest` links are skipped), reads each
package's `.meta.json`, and writes `LCG_<num><postfix>/LCG_externals_<platform>.txt`
and `LCG_generators_<platform>.txt` under `--out` (all packages go into the externals
file; the generators file is a placeholder, written because AtlasLCG requires both). It replaces `bits lcg-view`,
which still works as a deprecated alias that warns. `bits overlay` with no format
lists the available formats.

```bash
bits overlay lcg -a ARCH --platform PLATFORM --version-number N \
                 [--postfix P] [--out DIR] [--build-view DIR] [-w DIR]
```

| Option | Description |
|--------|-------------|
| `-a ARCH`, `--architecture ARCH` | Arch subtree under the work dir (e.g. `x86_64-el9-gcc15`). Required. |
| `-w DIR`, `--work-dir DIR` | Work dir holding the install tree (default `sw`). |
| `--platform PLATFORM` | LCG platform string for the manifest file name (e.g. `x86_64-el9-gcc15-opt`). Required. |
| `--version-number N` | LCG version number of the release directory (e.g. `110`). Required. |
| `--postfix P` | LCG version postfix (e.g. `_ATLAS_5`). Default: none. |
| `--out DIR` | `LCG_RELEASE_BASE` root to write `LCG_<num><postfix>/` under (default: current directory). |
| `--build-view DIR` | Also build the merged symlink view of the closure in DIR, with a `setup.sh` that collapses `PATH`/`LD_LIBRARY_PATH`/… onto it (no lcgcmake `create_lcg_view` needed). |
| `--lib-path-var VAR` | Loader path variable for that `setup.sh` (default `LD_LIBRARY_PATH`; use `DYLD_LIBRARY_PATH` on macOS). |

---

### bits import

Turn a foreign deployment (e.g. an LCG release on CVMFS) into a reuse overlay:
harvest each deployed module's resolved environment (or read `--manifest`), check
that the set is closed, stamp it with one deterministic `build_id`, and write a
per-`build_id` overlay (modulefiles plus module-side `.meta.json`) that
`bits build --reuse-from <modules-path>|cvmfs` can reuse without recompiling.

| Option | Description |
|--------|-------------|
| `--modulepath DIR` | MODULEPATH of the deployment to harvest via `modulecmd`. |
| `--manifest FILE` | JSON manifest to import instead of harvesting (when no modulefiles exist). |
| `--trusted` | Harvest a bits-built deployment directly from its own modulefiles, re-anchored to `--install-base`, reading package hashes from the install tree. Deterministic (no `modulecmd`); publishable strict reuse. |
| `--install-base DIR` | With `--trusted`: the absolute Packages root the modulefiles' `BASEDIR` resolves to. |
| `--aliases FILE` | JSON map of foreign → bits package names. |
| `--label NAME` | `build_id` prefix (e.g. `LCG_109`; default `import`). |
| `--out DIR` | Overlay root (default `<work-dir>/MODULES`). |
| `--force-overwrite` | Stamp and write even if the release is not closed. (`--force` is the older spelling.) |
| `-w DIR`, `-a ARCH` | Work dir, and the architecture the deployment was built for. |

---

### bits cvmfs-path

Print the absolute `/cvmfs/<repo>/<path>` a package will be published to,
resolved from the group's path templates in the defaults `system:` block,
without building. The publish pipeline uses it to reserve the path before the
build, so the reserved and the published path always agree.

| Option | Description |
|--------|-------------|
| `--package NAME` | `{pkg}`. Required. |
| `--version VER` | `{tag}` / `{version}`. |
| `--platform PLAT`, `--install-dir DIR` | `{platform}`, `{install_dir}`. |
| `--kind {releases,packages,modules,shared}` | Which template to resolve (default: `packages` when the group has a `cvmfs_packages_template`, else `releases`). |
| `--admin` / `--login USER` | The admin (group-prefix) path, or a user path under `<user_prefix>/<login>`. |
| `--day DAY` | `{day}` value (default: the current UTC weekday); pass the same value as the build. |
| `--set NAME=VALUE` (`--flavour`, `--flavor`) | As for `bits build` (e.g. `release=LCG_110`), so `{release}` resolves as in the build. |
| `--prefix ROOT` | Fallback CVMFS root, used only when the defaults declare no `system.prefix`. |
| `--defaults`, `-a`, `-c`, `--search-path`, `-C`, `--disable` | As for `bits build`, to load the defaults. |

---

### bits store / bits cvmfs (admin & CI groups)

Two `bits <noun> <verb>` groups act on shared infrastructure rather than a local build.

**`bits store <verb>`** — the shared S3 binary store:

```bash
bits store ls                         # list manifests / store objects (default verb)
bits store verify --arch <A>          # check signed manifests against the store
bits store gc --trust-manifest <M>    # reachability GC (was `bits gc`)
bits store stats                      # per-arch/per-build usage report (was `bits store-stats`)
bits store upload <PKG>               # upload one built package to the store (was `publish --to s3`)
bits store rm <selection>             # delete objects (narrowed selection required)
bits store cat <key>                  # dump a store object's bytes to stdout
bits store ls|rm --stale-boms [--manifests-dir DIR]  # BOMs the store no longer backs
```

**`bits cvmfs <verb>`** — a deployed CVMFS tree and the producer-side publish pipeline:

```bash
bits cvmfs platforms|show|summary     # inspect a deployed tree (read-only)
bits cvmfs stage   …                  # producer-side staging (was `bits cvmfs-stage`)
bits cvmfs publish …                  # producer-side publish of a build manifest (was `bits cvmfs-publish`)
```

`bits cvmfs platforms|show|summary` inspect the tree under `--cvmfs`, by default the
`prefix:` of `./cvmfs.yaml`.

#### The CVMFS layout file: cvmfs.yaml

A recipe repository may keep its CVMFS layout in a `cvmfs.yaml` in its top directory
(the recipe directory, `-c`/`--config-dir`, by default the current directory), the same
keys as under `system:` in its defaults: `prefix`,
`cvmfs_user_prefix`, `cvmfs_packages_template`, `cvmfs_releases_template`,
`cvmfs_modules_template`, `cvmfs_shared_path_template`, `cvmfs_views_template` and
`cvmfs_view_exclude`. A key the defaults set wins, so a profile can still change one
(e.g. a nightly's releases and views templates). The prefix bits-console injects
still bounds it: a declared prefix must be that one or below it. The layout never enters a package hash. Without the file, the defaults
alone give the layout, as before. Besides builds and publishing, `bits q`, `enter`
and `load` use it for the group's modules (from the current directory, or beside the
work directory), and `bits cvmfs platforms|show|summary` for their root (from the
current directory).

```yaml
prefix:                     /cvmfs/bits.cern.ch/lcg
cvmfs_user_prefix:          "{prefix}/user"
cvmfs_packages_template:    "{prefix}/{arch}/Packages/{pkg}/{tag}"
cvmfs_modules_template:     "{prefix}/{arch}/Modules/modulefiles/{pkg}"
cvmfs_shared_path_template: "{prefix}/noarch/{pkg}/{tag}"
cvmfs_releases_template:    "{prefix}/releases/{release}/{family}{pkg}/{version}/{arch}"
cvmfs_views_template:       "{prefix}/views/{release}/{arch}"
```

The shell reads `prefix` and `cvmfs_modules_template` as plain `key: value` lines.

`bits cvmfs publish` places every package of the build with the build's own CVMFS
templates (recorded in its manifest). A group that sets `cvmfs_packages_template`
(e.g. `{prefix}/{arch}/{family}{pkg}/{tag}`) publishes each package once at that
path: one already published by the same build hash is skipped, one published by a
different build is refused. Its `cvmfs_releases_template` is then the **release
view**, created only with `--release-view` (bits-console: *Create release view*),
and only after every package published: one relative symlink per package, sent
via prepub's ingest path so it merges into a release directory other platforms
already use (without ingest, only a new release directory works). Such a publish
also adds, once per arch, a `BASE/1.0` modulefile in the modules directory that
sets `BASEDIR` (relative to itself) to the packages directory, which is what bits
modulefiles resolve against ($BASEDIR/<pkg>/<ver-rev>). Packages published
elsewhere (noarch packages, a toolchain with [`own_hash: true`](#shared-toolchains-own_hash)) get a relative symlink there. With the
ALICE-style templates
`{prefix}/{arch}/Packages/…` and `{prefix}/{arch}/Modules/modulefiles/{pkg}`,
`BITS_MODULEDIR=<prefix> BITS_PLATFORM=<arch> bitsenv …` then works unchanged.
A package's modulefile is published inside it (`etc/modulefiles/<pkg>`); after
the packages, one job adds a relative symlink `<modules dir>/<pkg>/<ver-rev>` to
it for each package that has none yet, instead of a commit per modulefile. Like
the release view, it merges into an existing directory and so needs prepub's
ingest path; two publishes adding the same links at the same time make the
second one fail (the next publish finds them there). Template tokens: `{pkg} {version} {revision} {tag}`
(version-revision) `{family} {platform}` (console platform, e.g. `x86_64-el9`)
`{arch}` (build arch, e.g. `x86_64-el9-gcc15-opt`) `{release}` `{day}`.

A store tarball's file times are zero (it is reproducible, see the Pack step of the
[build lifecycle](#build-lifecycle-with-a-store)); the published files get the time
the build recorded the package instead (its manifest `completed_at`), so CVMFS
shows when it was built (a reused package: when the build reused it), not 1970.

`{day}` is a nightly path slot: when a template uses it, bits fills in the UTC
weekday (`Mon` … `Sun`); `bits build --day DAY` (or `day:` under `system:`) sets
it, and an empty value collapses the segment. It is layout only — never hashed,
never part of the store path or manifest key. A reserved build (`bits cvmfs-path`,
then the build) should pass the same `--day` to both, so the two agree across a
UTC midnight.

When a `cvmfs_packages_template` is set, each package upload carries its target
path and build hash, so cvmfs-prepub
completes a rerun queued behind the original without publishing it twice, and
refuses a path another build published. With `--replace-on-conflict` a package
whose published hash differs is sent with `replace`: prepub deletes the old
subtree and commits this build's. That needs prepub with `replace_on_conflict`
(its `/api/v1/health` says `replace_allowed`), which bits checks before uploading;
a package published without a hash is never replaced. Modulefiles, release views
and aliases are never replaced. On the staged path, replacing also needs the
ingest path on the prepub node, which does the delete.
Before uploading on the ingest path, bits asks prepub for its per-package size limit
(`max_tar_size`) and refuses a larger tar with a message naming both sizes,
instead of failing with a bare connection reset.

A group that also sets `cvmfs_views_template` (e.g. `{prefix}/views/{release}/{arch}`)
gets, with each release, a **merged view** like an LCG view: `bin lib lib64 include
share cmake python libexec man` of every package merged into one tree of relative
symlinks to the published packages (the dependent package wins a collision; each
is logged). A subdirectory only one package fills, with nothing of it excluded, is
linked whole (e.g. `include/boost`); the top-level directories, `lib*/pkgconfig`
and `lib*/python*/site-packages` stay real directories. The view also has
`setup.sh` (locates itself under bash/zsh) and `setup.csh`.
`system.cvmfs_view_exclude: [pkg, …]` leaves packages out. The file lists come from `.bits-view.json`, which every build writes into the package just
before packing (so later changes to the local tree don't leak in); for a tarball
built before that, from the tarball itself. A recipe shapes its own part of the
view with `view:`: `false` keeps the package out, `exclude: [share/doc, lib/*.a]`
drops paths, `include: [etc/root]` adds paths beyond the default directories.
`view:` is presentation only: it is not hashed (no rebuild), and the rules of the
build that creates the view (recorded in its manifest) apply. The view's
`.meta.json` holds a fingerprint of its links and setup scripts. A view already
published is kept; with `--replace-on-conflict` it is replaced when its
fingerprint differs (a view published without one is kept).

The deprecated hyphenated names (`store-stats`, `cvmfs-stage`, `cvmfs-publish`) still
work for one release and warn, as do `bits cleanup` (→ `bits prune`) and `bits lcg-view`
(→ `bits overlay lcg`); `bits gc` and `bits publish --to s3` were removed outright.

### Confusing command pairs

| Pair | Acts on | Which is which |
|---|---|---|
| `clean` vs `prune` | build leftovers vs persistent workDir | `clean` deletes temporary build trees and installed versions no `latest` link points to; `prune` evicts packages by age, disk pressure or retention rules |
| `prune` vs `store gc` | persistent workDir vs shared S3 store | `prune` is local disk/age eviction; `store gc` is reachability GC of the S3 store |
| `stats` vs `store stats` | local build logs vs S3 store | `stats` reports a monitored build; `store stats` summarises store usage |
| `publish` vs `store upload` | CVMFS vs S3 store | `publish` puts a package on CVMFS; `store upload` writes a tarball to the S3 reuse store |
| `publish` vs `cvmfs publish` | one local package vs a whole build | `bits publish` publishes one package (or a release view) from the local work dir; `bits cvmfs publish` publishes every package of a build manifest from its store tarballs (the CI publish pipeline) |

---

### Work Directory Layout

After `bits build ROOT` the work directory (`sw/` by default) has this structure:

```
sw/
├── <arch>/                        ← architecture string (e.g. slc9_x86-64)
│   ├── <package>/
│   │   ├── <version>-<revision>/  ← installed package tree
│   │   │   ├── bin/, lib/, include/, etc/
│   │   │   └── etc/profile.d/init.sh
│   │   └── latest -> <version>-<revision>   ← convenience symlink
│   ├── <family>/<package>/…       ← same layout when package_family is set
│   └── Brewfile                   ← macOS only: Homebrew system layer (bits brew)
│
├── share/                         ← architecture-independent packages (architecture: share)
│   └── <package>/<version>-<revision>/
│
├── BUILD/                         ← temporary per-package build trees
│   └── <pkghash>/
│       ├── <package>/             ← $BUILDDIR during compilation
│       └── log                    ← build log (removed with the tree after a successful build)
│
├── TARS/                          ← content-addressed tarball store
│   └── <arch>/
│       ├── store/<h2>/<hash>/*.tar.gz
│       ├── <package>/<tarball> -> ../../store/…   ← by-name symlinks
│       └── dist/, dist-direct/, dist-runtime/     ← dependency-set symlinks
│
├── SOURCES/                       ← source checkouts and downloads
│   ├── <package>/<version>/<commit>/   ← $SOURCEDIR
│   └── cache/<h2>/<hash>/<filename>    ← downloaded source archives (sources: field)
│
├── REPOS/                         ← cached repository-provider checkouts
│   └── <provider>/<commit>/       ← recipe files live here
│
├── MODULES/                       ← modulefile cache for bits enter / load
│   ├── <arch>/
│   └── <build_id>/<arch>/         ← reuse overlays (bits import)
│
├── VIEWS/<arch>/                  ← merged views (--view)
│
├── MIRROR/                        ← git mirrors (--reference-sources)
│
├── SPECS/                         ← generated build scripts
│   └── <arch>/[<family>/]<package>/<version>-<revision>/
│
├── MANIFESTS/                     ← build manifests (see §25)
│   ├── bits-manifest-<timestamp>.json
│   └── bits-manifest-latest.json  ← symlink to most recent
│
└── STORE_CHECKSUMS/               ← integrity ledger (opt-in, see §21)
    └── TARS/<arch>/store/…/<tarball>.sha256
```

`BUILD/` directories are removed after a successful build unless `--no-auto-cleanup` is given (development packages from `bits init` keep theirs). Use `bits clean` to remove stale `BUILD/` and `TMP/` trees, or `bits prune` to evict old packages from `<arch>/` and `TARS/` based on age or disk pressure.

---

## 17. Recipe Format Reference

### File layout

```
<recipe-repo>.bits/
  <package>.sh         normal recipe
  defaults-<name>.sh   defaults profile
  patches/             patch files referenced by the patches: field
```

A recipe file consists of a YAML block, a `---` separator, and a Bash script:

```
<yaml header>
---
<bash build script>
```

The header ends at the first line that contains only `---` (surrounding spaces or
tabs allowed). A `---` that shares its line with other text (a comment or a value)
does not end it, but a bare `---` line inside a block scalar does. A file with
no such line is rejected ("recipe has no '---' front-matter terminator line"). The
same rule applies to `defaults-*.sh` files.

**Includes.** In the Bash body, a line consisting only of `#!include <path.sh>`
(resolved under the recipes repository root) or `#!include "path.sh"` (relative to
the recipe's own directory) is replaced by that file's content before variable
substitution and hashing, exactly as if it were written inline. Includes may nest;
cycles, absolute paths and paths escaping their base with `..` are rejected. Ordinary
`#include` lines (e.g. C code in a heredoc) are left alone. In the YAML header,
`key: !include file` inserts a file's content (`.yaml`/`.yml` parsed as YAML, `.json`
as JSON, anything else as text); the path is relative to the directory bits runs in.

### YAML header fields

#### Identity

| Field | Required | Description |
|-------|----------|-------------|
| `package` | Yes | Package name. The file name must be this name in lower case plus `.sh` (`ROOT` → `root.sh`), and dependants must spell it exactly as written here. |
| `version` | Yes, unless `version_from` is set | Version string (must be a YAML string). May use `%(year)s`, `%(month)s`, `%(day)s`, `%(hour)s`, `%(tag)s`, `%(commit_hash)s`, `%(short_hash)s` and the recipe's own `variables:`. |
| `version_from` | No | Name of a defaults `variables:` entry whose value becomes this package's `version`. For a package without `source:`/`sources:` it also sets `tag` and the commit hash, so a synthetic package (e.g. a release view) can follow a release variable. Fatal if the variable is not defined in the active defaults. |

#### Source

| Field | Description |
|-------|-------------|
| `source` | Git or Sapling repository URL. The repository is cloned / updated into `$SOURCEDIR`. |
| `tag` | Tag, branch, or commit to check out (default: `version`). May use the date substitutions (`%(year)s`, `%(month)s`, `%(day)s`, `%(hour)s`), other header fields such as `%(version)s`, and defaults or recipe `variables:`. |
| `sources` | List of source archive URLs (or local `file://` paths) to download before the build. Each file is placed in `$SOURCEDIR` and exposed as `$SOURCE0`, `$SOURCE1`, … An entry may start with `(regex)` to apply only on matching architectures (e.g. `(?!osx)https://…`) and may carry an inline checksum (see [Checksum verification](#checksum-verification)). If a recipe declares both `source:` and `sources:`, the defaults choose which one is built: `source_mode: tar` (default) or `git`, under `system:` or `variables:`; `BITS_SOURCE_MODE` overrides it for one build. |
| `patches` | List of patch file names, relative to the `patches/` directory of the recipe repository. They are copied to `$SOURCEDIR`, exposed as `$PATCH0`, `$PATCH1`, …, and by default applied with `patch -p1` before the recipe body runs. Each entry may carry an inline checksum, a `strip=N` level and/or a conditional matcher — see [Conditional patches](#conditional-patches). |
| `auto_patch` | Whether bits applies the `patches:` itself. Default `true`. With `false`, bits still stages the patch files in `$SOURCEDIR` and exports `$PATCH0`, … and `$PATCH_COUNT`, but the recipe body must apply them. The global `--no-auto-patch` flag or `auto_patch: false` in the active defaults turns it off for **every** package. See [Controlling patch application](#controlling-patch-application). |

Metadata / publish-policy fields — all **hash-excluded** (editing them never
rebuilds anything; see [Licence compliance and redistribution policy](#licence-compliance-and-redistribution-policy)):

| Field | Description |
|-------|-------------|
| `license` | SPDX licence identifier (`LicenseRef-*` for custom licences; `NOASSERTION` for system shims). Recorded in the build manifest, the publish BOM and the signed manifest; feeds the per-package and per-release `NOTICE` files and the `bits compliance` audit. |
| `acknowledgment` | Attribution text required by the licence; written into the per-package `NOTICE`. |
| `redistributable` | Which forms may be redistributed: `all` (default), `binaries`, `sources`, `none`. Restricted binaries are never uploaded to the store nor published to CVMFS; restricted sources are never mirrored to `SOURCES/cache/`. Legacy `true`/`false` = `all`/`none`; unknown values fail closed as `none`. |
| `description`, `url`, `homepage`, `source_url` | Free-text metadata, also hash-excluded. |

**Source archives detail.** When `sources:` is specified, bits downloads each file to `$SOURCEDIR` under its basename. Archives (`.tar.gz`, `.tgz`, `.tar.bz2`, `.tbz2`, `.tar.xz`, `.txz`, `.tar.zst`, `.zip`) are then **unpacked in place**, with the top-level directory common to all their entries stripped, so `$SOURCEDIR` holds the source tree; other files are left as they are. `$SOURCE_COUNT` holds the number of entries:

```yaml
sources:
  - https://example.com/mylib-1.0.tar.gz,sha256:e3b0c...
  - https://example.com/mylib-data-1.0.tar.gz
```

```bash
# Both archives are already unpacked into $SOURCEDIR; their names stay available
echo "building from $SOURCE0 ($SOURCE_COUNT source files)"
cmake -S "$SOURCEDIR" -B "$BUILDDIR" -DCMAKE_INSTALL_PREFIX="$INSTALLROOT"
```

**Patches detail.** Patch file names listed in `patches:` must exist in the `patches/` subdirectory of the recipe repository. They are copied to `$SOURCEDIR` and, by default, applied there in order with `patch -p1` before the recipe body runs:

```yaml
patches:
  - fix-include-order.patch
  - disable-broken-test.patch,md5:d41d8cd98f00b204e9800998ecf8427e
```

The `$PATCH0`, `$PATCH1`, … and `$PATCH_COUNT` variables are exported too; you only need them when you apply the patches yourself (next section).

##### Controlling patch application

By default bits applies the `patches:` list automatically (with `patch -p1`) before
the recipe body runs, and writes a `.bits_patched` sentinel so incremental rebuilds
don't double-apply. Sometimes a recipe needs to patch differently — a non-default
strip level, a patch that must be applied *after* an in-tree code generation step, or
a source tree that has to be rearranged first. For those cases you can turn the
automatic application **off** and do it yourself; the patch files are still staged in
`$SOURCEDIR` and named by `$PATCH0..$PATCH_COUNT` either way.

Three ways to disable automatic application, from most to least targeted:

- **Per recipe** — add `auto_patch: false` to the recipe header. Only that package is
  affected; everything else still auto-patches. This is almost always the right choice.
- **Whole build, command line** — pass `--no-auto-patch` to `bits build`. No package is
  auto-patched for that invocation.
- **Whole build, defaults profile** — add `auto_patch: false` to a `defaults-*.sh`
  file. Every build using that profile skips automatic patching.

A global switch (CLI flag or defaults) wins over the per-recipe field, and **every
patched recipe** is then responsible for applying its own patches or it will build
against unpatched sources.

When you take over, use the `bits_apply_patches` shell helper (available in every
recipe body) instead of hand-rolling the loop — it applies every staged patch in
order with a single strip level (per-entry `strip=N` is not used) and is idempotent
across incremental rebuilds:

```yaml
package: mylib
version: "1.0"
sources:
  - https://example.com/mylib-1.0.tar.gz
patches:
  - fix-include-order.patch
auto_patch: false        # bits stages the patches; we apply them ourselves
---
#!/bin/bash -e
function Configure() {
  cd "$SOURCEDIR"
  bits_apply_patches          # apply all staged patches with patch -p1
  # bits_apply_patches 0      # ...or a different strip level
  ./configure --prefix="$INSTALLROOT"
}
```

The build hash already includes every patch's content, so toggling `auto_patch` (or
editing the recipe body that now applies them) triggers a rebuild as expected.

**Per-patch strip level.** A patch authored with bare paths (or the `file.orig`
convention) can keep automatic application by declaring its level on the entry,
e.g. `- generator-fix.patch:strip=0`; bits then applies it with `patch -p0`
instead of the default `-p1`. `strip=N` is an apply option, not a gate: it may
stand alone or be `&&`-joined with matcher clauses (`foo.patch:(?cuda)&&strip=0`),
but not combined with `||`; a malformed form is an error. A declared level is part
of the build hash.

##### Conditional patches

A `patches:` entry may carry a `:matcher` suffix that gates whether the patch is
applied for a given build. This is the same matcher syntax used by conditional
`requires:`, plus a version comparison, and it is most useful when a patch only
applies to a particular upstream version:

```yaml
version: "v40r4"
patches:
  # only applied (and only hashed) when the resolved version is v40r2
  - "gaudi-GaudiToolbox.cmake.patch:version=v40r2"
  # always applied
  - gaudi-merge_confdb2_parts.patch
```

The matcher is evaluated against the **resolved** version (after defaults
`overrides:` and `requires:` pins), so the same recipe patches correctly whether
the version comes from the recipe, an override, or a pin. Inactive patches are
dropped *before* hashing, checkout and application, so they never affect the
build hash.

Matcher atoms:

- `version<op><value>` — `op` is one of `=`, `==`, `!=`, `<`, `<=`, `>`, `>=`;
  versions compare in **natural order** (`sort -V` semantics, so `v40r10 > v40r2`).
- `(?!osx)` / arch regex — matched against the architecture string (as in `requires:`).
- `defaults=<regex>` — active when the regex matches an active defaults profile.
- `(?VAR)` — active when the variable `VAR` is truthy (a defaults `variables:`
  entry, or a `--flavour` — see [Flavours](#flavours)).

Atoms combine with `&&` (all) and `||` (any); `||` has the lower precedence, e.g.
`version>=v40r2 && version<v41r0` or `(?cuda) || version<v40r0`. A single `|`
inside an arch regex stays ordinary alternation — only the doubled `||` combines.
If a patch carries both a matcher and an inline checksum, write them as
`name:matcher,algo:digest` (the checksum comes last). The same matcher grammar
is also accepted on `requires:`/`build_requires:` entries (there `version` means the
requiring package's own version) and on defaults `overrides:` keys.

##### Flavours

Flavour variables let a single build be tuned without editing defaults files.
They feed the `(?NAME)` matcher above and are also exported into the build
environment, so a recipe body can read them:

```bash
bits build --defaults gcc15::dev4 --flavour cuda --flavour onnx=cpu key4hep
```

Grammar (repeatable, comma-separated): `NAME` → `true`, `NAME=VALUE` → `VALUE`,
`!NAME` → `false`. A value is *truthy* unless it is empty, `0`, `false`, `off`,
or `no`. Each flavour is merged into the defaults' `variables:` map (so `(?NAME)`
sees it) **and** the `env:` map (so it is exported as `$NAME` in every recipe's
build and contributes to the package hash). A `--flavour` overrides a defaults
`variables:`/`env:` entry of the same name.

`NAME` must be a plain identifier — `[A-Za-z_][A-Za-z0-9_]*` (a letter or
underscore, then letters/digits/underscores). **Hyphens are not allowed:** use
`use_openloops`, not `use-openloops`. The `(?NAME)` matcher only recognises an
identifier, so a hyphenated name is silently treated as an architecture regex
(and its gate never fires); names are also exported as `$NAME` env vars, which
cannot contain `-`. The same rule applies to defaults `variables:` keys.

Because flavours enter the shared `defaults-release` environment, they are
**global** to the build (they gate dependencies anywhere in the DAG, not just on
the named package) and changing one re-hashes the affected packages, triggering
a rebuild — the same as changing a defaults `env:` value.

##### Defaults `variables:` and predefined platform variables

The `(?NAME)` matcher reads from three merged sources:

- a `--flavour NAME[=VALUE]` on the command line (above);
- a `variables:` entry in any active defaults file;
- **predefined platform variables** derived from the architecture — on
  `osx_arm64` these are `osx`, `arm64`, and `aarch64` (the full set is `osx`,
  `linux`, `arm64`, `aarch64`, `x86_64`; only those that apply are set). So
  `pkg:(?osx)` is a macOS-only dependency and `pkg:(?linux)` its counterpart. Note
  `pkg:(?!osx)` is a negative-lookahead **regex** matched against the architecture
  string, *not* variable negation — there is no variable-negation atom.

A defaults `variables:` entry is either a plain `name: value`, or a **gated**
form that only takes effect when its own matcher (same grammar as above) holds:

```yaml
variables:
  cuda: false                        # plain default
  use_openloops:
    value: true
    when: "(?openloops) && (?!osx)"  # only with --flavour openloops, and off macOS
```

A truthy value is anything except empty, `0`, `false`, `off`, or `no`. A
`--flavour` of the same name overrides a defaults `variables:` value.

##### `variables` vs `env` vs `flavours` — quick comparison

These three are easy to confuse. `variables:` is **text templating only** (Python
`%(NAME)s`, never a shell variable); `env:` is a **shell variable only** (`$NAME`,
never `%(NAME)s`); a `flavour` is a CLI knob that feeds **both** at once. All keep
the name **verbatim** — none of them upper-cases it.

| | `variables:` | `env:` | `flavours` (`--flavour`) |
|---|---|---|---|
| Defined in | defaults / recipe `variables:` | defaults `env:` | CLI (repeatable) |
| Surface in recipe | `%(NAME)s` (text) | `$NAME` (shell) | both `%(NAME)s` **and** `$NAME` |
| Gates `(?NAME)` in requires/patches/overrides | defaults `variables:` only | no | yes |
| Exported into build shell | no | yes (via `defaults-release`) | yes |
| In the package hash | only through the text they expand into: recipe `variables:` in `version`/`tag`/`source`/`sources`/`patches`/body; defaults `variables:` in `tag` and the body | yes (via the `defaults-release` environment) | yes (both paths) |
| When evaluated | build-time text substitution, **before** hashing | exported into the shell before the recipe body runs | both |
| Name case | verbatim | verbatim | verbatim |

To use one name as *both* `%(NAME)s` and `$NAME`, pass it as a `--flavour`, or
define it in **both** `variables:` and `env:`.

> **Auto-uppercased shell variables are a separate, per-package mechanism.** For
> every dependency, bits exports `<PKG>_ROOT`, `<PKG>_VERSION`, `<PKG>_REVISION`,
> `<PKG>_HASH`, and `<PKG>_COMMIT`, where `<PKG>` is the package name with every
> non-alphanumeric character turned into `_`, then upper-cased: `boost` →
> `$BOOST_ROOT`, `common.bits` → `$COMMON_BITS_ROOT`, `o2.framework` →
> `$O2_FRAMEWORK_ROOT`. The same transform backs `%(root_dir)s` (→ `${<PKG>_ROOT}`).
> This is keyed off the **package name**, not off any `variables`/`env`/`flavour`
> entry — those keep whatever case you write.

#### Dependencies

| Field | Description |
|-------|-------------|
| `requires` | Runtime + build-time dependencies. |
| `build_requires` | Build-time-only dependencies (e.g. `cmake`, `ninja`). |
| `runtime_requires` | Not a recipe input: bits fills it from the resolved `requires:` list, so a value written in a recipe is ignored. |
| `untracked_requires` | Runtime-linked dependencies **left out of this package's identity hash**: changing one rebuilds only the dependency itself (hashed normally), not this package or anything above it. Meant for iterating on a dependency you control. **You are responsible for ABI compatibility** — a reused consumer is not recompiled against the new dependency. Builds whose closure includes one are recorded as `provenance: loose` in `.meta.json` (still publishable). Give the dependency an explicit `force_revision` (`""` or a fixed label) to keep its install path stable; without one bits warns, and under `revision_policy: "hash"` it stops. |

Each entry in `requires` / `build_requires` is a string in one of these forms:

| Form | Meaning |
|------|---------|
| `name` | Plain dependency. |
| `name:matcher` | Conditional dependency. `matcher` uses the [patch matcher grammar](#conditional-patches): an architecture regex matched from the start of the architecture string (`(?!osx)` for non-macOS, `osx` for macOS only), `defaults=<regex>`, `(?VAR)`, or `version<op><value>` on the requiring package's version, combined with `&&`/`\|\|`. |
| `name = version` | Pin the dependency to `version` (sets both its `version` and `tag`). |
| `name = version:matcher` | Version pin that applies only when `matcher` is satisfied. |

Only one version pin per dependency is allowed across the whole graph; conflicting pins (or a pin that arrives after the dependency was already resolved) abort the build. Prefer the defaults `overrides:` block (see [Defaults Profiles](#18-defaults-profiles)) for version pinning; the in-recipe `= version` form is for constraints that belong to the consuming package.

#### Environment exported by this package

| Field | Description |
|-------|-------------|
| `env` | Key-value pairs exported when this package is loaded via `modulecmd`. |
| `prepend_path` | Variables to prepend to (e.g. `PATH`, `LD_LIBRARY_PATH`). |
| `append_path` | Variables to append to. |

#### System-package integration

| Field | Description |
|-------|-------------|
| `prefer_system` | Architecture regex. When it matches (or with `--always-prefer-system`), bits runs `prefer_system_check`. |
| `prefer_system_check` | Bash snippet (sees `$REQUESTED_VERSION`). Exit 0 to use the system package instead of building it; non-zero to build it. |
| `system_requirement` | Architecture regex. When it matches, bits runs `system_requirement_check`; a non-zero exit aborts the build with a missing-requirement error. Such a recipe must have an empty body. |
| `system_requirement_check` | Bash snippet that checks for the required system package. |
| `system_requirement_missing` | Message (e.g. install instructions) printed by `bits doctor` when `system_requirement_check` fails. |

#### Repository provider

| Field | Description |
|-------|-------------|
| `provides_repository` | Set to `true` to mark this recipe as a repository provider. |
| `tag` | The git ref of the provider repository to clone — a branch, tag, or commit hash. Selects which snapshot of the recipe repository is pulled (falls back to `version`, then the repo's default branch). The resolved commit is recorded in the build manifest; it does not enter package build hashes (a package's hash depends only on its own recipe, sources and dependencies). |
| `always_load` | Set to `true` (alongside `provides_repository: true`) to clone this provider unconditionally at startup, before any dependency-graph traversal. Recipes in the provider's repository are then visible to all packages without requiring an explicit dependency. |
| `repository_position` | `append` (default) or `prepend` — where to insert the cloned directory in `BITS_PATH`. A provider cannot grant itself `prepend`: it is honoured only when the operator allows it with `--provider-policy NAME:prepend` (see [Provider policy](#provider-policy)); otherwise bits appends. |

The bits-providers repository URL itself accepts an `@<tag>` suffix (`$BITS_PROVIDERS`; without it, `main`), e.g. `https://github.com/bitsorg/bits-providers@<tag>`. A defaults `overrides:` entry for a provider package (`source:` and/or `tag:`, which may use `%(var)s` from `variables:`) does change which repository and snapshot is cloned; the bits-providers registry itself is chosen only by `$BITS_PROVIDERS` and its `@<tag>` suffix.

When `provides_repository: true` is set, the package's `source` URL must point to a git repository containing recipe files. It is cloned before the main build and its directory added to `BITS_PATH`. With `always_load: true` the clone happens unconditionally at startup, before dependency resolution, rather than only when the package appears in the dependency graph. See [§13](#13-repository-provider-feature) for full details.

#### Memory-aware parallelism

bits sets `$JOBS` for each package build so that concurrent `--parallel` builds do not oversubscribe the machine. Two limits apply:

- **CPU / load (all packages).** `$JOBS` is capped at `ceil(requested × oversubscribe ÷ builders)`, so the combined `-j` of all builders stays near the single-builder budget (`oversubscribe` comes from `--oversubscribe`, default 1.0).
- **Memory (all packages).** The available memory is split across the concurrent builders and divided by the per-job footprint: the recipe's `mem_per_job`, or else `mem_per_job_default` under the defaults `system:` block (2 GiB when unset; `0`/`off` removes the cap for recipes without `mem_per_job`).

The result is `min(requested, ceil(requested × oversubscribe ÷ builders), floor((available ÷ builders) × utilisation ÷ mem_per_job))`, and at least 1. With `--parallel 1` only the memory cap can lower `$JOBS`.

The **final (top-level) package** is exempt from the `÷ builders` CPU split: it depends on every other package, so it builds alone once they finish, and dividing its `-j` would needlessly starve the largest compile of the run. It is computed as if `builders = 1` — i.e. the full `-j`, bounded only by the (now full-RAM) `mem_per_job` cap. Controlled by [`--unleash-final` / `--no-unleash-final`](#bits-build) and `build_unleash_final:` under the defaults `system:` block (default on for `--parallel > 1`). `$JOBS` never enters a package hash, so this is wall-time-only build-host policy.

| Field | Description |
|-------|-------------|
| `mem_per_job` | Expected peak RSS per parallel compilation process. Accepts a plain integer (MiB) or a string with a unit suffix: `512`, `"1500"`, `"1.5 GiB"`, `"2 GB"`. When set, bits samples available system memory at the start of the package's build and applies the memory term above. Without it, `mem_per_job_default` (2 GiB unless the defaults change it) is assumed. |
| `mem_utilisation` | Fraction of available memory bits may commit, in the range `0.0`–`1.0`. Default: `0.9`. Applies to the memory cap, whether the footprint comes from `mem_per_job` or the host default. |

See also `--build-nice` ([bits build options](#bits-build)) for staggering the *priority* of concurrent builders on top of these caps.

Examples:

```yaml
# LLVM — each clang process can peak at ~2 GiB with LTO
mem_per_job: 2048

# ROOT — template-heavy; be more conservative on shared hosts
mem_per_job: 1500
mem_utilisation: 0.80
```

#### Build sandbox

| Field | Description |
|-------|-------------|
| `sandbox_network` | Outgoing network access for the build script when it runs in a sandbox. `on` — network is **blocked**; `off` — network is **allowed** (for recipes that `pip install` or `gem install` at build time). Without the field, `--sandbox-network` or `sandbox_network:` under the defaults `system:` block decides, else `on`. May also be given under the recipe's `system:` block; the top-level field wins. Ignored when `--sandbox=off`. See [§22.1 Recipe Sandbox](#221-recipe-sandbox). |

Example:

```yaml
package: my-python-tool
version: "1.0"
tag: v1.0
sandbox_network: off   # allow pip install during build
---
pip install -r requirements.txt
```

#### Checksum verification

Each entry in the `sources` and `patches` lists may carry an inline checksum using a comma suffix:

```
<url-or-filename>,<algorithm>:<hexdigest>
```

The checksum is appended after the **last comma** in the entry. Bits recognises a suffix as a checksum only when it matches the pattern `<algo>:<hex>` where `<algo>` is one of `sha256`, `sha512`, `sha1`, or `md5` (case-insensitive). This means URLs that happen to contain commas in query parameters (e.g. `https://example.com/file?a=1,2`) are handled safely — only a suffix that looks like an actual checksum is stripped.

Examples:

```yaml
sources:
  # Plain entry — no verification
  - https://example.com/mylib-1.0.tar.gz

  # SHA-256 checksum declared inline
  - https://example.com/mylib-1.0.tar.gz,sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855

  # SHA-512 is also supported
  - https://example.com/data.tar.bz2,sha512:cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e

patches:
  # Patch with MD5 checksum
  - fix-build.patch,md5:d41d8cd98f00b204e9800998ecf8427e
```

The `sources` entries are used to populate the `$SOURCE0`, `$SOURCE1`, … environment variables inside the build script. Bits automatically strips the checksum suffix before setting these variables, so the build script always sees a clean filename or URL.

The enforcement behaviour is controlled by the `--check-checksums`, `--enforce-checksums`, and `--print-checksums` CLI flags (see [§16](#16-command-line-reference)) and by the per-recipe field below:

| Field | Description |
|-------|-------------|
| `enforce_checksums` | Set to `true` to verify this package's checksums in `enforce` mode even when the defaults profile asks for less. A `--print-checksums`, `--enforce-checksums` or `--check-checksums` flag still takes precedence. |

Mode precedence (highest wins): `--print-checksums` > `--enforce-checksums` > `--check-checksums` > recipe `enforce_checksums: true` > defaults `checksum_mode:` > `off`.

| Mode | Behaviour |
|------|-----------|
| `off` (default) | Checksums in the recipe are stored but never evaluated. |
| `warn` | A declared checksum is verified; a mismatch emits a warning and the build continues. |
| `enforce` | A declared checksum is verified and must match; the build aborts on mismatch. A **missing** checksum also aborts the build, however `enforce` was selected. |
| `print` | No verification. After the build, bits prints the SHA-256 of every source (from `SOURCES/cache/`) and patch, including packages taken from cache. Use this to populate recipes with correct checksums for the first time. |

#### External checksum files

As an alternative to embedding checksums inline, a recipe repository may store them in a dedicated sidecar file. This keeps recipes readable and makes automated checksum management simpler.

**File location:** `<recipe-repo>.bits/checksums/<pkgname>.checksum`

The `checksums/` directory is optional. If the file does not exist, bits falls back to any inline comma-suffix values in the recipe.

**File format (YAML):**

```yaml
# checksums/mylib.checksum
# Re-generate with:  bits checksums --write mylib

commits:                  # git tag -> pinned commit SHA
  v1.0: abc123def456abc123def456abc123def456abc1

sources:
  https://example.com/mylib-1.0.tar.gz: sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
  https://example.com/extra-data.tar.bz2: sha512:cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e

patches:
  fix-endian.patch: sha256:a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3
  add-missing-header.patch: md5:d41d8cd98f00b204e9800998ecf8427e
```

All sections are optional. `commits` maps a git `tag:` to the **pinned commit SHA** expected after checking out `source:` + `tag:`. This protects against tag movement (force-pushed tags pointing to a different commit). Keyed by tag, pins for the recipe's own tag and for the tags defaults profiles override it to coexist; a branch is never pinned, since it moves. The value is a bare 40-character (SHA-1) or 64-character (SHA-256) hex string without an algorithm prefix. The older single `tag: <sha>` pin is still honoured, but only while the recipe's own tag, version and source are built: an override or version pin that changes them drops it.

**Profile repositories:** a repository providing an active `defaults-*.sh` profile (e.g. `stacks.bits`) may carry `checksums/<pkgname>.checksum` files for the sources and tags its overrides introduce. They are merged over the recipe repository's file, per entry. `bits checksums --defaults` writes them.

**Merge semantics — external file wins:** if a URL or patch filename appears in both the checksum file and as an inline comma-suffix in the recipe, the checksum file value takes precedence. This makes the checksum file the single authoritative security artefact while retaining the inline syntax as a convenient fallback for simple cases.

**Generating checksum files:** run `bits checksums --write` in a recipe repository to record every recipe's checksums without building (see [bits checksums](#bits-checksums)), or `bits build --write-checksums <package>` to record those of the packages a build fetched (for a package whose sources a defaults profile's override changed, into that profile's repository). Both add new entries to the existing file, keeping its comments, and never overwrite one that disagrees. Subsequent builds will pick it up without any further changes to the recipe `.sh` file.

**Commit pin enforcement:** the commit pin is verified using the same `--check-checksums` / `--enforce-checksums` modes as source and patch checksums. A mismatch means the tag has been moved to a different commit since the checksum file was generated.

#### Miscellaneous

| Field | Description |
|-------|-------------|
| `valid_defaults` | List of defaults profiles this recipe is compatible with. |
| `incremental_recipe` | Bash snippet for fast incremental (development) rebuilds. |
| `relocate_paths` | Paths to rewrite when relocating an installation. |
| `variables` | Custom key-value pairs for `%(name)s` substitution in `version`, `tag`, `source`, `sources`, `patches` and the recipe body. Setting it (or `expand_recipe: true`) makes an unknown `%(name)s` in the body an error; otherwise only known variables are replaced there. |
| `from` | Recipe inheritance: names a recipe directory (relative to the recipes root) whose recipe with the same file name is the parent. The child's header keys replace the parent's and its body is placed before the parent's; `merge_policy:` (`remove`, `inherit`, `merge` key lists) adjusts this. |
| `architecture` | Set to `share` to mark a package as architecture-independent (see [§19](#19-architecture-independent-shared-packages)). The older spelling `shared` is no longer recognised. |
| `own_hash` | Set to `true` for a package whose output does not depend on the community/build-type defaults — the compiler toolchain — so one build is reused across them. See [Shared toolchains](#shared-toolchains-own_hash) below. |
| `view` | How the package appears in a release's merged view (see [bits store / bits cvmfs](#bits-store--bits-cvmfs-admin--ci-groups)): `false` keeps it out; a mapping with `exclude:` / `include:` path lists shapes it. `true` also makes `bits enter` / `bits setenv` turn on `--view` automatically when the package is loaded. Hash-excluded (presentation only). |

#### Shared toolchains (`own_hash`)

`own_hash: true` gives a package an identity independent of the ambient defaults,
so one build (typically GCC) serves several communities and build types:

- **Hash.** The merged `defaults-release` is left out of its identity hash (it is
  still a dependency, and still in the dependency hash that drives development
  rebuilds), and the build container's fingerprint is folded in
  (`/opt/bits/container-fingerprint.hash` in a bits-containers image; `none` for a
  native build). A `--docker` build whose image has no readable fingerprint stops
  with an error rather than hash a container build under the native identity.
- **Architecture.** It is installed, stored and published under a
  build-type-neutral architecture: the combined architecture without the
  `append_arch` qualifiers a defaults file marks `own_hash_neutral: true` (e.g.
  `-opt`/`-dbg`); compiler qualifiers such as `-gcc15` are kept. Dependants'
  `init.sh` source it from that path, and when it is reused bits links the
  build-arch path to the neutral one so references baked by older builds resolve.
- **Provenance.** Its `.meta.json` records `provenance: own_hash`, with
  `defaults-release` listed in `untracked_requires`.

### Build-time environment variables

For the complete reference of all variables injected by bits into each package build script, see [§20 Environment Variables — Recipe build-time variables](#20-environment-variables). The key variables are `$INSTALLROOT`, `$BUILDDIR`, `$SOURCEDIR`, `$JOBS`, `$PKGNAME`, `$PKGHASH`, `$SOURCE0`/`$SOURCEn`, `$PATCH0`/`$PATCHn`, and `${DEP_ROOT}` for each dependency.

---

## 18. Defaults Profiles

A **defaults profile** is a special recipe file named `defaults-<name>.sh` that lives in the recipe repository alongside ordinary package recipes. It is not a buildable package — its Bash body is never executed. Instead, its YAML header carries **global configuration** that is applied across the entire dependency graph before any package is resolved.


### Selecting a profile

The active profile is selected with `--defaults PROFILE`. If the flag is omitted, bits uses `release`, loading `defaults-release.sh`. Any other selection is layered on top of `release`: `--defaults dev` behaves like `release::dev` (a missing `defaults-release.sh` is skipped).

`defaults-release.sh` occupies a privileged position: every package in the build graph automatically depends on a pseudo-package named `defaults-release`, which is fulfilled by whatever profile(s) are loaded. This is the mechanism that injects the global `env:` block into every package's `init.sh`.


---

### Combining multiple profiles with `::`

Two or more profiles can be combined in a single `--defaults` value using `::` as a separator:

```
bits build --defaults dev::gcc13 MyPackage
```

This loads `defaults-release.sh`, `defaults-dev.sh` and `defaults-gcc13.sh` (in that order) and deep-merges their YAML headers left to right into a single configuration: scalars from the later file win, lists are concatenated, dicts are recursively merged.

> **Note:** `release` is prepended automatically unless it already appears in the chain, so `--defaults myproject` and `--defaults release::myproject` are equivalent. Name `release` explicitly only to place it somewhere other than first.


---

### File syntax

A defaults file is a standard bits recipe file. The YAML header supports a superset of ordinary recipe fields:

```yaml
package: defaults-release          # must match the file name (without .sh)
version: v1                        # required; used in the spec but not for building

# ── Global environment ────────────────────────────────────────────────────────
env:
  CXXSTD: '20'
  CMAKE_BUILD_TYPE: 'Release'
  MY_GLOBAL_FLAG: '-O3'

# ── Disable packages ──────────────────────────────────────────────────────────
disable:
  - alien
  - monalisa

# ── Architecture / defaults compatibility ─────────────────────────────────────
valid_defaults:
  - release
  - o2

# ── Per-package overrides ─────────────────────────────────────────────────────
overrides:
  ROOT:
    version: "6-30-06"
    requires:
      - Python
      - XRootD

  # Regular expression matching — this applies to any package starting with "O2"
  O2.*:
    env:
      O2_BUILD_TYPE: Release

  # Remote tap — load ROOT from a specific git ref in the recipe repo
  ROOT@v6-30-06-alice1:

# ── Package families (optional) ───────────────────────────────────────────────
package_family:
  default: cms
  lcg:
    - ROOT
    - SCRAMV1
    - demo2
  cms:
    - data-*
    - coral
---
# Any Bash body is ignored (bits warns if it contains more than comments).
```


---

### YAML fields specific to defaults files

| Field | Description |
|-------|-------------|
| `env` | Key-value pairs exported into every package's `init.sh` (via `defaults-release` auto-dependency). Equivalent to setting the same `env:` in every recipe. |
| `disable` | List of package names to exclude from the dependency graph. |
| `overrides` | Map keyed by package name or regex, matched against the whole name case-insensitively. Each field in an entry **replaces** that field of the recipe (lists and maps are not merged with the recipe's). A key may carry a `:matcher` suffix to apply only when it holds (e.g. `ROOT:osx`), or an `@ref` suffix to read the recipe from that git ref of the recipe repository. A list item `name = version` is shorthand for setting `version` and `tag`. |
| `valid_defaults` | Restricts which profiles may be used. Each component of the `::` chain is checked, except `release` and profiles marked `valid_defaults_exempt: true`; bits aborts if one is not listed. |
| `package_family` | Optional install grouping; see [Package families](#package-families) below. |
| `qualify_arch` | Set to `true` to append **all** non-`release` default names to the install architecture string; see [Qualifying the install architecture](#qualifying-the-install-architecture) below. |
| `append_arch` | String appended **verbatim** to the install architecture string, **only for this defaults file** (include the separator, e.g. `-gcc13`). Unlike `qualify_arch`, which qualifies with every default name in the chain, each file opts in independently and chooses the exact string; see [Selective qualification with append_arch](#selective-qualification-with-append_arch) below. |
| `own_hash_neutral` | Set to `true` next to an `append_arch` (typically a build type such as `opt`/`dbg`) to leave that qualifier out of the architecture of `own_hash` packages, so one toolchain build serves every build type. See [Shared toolchains](#shared-toolchains-own_hash). |
| `docker_registry` | (in `defaults-release`) Registry the default `--docker` builder image is taken from; `$BITS_DOCKER_REGISTRY` wins. See [§22](#22-docker-support). |
| `checksum_mode` | Base checksum verification policy for every build using this profile. Accepted values: `off` (default), `warn`, `enforce`, `print`. Equivalent to passing the corresponding `--*-checksums` flag on every invocation. CLI flags override this setting; see [Checksum policy in defaults profiles](#checksum-policy-in-defaults-profiles) below. |
| `write_checksums` | Set to `true` to automatically write/update `checksums/<pkg>.checksum` files after every build. Equivalent to passing `--write-checksums` on every invocation. The CLI flag overrides this setting. |
| `variables` | Values for `(?NAME)` matchers and `%(NAME)s` substitution; see [Defaults `variables:` and predefined platform variables](#defaults-variables-and-predefined-platform-variables). |
| `auto_patch` | `false` turns off automatic patch application for every package; see [Controlling patch application](#controlling-patch-application). |
| `force_revision`, `revision_policy` | Revision labels for installed packages; see [Forcing or dropping the revision suffix](#forcing-or-dropping-the-revision-suffix-force_revision). |
| `system` | Build-host and publishing settings, e.g. `remote_store`, `sandbox_network`, `mem_per_job_default`, `build_oversubscribe`, `build_unleash_final`, `source_mode` and the CVMFS path templates (see [CVMFS layout](#cvmfs-layout)). |


---

### Role in the build pipeline

Defaults are applied in two steps:

**1. Before package resolution**, bits loads each profile in the chain, merges their YAML headers into one configuration, overlays an architecture-specific file if one exists (e.g. `defaults-slc9_x86-64.sh`), then extracts:

- `disable` — packages to exclude from the build graph entirely.
- `env` — environment variables propagated to every package's `init.sh` (injected via the `defaults-release` pseudo-dependency).
- `overrides` — per-package YAML patches applied after the recipe is parsed (see below).
- `package_family` — optional install grouping (see [Package families](#package-families) below).
- `requires` / `build_requires` — repository providers (packages with `provides_repository: true`) to clone and add to `BITS_PATH` for builds using this profile. They are used only to find and clone providers and are **not** added as regular build dependencies (to avoid a dependency cycle — see [Triggering providers from a defaults file](#triggering-providers-from-a-defaults-file) in §13).

**2. As each recipe is parsed**, every `overrides` entry whose key matches the package name (a case-insensitive regex over the whole name) is applied: each field it sets replaces the recipe's value. This means a defaults file can change any recipe field — version, `requires`, `env`, `prefer_system`, etc. — for targeted packages.


---

### Checksum policy in defaults profiles

Groups that require a consistent security policy can embed it directly in the defaults file rather than relying on every developer to remember the right CLI flag:

```yaml
# In defaults-production.sh — enforce checksums on all builds using this profile
checksum_mode: enforce

# Also regenerate checksums automatically after each build
write_checksums: true
```

**Accepted values for `checksum_mode`:**

| Value | Behaviour | CLI equivalent |
|-------|-----------|----------------|
| `off` | No verification (default) | *(none)* |
| `warn` | Verify declared checksums; warn on mismatch; ignore missing | `--check-checksums` |
| `enforce` | Verify declared checksums; abort on mismatch; abort if any declaration is missing | `--enforce-checksums` |
| `print` | Compute and print checksums after the build; no verification | `--print-checksums` |

**Precedence (highest → lowest):**

1. CLI flag (`--print/enforce/check-checksums`) — unconditional override for this run.
2. Per-package recipe field (`enforce_checksums: true`) — opts that package into `enforce` mode regardless of the profile.
3. Defaults profile `checksum_mode:` — site-wide base policy.
4. `off` — no verification if nothing is configured.

**Timing:** `warn` and `enforce` fire during source download (before compilation), acting as a security gate. `print` and `write` operations run as a single consolidated pass **after all packages have finished building**. This means they cover packages whose binary tarball was already cached (and whose sources were not re-downloaded during this run), as long as the source files are still present in `SOURCES/cache/`.


---

### Package families

The `package_family` key enables optional **install-path grouping**. When present, bits inserts an extra directory segment between the architecture and the package name in every path where the package appears:

```
sw/<arch>/<family>/<package>/<version>-<revision>/
```

Without `package_family` the layout is the legacy two-level form and everything is fully backward compatible:

```
sw/<arch>/<package>/<version>-<revision>/
```

#### Configuration

```yaml
package_family:
  default: cms          # fallback family for any package not matched below
  lcg:
    - ROOT
    - SCRAMV1
    - demo2
  cms:
    - data-*            # fnmatch glob — matches data-Geometry, data-L1T, …
    - coral
```

`default` is optional. When omitted, any package that does not match any pattern gets an empty family and falls back to the legacy two-level layout. This means you can roll out families incrementally — only packages explicitly listed get a family segment; everything else is unchanged.

#### Matching rules

- Patterns are shell-style globs, matched case-sensitively: `*` matches any sequence of characters, `?` a single character.
- Families are tried in definition order; the **first match wins**.
- The `default` key is a fallback, not a pattern list, so it is never tried as a family name during matching.
- A package may only belong to one family.

#### What the family segment affects

Every place that bits constructs a path based on the install location is family-aware:

| Path type | Without family | With family `lcg` |
|-----------|---------------|------------------|
| Install dir | `sw/<arch>/ROOT/v6-30-06-1/` | `sw/<arch>/lcg/ROOT/v6-30-06-1/` |
| `$ROOT_ROOT` in `init.sh` | `…/$BITS_ARCH_PREFIX/ROOT/v6-30-06-1` | `…/$BITS_ARCH_PREFIX/lcg/ROOT/v6-30-06-1` |
| Dep sourcing in `init.sh` | `. …/ROOT/v6-30-06-1/etc/profile.d/init.sh` | `. …/lcg/ROOT/v6-30-06-1/etc/profile.d/init.sh` |
| `SPECS/` script dir | `SPECS/<arch>/ROOT/v6-30-06-1/` | `SPECS/<arch>/lcg/ROOT/v6-30-06-1/` |
| `latest` symlink parent | `sw/<arch>/ROOT/` | `sw/<arch>/lcg/ROOT/` |
| Shell build `$PKGPATH` | `<arch>/ROOT/<version>-<revision>` | `<arch>/lcg/ROOT/<version>-<revision>` |
| `$PKGFAMILY` env var | _(empty)_ | `lcg` |

The content-addressed tarball store (`TARS/<arch>/store/<h2>/<hash>/`) and the TARS convenience symlinks are **not** family-aware — they are indexed by hash, not by install path.

#### Dependency paths in `init.sh`

Each dependency's sourcing line uses **that dependency's own family**, not the family of the package being built. If `MyPkg` (family `cms`) depends on `ROOT` (family `lcg`), the generated `init.sh` for `MyPkg` contains:

```bash
[ -n "${ROOT_REVISION}" ] || \
  . "$WORK_DIR/$BITS_ARCH_PREFIX"/lcg/ROOT/v6-30-06-1/etc/profile.d/init.sh
```

and exports:

```bash
export MYPKG_ROOT="$WORK_DIR/$BITS_ARCH_PREFIX"/cms/MyPkg/v1-1
```

This means every package in a mixed-family build is correctly self-describing in its `init.sh` without any additional configuration.

#### Backward compatibility guarantee

`package_family` is entirely opt-in. When the key is absent from all defaults files:

- every package has an empty family, and `$PKGFAMILY` is exported as an empty string;
- `$PKGPATH`, the `init.sh` paths, `SPECS/` and the `latest` symlinks use the original `<arch>/<package>/…` layout.

An existing recipe repository with no `package_family` key will produce bit-for-bit identical install trees, tarballs, and hashes compared to a build that predates the feature.

---

### Qualifying the install architecture

By default all packages built with any set of defaults land under the same architecture directory (e.g. `sw/slc7_x86-64/`). If you maintain two profiles that are **incompatible with each other** — for example `gcc12` and `gcc13` — builds from one profile will silently overwrite the install tree of the other.

Bits provides two complementary mechanisms to add a qualifying suffix to the architecture string (e.g. `slc7_x86-64-gcc13`). The combined string is then used for the install tree, tarballs, and `init.sh` generation.

#### How the combined architecture is used

Whichever mechanism is active, the derived string is used consistently for:

- **Install tree** — `sw/<combined_arch>/<package>/<version>-<revision>/`
- **`BITS_ARCH_PREFIX` default** in every `init.sh` — so the environment resolves to the right prefix at runtime
- **`$EFFECTIVE_ARCHITECTURE`** passed to the build script
- **`TARS/<combined_arch>/`** symlink directories and store paths — ensuring tarballs from different defaults combinations do not collide

The original platform architecture (`slc7_x86-64`) is still passed to the build script as **`$ARCHITECTURE`** (used for platform detection such as the macOS `${ARCHITECTURE:0:3}` check) and to system-package preference matching, so build scripts need no changes.

Packages that declare `architecture: share` (see [§19](#19-architecture-independent-shared-packages)) are **unaffected** by either mechanism: their effective architecture is always `share` regardless of which defaults are active. `own_hash` packages drop the `append_arch` qualifiers marked `own_hash_neutral: true` (see [Shared toolchains](#shared-toolchains-own_hash)).

##### Entering a qualified-architecture build

The module frontend (`bits enter`/`q`/`load`) auto-detects only the **raw**
architecture, so when a build was qualified you must point it at the combined
string. After a successful qualified build the success banner prints the exact
command, e.g. `bits -a slc7_x86-64-dev-gcc13 enter MyPackage/latest-…`, and
suggests `export BITS_ARCHITECTURE=slc7_x86-64-dev-gcc13` to make it the default
for the session. As a convenience, when `-a` is not given and the detected raw
architecture has no install tree under the work dir, the frontend uses the sole
architecture present (if there is exactly one) or warns and lists them (if
several) instead of silently picking one. An explicit `-a` is always respected.

---

#### Global qualification with `qualify_arch`

Setting `qualify_arch: true` in **any** defaults file instructs bits to append **every non-`release` default name** in the chain to the architecture string. For example:

```
bits build --defaults dev::gcc13 MyPackage
```

with `qualify_arch: true` in `defaults-gcc13.sh` installs everything under:

```
sw/slc7_x86-64-dev-gcc13/
```

instead of the plain `sw/slc7_x86-64/`. The `release` component is never appended (it is the implicit baseline); all other components are joined with `-` in the order they appear on the command line.

```yaml
# defaults-gcc13.sh
package: defaults-gcc13
version: v1
qualify_arch: true            # ← all non-release defaults are appended
env:
  CC: gcc-13
  CXX: g++-13
```

The trade-off is that **every** default in the chain contributes to the suffix. With a long chain like `--defaults release::base::gcc13::cuda`, the install tree becomes `slc7_x86-64-base-gcc13-cuda` — which may include components (like `base`) that do not actually affect binary compatibility.

---

#### Selective qualification with `append_arch`

`append_arch` is a per-file alternative that gives each defaults file independent control over its contribution to the architecture suffix. Only files that declare `append_arch` add anything to the suffix; the rest are transparent.

```yaml
# defaults-gcc13.sh
package: defaults-gcc13
version: v1
append_arch: -gcc13           # ← only this file contributes "-gcc13"
env:
  CC: gcc-13
  CXX: g++-13
```

```yaml
# defaults-release.sh
package: defaults-release
version: v1
                              # ← no append_arch → contributes nothing
```

With `--defaults release::gcc13`, the effective architecture is:

```
sw/slc7_x86-64-gcc13/
```

`release` adds nothing because it has no `append_arch`. If `defaults-cuda.sh` also declares `append_arch: -cuda`, then `--defaults release::gcc13::cuda` produces `slc7_x86-64-gcc13-cuda` — only the two files that opted in contribute, in chain order.

The value of `append_arch` is used **verbatim** and need not match the filename. No separator is added, so include it in the value (`-gcc13`; `_gcc13` or no separator also work). This lets you decouple the defaults filename from the suffix token:

```yaml
# defaults-gcc13-lto.sh
package: defaults-gcc13-lto
version: v1
append_arch: -gcc13-lto       # ← custom suffix, not derived from the filename
```

**Precedence:** when any defaults file in the chain uses `append_arch`, the `append_arch` mechanism takes full control — `qualify_arch` is ignored. This keeps the behaviour predictable when both fields appear in a mixed chain.

---

#### Comparison

| | `qualify_arch` | `append_arch` |
|---|---|---|
| Granularity | Global — one file enables it for the whole chain | Per-file — each file opts in independently |
| Suffix content | Every non-`release` default name | Only the explicit `append_arch` values |
| Suffix token | Default filename | Arbitrary string set by the author |
| Precedence | Fallback (used when no `append_arch` present) | Takes precedence when any file uses it |

---

#### Cleaning up

The `bits clean` command accepts an explicit `-a`/`--architecture` flag. To clean a qualified-arch tree, pass the combined string:

```
bits clean -a slc7_x86-64-gcc13
```


---

### Architecture-specific overlay

If a file named `defaults-<architecture>.sh` exists in the recipe repository (e.g. `defaults-osx_arm64.sh`), bits also loads it (and says so in the log) and merges its header on top of the already-merged profile, skipping the `package` key to avoid a name clash. This is the mechanism for per-platform tweaks such as disabling packages that do not build on a particular OS.


---

### macOS Homebrew system layer

macOS is a developer platform for bits — it does not build or publish CVMFS
tarballs there, so stable low-level system libraries and build tools are sourced
from **Homebrew** rather than built. A recipe opts in via its YAML header:

```yaml
homebrew_formula: readline          # one formula, or a list
homebrew_taps:                      # optional, rarely needed
  - some/tap
```

`bits brew` scans the recipes and writes a Brewfile (default
`<work-dir>/<arch>/Brewfile`, a local per-arch build artifact) listing every
declared formula that applies to the target architecture, plus `gnu-tar` (GNU tar
for reproducible package tarballs). `bits build` on macOS records the same file
from a recipe scan. On the **first** macOS build, when that file did not exist yet
and `--brew` is not given, bits writes it and stops (exit 2) with the
`brew bundle` command to run; the next build proceeds. Two ways to install them:

- **Build node (all up front):** `brew bundle --file sw/<arch>/Brewfile`.
- **Individual user (on demand):** `bits build --brew …`. With `--brew`, a
  recipe's `prefer_system_check` (which runs unsandboxed during dependency
  resolution and sees `BITS_BREW=1`) runs `brew install <formula>` only for a
  formula a package actually being built needs and that is missing. With `--brew`
  and an existing Brewfile, bits first runs `brew bundle` on it in one shot.

The build phase does not install either (with `--sandbox auto` it runs under
`sandbox-exec`, without network): `HomebrewRecipe` never installs — it only exposes an installed formula as a bits package by
symlinking its prefix into `$INSTALLROOT` (so `<PKG>_ROOT`, `PKG_CONFIG_PATH`
etc. resolve to the Homebrew tree). `bits doctor` runs `brew bundle check`
against a Brewfile kept next to the recipes (`macos/Brewfile` or `Brewfile`) on
macOS and reports missing formulae.

The Brewfile is a **derived** artifact (the recipes are the source of truth):
regenerate it whenever a recipe's `homebrew_formula` changes, and use
`bits brew --check` to detect a stale file before building.


---

### Merge semantics

When the `::` list contains more than one name (e.g. `--defaults release::alice`), bits merges their YAML headers left to right (a deep merge):

- Scalar values: later profile wins.
- Lists: concatenated.
- Dicts: recursively merged.

This lets a project-level profile (`alice`) layer on top of a base profile (`release`) without duplicating common settings. Bits also checks each component of the `::` list (except `release` and profiles marked `valid_defaults_exempt: true`) against the `valid_defaults` lists of the packages being built, and aborts with an error if one is not accepted.

---

### Forcing or Dropping the Revision Suffix (`force_revision`)

By default every installed package path and tarball filename includes a **revision counter** assigned by bits, e.g. `slc9_amd64/gcc/15.2.1-1`. The trailing `-1` is the revision. For some packages — notably CMS software releases where the version string `CMSSW_13_0_0` is the authoritative label used by downstream infrastructure — this suffix is undesirable. The `force_revision` field lets you pin the revision to a specific value or drop it entirely, **without touching the recipe file**.

`force_revision` is normally set in a `defaults-*.sh` file, so different groups can reuse the same recipes while opting in or out independently; a recipe may also set it directly.

#### Per-package override

```yaml
overrides:
  "cmssw_.*":
    force_revision: ""          # drop the revision suffix entirely
  "special-tool":
    force_revision: "rc1"       # pin to a literal string
```

When the regex matches a package name (case-insensitive), the package's revision is set to that value instead of a counter.

#### Global fallback

Add a top-level `force_revision:` field to apply to every package not matched by an override:

```yaml
# drops the revision suffix from every package in this defaults profile
force_revision: ""
```

A global value of `~` (YAML null) means "not set" and has no effect.

#### Hash revision policy

Set this top-level field in `defaults-*.sh` to label each package with its own
full build hash:

```yaml
revision_policy: "hash"
```

Once a package's build hash is known, bits uses it as the revision when no
`force_revision` is set.
Explicit recipe values, per-package overrides, and the global `force_revision`
fallback retain precedence, including `force_revision: ""`.
An untracked dependency must have an explicit `force_revision`; the hash policy
alone cannot supply its install label (the build stops otherwise). Development
packages keep counter (`localN`) revisions: they are built from a local checkout
under their local hash.
The hash includes tracked dependencies; this is the package build hash, not its
source commit hash. Install paths and tarball names use `<version>-<hash>` with
no `local` prefix, through the existing forced-revision mechanism. With a
read-only store, or none, bits checks for the remote hash first and reuses it
when available; if it must build the package locally, it uses the local hash as
the label and store path, and its dependents then hash in that local hash. With a
writable store (`--write-store`, `::rw`, also from the defaults'
`system: remote_store`) it keeps the remote hash. Uploads keep using the same
content-addressed store paths. `bits status` and `bits plan` make the same
choice; without `--check-store`, or for a store that cannot be listed, `bits
status` cannot see what the store holds and assumes a local build.
Omitting `revision_policy` retains the existing revision-counter behavior.

#### How the install path changes

| `force_revision` | Example install path |
|---|---|
| *(not set, default)* | `slc9_amd64/CMSSW_13_0_0/CMSSW_13_0_0-1` |
| `"1"` (pinned to 1) | `slc9_amd64/CMSSW_13_0_0/CMSSW_13_0_0-1` |
| `"rc1"` (literal) | `slc9_amd64/CMSSW_13_0_0/CMSSW_13_0_0-rc1` |
| `""` (empty, drop) | `slc9_amd64/CMSSW_13_0_0/CMSSW_13_0_0` |

The content-addressed store path (`TARS/<arch>/store/<h2>/<hash>/`) is unaffected — binary integrity is always preserved via the hash.

#### Risks and caveats

**Symlink overwrite risk (empty revision only).** When `force_revision: ""` is used, two different builds of the same version share the same install path. The convenience symlinks (`latest`, `latest-*`) will be silently overwritten by the later build. bits emits a `WARNING` when it detects `force_revision: ""` on a package.

**No `local` prefix protection.** Normally bits prefixes revision numbers with `local` (e.g. `local1`) when there is no writable remote store. When `force_revision` is set, this prefix logic is bypassed and the revision is used exactly as given — revision collision is possible if a literal integer is used in a mixed local/remote workflow.

**Shared across defaults profiles.** If you share a workspace between two groups using different defaults files — one with `force_revision: ""` and one without — the paths they install to will differ. Keep workspaces separate or agree on a common value.

---

## 19. Architecture-Independent (Shared) Packages

Some packages — calibration databases, reference data files, pure-Python libraries, architecture-neutral scripts — produce identical output regardless of the build platform. Rebuilding them on every architecture wastes time and storage. The `architecture: share` recipe field tells bits to install such packages into a single, platform-neutral directory tree that all architectures can read.

> **Renamed.** The sentinel was `shared` in earlier versions; it is now `share`
> (`architecture: share`, installed under `sw/share/…`). A recipe still declaring
> `architecture: shared` is no longer recognised as architecture-independent and
> builds as an ordinary per-architecture package — update it.

### Declaring a package as shared

Add the field to the YAML header of the recipe:

```yaml
package: my-calibration-db
version: "2024-01"
---
# Bash body that downloads or generates the data
curl -O https://example.com/calib-2024-01.tar.gz
tar -xzf calib-2024-01.tar.gz -C "$INSTALLROOT"
```

becomes

```yaml
package: my-calibration-db
version: "2024-01"
architecture: share
---
curl -O https://example.com/calib-2024-01.tar.gz
tar -xzf calib-2024-01.tar.gz -C "$INSTALLROOT"
```

No other change to the recipe or to the packages that depend on it is required.

### Install-tree layout

| Package type | Install path |
|---|---|
| Normal | `<work_dir>/<arch>/<pkg>/<version>-<revision>` |
| Shared, no family | `<work_dir>/share/<pkg>/<version>-<revision>` |
| Shared, with family | `<work_dir>/share/<family>/<pkg>/<version>-<revision>` |

The `share/` segment replaces the architecture string throughout: in the install tree, in tarball names (`<pkg>-<version>-<revision>.share.tar.gz`), and in the remote binary store (`TARS/share/store/…`).

### `$EFFECTIVE_ARCHITECTURE`

Every build script receives two architecture variables:

- `$ARCHITECTURE` — the real build-host architecture, always present, unchanged.
- `$EFFECTIVE_ARCHITECTURE` — `share` for shared packages, equal to `$ARCHITECTURE` otherwise.

Use `$EFFECTIVE_ARCHITECTURE` wherever a path should end up in the shared tree. The existing `$ARCHITECTURE` variable is still available for platform-specific logic such as selecting compiler flags.

```bash
# $INSTALLROOT already points into the share/ tree for a shared package
install -m 644 mydata.db "$INSTALLROOT/"
echo "Installing into the $EFFECTIVE_ARCHITECTURE tree (built on $ARCHITECTURE)"
```

### Environment initialisation (`init.sh`)

When a package depends on a shared package, bits generates the corresponding `init.sh` source line with a **literal** path prefix instead of the runtime variable `$BITS_ARCH_PREFIX`. The `share/` tree has the same name on every platform, so the literal path is correct for every consumer architecture, including in CVMFS deployments.

```bash
# Dependency on an arch-specific package — uses runtime variable:
[ -n "${MYLIB_REVISION}" ] || \
  . "$WORK_DIR/$BITS_ARCH_PREFIX"/mylib/1.0-1/etc/profile.d/init.sh

# Dependency on a shared package — uses literal path:
[ -n "${MY_CALIBRATION_DB_REVISION}" ] || \
  . "$WORK_DIR/share"/my-calibration-db/2024-01-1/etc/profile.d/init.sh
```

### Hashing and reproducibility

The build hash of a shared package is computed from the same inputs as any other package (recipe text, dependency hashes). Because `architecture` is not hashed directly (it enters only through the dependency tree), a shared package whose dependencies are all shared (apart from `defaults-release`) has the **same hash on every platform**. On macOS, relocation paths are also left out of a shared package's hash. This means:

- A shared package built on `slc7_x86-64` can be fetched and reused on `osx_x86-64` or `ubuntu2204_x86-64` without rebuilding.
- Once uploaded to the remote store, it is a single artifact shared by all build platforms.

### Warning: arch-specific dependencies

If a package marked `architecture: share` depends on a package that is *not* shared (other than `defaults-release`), bits emits a warning at build time:

```
WARNING: Package my-calibration-db declares 'architecture: share' but depends on
arch-specific package(s): mylib. Its hash may differ across platforms.
```

This is not an error — bits will still build the package — but the hash will vary across platforms (because the arch-specific dependency has a different hash on each platform), negating the cross-platform reuse benefit. In most cases the fix is either to remove the arch-specific dependency or to mark that dependency as shared too.

### Relocation

Shared packages go through the normal relocation step, but their relocation paths never enter the hash, so one tarball serves every platform. Keep them to data, scripts and pure-Python code: a package with compiled binaries that need per-platform path rewriting should not be marked `architecture: share`. When published to CVMFS, shared packages go to the layout's `cvmfs_shared_path_template` (default `{prefix}/noarch/{pkg}/{tag}`) instead of the per-architecture packages tree.

### Backward compatibility

The feature is entirely opt-in. A recipe without `architecture: share` behaves exactly as before — its effective architecture is the build-host architecture string and its install paths are unchanged.

---

## 20. Environment Variables

### Recipe build-time variables

bits sets these variables in each package's build script before the recipe body runs. Treat them as read-only.

#### Core build paths

| Variable | Purpose |
|----------|---------|
| `$INSTALLROOT` | Install all files here (the final installation prefix). Created by bits before the recipe runs. |
| `$BUILDDIR` | Temporary build directory inside `$BUILDROOT`. Created automatically. |
| `$SOURCEDIR` | Checked-out source directory (git) or the directory where archives are downloaded (`sources:`). |
| `$BUILDROOT` | Parent of `$BUILDDIR`; corresponds to `BUILD/<pkghash>/` in the work tree. |
| `$PKGPATH` | Relative path from the work directory to the install root: `<arch>[/<family>]/<pkg>/<version>-<revision>`. |
| `$WORK_DIR` | Absolute path of the work directory (`sw/` by default). |
| `$PKGDIR` | Directory containing the package's recipe file (its `patches/` live here). |
| `$BITS_CONFIG_DIR` | Absolute path of the recipe directory (`-c`/`--config-dir`). |

#### Package identity

| Variable | Purpose |
|----------|---------|
| `$PKGNAME` | Package name as declared in the recipe. |
| `$PKGVERSION` | Package version string. |
| `$PKGREVISION` | Build revision: `1`, `2`, … (or `local1`, `local2`, … when there is no write store); empty when `force_revision` is `""`. |
| `$PKGHASH` | Unique content-addressable build hash (hex string). |
| `$PKGFAMILY` | Install family (empty string if no family is assigned). |
| `$BUILD_FAMILY` | Full `build_family` string, which may include the defaults combination used. |
| `$ARCHITECTURE` | Real build-host architecture string (e.g. `ubuntu2204_x86-64`). |
| `$EFFECTIVE_ARCHITECTURE` | `share` for shared packages; the build-type-neutral arch for `own_hash` packages; equal to `$ARCHITECTURE` otherwise. |
| `$JOBS` | Parallel compilation jobs. Pass to `make -j$JOBS`, `cmake --build --parallel $JOBS`, etc. Already divided across `--parallel` and reduced by `mem_per_job` when memory is tight (see [Memory- and load-aware parallelism](#memory-aware-parallelism)). |
| `$COMMIT_HASH` | Commit checked out for the `source:` field, shortened to 10 characters (the ref as written if it could not be resolved to a commit). |
| `$BITS_SCRIPT_DIR` | Absolute path to the bits installation directory. |
| `$INCREMENTAL_BUILD_HASH` | Non-zero when an incremental recipe is in use (development mode). |
| `$DEVEL_PREFIX` | Non-empty for development packages (directory name of the devel source tree). |
| `$SOURCE_DATE_EPOCH` | The build's start time (seconds since the epoch), unless set already. Tools that follow [reproducible-builds.org](https://reproducible-builds.org/specs/source-date-epoch/) use it; Python's byte-compiling (pip, `compileall`, its own install) then writes hash-based `.pyc` files, which stay valid in a package unpacked from its tarball (zero file times). |

#### Source archives (`sources:` field)

When the recipe uses the `sources:` field, bits downloads each archive to `$SOURCEDIR` before the recipe runs:

| Variable | Purpose |
|----------|---------|
| `$SOURCE0` | Filename (basename) of the first archive. |
| `$SOURCE1` | Filename of the second archive (if present). |
| `$SOURCEn` | Filename of the *n*-th archive (zero-indexed). |
| `$SOURCE_COUNT` | Total number of source archives (`0` if no `sources:` field). |

```bash
tar -xzf "$SOURCEDIR/$SOURCE0" -C "$BUILDDIR"
[ "$SOURCE_COUNT" -gt 1 ] && tar -xzf "$SOURCEDIR/$SOURCE1" -C "$BUILDDIR/data"
```

#### Patch files (`patches:` field)

| Variable | Purpose |
|----------|---------|
| `$PATCH0` | Filename (basename) of the first patch file. |
| `$PATCHn` | Filename of the *n*-th patch file (zero-indexed). |
| `$PATCH_COUNT` | Total number of patch files (`0` if no `patches:` field). |

```bash
# bits applies patches itself unless auto_patch is false (or --no-auto-patch);
# only then does the recipe apply them, with the built-in helper:
bits_apply_patches        # patch -p1 in declaration order; bits_apply_patches 0 for -p0
```

#### Dependency variables

| Variable | Purpose |
|----------|---------|
| `$REQUIRES` | Space-separated runtime + build-time dependencies. |
| `$BUILD_REQUIRES` | Space-separated build-time-only dependencies. |
| `$RUNTIME_REQUIRES` | Space-separated runtime-only dependencies. |
| `$FULL_REQUIRES` | Full transitive closure of `requires`. |
| `$FULL_BUILD_REQUIRES` | Full transitive closure of `build_requires`. |
| `$FULL_RUNTIME_REQUIRES` | Full transitive closure of `runtime_requires`. |

For each built dependency `DEP`, bits also sets `${DEP_ROOT}` to its absolute install path (e.g. `$ZLIB_ROOT/include/zlib.h`).

| Variable | Purpose |
|----------|---------|
| `$BITS_PROVIDERS` | URL of the active repository-provider set, inherited from the environment (see below). |

### Build and configuration variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `BITS_BRANDING` | _(empty)_ | Cosmetic program-name branding; set by the `aliBuild` wrapper. |
| `BITS_COMMUNITY` | _(empty)_ | Community whose registry entry (`<community>.bits.sh`) the bootstrap clones when `-c` names a missing recipe directory. Empty by default; the `aliBuild` wrapper sets `ALICE`. (`bits init --community` does not persist it; set the variable.) `BITS_ORGANISATION`, its former name, still works. |
| `BITS_PKG_PREFIX` | _(empty)_ | Display prefix for `bits q`. Empty prints native `PKG/VERSION`; when set (e.g. `VO_ALICE` via `aliBuild`) output becomes `PREFIX@PKG::VERSION`. |
| `BITS_REPO_DIR` | `.` (`alidist` under the `aliBuild` wrapper) | Recipe directory (`-c`/`--config-dir` default): normally the community repository you run bits in. |
| `BITS_WORK_DIR` | `sw` | Output and work directory (`ALICE_WORK_DIR` is accepted as a fallback). |
| `BITS_PATH` | _(empty)_ | Comma-separated list of additional recipe search directories. Absolute paths are used directly; a relative name resolves to `<recipe dir>/<name>.bits`. `--search-path` seeds it; an explicit `BITS_PATH` wins. |
| `BITS_CHDIR` | _(unset)_ | Directory to change to before building (same as `-C`). |
| `BITS_TAR_COMPRESSOR` | `gzip -n` | Compressor for package tarballs. Must be deterministic (e.g. `pigz -n -p4` on a farm with a uniform pigz, never plain `pigz`). |
| `BITS_DOCKER_REGISTRY` | `gitlab-registry.cern.ch/bits/containers` | Registry of the default `--docker` builder image (wins over `docker_registry:` in `defaults-release`). |
| `BITS_DOCKER_TAG` | `latest` | Tag of the default builder image. |
| `BITS_LEGACY_REGISTRY` | _(unset)_ | `1` selects the legacy `alisw/<distro>-builder` images (the `aliBuild` wrapper sets it). |
| `BITS_LEGACY_INITDOTSH` | _(unset)_ | `1` selects the legacy build-time `init.sh` (same as `--legacy-initdotsh`); the `aliBuild` wrapper sets it. |
| `BITS_PROVIDERS` | `https://github.com/bitsorg/bits-providers` (empty under the `aliBuild` wrapper) | URL of the repository-provider set; an `@<tag>` suffix pins a snapshot. Environment only (no build flag). |
| `BITS_CVMFS_PREFIX` | _(unset)_ | A community's CVMFS prefix (e.g. `/cvmfs/bits.cern.ch/key4hep`): `bits q`, `enter`, `load`, `printenv`, `unload` and `setenv` also use its modules for the architecture, after the local ones (see [bits query](#bits-query--list--avail)). The bits entry point on CVMFS sets it for a community. Unset, a recipe repository's `cvmfs.yaml` gives the trees; empty turns them off. |
| `BITS_CATALOG_LISTING` | _(unset)_ | `1`: list module trees on CVMFS from their serving catalog (the `bitsModules` helper) before walking the directory (see [bits query](#bits-query--list--avail)). |
| `BITS_REUSE_FROM` | _(unset)_ | Default of `--reuse-from` for `bits build` (e.g. `cvmfs`); the bits entry point on CVMFS sets it for a community. Any `--reuse-from`, from the command line or a `bits use` profile, wins, and an empty one turns reuse off. When the recipes declare no CVMFS layout, or the modules tree it names does not exist, the default is skipped with a warning instead of stopping the build; an explicit `--reuse-policy` wins over its `::relaxed`/`::strict` suffix. |
| `REMOTE_STORE`, `WRITE_STORE` | _(unset)_ | Read and write store URLs when no flag is given; `BITS_REMOTE_STORE`/`BITS_WRITE_STORE` override them (see [§21](#21-remote-binary-store-backends)). |
| `BITS_S3_STORE` | `https://s3.cern.ch/lcgapp-bits-testing` | Default store for `bits publish`, `certify`, `sign`, `bits store` and `compliance`. |
| `BITS_AWS_KEYS_FILE` | `~/.bits/s3keys` | Private file with S3 credentials, used when they are not in the environment. |
| `BITS_STRICT_STORE_INTEGRITY` | _(unset)_ | `1` makes a recalled tarball with no integrity-ledger entry fatal (with `--store-integrity`). |

### Environment module variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MODULES_SHELL` | _(auto-detected)_ | Shell type passed to `modulecmd` and used when spawning a new sub-shell via `bits enter`. Auto-detected from the parent process. Accepted values: `bash`, `zsh`, `ksh`, `csh`, `tcsh`, `sh`. |
| `MODULEPATH` | _(set by bits)_ | Colon-separated list of directories searched by `modulecmd` for modulefiles. Bits prepends `<WORK_DIR>/MODULES/<ARCH>` (plus any reused-release module trees) and keeps existing entries. |
| `BITSLVL` | `0` | Nesting counter, incremented by every `bits` invocation. `bits enter` refuses to run inside an environment it already opened (`BITSLVL` already set to 1 or more). |
| `BITS_ENV` | _(optional)_ | Absolute path to the `bits` executable, used by `shell-helper` to locate bits without relying on `$PATH`. If unset, `shell-helper` resolves `bits` via `type -p bits`. |
| `BITSBUILD_CHDIR` | _(unset)_ | If set, `<value>/sw` is added to the list of default work directories tried when `--work-dir` is not specified. |

### `modulecmd` discovery

The `bits` script locates `modulecmd` by trying three paths in order:

1. `modulecmd` on `$PATH` — Environment Modules v3.
2. `$(dirname $(which envml))/../libexec/modulecmd-compat` — Environment Modules v4+.
3. `$(brew --prefix modules)/libexec/modulecmd-compat` — Homebrew on macOS.

If none is executable, bits prints an install hint and exits with an error.

---

## 21. Remote Binary Store Backends

A **remote binary store** is an external storage location where bits uploads completed build tarballs and from which future builds can download them, skipping recompilation entirely. The mechanism is content-addressable: every tarball is keyed on a hash that captures the recipe, source commit, dependency hashes, and build environment. If the hash already exists in the store, bits fetches the tarball instead of building.

### CLI options

| Option | Description |
|--------|-------------|
| `--remote-store URL` | Fetch pre-built tarballs from this store before deciding whether to build. |
| `--write-store URL` | Upload each newly-built tarball to this store after a successful build. May be the same URL as `--remote-store`. |
| `--remote-store URL::rw` | Shorthand: sets both `--remote-store` and `--write-store` to `URL` in a single flag. Cannot be combined with `--write-store`. |
| `--no-remote-store` | Disable the remote store even on architectures where one is enabled by default (a `--write-store` is still read from). |
| `--insecure` | Skip TLS certificate verification for `https://` stores. |
| `--trust-manifest SRC`, `--no-require-signed-reuse` | Signed reuse is on by default: a remote tarball is reused only if a verified signed manifest lists it. `--trust-manifest` names the manifest (otherwise derived from an `http(s)`/`s3`/`b3` store); `--no-require-signed-reuse` disables the check (insecure, warns). See [Signing and verifying the archive tier](#signing-and-verifying-the-archive-tier). |

A `--write-store` given on its own is also used as the read store, unless the architecture has a default read store (`https://s3.cern.ch/swift/v1/alibuild-repo` on slc7/slc8/slc9 and ubuntu2004/2204/2404 x86-64 and on slc9_aarch64, unless `--always-prefer-system` is given): that one stays the read store, so use `--remote-store URL::rw` to read from and write to one store. Unlike aliBuild, configuring a store does **not** force `--no-system` (reuse is content-hash addressed); pass `--no-system` for a self-contained build. Store URLs may also come from the environment (`REMOTE_STORE`/`WRITE_STORE`, overridden by `BITS_`-prefixed names; see [S3 store: common CI/CD config](#s3-store-common-cicd-config-with-per-runner-overrides)) or from `remote_store:` under the defaults `system:` block. CERN S3 path-style URLs (`https://s3.cern.ch/<bucket>`) are accepted and rewritten to the listable form.

### Supported backends

| URL scheme | Backend | Read | Write | Authentication |
|------------|---------|:----:|:-----:|----------------|
| `http://` or `https://` | HTTP/HTTPS | ✓ | — | None (public) or TLS; use `--insecure` to skip cert check |
| `s3://BUCKET/PATH` | CERN S3 via `s3cmd` | ✓ | ✓ | `~/.s3cfg` config file |
| `b3://BUCKET/PATH` | Any S3-compatible service via `boto3` | ✓ | ✓ | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` (environment, `--s3-*` flags or `~/.bits/s3keys`) |
| `HOST:/PATH` (or `ssh://HOST:/PATH`), `rsync://HOST/MODULE`, `/local/path` | rsync | ✓ | ✓ | SSH keys, rsync-daemon access, or filesystem permissions |

> `cvmfs://` is **not** a `--remote-store` backend. `--remote-store` is the
> tarball store; to reuse components already deployed on CVMFS use
> [`--reuse-from`](#relaxed-cvmfs-reuse). A `cvmfs://` `--remote-store` is
> rejected with an error pointing at `--reuse-from`.

#### Mixing a read-only remote with a separate write store

A read-only `--remote-store` (`http(s)://`) can be paired with a writable `--write-store` of a different backend, e.g. recall pre-built packages from an HTTP mirror and upload newly-built ones to S3:

```bash
bits build ... --remote-store https://mirror.example/bits/ --write-store b3://mybucket
```

Reads (recall) go to the remote store; uploads go to the write store. **Only freshly-built packages are uploaded** — packages recalled from the read-only store keep their original provenance and are not re-published.

#### HTTP / HTTPS

The HTTP backend is the simplest and most portable. It is read-only: bits fetches tarballs with automatic exponential-backoff retries (up to four attempts) but cannot upload. Use it for public artifact mirrors or CI read caches:

```bash
bits build --remote-store https://artifacts.example.com/bits ROOT
```

Pair it with a writable `--write-store` (rsync, `s3://` or `b3://`) if needed.

#### S3 via `s3cmd` (`s3://`)

Uses the [`s3cmd`](https://s3tools.org/s3cmd) command-line tool. Credentials are read from `~/.s3cfg`. bits pins the host to CERN S3 (`s3.cern.ch`), so for AWS, MinIO, Ceph or any other endpoint use `b3://` instead.

```bash
bits build --remote-store s3://mybucket/bits-cache \
           --write-store  s3://mybucket/bits-cache ROOT
```

#### S3-compatible via `boto3` (`b3://`)

The preferred S3 backend. Uses the `boto3` Python library over one reused connection and works with any S3-compatible endpoint (CERN S3 by default; set `--s3-endpoint` otherwise). Credentials come from environment variables:

```bash
export AWS_ACCESS_KEY_ID=your-key-id
export AWS_SECRET_ACCESS_KEY=your-secret-key

bits build --remote-store b3://mybucket/bits-cache \
           --write-store  b3://mybucket/bits-cache ROOT
# Equivalent shorthand:
bits build --remote-store b3://mybucket/bits-cache::rw ROOT
```

To keep the keys out of the environment, put them in a private file (default
`~/.bits/s3keys`, mode 600; override the path with `$BITS_AWS_KEYS_FILE`) instead:

```ini
# ~/.bits/s3keys   (chmod 600)
AWS_ACCESS_KEY_ID=your-key-id
AWS_SECRET_ACCESS_KEY=your-secret-key
# optional: S3_ENDPOINT_URL=https://s3.cern.ch, AWS_DEFAULT_REGION=...
```

`export`-prefixed, quoted, and `aws_access_key_id = …` (AWS credentials INI)
forms are all accepted. Precedence is: `--s3-*` flags > environment (CI) > this
file > built-in default — so CI-injected credentials are never overridden by the
file.

The `b3://` store holds only the content-addressed tarballs, plus a small revision marker per build under `MANIFESTS/rev-index/`; no package or dist symlink objects are written. A tarball already present in the store is kept, never overwritten, and its stored sha256 is what the build manifest records.

#### Publishing an existing local build to S3 (`bits publish`)

Building with `--write-store` uploads each package **as it is built**. To push a
store you already built (nothing re-uploads on a cached rebuild), use
`bits publish` — it reads the build manifest and uploads each package's content
tarball.

```bash
# credentials from the environment or ~/.bits/s3keys (see above)

# Bulk: upload every package in the latest manifest. This is the default when
# no PACKAGE is given, so bare `bits publish` is the whole-stack push:
bits publish
bits publish --remote-store https://s3.cern.ch/lcgapp-bits-testing  # pick the bucket
bits publish --from-manifest /path/to/bits-manifest-XYZ.json  # a specific manifest

# Single package to the S3 store (from its manifest entry):
bits store upload ROOT --remote-store b3://lcgapp-bits-testing

# Preview without uploading (no credentials/network needed):
bits publish --dry-run
```

Modes: bare `bits publish` (or `--from-manifest`) bulk-uploads a build manifest to the
S3 store — the community push shown above. `bits publish PACKAGE --cvmfs-target … --prepub-url …`
publishes one package to CVMFS. The single-package S3-store write is now `bits store upload`
(it replaces the removed `bits publish --to s3`).

`--dry-run` (`-n`) lists exactly what would be uploaded and to which store,
without contacting S3 — handy to check the package set and target before pushing.

`--remote-store` accepts an `https://<host>/<bucket>` URL (from which the boto3
endpoint and path-style addressing are derived), or `b3://<bucket>` / `s3://<bucket>`.
It is the canonical store flag across `publish`, `certify`, `sign`, `prune --retain`,
`bits store` (`gc`/`stats`/`upload`) and `compliance`; the old `--store` spelling still works but is deprecated.
The default is `$BITS_S3_STORE` if set, else `https://s3.cern.ch/lcgapp-bits-testing`.
A bulk publish also uploads a trimmed copy of the manifest (one per architecture)
under `MANIFESTS/`, but only if every package uploaded; `bits certify` and
`bits sign` turn it into the signed manifest that consumers trust. Packages whose
`redistributable:` forbids binary redistribution are skipped.

Uploading needs only valid S3 keys, but reuse is **signed by default**: other
builds reuse an uploaded tarball only once a signature-verified manifest lists
it, so until the build is certified and signed they rebuild those packages.
`--no-require-signed-reuse` turns the check off (insecure; bits warns). See
[Artifact resolution order](#artifact-resolution-order-trust-tiered-reuse).

#### Signing a manifest on a GitLab runner (no private server)

A GitLab CI runner **dials out** to fetch jobs, so signing needs no
inbound-reachable service. The manifests project's CI runs `bits sign` after
`bits certify` opens the merge request. Either it signs through the signing
service (`--sign-via-service`, no key stored in CI), or it uses `--key` with the
Ed25519 **private** key in a Protected + Masked CI/CD variable and the job limited
to **protected** refs, so fork/MR pipelines never see it. Consumers verify with
the **public** keys in `bits/keys/`; `keys/key-policy.json` says which key may sign
for which group. See [Signing — `bits sign`](#signing--bits-sign).

#### rsync / local filesystem

Supports remote hosts (over SSH or an rsync daemon) and local paths. Useful for shared NFS or a build server reachable over SSH. bits cannot find a signed manifest in such a store, so pass `--trust-manifest`, or `--no-require-signed-reuse` for a store you control; otherwise nothing is reused from it:

```bash
# Remote via SSH
bits build --remote-store buildserver.example.com:/srv/bits-cache::rw ROOT

# Local filesystem path (useful for cross-project caching on the same machine)
bits build --remote-store /shared/bits-cache \
           --write-store  /shared/bits-cache ROOT
```

### Content-addressable tarball layout

Every tarball is named and stored by its build hash. The local layout (in the `TARS/` work directory) is below. A `b3://` store holds only the `store/` part; `rsync` and `s3://` stores also receive the package symlinks and dist trees:

```
TARS/
└── <architecture>/
    ├── store/
    │   └── <hash[0:2]>/          ← two-character prefix for directory sharding
    │       └── <hash>/
    │           └── <pkg>-<version>-<revision>.<architecture>.tar.gz
    ├── <package>/                 ← convenience symlinks by package name
    │   └── <pkg>-<version>-<revision>.<architecture>.tar.gz -> ../../<architecture>/store/…
    └── dist/ dist-direct/ dist-runtime/   ← dependency-set symlink trees (see below)
```

For packages marked `architecture: share` (see [§19](#19-architecture-independent-shared-packages)) the architecture segment is replaced with `share`:

```
TARS/share/store/<hash[0:2]>/<hash>/<pkg>-<version>-<revision>.share.tar.gz
```

`own_hash` packages use their build-type-neutral architecture here (see [Shared toolchains](#shared-toolchains-own_hash)).

The hash is a 40-character SHA-1 of the recipe text (comments stripped), package name, version and family, the source commit, `sources:` and patches, `env`/path settings, hooks, and the hashes of its dependencies (which cover theirs in turn); on macOS also the relocation paths. Changing anything in this set produces a different hash and therefore a different cache entry.

### Dependency-set symlink trees

After each successful build, bits creates three symlink trees under `TARS/<arch>/` that group together everything needed to reproduce or run the package:

| Directory | Contents |
|-----------|----------|
| `dist/<pkg>/<pkg>-<ver>-<rev>/` | Full transitive closure — all build and runtime dependencies. |
| `dist-direct/<pkg>/<pkg>-<ver>-<rev>/` | Direct dependencies only (`requires` + `build_requires`). |
| `dist-runtime/<pkg>/<pkg>-<ver>-<rev>/` | Runtime transitive closure (`runtime_requires`). |

Each entry in these trees is a symlink to the corresponding tarball in `store/`. The `rsync` and `s3://` backends also upload the trees; the `b3://` backend does not, and bits rebuilds them locally from the dependency graph.

### Build lifecycle with a store

```
bits build --remote-store URL --write-store URL PACKAGE
```

For each package in topological order:

1. **Hash** — Compute the content-addressable hash from recipe, source commit, and dependency hashes.
2. **Fetch** — Ask the remote store for `TARS/<arch>/store/<h2>/<hash>/*.tar.gz`. If found, download it. With signed reuse (the default) it is kept only if a verified signed manifest lists that hash with a matching sha256; otherwise it is discarded and the package is built.
3. **Unpack or build** — If a cached tarball was downloaded, unpack it into `$INSTALLROOT` and skip compilation. Otherwise run the full Bash build script.
4. **Pack** — After a successful from-source build (and any `POST_INSTALL` hooks), bits makes the package's pkg-config, CMake and `bin/*-config` files use relative paths, records which files need relocating and the file list for merged views, then packs `$INSTALLROOT` into `TARS/<arch>/store/<h2>/<hash>/<pkg>-<ver>-<rev>.<arch>.tar.gz`. Packing is **deterministic** (sorted members, owner/group 0, fixed mtime, pinned compressor `gzip -n` or `$BITS_TAR_COMPRESSOR`) so that two nodes building the same hash produce the same bytes. A package unpacked from it locally keeps the archive's file times (zero); `bits cvmfs publish` stamps the build time on the published files. This needs **GNU tar** (`gtar` or a GNU `tar`); without it (e.g. macOS without `brew install gnu-tar`) bits warns and the tarball may not be byte-reproducible.
5. **Upload** — Bits uploads the tarball to the write store (the `rsync` and `s3://` backends also upload the package symlink and dist trees). Packages recalled from a store, `local` revisions, and packages whose `redistributable:` forbids binary redistribution are not uploaded.

### Revision numbering

Revisions count builds of the same package version: a build whose hash matches an existing revision reuses it, and a new hash gets the next free integer (`1`, `2`, …). Builds without a write store, and development packages (`bits init`), get `local` revisions (`local1`, `local2`, …), which are never uploaded so in-progress work cannot reach the shared cache.

### CI/CD patterns

#### Read-only cache for developers, read-write for CI

```bash
# CI job: build and publish
export AWS_ACCESS_KEY_ID=ci-key
export AWS_SECRET_ACCESS_KEY=ci-secret
bits build --remote-store b3://mybucket/bits-cache::rw MyStack

# Developer workstation: fetch from CI cache, never upload
bits build --remote-store b3://mybucket/bits-cache MyStack
```

Developers reuse the CI tarballs once that build is certified and signed (see
[Signing — `bits sign`](#signing--bits-sign)); for a private bucket you trust,
add `--no-require-signed-reuse` instead.

#### Layered stores: fast read from HTTP, write to S3

```bash
bits build --remote-store https://public-mirror.example.com/bits \
           --write-store  b3://private-bucket/bits MyStack
```

Bits tries to download from the HTTP mirror first; if a tarball is missing it builds from source and uploads to the private S3 bucket. A periodic sync job can mirror the S3 bucket to the HTTP server.

#### Local filesystem cache for team NFS

```bash
bits build --remote-store /nfs/shared/bits-cache::rw MyStack
```

All team members building on machines with access to the shared NFS path reuse each other's artifacts, once reuse is trusted with `--no-require-signed-reuse` or a `--trust-manifest` (see [rsync / local filesystem](#rsync--local-filesystem)).

### Source archive caching

Packages that use the `sources:` key in their recipe (downloadable URL tarballs, distinct from the primary `source:` git repository) are also archived in the remote store, besides being cached locally. This means bits can rebuild a package even if the upstream server has removed or moved the tarball.

#### How it works

When bits encounters a `sources:` entry it proceeds in three steps:

1. **Local cache hit** — if `SOURCES/cache/<h2>/<hash>/<filename>` already exists on disk, it is used immediately and the remote store is not contacted at all.
2. **Remote store hit** — if the local cache is empty, bits asks the configured backend for the archived copy before contacting the upstream URL. On success the file is placed in the local cache and no upload is required (it is already in the store).
3. **Upstream download + archive** — only when both the local cache and the remote store miss does bits download from the original URL. The freshly downloaded file is then uploaded to the write store so that future builds (and other machines) can benefit from step 2, unless the recipe's `redistributable:` forbids redistributing sources.

#### Remote namespace

Source archives occupy a dedicated namespace inside the same store used for build tarballs:

```
SOURCES/cache/<hash[0:2]>/<hash>/<filename>
```

This mirrors the local `SOURCES/cache/` layout exactly, so the remote path follows from the MD5 of the source URL (`hash`) and the bare filename. For example:

```
SOURCES/cache/a1/a1b2c3d4.../libfoo-1.2.tar.gz
```

#### Backend support matrix

| Store | Fetch sources | Upload sources | Notes |
|-------|:-------------:|:--------------:|-------|
| none | — | — | Local cache only. |
| `http(s)://` | ✓ | — | Read-only; add a `--write-store` to archive sources. |
| rsync / local path | ✓ | ✓ | Upload needs a write store; existing files are kept. |
| `s3://` (s3cmd) | ✓ | ✓ | Upload needs a write store; existing objects are kept. |
| `b3://` (boto3) | ✓ | ✓ | Upload needs a write store; existing objects are kept. |

#### Enabling source archive caching

No extra flags are needed. Source caching is activated automatically whenever a remote store is configured:

```bash
# Build ROOT; source tarballs fetched via sources: are archived to S3.
bits build --remote-store b3://mybucket/bits-cache::rw ROOT
```

Without a write store (an `http(s)://` store alone is read-only), bits still fetches source archives from the store but does not upload them — the same as for build tarballs.

### Store integrity verification

Remote store backends — S3 buckets, rsync servers, HTTP mirrors — are operated by infrastructure that bits does not control.  An operator with write access to the backend, or an attacker who has compromised it, could silently replace a legitimate build tarball with a trojanised one.  Because bits unpacks and executes tarball content directly, such a replacement would result in arbitrary code execution on every machine that subsequently fetches the affected package.

The **store integrity ledger** is an opt-in defence against this class of attack.  It is disabled by default to preserve backward compatibility with existing work directories.

#### How it works

After each successful upload to the write store, bits computes the SHA-256 digest of the local tarball and writes it to a file in `$WORK_DIR/STORE_CHECKSUMS/`, mirroring the remote store path:

```
$WORK_DIR/
  STORE_CHECKSUMS/
    TARS/
      <architecture>/
        store/
          <hash[0:2]>/
            <hash>/
              <pkg>-<ver>-<rev>.<arch>.tar.gz.sha256
```

`STORE_CHECKSUMS/` is a **local-only subtree** — it is never uploaded to the remote store and therefore cannot be forged through the same channel it protects against.

The next time the tarball is recalled from the store, bits recomputes the SHA-256 and compares it against the ledger.  Three outcomes are possible:

| Outcome | Effect |
|---------|--------|
| **Match** | The file is intact; the build continues normally. |
| **No ledger entry** | The tarball predates the feature, or the work directory was rebuilt. A warning is emitted and the digest is recorded for future verification. Build continues. |
| **Mismatch** | Always fatal: bits prints the expected and actual digests, explains how to investigate, and aborts. |

A missing ledger entry can be made fatal too — useful for CI pipelines that have adopted the feature from day one — by setting the environment variable `BITS_STRICT_STORE_INTEGRITY=1`.

#### Enabling store integrity verification

Per-invocation:

```bash
bits build --store-integrity --remote-store b3://mybucket/bits-cache::rw ROOT
```

Persistent opt-in for the current directory (recommended for teams that have adopted the feature):

```bash
bits use build --store-integrity
```

This saves `--store-integrity` to the profile's `[build]` section so every `bits build` in this directory verifies recalled tarballs.

#### Strict mode for CI (no unverified tarballs)

```bash
export BITS_STRICT_STORE_INTEGRITY=1
bits build --store-integrity --remote-store b3://mybucket/bits-cache ROOT
```

In strict mode a tarball that has no ledger entry — rather than a mismatched entry — is also treated as a fatal error.  Use this when you want to guarantee that every recalled tarball was recorded by *this* instance (not an older one that predates the feature).

#### Investigating a mismatch

When bits reports an integrity failure the output includes:

- The **expected** SHA-256 from the local ledger (what was recorded at upload time).
- The **actual** SHA-256 of the recalled file (what arrived from the remote store).
- The local tarball path and the ledger file path.

Steps to investigate:

1. Delete the local tarball so bits will re-fetch it:
   ```bash
   rm -rf $WORK_DIR/TARS/<arch>/store/<h2>/<hash>/
   ```
2. Fetch the tarball from a second, independent source (e.g. a different mirror or the original CI artefact) and compute its SHA-256 manually:
   ```bash
   sha256sum <pkg>-<ver>-<rev>.<arch>.tar.gz
   ```
3. Compare with the ledger entry:
   ```bash
   cat $WORK_DIR/STORE_CHECKSUMS/TARS/<arch>/store/<h2>/<hash>/<tarball>.sha256
   ```
4. If the independent source matches the ledger but the store does not, the store has been compromised.  Rotate credentials, audit access logs, and rebuild from source.
5. If you have confirmed the mismatch is benign (e.g. a legitimate force-push to the store), reset the ledger entry:
   ```bash
   rm $WORK_DIR/STORE_CHECKSUMS/TARS/<arch>/store/<h2>/<hash>/<tarball>.sha256
   ```
   The next build run will re-record the current digest and warn instead of aborting.

<a id="relaxed-cvmfs-reuse"></a>
### Reusing deployed components (`--reuse-from`)

To build on top of a release already deployed on CVMFS, point `--reuse-from` at
its published **modules tree** (or the literal `cvmfs`, which resolves the
location from the defaults `system:` layout / `cvmfs_modules_template`). Each
reused component is set up from its deployed modulefile / `init.sh` — sourced in
place from `/cvmfs`, not copied — so only the top of the stack is built and
everything below it is consumed from the deployment. `--reuse-from` is distinct
from `--remote-store`, which remains the tarball store.

```bash
bits build --reuse-from cvmfs::relaxed \
           --docker --docker-image <img> \
           --architecture x86_64-el9-gcc14-opt \
           --defaults lcg::release::gcc14::opt \
           --build-local xrootd  xrootd
```

Reused components are logged as **Reuse: … (not built)**; only the requested top
package is **Compiling**. Use `--build-local PKG[,PKG…]` to force specific
packages to build locally anyway (e.g. one you need patched).

**Policy.** `--reuse-policy strict` (default) reuses a component only on an exact
content-hash match, so the build stays reproducible and publishable.
`--reuse-policy relaxed` reuses any version present in the one-release overlay
(matched via its `build_id`) — faster for local iteration, but **loose
provenance**: the result is not reproducible from hash alone, so the **publish
path refuses it** (`--reuse-policy relaxed` with `--write-store`
is rejected). The `<src>::relaxed`/`::strict` suffix on `--reuse-from` sets the
policy inline; an explicit `--reuse-policy` must agree with it.

> The builder image must match the reused release's ABI (OS + compiler): reused
> binaries carry the toolchain they were built with, so run them in an image that
> provides a compatible runtime.

> **Note.** Earlier versions reused deployed packages through a `cvmfs://`
> `--remote-store` and a `--reuse-base <build_id>` graft; that path has been
> removed in favour of `--reuse-from`. A `cvmfs://` `--remote-store` now errors
> and points here.

---

## 22. Docker Support

When `--docker` is specified, bits wraps the build in a `docker run` invocation. This is useful for building against an older Linux ABI from a newer host, or for reproducible CI.

```bash
# Use the default image for the target architecture
bits build --docker --architecture ubuntu2004_x86-64 ROOT

# Specify an image explicitly
bits build --docker --docker-image alisw/slc9-builder:latest ROOT

# Pass extra options to docker run
bits build --docker --docker-extra-args "--memory=8g --cpus=4" ROOT
```

Without `--docker-image`, the image is derived from the architecture as `<registry>/<machine>-<distro>[-cuda]:<tag>`, for example `gitlab-registry.cern.ch/bits/containers/x86_64-slc9:latest` for `slc9_x86-64`. Only the OS, the CPU and a `cuda` part of the architecture select the image; compiler and build-type parts do not. The registry is `$BITS_DOCKER_REGISTRY`, else `docker_registry:` in `defaults-release` (top level or under `system:`), else `gitlab-registry.cern.ch/bits/containers`. The tag is `$BITS_DOCKER_TAG`, else `latest`. With `BITS_LEGACY_REGISTRY=1` (set by default by the `aliBuild` wrapper), or when the architecture cannot be split into OS and CPU, the legacy `registry.cern.ch/alisw/<distro>-builder` image is used instead. Images are pulled when missing.

Bits mounts into the container the work directory (read-write, except `SOURCES/`, which is read-only so a recipe cannot modify shared sources in place; `BITS_READONLY_SOURCES=0` turns this off), the recipe directory (read-only), the bits installation, and any `-v` volumes. `~/.ssh` is **not** mounted. When components deployed on CVMFS are reused (`--reuse-from`), `/cvmfs` is also mounted read-only.

The build runs under your own user ID, with `HOME=/tmp` and `SHELL=/bin/bash`. Bits adds `--network=host` to `docker run` and, unless you pass them yourself in `--docker-extra-args`, `--cpuset-cpus=<all online host CPUs>` and a memory cap of host memory minus max(4 GiB, 10 %) (`BITS_DOCKER_MEMORY=<size>` sets the cap, `BITS_DOCKER_MEMORY=off` removes it).

**Rootless podman.** When `docker` is podman run by a non-root user (for example via `podman-docker`), bits runs the container with `--userns=keep-id` instead of `--user`, disables SELinux labelling (`--security-opt=label=disable`) unless you pass a `--security-opt` yourself, and drops any CPU or memory limit whose cgroup controller is not delegated to your user, with a warning explaining how to delegate it. `bits doctor` checks this setup.

### workDir mount point inside the container

By default the workDir is bind-mounted at `/container/bits/sw` inside the container, so that the container-internal paths do not collide with the host paths. Two flags change this behaviour:

| Flag | Effect |
|------|--------|
| `--container-use-workdir` | Mount the workDir at the same path as on the host (i.e. `container_workDir = workDir`). Useful when the host and container share the same filesystem. |
| `--cvmfs-prefix PATH` | Mount the workDir at `PATH` inside the container. Packages then compile with `PATH` embedded in all install-time paths. |

### No-relocation builds with `--cvmfs-prefix`

In a conventional CVMFS publishing workflow the package is first compiled with the bits workDir as its install prefix (e.g. `/data/alice/sw/slc9_x86-64/ROOT/6.32.0-1`), and then `relocate-me.sh` rewrites every embedded path to the final CVMFS location (e.g. `/cvmfs/sft.cern.ch/lcg/releases/ROOT/6.32.0`). Relocation is a post-build transformation that can be expensive for packages with many compiled files.

`--cvmfs-prefix` (with `--docker`) removes this step: the workDir is mounted at the final CVMFS prefix inside the container, so install paths embedded at build time already point under that prefix. The package is installed at `<prefix>/<arch>[/<family>]/<package>/<version>-<revision>`, and that must be the `--cvmfs-target` passed to `bits publish --no-relocate`, since no paths are rewritten.

> **Note.** In the normal bits-console workflow these commands are run by the CI pipeline on a registered build runner — not typed by the user. bits-console passes `cvmfs_prefix` from the community's `ui-config.yaml` to the pipeline, which then calls `bits build --docker --cvmfs-prefix …` and `bits publish --no-relocate` automatically. The flags are documented here for CI pipeline authors and runner administrators.

```bash
# These commands run inside the bits-console-triggered CI pipeline on the build runner.
# Pipeline stage 1 — build with deployment paths embedded at compile time:
bits build --docker \
           --cvmfs-prefix /cvmfs/sft.cern.ch/lcg/releases \
           ROOT

# Pipeline stage 1 (continued) — hand to cvmfs-prepub; no relocation needed:
bits publish ROOT \
           --cvmfs-target /cvmfs/sft.cern.ch/lcg/releases/slc9_x86-64/ROOT/6.32.0-1 \
           --prepub-url https://prepub.example.org:8080 \
           --no-relocate
```

**Persistent workDir across CI jobs.** For communities that publish to CVMFS regularly, keeping the workDir alive between CI jobs (on a persistent build runner) turns `--cvmfs-prefix` into an incremental cache: only packages whose recipe or source changed are rebuilt; already-installed dependencies are reused from the previous run. The `bits prune` subcommand manages the cache size over time (see [bits prune](#bits-prune)).

---

### 22.1 Recipe Sandbox

Bits can run each recipe build script inside an isolated sandbox to limit the damage a malicious or buggy recipe can do. The sandbox wraps the actual `bash build.sh` execution — it does not affect source downloads, tarball extraction, or publishing.

#### How it works

Sandboxing is **off by default** (`--sandbox=off`); opt in with `--sandbox=auto` (or a specific mode), or persist it with `bits use build --sandbox auto`. With `auto`:

| Platform | With `--sandbox=auto` | Mechanism |
|----------|-----------------|-----------|
| Linux (local build, no `--docker`) | `off` | podman is **not** used (or even probed) for plain local builds |
| macOS (local build) | `sandbox-exec` if available, otherwise `off` | Built-in SBPL sandbox profile; no VM, no overhead |
| Any platform, `--docker` active | Nested podman inside the container if the builder image contains `podman`; otherwise `off`, with a warning | `podman run` launched from inside the Docker build container |

> **Note.** On a local Linux build without `--docker`, `--sandbox=auto` resolves to `off` and bits never invokes `podman` (not even `podman info`). podman-based recipe isolation on Linux is only engaged when the build runs inside `--docker`, or when it is requested explicitly with `--sandbox=podman` / `--sandbox-image`.

The workDir is bind-mounted at the same absolute path inside the podman container so that all paths embedded in the build environment (`$WORK_DIR`, `$INSTALLROOT`, `$SOURCEDIR`, etc.) resolve correctly.

#### Sandbox modes

Pass `--sandbox MODE` to `bits build`:

| Mode | Behaviour |
|------|-----------|
| `auto` | Pick the best available option: `sandbox-exec` on macOS, nested podman when `--docker` is active and the builder image contains `podman`, and `off` on a local Linux build (no `--docker`). On local Linux, podman is neither used nor probed — request it explicitly with `--sandbox=podman` if you want it. |
| `podman` | Always use podman; fails with an error if it is not available. With `--docker`, `podman` must exist in the builder image. Without `--docker`, `podman info` must succeed on the host and `--sandbox-image` must name the image (otherwise sandboxing is disabled with a warning). |
| `sandbox-exec` | macOS only. Fails with an error on Linux. |
| `off` | (default) No sandboxing. Recipe runs directly on the host. |

```bash
# Let bits choose
bits build --sandbox=auto ROOT

# Force podman with a specific image (no --docker required)
bits build --sandbox=podman --sandbox-image alisw/slc9-builder:latest ROOT

# No sandbox (the default)
bits build ROOT
```

Giving `--sandbox-image` alone selects `--sandbox=podman`. When `--docker` is used, `--sandbox-image` defaults to the same image as `--docker-image`, so no extra flag is needed:

```bash
# Docker build with nested podman sandbox — same image used for both layers
bits build --docker --sandbox=auto --docker-image alisw/slc9-builder:latest ROOT
```

#### Per-recipe network control

By default the sandbox blocks all outgoing network access from the recipe script. Some recipes need to reach the internet during their build (for example, to run `pip install` or `gem install`). Use the `sandbox_network` recipe field to opt in:

```yaml
package: my-tool
version: "1.0"
sandbox_network: off   # allow outgoing network inside the sandbox
---
pip install -r requirements.txt
make install
```

| `sandbox_network` value | Effect |
|-------------------------|--------|
| `on` | (default) Outgoing network is **blocked**. The restriction is active. |
| `off` | Outgoing network is **allowed**. The restriction is lifted. |

The field is silently ignored when `--sandbox=off`. The build-wide default comes from `bits build --sandbox-network on|off`, then `sandbox_network:` in the active defaults, then `on`; a recipe's own field always wins.

#### Docker-in-Docker (DinD)

If `bits --docker` is invoked from inside an existing Docker container (for example, a GitLab CI job that itself runs inside Docker), adding a nested podman layer is still possible but requires the outer Docker container to have been started with:

```
--security-opt seccomp=unconfined
```

or an equivalent unprivileged user-namespace configuration. Without this, the kernel will reject the `clone(CLONE_NEWUSER)` call that podman uses for rootless containers.

Bits detects that it is itself running inside a container and prints a warning when it adds the nested podman layer. If the outer container cannot be reconfigured, disable sandboxing for that job with `--sandbox=off`.

---

### 22.2 Cross-compilation via QEMU

Bits supports cross-compilation on any Docker-capable host by combining Docker's
`--platform` flag with QEMU user-mode emulation.  When the target architecture
differs from the host, Docker pulls the matching image variant (e.g. `arm64`)
and uses QEMU to transparently execute the foreign ELF binaries — the build script
sees a native `aarch64` environment without any changes to the recipe.

#### One-time host setup

Register QEMU binfmt handlers on the Docker host (persists until reboot):

```bash
# Option A — via the multiarch helper image (recommended, requires docker)
docker run --rm --privileged multiarch/qemu-user-static --reset -p yes

# Option B — via the OS package manager (Debian / Ubuntu)
apt-get install -y qemu-user-static binfmt-support
update-binfmts --enable

# Verify
docker run --rm --platform linux/arm64 alpine uname -m   # should print: aarch64
docker run --rm --platform linux/ppc64le alpine uname -m # should print: ppc64le
```

This is a one-time privileged operation on the runner host.  Subsequent containers
do not need elevated privileges; the kernel handles the QEMU dispatch transparently.

#### Supported target platforms

| bits `--architecture` substring | Docker `--platform` |
|----------------------------------|---------------------|
| `x86-64` / `x86_64` | `linux/amd64` |
| `aarch64` / `arm64` | `linux/arm64` |
| `ppc64le` | `linux/ppc64le` |
| `s390x` | `linux/s390x` |
| `riscv64` | `linux/riscv64` |

#### Automatic platform injection

When `--docker` is active, bits derives the required `--platform` string from
`--architecture` automatically and compares it to the detected host architecture.
If they differ, `--platform` is injected into both `docker run` invocations (the
long-running helper container used for pre-flight checks and the per-package build
container).  **No extra flags are needed for the common case**:

```bash
# On an x86-64 host, build for aarch64 — platform injected automatically
bits build MyAnalysis -a slc9_aarch64 --docker

# Equivalent explicit form
bits build MyAnalysis -a slc9_aarch64 --docker --docker-platform linux/arm64
```

Pass `--docker-platform native` to suppress automatic injection and always use the
daemon-default image variant (useful on a native ARM runner running an x86-64 bits
client, or for testing without QEMU overhead).

#### Builder image availability

A builder image must exist for the target CPU. The default images carry the CPU in
their name (for example `aarch64-slc9` for `slc9_aarch64`); the legacy
`alisw/<distro>-builder` images need an arm64 variant in their multi-arch manifest.
Confirm availability before scheduling cross-compilation CI jobs:

```bash
# Check that the default slc9 builder image for aarch64 exists
docker manifest inspect gitlab-registry.cern.ch/bits/containers/aarch64-slc9:latest
```

If no image exists for the target CPU, an ARM-native runner (available on CERN's
infrastructure and cheaply on cloud spot markets) is the practical alternative for
full-stack cross-compilation.

#### Architecture matching for batch jobs

Tarballs built for one architecture will not run on another.  When using the
S3-overlay workflow (personal analysis packages pushed to an S3 bucket and fetched
by WLCG batch jobs), the batch job description must constrain worker node selection
to match the build architecture:

```
# HTCondor
Requirements = (TARGET.OpSysAndVer == "CentOS9") && (TARGET.Arch == "X86_64")

# DIRAC JDL
SystemConfig = x86_64-slc9-gcc13-opt
```

bits does not check the worker node's architecture for you. Run
[`bits verify`](#23-bits-verify--deployment-verification) on a node to compare the
manifest's `architecture` with the node's.

#### Performance expectations

QEMU user-mode emulation runs at roughly 20–50 % of native execution speed for
compute-heavy C++ compilation.  This is acceptable for small analysis packages
(seconds to minutes per package) but impractical for large stacks such as ROOT or
Geant4 (builds would take 10–20 hours).  The recommended scope for QEMU
cross-compilation is:

- Personal analysis overlays (the S3-overlay workflow above): a few packages, tens of MB of output.
- Validation builds: confirming that a recipe compiles clean on a target
  architecture before scheduling a native-runner CI job for the full stack.

For full experiment stacks on non-x86-64 architectures, use a native runner of
the target architecture.

#### Sandbox interaction

Nested QEMU + rootless podman (the DinD sandbox scenario) requires
`--security-opt seccomp=unconfined` on the outer `docker run` and may still fail
on older kernels without unprivileged user-namespace support.  Bits emits a warning
when cross-compilation is active and `--sandbox` is not `off`.  For cross-compilation
builds, `--sandbox=off` is the recommended setting unless the runner is known to
support nested namespaces under QEMU:

```bash
bits build MyAnalysis -a slc9_aarch64 --docker --sandbox=off
```

---

## 23. bits verify — Deployment Verification

`bits verify` confirms that a live deployment — packages in a CVMFS mount or a
local work directory — matches the build manifest written by `bits build`.  It
is the primary tool for closing the loop between the build record and what is
actually deployed on worker nodes.

```
bits verify --from-manifest bits-manifest-2026-01-15.json \
            --cvmfs-root /cvmfs/alice.cern.ch \
            --work-dir /opt/sw
```

### What is checked

**Packages** — for each entry in `manifest.packages[]`:

1. The tarball is located in the content-addressed store under `TARS/<arch>/store/<hash[:2]>/<hash>/<tarball>`, where `<arch>` is the manifest's top-level `architecture`.
2. Its SHA-256 is recomputed and compared to `tarball_sha256` in the manifest.
3. Packages with no tarball or checksum recorded in the manifest (for example `outcome: already_installed`) are marked **SKIP**.

**Providers** — for each entry in `manifest.providers[]`:

1. If the `checkout_dir` does not exist on the current machine, the entry is **SKIP** (provider checkouts are usually only present on build hosts).
2. Otherwise, the checkout's current `HEAD` commit is compared to the manifest's `commit` field. If `HEAD` cannot be read, the entry is **MISS**.

**Architecture** — the manifest's `architecture` is compared, as an exact string,
with the architecture bits detects on the current host. A mismatch is a **FAIL** and
counts toward the exit code. A manifest whose architecture carries a defaults suffix
(for example `slc9_x86-64-gcc13`) therefore reports FAIL even on a matching host.

### Search order

Tarballs are searched in this order:

1. `--cvmfs-root PATH` (if given) — typically the CVMFS mount point.
2. `--work-dir DIR` (default: `$BITS_WORK_DIR`, else `sw`) — the local bits work directory.

The first root where the content-addressed tarball file exists is used.  This
allows verifying a deployment that spans both CVMFS (for the common stack) and
a local overlay (for personal analysis packages).

### Output formats

**Human-readable (default)**

```
━━━ bits verify  —  bits-manifest-2026-01-15.json ━━━━━━━━━━━━━━━━━━━

  File:       /builds/bits-manifest-2026-01-15.json
  Schema:     v4
  Created:    2026-01-15T08:42:11Z
  Build:      complete

  Architecture: PASS  slc9_x86-64

  Packages (4):
        package                        version-revision   detail
    --------------------------------------------------------------------------
    PASS  ROOT                           6.32.02-1          sha256 OK
    PASS  Geant4                         11.2.1-2           sha256 OK
    SKIP  CMake                          3.28.0-0           already_installed — no output tarball expected
    MISS  MyAnalysis                     1.0-3              tarball not found
        searched: /cvmfs/alice.cern.ch/TARS/slc9_x86-64/store/ab/ab3f.../MyAnalysis-1.0-3.slc9_x86-64.tar.gz

  Providers (1):
        name                           detail
    --------------------------------------------------------------------------
    SKIP  alidist                        checkout not present locally

  Summary: 2 PASS  0 FAIL  1 MISS  2 SKIP  (of 5 total)
```

ANSI colours are emitted when stdout is a TTY: green for PASS, red for FAIL,
yellow for MISS, dark grey for SKIP.

**JSON (`--json`)**

```json
{
  "manifest_created_at": "2026-01-15T08:42:11Z",
  "manifest_status": "complete",
  "schema_version": 4,
  "architecture": { "manifest": "slc9_x86-64", "host": "slc9_x86-64", "status": "PASS" },
  "packages": [
    { "package": "ROOT", "version": "6.32.02", "revision": "1", "status": "PASS", "detail": "sha256 OK" },
    ...
  ],
  "providers": [ ... ],
  "summary": { "PASS": 2, "FAIL": 0, "MISS": 1, "SKIP": 2 },
  "exit_code": 2
}
```

### Exit codes

| Code | Meaning |
|------|---------|
| 0 | All verifiable entries match — deployment is consistent with the manifest. |
| 1 | One or more entries are **FAIL** (hash mismatch or provider commit mismatch). |
| 2 | One or more entries are **MISS** (tarball not found; consistency unknown). If there are also FAILs, exit code 1 takes precedence. |
| 3 | The manifest file is missing, cannot be read, or is not valid JSON. |

### Status values

| Status | Meaning |
|--------|---------|
| **PASS** | Entry verified successfully. |
| **FAIL** | Checksum or commit mismatch — the deployed artifact differs from the build record. |
| **MISS** | Tarball not found in any search root, or a provider checkout's `HEAD` cannot be read — cannot confirm consistency. |
| **SKIP** | Entry not verifiable on this machine (already-installed packages, absent provider checkouts). |

### Options

See the [`bits verify`](#bits-verify) entry of the command-line reference.

---

## 24. Design Principles & Limitations

### Principles

1. **Reproducibility** — Stripping the shell environment and pinning exact git commits ensures the same inputs always produce the same build.
2. **Incrementalism** — The content-addressable hash scheme rebuilds only what has changed, keeping iteration fast even on large stacks.
3. **Isolation** — Each package builds in its own directory with a sanitised environment (locale forced to `C`, `BASH_ENV` unset, only declared dependencies visible).
4. **Parallelism** — Independent packages can build at the same time (`--builders N`), and each build script can run parallel jobs (`$JOBS`).
5. **Simplicity** — Build scripts are plain Bash, not a new DSL; the YAML header is metadata only.
6. **Portability** — Runs on any modern Linux distribution and on macOS (Intel and Apple Silicon).
7. **Extensibility** — The repository provider mechanism allows recipe sets to be composed dynamically from versioned git repositories without modifying the main configuration.

### Current limitations

- **Linux and macOS only** — Bits runs on Linux and macOS (Intel and Apple Silicon); Windows is not supported.
- **Git and Sapling only** — Version-controlled sources must come from Git or Sapling; Subversion and Mercurial are not supported. Plain archives can be downloaded through a recipe's `sources:` list (HTTP(S), FTP(S) or local `file:` URLs).
- **Environment Modules required** for `bits enter / load / unload` — the `modulecmd` binary must be installed separately.
- **Active development** — The recipe format and Python APIs may change between versions. Evaluate thoroughly before adopting in production pipelines.

---

## 25. Build Manifest

Every `bits build` run writes a self-contained JSON manifest to the work
directory. It records the inputs and outputs of the build: the requested
packages, architecture, defaults profile, recipe and provider commits, and the
identity (hash and tarball checksum) of every package that was built, taken from
the store, or already installed.

```bash
# Build normally — manifest is always written
bits build ROOT

# The manifest file is printed in the success banner, e.g.:
#   Build manifest written to:
#     $WORK_DIR/MANIFESTS/bits-manifest-ROOT-20260411T143000Z.json
#
# A convenience symlink is kept current after every write:
ls -la $WORK_DIR/MANIFESTS/bits-manifest-latest.json
```

### What is recorded

The manifest records every input and output that could affect reproducibility:

**Global build parameters**

| Field | Description |
|---|---|
| `bits_version` | Version string of the bits tool itself |
| `bits_dist_hash` | Commit of the recipe repository checkout (`$BITS_DIST_HASH`; same value as `config_commit`) |
| `requested_packages` | Packages passed on the command line |
| `architecture` | Combined architecture string (may include defaults suffix) |
| `defaults` | Active defaults profile(s) |
| `config_dir` | Absolute path to the recipe repository (`.bits` checkout) |
| `config_commit` | HEAD commit of the recipe repository at build time |
| `status` | `"in_progress"` → `"complete"` or `"failed"` |
| `system_packages` | Packages taken from the system instead of built (schema v4) |
| `cvmfs_templates` | The build's CVMFS path templates, used by `bits cvmfs publish` to place every package (optional) |

**Providers** (one entry per repository-provider package)

| Field | Description |
|---|---|
| `name` | Provider package name |
| `checkout_dir` | Absolute path of the local clone |
| `commit` | Full git commit hash of the cloned provider |
| `remote_url` | `origin` remote URL (or `null` if not readable) |

**Packages** (one entry per package, in build order)

| Field | Description |
|---|---|
| `package` | Package name |
| `version` | Package version |
| `revision` | Assigned revision (local or remote) |
| `hash` | Content-addressable build hash |
| `commit_hash` | Source commit hash (or `"0"` for untracked sources) |
| `outcome` | `"already_installed"`, `"from_store"`, or `"built_from_source"` |
| `tarball` | Tarball filename (or `null`) |
| `tarball_sha256` | `sha256:<hex>` digest of the tarball, if present; when the package is in the remote store, the digest of the store copy |
| `source_checksums` | List of `{url, checksum}` entries from the recipe's `sources:` list; `checksum` is `null` when none was declared |
| `built_by` | `user@host` that compiled this hash; `null` unless `outcome` is `"built_from_source"` (recalled artifacts carry their builder in another build's manifest) |
| `completed_at` | ISO-8601 UTC timestamp of package completion |
| `pkg_family` | Install family sub-directory (empty if none) |
| `effective_architecture` | `share` for noarch packages, the neutral arch for `own_hash` packages, else the build arch |
| `patches`, `variables` | Recipe patches with their checksums, and the resolved recipe variables (schema v3) |
| `requires`, `build_requires` | Direct runtime (+ untracked) and build-only dependencies, including system-provided ones (schema v4; the SBOM dependency graph) |
| `source`, `tag` | Git repository URL and tag/branch built (empty if none; schema v4) |
| `provides_repository`, `redistributable`, `license`, `view` | Recipe metadata used when publishing: repository-provider packages are skipped; `redistributable` (`all`, `binaries`, `sources` or `none`) limits what may be uploaded or published; `license` feeds the release NOTICE file; `view` (only when the recipe sets it) holds the recipe's merged-view rules |

### Manifest location and naming

Manifests are written to a dedicated subdirectory of the bits work directory (`--work-dir`, default `sw`):

```
$WORK_DIR/
  MANIFESTS/
    bits-manifest-ROOT-20260411T143000Z.json   ← one file per build run (top-level package + UTC timestamp)
    bits-manifest-latest.json             ← symlink to the most recent manifest
```

Keeping manifests in `MANIFESTS/` prevents them from cluttering the work directory root alongside package install trees.

The manifest is written **incrementally**: after each package completes (or
is confirmed already installed), so a failed build still produces a partial
manifest recording what succeeded.

The `bits-manifest-latest.json` symlink is replaced atomically after every
write, so readers always see a complete manifest.

### Manifest schema reference

```json
{
  "schema_version": 4,
  "bits_version": "1.0.0",
  "bits_dist_hash": "abc123def456...",
  "created_at": "2026-04-11T14:30:00Z",
  "updated_at": "2026-04-11T14:45:12Z",
  "status": "complete",
  "requested_packages": ["ROOT"],
  "architecture": "slc7_x86-64",
  "defaults": ["release"],
  "config_dir": "/home/user/myrecipes",
  "config_commit": "abc123def456...",
  "providers": [
    {
      "name": "myorg-recipes",
      "checkout_dir": "/home/user/sw/REPOS/myorg-recipes",
      "commit": "deadbeef12345678...",
      "remote_url": "https://github.com/myorg/recipes.git"
    }
  ],
  "packages": [
    {
      "package": "zlib",
      "version": "1.2.11",
      "revision": "3",
      "hash": "abcd1234abcd1234...",
      "commit_hash": "0",
      "outcome": "from_store",
      "tarball": "zlib-1.2.11-3.slc7_x86-64.tar.gz",
      "tarball_sha256": "sha256:e3b0c44298fc1c14...",
      "source_checksums": [
        {"url": "https://zlib.net/zlib-1.2.11.tar.gz",
         "checksum": "sha256:c3e5e9fdd5004dcb542feda5ee4f0ff0744628baf8ed2dd5d66f8ca1197cb1a1"}
      ],
      "completed_at": "2026-04-11T14:31:05Z"
    },
    {
      "package": "ROOT",
      "version": "6.32.04",
      "revision": "2",
      "hash": "ef567890ef567890...",
      "commit_hash": "feedcafe12345678...",
      "outcome": "built_from_source",
      "tarball": "ROOT-6.32.04-2.slc7_x86-64.tar.gz",
      "tarball_sha256": "sha256:f4ca408ad2b...",
      "source_checksums": [],
      "completed_at": "2026-04-11T14:45:10Z"
    }
  ]
}
```

(Abbreviated: the v3/v4 fields listed above are omitted from the example.)

When a build fails, the manifest contains a `"failed_package"` field and
optionally a `"failure_reason"`:

```json
{
  "status": "failed",
  "failed_package": "ROOT",
  "failure_reason": "build script exited 1"
}
```

### Replaying a build with `--from-manifest`

Pass `--from-manifest FILE` to instruct bits to re-run the build described
by a manifest.  The `PACKAGE` positional argument is optional when
`--from-manifest` is given — the manifest's `requested_packages` list is
used automatically:

```bash
# Replay from the latest manifest (no package name needed):
bits build --from-manifest $WORK_DIR/MANIFESTS/bits-manifest-latest.json

# Build the named package instead of the manifest's package list:
bits build --from-manifest bits-manifest-ROOT-20260411T143000Z.json ROOT

# Replay an older manifest:
bits build --from-manifest bits-manifest-ROOT-20260101T090000Z.json
```

During a replay run bits takes only the package list (`requested_packages`)
from the manifest. Everything else comes from the current invocation, as for a
normal build: the architecture, `--defaults`, and the recipe repository as it is
checked out now. Versions and hashes are not pinned to the manifest, and recalled
tarballs are not checked against its `tarball_sha256` values.

> **Exact replay.** Check out the manifest's `config_commit` in the recipe
> repository (and the recorded provider commits), and pass the recorded
> `architecture` and `defaults` on the command line. Use
> [`bits verify`](#23-bits-verify--deployment-verification) to check tarballs
> against the manifest's checksums.

### Manifest and store integrity

The build manifest and the [store integrity ledger](#store-integrity-verification)
are complementary:

- The **ledger** (`STORE_CHECKSUMS/`) guards individual tarballs against
  store-backend tampering during the current build cycle.
- The **manifest** records the complete provenance of a build run and
  enables future replays and audits.

With `--store-integrity` on, the manifest's `tarball_sha256` fields are a
second, portable copy of each digest, which survives even if the local ledger
directory is deleted.

---

## 26. CVMFS Publishing Pipeline

The publishing commands are part of bits: [`bits publish`](#bits-publish) sends one package (or a release view) to CVMFS through the cvmfs-prepub service, and `bits cvmfs publish` publishes every package of a build from its manifest (see [bits store / bits cvmfs](#bits-store--bits-cvmfs-admin--ci-groups)).

The bits-console web interface, which triggers and monitors these CI builds, is maintained in the **[bits-console](https://gitlab.cern.ch/buncic/bits-console)** repository, together with the community `ui-config.yaml` reference, role-based access configuration (production vs personal-area builds) and the pipeline variable reference. Its backend service (manifest signing) lives in this repository under `console-backend/`.

---

*Back to [User Guide](USERGUIDE.md) · [Cookbook](COOKBOOK.md)*
