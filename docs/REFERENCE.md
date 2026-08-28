# Bits — Reference Manual

> **See also:** [User Guide](USERGUIDE.md) · [Cookbook](COOKBOOK.md) · [Workflows](WORKFLOWS.md) · [Roadmap](ROADMAP.md)

## Table of Contents

> **Note:** Sections §§1–7 (Introduction through Cleaning Up) are in [USERGUIDE.md](USERGUIDE.md). This document covers developer and technical reference material starting from §9.

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
    - [§22.1 Recipe Sandbox](#221-recipe-sandbox)
    - [§22.2 Cross-compilation via QEMU](#222-cross-compilation-via-qemu)
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
  ├─ environment sub-commands (enter, load, unload, setenv, q, list)
  │    └─ handled directly via modulecmd calls
  │
  └─ build sub-commands (build, clean, deps, doctor, init, version …)
       └─ bitsBuild  (Python entry point)
            └─ bits_helpers/
                 ├─ args.py           argument parsing
                 ├─ build.py          main orchestration loop
                 ├─ utilities.py      recipe parsing, hashing, dep resolution
                 ├─ repo_provider.py  dynamic recipe-repository loading
                 ├─ scheduler.py      parallel build scheduler
                 ├─ sync.py           remote binary store backends
                 ├─ workarea.py       source checkout management
                 ├─ git.py / sl.py    SCM wrappers
                 └─ ...
```

### Architecture string and the `architecture:` template

The architecture string (e.g. `ubuntu2510_x86-64`) names install dirs, tarballs,
store paths and Docker images. By default it is auto-detected by `doDetectArch`
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

Precedence: an explicit `--architecture` on the command line always wins and the
template is ignored; otherwise the template (if any) is rendered against the
detected platform; with neither, the auto-detected default stands. Architecture
recognition (`matchValidArch`), the Docker builder-image name and the S3 cache
lookup all match by content — the distro and CPU tokens — independently of order
and of the `x86-64`/`x86_64` separator, so custom layouts work without
`--force-unknown-architecture`.

### CVMFS layout

A defaults profile (typically `defaults-release.sh`) may declare where a build's
packages and modulefiles live on CVMFS, so the build / publish / reuse paths are
derived from one place instead of repeated CLI flags. Three optional, templated
fields (templates may use `%(architecture)s`, the effective combined arch):

```yaml
cvmfs_dir:   /cvmfs/sft.cern.ch/lcg/releases   # CVMFS root
install_dir: %(architecture)s/Packages         # relative to cvmfs_dir
module_dir:  %(architecture)s/modules          # relative to cvmfs_dir
```

bits resolves these to `install_path` / `module_path` and uses them to default:

- **docker build:** `--cvmfs-prefix` ← `<cvmfs_dir>/<install_dir>`, so packages
  compile at their final CVMFS prefix and relocation on publish is a no-op
  (explicit `--cvmfs-prefix` still wins);
- **reuse:** `--reuse-from cvmfs` resolves the deployed modules tree from the
  same layout, so already-deployed components are set up from their published
  modulefiles (`--remote-store` stays the tarball store; it is never `cvmfs://`).

Builds that don't set any of these fields are unaffected.

### Build pipeline (inside `doBuild`)

```
fetch_repo_providers_iteratively()   ← clone any repository-provider packages,
                                        extend BITS_PATH, repeat until stable
        │
getPackageList()                     ← parse all recipes, resolve full DAG
        │
storeHashes()                        ← compute content-addressable hash per pkg
        │
        ├─ download pre-built tarballs from remote store (parallel)
        │
        └─ for each package in topological order:
               updateReferenceRepoSpec()  ← mirror source repo
               checkoutSource()           ← clone/checkout into build area
               runBuildScript()           ← execute the recipe's Bash script
               packageTarball()           ← archive the install root
               uploadTarball()            ← push to write store (if configured)
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

Reuse at any tier requires an exact content-hash match — the hash encodes the
full recipe + resolved dependency closure + defaults + architecture, so a hit is
byte-for-byte the artifact bits would otherwise have built. `--no-remote-store`
disables the remote tiers (CVMFS reuse and the S3/HTTP archive); the local store
and build-from-source always remain.

#### Signing and verifying the archive tier

The tier-3 attestation is driven by three build flags:

- `--sign-manifest KEY.pem` — after a successful build, sign the build manifest
  (`bits-manifest-latest.json`) with an Ed25519 private key. The detached
  signature is written next to the manifest (`.sig`). Run this on the release/CI
  host that produced the archive; the private key never ships.
- `--trust-manifest URL|PATH` — the signed release manifest a consumer trusts as
  the authority for archive reuse. Its signature is verified against the public
  keys shipped in `bits/keys/` (plus `$BITS_TRUST_KEYS` and
  `~/.config/bits/keys`).
- `--require-signed-reuse` — fail closed: a tarball recalled from the remote
  store is reused only when `--trust-manifest` lists its hash **and** its sha256
  matches. Unlisted → discard and rebuild; sha256 mismatch → fatal (tampering).
  Local build-node and CVMFS artifacts are unaffected.
- `--trust-groups G1,G2,…` — scope reuse by group. The signed common manifest may
  tag each entry with a `group`; with `--trust-groups` a consumer trusts only
  those groups plus the always-trusted `common` base (untagged entries count as
  base). Omit it to trust every signed entry. Produce group tags at certification
  time with `bits certify --group GROUP`.
- `--reuse-beacon URL` (or `$BITS_REUSE_BEACON`) — report the hashes this build
  reused from the store to `<URL>/api/reuse` (best-effort, fire-and-forget in a
  daemon thread; never blocks or fails the build). Only small references are
  sent, never artifact data. Feeds usage-informed GC.

#### Publish-triggered certification

`bits publish --certify --certify-group <group> --manifests-remote <git-url>`
uploads to S3 and then **opens a merge request** in the manifests repo that adds
this build's manifest under `manifests/<group>/`. The MR is created via the
GitLab REST API with your PAT (works even when you push over SSH — only the host
+ project path are taken from the remote URL; PAT from `--gitlab-token`,
`$BITS_CERTIFIER_TOKEN`/`$GITLAB_TOKEN`, or `~/.bits/gitlab-token`, chmod 600).
The MR **author is you**; CI validates that author is a group/bits admin, signs
the merged common manifest, and publishes it to S3 (recording you as
`certified_by`). No PAT is exposed to the CI job.

`--certify-group` and `--manifests-remote` default from the active defaults'
`system:` block, so a community that configures them can just run `bits publish`:

```yaml
system:
  certify_group:    ship
  manifests_remote: https://gitlab.cern.ch/buncic/bits-manifests.git
```

Giving `--certify-group` (or having both configured) implies `--certify`;
`--no-certify` opts out. These live under `system:` because they are publish
policy, not part of any package hash.

#### Certification — `bits certify`

`bits certify <manifests…> --key <ed25519.pem> -o common-manifest.json` merges
published build manifests into one signed common manifest (the trust unit), after
validating every hash against the S3 store (`--store`). Group tagging with
`--group`, offline dry-merge with `--no-store-check`. In the manifests-repo CI,
`--require-approval --admins ADMINS` refuses to sign unless the certifier is an
authorised admin. The identity is established in one of three ways (in order):
`--certifier USERNAME` (default `$GITLAB_USER_LOGIN` — the pipeline initiator
GitLab already authenticated; no API call); `--certifier-token PAT` (identify via
`GET /user`); or, failing both, reading who approved the merge request. See the
`bits-manifests` repo for the pipeline scaffolding.

Offline freshness: `--valid-days N` stamps an `expires` timestamp and
`--source-commit SHA` (default `$CI_COMMIT_SHA`) records the certified commit. A
consumer's trust gate (`trusted_index`) rejects a signed manifest whose `expires`
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
  Enforced at signing (producer) and in `trusted_index` (consumer); absent policy
  = no restriction. See `keys/README.md`.

Per-platform certification: the store is content-addressed **per
architecture** — object identity is `(effective_architecture, hash)` — so
entries from different platforms can never conflict and certification is
scoped by platform. `bits publish` emits **one BOM per effective
architecture** ("shared" — noarch — is just another platform; the arch is in
the BOM file name), and `bits certify --architectures A1,A2` merges,
store-validates and signs only those platforms' BOMs, leaving the other
platforms' signed manifests untouched. A scoped platform whose BOMs are all
gone is re-signed **empty** — deleting a platform's BOMs revokes its entries.
The manifests-repo CI derives the changed architectures from the merge diff,
so validation cost scales with the change, not with the whole store, and one
platform's problem never blocks another's certification. Without
`--architectures` every architecture present is re-derived (full run).

Store objects are **authoritative** for their checksum: a `.tar.gz` is not
byte-reproducible, so the same content hash re-packed by a later build is a
different file — expected and benign. An upload that finds an object already
at its designated path keeps it and records **its** sha256 in the build
manifest and BOM (read cheaply from the object's `x-amz-meta-sha256`; legacy
objects are streamed once and stamped), so every manifest converges on the
one stable object that certification verifies. Store objects are never
overwritten.

#### Store garbage collection — `bits gc`

`bits gc --trust-manifest <signed-common-manifest>` sweeps unreferenced objects
from the shared S3 store. The roots are every content hash in the *verified*
signed common manifest; any store object whose hash is not a root and is older
than `--grace-days` (default 7) is removed. It is deliberately conservative:

- **Fail-closed** — if the manifest does not verify, or verifies to zero roots,
  nothing is swept (an unverifiable manifest never becomes "delete everything").
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

Certification (signing) is a separate, deliberate step performed by a **group
admin** via bits-console (SSO-authenticated), which triggers a CI job to sign the
manifest with the single trust anchor key. Consumers reuse only artifacts listed
in a verified signed manifest (`--require-signed-reuse` + `--trust-manifest`). See
`docs/adr/0004-group-signed-trusted-reuse.md` for the full model.

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
build script writes a per-package `NOTICE` into each `$INSTALLROOT` (from
`license:`/`acknowledgment:` and the source location), and `bits publish`
generates the per-release aggregation — `NOTICE` (required attributions,
every distributed package with its SPDX id, and the licence-excluded list)
plus `LICENSE-SOURCE-OFFER.txt` (where the corresponding sources of every
copyleft component are archived, and for how long) — uploaded next to the
release's BOMs under `MANIFESTS/<build_id>/` and placed at the root of a
published release view.

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
pip install -e .[test,docs]
```

Code style is enforced by `.flake8` (flake8) and `.pylintrc` (pylint). Run the linters before submitting a patch:

```bash
flake8 bits_helpers/
pylint bits_helpers/
```

---

## 11. Key Source Files

| Path | Purpose |
|------|---------|
| `bits` | Bash entry point; handles environment sub-commands, delegates build to `bitsBuild` |
| `bitsBuild` | Python entry point; dispatches all build sub-commands |
| `bitsDeps` | Thin wrapper calling `bitsBuild deps` |
| `bitsDoctor` | Thin wrapper calling `bitsBuild doctor` |
| `bitsenv` | Legacy environment manager |
| `bits_helpers/args.py` | Argument parsing for all sub-commands |
| `bits_helpers/build.py` | Core build orchestration (~2 200 lines); `doBuild`, `storeHashes` |
| `bits_helpers/utilities.py` | Recipe YAML parsing, hash computation, `getPackageList`, `getConfigPaths` |
| `bits_helpers/repo_provider.py` | Iterative repository-provider discovery and caching |
| `bits_helpers/deps.py` | DOT/PDF dependency graph generation via Graphviz |
| `bits_helpers/init.py` | `bits init` — writable development checkouts |
| `bits_helpers/doctor.py` | `bits doctor` — system-requirements checking |
| `bits_helpers/clean.py` | `bits clean` — stale artifact removal from temporary build area |
| `bits_helpers/cleanup.py` | `bits cleanup` — LRU + disk-pressure eviction from persistent workDir; sentinel management |
| `bits_helpers/publish.py` | `bits publish` — copy, relocate, and stream packages to a CVMFS ingestion spool |
| `bits_helpers/scheduler.py` | Multi-threaded parallel build scheduler |
| `bits_helpers/sync.py` | Remote binary store backends (HTTP, S3, Boto3, CVMFS, rsync) |
| `bits_helpers/git.py` | Git SCM wrapper |
| `bits_helpers/sl.py` | Sapling (`sl`) SCM wrapper |
| `bits_helpers/workarea.py` | Source-checkout and reference-mirror management |
| `bits_helpers/download.py` | Tarball download helpers |
| `bits_helpers/log.py` | Logging and progress output |
| `bits_helpers/cmd.py` | Subprocess execution helpers; `DockerRunner` |
| `bits_helpers/analytics.py` | Optional anonymous usage analytics |
| `bits_helpers/resource_manager.py` | Resource-aware build scheduling |
| `templates/` | Jinja2 templates for generated build scripts and module files |
| `tests/` | Full test suite |
| `docs/` | MkDocs documentation source |

---

## 12. Writing Recipes

A recipe is a file named `<package>.sh` placed inside a `*.bits` directory. It has two sections separated by `---`:

1. A **YAML header** — package metadata, dependencies, and environment.
2. A **Bash build script** — the actual build steps.

### Minimal recipe

```yaml
package: zlib
version: "1.2.13"
source: https://github.com/madler/zlib.git
tag: v1.2.13
---
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
  BOOST_ROOT: "$INSTALLROOT"
prepend_path:
  PATH:              "$INSTALLROOT/bin"
  LD_LIBRARY_PATH:   "$INSTALLROOT/lib"
---
cd "$SOURCEDIR"
./bootstrap.sh --prefix="$INSTALLROOT" --with-python=$(which python3)
./b2 -j${JOBS:-1} \
     --build-dir="$BUILDDIR" \
     --prefix="$INSTALLROOT" \
     variant=release link=shared install
```

For the complete list of YAML header fields and build-time environment variables see [§17 Recipe Format Reference](#17-recipe-format-reference).

### Function-based recipes with bits-recipe-tools

The optional [`bits-recipe-tools`](https://github.com/bitsorg/bits-recipe-tools) package provides a higher-level authoring style using reusable shell function hooks (`CMakeRecipe`, `AutoToolsRecipe`, etc.). Instead of writing a flat Bash build script, you override only the lifecycle hooks that differ from the defaults (`Prepare`, `Configure`, `Make`, `MakeInstall`, `PostInstall`). See the [Cookbook — Using bits-recipe-tools](COOKBOOK.md#using-bits-recipe-tools) for worked examples.

---

## 13. Repository Provider Feature

A **repository provider** is a recipe that, instead of describing a software package to build, describes *another recipe repository* to load dynamically at dependency-resolution time.

### Why it exists

Normally the set of recipe repositories (`*.bits` directories) is fixed at startup via the `BITS_PATH` environment variable. The repository provider feature lets a recipe itself pull in an additional recipe repository from git, enabling modular recipe sets and nested providers.

### Defining a repository provider

Add these fields to any recipe's YAML header:

```yaml
package: my-extra-recipes
version: "1.0"
source: https://github.com/myorg/my-extra-recipes.git
tag: v1.0

# Mark this recipe as a repository provider
provides_repository: true

# Where to insert the cloned directory in BITS_PATH (default: append)
repository_position: prepend   # or: append
```

The `source` URL must point to a git repository whose top-level directory contains `*.sh` recipe files (the same layout as any other `*.bits` directory).

### Always-on providers (`always_load: true`)

A provider recipe can be marked to load unconditionally — before the dependency graph is even traversed — by setting `always_load: true` alongside `provides_repository: true`:

```yaml
package: shared-recipes
version: "1"
source: https://github.com/myorg/shared-recipes.git
tag: stable
provides_repository: true
always_load: true
repository_position: prepend
```

Any recipe file in the primary config directory (`-c / --configDir`) that has both flags set is cloned and added to `BITS_PATH` at startup, making its recipes visible to all subsequent dependency resolution without any package needing to declare an explicit dependency on it. This is the recommended way to distribute a curated set of approved recipes across a team.

### The `bits-providers` standard repository

Bits ships a **built-in default provider** pointing at the official `bitsorg/bits-providers` repository on GitHub. This repository contains vetted, community-approved recipes and is loaded automatically on every build unless overridden:

```
BITS_PROVIDERS=https://github.com/bitsorg/bits-providers  (default)
```

**Overriding or disabling the default:**

```bash
# Use a private provider repository instead
export BITS_PROVIDERS=https://github.com/myorg/my-recipes.git@main

# Or set it persistently in bits.rc / .bitsrc / ~/.bitsrc:
# [bits]
# providers = https://github.com/myorg/my-recipes.git@stable

# Pin to a specific tag
export BITS_PROVIDERS=https://github.com/bitsorg/bits-providers@v2.0
```

The `@tag` suffix is optional; when omitted, `main` is used.

### Front-end choice: native `bits` (provider path) vs `aliBuild` (legacy path)

Which path is used is chosen by the front-end:

- **Native `bits`** uses the **provider path**: `bits_providers` defaults to the official `bitsorg/bits-providers` registry, so the always-on provider and the org-pointer bootstrap are active.
- **The `aliBuild` wrapper** (it exports `BITS_BRANDING=aliBuild`) emulates **legacy aliBuild**: the providers default is *empty*, so no registry is loaded and recipes come from a local `alidist` checkout instead. `aliBuild init` clones `alisw/alidist`, `aliBuild build <PKG>` uses it directly, and the legacy build-time `init.sh` is kept (`BITS_LEGACY_INITDOTSH=1`, alidist-compatible hashes; `--legacy-initdotsh` selects it explicitly).

An explicit `BITS_PROVIDERS` / `--providers` / `bits.rc providers` overrides the default in either mode.

### Bootstrapping a recipe repository from the registry

When native `bits` runs without a recipe directory, it bootstraps one through the registry: it follows the `<organisation>.bits.sh` (or `default.bits.sh`) pointer in `bits-providers` and clones the recipe repo it names. That pointer recipe's own `requires` are then seeded into provider discovery, so a base provider it depends on (e.g. `alice.bits` `requires: [alidist.bits]`) is loaded too — even though it is not a dependency of the package being built.

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
repository_position: prepend
```

This package is loaded in Phase 1 (before the iterative scan), so its recipes are visible from the very first dependency-resolution pass. Because the package name `bits-providers` is reserved, any recipe file of that name found in the config directory is skipped during the Phase 2 config-dir scan to prevent double-cloning.

### `bits.rc` configuration

Provider settings can be stored persistently in a bits configuration file. Bits searches for the following files in order and reads the first one found:

Relevant keys in the `[bits]` section:

```ini
[bits]
# Override or disable the default BITS_PROVIDERS URL.
# An explicit BITS_PROVIDERS environment variable takes precedence.
providers = https://github.com/myorg/my-recipes.git@stable
```

### Provider policy

By default every repository-provider's checkout is **appended** to `BITS_PATH`, regardless of what its `repository_position` field declares.  This is the safe default: an appended provider can only add new recipes, never silently replace an existing one.

A provider that needs to appear *before* other directories — for example to shadow a recipe in the default repository with a patched version — must be explicitly granted `prepend` access by the operator via the `provider_policy` setting.  Provider recipes cannot self-elevate.

#### Configuration

In `bits.rc` (persistent, applies to every run in this work tree):

```ini
[bits]
# Grant one provider prepend access; keep all others at the safe default.
provider_policy = bits-providers:prepend

# Multiple entries are comma-separated.
provider_policy = bits-providers:prepend, myorg-extras:append
```

On the command line (per-invocation override):

```bash
bits build --provider-policy bits-providers:prepend MyPackage
```

The CLI flag takes precedence over `bits.rc`.

#### How position is resolved

For each provider, bits evaluates the policy in this order:

| Priority | Source | Effect |
|----------|--------|--------|
| 1 (highest) | `provider_policy` entry for this provider | Exact position used, overrides recipe |
| 2 | Recipe's `repository_position` field, **only if `append`** | Respected as-is |
| 3 (default) | Recipe's `repository_position: prepend` **without policy** | Downgraded to `append`; a warning names the required `bits.rc` line |
| 4 | No field in recipe | `append` |

When a provider is about to be prepended (whether from policy or recipe), bits scans recipes already visible on `BITS_PATH` and warns for every name collision, listing the affected recipes and the `bits.rc` line that would suppress the warning.  The primary config directory (passed via `-c / --config-dir`) is always position 0 in the search order and **cannot** be shadowed by any provider.

#### Example: patching a default recipe

Suppose `myorg-patches` contains a modified `zlib.sh` that you want to take precedence over the version in the upstream provider:

```ini
[bits]
provider_policy = myorg-patches:prepend
```

```bash
bits build --provider-policy myorg-patches:prepend ROOT
# Warning: Provider 'myorg-patches' will shadow 1 recipe(s) already visible
#   from /path/to/bits-providers: zlib
# (expected and intended — the warning is informational)
```

### Precedence for `BITS_PROVIDERS`

| Priority | Source | Example |
|----------|--------|---------|
| 1 (highest) | `BITS_PROVIDERS` environment variable | `export BITS_PROVIDERS=…` |
| 2 | `providers` key in `bits.rc` / `.bitsrc` / `~/.bitsrc` | `providers = …` |
| 3 (default) | Built-in default | `https://github.com/bitsorg/bits-providers` |

### How providers are discovered (two-phase)

`bits build` loads providers in two phases before the main `getPackageList` call:

**Phase 1 — always-on providers** (`load_always_on_providers`):

1. If `BITS_PROVIDERS` is set, synthesise and clone the `bits-providers` package and prepend it to `BITS_PATH`.
2. Glob `*.sh` files in the config directory; clone any that have both `provides_repository: true` and `always_load: true` (skipping `bits-providers` if already handled).

**Phase 2 — iterative dependency-driven scan** (`fetch_repo_providers_iteratively`):

The scan is seeded with the union of:
- the user-requested packages, and
- any top-level `requires` / `build_requires` declared in the active defaults file(s).

This second seed is what allows a defaults file to trigger provider loading (see [Triggering providers from a defaults file](#triggering-providers-from-a-defaults-file) below).

1. Walk the dependency graph from the seeded list.
2. When a package with `provides_repository: true` is encountered for the first time, clone its source repository into the cache and add the checkout to `BITS_PATH`.
3. Restart the walk — recipes newly visible on the extended path (including further providers) are now reachable.
4. Repeat until stable (no new providers found) or until `MAX_PROVIDER_ITERATIONS` (20) is reached.

This naturally handles **nested providers**: a provider whose own recipe repository contains a further provider recipe.

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

The provider's recipe (`myorg-recipes.sh`) must be findable on the existing `BITS_PATH` at the time Phase 2 starts — i.e., it should live in the primary config directory or be provided by a Phase 1 always-on provider. Once cloned, its recipes are visible to all subsequent dependency resolution.

> **Important — provider packages only.** The `requires` field in a defaults file is consumed exclusively by the Phase 2 provider scan. It does **not** add the listed packages as regular build dependencies. Because every non-defaults package automatically receives a `defaults-release` build dependency inside `getPackageList`, allowing defaults' own `requires` to propagate into the build graph would create an unresolvable cycle (`defaults-release → provider-pkg → defaults-release`). To prevent this, bits strips `requires` and `build_requires` from the `defaults-release` spec before the dependency-following step in `getPackageList`. The provider repositories are already loaded and their recipes are on `BITS_PATH` by this point, so nothing is lost.

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

**Staleness detection:** On every run after the first, bits refreshes the provider's git mirror (even when `--no-fetch` is active) so that tag advances in the upstream repository are always detected. This ensures that a team-wide recipe update published as a new tag is picked up on the next build without any manual cache purge.

### Effect on build hashes

The commit hash of every provider whose recipes are used is stored in `spec["recipe_provider_hash"]` for each package sourced from that provider. `storeHashes` in `build.py` folds this value into the package's content-addressable build hash, so upgrading a provider (new commit) automatically triggers a rebuild of all packages sourced from it.

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
tox          # runs the full matrix defined in tox.ini (Linux)
tox -e darwin  # reduced matrix for macOS
```

### Test file overview

| Test file | What it covers |
|-----------|---------------|
| `test_args.py` | CLI argument parsing (legacy tests) |
| `test_new_args.py` | New CLI arguments: `bits cleanup` subparser, `--cvmfs-prefix`, `--no-relocate`; backward-compatibility assertions |
| `test_cleanup.py` | `bits_helpers/cleanup.py`: sentinel paths, LRU eviction, age-based eviction, disk-pressure mode, flock concurrency safety |
| `test_container_workdir.py` | `container_workDir` / `cachedTarball` path rewriting logic in `build.py`; all four flag combinations; `re.escape()` correctness for paths with regex metacharacters |
| `test_always_on_providers.py` | `_read_bits_rc`, `_parse_provider_url`, `_make_bits_providers_spec`, `load_always_on_providers` (BITS_PROVIDERS path, `always_load` scan, double-clone prevention, failure isolation) |
| `test_defaults_requires_provider.py` | `parseDefaults` propagating top-level `requires`; defaults-provider seed construction; provider discovery seeded from defaults requires; backward compatibility |
| `test_build.py` | `doBuild` integration, hash computation, build script generation |
| `test_clean.py` | Stale-artifact detection and removal |
| `test_cmd.py` | `DockerRunner` and subprocess helpers |
| `test_deps.py` | Dependency graph generation |
| `test_git.py` | Git SCM wrapper |
| `test_pkg_to_shell_id.py` | `pkg_to_shell_id` sanitisation (dots, dashes, `@`, `+`); `generate_initdotsh` export correctness for dot-in-package-name |
| `test_provider_staleness.py` | Mirror always refreshed when cache exists; upstream tag advances detected; `fetch_repos=False` respected on first run |
| `test_qualify_arch.py` | `compute_combined_arch`: legacy `qualify_arch` and new per-default `append_arch`; end-to-end through `effective_arch`, install path, and `init.sh` generation |
| `test_repo_provider.py` | Repository provider: `getConfigPaths` absolute paths, `_add_to_bits_path`, `clone_or_update_provider` caching, iterative discovery, nested providers, hash propagation |
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
- All tests must pass (`tox` on Linux, `tox -e darwin` on macOS) before a PR is merged.
- The main development branch is `main`; do not target `stable` or release branches directly.

### Code style

- Follow the code style enforced by `.flake8` and `.pylintrc`; run both before pushing.
- Write docstrings for all new public functions and classes.
- Prefer small, focused commits; each commit should leave the test suite green.

### Which document to update

| What changed | Update |
|---|---|
| Installation, quick start, configuration, `bits enter/load/clean` usage | `docs/USERGUIDE.md` |
| Practical how-to examples for common tasks | `docs/COOKBOOK.md` |
| CLI flags, recipe YAML fields, environment variables, architecture/store/Docker internals | `docs/REFERENCE.md` (this file) |
| End-to-end development-to-CVMFS workflow | `docs/WORKFLOWS.md` |
| Planned features, design decisions, known limitations | `docs/ROADMAP.md` |

When a change affects the public CLI (new flag, renamed option, changed default), also update the relevant entry in §16 Command-Line Reference and the short description in README.md.

### License

The project is licensed under the terms in `LICENSE.md`.

---

# Part II — Technical Reference

## 16. Command-Line Reference

All sub-commands are accessed through the unified `bits` entry point:

```
bits [--config=FILE] [--debug|-d] [--dry-run|-n] <subcommand> [options]
```

| Global option | Description |
|---------------|-------------|
| `--config=FILE` | Use the specified configuration file |
| `-d`, `--debug` | Enable verbose debug output |
| `-n`, `--dry-run` | Print what would happen without executing |

---

### bits build

Build one or more packages and all their dependencies.

```bash
bits build [options] PACKAGE [PACKAGE ...]
```

| Option | Description |
|--------|-------------|
| `--defaults PROFILE` | Defaults profile(s); use `::` to combine (e.g. `release::myproject`). Default: `release`. |
| `--flavour NAME[=VALUE]` | Set a build-wide flavour variable (repeatable, comma-separated). `NAME`→`true`, `NAME=VALUE`→`VALUE`, `!NAME`→`false`. Gates `(?NAME)` conditional requires/sources/patches and is exported into the build environment; overrides a defaults `variables:` entry of the same name. See [Flavours](#flavours). |
| `--reuse-from PATH\|cvmfs` | Reuse deployed components via their published modulefiles at this absolute modules-tree path (distinct from `--remote-store`, which is the tarball store). The literal `cvmfs` resolves the location from the defaults `system:` layout (`module_dir`/`cvmfs_dir`) or the `cvmfs_modules_template`. A trailing `::relaxed`/`::strict` also sets the reuse policy (e.g. `cvmfs::relaxed`). See [Reusing deployed components](#relaxed-cvmfs-reuse). |
| `--reuse-policy {strict,relaxed}` | How a reused (`--reuse-from`) component is matched. `strict` (default): reuse only on an exact content-hash match; the result is publishable. `relaxed`: reuse any version present in the one-release overlay, for fast local dev on top of e.g. an LCG release — only the top of the stack is built. Relaxed builds are *loose-provenance* and are refused by the publish path. Falls back to the defaults `reuse_policy:` value. |
| `--build-local PKG[,PKG…]` | Packages to always build locally even when they could be reused (e.g. one you need patched), instead of taking them from `--reuse-from`. |
| `-a ARCH`, `--architecture ARCH` | Target architecture. Default: auto-detected, or the `architecture:` template from defaults (see [§9](#9-architecture-overview)). An explicit value here overrides the template. |
| `--force-unknown-architecture` | Proceed even if architecture is unrecognised. |
| `-j N`, `--jobs N` | Parallel compilation jobs per package. Default: CPU count. |
| `--no-auto-patch` | Do not apply recipe `patches:` automatically for any package in this build. The patch files are still staged in `$SOURCEDIR` and exported as `$PATCH0..$PATCH_COUNT`, but each recipe must apply its own patches (e.g. via the `bits_apply_patches` helper). Default: patches are auto-applied. A single recipe can opt out with `auto_patch: false` in its header; a defaults profile can opt out with `auto_patch: false`. See [Controlling patch application](#controlling-patch-application). |
| `--parallel [N]` | Packages to build simultaneously using the built-in Python scheduler. Bare `--parallel` uses 4; omit it for serial (the default). With N>1, each build's `$JOBS` is divided across the builders (the CPU/load budget, see [Memory- and load-aware parallelism](#memory-aware-parallelism)) so the concurrent jobs together do not oversubscribe the machine. (`--builders` is a kept alias.) |
| `--unleash-final` / `--no-unleash-final` | The final (top-level) package depends on every other package, so it is always scheduled last and builds **alone**. With unleashing on (the default for `--builders > 1`), it uses the full `-j` instead of the per-builder share, since nothing else is running; the `mem_per_job` cap still applies (now against the full free RAM). Pass `--no-unleash-final` to keep it on the per-builder share. Falls back to `build_unleash_final:` under the defaults `system:` block when unset. No effect for `--builders 1`. |
| `--legacy-initdotsh` / `--initdotsh-from-modules` | How each build's **dependency environment** is set up. The default (`--initdotsh-from-modules`) derives it from the dependencies' modulefiles — the single source of truth for runtime *and* development — so recipes need not hand-reconstruct `PYTHONPATH`/include dirs. `--legacy-initdotsh` uses the legacy build-time `init.sh` instead. **HASHED**: the default folds `BITS_INITDOTSH_FROM_MODULES` into every package hash (a distinct, reproducible identity); legacy folds in nothing, so its hashes are byte-identical to the pre-modules default and bits can still reuse **alidist** tarballs. Legacy is also selectable with `BITS_LEGACY_INITDOTSH=1` in the environment — the aliBuild compatibility wrapper sets it. |
| `--critical-path-schedule` / `--no-critical-path-schedule` | Order ready `--builders` jobs by their **critical-path weight** — the longest path, weighted by recorded build times, from each job to the final target — so the build's long pole starts as early as its dependencies allow (Ninja-style scheduling). Weights come from a previous run's `bits_build_stats.json`; with no history the weight reduces to graph depth. **On by default**; `--no-critical-path-schedule` falls back to registration-order dispatch. Falls back to `build_critical_path_schedule:` under the defaults `system:` block when unset. Affects dispatch order only — never what is built or any hash. |
| `--build-nice` / `--no-build-nice` | Stagger the concurrent `--builders` jobs across OS scheduling priority so CPU contention degrades gracefully: at any moment one build runs at top priority (full speed) and the others are progressively backed off, with the freed top slot taken over as builds finish. Native builds are wrapped in `nice -n N`; `--docker`/podman builds get `docker run --cpu-shares=W` (each builder is a separate container/cgroup, so the host ranks the build *containers* by cgroup CPU weight). Memory is still capped separately via `mem_per_job`. **On by default** for `--builders > 1`; pass `--no-build-nice` to disable. |
| `--build-nice-step N` | Nice increment between concurrent build slots when `--build-nice` is set: slot *k* → nice `min(k×N, 19)`. `N=1` gives a gentle `0,1,2,3` ladder; larger values separate the slots more aggressively. Default: 5. |
| `--build-nice-boost-after SECONDS` | With `--build-nice`, a watchdog boosts a long-running straggler compile — one at a time — so a single heavy Fortran/C++ translation unit does not drag out the end of the build. Native builds: the longest-running niced-down build subtree is reniced toward 0 (requires privilege — root / `CAP_SYS_NICE` — and is a logged no-op otherwise). `--docker`/podman builds: each build runs in a named container (`bits-build-<pkg>-<id>`); the watchdog peeks inside with `docker exec … ps`, finds a compiler back-end (cc1plus/f951/…) that has been running longer than this, and renices it with `docker exec --user 0 … renice` (run as root inside the container, so it can raise priority). **Requires `ps` (the `procps` package) in the build image** — see note below. `0` disables. Default: 600. |
| `--prefetch-workers N` | Spawn *N* background threads to fetch remote tarballs and source archives ahead of the main build loop, so downloads overlap with compilation instead of blocking the serial preparation pass. Default: `-1` (auto — scales with `--builders`, capped at 4); `0` disables. No effect without `--remote-store`. |
| `--parallel-downloads N` | Maximum concurrent source/tarball downloads the `--builders` scheduler runs as standalone download tasks (so a checkout overlaps the previous package's build). Default: 2. |
| `--auto-resources` | Opt-in measurement-driven scheduling for `--builders > 1`: auto-load the per-package CPU/RAM stats a previous run recorded (re-stamped for this machine) and enable monitoring to refresh them, so the scheduler only admits a new build when the machine still has budget. Off by default (concurrency is then bounded purely by `--builders`); explicit `--resources`/`--resource-monitoring` still take precedence. |
| `--brew` | **macOS only.** Let a recipe that sources a system library from Homebrew run `brew install <formula>` on demand (during dependency resolution) when the formula is missing. Without it, such a recipe fails with a message naming the formula to install. Exported to recipe `prefer_system_check` scripts as `BITS_BREW=1`. See [macOS Homebrew system layer](#macos-homebrew-system-layer). |
| `--parallel-sources N` | Download up to *N* `sources:` URLs concurrently within a single package checkout. Default: 1 (sequential). |
| `-e KEY=VALUE` | Extra environment variable binding (repeatable). |
| `-z PREFIX`, `--devel-prefix PREFIX` | Version prefix for development packages. |
| `-u`, `--fetch-repos` | Fetch/update source mirrors before building. |
| `--no-local PACKAGE` | Do not use a local checkout for PACKAGE (repeatable). |
| `-w DIR`, `--work-dir DIR` | Work/output directory. Default: `sw`. |
| `--config-dir DIR` | Directory containing recipe files. |
| `--reference-sources DIR` | Local mirror of git repositories. |
| `--remote-store URL` | Binary store to fetch pre-built tarballs from. Append `::rw` to also upload to it. |
| `--write-store URL` | Binary store to upload built tarballs to. |
| `--s3-endpoint URL` | S3 endpoint for `b3://` stores. Overrides `$S3_ENDPOINT_URL` / `$AWS_ENDPOINT_URL_S3`; default `https://s3.cern.ch`. Set for a **non-CERN** bucket (AWS, MinIO, Ceph RGW). |
| `--s3-access-key KEY` | S3 access key id. Overrides `$AWS_ACCESS_KEY_ID` (prefer the env var — a CI/CD variable or gitlab-runner `environment` entry — so the secret is not on the command line). |
| `--s3-secret-key KEY` | S3 secret access key. Overrides `$AWS_SECRET_ACCESS_KEY`. |
| `--s3-region REGION` | S3 region. Overrides `$AWS_DEFAULT_REGION`. |
| `--s3-addressing-style {auto,path,virtual}` | S3 addressing style for `b3://` stores. MinIO usually needs `path`. Overrides `$S3_ADDRESSING_STYLE`. |
| `--disable PACKAGE` | Skip PACKAGE entirely (repeatable). |
| `--prefer-system` | Always prefer system packages where supported. (`--always-prefer-system` is a kept alias.) |
| `--no-system` | Never use system-installed packages. |
| `--check-system-packages` | Check system packages without building. |
| `--docker` | Build inside a Docker container. |
| `--docker-image IMAGE` | Docker image to use. Implies `--docker`. |
| `--docker-extra-args ARGS` | Extra arguments for `docker run`. |
| `--cvmfs-prefix PATH` | Bind-mount the workDir at `PATH` inside the container so packages compile with their final CVMFS paths embedded. Requires `--docker`. See [§22.1 Recipe Sandbox](#221-recipe-sandbox). |
| `--container-use-workdir` | Mount the workDir at the same absolute path inside the container. Mutually exclusive with `--cvmfs-prefix`. |
| `--docker-platform PLATFORM` | Docker `--platform` for cross-compilation (e.g. `linux/arm64`). Inferred automatically from `--architecture`; pass `native` to suppress. Requires QEMU binfmt handlers. See [§22.2 Cross-compilation via QEMU](#222-cross-compilation-via-qemu). |
| `--sandbox MODE` | Sandbox recipe builds: `auto` (default), `podman`, `sandbox-exec` (macOS), or `off`. See [§22.1 Recipe Sandbox](#221-recipe-sandbox). |
| `--sandbox-image IMAGE` | Container image for `--sandbox=podman` when not using `--docker`. |
| `--force` | Rebuild even if the package hash already exists. |
| `--keep-tmp` | Keep temporary build directories after success. |
| `--resource-monitoring` | Enable per-package CPU/memory monitoring. **Default: on when `--builders` > 1**, off for serial builds. |
| `--no-resource-monitoring` | Disable per-package monitoring even when `--builders` > 1. |
| `--resources FILE` | JSON resource-utilisation file for scheduling. |
| `--check-checksums` | Warn on source/patch checksum mismatch; continue the build. |
| `--enforce-checksums` | Abort on source/patch checksum mismatch or missing checksum. |
| `--print-checksums` | Print checksums for all sources/patches in YAML format after the build. |
| `--write-checksums` | Write or update `checksums/<package>.checksum` after the build. |
| `--store-integrity` | Record and verify SHA-256 of every recalled tarball. Can also be set with `store_integrity = true` in `bits.rc`. See [§21 Store integrity verification](#store-integrity-verification). |
| `--provider-policy POLICY` | Control `BITS_PATH` insertion order for repository providers. Format: `name:prepend\|append` pairs. See [§13 Provider policy](#provider-policy). |
| `--from-manifest FILE` | Replay a build from a manifest JSON file; verifies each tarball against `tarball_sha256`. See [§25 Build Manifest](#25-build-manifest). |

The three `--*-checksums` flags are mutually exclusive. Precedence (highest → lowest): `--print-checksums` > `--enforce-checksums` > `--check-checksums` > `checksum_mode:` in defaults profile > per-recipe `enforce_checksums: true` > `off`. `--write-checksums` is independent and can be combined with any of the above. See [§18 — Checksum policy in defaults profiles](#checksum-policy-in-defaults-profiles).

**Build-image requirement — `procps` (for `--build-nice` straggler boosting under `--docker`).** When `--build-nice` (on by default) boosts a long-running straggler compile in a `--docker`/podman build, it locates the offending process by running `ps` *inside* the build container (`docker exec … ps`). This requires the `ps` utility — provided by the **`procps`** package (`procps-ng` on RPM distros) — to be installed in the build image. If `ps` is not present, bits prints a one-time warning and disables in-container straggler renicing for the run; the build itself is unaffected. Build images intended for `bits build --docker` should therefore include `procps`. (Native, non-`--docker` builds use `psutil` on the host instead and do not need `ps` in any image.)

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
4. the built-in default (CERN S3, public read store on supported architectures).

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

`runner_installer.sh` installs `python3-boto3` automatically on both Ubuntu and
AlmaLinux hosts.

---

### bits deps

Generate a visual dependency graph for a package (requires Graphviz).

```bash
bits deps [options] PACKAGE
```

| Option | Description |
|--------|-------------|
| `--outgraph FILE` | Output PDF file (required). |
| `--defaults PROFILE` | Defaults profile(s); use `::` to combine (e.g. `release::myproject`). Default: `release`. |
| `-a ARCH` | Architecture for dependency resolution. |
| `--disable PACKAGE` | Exclude PACKAGE from the graph (repeatable). |
| `--prefer-system` | Mark system-provided packages differently. |
| `--no-system` | Treat all packages as needing to be built. |

Colour coding in the generated graph: **gold** = requested top-level package; **green** = runtime-only dependency; **purple** = build-only dependency; **tomato** = both runtime and build dependency.

---

### bits doctor

Check that the system satisfies all requirements for the requested packages, validate the full build-runner environment with `--runner`, or probe the remote binary store with `--check-store`.

```bash
bits doctor [options] [PACKAGE ...]          # recipe system-requirement check
bits doctor --runner [options]               # runner environment validation
bits doctor --check-store PACKAGE ...        # pre-build store availability report
```

**Recipe-check mode** (default) evaluates each package's `system_requirement` and `prefer_system` snippets in the dependency tree and reports which packages can be satisfied by the host and which will be built by bits. The `PACKAGE` positional argument is required in this mode.

**`--runner` mode** skips the recipe scan and instead runs a structured checklist of the build-runner environment. Each check returns PASS / FAIL / WARN / SKIP. WARN is advisory; only FAIL affects the exit code.

| Check performed | When included |
|-----------------|---------------|
| `git` on PATH | always |
| C++ compiler (`c++`, `g++`, or `clang++`) | always |
| Docker daemon reachable | `--docker` or `--runner` |
| QEMU binfmt handler for the target architecture | when `--docker` is set |
| podman availability and user-namespace support | always |
| CVMFS repository path(s) accessible and non-empty | `--cvmfs-repos` / `bits.rc cvmfs_repos` |
| Free disk space in `--work-dir` ≥ `--min-disk` GiB | always |
| Remote store reachable and credentials present | when `--remote-store` is configured |

**`--check-store` mode** runs the standard dependency-tree resolution (same as recipe-check mode), computes the expected tarball hash for each package bits would need to build, and probes the remote store to report which are pre-built. The report is informational: exit code is always 0. Use it to estimate how much of a build will compile vs. be downloaded.

Hash computation notes:

- For tagged releases (the common CI case) the hash is exact — the tag string deterministically identifies the commit.
- For branch builds without `--fetch-repos`, the commit hash is approximated with "0". If the store probe shows FAIL for all packages in a branch build, re-run via `bits status --fetch-repos --check-store` for accurate hashes.
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
| `--check-store` | off | Probe the remote store for each package bits would build. Requires `--remote-store`. Always exits 0. |
| `--runner` | off | Validate the full build-runner environment instead of checking package recipes. |
| `--json` | off | Emit a machine-readable JSON report (works with `--runner` and `--check-store`). |
| `--cvmfs-repos PATH` | _(none)_ | CVMFS mount path to check (repeatable, `--runner` mode only). Can also be set as `cvmfs_repos = /cvmfs/a,/cvmfs/b` in `bits.rc`. |
| `--min-disk GIB` | `10.0` | Minimum free disk in `--work-dir` (`--runner` mode). Lower triggers WARN, not FAIL. |
| `-a ARCH`, `--architecture ARCH` | auto-detected | Target architecture. |
| `--defaults PROFILE` | `release` | Defaults profile for dependency resolution. |
| `-w DIR`, `--work-dir DIR` | `sw` | Work directory checked for disk space (`--runner`). |
| `--docker` | off | Run recipe checks inside a Docker container (also enables docker-daemon and QEMU checks in `--runner`). |
| `--remote-store URL` | _(none)_ | Remote binary store URL; checked for reachability in `--runner` mode and probed per-package in `--check-store` mode. |
| `--insecure` | off | Skip TLS certificate validation when probing an `https://` store. |

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

**Example — runner health check (JSON, for bits-console):**
```bash
bits doctor --runner --json \
    --cvmfs-repos /cvmfs/alice.cern.ch \
    --remote-store https://s3.cern.ch/swift/v1/alibuild-repo
```

**bits.rc keys relevant to `bits doctor`:**

| Key | Description |
|-----|-------------|
| `prerequisites_url` | URL shown when the C++ compiler or git is missing. Defaults to the ALICE prerequisite guide. |
| `cvmfs_repos` | Comma-separated list of CVMFS paths checked in `--runner` mode (e.g. `/cvmfs/alice.cern.ch,/cvmfs/sft.cern.ch`). |

---

### bits status

Show what `bits build` would do for each package in the dependency tree, without building anything. Each package is classified into one of the states below. Git refs are read from the local mirror cache; packages whose refs have not been cached yet are reported as `hash_unknown`. Pass `--fetch-repos` to populate the cache on first use.

```bash
bits status [options] PACKAGE [PACKAGE...]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--defaults PROFILE` | `release` | Defaults profile(s); use `::` to combine. |
| `-a ARCH`, `--architecture ARCH` | detected | Target architecture. |
| `-w DIR`, `--work-dir DIR` | `sw` | bits work directory to inspect. |
| `-c DIR`, `--config DIR` | `alidist` | Recipe directory. |
| `--reference-sources DIR` | `<workDir>/MIRROR` | Mirror directory for git ref cache. |
| `--no-local PACKAGE` | _(none)_ | Exclude a package from local-checkout detection. May be repeated. |
| `--force-tracked` | off | Ignore all local checkouts. |
| `--disable PACKAGE` | _(none)_ | Exclude a package from the dependency tree. |
| `--force-rebuild PACKAGE` | _(none)_ | Report the named package as needing a rebuild regardless of its hash. |
| `-u`, `--fetch-repos` | off | Clone / fetch reference repos to populate the git ref cache before computing hashes. Requires network access. |
| `--remote-store URL` | _(none)_ | Remote binary store URL. Only consulted when `--check-store` is given. |
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
| `hash_unknown` | Git refs unavailable (mirror not yet populated); re-run with `--fetch-repos`. |

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
| `--cvmfs-root PATH` | _(none)_ | CVMFS tarball store root to search first. |
| `-w DIR`, `--work-dir DIR` | `sw` | Local bits work directory containing the `TARS/` store. |
| `--no-providers` | off | Skip provider checkout commit verification. |
| `--json` | off | Emit a machine-readable JSON report. |

**Exit codes:** 0 = consistent; 1 = FAIL (hash/commit mismatch); 2 = MISS (tarball not found); 3 = manifest unreadable.

---

### bits stats

Summarise the resource data recorded when a build ran with `--resource-monitoring`
(on by default for `--builders > 1`). Reads `<work-dir>/LOGS/<arch>/bits_build_stats.json`
(per-package peaks; written per-architecture so concurrent builds of different
platforms in one work area don't clobber each other) and the per-package traces
under `SPECS/` (for average CPU and thread counts). When the architecture isn't
specified, `bits stats` reads the most recent `LOGS/*/bits_build_stats.json`.

**CPU-utilisation tuning hint.** At the end of a `--builders` run, bits estimates
the whole-run CPU utilisation (useful core-seconds ÷ cores × wall-clock) and the
average number of builders busy at once, and writes them under a `"tuning"` key
in `bits_build_stats.json` together with a `recommendation`. When there is
headroom (utilisation below ~90%) the recommendation is also printed at the end
of the build: if the builder slots were mostly full it suggests a higher
`--oversubscribe` (which raises each builder's `-j` without changing the memory
budget); if the slots were often empty it points at the dependency graph and
suggests more `--builders` and/or reusing prebuilt tarballs.

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
  `mem_per_job` on the recipe so the `--builders` scheduler reserves for it.

---

### bits compliance

Audit recipe licence metadata and the binary store; optionally enforce.
Read-only by default; exit `0` = clean, `1` = issues found, so it can gate CI.

```bash
bits compliance [--recipes DIR] [--store URL] [--no-store-check]
bits compliance --enforce [--dry-run] [--key PEM]     # admin
```

| Option | Default | Description |
|--------|---------|-------------|
| `--recipes DIR` | `.` | Recipe repository to audit (a directory of `*.sh` recipes). |
| `--store URL` | CERN test store | S3 store to audit against the recipe flags (`https`, `b3://`, `s3://`). |
| `--no-store-check` | off | Audit the recipes only. |
| `--enforce` | off | ADMIN: purge non-compliant packages from the store (see below). |
| `--dry-run` | off | With `--enforce`: print every action, touch nothing. |
| `--key PEM` | _(none)_ | With `--enforce`: re-sign the affected platforms after the purge. |
| `-w DIR`, `--work-dir DIR` | `sw` | Scratch for the store client. |

The **audit** reports: recipes missing a `license:` field, unverified
`LicenseRef-*` ids, `NOASSERTION` system shims, and the binaries-restricted /
sources-restricted sets (`redistributable:` — see §9). The **store walk**
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

`bits init` has two distinct modes selected by whether a PACKAGE name is given.

#### Clone mode — create a writable source checkout (legacy / unchanged)

```bash
bits init [options] PACKAGE[@VERSION][,PACKAGE[@VERSION]...]
```

Clones the upstream source repository for each named package into a writable local directory. After `bits init`, the created directory is automatically used as the source for subsequent `bits build` invocations of that package.

| Option | Description |
|--------|-------------|
| `--dist REPO@TAG` | Recipe repository. Default: `alisw/alidist@master`. |
| `-z PREFIX`, `--devel-prefix PREFIX` | Directory for development checkouts. |
| `--reference-sources DIR` | Mirror directory to speed up cloning. |
| `-a ARCH` | Architecture. |
| `--defaults PROFILE` | Defaults profile(s); use `::` to combine (e.g. `release::myproject`). Default: `release`. |

#### Config mode — write persistent settings to bits.rc

When **no PACKAGE** is given, `bits init` writes the supplied options to a `bits.rc` file and exits. All subsequent `bits` invocations in that directory (or globally, if written to `~/.bitsrc`) will use those settings as defaults without requiring them to be repeated on every command line. Explicit CLI flags always take precedence over bits.rc values.

```bash
# Persist a remote binary store for the current project
bits init --remote-store https://store.example.com/store

# Persist both a read store and a write store
bits init --remote-store https://store.example.com/store \
          --write-store b3://mybucket/store

# Record the organisation and update (not replace) the existing bits.rc
bits init --organisation ALICE --append

# Preview what would be written without touching the file
bits init --dry-run --remote-store https://store.example.com/store

# Write to a specific file (default is bits.rc in the current directory)
bits init --rc-file ~/.bitsrc --remote-store https://store.example.com/store
```

| Config option | bits.rc key | Description |
|---------------|-------------|-------------|
| `--remote-store URL` | `remote_store` | Binary store to fetch pre-built tarballs from. |
| `--write-store URL` | `write_store` | Binary store to upload newly-built tarballs to. |
| `--providers URL` | `providers` | URL of the bits-providers repository (overrides `BITS_PROVIDERS`). |
| `--organisation NAME` | `organisation` | Organisation selecting the registry/provider "home" repo. Also settable via the `BITS_ORGANISATION` environment variable (the `aliBuild` wrapper sets it). |
| `-w DIR`, `--work-dir DIR` | `work_dir` | Default work/output directory (overrides `BITS_WORK_DIR`). |
| `-a ARCH`, `--architecture ARCH` | `architecture` | Default target architecture. |
| `--defaults PROFILE` | `defaults` | Default profile(s), `::` separated. |
| `-c DIR`, `--config-dir DIR` | `config_dir` | Default recipe directory. |
| `--reference-sources DIR` | `reference_sources` | Default mirror directory. |
| `--rc-file FILE` | — | Destination file. Default: `bits.rc` in the current directory. |
| `--append` | — | Merge new settings into the existing file rather than replacing it. |

**Search order for bits.rc.** Bits searches for persistent configuration in the following locations (highest priority first): `bits.rc`, `.bitsrc`, `~/.bitsrc`. The first file found is used. Only the `[bits]` INI section is read.

**Example `bits.rc` created by config mode:**

```ini
[bits]
remote_store = https://store.example.com/store
write_store  = b3://mybucket/store
work_dir     = /opt/sw
organisation = MYORG
```

> **Format note.** `bits.rc` may be a flat `key = value` file or a single `[bits]` INI section — a header-less file is read as the `[bits]` section. The old per-organisation `[NAME]` sections and the keys `sw_dir`, `repo_dir`, `pkg_prefix`, and `branding` are no longer accepted: `bits` detects such a file, prints the required renames (`sw_dir`→`work_dir`, `repo_dir`→`config_dir`), and exits. `search_path` **is** still supported — it seeds `BITS_PATH` (comma-separated relative names resolve to `<config_dir>/<name>.bits`), which is required so that building a single package whose recipe lives in a sub-repo (e.g. `bits build ROOT` where `ROOT` is in `./lcg.bits`) finds it; an explicit `BITS_PATH` environment variable wins. Display prefix and branding (`BITS_PKG_PREFIX`, `BITS_BRANDING`) are environment concerns the `aliBuild` wrapper sets automatically.

---

### bits clean

Remove stale build artifacts from the temporary build area.

```bash
bits clean [options]
```

| Option | Description |
|--------|-------------|
| `-w DIR`, `--work-dir DIR` | Work directory to clean. Default: `sw`. |
| `-a ARCH` | Restrict to this architecture. |
| `--aggressive-cleanup` | Also remove source mirrors and `TARS/` content. |
| `-n`, `--dry-run` | Show what would be removed without deleting. |

---

### bits cleanup

Evict packages from a **persistent workDir** based on last-use age and/or available disk space. Intended for shared CI build caches where packages accumulate over time. See [§7 bits cleanup](#bits-cleanup--evict-packages-from-a-persistent-workdir) for full details.

```bash
bits cleanup [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-w DIR`, `--work-dir DIR` | `sw` | workDir to manage. |
| `-a ARCH`, `--architecture ARCH` | auto-detected | Architecture to evict packages for. |
| `--max-age DAYS` | `7.0` | Evict packages not touched in more than `DAYS` days. Set to `0` to disable age-based eviction. |
| `--min-free GIB` | _(none)_ | Evict LRU packages until `GIB` GiB are free on the workDir filesystem. |
| `--disk-pressure-only` | — | Run only the disk-pressure pass; skip age-based eviction. |
| `-n`, `--dry-run` | — | Show what would be evicted without removing anything. |

---

### bits enter

Spawn a new interactive sub-shell with one or more modules loaded. Exit the sub-shell with `exit` to return to the original environment.

```bash
bits enter [--shellrc] [--dev] MODULE1[,MODULE2,...]
```

| Option | Description |
|--------|-------------|
| `--shellrc` | Source the user's shell startup file (`.bashrc`, `.zshrc`, etc.) in the new shell. Suppressed by default to avoid environment conflicts. |
| `--dev` | Source `etc/profile.d/init.sh` from each package directly instead of using `modulecmd`. Development use only. Appends `(dev)` to the shell prompt. |

The shell type is auto-detected from the parent process (`bash`, `zsh`, `ksh`, `csh`/`tcsh`, `sh`). Override with the `MODULES_SHELL` environment variable. The prompt is set to `[MODULE_LIST] \w $>` (or the zsh/ksh equivalent) for the duration of the session. Nesting `bits enter` inside another bits environment is blocked.

---

### bits load / printenv

Print the shell commands to load one or more modules. Must be `eval`'d to take effect, or used via `bits shell-helper`.

```bash
eval "$(bits load [-q] MODULE1[,MODULE2,...])"
```

`-q` suppresses the informational message on stderr. `printenv` is an alias for `load`. The modules directory is refreshed and the module is verified to exist before printing. `--dev` mode prints manual `source` commands to stderr instead (eval of dev mode is unsupported).

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
bits setenv MODULE1[,MODULE2,...] -c COMMAND [ARGS...]
```

Everything after `-c` is executed as-is. The modules directory is refreshed and modules are verified before execution.

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

`bits q` lists modules in the native `PKG/VERSION` form. When a display prefix is set in the environment (`BITS_PKG_PREFIX`, e.g. via the `aliBuild` wrapper) the output is reformatted to `PREFIX@PKG::VERSION` (so `aliBuild q` prints `VO_ALICE@zstd::1.5.7-local1`). The optional `REGEXP` is a case-insensitive extended regular expression. `bits q` reads the installed modulefiles straight from the work tree (reusing the fast CVMFS catalog path where applicable) — it does **not** rebuild the MODULES cache or spawn `modulecmd`, so it stays fast even with hundreds of packages. `bits avail` delegates directly to `modulecmd bash avail` (and does refresh the cache).

**Fast listing on CVMFS.** Enumerating the install tree per file is expensive on
CVMFS (every directory test is a FUSE lookup). When the tree is served from
`/cvmfs` the refresh first tries the `bitsModules` helper, which reads the
serving catalog's content hash from the cvmfs `user.catalog_counters` xattr,
fetches that one catalog object over HTTP, and lists every entry from a single
local SQLite query — no per-file walk. It applies only when the queried path is
served by a single dedicated catalog rooted there with no deeper nested
catalogs; otherwise (and always off CVMFS) it falls back transparently to the
POSIX `find` walk, so behaviour is unchanged.

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
bits version        # print the bits version string and detected architecture
bits architecture   # print only the architecture string (e.g. ubuntu2204_x86-64)
```

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
│   └── <family>/<package>/…       ← same layout when package_family is set
│
├── shared/                        ← architecture-independent packages
│   └── <package>/<version>-<revision>/
│
├── BUILD/                         ← temporary per-package build trees
│   └── <pkghash>/
│       ├── BUILD/                 ← $BUILDDIR during compilation
│       ├── SOURCES/               ← source checkout ($SOURCEDIR)
│       └── log                    ← build log (kept on failure; removed on success)
│
├── TARS/                          ← content-addressed tarball store
│   └── <arch>/
│       ├── store/<h2>/<hash>/*.tar.gz
│       ├── <package>/<tarball> -> ../../store/…   ← by-name symlinks
│       └── dist/, dist-direct/, dist-runtime/     ← dependency-set symlinks
│
├── SOURCES/cache/                 ← downloaded source archives (sources: field)
│   └── <h2>/<hash>/<filename>
│
├── REPOS/                         ← cached repository-provider checkouts
│   └── <provider>/<commit>/       ← recipe files live here
│
├── MODULES/                       ← modulefiles for bits enter / bits q
│   └── <arch>/
│
├── SPECS/                         ← generated build scripts
│   └── <arch>/<package>/<version>/
│
├── MANIFESTS/                     ← build manifests (see §25)
│   ├── bits-manifest-<timestamp>.json
│   └── bits-manifest-latest.json  ← symlink to most recent
│
└── STORE_CHECKSUMS/               ← integrity ledger (opt-in, see §21)
    └── TARS/<arch>/store/…/<tarball>.sha256
```

`BUILD/` directories are removed after a successful build unless `--keep-tmp` is given. Use `bits clean` to remove stale `BUILD/` and `TMP/` trees, or `bits cleanup` to evict old packages from `<arch>/` and `TARS/` based on age or disk pressure.

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

### YAML header fields

#### Identity

| Field | Required | Description |
|-------|----------|-------------|
| `package` | Yes | Package name. Must match the filename (without `.sh`). |
| `version` | Yes | Version string. May contain `%(year)s`, `%(month)s`, `%(day)s`, `%(hour)s` substitutions. |

#### Source

| Field | Description |
|-------|-------------|
| `source` | Git or Sapling repository URL. The repository is cloned / updated into `$SOURCEDIR`. |
| `tag` | Tag, branch, or commit to check out. Supports date substitutions (`%(year)s`, `%(month)s`, `%(day)s`, `%(hour)s`). |
| `sources` | List of source archive URLs (or local `file://` paths) to download before the build. Each file is placed in `$SOURCEDIR` and exposed as `$SOURCE0`, `$SOURCE1`, … Each entry may optionally carry an inline checksum (see [Checksum verification](#checksum-verification) below). |
| `patches` | List of patch file names to apply, relative to the `patches/` directory inside the recipe repository. Patch files are copied to `$SOURCEDIR` and exposed as `$PATCH0`, `$PATCH1`, … before the recipe body runs. Each entry may optionally carry an inline checksum and/or a conditional matcher — see [Conditional patches](#conditional-patches). |
| `auto_patch` | Whether bits applies the `patches:` automatically. Default `true` (unchanged behaviour). Set to `false` to take over patching in the recipe body: bits still stages the patch files in `$SOURCEDIR` and exports `$PATCH0..$PATCH_COUNT`, but runs no `patch(1)` and writes no `.bits_patched` sentinel, so the recipe owns ordering, strip level and idempotency. Can also be forced off for **every** package with the global `--no-auto-patch` flag or `auto_patch: false` in the active `defaults-*` file. See [Controlling patch application](#controlling-patch-application). |

Metadata / publish-policy fields — all **hash-excluded** (editing them never
rebuilds anything; see "Licence compliance and redistribution policy" in §9):

| Field | Description |
|-------|-------------|
| `license` | SPDX licence identifier (`LicenseRef-*` for custom licences; `NOASSERTION` for system shims). Recorded in the build manifest, the publish BOM and the signed manifest; feeds the per-package and per-release `NOTICE` files and the `bits compliance` audit. |
| `acknowledgment` | Attribution text required by the licence; written into the per-package `NOTICE`. |
| `redistributable` | Which forms may be redistributed: `all` (default), `binaries`, `sources`, `none`. Restricted binaries are never uploaded to the store nor published to CVMFS; restricted sources are never mirrored to `SOURCES/cache/`. Legacy `true`/`false` = `all`/`none`; unknown values fail closed as `none`. |
| `description`, `url`, `homepage`, `source_url` | Free-text metadata, also hash-excluded. |

**Source archives detail.** When `sources:` is specified, bits downloads each archive to `$SOURCEDIR` using the file's basename as the local filename. Archives are not automatically unpacked — the recipe is responsible for extraction. The variable `$SOURCE_COUNT` holds the total count so scripts can handle a variable-length list:

```yaml
sources:
  - https://example.com/mylib-1.0.tar.gz,sha256:e3b0c...
  - https://example.com/mylib-data-1.0.tar.gz
```

```bash
# Unpack first archive
tar -xzf "$SOURCEDIR/$SOURCE0" -C "$BUILDDIR"
# Optionally unpack subsequent archives
[ "$SOURCE_COUNT" -gt 1 ] && tar -xzf "$SOURCEDIR/$SOURCE1" -C "$BUILDDIR/data"
```

**Patches detail.** Patch file names listed in `patches:` must exist in the `patches/` subdirectory of the recipe repository. They are copied to `$SOURCEDIR` and the corresponding `$PATCHn` variables let the script apply them in order:

```yaml
patches:
  - fix-include-order.patch
  - disable-broken-test.patch,md5:d41d8cd98f00b204e9800998ecf8427e
```

```bash
cd "$SOURCEDIR"
for i in $(seq 0 $(( PATCH_COUNT - 1 ))); do
  eval pf="\$PATCH$i"; patch -p1 < "$SOURCEDIR/$pf"
done
```

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
recipe body) instead of hand-rolling the loop — it applies `$PATCH0..$PATCH_COUNT` in
order and is idempotent across incremental rebuilds:

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
is also accepted on `requires:`/`build_requires:` entries.

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
  `osx_arm64` these are `osx`, `arm64`, and `aarch64` (truthy). So `pkg:(?osx)`
  is an osx-only dependency. Note `pkg:(?!osx)` is a negative-lookahead **regex**
  matched against the architecture string (the non-osx counterpart), *not*
  variable negation — there is no variable-negation atom.

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
| Gates `(?NAME)` requires/sources/patches | yes | no | yes |
| Exported into build shell | no | yes (via `defaults-release`) | yes |
| In the package hash | only when the expanded text lands in a hashed field (`version`/`source`/`patches`, or the body when expansion is opted in) | yes — folded through the `defaults-release` `env` dict | yes (both paths) |
| When evaluated | build-time text substitution, **before** hashing | exported into the shell before the recipe body runs | both |
| Name case | verbatim | verbatim | verbatim |

To use one name as *both* `%(NAME)s` and `$NAME`, pass it as a `--flavour`, or
define it in **both** `variables:` and `env:`.

> **Auto-uppercased shell variables are a separate, per-package mechanism.** For
> every dependency, bits exports `<PKG>_ROOT`, `<PKG>_VERSION`, `<PKG>_REVISION`,
> `<PKG>_HASH`, and `<PKG>_COMMIT`, where `<PKG>` is the package name run through
> `pkg_to_shell_id()` (non-alphanumerics → `_`, then upper-cased): `boost` →
> `$BOOST_ROOT`, `common.bits` → `$COMMON_BITS_ROOT`, `o2.framework` →
> `$O2_FRAMEWORK_ROOT`. The same transform backs `%(root_dir)s` (→ `${<PKG>_ROOT}`).
> This is keyed off the **package name**, not off any `variables`/`env`/`flavour`
> entry — those keep whatever case you write.

#### Dependencies

| Field | Description |
|-------|-------------|
| `requires` | Runtime + build-time dependencies. |
| `build_requires` | Build-time-only dependencies (e.g. `cmake`, `ninja`). |
| `runtime_requires` | Runtime-only dependencies. |
| `untracked_requires` | Runtime-linked dependencies **excluded from this package's identity hash**. Editing one does **not** invalidate or rebuild this package or anything above it — only the dependency itself rebuilds (it is hashed normally). For iterating on a dependency you control without paying a full-stack rebuild. **You are responsible for ABI compatibility**: a reused consumer links the new dependency without recompiling, so an interface-breaking change can produce a broken build. Any build whose closure includes one is recorded `provenance: loose` in `.meta.json` (discoverable; still publishable). The dependency must keep a **stable install label** — set `force_revision:` on it — so its `<pkg>/<version-revision>` path does not move when it changes, or already-built consumers keep linking the previous build (bits warns if it lacks one). |

Each entry in `requires` / `build_requires` is a string in one of these forms:

| Form | Meaning |
|------|---------|
| `name` | Plain dependency. |
| `name:matcher` | Conditional dependency. `matcher` is an architecture regex (`re.match`-ed against the arch, e.g. `(?!osx)` for non-osx, `.*osx.*` for osx-only) or `defaults=<regex>` (matched against the active defaults). |
| `name = version` | Pin the dependency to `version` (sets both its `version` and `tag`). |
| `name = version:matcher` | Version pin that applies only when `matcher` is satisfied. |

Only one version pin per dependency is allowed across the whole graph; conflicting pins (or a pin that arrives after the dependency was already resolved) abort the build. Prefer the defaults `overrides:` block (see [Configuration files](#configuration-files)) for version pinning; the in-recipe `= version` form is for constraints that belong to the consuming package.

#### Environment exported by this package

| Field | Description |
|-------|-------------|
| `env` | Key-value pairs exported when this package is loaded via `modulecmd`. |
| `prepend_path` | Variables to prepend to (e.g. `PATH`, `LD_LIBRARY_PATH`). |
| `append_path` | Variables to append to. |

#### System-package integration

| Field | Description |
|-------|-------------|
| `prefer_system` | Bash snippet; exit 0 to use the system package instead of building. |
| `system_requirement` | Bash snippet; exit non-0 to abort with a missing-package error. |
| `system_requirement_missing` | Error message shown when `system_requirement` fails. |

#### Repository provider

| Field | Description |
|-------|-------------|
| `provides_repository` | Set to `true` to mark this recipe as a repository provider. |
| `tag` | The git ref of the provider repository to clone — a branch, tag, or commit hash. Selects which snapshot of the recipe repository is pulled (falls back to `version`, then the repo's default branch). The resolved commit hash is folded into every dependent's build hash. |
| `always_load` | Set to `true` (alongside `provides_repository: true`) to clone this provider unconditionally at startup, before any dependency-graph traversal. Recipes in the provider's repository are then visible to all packages without requiring an explicit dependency. |
| `repository_position` | `append` (default) or `prepend` — where to insert the cloned directory in `BITS_PATH`. |

The bits-providers repository URL itself accepts an `@<tag>` suffix (`BITS_PROVIDERS` / `--providers`, default branch otherwise), e.g. `https://github.com/bitsorg/bits-providers@LCG_106`. Because providers are cloned before defaults `overrides:` are applied, an `overrides:` entry cannot change which provider snapshot is fetched — use the provider recipe's `tag:` field or the `@<tag>` URL suffix.

#### Memory-aware parallelism

`$JOBS` for each package build is computed by `effective_jobs(requested, spec, builders)` and bounds two axes so that concurrent `--builders` jobs never oversubscribe the machine:

- **CPU / load (all packages).** `$JOBS` is capped at `requested ÷ builders`, so the collective `-j` of all builders stays within the single-builder budget. This applies whether or not the recipe sets `mem_per_job`.
- **Memory (packages that set `mem_per_job`).** The available memory is split across the concurrent builders and divided by the per-job footprint.

The result is `min(requested, requested ÷ builders, floor((available ÷ builders) × utilisation ÷ mem_per_job))`, floored at 1. With `--builders 1` the CPU cap is a no-op and behaviour is unchanged.

The **final (top-level) package** is exempt from the `÷ builders` CPU split: it depends on every other package, so it builds alone once they finish, and dividing its `-j` would needlessly starve the largest compile of the run. It is computed as if `builders = 1` — i.e. the full `-j`, bounded only by the (now full-RAM) `mem_per_job` cap. Controlled by [`--unleash-final` / `--no-unleash-final`](#) and `build_unleash_final:` (default on for `--builders > 1`). `$JOBS` never enters a package hash, so this is wall-time-only build-host policy.

| Field | Description |
|-------|-------------|
| `mem_per_job` | Expected peak RSS per parallel compilation process. Accepts a plain integer (MiB) or a string with a unit suffix: `512`, `"1500"`, `"1.5 GiB"`, `"2 GB"`. When set, bits samples available system memory at the start of the package's build and applies the memory term above. Omitting the field leaves only the CPU/`builders` cap in effect. |
| `mem_utilisation` | Fraction of available memory bits may commit, in the range `0.0`–`1.0`. Default: `0.9`. Only used when `mem_per_job` is also set. |

See also `--build-nice` ([§5 build options](#5-building-packages)) for staggering the *priority* of concurrent builders on top of these caps.

Examples:

```yaml
# LLVM — each clang process can peak at ~2 GiB with LTO
mem_per_job: 2048

# ROOT — template-heavy; be more conservative on shared hosts
mem_per_job: 1500
mem_utilisation: 0.80
```

When `provides_repository: true` is set, the package's `source` URL must point to a git repository containing recipe files. It will be cloned before the main build and its directory added to `BITS_PATH`. Adding `always_load: true` causes the clone to happen unconditionally at startup (Phase 1) rather than only when the package appears in the dependency graph (Phase 2). See [§13](#13-repository-provider-feature) for full details.

#### Build sandbox

| Field | Description |
|-------|-------------|
| `sandbox_network` | Controls outgoing network access when the build script runs inside a sandbox. `on` (default) — network is **blocked**. `off` — network is **allowed** (useful for recipes that `pip install` or `gem install` at build time). Ignored when `--sandbox=off`. See [§22.1 Recipe Sandbox](#221-recipe-sandbox). |

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
| `enforce_checksums` | Set to `true` to make this recipe always verify checksums in `enforce` mode, regardless of the global CLI flag. Equivalent to passing `--enforce-checksums` for this package only. |

Mode precedence (highest wins): `--print-checksums` > `--enforce-checksums` > `enforce_checksums: true` > `--check-checksums` > default (`off`).

| Mode | Behaviour |
|------|-----------|
| `off` (default) | Checksums in the recipe are stored but never evaluated. |
| `warn` | A declared checksum is verified; a mismatch emits a warning and the build continues. |
| `enforce` | A declared checksum is verified and must match; the build aborts on mismatch. If `--enforce-checksums` is active globally, a **missing** checksum also aborts the build. |
| `print` | The actual checksum of every downloaded file is printed to stdout; no verification is performed. Use this to populate recipes with correct checksums for the first time. |

#### External checksum files

As an alternative to embedding checksums inline, a recipe repository may store them in a dedicated sidecar file. This keeps recipes readable and makes automated checksum management simpler.

**File location:** `<recipe-repo>.bits/checksums/<pkgname>.checksum`

The `checksums/` directory is optional. If the file does not exist, bits falls back to any inline comma-suffix values in the recipe.

**File format (YAML):**

```yaml
# checksums/mylib.checksum
# Re-generate with:  bits build --write-checksums mylib

tag: abc123def456abc123def456abc123def456abc1   # pinned commit SHA

sources:
  https://example.com/mylib-1.0.tar.gz: sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
  https://example.com/extra-data.tar.bz2: sha512:cf83e1357eefb8bdf1542850d66d8007d620e4050b5715dc83f4a921d36ce9ce47d0d13c5d85f2b0ff8318d2877eec2f63b931bd47417a81a538327af927da3e

patches:
  fix-endian.patch: sha256:a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3
  add-missing-header.patch: md5:d41d8cd98f00b204e9800998ecf8427e
```

All sections are optional. The `tag` field holds the **pinned git commit SHA** expected after checking out `source:` + `tag:`. This protects against tag movement (force-pushed tags pointing to a different commit). The value is a bare 40-character (SHA-1) or 64-character (SHA-256) hex string without an algorithm prefix.

**Merge semantics — external file wins:** if a URL or patch filename appears in both the checksum file and as an inline comma-suffix in the recipe, the checksum file value takes precedence. This makes the checksum file the single authoritative security artefact while retaining the inline syntax as a convenient fallback for simple cases.

**Generating checksum files:** run `bits build --write-checksums <package>` to download sources, compute checksums, record the checked-out commit SHA, and write (or update) the file automatically. Subsequent builds will pick it up without any further changes to the recipe `.sh` file.

**Commit pin enforcement:** the `tag:` pin is verified using the same `--check-checksums` / `--enforce-checksums` modes as source and patch checksums. A mismatch means the tag has been moved to a different commit since the checksum file was generated.

#### Miscellaneous

| Field | Description |
|-------|-------------|
| `valid_defaults` | List of defaults profiles this recipe is compatible with. |
| `incremental_recipe` | Bash snippet for fast incremental (development) rebuilds. |
| `relocate_paths` | Paths to rewrite when relocating an installation. |
| `variables` | Custom key-value pairs for `%(variable)s` substitution in other fields. |
| `from` | Parent recipe name for recipe inheritance. |
| `architecture` | Set to `shared` to mark a package as architecture-independent (see [§19](#19-architecture-independent-shared-packages)). |

### Build-time environment variables

For the complete reference of all variables injected by bits into each package build script, see [§20 Environment Variables — Recipe build-time variables](#20-environment-variables). The key variables are `$INSTALLROOT`, `$BUILDDIR`, `$SOURCEDIR`, `$JOBS`, `$PKGNAME`, `$PKGHASH`, `$SOURCE0`/`$SOURCEn`, `$PATCH0`/`$PATCHn`, and `${DEP_ROOT}` for each dependency.

---

## 18. Defaults Profiles

A **defaults profile** is a special recipe file named `defaults-<name>.sh` that lives in the recipe repository alongside ordinary package recipes. It is not a buildable package — its Bash body is never executed. Instead, its YAML header carries **global configuration** that is applied across the entire dependency graph before any package is resolved.


### Selecting a profile

The active profile is selected with `--defaults PROFILE`. If the flag is omitted, bits falls back to `release`, loading `defaults-release.sh`.

`defaults-release.sh` occupies a privileged position: every package in the build graph automatically depends on a pseudo-package named `defaults-release`, which is fulfilled by whatever profile(s) are loaded. This is the mechanism that injects the global `env:` block into every package's `init.sh`.


---

### Combining multiple profiles with `::`

Two or more profiles can be combined in a single `--defaults` value using `::` as a separator:

```
bits build --defaults dev::gcc13 MyPackage
```

This loads `defaults-dev.sh` and `defaults-gcc13.sh` (in that order) and deep-merges their YAML headers into a single configuration. The merge follows the same left-to-right rules as specifying separate profiles: scalars from the later file win, lists are concatenated, dicts are recursively merged.

> **Note:** `defaults-release.sh` is **not** automatically prepended when you use `::`. If you want the release baseline plus a project overlay, write `--defaults release::myproject` explicitly.


---

### File syntax

A defaults file is a standard bits recipe file. The YAML header supports a superset of ordinary recipe fields:

```yaml
package: defaults-release          # must match filename (without defaults- prefix)
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
# Bash body is allowed but its output is appended to every package's build
# environment script. In practice this section is almost always empty.
```


---

### YAML fields specific to defaults files

| Field | Description |
|-------|-------------|
| `env` | Key-value pairs exported into every package's `init.sh` (via `defaults-release` auto-dependency). Equivalent to setting the same `env:` in every recipe. |
| `disable` | List of package names to exclude from the dependency graph. |
| `overrides` | Dict keyed by package name or regex. Each value is a YAML fragment merged into that package's spec after it is parsed. Keys are matched case-insensitively as `re.fullmatch` patterns, so regex metacharacters work. |
| `valid_defaults` | Restricts which profiles this recipe is compatible with. Each component of the `::` list is checked independently; bits aborts if any component is absent from the list. |
| `package_family` | Optional install grouping; see [Package families](#package-families) below. |
| `qualify_arch` | Set to `true` to append **all** non-`release` default names to the install architecture string; see [Qualifying the install architecture](#qualifying-the-install-architecture) below. |
| `append_arch` | String value appended to the install architecture string **only for this defaults file**. Unlike `qualify_arch`, which qualifies with every default name in the chain, `append_arch` lets each file opt in independently and choose the exact string to append; see [Selective qualification with append_arch](#selective-qualification-with-append_arch) below. |
| `checksum_mode` | Base checksum verification policy for every build using this profile. Accepted values: `off` (default), `warn`, `enforce`, `print`. Equivalent to passing the corresponding `--*-checksums` flag on every invocation. CLI flags override this setting; see [Checksum policy in defaults profiles](#checksum-policy-in-defaults-profiles) below. |
| `write_checksums` | Set to `true` to automatically write/update `checksums/<pkg>.checksum` files after every build. Equivalent to passing `--write-checksums` on every invocation. The CLI flag overrides this setting. |


---

### Role in the build pipeline

Defaults processing happens in two phases:

**Phase 1 — `readDefaults()` + `parseDefaults()`** runs before package resolution. Bits loads each named profile file, merges their YAML headers into a single `defaultsMeta` dict, optionally overlays an architecture-specific file (e.g. `defaults-slc9_x86-64.sh`), then extracts:

- `disable` — packages to exclude from the build graph entirely.
- `env` — environment variables propagated to every package's `init.sh` (injected via the `defaults-release` pseudo-dependency).
- `overrides` — per-package YAML patches applied after the recipe is parsed (see below).
- `package_family` — optional install grouping (see [Package families](#package-families) below).
- `requires` / `build_requires` — repository providers (packages with `provides_repository: true`) to clone and add to `BITS_PATH` for builds using this profile. These are consumed by the Phase 2 provider scan and are **not** added as regular build dependencies (to avoid a dependency cycle — see [Triggering providers from a defaults file](#triggering-providers-from-a-defaults-file) in §13).

**Phase 2 — per-package application** happens inside `getPackageList()` as each recipe is parsed. The merged `overrides` dict is checked against the package name (case-insensitive regex match); matching entries are merged into the spec with `spec.update(override)`. This means a defaults file can change any recipe field — version, `requires`, `env`, `prefer_system`, etc. — for targeted packages.


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

- Patterns are matched with `fnmatch.fnmatch` — case-sensitive; `*` matches any sequence of characters, `?` matches a single character.
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

- `resolve_pkg_family()` returns `""` for every package.
- `PKGFAMILY` is exported as an empty string.
- `build_template.sh` falls back to the legacy two-segment `PKGPATH`.
- `init.sh` path templates omit the family segment.
- `SPECS/`, `latest` symlinks, and `hashPath` all use the original layout.

An existing recipe repository with no `package_family` key will produce bit-for-bit identical install trees, tarballs, and hashes compared to a build that predates the feature.

---

### Qualifying the install architecture

By default all packages built with any set of defaults land under the same architecture directory (e.g. `sw/slc7_x86-64/`). If you maintain two profiles that are **incompatible with each other** — for example `gcc12` and `gcc13` — builds from one profile will silently overwrite the install tree of the other.

Bits provides two complementary mechanisms to add a qualifying suffix to the architecture string. Both produce a combined string of the form `<raw_arch>-<suffix>`, which is then used for the install tree, tarballs, and `init.sh` generation.

#### How the combined architecture is used

Whichever mechanism is active, the derived string is used consistently for:

- **Install tree** — `sw/<combined_arch>/<package>/<version>-<revision>/`
- **`BITS_ARCH_PREFIX` default** in every `init.sh` — so the environment resolves to the right prefix at runtime
- **`$EFFECTIVE_ARCHITECTURE`** passed to the build script
- **`TARS/<combined_arch>/`** symlink directories and store paths — ensuring tarballs from different defaults combinations do not collide

The original platform architecture (`slc7_x86-64`) is still passed to the build script as **`$ARCHITECTURE`** (used for platform detection such as the macOS `${ARCHITECTURE:0:3}` check) and to system-package preference matching, so build scripts need no changes.

Packages that declare `architecture: shared` (see [§20](#20-architecture-independent-shared-packages)) are **unaffected** by either mechanism: their effective architecture is always `shared` regardless of which defaults are active.

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
append_arch: gcc13            # ← only this file contributes "gcc13"
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

`release` adds nothing because it has no `append_arch`. If `defaults-cuda.sh` also declares `append_arch: cuda`, then `--defaults release::gcc13::cuda` produces `slc7_x86-64-gcc13-cuda` — only the two files that opted in contribute, in chain order.

The value of `append_arch` is used **verbatim** and need not match the filename. This lets you decouple the defaults filename from the suffix token:

```yaml
# defaults-gcc13-lto.sh
package: defaults-gcc13-lto
version: v1
append_arch: gcc13-lto        # ← custom suffix, not derived from the filename
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

If a file named `defaults-<architecture>.sh` exists in the recipe repository (e.g. `defaults-osx_arm64.sh`), bits silently loads it and merges its header on top of the already-merged profile, skipping the `package` key to avoid a name clash. This is the mechanism for per-platform tweaks such as disabling packages that do not build on a particular OS.


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
`<recipe-dir>/macos/Brewfile`) listing every declared formula that applies to
the target architecture. Two ways to install them:

- **Build node (all up front):** `brew bundle --file macos/Brewfile`.
- **Individual user (on demand):** `bits build --brew …`. With `--brew`, a
  recipe's `prefer_system_check` (which runs unsandboxed during dependency
  resolution and sees `BITS_BREW=1`) runs `brew install <formula>` only for a
  formula a package actually being built needs and that is missing.

The build phase itself is sandboxed on macOS (no network), so `HomebrewRecipe`
never installs — it only exposes an installed formula as a bits package by
symlinking its prefix into `$INSTALLROOT` (so `<PKG>_ROOT`, `PKG_CONFIG_PATH`
etc. resolve to the Homebrew tree). `bits doctor` runs `brew bundle check`
against the Brewfile on macOS and reports missing formulae.

The Brewfile is a **derived** artifact (the recipes are the source of truth):
regenerate and commit it whenever a recipe's `homebrew_formula` changes, and use
`bits brew --check` in CI to fail on a stale file.


---

### Merge semantics

When the `::` list contains more than one name (e.g. `--defaults release::alice`), `readDefaults()` processes them left to right and merges their YAML headers using `merge_dicts()`, which performs a deep merge:

- Scalar values: later profile wins.
- Lists: concatenated.
- Dicts: recursively merged.

This lets a project-level profile (`alice`) layer on top of a base profile (`release`) without duplicating common settings. Bits also validates that each component in the `::` list is present in any `valid_defaults` list found in the loaded recipes; it aborts with a clear error message if any component is incompatible.

---

### Forcing or Dropping the Revision Suffix (`force_revision`)

By default every installed package path and tarball filename includes a **revision counter** assigned by bits, e.g. `slc9_amd64/gcc/15.2.1-1`. The trailing `-1` is the revision. For some packages — notably CMS software releases where the version string `CMSSW_13_0_0` is the authoritative label used by downstream infrastructure — this suffix is undesirable. The `force_revision` field lets you pin the revision to a specific value or drop it entirely, **without touching the recipe file**.

`force_revision` is set in a `defaults-*.sh` file, never in a recipe, so different groups can reuse the same recipes while opting in or out independently.

#### Per-package override

```yaml
overrides:
  "cmssw_.*":
    force_revision: ""          # drop the revision suffix entirely
  "special-tool":
    force_revision: "rc1"       # pin to a literal string
```

When the regex matches a package name (case-insensitive), `spec["revision"]` is set to the given value before any counter logic runs.

#### Global fallback

Add a top-level `force_revision:` field to apply to every package not matched by an override:

```yaml
# drops the revision suffix from every package in this defaults profile
force_revision: ""
```

A global value of `~` (YAML null) means "not set" and has no effect.

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

Some packages — calibration databases, reference data files, pure-Python libraries, architecture-neutral scripts — produce identical output regardless of the build platform. Rebuilding them on every architecture wastes time and storage. The `architecture: shared` recipe field tells bits to install such packages into a single, platform-neutral directory tree that all architectures can read.

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
architecture: shared
---
curl -O https://example.com/calib-2024-01.tar.gz
tar -xzf calib-2024-01.tar.gz -C "$INSTALLROOT"
```

No other change to the recipe or to the packages that depend on it is required.

### Install-tree layout

| Package type | Install path |
|---|---|
| Normal | `<work_dir>/<arch>/<pkg>/<version>-<revision>` |
| Shared, no family | `<work_dir>/shared/<pkg>/<version>-<revision>` |
| Shared, with family | `<work_dir>/shared/<family>/<pkg>/<version>-<revision>` |

The `shared/` segment replaces the architecture string throughout: in the install tree, in tarball names (`<pkg>-<version>-<revision>.shared.tar.gz`), and in the remote binary store (`TARS/shared/store/…`).

### `$EFFECTIVE_ARCHITECTURE`

Every build script receives two architecture variables:

- `$ARCHITECTURE` — the real build-host architecture, always present, unchanged.
- `$EFFECTIVE_ARCHITECTURE` — `shared` for shared packages, equal to `$ARCHITECTURE` otherwise.

Use `$EFFECTIVE_ARCHITECTURE` wherever a path should end up in the shared tree. The existing `$ARCHITECTURE` variable is still available for platform-specific logic such as selecting compiler flags.

```bash
# Example: a recipe that installs under the effective arch tree
install -m 644 mydata.db "$INSTALLROOT/share/"
echo "Installing to $EFFECTIVE_ARCHITECTURE tree"
```

### Environment initialisation (`init.sh`)

When a package depends on a shared package, bits generates the corresponding `init.sh` source line with a **literal** path prefix instead of the runtime variable `$BITS_ARCH_PREFIX`. This is intentional: shared packages are never relocated (they contain no compiled binaries), so the literal `shared/` segment is always correct, including in CVMFS deployments.

```bash
# Dependency on an arch-specific package — uses runtime variable:
[ -n "${MYLIB_REVISION}" ] || \
  . "$WORK_DIR/$BITS_ARCH_PREFIX"/mylib/1.0-1/etc/profile.d/init.sh

# Dependency on a shared package — uses literal path:
[ -n "${MY_CALIBRATION_DB_REVISION}" ] || \
  . "$WORK_DIR/shared"/my-calibration-db/2024-01-1/etc/profile.d/init.sh
```

### Hashing and reproducibility

The build hash of a shared package is computed from the same inputs as any other package (recipe text, dependency hashes). Because `architecture` is not directly hashed (it enters only through the dependency tree), a shared package with no compiled dependencies will produce the **same hash on every platform**. This means:

- A shared package built on `slc7_x86-64` can be fetched and reused on `osx_x86-64` or `ubuntu2204_x86-64` without rebuilding.
- Once uploaded to the remote store, it is a single artifact shared by all build platforms.

### Warning: arch-specific dependencies

If a package marked `architecture: shared` depends on a package that is *not* shared (other than `defaults-release`), bits emits a warning at build time:

```
WARNING: Package my-calibration-db declares 'architecture: shared' but depends on
arch-specific package(s): mylib. Its hash may differ across platforms.
```

This is not an error — bits will still build the package — but the hash will vary across platforms (because the arch-specific dependency has a different hash on each platform), negating the cross-platform reuse benefit. In most cases the fix is either to remove the arch-specific dependency or to mark that dependency as shared too.

### Relocation

Relocation (path-rewriting for CVMFS deployment) is **disabled** for shared packages. Shared packages should contain only data, scripts, or pure-Python code; if a shared package were relocated the `shared/` prefix would still be constant anyway. If your package genuinely requires relocation, it should not be marked `architecture: shared`.

### Backward compatibility

The feature is entirely opt-in. A recipe without `architecture: shared` behaves exactly as before — its effective architecture is the build-host architecture string and its install paths are unchanged.

---

## 20. Environment Variables

### Recipe build-time variables

These variables are set automatically inside each package's Bash build script by `build_template.sh` before the recipe body is sourced. They cannot be overridden by the recipe.

#### Core build paths

| Variable | Purpose |
|----------|---------|
| `$INSTALLROOT` | Install all files here (the final installation prefix). Created by bits before the recipe runs. |
| `$BUILDDIR` | Temporary build directory inside `$BUILDROOT`. Created automatically. |
| `$SOURCEDIR` | Checked-out source directory (git) or the directory where archives are downloaded (`sources:`). |
| `$BUILDROOT` | Parent of `$BUILDDIR`; corresponds to `BUILD/<pkghash>/` in the work tree. |
| `$PKGPATH` | Relative path from the work directory to the install root: `<arch>[/<family>]/<pkg>/<version>-<revision>`. |

#### Package identity

| Variable | Purpose |
|----------|---------|
| `$PKGNAME` | Package name as declared in the recipe. |
| `$PKGVERSION` | Package version string. |
| `$PKGREVISION` | Build revision (integer, incremented on each local rebuild). |
| `$PKGHASH` | Unique content-addressable build hash (hex string). |
| `$PKGFAMILY` | Install family (empty string if no family is assigned). |
| `$BUILD_FAMILY` | Full `build_family` string, which may include the defaults combination used. |
| `$ARCHITECTURE` | Real build-host architecture string (e.g. `ubuntu2204_x86-64`). |
| `$EFFECTIVE_ARCHITECTURE` | `shared` for shared packages; equal to `$ARCHITECTURE` otherwise. |
| `$JOBS` | Parallel compilation jobs. Pass to `make -j$JOBS`, `cmake --build --parallel $JOBS`, etc. Already divided across `--builders` and reduced by `mem_per_job` when memory is tight (see [Memory- and load-aware parallelism](#memory-aware-parallelism)). |
| `$COMMIT_HASH` | Git commit SHA checked out for the `source:` field. |
| `$BITS_SCRIPT_DIR` | Absolute path to the bits installation directory. |
| `$INCREMENTAL_BUILD_HASH` | Non-zero when an incremental recipe is in use (development mode). |
| `$DEVEL_PREFIX` | Non-empty for development packages (directory name of the devel source tree). |

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
cd "$SOURCEDIR"
for i in $(seq 0 $(( PATCH_COUNT - 1 ))); do
  eval patch_file="\$PATCH$i"; patch -p1 < "$SOURCEDIR/$patch_file"
done
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
| `$BITS_PROVIDERS` | URL(s) identifying the active provider repository set. |

### Build and configuration variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `BITS_BRANDING` | _(empty)_ | Cosmetic program-name branding; set by the `aliBuild` wrapper. |
| `BITS_ORGANISATION` | _(empty)_ | Organisation selecting the registry/provider "home" repo. Empty by default; the `aliBuild` wrapper sets `ALICE`, or use `--organisation` / `bits.rc`. |
| `BITS_PKG_PREFIX` | _(empty)_ | Display prefix for `bits q`. Empty prints native `PKG/VERSION`; when set (e.g. `VO_ALICE` via `aliBuild`) output becomes `PREFIX@PKG::VERSION`. |
| `BITS_REPO_DIR` | `alidist` | Root directory for recipe repositories. |
| `BITS_WORK_DIR` | `sw` | Output and work directory. |
| `BITS_PATH` | _(empty)_ | Comma-separated list of additional recipe search directories. Absolute paths are used directly; relative names have `.bits` appended and are resolved under `BITS_REPO_DIR`. |
| `BITS_PROVIDERS` | `https://github.com/bitsorg/bits-providers` | URL(s) of the repository provider set to use. Can be set in the environment, in `bits.rc` as `providers = …`, or overridden per-run. The built-in default points to the official bits-providers repository. |

### Environment module variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `MODULES_SHELL` | _(auto-detected)_ | Shell type passed to `modulecmd` and used when spawning a new sub-shell via `bits enter`. Auto-detected from the parent process. Accepted values: `bash`, `zsh`, `ksh`, `csh`, `tcsh`, `sh`. |
| `MODULEPATH` | _(set by bits)_ | Colon-separated list of directories searched by `modulecmd` for modulefiles. Bits prepends `<WORK_DIR>/MODULES/<ARCH>` and preserves any pre-existing entries. |
| `BITSLVL` | `0` | Nesting depth counter incremented each time `bits enter` is called. `bits enter` refuses to proceed if this is already greater than 1, preventing double-nesting. |
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
| `--remote-store URL::rw` | Shorthand: sets both `--remote-store` and `--write-store` to `URL` in a single flag. |
| `--no-remote-store` | Disable the remote store even on architectures where one is enabled by default. |
| `--insecure` | Skip TLS certificate verification for `https://` stores. |

When either `--remote-store` or `--write-store` is given, bits automatically sets `--no-system` to prevent system packages from affecting the build hash.

### Supported backends

| URL scheme | Backend | Read | Write | Authentication |
|------------|---------|:----:|:-----:|----------------|
| `http://` or `https://` | HTTP/HTTPS | ✓ | — | None (public) or TLS; use `--insecure` to skip cert check |
| `s3://BUCKET/PATH` | Amazon S3 via `s3cmd` | ✓ | ✓ | `~/.s3cfg` config file |
| `b3://BUCKET/PATH` | S3-compatible via `boto3` | ✓ | ✓ | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` env vars |
| `rsync://HOST/PATH` or `/local/path` | rsync | ✓ | ✓ | SSH keys (`~/.ssh/`) or filesystem permissions |

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

Pair it with a writable backend (rsync or boto3) for the write side if needed.

#### S3 via `s3cmd` (`s3://`)

Uses the [`s3cmd`](https://s3tools.org/s3cmd) command-line tool. Credentials are read from `~/.s3cfg`. Supports both AWS and S3-compatible services (Ceph, MinIO, etc.) when the endpoint is configured in `~/.s3cfg`.

```bash
bits build --remote-store s3://mybucket/bits-cache \
           --write-store  s3://mybucket/bits-cache ROOT
```

#### S3-compatible via `boto3` (`b3://`)

The preferred S3 backend. Uses the `boto3` Python library for efficient parallel uploads (up to 32 concurrent connections). Authentication is via environment variables:

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

Upload order is designed to avoid partial-artifact races: the main package symlink is written first (reserving the revision number), then all dependency-set symlinks are uploaded in parallel, and the final tarball is written last. A downloader that finds the symlink but not yet the tarball simply waits for the next build cycle.

#### Publishing an existing local build to S3 (`bits publish`)

Building with `--write-store` uploads each package **as it is built**. To push a
store you already built (nothing re-uploads on a cached rebuild), use
`bits publish` — it reads the build manifest and uploads the content tarballs
plus their named symlink objects.

```bash
# credentials from the environment or ~/.bits/s3keys (see above)

# Bulk: upload every package in the latest manifest. This is the default when
# no PACKAGE is given, so bare `bits publish` is the whole-stack push:
bits publish
bits publish --remote-store https://s3.cern.ch/lcgapp-bits-testing  # pick the bucket
bits publish --from-manifest /path/to/bits-manifest-XYZ.json  # a specific manifest

# Single package (from its manifest entry):
bits publish ROOT --to s3 --write-store b3://lcgapp-bits-testing

# Preview without uploading (no credentials/network needed):
bits publish --dry-run
```

`--dry-run` (`-n`) lists exactly what would be uploaded and to which store,
without contacting S3 — handy to check the package set and target before pushing.

`--remote-store` accepts an `https://<host>/<bucket>` URL (from which the boto3
endpoint and path-style addressing are derived), or `b3://<bucket>` / `s3://<bucket>`.
It is the canonical store flag across `publish`, `certify`, `gc`, `store-stats` and
`compliance`; the old `--store` spelling still works but is deprecated and warns.
The default is `$BITS_S3_STORE` if set, else `https://s3.cern.ch/lcgapp-bits-testing`.
`--from-manifest` also uploads the manifest itself under `MANIFESTS/`, so a CI job
can fetch and sign it.

Under the current posture uploading requires only valid S3 keys, and unsigned
manifests are trusted (reuse works without signatures). Signing becomes relevant
only when consumers build with `--require-signed-reuse` (see *Artifact resolution
order*).

#### Signing a manifest on a GitLab runner (no private server)

A GitLab CI runner **dials out** to fetch jobs, so signing needs no
inbound-reachable service. Keep the Ed25519 **private** key in a Protected +
Masked CI/CD variable, restrict the job to **protected** refs (so fork/MR
pipelines can never see it), and have it fetch the uploaded manifest, sign it
with `trust.sign_manifest`, and push the `.sig` back next to it. Consumers verify
with the **public** keys shipped in `bits/keys/`. This works on CERN's shared
runners; if the key must never touch shared infrastructure, register one
dedicated protected runner (it only dials out — no standing server). A ready
template lives in `bits-console/.gitlab/sign-manifest.yml`.

#### rsync / local filesystem

Supports both remote hosts (via SSH) and local paths. Useful for shared NFS or a build server accessible over SSH:

```bash
# Remote via SSH
bits build --remote-store rsync://buildserver.example.com/bits-cache \
           --write-store  rsync://buildserver.example.com/bits-cache ROOT

# Local filesystem path (useful for cross-project caching on the same machine)
bits build --remote-store /shared/bits-cache \
           --write-store  /shared/bits-cache ROOT
```

### Content-addressable tarball layout

Every tarball is named and stored by its build hash. The layout is the same locally (in the `TARS/` work directory) and in the remote store:

```
TARS/
└── <architecture>/
    ├── store/
    │   └── <hash[0:2]>/          ← two-character prefix for directory sharding
    │       └── <hash>/
    │           └── <pkg>-<version>-<revision>.<architecture>.tar.gz
    └── <package>/                 ← convenience symlinks by package name
        ├── <pkg>-<version>-<revision>.<architecture>.tar.gz -> ../../store/…
        └── <pkg>-<version>-<revision>.<architecture>.tar.gz.manifest
```

For packages marked `architecture: shared` (see [§19](#19-architecture-independent-shared-packages)) the architecture segment is replaced with `shared`:

```
TARS/shared/store/<hash[0:2]>/<hash>/<pkg>-<version>-<revision>.shared.tar.gz
```

The hash is a 40-character SHA-1 computed from the recipe text, package name and version, checked-out source commit, all transitive dependency hashes, relocation paths, and hooks. Changing anything in this set produces a different hash and therefore a different cache entry.

### Dependency-set symlink trees

After each successful build, bits creates three symlink trees under `TARS/<arch>/dist/` that group together everything needed to reproduce or run the package:

| Directory | Contents |
|-----------|----------|
| `dist/<pkg>-<ver>-<rev>/` | Full transitive closure — all build and runtime dependencies. |
| `dist-direct/<pkg>-<ver>-<rev>/` | Direct dependencies only (`requires` + `build_requires`). |
| `dist-runtime/<pkg>-<ver>-<rev>/` | Runtime transitive closure (`runtime_requires`). |

Each entry in these trees is a symlink to the corresponding tarball in `store/`. The trees are uploaded to the remote store alongside the tarball so that a downstream consumer can fetch an entire coherent set with a single rsync or S3 prefix listing.

### Build lifecycle with a store

```
bits build --remote-store URL --write-store URL PACKAGE
```

For each package in topological order:

1. **Hash** — Compute the content-addressable hash from recipe, source commit, and dependency hashes.
2. **Fetch** — Ask the remote store for `TARS/<arch>/store/<h2>/<hash>/*.tar.gz`. If found, download it.
3. **Unpack or build** — If a cached tarball was downloaded, unpack it into `$INSTALLROOT` and skip compilation. Otherwise run the full Bash build script.
4. **Pack** — After a successful from-source build, `build_template.sh` compresses `$INSTALLROOT` into a tarball at `TARS/<arch>/store/<h2>/<hash>/<pkg>-<ver>-<rev>.<arch>.tar.gz`.
5. **Upload** — Bits uploads the tarball and the dist symlink trees to the write store. Development builds (revisions starting with `local`) are never uploaded.

### Revision numbering

Within a given hash, bits assigns monotonically increasing integer revisions (`1`, `2`, …). A rebuild of the same recipe and inputs (same hash) gets the next available integer. Development-mode builds (created by `bits init`) use a `local` prefix (`local1`, `local2`, …) and are excluded from upload to prevent polluting the shared cache with unreviewed in-progress builds.

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

All team members building on machines with access to the shared NFS path reuse each other's artifacts automatically.

### Source archive caching

Packages that use the `sources:` key in their recipe (downloadable URL tarballs, distinct from the primary `source:` git repository) are now archived in the remote store in addition to being cached locally. This means bits can rebuild a package even if the upstream server has removed or moved the tarball.

#### How it works

When bits encounters a `sources:` entry it proceeds in three steps:

1. **Local cache hit** — if `SOURCES/cache/<h2>/<hash>/<filename>` already exists on disk, it is used immediately and the remote store is not contacted at all.
2. **Remote store hit** — if the local cache is empty, bits asks the configured backend for the archived copy before contacting the upstream URL. On success the file is placed in the local cache and no upload is required (it is already in the store).
3. **Upstream download + archive** — only when both the local cache and the remote store miss does bits download from the original URL. The freshly downloaded file is then uploaded to the write store so that future builds (and other machines) can benefit from step 2.

#### Remote namespace

Source archives occupy a dedicated namespace inside the same store used for build tarballs:

```
SOURCES/cache/<hash[0:2]>/<hash>/<filename>
```

This mirrors the local `SOURCES/cache/` layout exactly, so the remote path can be derived mechanically from the URL's MD5 checksum (`hash`) and the bare filename. For example:

```
SOURCES/cache/a1/a1b2c3d4.../libfoo-1.2.tar.gz
```

#### Backend support matrix

| Backend | `fetch_source` | `upload_source` | Notes |
|---------|---------------|-----------------|-------|
| `NoRemoteSync` | — | — | No store configured; local cache only. |
| `HttpRemoteSync` | ✓ | — | Read-only; HTTP stores do not support upload. |
| `RsyncRemoteSync` | ✓ | ✓ | Uses `rsync -vW`; skipped if `--write-store` is absent. |
| `S3RemoteSync` | ✓ | ✓ | Uses `s3cmd get/put`; skipped if `--write-store` is absent. |
| `Boto3RemoteSync` | ✓ | ✓ | Native boto3 API; skips upload if the key already exists. |

#### Enabling source archive caching

No extra flags are needed. Source caching is activated automatically whenever a remote store is configured:

```bash
# Build ROOT; source tarballs fetched via sources: are archived to S3.
bits build --remote-store b3://mybucket/bits-cache::rw ROOT
```

If `--remote-store` is set but `--write-store` is not (or the backend is HTTP/CVMFS), bits will still try to fetch source archives from the store but will silently skip uploading — the same behaviour as for build tarballs.

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

Persistent opt-in via `bits.rc` (recommended for teams that have adopted the feature):

```ini
[bits]
store_integrity = true
```

Accepted values for the config key: `true`, `1`, `yes` (case-insensitive).

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

Bits automatically mounts the work directory, the recipe directories, and `~/.ssh` (for authenticated git operations) into the container. The `DockerRunner` class in `bits_helpers/cmd.py` manages container lifecycle and cleanup.

### workDir mount point inside the container

By default the workDir is bind-mounted at `/container/bits/sw` inside the container, so that the container-internal paths do not collide with the host paths. Two flags change this behaviour:

| Flag | Effect |
|------|--------|
| `--container-use-workdir` | Mount the workDir at the same path as on the host (i.e. `container_workDir = workDir`). Useful when the host and container share the same filesystem. |
| `--cvmfs-prefix PATH` | Mount the workDir at `PATH` inside the container. Packages then compile with `PATH` embedded in all install-time paths. |

### No-relocation builds with `--cvmfs-prefix`

In a conventional CVMFS publishing workflow the package is first compiled with the bits workDir as its install prefix (e.g. `/data/alice/sw/slc9_x86-64/ROOT/6.32.0-1`), and then `relocate-me.sh` rewrites every embedded path to the final CVMFS location (e.g. `/cvmfs/sft.cern.ch/lcg/releases/ROOT/6.32.0`). Relocation is a post-build transformation that can be expensive for packages with many compiled files.

`--cvmfs-prefix` eliminates this step entirely: by mounting the workDir at the final CVMFS prefix inside the container, the compiler sees that path as `$INSTALLROOT` and embeds it directly. The package is already at its deployment-ready paths when the build finishes.

> **Note.** In the normal bits-console workflow these commands are run by the CI pipeline on a registered build runner — not typed by the user. bits-console passes `cvmfs_prefix` from the community's `ui-config.yaml` to the pipeline, which then calls `bits build --docker --cvmfs-prefix …` and `bits publish --no-relocate` automatically. The flags are documented here for CI pipeline authors and runner administrators.

```bash
# These commands run inside the bits-console-triggered CI pipeline on the build runner.
# Pipeline stage 1 — build with deployment paths embedded at compile time:
bits build --docker \
           --cvmfs-prefix /cvmfs/sft.cern.ch/lcg/releases \
           ROOT

# Pipeline stage 1 (continued) — upload to spool; no relocation needed:
bits publish ROOT \
           --cvmfs-target /cvmfs/sft.cern.ch/lcg/releases/ROOT/6.32.0 \
           --spool ingestuser@ingest.example.com:/var/spool/cvmfs-ingest \
           --no-relocate
```

**Persistent workDir across CI jobs.** For communities that publish to CVMFS regularly, keeping the workDir alive between CI jobs (on a persistent build runner) turns `--cvmfs-prefix` into an incremental cache: only packages whose recipe or source changed are rebuilt; already-installed dependencies are reused from the previous run. The `bits cleanup` subcommand manages the cache size over time (see [§7 bits cleanup](#bits-cleanup--evict-packages-from-a-persistent-workdir)).

---

## §22.1 Recipe Sandbox

Bits can run each recipe build script inside an isolated sandbox to limit the damage a malicious or buggy recipe can do. The sandbox wraps the actual `bash build.sh` execution — it does not affect source downloads, tarball extraction, or publishing.

### How it works

| Platform | Default sandbox | Mechanism |
|----------|-----------------|-----------|
| Linux (local build, no `--docker`) | `off` | podman is **not** used (or even probed) for plain local builds |
| macOS (local build) | `sandbox-exec` if available, otherwise `off` | Built-in SBPL sandbox profile; no VM, no overhead |
| Any platform, `--docker` active | Nested podman inside the container, if available | `podman run` launched from inside the Docker build container |

> **Note.** On a local Linux build without `--docker`, `--sandbox=auto` resolves to `off` and bits never invokes `podman` (not even `podman info`). podman-based recipe isolation on Linux is only engaged when the build runs inside `--docker`, or when it is requested explicitly with `--sandbox=podman` / `--sandbox-image`.

The workDir is bind-mounted at the same absolute path inside the podman container so that all paths embedded in the build environment (`$WORK_DIR`, `$INSTALLROOT`, `$SOURCEDIR`, etc.) resolve correctly.

### Sandbox modes

Pass `--sandbox MODE` to `bits build`:

| Mode | Behaviour |
|------|-----------|
| `auto` | (default) Pick the best available option: `sandbox-exec` on macOS, nested podman when `--docker` is active, and `off` on a local Linux build (no `--docker`). On local Linux, podman is neither used nor probed — request it explicitly with `--sandbox=podman` if you want it. |
| `podman` | Always use podman. Requires the podman binary to be reachable and `podman info` to succeed. When used without `--docker`, also requires `--sandbox-image` to name the container image. |
| `sandbox-exec` | macOS only. Fails with an error on Linux. |
| `off` | No sandboxing. Recipe runs directly on the host (same as the behaviour before this feature was added). |

```bash
# Let bits choose (the default)
bits build ROOT

# Force podman with a specific image (no --docker required)
bits build --sandbox=podman --sandbox-image alisw/slc9-builder:latest ROOT

# Disable sandboxing explicitly
bits build --sandbox=off ROOT
```

When `--docker` is used, `--sandbox-image` defaults to the same image as `--docker-image`, so no extra flag is needed:

```bash
# Docker build with nested podman sandbox — same image used for both layers
bits build --docker --docker-image alisw/slc9-builder:latest ROOT
```

### Per-recipe network control

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

The field is silently ignored when `--sandbox=off`.

### Docker-in-Docker (DinD)

If `bits --docker` is invoked from inside an existing Docker container (for example, a GitLab CI job that itself runs inside Docker), adding a nested podman layer is still possible but requires the outer Docker container to have been started with:

```
--security-opt seccomp=unconfined
```

or an equivalent unprivileged user-namespace configuration. Without this, the kernel will reject the `clone(CLONE_NEWUSER)` call that podman uses for rootless containers.

Bits detects this situation automatically (by checking for `/.dockerenv` and `/proc/1/cgroup`) and emits a warning at build time. If the outer container cannot be reconfigured, disable sandboxing for that job with `--sandbox=off`.

---

## §22.2 Cross-compilation via QEMU

Bits supports cross-compilation on any Docker-capable host by combining Docker's
`--platform` flag with QEMU user-mode emulation.  When the target architecture
differs from the host, Docker pulls the matching image variant (e.g. `arm64`)
and uses QEMU to transparently execute the foreign ELF binaries — the build script
sees a native `aarch64` environment without any changes to the recipe.

### One-time host setup

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

### Supported target platforms

| bits `--architecture` substring | Docker `--platform` |
|----------------------------------|---------------------|
| `x86-64` / `x86_64` | `linux/amd64` |
| `aarch64` / `arm64` | `linux/arm64` |
| `ppc64le` | `linux/ppc64le` |
| `s390x` | `linux/s390x` |
| `riscv64` | `linux/riscv64` |

### Automatic platform injection

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

### Builder image availability

The target architecture must have a corresponding builder image variant published
as a multi-arch manifest or a separate tag.  For the CERN experiment ecosystem, the
relevant images are the `alisw/*-builder` series.  Confirm availability before
scheduling cross-compilation CI jobs:

```bash
# Check whether the arm64 variant exists for the slc9 builder
docker manifest inspect registry.cern.ch/alisw/slc9-builder:latest | \
  grep -A2 '"platform"'
```

If only the `x86-64` variant exists, an ARM-native runner (available on CERN's
infrastructure and cheaply on cloud spot markets) is the practical alternative for
full-stack cross-compilation.

### Architecture matching for batch jobs

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

`bits fetch` verifies the manifest's `architecture` field against the executing
node before unpacking anything and aborts with a clear diagnostic on mismatch.

### Performance expectations

QEMU user-mode emulation runs at roughly 20–50 % of native execution speed for
compute-heavy C++ compilation.  This is acceptable for small analysis packages
(seconds to minutes per package) but impractical for large stacks such as ROOT or
Geant4 (builds would take 10–20 hours).  The recommended scope for QEMU
cross-compilation is:

- Personal analysis overlays (M6 workflow): a few packages, tens of MB of output.
- Validation builds: confirming that a recipe compiles clean on a target
  architecture before scheduling a native-runner CI job for the full stack.

For full experiment stacks on non-x86-64 architectures, use a native runner of
the target architecture.

### Sandbox interaction

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

1. The tarball is located in the content-addressed store under `TARS/<arch>/store/<hash[:2]>/<hash>/<tarball>`.
2. Its SHA-256 is recomputed and compared to `tarball_sha256` in the manifest.
3. Packages with `outcome: already_installed` and no recorded tarball are silently marked **SKIP** — no output tarball is expected for them.

**Providers** — for each entry in `manifest.providers[]`:

1. If the `checkout_dir` does not exist on the current machine, the entry is **SKIP** (provider checkouts are usually only present on build hosts).
2. Otherwise, `git rev-parse HEAD` is run in the checkout and the result is compared to the manifest's `commit` field.

**Architecture** — the `architecture` field in the manifest is compared to the
current host architecture (via `detectArch()`).  A mismatch is a **FAIL** and
counts toward the exit code.

### Search order

Tarballs are searched in this order:

1. `--cvmfs-root PATH` (if given) — typically the CVMFS mount point.
2. `--work-dir DIR` (default: `sw`) — the local bits work directory.

The first root where the content-addressed tarball file exists is used.  This
allows verifying a deployment that spans both CVMFS (for the common stack) and
a local overlay (for personal analysis packages).

### Output formats

**Human-readable (default)**

```
━━━ bits verify  —  bits-manifest-2026-01-15.json ━━━━━━━━━━━━━━━━━━━

  File:       /builds/bits-manifest-2026-01-15.json
  Schema:     v2
  Created:    2026-01-15T08:42:11Z
  Build:      success

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
  "manifest_status": "success",
  "schema_version": 2,
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
| 3 | The manifest file cannot be read or is malformed. |

### Status values

| Status | Meaning |
|--------|---------|
| **PASS** | Entry verified successfully. |
| **FAIL** | Checksum or commit mismatch — the deployed artifact differs from the build record. |
| **MISS** | Tarball not found in any search root — cannot confirm consistency. |
| **SKIP** | Entry not verifiable on this machine (already-installed packages, absent provider checkouts). |

### CLI reference {#cli-reference-verify}

| Flag | Default | Description |
|------|---------|-------------|
| `--from-manifest FILE` | _(required)_ | Path to the bits build manifest JSON file. |
| `--cvmfs-root PATH` | _(none)_ | Root of a CVMFS tarball store to search first (e.g. `/cvmfs/alice.cern.ch`). |
| `-w / --work-dir DIR` | `sw` | Local bits work directory containing the `TARS/` store. |
| `--no-providers` | off | Skip verification of provider checkout commits. |
| `--json` | off | Emit a machine-readable JSON report instead of the human-readable table. |

---

## 24. Design Principles & Limitations

### Principles

1. **Reproducibility** — Stripping the shell environment and pinning exact git commits ensures the same inputs always produce the same build.
2. **Incrementalism** — The content-addressable hash scheme rebuilds only what has changed, keeping iteration fast even on large stacks.
3. **Isolation** — Each package builds in its own directory with a sanitised environment (locale forced to `C`, `BASH_ENV` unset, only declared dependencies visible).
4. **Parallelism** — Both inter-package (via the `Scheduler`) and intra-package (via `$JOBS`) parallelism are supported.
5. **Simplicity** — Build scripts are plain Bash, not a new DSL; the YAML header is metadata only.
6. **Portability** — Runs on any modern Linux distribution and on macOS (Intel and Apple Silicon).
7. **Extensibility** — The repository provider mechanism allows recipe sets to be composed dynamically from versioned git repositories without modifying the main configuration.

### Current limitations

- **No Windows support** — Windows is not supported.
- **Git and Sapling only** — No Subversion, Mercurial, or plain-tarball sources (except via `sources:` with `file://` URLs).
- **Linux and macOS only** — Bits runs on Linux and macOS (Intel and Apple Silicon).
- **Environment Modules required** for `bits enter / load / unload` — the `modulecmd` binary must be installed separately.
- **Active development** — The recipe format and Python APIs may change between versions. Evaluate thoroughly before adopting in production pipelines.

---

## 25. Build Manifest

Every `bits build` run writes a self-contained JSON manifest to the work
directory.  The manifest captures everything bits needs to reproduce the
build at a later date: the requested packages, architecture, defaults
profile, provider checkouts, and the identity (hash + tarball checksum) of
every package that was built or retrieved from the remote store.

```bash
# Build normally — manifest is always written
bits build ROOT

# The manifest file is printed in the success banner, e.g.:
#   Build manifest written to:
#     $WORK_DIR/MANIFESTS/bits-manifest-20260411T143000Z.json
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
| `bits_dist_hash` | Git commit of the bits distribution (= `BITS_DIST_HASH`) |
| `requested_packages` | Packages passed on the command line |
| `architecture` | Combined architecture string (may include defaults suffix) |
| `defaults` | Active defaults profile(s) |
| `config_dir` | Absolute path to the recipe repository (`.bits` checkout) |
| `config_commit` | HEAD commit of the recipe repository at build time |
| `status` | `"in_progress"` → `"complete"` or `"failed"` |

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
| `tarball_sha256` | `sha256:<hex>` digest of the tarball, if present |
| `source_checksums` | List of `{url, checksum}` entries from the recipe's `sources:` list; `checksum` is `null` when none was declared |
| `built_by` | `user@host` that compiled this hash; `null` unless `outcome` is `"built_from_source"` (recalled artifacts carry their builder in another build's manifest) |
| `completed_at` | ISO-8601 UTC timestamp of package completion |

### Manifest location and naming

Manifests are written to a dedicated subdirectory of the bits work directory (`--work-dir`, default `sw`):

```
$WORK_DIR/
  MANIFESTS/
    bits-manifest-20260411T143000Z.json   ← one file per build run (UTC timestamp)
    bits-manifest-latest.json             ← symlink to the most recent manifest
```

Keeping manifests in `MANIFESTS/` prevents them from cluttering the work directory root alongside package install trees.

The manifest is written **incrementally**: after each package completes (or
is confirmed already installed), so a failed build still produces a partial
manifest recording what succeeded.

The `bits-manifest-latest.json` symlink is updated atomically after every
incremental write using `os.replace()` on a temporary symlink, so readers
always see a consistent view.

### Manifest schema reference

```json
{
  "schema_version": 2,
  "bits_version": "1.0.0",
  "bits_dist_hash": "a1b2c3d4e5...",
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

# Override a specific package while replaying the rest:
bits build --from-manifest bits-manifest-20260411T143000Z.json ROOT

# Pin to a specific manifest from the archive:
bits build --from-manifest bits-manifest-20260101T090000Z.json
```

During a replay run bits will:

1. Read `requested_packages`, `architecture`, `defaults`, and `config_commit`
   from the manifest and use them as the effective build parameters.
2. Build the dependency graph as usual, but with versions and hashes pinned
   to the values recorded in the manifest.
3. Verify each recalled tarball's `sha256` against the manifest entry,
   providing end-to-end integrity even for a replay run.

> **Note on `config_commit` pinning:** The replay currently uses the
> `config_commit` field for informational purposes.  To guarantee an exact
> replay you should check out the same commit of the recipe repository before
> invoking `bits build --from-manifest`.

### Manifest and store integrity

The build manifest and the [store integrity ledger](#store-integrity-verification)
are complementary:

- The **ledger** (`STORE_CHECKSUMS/`) guards individual tarballs against
  store-backend tampering during the current build cycle.
- The **manifest** records the complete provenance of a build run and
  enables future replays and audits.

When both `--store-integrity` and a manifest are active, the manifest's
`tarball_sha256` fields provide a second, portable copy of the digest that
survives even if the local ledger directory is deleted.

---

## 26. CVMFS Publishing Pipeline

The CVMFS publishing pipeline — including the `cvmfs-prepub` delivery path, the legacy spool path, the `cvmfs-ingest` Go daemon, and the bits-console web interface — is maintained in the **[bits-console](https://gitlab.cern.ch/bitsorg/bits-console)** repository. That repository contains the GitLab SPA for triggering and monitoring builds, the community `ui-config.yaml` reference, role-based access configuration (production vs personal-area builds), and the pipeline variable reference.

---

*Back to [User Guide](USERGUIDE.md) · [Cookbook](COOKBOOK.md)*
