# Bits — User Guide

> **See also:** [Cookbook](COOKBOOK.md) · [Reference Manual](REFERENCE.md) · [Workflows](WORKFLOWS.md) · [Roadmap](ROADMAP.md)

## Table of Contents
1. [Introduction](#1-introduction)
2. [Installation & Prerequisites](#2-installation--prerequisites)
3. [Quick Start](#3-quick-start)
4. [Configuration](#4-configuration)
5. [Building Packages](#5-building-packages)
6. [Managing Environments](#6-managing-environments)
7. [Cleaning Up](#7-cleaning-up)
8. [Publishing, Trust and Release Tasks](#8-publishing-trust-and-release-tasks)

---
## 1. Introduction

**Bits** is a build orchestration and dependency management tool for complex software stacks. It is derived from [aliBuild](https://github.com/alisw/alibuild), the build system developed for the ALICE experiment software at CERN, and is designed for communities that need to build and maintain large collections of interdependent packages with reproducibility, parallelism, and minimal overhead.

> **Acknowledgement.** Bits is a fork of [aliBuild](https://github.com/alisw/alibuild), originally created by the ALICE collaboration at CERN. The recipe format, dependency-resolution model, content-addressable build hashing, remote binary store, and Docker build support all originate from aliBuild. Bits extends aliBuild with the repository provider mechanism, package families, shared packages, extended parallel builds and other features described in this document.

Bits is **not** a traditional package manager like `apt` or `conda`. Instead it automates fetching sources, resolving dependencies, building, and installing software in a controlled, reproducible environment. Each package is described by a *recipe* — a plain-text file with a YAML metadata header and a Bash build script — stored in a version-controlled recipe repository.

Key capabilities at a glance:

- Automatic topological dependency resolution and ordering
- Content-addressable incremental builds — only rebuilds what changed
- Parallel package builds and multi-core compilation
- Remote binary stores (HTTP, S3, CVMFS, rsync) to share pre-built artifacts
- Docker-based builds for cross-compilation or reproducible CI environments
- Git and Sapling SCM support
- Dynamic recipe repositories loaded at dependency-resolution time
- Deterministic package tarballs, signed manifests and passkey-approved certification for trusted binary reuse
- SBOM export (CycloneDX / SPDX) and recipe-wide source checksums

The same `bits build` command that a developer runs interactively also drives the CI pipeline that publishes packages to CVMFS for the entire community — there is no separate local and CI toolchain. The full development-to-deployment workflow is described in [WORKFLOWS.md](WORKFLOWS.md).

---

## 2. Installation & Prerequisites

### System requirements

| Requirement | Notes |
|-------------|-------|
| Linux or macOS | x86-64 or ARM64 |
| Python 3.8+ | Required |
| Git | Required; Sapling (`sl`) is optional |
| `modulecmd` | Required for `bits enter / load / unload` |
| GNU tar (macOS) | Recommended on macOS (`brew install gnu-tar`) — without it bits warns and tarballs may not be byte-reproducible |

Install Environment Modules for your platform:

```bash
# macOS
brew install modules

# Debian / Ubuntu
apt-get install environment-modules

# RHEL / CentOS / AlmaLinux
yum install environment-modules
```

### Installing the build toolchain and system packages

bits builds almost everything it needs from source, but it still relies on a host
toolchain to bootstrap (a working C/C++/Fortran compiler, the autotools/CMake build
tools, `git`, `patch`, `make`, and the usual archive utilities). Install these once
before your first build:

```bash
# Debian / Ubuntu
sudo apt install \
  build-essential gfortran git patch make cmake autoconf automake libtool m4 \
  pkg-config curl wget tar gzip bzip2 xz-utils unzip \
  python3 python3-pip python3-venv environment-modules

# RHEL / AlmaLinux / Rocky (enable CRB/EPEL for some packages)
sudo dnf groupinstall "Development Tools"
sudo dnf install \
  gcc-gfortran git patch make cmake autoconf automake libtool m4 \
  pkgconfig curl wget tar gzip bzip2 xz unzip \
  python3 python3-pip environment-modules

# macOS (Xcode command-line tools provide the compiler / git / make)
xcode-select --install
brew install cmake autoconf automake libtool pkg-config gnu-tar wget modules
```

**Per-recipe system packages.** A few recipes deliberately use a library or tool from
the system instead of building it (these are declared as `system_requirement` recipes,
e.g. `readline`, `elfutils`/`libdw`, `perf`). When such a package is missing, bits stops
early with an explicit install hint rather than failing mid-build. You don't need to
install them all up front — build what you need and follow the hint, or check ahead of
time with:

```bash
bits doctor               # no package: checks this machine (Python modules, git/compiler,
                          # docker or rootless podman, disk space, store access)
bits doctor <package>     # reports any missing system requirements for that package
```

Common ones on Debian / Ubuntu (RHEL/AlmaLinux equivalents in parentheses):

```bash
sudo apt install libreadline-dev       # readline           (readline-devel)
sudo apt install libdw-dev libelf-dev  # elfutils / libdw    (elfutils-devel)  e.g. heaptrack
sudo apt install linux-perf            # perf                (perf)            e.g. adaptyst
```

**macOS: the Homebrew system layer.** On macOS, stable low-level libraries are taken from
Homebrew instead of being built (recipes declare `homebrew_formula:`). Every macOS build
records the formulae declared by the recipes it sees (config dir and providers) in `sw/<arch>/Brewfile`. On the **first** macOS build
that file does not exist yet, so bits writes it and stops with instructions; install the
formulae and re-run:

```bash
bits build ROOT                              # first run: writes sw/<arch>/Brewfile and stops
brew bundle --file sw/<arch>/Brewfile        # install the listed formulae
bits build ROOT                              # now builds

bits build --brew ROOT                       # alternative: install missing formulae on demand
bits brew                                    # write the Brewfile without resolving a build
bits build --dry-run ROOT                    # also writes the Brewfile, nothing built
```

See [REFERENCE.md — macOS Homebrew system layer](REFERENCE.md#macos-homebrew-system-layer).

### Installing Bits

```bash
git clone https://github.com/bitsorg/bits.git
cd bits
export PATH=$PWD:$PATH
pip install -e .
```

---

## 3. Quick Start

### 1. Check out your community's recipe repository

bits always builds from a community recipe repository: a `*.bits` repository with the
community's defaults (`defaults-release.sh`), CVMFS layout and recipes. Clone it first and
run bits inside it — bits uses the current directory as its recipe directory. In a
directory without recipes a build stops.

```bash
git clone https://github.com/bitsorg/stacks.bits && cd stacks.bits
# or resolve it in the bits-providers registry:  bits init stacks.bits && cd stacks.bits
```

Community repositories: `stacks.bits` (LCG-based stacks), `alice.bits`, `atlas.bits`,
`cms.bits`, `key4hep.bits`, `lhcb.bits`, `ship.bits`. They pull shared recipe pools
(`lcg.bits`, `common.bits`, `alidist.bits`) on demand; `bits init <name>.bits` clones a
community repository from the [bits-providers](https://github.com/bitsorg/bits-providers)
registry.

### 2. Check, then build

```bash
bits doctor                             # check that this machine is set up to run bits
bits doctor --defaults gcc15 ROOT       # check ROOT's system requirements
bits build --dry-run --defaults gcc15 ROOT  # optional: what would be reused and what built
bits build --defaults gcc15 ROOT        # resolves and builds ROOT and all dependencies

bits enter ROOT/latest                  # open a sub-shell with the environment loaded
root -b
exit                                    # return to your normal shell
```

For another community the steps are the same, e.g.
`git clone https://github.com/bitsorg/lhcb.bits && cd lhcb.bits && bits build DaVinci`.
Record per-directory options (work directory, stores) once with `bits init --work-dir …`
inside the repository (see [Configuration](#4-configuration)).

### ALICE: the aliBuild workflow

The `aliBuild` wrapper keeps ALICE's classic workflow, working from an `alidist` checkout:

```bash
aliBuild init            # check out alidist
aliBuild build O2
```

---

## 4. Configuration

Record per-directory build settings once with `bits init` (given configuration options and no package), so you do not repeat them on every build:

```bash
bits init --work-dir /path/to/sw \
          --remote-store https://s3.cern.ch/swift/v1/mybucket
```

This writes a `bits use` profile — `./.bitsuse` in the current directory, or a record under `~/.bits/use/` when the directory is not writeable. `--architecture` is saved to the profile's `[common]` section; `--remote-store`, `--write-store`, `--defaults`, `-c/--config-dir`, `-w/--work-dir` and `--reference-sources` are saved to `[build]`. `bits use` records the same kind of profile from any command's flags (e.g. `bits use build --docker`, or `bits use build --store-integrity` to enable SHA-256 verification of every recalled tarball).

```bash
bits use --architecture x86_64-el9-gcc14-opt     # [common]: every arch-aware command
bits use build --parallel 4 --docker             # [build]: bits build only
bits use                                         # show the active profile and where it lives
bits use --clear build                           # clear one section (no SECTION: clear all)
```

Saved arguments are inserted before your own, so an explicit flag on the command line still wins. A local `.bitsuse` is only honoured when it is owned by you; otherwise the `~/.bits/use/` record is used.

> **`bits.rc` is retired.** Earlier versions read `bits.rc` / `.bitsrc` / `~/.bitsrc`; those files are no longer read. Move per-directory settings into a `bits use` profile and global ones into the environment variables below.

Global settings come from environment variables:

| Variable | Related flag | Description |
|----------|--------------|-------------|
| `$BITS_ORGANISATION` | `--organisation` | Community name (uppercase), e.g. `LHCB`. Used only when `-c`/`--config-dir` names a directory that does not exist: bits then clones that community's recipe repository from the registry and uses it. The `aliBuild` wrapper sets `ALICE`. |
| `$BITS_WORK_DIR` | `-w` / `--work-dir` | Output directory for built packages (default: `sw`). |
| `$BITS_REPO_DIR` | `-c` / `--config-dir` | Root directory for recipe repositories. |
| `$BITS_PROVIDERS` | `--providers` | Repository provider set URL(s). |
| `$BITS_PATH` | `--search-path` | Recipe search path. |
| `$BITS_S3_STORE` | `--remote-store` (store ops) | Default S3 store for `bits store` (`gc`/`stats`/`upload`), `certify`, `publish`, `compliance`. |

`$BITS_ORGANISATION` is set **uppercase** (`ALICE`, `LHCB`, …). Bits lowercases it internally when resolving the community recipe repository from bits-providers (e.g. `LHCB` → `lhcb.bits.sh` → `https://github.com/bitsorg/lhcb.bits`). Normally you do not need it: check out the community repository and run bits inside it.

Settings follow the precedence `CLI flag > bits use profile > environment variable > built-in default`. For the full list of environment variables, see [REFERENCE.md §20](REFERENCE.md#20-environment-variables) and [REFERENCE.md — bits init](REFERENCE.md#bits-init).

---

## 5. Building Packages

```bash
bits build [options] PACKAGE [PACKAGE ...]
```

Bits resolves the full transitive dependency graph of each requested package, computes a content-addressable hash for every node, downloads any pre-built artifacts that already exist in a remote store, and builds the rest in topological order.

### How a build proceeds

1. **Recipe discovery** — Bits locates `<package>.sh` in each directory on `search_path` (appending `.bits` to each name). Repository-provider packages (see [§13](REFERENCE.md#13-repository-provider-feature)) are cloned first to extend the search path before the main resolution pass.
2. **Dependency resolution** — `requires`, `build_requires`, and `runtime_requires` fields are read recursively, forming a DAG. Cycles are reported as errors.
3. **Hash computation** — A hash is computed for each package from its recipe text, source commit, dependency hashes, and environment. Packages with a matching hash in a store are downloaded instead of rebuilt.
4. **Source fetching** — Source repositories are cloned into a local mirror and then checked out into a build area. Up to 8 repositories are fetched in parallel.
5. **Build execution** — Each package's Bash script runs in an isolated environment with sanitised locale and only its declared dependencies visible.
6. **Post-build** — A modulefile and a versioned tarball are written; the tarball may be uploaded to a write store.

### Common options

| Option | Description |
|--------|-------------|
| `--defaults PROFILE` | Defaults profile(s) to load. Combines multiple files with `::` (e.g. `--defaults release::myproject`). Default: `release`. |
| `--set NAME[=VALUE]` | Set a build-wide flavour variable (alias of `--flavour`); gates conditional `(?NAME)` requires/sources/patches and overrides a defaults `variables:` entry. |
| `-j N`, `--jobs N` | Parallel compilation jobs per package. Default: CPU count. |
| `--parallel [N]` | Number of packages to build simultaneously. Bare `--parallel` uses 4; omit it for serial (the default). With N>1 each build's `$JOBS` is divided across the builders (`-j ÷ N`) so the concurrent jobs together stay within one machine's worth of cores. (`--builders` is a kept alias.) |
| `--build-nice` | Stagger concurrent builders across OS priority levels so CPU contention degrades gracefully — one build runs at full speed, the others are backed off, and the freed top slot is taken over as builds finish. Native builds use `nice`; `--docker` builds use `docker run --cpu-shares`. Opt-in; only affects `--parallel > 1`. Memory is still capped separately. |
| `--build-nice-step N` | Priority spread between concurrent build slots for `--build-nice` (slot *k* → nice `min(k×N, 19)`). `N=1` is a gentle ladder; larger separates slots more. Default: 5. |
| `--prefetch-workers N` | Background threads that fetch remote tarballs and sources ahead of the build loop. Default: auto (`min(builders, 4)`); `0` disables. |
| `-u`, `--fetch-repos` | Update all source mirrors before building. |
| `-w DIR`, `--work-dir DIR` | Work/output directory. Default: `sw`. |
| `--remote-store URL` | Binary store to pull pre-built tarballs from. Append `::rw` to also upload to it. |
| `--write-store URL` | Binary store to push newly-built tarballs to. Given alone (no `--remote-store`), it is also the store reused from, except where a default remote store applies (slc7/8/9, ubuntu x86-64, slc9_aarch64); use `--remote-store URL::rw` there. |
| `--force-rebuild PKG` | Rebuild these packages from scratch even if they were built before (repeatable or comma-separated). |
| `--prefer-system` | Always use a compatible system package instead of building it (`--always-prefer-system` is the older spelling). `--no-system [PKGS]` does the opposite. |
| `--brew` | macOS only: `brew install` missing Homebrew formulae on demand (see [Installation](#2-installation--prerequisites)). |
| `--docker` | Build inside a Docker container. |
| `-d`, `--debug` | Verbose debug output. |
| `-n`, `--dry-run` | Print a per-package reuse plan without building (see below). |
| `--no-auto-cleanup` | Keep build directories after a successful build (useful for debugging). |

For the full option list, parallel build tuning (`--parallel`, `--oversubscribe`, `--prefetch-workers`, `--parallel-sources`) and Docker/cross-compilation options, see [REFERENCE.md — bits build](REFERENCE.md#bits-build) and [REFERENCE.md §22](REFERENCE.md#22-docker-support). The `--makeflow` / `--pipeline` build modes have been removed; use `--parallel`.

### Building several packages at once

```bash
bits build --parallel ROOT          # 4 packages at a time
bits build --parallel 8 -j 32 ROOT  # 8 builders sharing 32 compile jobs
```

Without `--parallel` packages are built one after another, each with the full `-j`. With N builders each package gets a share of `-j`; the final top-level package, which builds alone, gets the full `-j` again. `--builders N` still works as an alias.

### Previewing a build: `--dry-run`

```bash
bits build --dry-run ROOT
```

Computes every package's hash exactly as the build would and prints, in build order, where each one would come from — **installed**, **local tarball**, **from remote store**, or **build** — followed by a summary. Nothing is downloaded or installed; http(s):// and b3:// stores are only listed. `bits status --check-store` uses the same store listing. On macOS a dry run also writes `sw/<arch>/Brewfile`.

### Sharing binaries through a store

```bash
# Developer: reuse what CI built, never upload
bits build --remote-store https://s3.cern.ch/swift/v1/mybucket ROOT

# CI: reuse from and upload to the same bucket
bits build --remote-store b3://mybucket::rw ROOT
```

Remote reuse is fail-closed: a tarball from a remote store is only reused when a verified signed manifest vouches for it (`--no-require-signed-reuse` turns this off, e.g. to bootstrap a new store). Local and CVMFS artifacts are unaffected. To upload a single package you already built, use `bits store upload PKG`. See [REFERENCE.md §21](REFERENCE.md#21-remote-binary-store-backends).

### Exporting the dependency tree

```bash
bits deps ROOT --outgraph root.pdf        # Graphviz graph
bits deps ROOT --outmake root.mk          # Makefile rules (pkg: dep1 dep2), no Graphviz needed
bits deps ROOT --outmake root.mk --runtime-only   # skip build-only dependencies
```

See [REFERENCE.md — bits deps](REFERENCE.md#bits-deps).

---

## 6. Managing Environments

Bits uses the standard [Environment Modules](https://modules.sourceforge.net/) system (`modulecmd`) to manage runtime environments. A *module* corresponds to one built package version. The `bits` shell script discovers `modulecmd` automatically — on macOS via Homebrew, on Linux via `envml` or `$PATH`. If it cannot be found, it prints the appropriate install command.

### Enter a sub-shell with modules loaded

```bash
bits enter ROOT/latest
# A new sub-shell opens with ROOT and all its dependencies in PATH etc.
exit   # return to your normal shell
```

`bits enter` sets the shell prompt so it is always clear when inside a bits environment. Nesting `bits enter` inside another bits environment is blocked.

| Option | Description |
|--------|-------------|
| `--shellrc` | Source your shell startup file (`.bashrc`, `.zshrc`, etc.) in the new shell. |
| `--dev` | Source each package's `etc/profile.d/init.sh` directly instead of using modulecmd. |
| `--view` | Collapse `PATH`, `LD_LIBRARY_PATH`, `PYTHONPATH`, … onto one merged view of the loaded closure, keeping the environment short on big stacks (also accepted by `bits load`). Entering a package whose recipe sets `view: true` turns it on automatically. |
| `-q` | Silence the module-load messages. |

### Load / unload in the current shell

Add the shell helper to your `.bashrc` or `.zshrc` once so that `bits load` and `bits unload` modify the current shell's environment without requiring an explicit `eval`:

```bash
BITS_WORK_DIR=/path/to/sw
eval "$(bits shell-helper)"
```

Then in any shell session:

```bash
bits load ROOT/latest        # adds ROOT to the current environment
bits unload ROOT             # removes it (version can be omitted)
bits list                    # show currently loaded modules
bits q [REGEXP]              # list available modules, optionally filtered
```

Without `shell-helper`, use `eval` manually:

```bash
eval "$(bits load ROOT/latest)"
eval "$(bits unload ROOT)"
```

### Run a single command in a module environment

```bash
bits setenv ROOT/latest -c root -b
# Everything after -c is executed as-is; the exit code is preserved.
```

`bits setenv` loads the modules into the current process environment and then `exec`s the command — no new shell is spawned.

---

## 7. Cleaning Up

Bits provides two distinct cleaning subcommands for different scenarios.

### bits clean — remove temporary build artifacts

```bash
bits clean [options]
```

| Option | Description |
|--------|-------------|
| `-w DIR` | Work directory to clean. Default: `sw`. |
| `-a ARCH` | Restrict to this architecture. |
| `--aggressive-cleanup` | Also remove source mirrors and `TARS/` content. |
| `-n`, `--dry-run` | Show what would be removed without deleting. |

The default (non-aggressive) clean removes the `TMP/` staging area, stale `BUILD/` directories (those without a `latest` symlink), and stale versioned installation directories. Aggressive cleanup additionally removes source mirrors and `TARS/` content. Use `bits clean` after temporary or experimental builds to reclaim disk space without affecting the persistent package cache.

### bits prune — evict packages from a persistent workDir

> **Renamed to `bits prune`.** `bits cleanup` still works as a deprecated alias that
> warns and forwards; use `bits prune` in new scripts.

`bits prune` manages a long-lived, shared workDir by evicting packages that have not been used recently or when disk space falls below a threshold. It is intended for **persistent CI build caches** where packages accumulate over time.

```bash
bits prune [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-w DIR`, `--work-dir DIR` | `sw` | workDir to manage. |
| `-a ARCH`, `--architecture ARCH` | auto-detected | Architecture to evict packages for. |
| `--max-age DAYS` | `7.0` | Evict packages whose sentinel has not been touched in more than `DAYS` days. Set to `0` to disable age-based eviction. |
| `--min-free GIB` | _(none)_ | Evict the least-recently-used packages until at least `GiB` GiB of free disk space is available. |
| `--disk-pressure-only` | — | Run only the disk-pressure eviction pass; skip age-based eviction. |
| `--retain` | — | Manifest-rooted sweep over all architectures: keep the packages of the newest `--keep-builds N` (default 2) build manifests and anything certified but not yet on CVMFS; evict what is safely upstream (stored, in the verified signed manifest and published to CVMFS). Signed manifests come from `--remote-store` and/or `--trust-manifest`; an architecture whose manifest cannot be verified is skipped. |
| `-n`, `--dry-run` | — | Show which packages would be evicted without removing anything. |

**How it works.** Every time a package is built or confirmed already installed, bits touches a *sentinel file* at `$WORK_DIR/.packages/<arch>/<package>/<version>`. The `prune` command reads these sentinels, sorts packages by last-touched time (oldest first), and evicts those that are too old or that need to be removed to recover disk space.

**Typical usage patterns:**

```bash
# Pre-build: free space if below 50 GiB, evicting LRU packages first
bits prune --min-free 50 --disk-pressure-only || true

# Nightly cron: evict packages not used in 7 days
bits prune --max-age 7

# See what would be removed without touching anything
bits prune --max-age 3 --min-free 100 --dry-run
```

For the full option list see [REFERENCE.md — bits prune](REFERENCE.md#bits-prune). To clean the shared **S3 store** rather than a local workDir, use `bits store gc` (formerly `bits gc`) — see [Confusing command pairs](REFERENCE.md#confusing-command-pairs).

---

## 8. Publishing, Trust and Release Tasks

Short recipes for the commands that act beyond your local build. Most are run by CI or by a group's release manager; each links to the full reference.

### Publish to CVMFS

`bits publish PACKAGE` relocates a built package to its final CVMFS path and hands it to the cvmfs-prepub service; it is CVMFS-only:

```bash
bits publish ROOT --cvmfs-target /cvmfs/sft.cern.ch/lcg/releases/ROOT/6.32.02/x86_64-el9 \
                  --prepub-url https://prepub.example.org:8080
bits publish --release-view LCG_110 --cvmfs-target /cvmfs/sft.cern.ch/lcg   # merged view under Views/ (--view is deprecated)
```

- With no PACKAGE, `bits publish` uploads every package of the latest build manifest to the S3 store (`--manifest FILE` picks another), together with the release's NOTICE and SBOMs. To upload **one** package to the S3 store use `bits store upload PKG`.
- On the ingest path, when a `cvmfs_packages_template` is set, the pipeline's `bits cvmfs publish` sends each package's identity path and hash (a modulefile tar sends only its path), so prepub skips a duplicate that is already queued or published; it is not sent with `--replace-on-conflict`. The same path refuses up front a tarball larger than prepub's advertised `max_tar_size`.
- A group that sets `cvmfs_packages_template` publishes each package once per build architecture; releases are then views over those packages (`cvmfs_releases_template`, and with `cvmfs_views_template` an LCG-style merged view with a self-locating `setup.sh`). Path templates may use `{arch}` and the nightly `{day}` token.
- The producer-side commands are now grouped: `bits cvmfs stage` / `bits cvmfs publish` (formerly `bits cvmfs-stage` / `bits cvmfs-publish`) and `bits store stats` (formerly `bits store-stats`); the old names still work and warn.

See [REFERENCE.md — bits store / bits cvmfs](REFERENCE.md#bits-store--bits-cvmfs-admin--ci-groups) and [REFERENCE.md §26](REFERENCE.md#26-cvmfs-publishing-pipeline).

### Find where a package will land on CVMFS

`bits cvmfs-path` resolves a package's publish path from the group's templates (in `defaults-release.sh` under `system:`) without building:

```bash
bits cvmfs-path --package ROOT --version 6.32.02 --admin --set release=LCG_110
bits cvmfs-path --package ROOT --version 6.32.02 --login jdoe        # personal (user-prefix) path
```

Pass the same `--set` / `--day` values as the build so `{release}` and `{day}` resolve identically; `--kind` selects the `packages`, `releases`, `modules` or `shared` template. See `bits cvmfs-path --help`.

### Make a build trusted: `bits certify` vs `bits sign`

```bash
bits certify            # latest build: upload what is missing, approve with a passkey, open the MR
```

`bits certify` is what a user runs: it uploads anything still missing from the store, asks bits-console for a passkey approval (scan the QR code, compare the code, approve on your phone) and then opens the certification merge request in the manifests repository. `bits sign` — the command formerly called `certify` — is run by the manifests-repo CI to merge the approved build manifests into the signed common manifest. Group, manifests repo and console URL default from the defaults' `system:` block. See [REFERENCE.md — Publishing and certifying a build](REFERENCE.md#publishing-and-certifying-a-build--bits-publish-bits-certify) and [Signing — bits sign](REFERENCE.md#signing--bits-sign).

### Export an SBOM

```bash
bits sbom sw/MANIFESTS/bits-manifest-latest.json                 # sbom.cdx.json + sbom.spdx.json
bits sbom --format spdx -o - sw/MANIFESTS/bits-manifest-latest.json
```

Writes a deterministic CycloneDX 1.6 and/or SPDX 2.3 document from a build manifest (credentials are stripped from source URLs); manifests older than schema v4 give components only, without dependency edges. A whole-build upload (`bits publish` with no package, or `bits certify`) stores both next to the release NOTICE. See [REFERENCE.md — bits sbom](REFERENCE.md#bits-sbom).

### Record source checksums for a recipe repository

```bash
cd myproject.bits
bits checksums                               # report: hash every tarball and patch, resolve git tags
bits checksums --write                       # record new entries in checksums/ (never overwrites)
bits checksums --defaults all --recipes ../lcg.bits   # also the sources its defaults-*.sh profiles override
```

Git tags are pinned to their commit; branches are reported as moving and never pinned. The exit status is 1 on any mismatch. See [REFERENCE.md — bits checksums](REFERENCE.md#bits-checksums).

### Produce an LCG release view

`bits overlay lcg` writes an lcgcmake-style LCG release (the `LCG_externals` manifest and a merged `setup.sh`) over a built closure, optionally with a merged symlink-farm view:

```bash
bits overlay lcg -a x86_64-el9-gcc15-opt --platform x86_64-el9-gcc15-opt \
                 --version-number 110 --out /path/to/releases --build-view /path/to/view
```

`bits lcg-view` is the deprecated former name. See `bits overlay lcg --help`.

---

## Next Steps

- **[Cookbook](COOKBOOK.md)** — practical recipes for common tasks
- **[Reference Manual](REFERENCE.md)** — command-line flags, recipe format, environment variables, Docker, stores, CVMFS pipeline, developer guide
- **[Workflows](WORKFLOWS.md)** — development-to-deployment walkthrough
- **[Roadmap](ROADMAP.md)** — planned features and priorities
