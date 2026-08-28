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
bits doctor <package>     # reports any missing system requirements for that package
```

Common ones on Debian / Ubuntu (RHEL/AlmaLinux equivalents in parentheses):

```bash
sudo apt install libreadline-dev       # readline           (readline-devel)
sudo apt install libdw-dev libelf-dev  # elfutils / libdw    (elfutils-devel)  e.g. heaptrack
sudo apt install linux-perf            # perf                (perf)            e.g. adaptyst
```

### Installing Bits

```bash
git clone https://github.com/bitsorg/bits.git
cd bits
export PATH=$PWD:$PATH
pip install -e .
```

---

## 3. Quick Start

### ALICE (default community — no configuration needed)

```bash
# In any empty directory: bits auto-bootstraps the ALICE recipe repo
bits doctor ROOT          # check system requirements first
bits build ROOT           # resolves and builds ROOT and all dependencies

bits enter ROOT/latest    # open a sub-shell with the environment loaded
root -b
exit                      # return to your normal shell
```

### Another community (e.g. LHCb) — one-time setup

```bash
# Write community and work-directory to bits.rc once
bits init --organisation LHCB --work-dir /path/to/sw

# Then build as normal — bits auto-bootstraps the LHCb recipe repo
bits build DaVinci
bits enter DaVinci/latest
```

### Inside a cloned recipe repository

```bash
# bits detects defaults-release.sh and uses "." as the recipe directory
git clone https://github.com/bitsorg/lhcb.bits
cd lhcb.bits
bits build DaVinci
```

---

## 4. Configuration

Bits reads an optional INI-style configuration file at startup. Create one with `bits init`:

```bash
bits init --organisation LHCB \
          --work-dir /path/to/sw \
          --remote-store https://s3.cern.ch/swift/v1/mybucket
```

This writes a `bits.rc` file in the current directory. You can also write it by hand (INI format, `[bits]` section):

```ini
[bits]
organisation  = LHCB
work_dir      = /path/to/sw
remote_store  = https://s3.cern.ch/swift/v1/mybucket
```

`organisation` is written **uppercase** (`ALICE`, `LHCB`, …). Bits lowercases it internally when resolving the community recipe repository from bits-providers (e.g. `LHCB` → `lhcb.bits.sh` → `https://github.com/bitsorg/lhcb.bits`).

Bits looks for `bits.rc` in: `--rc-file FILE` → `./bits.rc` → `./.bitsrc` → `~/.bitsrc`.

Commonly used `[bits]` keys:

| Key | CLI flag | Description |
|-----|----------|-------------|
| `organisation` | `--organisation` | Community name (uppercase). Used to auto-bootstrap the recipe repo. |
| `work_dir` | `-w` / `--work-dir` | Output directory for built packages (default: `sw`). |
| `remote_store` | `--remote-store` | Binary store URL for pre-built tarball retrieval. |
| `write_store` | `--write-store` | Binary store URL for uploading newly built tarballs. |
| `store_integrity` | `--store-integrity` | `true` to enable SHA-256 verification of every recalled tarball. |

Settings follow the precedence `CLI flag > environment variable > bits.rc value > built-in default`. For the full list of configuration keys, environment variables, and the organisation-section override mechanism, see [REFERENCE.md §19](REFERENCE.md#19-environment-variables).

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
| `-j N`, `--jobs N` | Parallel compilation jobs per package. Default: CPU count. |
| `--builders N` | Number of packages to build simultaneously. Default: 1 (serial). With N>1 each build's `$JOBS` is divided across the builders (`-j ÷ N`) so the concurrent jobs together stay within one machine's worth of cores. |
| `--build-nice` | Stagger concurrent builders across OS priority levels so CPU contention degrades gracefully — one build runs at full speed, the others are backed off, and the freed top slot is taken over as builds finish. Native builds use `nice`; `--docker` builds use `docker run --cpu-shares`. Opt-in; only affects `--builders > 1`. Memory is still capped separately. |
| `--build-nice-step N` | Priority spread between concurrent build slots for `--build-nice` (slot *k* → nice `min(k×N, 19)`). `N=1` is a gentle ladder; larger separates slots more. Default: 5. |
| `--prefetch-workers N` | Background threads that fetch remote tarballs ahead of the build loop. Default: 0. |
| `-u`, `--fetch-repos` | Update all source mirrors before building. |
| `-w DIR`, `--work-dir DIR` | Work/output directory. Default: `sw`. |
| `--remote-store URL` | Binary store to pull pre-built tarballs from. |
| `--write-store URL` | Binary store to push newly-built tarballs to. |
| `--force` | Rebuild even if the package hash already exists. |
| `--docker` | Build inside a Docker container. |
| `--debug` | Verbose debug output. |
| `--dry-run` | Print what would happen without executing. |
| `--keep-tmp` | Preserve build directories after success (useful for debugging). |

For parallel build modes (`--builders`, `--prefetch-workers`, `--parallel-sources`) and Docker/cross-compilation options, see [REFERENCE.md §5](REFERENCE.md#5-building-packages) and [REFERENCE.md §22](REFERENCE.md#22-docker-support).

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

### bits cleanup — evict packages from a persistent workDir

`bits cleanup` manages a long-lived, shared workDir by evicting packages that have not been used recently or when disk space falls below a threshold. It is intended for **persistent CI build caches** where packages accumulate over time.

```bash
bits cleanup [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-w DIR`, `--work-dir DIR` | `sw` | workDir to manage. |
| `-a ARCH`, `--architecture ARCH` | auto-detected | Architecture to evict packages for. |
| `--max-age DAYS` | `7.0` | Evict packages whose sentinel has not been touched in more than `DAYS` days. Set to `0` to disable age-based eviction. |
| `--min-free GIB` | _(none)_ | Evict the least-recently-used packages until at least `GiB` GiB of free disk space is available. |
| `--disk-pressure-only` | — | Run only the disk-pressure eviction pass; skip age-based eviction. |
| `-n`, `--dry-run` | — | Show which packages would be evicted without removing anything. |

**How it works.** Every time a package is built or confirmed already installed, bits touches a *sentinel file* at `$WORK_DIR/.packages/<arch>/<package>/<version>`. The `cleanup` command reads these sentinels, sorts packages by last-touched time (oldest first), and evicts those that are too old or that need to be removed to recover disk space.

**Typical usage patterns:**

```bash
# Pre-build: free space if below 50 GiB, evicting LRU packages first
bits cleanup --min-free 50 --disk-pressure-only || true

# Nightly cron: evict packages not used in 7 days
bits cleanup --max-age 7

# See what would be removed without touching anything
bits cleanup --max-age 3 --min-free 100 --dry-run
```

---

## Next Steps

- **[Cookbook](COOKBOOK.md)** — practical recipes for common tasks
- **[Reference Manual](REFERENCE.md)** — command-line flags, recipe format, environment variables, Docker, stores, CVMFS pipeline, developer guide
- **[Workflows](WORKFLOWS.md)** — development-to-deployment walkthrough
- **[Roadmap](ROADMAP.md)** — planned features and priorities
