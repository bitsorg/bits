# Bits Build Tool — Reference Manual

## Table of Contents

### Part I — User Guide
1. [Introduction](#1-introduction)
2. [Installation & Prerequisites](#2-installation--prerequisites)
3. [Quick Start](#3-quick-start)
    - [The bits development-to-deployment workflow](WORKFLOWS.md) ↗
4. [Configuration](#4-configuration)
5. [Building Packages](#5-building-packages)
    - [Parallel build modes](#parallel-build-modes)
    - [Async pipeline options](#--pipeline----pipelined-tarball-creation-and-upload-makeflow-only)
6. [Managing Environments](#6-managing-environments)
7. [Cleaning Up](#7-cleaning-up)
    - [bits clean — remove temporary build artifacts](#bits-clean--remove-temporary-build-artifacts)
    - [bits cleanup — evict packages from a persistent workDir](#bits-cleanup--evict-packages-from-a-persistent-workdir)
8. [Cookbook](#8-cookbook)

### Part II — Developer Guide
9. [Architecture Overview](#9-architecture-overview)
10. [Setting Up a Development Environment](#10-setting-up-a-development-environment)
11. [Key Source Files](#11-key-source-files)
12. [Writing Recipes](#12-writing-recipes)
    - [Function-based recipes with bits-recipe-tools](#function-based-recipes-with-bits-recipe-tools)
13. [Repository Provider Feature](#13-repository-provider-feature)
14. [Writing and Running Tests](#14-writing-and-running-tests)
15. [Contributing](#15-contributing)

### Part III — Reference Guide
16. [Command-Line Reference](#16-command-line-reference)
    - [bits build](#bits-build)
    - [bits deps](#bits-deps)
    - [bits doctor](#bits-doctor)
    - [bits status](#bits-status)
    - [bits verify](#bits-verify)
    - [bits init](#bits-init)
    - [bits clean / bits cleanup](#bits-clean)
    - [bits publish](#bits-publish-1)
17. [Recipe Format Reference](#17-recipe-format-reference)
18. [Defaults Profiles](#18-defaults-profiles)
19. [Architecture-Independent (Shared) Packages](#19-architecture-independent-shared-packages)
20. [Environment Variables](#20-environment-variables)
21. [Remote Binary Store Backends](#21-remote-binary-store-backends)
    - [Supported backends](#supported-backends)
    - [Content-addressable tarball layout](#content-addressable-tarball-layout)
    - [Build lifecycle with a store](#build-lifecycle-with-a-store)
    - [CI/CD patterns](#cicd-patterns)
    - [Source archive caching](#source-archive-caching)
    - [Store integrity verification](#store-integrity-verification)
22. [Docker Support](#22-docker-support)
    - [workDir mount point inside the container](#workdir-mount-point-inside-the-container)
    - [No-relocation builds with `--cvmfs-prefix`](#no-relocation-builds-with---cvmfs-prefix)
22a. [Recipe Sandbox](#22a-recipe-sandbox)
22b. [Cross-compilation via QEMU](#22b-cross-compilation-via-qemu)
    - [How it works](#how-it-works)
    - [Sandbox modes](#sandbox-modes)
    - [Per-recipe network control](#per-recipe-network-control)
    - [Docker-in-Docker (DinD)](#docker-in-docker-dind)
22c. [bits verify — Deployment Verification](#22c-bits-verify--deployment-verification)
    - [What is checked](#what-is-checked)
    - [Search order](#search-order)
    - [Output formats](#output-formats)
    - [Exit codes](#exit-codes)
    - [Status values](#status-values)
    - [CLI reference](#cli-reference-verify)
23. [Forcing or Dropping the Revision Suffix (`force_revision`)](#23-forcing-or-dropping-the-revision-suffix-force_revision)
24. [Design Principles & Limitations](#24-design-principles--limitations)
25. [Build Manifest](#25-build-manifest)
    - [What is recorded](#what-is-recorded)
    - [Manifest location and naming](#manifest-location-and-naming)
    - [Manifest schema reference](#manifest-schema-reference)
    - [Replaying a build with `--from-manifest`](#replaying-a-build-with---from-manifest)
26. [CVMFS Publishing Pipeline](#26-cvmfs-publishing-pipeline)
    - [Overview](#overview-1)
    - [bits publish](#bits-publish)
    - [bits-cvmfs-ingest — building from source](#bits-cvmfs-ingest--building-from-source)
    - [bits-cvmfs-ingest — configuration and running](#bits-cvmfs-ingest--configuration-and-running)
    - [cvmfs-publish.sh — the publisher script](#cvmfs-publishsh--the-publisher-script)
    - [CI/CD integration](#cicd-integration-1)
    - [bits-console — web interface for the GitLab-driven pipeline](#bits-console--web-interface-for-the-gitlab-driven-pipeline)

---

# Part I — User Guide

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

### What sets bits apart from other package managers

The key distinction between bits and conventional package managers (apt, conda, Spack, …) is that it operates on a **single, unified recipe language and build system that works identically on a developer's laptop and in CI**. There is no separate "local build tool" and "CI build tool". The exact same `bits build` command that a developer runs interactively also drives the CI pipeline that publishes packages to CVMFS for the entire community.

This has three practical consequences:

**Local development with full-stack context.** A developer can check out a package's source in a local directory, run `bits build`, and have bits automatically build that local version while resolving all other dependencies from the upstream repository. The full software stack is available on the developer's workstation without any manual environment setup.

**"Works on my machine" is meaningful.** Because the build environment — recipe, flags, dependency graph, compiler toolchain — is identical locally and in CI, a package that builds and runs correctly locally will behave the same in CI. There is no hidden discrepancy between local and CI environments.

**A continuous path from edit to CVMFS.** The lifecycle of a change travels along a single, unbroken toolchain: local edit → local build & test → commit → CI build → CVMFS publication. Each step reuses the same recipes, the same binary store, and the same bits commands. The [full development workflow](WORKFLOWS.md) is described in detail in WORKFLOWS.md.

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

### Installing Bits

```bash
git clone https://github.com/bitsorg/bits.git
cd bits
export PATH=$PWD:$PATH
pip install -e .
```

---

## 3. Quick Start

```bash
# 1. Clone bits and at least one recipe repository
git clone https://github.com/bitsorg/bits.git
cd bits && export PATH=$PWD:$PATH && cd ..

git clone https://github.com/bitsorg/alice.bits.git
cd alice.bits

# 2. Check that your system is ready
bits doctor ROOT

# 3. Build a package (all dependencies are resolved and built automatically)
bits build ROOT

# 4. Enter the built environment in a new sub-shell
bits enter ROOT/latest

# 5. Use the software
root -b

# 6. Leave the sub-shell to return to your normal environment
exit
```

---

## 3a. The bits Development-to-Deployment Workflow {#the-bits-development-to-deployment-workflow}

The key distinction between bits and conventional package managers is that a **single, shared toolchain connects every developer's laptop to the experiment's CVMFS software repository**. The exact same `bits build` command that a developer runs interactively drives the CI pipeline that publishes packages to CVMFS for the entire community. Local source checkouts (`git clone <repo>` placed next to the recipe directory) are detected automatically and built in preference to the upstream version — while all other dependencies are resolved from the shared recipe repository as usual.

The workflow spans five phases: local setup from shared recipes → local development with full-stack context → full-stack local testing → commit and peer review → CI build and CVMFS publication. The CI publication step supports two distinct paths, resulting in packages in **different CVMFS namespaces** depending on the role of the person triggering the build:

- **Production build** (`group-admin` / `bits-admin`) — triggered via the **Build → Production** button in bits-console; publishes to the community's `cvmfs_prefix` (e.g. `/cvmfs/sft.cern.ch/lcg/releases/`), available experiment-wide. The pipeline enforces this role server-side; it cannot be bypassed.
- **Personal-area build** (any authenticated user) — triggered via the **Build → Personal area** button; publishes to `cvmfs_user_prefix/<username>/…` (e.g. `/cvmfs/sft.cern.ch/lcg/user/jsmith/`), independent of the group stack rebuild cycle and accessible without admin rights.

The full phase-by-phase walkthrough, workflow diagram, and command examples are in **[WORKFLOWS.md](WORKFLOWS.md)**.

---

## 4. Configuration

Bits reads an optional INI-style configuration file at startup to set the working directory, recipe search paths, and other defaults. The file can be created manually or with `bits init` in [config mode](#config-mode----write-persistent-settings-to-bitsrc).

### File locations and search order

Bits tries the following locations in order and loads the **first file it finds**, ignoring the rest:

| Priority | Path | Description |
|---|---|---|
| 1 | `--config=FILE` | Explicit path given on the command line |
| 2 | `./bits.rc` | Project-local config in the current directory |
| 3 | `./.bitsrc` | Hidden project-local config |
| 4 | `~/.bitsrc` | User-level config in the home directory |

If `--config` names a file that does not exist the search continues down the list. If no file is found at all the built-in defaults apply.

### File format

The file uses Windows INI-style syntax. Two section names are recognised:

- **`[bits]`** — read first; provides global defaults.
- **`[<organisation>]`** — read second and overrides `[bits]`; the section name must match the current `organisation` value (default `ALICE`). This allows a single file to serve multiple organisations with different settings.

Within each section, each line is `key = value` (spaces around `=` are stripped). Lines that do not contain `=` are ignored, so plain-text comments work without a `#` prefix (though `#` comments are harmless too). Sections are delimited by blank lines — the parser reads from the section header up to the first blank line.

### Variables

The `[bits]` section recognises two classes of keys: legacy shell-level variables (exported to the environment for use by shell scripts) and Python-level settings (applied directly to `bits` option defaults before argument parsing).

**Shell-level variables** (also exported to the environment for shell scripts):

| Config key | Exported as | Default | Description |
|---|---|---|---|
| `organisation` | `BITS_ORGANISATION` | `ALICE` | Organisation name. Also selects the organisation-specific section in this file. |
| `pkg_prefix` | `BITS_PKG_PREFIX` | `VO_<organisation>` | Prefix prepended to package names in `bits q` output. |
| `repo_dir` | `BITS_REPO_DIR` | `alidist` | Root directory for recipe repositories. |
| `sw_dir` | `BITS_WORK_DIR` | `sw` | Output and work directory for built packages, source mirrors, and module files. |
| `search_path` | `BITS_PATH` | _(empty)_ | Comma-separated list of additional recipe search directories. Absolute paths are used directly; relative names have `.bits` appended. |

**Python-level option defaults** (set before argument parsing; overridden by any explicit CLI flag or environment variable):

| Config key | Equivalent CLI flag | Description |
|---|---|---|
| `remote_store` | `--remote-store URL` | Binary store to fetch pre-built tarballs from. |
| `write_store` | `--write-store URL` | Binary store to upload newly-built tarballs to. |
| `providers` | `--providers URL` / `$BITS_PROVIDERS` | URL of the bits-providers repository. |
| `provider_policy` | `--provider-policy POLICY` | Comma-separated `name:position` pairs controlling where each repository-provider's checkout lands in `BITS_PATH`. See [§13 Provider policy](#provider-policy). |
| `store_integrity` | `--store-integrity` | Set to `true`, `1`, or `yes` to enable local tarball integrity verification. Off by default. See [§21 Store integrity verification](#store-integrity-verification). |
| `work_dir` | `-w DIR` / `$BITS_WORK_DIR` | Default work/output directory. |
| `architecture` | `-a ARCH` | Default target architecture. |
| `defaults` | `--defaults PROFILE` | Default profile(s), `::` separated. |
| `config_dir` | `-c DIR` | Default recipe directory. |
| `reference_sources` | `--reference-sources DIR` | Default mirror directory. |
| `organisation` | `--organisation NAME` | Organisation tag (see also shell-level table above). |

These keys can be written automatically with `bits init` — see [§16 bits init config mode](#config-mode----write-persistent-settings-to-bitsrc).

### Precedence

The config file only fills in values that are not already set. The full precedence chain from highest to lowest is:

```
explicit CLI flag  >  environment variable  >  bits.rc value  >  built-in default
```

For example, if `bits.rc` sets `sw_dir = /data/sw` but the user runs `bits build -w /tmp/sw ROOT`, the `-w` flag wins. If neither a flag nor an environment variable is set, `/data/sw` from the config file applies.

### Example configuration

```ini
[bits]
organisation = ALICE

[ALICE]
pkg_prefix   = VO_ALICE
sw_dir       = ../sw
repo_dir     = .
search_path  = common.bits
```

The `[ALICE]` section overrides or extends `[bits]` for the `ALICE` organisation. A second organisation (e.g. `[CMS]`) can coexist in the same file with different `sw_dir` and `search_path` values; only the section matching the current `organisation` key is applied.

Every setting can also be overridden by an environment variable — see [§19 Environment Variables](#19-environment-variables) for the full mapping.

---

## 5. Building Packages

```bash
bits build [options] PACKAGE [PACKAGE ...]
```

Bits resolves the full transitive dependency graph of each requested package, computes a content-addressable hash for every node, downloads any pre-built artifacts that already exist in a remote store, and builds the rest in topological order.

### How a build proceeds

1. **Recipe discovery** — Bits locates `<package>.sh` in each directory on `search_path` (appending `.bits` to each name). Repository-provider packages (see [§13](#13-repository-provider-feature)) are cloned first to extend the search path before the main resolution pass.
2. **Dependency resolution** — `requires`, `build_requires`, and `runtime_requires` fields are read recursively, forming a DAG. Cycles are reported as errors.
3. **Hash computation** — A hash is computed for each package from its recipe text, source commit, dependency hashes, and environment. Packages with a matching hash in a store are downloaded instead of rebuilt.
4. **Source fetching** — Source repositories are cloned into a local mirror and then checked out into a build area. Up to 8 repositories are fetched in parallel.
5. **Build execution** — Each package's Bash script runs in an isolated environment with sanitised locale and only its declared dependencies visible.
6. **Post-build** — A modulefile and a versioned tarball are written; the tarball may be uploaded to a write store.


---

### Common options

| Option | Description |
|--------|-------------|
| `--defaults PROFILE` | Defaults profile(s) to load. Combines multiple files with `::` (e.g. `--defaults release::myproject`). Default: `release`, which loads `defaults-release.sh`. |
| `-j N`, `--jobs N` | Parallel compilation jobs per package. Default: CPU count. |
| `--builders N` | Number of packages to build simultaneously using the Python scheduler. Default: 1 (serial). Mutually exclusive with `--makeflow`. |
| `--makeflow` | Hand the entire dependency graph to the external [Makeflow](https://ccl.cse.nd.edu/software/makeflow/) workflow engine instead of the built-in Python scheduler. Mutually exclusive with `--builders N`. |
| `--pipeline` | Split each Makeflow rule into three stages (`.build`, `.tar`, `.upload`) so that tarball creation and upload overlap with downstream builds. Requires `--makeflow`; silently disabled otherwise. |
| `--prefetch-workers N` | Spawn *N* background threads that fetch remote tarballs and source archives ahead of the main build loop. Default: 0 (disabled). Has no effect when no remote store is configured. |
| `--parallel-sources N` | Download up to *N* `sources:` URLs concurrently within a single package checkout. Default: 1 (sequential). |
| `-u`, `--fetch-repos` | Update all source mirrors before building. |
| `-w DIR`, `--work-dir DIR` | Work/output directory. Default: `sw`. |
| `--remote-store URL` | Binary store to pull pre-built tarballs from. |
| `--write-store URL` | Binary store to push newly-built tarballs to. |
| `--force` | Rebuild even if the package hash already exists. |
| `--docker` | Build inside a Docker container. |
| `--debug` | Verbose debug output. |
| `--dry-run` | Print what would happen without executing. |
| `--keep-tmp` | Preserve build directories after success (useful for debugging). |

### Parallel build modes

Bits offers two independent mechanisms for building multiple packages at the same time. They are mutually exclusive — if `--makeflow` is given, `--builders` is ignored.

#### `--builders N` — Python scheduler (default)

The built-in Python scheduler runs up to *N* package builds concurrently using a thread-pool with a priority queue. Dependencies are tracked in memory: a package is only dispatched once all of its transitive dependencies have finished.

```bash
# Build up to 4 packages simultaneously, each using 8 cores
bits build --builders 4 --jobs 8 MyStack
```

**Characteristics:**

- No external dependencies — works out of the box.
- Scheduling is priority-aware: packages required by more dependents are started first.
- Optional resource-aware scheduling: if `--resources FILE` is provided (a JSON file that declares expected CPU and RSS per package), bits will not start a new package build unless the declared resources are available. This prevents memory exhaustion on machines where several large packages would otherwise run at the same time.
- Errors from any worker are reported after the full run completes and cause bits to exit with a non-zero status.

#### `--makeflow` — Makeflow workflow engine

When `--makeflow` is passed, bits does **not** execute builds during the dependency-graph walk. Instead, it collects every pending build command into a [Makeflow](https://ccl.cse.nd.edu/software/makeflow/) declarative workflow file and then invokes the `makeflow` binary to execute the graph. Makeflow must be installed separately (it is part of the [CCTools](https://ccl.cse.nd.edu/software/) suite).

```bash
# Run the full build under Makeflow
bits build --makeflow MyStack

# Debug a Makeflow failure
bits build --makeflow --debug MyStack
```

**Output locations (useful for debugging):**

| Path | Contents |
|------|----------|
| `sw/BUILD/<hash>/makeflow/Makeflow` | The generated workflow definition. |
| `sw/BUILD/<hash>/makeflow/log` | Makeflow's execution log. |

**When Makeflow fails**, bits prints a structured error message with the exact paths, the failed command, and suggested next steps — including how to rerun with `--debug` and where to find the full log.

**Choosing between the two modes:**

| | `--builders N` | `--makeflow` |
|-|---|---|
| External dependency | None | `makeflow` binary (CCTools) |
| Parallelism control | You set *N* | Makeflow decides |
| Resource awareness | Optional (`--resources`) | Not built-in |
| Best for | Interactive builds, CI | Large distributed or cluster builds |

#### `--pipeline` — pipelined tarball creation and upload (Makeflow only)

When both `--makeflow` and `--pipeline` are given, each package's Makeflow rule is split into three sequential stages:

| Stage | Makeflow target | What it does |
|-------|----------------|--------------|
| Build | `<pkg>.build` | Compiles the package; skips tarball creation (`SKIP_TARBALL=1`). |
| Tar   | `<pkg>.tar`   | Creates the versioned tarball and dist-link tree in a `tar_template.sh` invocation. |
| Upload | `<pkg>.upload` | Uploads the tarball to the write store (Boto3 or rsync). Omitted when no write store is configured or when using an HTTP/CVMFS read-only backend. |

Because `.tar` and `.upload` are separate Makeflow rules, Makeflow can overlap them with downstream package builds as soon as the `.build` rule completes. This is particularly effective in large stacks where package *B* depends on *A* but the tarball upload of *A* is slow: *B* can start building while *A*'s tarball is still being uploaded.

```bash
bits build --makeflow --pipeline --write-store b3://mybucket/store MyStack
```

Constraints:
- Requires `--makeflow`; silently reverts to standard behaviour when used without it.
- When combined with `--docker`, the `.tar` and `.upload` stages still run on the host after the container exits (via the volume mount), so the pipeline is fully compatible with Docker builds.

#### `--prefetch-workers N` — background tarball prefetch

Prefetch workers download remote tarballs and source archives in the background while the build loop is running. This hides network latency for the common case where a remote binary store holds most packages.

```bash
# Fetch up to 4 tarballs concurrently in the background
bits build --prefetch-workers 4 --remote-store https://store.example.com/store MyStack
```

Bits spawns a thread pool of *N* threads at startup and immediately submits a prefetch task for every pending package. Each task:
1. Attempts to fetch the pre-built tarball from the remote store into the content-addressable store directory.
2. Downloads any `sources:` URLs declared in the recipe.

Coordination with the main build loop uses *sentinel files*: a `<path>.downloading` file is created atomically when a thread claims a download, and deleted when the download finishes. The main loop waits for the sentinel before calling `fetch_tarball`, so it never blocks on a download that is already in progress. Stale sentinels from a crashed previous run are cleaned up automatically at startup.

`--prefetch-workers` has no effect when no `--remote-store` is configured, or when the remote store is read-only (e.g. HTTP).

#### `--parallel-sources N` — concurrent source downloads

Each package may declare multiple `sources:` URLs (e.g. upstream release tarball plus a patch archive). By default, bits downloads these sequentially. With `--parallel-sources N`, up to *N* URLs are fetched concurrently within a single package checkout:

```bash
bits build --parallel-sources 4 MyStack
```

If any source download fails, the exception is re-raised immediately and the package build is aborted. The remaining concurrent downloads are cancelled via thread pool shutdown. When `N ≤ 1` or the package has only a single source, the sequential code path is used (no overhead from the thread pool).


---

## 6. Managing Environments

Bits uses the standard [Environment Modules](https://modules.sourceforge.net/) system (`modulecmd`) to manage runtime environments. A *module* corresponds to one built package version. The `bits` shell script discovers `modulecmd` automatically in three locations: on `$PATH` (v3), via `envml` (v4+), or via Homebrew (`brew --prefix modules`) on macOS. If none is found, it prints the appropriate install command (`apt-get install environment-modules`, `yum install environment-modules`, or `brew install modules`).

Before any module command runs, bits rebuilds the `MODULES/<ARCH>/` directory by scanning every installed package for an `etc/modulefiles/<PKG>` file and copying it into the right place. Pass `--no-refresh` to skip this scan and use whatever is already on disk.

### Global options

The following options apply to all module sub-commands and must be placed before the sub-command name:

| Option | Description |
|--------|-------------|
| `-w DIR`, `--work-dir DIR` | Work directory containing the `sw/` tree. Defaults to `$BITS_WORK_DIR` (then `sw`, then `../sw`). |
| `-a ARCH`, `--architecture ARCH` | Architecture sub-directory. Auto-detected from `bitsBuild architecture` or the most recently modified directory under the work dir. |
| `--no-refresh` | Skip rebuilding `MODULES/<ARCH>/` before executing the command. Useful when the installation has not changed. |

### Enter a sub-shell with modules loaded

```bash
bits enter ROOT/latest
# A new sub-shell opens with ROOT and all its dependencies in PATH etc.
exit   # return to your normal shell
```

`bits enter` sets the shell prompt to `[MODULE] \w $>` (or equivalent for zsh/ksh) so it is always clear when inside a bits environment. Nesting `bits enter` inside another bits environment is blocked.

| Option | Description |
|--------|-------------|
| `--shellrc` | Source your shell startup file (`.bashrc`, `.zshrc`, etc.) in the new shell. By default startup files are suppressed to prevent environment conflicts. |
| `--dev` | Instead of loading modules through `modulecmd`, source each package's `etc/profile.d/init.sh` directly. Intended for development work. Appends `(dev)` to the shell prompt. |

The shell type is auto-detected from the parent process. Override it with the `MODULES_SHELL` environment variable (accepts `bash`, `zsh`, `ksh`, `csh`, `tcsh`, `sh`).

### Load / unload in the current shell

```bash
# Integrate once in ~/.bashrc or ~/.zshrc:
BITS_WORK_DIR=/path/to/sw
eval "$(bits shell-helper)"

# Then in any shell session:
bits load ROOT/latest        # adds ROOT to the current environment
bits unload ROOT             # removes it (version can be omitted)
bits list                    # show currently loaded modules
bits q [REGEXP]              # list available modules, optionally filtered
```

Without `shell-helper` you must use `eval` manually:

```bash
eval "$(bits load ROOT/latest)"
eval "$(bits unload ROOT)"
```

Pass `-q` to either command to suppress the informational message on stderr.

### Run a single command in a module environment

```bash
bits setenv ROOT/latest -c root -b
# Everything after -c is executed as-is; the exit code is preserved.
```

`bits setenv` loads the modules into the current process environment and then `exec`s the command — no new shell is spawned.

### Inspect and manage modules

```bash
bits q [REGEXP]     # list available modules, filtered by optional regex
bits list           # list currently loaded modules
bits avail          # raw modulecmd avail output
bits modulecmd zsh load ROOT/latest   # pass arguments directly to modulecmd
```

### Shell helper

Add the following to your `.bashrc`, `.zshrc`, or `.kshrc` so that `bits load` and `bits unload` modify the current shell's environment without requiring an explicit `eval`:

```bash
BITS_WORK_DIR=/path/to/sw
eval "$(bits shell-helper)"
```

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
| `--min-free GIB` | _(none)_ | Evict the least-recently-used packages until at least `GIB` GiB of free disk space is available on the workDir filesystem. |
| `--disk-pressure-only` | — | Run only the disk-pressure eviction pass; skip age-based eviction regardless of `--max-age`. Useful as a pre-build guard. |
| `-n`, `--dry-run` | — | Show which packages would be evicted without removing anything. |

**How it works.** Every time a package is built or confirmed already installed, bits touches a *sentinel file* at `$WORK_DIR/.packages/<arch>/<package>/<version>`. The `cleanup` command reads these sentinels, sorts packages by last-touched time (oldest first), and evicts those that are too old or that need to be removed to recover disk space. A package whose sentinel is locked by an in-progress build is always skipped safely.

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

## 8. Cookbook

### Build a complete stack from scratch

```bash
bits doctor ROOT            # verify system requirements first
bits build ROOT             # build everything
bits enter ROOT/latest      # drop into the built environment
```

### Develop and iterate on a single package

```bash
bits init libfoo            # create a writable source checkout
# … edit source in the libfoo/ directory …
bits build libfoo           # rebuilds only libfoo (devel mode)
eval "$(bits load libfoo/latest)"
```

### Set up a project with a persistent binary store

Instead of passing `--remote-store` on every `bits build` invocation, write it once with `bits init` (no package name):

```bash
# One-time setup — writes bits.rc in the current directory
bits init --remote-store https://store.example.com/store \
          --write-store  b3://mybucket/store \
          --organisation MYORG

# Every subsequent invocation picks up the settings automatically
bits build ROOT
```

To check what will be written before touching the file system, add `--dry-run`. To update a single key in an existing `bits.rc` without replacing the whole file, add `--append`.

### Debug a failed build

```bash
bits build --debug --keep-tmp my_package
# Build directory path is printed in the log
cd sw/BUILD/my_package-*/
cat log
# Re-run the failing command manually to iterate quickly
```

### Share pre-built artifacts over S3

```bash
# CI: build and upload (boto3 backend; ::rw sets both --remote-store and --write-store)
export AWS_ACCESS_KEY_ID=ci-key
export AWS_SECRET_ACCESS_KEY=ci-secret
bits build --remote-store b3://mybucket/bits-cache::rw ROOT

# Developer workstation: fetch from the same cache, never upload
bits build --remote-store b3://mybucket/bits-cache ROOT
```

See [§21](#21-remote-binary-store-backends) for the full list of backends (HTTP, S3, boto3, rsync, CVMFS) and detailed CI/CD patterns.

### Parallel build with the Python scheduler

```bash
# Build up to 4 independent packages simultaneously, each using 8 cores
bits build --builders 4 --jobs 8 my_large_stack
```

The built-in Python scheduler dispatches packages as soon as their dependencies are satisfied. See [§5 Parallel build modes](#parallel-build-modes) for resource-aware scheduling with `--resources`.

### Parallel build with Makeflow

```bash
# Hand the dependency graph to the Makeflow workflow engine
bits build --makeflow my_large_stack

# Inspect what Makeflow generated (useful if a build fails)
cat sw/BUILD/*/makeflow/Makeflow
cat sw/BUILD/*/makeflow/log
```

Makeflow must be installed separately from the [CCTools](https://ccl.cse.nd.edu/software/) suite. It automatically parallelises across all packages where the dependency graph permits.

### Pipelined build with overlapping upload (Makeflow + pipeline)

```bash
# Overlap tarball upload with downstream builds; prefetch tarballs 4 at a time
bits build --makeflow --pipeline \
           --write-store b3://mybucket/store \
           --prefetch-workers 4 \
           my_large_stack
```

`--pipeline` splits each package's Makeflow rule into `.build` / `.tar` / `.upload` stages so that upload of package *A* can overlap with the build of package *B*. `--prefetch-workers` hides network latency by downloading remote tarballs in the background before the build loop needs them. See [§5 Async pipeline options](#--pipeline----pipelined-tarball-creation-and-upload-makeflow-only) for full details.

### Speed up source downloads

```bash
# Download up to 4 source archives in parallel within each package
bits build --parallel-sources 4 my_large_stack
```

Useful when a package lists several large `sources:` URLs. Failed downloads still abort the build immediately.

### Build for a different Linux version (Docker)

```bash
bits build --docker --architecture ubuntu2004_x86-64 ROOT
```

### Generate a dependency graph

```bash
bits deps --outgraph deps.pdf ROOT   # requires Graphviz
```

### Run a single command in the built environment

```bash
bits setenv ROOT/v6-30 -c root -b
```

Use `bits setenv` to execute a single command (with optional arguments) in the built environment without spawning an interactive shell. The target module must be installed first. Exit code and output pass through unchanged.

### Load modules persistently into the current shell

Add to `~/.bashrc`, `~/.zshrc`, or `~/.kshrc`:

```bash
BITS_WORK_DIR=/path/to/sw
eval "$(bits shell-helper)"
```

Then in any new shell session:

```bash
bits load ROOT/latest      # load into current shell
bits unload ROOT           # unload from current shell
```

The `bits shell-helper` function modifies the current shell's environment directly without requiring an explicit `eval`. Combine with multiple modules: `bits load ROOT/latest,Python/3.11-1`.

### Override a package version without editing the recipe

Defaults profiles can pin package versions globally without modifying recipe files:

```yaml
# In defaults-myproject.sh
overrides:
  ROOT:
    version: "6-30-06"
```

Then build with:

```bash
bits build --defaults release::myproject MyStack
```

This is useful for shared recipes where different projects need different versions, or for emergency pinning when a new version breaks downstream packages.

### Enforce reproducible source downloads with checksums

First, compute and write checksums for all sources:

```bash
bits build --write-checksums MyPackage
```

This creates or updates `checksums/MyPackage.checksum` in the recipe directory. Then enforce them on all future builds:

```bash
bits build --enforce-checksums MyPackage
```

Or make it the site default in a defaults profile:

```yaml
# defaults-production.sh
checksum_mode: enforce
```

Any mismatch or missing checksum will abort the build, catching supply-chain tampering or silent mirror corruption.

### Build memory-hungry packages without exhausting RAM

For packages with large parallel builds that risk OOM, limit concurrent builds and/or specify per-package resource budgets:

```bash
# Option 1: reduce concurrent package builds
bits build --builders 1 --jobs 8 my_stack

# Option 2: use a resource file
bits build --builders 4 --resources my_resources.json my_stack
```

Where `my_resources.json` declares expected CPU and memory per package:

```json
{
  "gcc": {"cpu": 4, "rss_mb": 1024},
  "llvm": {"cpu": 8, "rss_mb": 4096}
}
```

The Python scheduler will not start a new build unless the declared resources are free, preventing overcommit.

### Use a private recipe repository alongside the defaults

Set `BITS_PATH` to prepend a custom repository to the search path:

```bash
BITS_PATH=myorg.bits bits build MyPackage
```

Or configure it persistently:

```bash
bits init --config-dir myorg.bits MyPackage
```

This is useful for building private packages that depend on public recipes, or for maintaining a vendor-specific overlay (e.g. a fork of `gcc` with custom patches) without modifying the main recipe repository.

### CI/CD: build and publish only on the main branch

Use conditional logic in CI to upload binaries only for production builds:

```bash
if [ "$CI_COMMIT_BRANCH" = "main" ]; then
  bits build --write-store b3://mybucket/store::rw MyStack
else
  # Feature branches: build locally but do not publish
  bits build MyStack
fi
```

The `::rw` suffix sets both `--remote-store` and `--write-store` (if already configured). For more control, use separate variables:

```bash
if [ "$CI_COMMIT_BRANCH" = "main" ]; then
  WRITE_STORE="b3://mybucket/store"
else
  WRITE_STORE=""
fi

bits build --remote-store b3://mybucket/store --write-store "$WRITE_STORE" MyStack
```

This ensures PR builds download cached binaries but never pollute the production store.

---

# Part II — Developer Guide

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

The `bits-recipe-tools` package (available at `https://github.com/bitsorg/bits-recipe-tools`) provides a higher-level recipe authoring style built around reusable shell function hooks. Instead of writing a flat Bash build script, the recipe author overrides only the steps that differ from the standard template.

#### How it works

`build_template.sh` sources the compiled recipe script and then calls a function named `Run` if one is defined:

```bash
source "$WORK_DIR/SPECS/.../PackageName.sh" && \
  [[ $(type -t Run) == function ]] && Run "$@"
```

`bits-recipe-tools` ships include files — `CMakeRecipe`, `AutoToolsRecipe`, and others — each of which defines a `Run()` function that orchestrates the build in terms of five lifecycle hooks:

| Hook | Default behaviour |
|------|-------------------|
| `Prepare()` | Sets up the build directory and any pre-configure steps. |
| `Configure()` | Runs `cmake` (or `./configure`) with standard flags. |
| `Make()` | Runs `make -j$JOBS` (or `cmake --build`). |
| `MakeInstall()` | Runs `make install` (or `cmake --install`). |
| `PostInstall()` | Runs any post-install fixups (e.g. removing libtool archives). |

A recipe overrides only the hooks it needs to customise; all others run with sensible defaults.

#### MODULE_OPTIONS — controlling modulefile generation

When using `bits-recipe-tools`, the variable `MODULE_OPTIONS` controls how the Environment Modules modulefile is generated for the package. It must be set **before** sourcing the include file so that the `PostInstall()` hook picks it up:

```bash
MODULE_OPTIONS="--bin --lib"
. $(bits-include CMakeRecipe)
```

`MODULE_OPTIONS` is a space-separated list of flags. Each flag causes `bits-recipe-tools` to add a specific entry to `$INSTALLROOT/etc/modulefiles/$PKGNAME`:

| Flag | Effect on the modulefile |
|------|--------------------------|
| `--bin` | Prepends `$INSTALLROOT/bin` to `PATH`. |
| `--lib` | Prepends `$INSTALLROOT/lib` to `LD_LIBRARY_PATH`. |
| `--cmake` | Adds `$INSTALLROOT` to `CMAKE_PREFIX_PATH`. |
| `--root` | Defines the variable `ROOT_<PACKAGE>` (uppercased package name) as `$INSTALLROOT`. |

Flags can be combined freely. Omitting `MODULE_OPTIONS` entirely causes the helper to use its built-in defaults, which is usually appropriate for standard library packages.

```bash
# A typical compiled library: export bin, lib, and the ROOT variable
MODULE_OPTIONS="--bin --lib --root"
. $(bits-include CMakeRecipe)

# A CMake-only build tool: just add to CMAKE_PREFIX_PATH
MODULE_OPTIONS="--cmake"
. $(bits-include CMakeRecipe)

# A header-only library: CMake discovery and the ROOT variable, no runtime paths
MODULE_OPTIONS="--cmake --root"
. $(bits-include CMakeRecipe)
```

#### Loading an include file

The `bits-include` helper command resolves an include file shipped by `bits-recipe-tools` and returns its absolute path, which the recipe then sources with `.`:

```bash
. $(bits-include CMakeRecipe)
```

`bits-recipe-tools` must be listed as a `build_requires` of the recipe.

#### Example — header-only CMake library (cppgsl)

```yaml
package: cppgsl
version: "4.0.0"
source: https://github.com/microsoft/GSL.git
tag: "v4.0.0"
build_requires:
  - cmake
  - bits-recipe-tools
---
# Header-only library: add to CMAKE_PREFIX_PATH and define ROOT_CPPGSL.
MODULE_OPTIONS="--cmake --root"
. $(bits-include CMakeRecipe)

# Override only the Configure step to disable tests.
Configure() {
  cmake -S "$SOURCEDIR" -B "$BUILDDIR" \
        -DCMAKE_INSTALL_PREFIX="$INSTALLROOT" \
        -DGSL_TEST=OFF \
        -DCMAKE_BUILD_TYPE=Release
}
```

`CMakeRecipe` provides the `Run()` dispatcher and default `Prepare`, `Make`, `MakeInstall`, and `PostInstall` implementations. The recipe above overrides only `Configure()` to pass the `-DGSL_TEST=OFF` flag; everything else is inherited from the template. `MODULE_OPTIONS` is set before sourcing the include so the `PostInstall()` step uses it when generating the modulefile.

#### Example — Autotools library

```yaml
package: libfoo
version: "1.4.2"
source: https://example.com/libfoo.git
tag: "v1.4.2"
build_requires:
  - autotools
  - bits-recipe-tools
---
. $(bits-include AutotoolsRecipe)

# The default Configure() runs:
#   "$SOURCEDIR/configure" --prefix="$INSTALLROOT"
# Override it to add custom options.
Configure() {
  "$SOURCEDIR/configure" \
    --prefix="$INSTALLROOT" \
    --enable-shared \
    --disable-static
}
```

#### Writing a recipe without an include file

The function pattern works without `bits-recipe-tools` too. Any recipe may define a `Run()` function directly:

```bash
Run() {
  cmake -S "$SOURCEDIR" -B "$BUILDDIR" \
        -DCMAKE_INSTALL_PREFIX="$INSTALLROOT"
  cmake --build "$BUILDDIR" --parallel "$JOBS"
  cmake --install "$BUILDDIR"
}
```

This is equivalent to a flat script but is sometimes clearer when the build needs multiple named phases.

---

## 13. Repository Provider Feature

A **repository provider** is a recipe that, instead of describing a software package to build, describes *another recipe repository* to load dynamically at dependency-resolution time.

### Why it exists

Normally the set of recipe repositories (`*.bits` directories) is fixed at startup via `BITS_PATH` / `search_path`. The repository provider feature lets a recipe itself pull in an additional recipe repository from git, enabling modular recipe sets and nested providers.

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
| `test_qualify_arch.py` | `compute_combined_arch`, `qualify_arch` end-to-end through `effective_arch`, install path, and `init.sh` generation |
| `test_repo_provider.py` | Repository provider: `getConfigPaths` absolute paths, `_add_to_bits_path`, `clone_or_update_provider` caching, iterative discovery, nested providers, hash propagation |
| `test_sync.py` | Remote store backends (requires `botocore` for S3 tests) |

### Guidelines for new tests

- Mock all network and filesystem side-effects; tests must pass offline.
- Place provider/SCM fixtures in `tempfile.mkdtemp()` directories cleaned up in `tearDown`.
- Use `unittest.mock.patch.object` to replace module-level functions (not `assertLogs` when the bits `LogFormatter` is active — patch `warning` directly instead).

---

## 15. Contributing

- The main development branch is `main`.
- All tests must pass before a pull request is merged.
- Follow the code style enforced by `.flake8` and `.pylintrc`.
- Write docstrings for new public functions.
- Update this document (REFERENCE.md) when changing any user-facing behaviour, CLI options, or recipe fields.
- The project is licensed under the terms in `LICENSE.md`.

---

# Part III — Reference Guide

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
| `-a ARCH`, `--architecture ARCH` | Target architecture. Default: auto-detected. |
| `--force-unknown-architecture` | Proceed even if architecture is unrecognised. |
| `-j N`, `--jobs N` | Parallel compilation jobs per package. Default: CPU count. |
| `--builders N` | Packages to build simultaneously using the built-in Python scheduler. Default: 1 (serial). Mutually exclusive with `--makeflow`; if both are given, `--makeflow` takes precedence. |
| `--makeflow` | Generate a [Makeflow](https://ccl.cse.nd.edu/software/makeflow/) workflow file from the dependency graph and execute it with the `makeflow` binary (must be installed separately from CCTools). Bits collects all pending builds, writes `sw/BUILD/<hash>/makeflow/Makeflow`, then runs `makeflow` to execute the graph in parallel. Mutually exclusive with `--builders N`. |
| `--pipeline` | Split each Makeflow rule into `.build`, `.tar`, and `.upload` stages so that tarball creation and upload can overlap with downstream builds. Requires `--makeflow`; silently ignored otherwise. |
| `--prefetch-workers N` | Spawn *N* background threads to fetch remote tarballs and source archives ahead of the main build loop. Default: 0 (disabled). No effect without `--remote-store`. |
| `--parallel-sources N` | Download up to *N* `sources:` URLs concurrently within a single package checkout. Default: 1 (sequential). |
| `-e KEY=VALUE` | Extra environment variable binding (repeatable). |
| `-z PREFIX`, `--devel-prefix PREFIX` | Version prefix for development packages. |
| `-u`, `--fetch-repos` | Fetch/update source mirrors before building. |
| `--no-local PACKAGE` | Do not use a local checkout for PACKAGE (repeatable). |
| `-w DIR`, `--work-dir DIR` | Work/output directory. Default: `sw`. |
| `--config-dir DIR` | Directory containing recipe files. |
| `--reference-sources DIR` | Local mirror of git repositories. |
| `--remote-store URL` | Binary store to fetch pre-built tarballs from. |
| `--write-store URL` | Binary store to upload built tarballs to. |
| `--disable PACKAGE` | Skip PACKAGE entirely (repeatable). |
| `--prefer-system` | Use system-installed packages where supported. |
| `--no-system` | Never use system-installed packages. |
| `--always-prefer-system` | Always prefer system packages. |
| `--check-system-packages` | Check system packages without building. |
| `--docker` | Build inside a Docker container. |
| `--docker-image IMAGE` | Docker image to use. Implies `--docker`. |
| `--docker-extra-args ARGS` | Extra arguments for `docker run`. |
| `--cvmfs-prefix PATH` | Bind-mount the workDir at `PATH` inside the container instead of the default `/container/bits/sw`. When set, packages compile with their final CVMFS paths already embedded so that `bits publish --no-relocate` can skip the relocation step. Requires `--docker`; has no effect without it. |
| `--container-use-workdir` | Mount the workDir at the same path inside the container (i.e. `container_workDir = workDir`). Useful when the host and container share the same filesystem namespace. Mutually exclusive with `--cvmfs-prefix`; if both are set `--cvmfs-prefix` takes precedence. |
| `--docker-platform PLATFORM` | Docker `--platform` argument for cross-compilation (e.g. `linux/arm64`, `linux/amd64`, `linux/ppc64le`). When not set, bits derives the platform automatically from `--architecture`: if the target differs from the host the matching platform is injected so QEMU emulates the target inside the builder container. Pass `native` to suppress automatic injection. Requires QEMU binfmt handlers on the Docker host. See [§22b Cross-compilation via QEMU](#22b-cross-compilation-via-qemu). |
| `--sandbox MODE` | Sandbox each recipe build script for extra isolation. `auto` (default): podman on Linux if available, `sandbox-exec` on macOS, nested podman inside Docker containers. `podman`: always use podman (requires `--docker` or `--sandbox-image`). `sandbox-exec`: macOS only. `off`: no sandboxing. See [§22a Recipe Sandbox](#22a-recipe-sandbox). |
| `--sandbox-image IMAGE` | Container image for `--sandbox=podman` when not using `--docker`. Implies `--sandbox=podman`. Defaults to the `--docker` image when `--docker` is set. |
| `--force` | Rebuild even if the package hash already exists. |
| `--keep-tmp` | Keep temporary build directories after success. |
| `--resource-monitoring` | Enable per-package CPU/memory monitoring. |
| `--resources FILE` | JSON resource-utilisation file for scheduling. |
| `--check-checksums` | Verify checksums declared in `sources`/`patches` entries during download; emit a warning on mismatch but continue the build. Overrides `checksum_mode:` in the active defaults profile. |
| `--enforce-checksums` | Verify checksums declared in `sources`/`patches` entries during download; abort the build on any mismatch or if a checksum is missing for a file. Overrides `checksum_mode:`. |
| `--print-checksums` | Compute and print checksums for all sources and patches in ready-to-paste YAML format **after** the build completes. Works for already-compiled packages (reads from the download cache). Overrides `checksum_mode:`. |
| `--write-checksums` | Write (or update) `checksums/<package>.checksum` in the recipe directory **after** the build completes. Works for already-compiled packages. Also records the pinned git commit SHA for `source:` + `tag:` packages. Overrides `write_checksums:` in the active defaults profile. |
| `--store-integrity` | Enable local tarball integrity verification. After each upload the tarball's SHA-256 is recorded in `$WORK_DIR/STORE_CHECKSUMS/`. On every subsequent recall from the remote store the digest is recomputed and compared; a mismatch is a fatal error. Disabled by default for backward compatibility. Can also be enabled persistently with `store_integrity = true` in `bits.rc`. See [§21 Store integrity verification](#store-integrity-verification). |
| `--provider-policy POLICY` | Control where each repository-provider's checkout is inserted into `BITS_PATH`. Format: comma-separated `name:position` pairs where `position` is `prepend` or `append`. Example: `--provider-policy bits-providers:prepend,myorg:append`. By default every provider is appended regardless of its recipe declaration. Can also be set in `bits.rc` as `provider_policy = …`. See [§13 Provider policy](#provider-policy). |
| `--from-manifest FILE` | Replay a build from a manifest JSON file. The `PACKAGE` positional argument is optional when this flag is given — bits uses the `requested_packages` field recorded in the manifest. Each recalled tarball is verified against the manifest's `tarball_sha256`. See [§25 Build Manifest](#25-build-manifest). |

The three `--*-checksums` flags are mutually exclusive. Precedence (highest → lowest): `--print-checksums` > `--enforce-checksums` > `--check-checksums` > `checksum_mode:` in defaults profile > per-recipe `enforce_checksums: true` > `off`. `--write-checksums` is independent and can be combined with any of the above. Both `--print-checksums` and `--write-checksums` can also be set site-wide via `checksum_mode: print` and `write_checksums: true` in the active defaults profile (see [§18 — Checksum policy in defaults profiles](#checksum-policy-in-defaults-profiles)).

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

Check that a live deployment matches the build manifest written by `bits build`. See [§22c bits verify — Deployment Verification](#22c-bits-verify--deployment-verification) for full details.

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
| `--organisation NAME` | `organisation` | Organisation tag used by defaults profiles and recipe tooling. |
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

`bits q` lists modules in `BITS_PKG_PREFIX@PKG::VERSION` format. The optional `REGEXP` is a case-insensitive extended regular expression. The modules directory is refreshed before listing. `bits avail` delegates directly to `modulecmd bash avail`.

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
| `patches` | List of patch file names to apply, relative to the `patches/` directory inside the recipe repository. Patch files are copied to `$SOURCEDIR` and exposed as `$PATCH0`, `$PATCH1`, … before the recipe body runs. Each entry may optionally carry an inline checksum. |

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

#### Dependencies

| Field | Description |
|-------|-------------|
| `requires` | Runtime + build-time dependencies. |
| `build_requires` | Build-time-only dependencies (e.g. `cmake`, `ninja`). |
| `runtime_requires` | Runtime-only dependencies. |

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
| `always_load` | Set to `true` (alongside `provides_repository: true`) to clone this provider unconditionally at startup, before any dependency-graph traversal. Recipes in the provider's repository are then visible to all packages without requiring an explicit dependency. |
| `repository_position` | `append` (default) or `prepend` — where to insert the cloned directory in `BITS_PATH`. |

#### Memory-aware parallelism

| Field | Description |
|-------|-------------|
| `mem_per_job` | Expected peak RSS per parallel compilation process. Accepts a plain integer (MiB) or a string with a unit suffix: `512`, `"1500"`, `"1.5 GiB"`, `"2 GB"`. When set, bits samples available system memory at the start of the package's build and lowers `$JOBS` to `min(requested, floor(available × utilisation / mem_per_job))`. Omitting the field leaves `$JOBS` unchanged. |
| `mem_utilisation` | Fraction of available memory bits may commit, in the range `0.0`–`1.0`. Default: `0.9`. Only used when `mem_per_job` is also set. |

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
| `sandbox_network` | Controls outgoing network access when the build script runs inside a sandbox. `on` (default) — network is **blocked**. `off` — network is **allowed** (useful for recipes that `pip install` or `gem install` at build time). Ignored when `--sandbox=off`. See [§22a Recipe Sandbox](#22a-recipe-sandbox). |

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

These variables are set automatically inside each package's Bash build script. They cannot be overridden by the recipe; they are injected by `build_template.sh` before the recipe body is sourced.

#### Core build paths

| Variable | Purpose |
|----------|---------|
| `$INSTALLROOT` | Install all files here (the final installation prefix). The directory is created by bits before the recipe runs. |
| `$BUILDDIR` | Temporary build directory inside `$BUILDROOT`. Created automatically. |
| `$SOURCEDIR` | Checked-out (or prepared) source directory. For git sources this is the working tree. For archive sources this is the directory to which archives are downloaded. |
| `$BUILDROOT` | Parent of `$BUILDDIR`; corresponds to `BUILD/<pkghash>/` in the work tree. |
| `$PKGPATH` | Relative path from the work directory to the install root, including any family segment: `<arch>[/<family>]/<pkg>/<version>-<revision>`. Useful for constructing paths in modulefiles. |

#### Package identity

| Variable | Purpose |
|----------|---------|
| `$PKGNAME` | Package name as declared in the recipe. |
| `$PKGVERSION` | Package version string. |
| `$PKGREVISION` | Build revision (integer, incremented on each local rebuild). |
| `$PKGHASH` | Unique content-addressable build hash (hex string). |
| `$PKGFAMILY` | Install family (empty string if no family is assigned). Set by `package_family` in the defaults profile; see [Package families](#package-families). |
| `$BUILD_FAMILY` | The full `build_family` string, which may include the defaults combination used. |

#### Architecture

| Variable | Purpose |
|----------|---------|
| `$ARCHITECTURE` | Build-platform architecture string (e.g. `ubuntu2204_x86-64`). Always reflects the real build host, even for shared packages. |
| `$EFFECTIVE_ARCHITECTURE` | Effective installation architecture. Equals `$ARCHITECTURE` for normal packages; equals `shared` for packages marked `architecture: shared`. Use this in paths that should land under the shared tree. |

#### Parallelism

| Variable | Purpose |
|----------|---------|
| `$JOBS` | Number of parallel compilation jobs. Derived from `-j <n>` and optionally reduced by `mem_per_job` / `mem_utilisation` if the system has less free memory than the requested parallelism would require. Always pass this to `make`, `cmake --build`, `ninja`, etc. |

#### Source archives

When the recipe uses the `sources:` field, bits downloads each archive to `$SOURCEDIR` before the recipe runs and sets:

| Variable | Purpose |
|----------|---------|
| `$SOURCE0` | Filename (basename) of the first archive. |
| `$SOURCE1` | Filename of the second archive (if present). |
| `$SOURCEn` | Filename of the *n*-th archive (zero-indexed). |
| `$SOURCE_COUNT` | Total number of source archives. `0` when no `sources:` field is present. |

Example usage:

```bash
# Unpack the primary archive
tar -xzf "$SOURCEDIR/$SOURCE0" -C "$BUILDDIR"

# Unpack a supplementary data archive
if [ "$SOURCE_COUNT" -gt 1 ]; then
  tar -xzf "$SOURCEDIR/$SOURCE1" -C "$BUILDDIR/data"
fi
```

#### Patch files

When the recipe uses the `patches:` field, the patch files are made available in `$SOURCEDIR` and:

| Variable | Purpose |
|----------|---------|
| `$PATCH0` | Filename (basename) of the first patch file. |
| `$PATCH1` | Filename of the second patch file (if present). |
| `$PATCHn` | Filename of the *n*-th patch file (zero-indexed). |
| `$PATCH_COUNT` | Total number of patch files. `0` when no `patches:` field is present. |

Applying patches in a build script:

```bash
cd "$SOURCEDIR"
for i in $(seq 0 $(( PATCH_COUNT - 1 ))); do
  eval patch_file="\$PATCH$i"
  patch -p1 < "$SOURCEDIR/$patch_file"
done
```

#### Dependencies

| Variable | Purpose |
|----------|---------|
| `$REQUIRES` | Space-separated list of runtime + build-time dependencies for this package. |
| `$BUILD_REQUIRES` | Space-separated list of build-time-only dependencies. |
| `$RUNTIME_REQUIRES` | Space-separated list of runtime-only dependencies. |
| `$FULL_REQUIRES` | Full transitive closure of `requires` (all levels). |
| `$FULL_BUILD_REQUIRES` | Full transitive closure of `build_requires`. |
| `$FULL_RUNTIME_REQUIRES` | Full transitive closure of `runtime_requires`. |

For each dependency `DEP` that has been built, bits also sets `${DEP_ROOT}` to the absolute install path of that dependency, so recipes can reference dependency files directly (e.g. `$ZLIB_ROOT/include/zlib.h`).

#### Miscellaneous

| Variable | Purpose |
|----------|---------|
| `$COMMIT_HASH` | The git commit SHA that was checked out for the `source:` field. |
| `$INCREMENTAL_BUILD_HASH` | Non-zero when an incremental recipe is in use (development mode). |
| `$DEVEL_PREFIX` | Non-empty for development packages (the directory name of the devel source tree). |
| `$BITS_SCRIPT_DIR` | Absolute path to the bits installation directory. Useful for referencing helpers shipped with bits. |

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
| `qualify_arch` | Set to `true` to append the defaults combination to the install architecture string; see [Qualifying the install architecture](#qualifying-the-install-architecture) below. |
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



---

### Qualifying the install architecture

By default all packages built with any set of defaults land under the same architecture directory (e.g. `sw/slc7_x86-64/`). If you maintain two profiles that are **incompatible with each other** — for example `gcc12` and `gcc13` — builds from one profile will silently overwrite the install tree of the other.

Setting `qualify_arch: true` in a defaults file instructs bits to **append the defaults combination to the architecture string**, producing a unique install prefix per combination. For example:

```
bits build --defaults dev::gcc13 MyPackage
```

with `qualify_arch: true` in `defaults-gcc13.sh` installs everything under:

```
sw/slc7_x86-64-dev-gcc13/
```

instead of the plain `sw/slc7_x86-64/`. The `release` component is never appended (it is the implicit baseline); all other components are joined with `-` in the order they appear on the command line.

#### How it works

After merging all defaults files, bits calls `compute_combined_arch()` to derive the effective install prefix:

```python
compute_combined_arch(defaultsMeta, args.defaults, raw_arch)
# e.g. ("slc7_x86-64", ["dev", "gcc13"]) → "slc7_x86-64-dev-gcc13"
```

This combined string is used for:

- **Install tree** — `sw/<combined_arch>/<package>/<version>-<revision>/`
- **`BITS_ARCH_PREFIX` default** in every `init.sh` — so the environment resolves to the right prefix at runtime
- **`$EFFECTIVE_ARCHITECTURE`** passed to the build script
- **`TARS/<combined_arch>/`** symlink directories and store paths — tarballs are keyed on the combined arch, ensuring they do not collide with tarballs from builds using a different defaults combination

The original platform architecture (`slc7_x86-64`) is still passed to the build script as **`$ARCHITECTURE`** (used for platform detection such as the macOS `${ARCHITECTURE:0:3}` check) and to system-package preference matching, so build scripts need no changes.

Packages that declare `architecture: shared` (see [§20](#20-architecture-independent-shared-packages)) are **unaffected** by `qualify_arch`: their effective architecture is always `shared` regardless of which defaults are active.

#### Example defaults file

```yaml
package: defaults-gcc13
version: v1
qualify_arch: true            # ← enables per-defaults isolation
env:
  CC: gcc-13
  CXX: g++-13
```

#### Cleaning up

The `bits clean` command accepts an explicit `-a`/`--architecture` flag. To clean a qualified-arch tree, pass the combined string:

```
bits clean -a slc7_x86-64-dev-gcc13
```


---

### Architecture-specific overlay

If a file named `defaults-<architecture>.sh` exists in the recipe repository (e.g. `defaults-osx_arm64.sh`), bits silently loads it and merges its header on top of the already-merged profile, skipping the `package` key to avoid a name clash. This is the mechanism for per-platform tweaks such as disabling packages that do not build on a particular OS.


---

### Merge semantics

When the `::` list contains more than one name (e.g. `--defaults release::alice`), `readDefaults()` processes them left to right and merges their YAML headers using `merge_dicts()`, which performs a deep merge:

- Scalar values: later profile wins.
- Lists: concatenated.
- Dicts: recursively merged.

This lets a project-level profile (`alice`) layer on top of a base profile (`release`) without duplicating common settings. Bits also validates that each component in the `::` list is present in any `valid_defaults` list found in the loaded recipes; it aborts with a clear error message if any component is incompatible.


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

Variables injected by bits into every package build script. See [§17 Build-time environment variables](#build-time-environment-variables) for the full reference including `$SOURCE0`/`$PATCHn`/`$PKGFAMILY` and dependency path variables.

| Variable | Purpose |
|----------|---------|
| `$INSTALLROOT` | Installation prefix. All package files go here. |
| `$BUILDDIR` | Temporary build working directory. |
| `$SOURCEDIR` | Checked-out source or downloaded archive directory. |
| `$JOBS` | Parallel job count (from `-j`, adjusted by `mem_per_job`). |
| `$PKGNAME` | Package name. |
| `$PKGVERSION` | Package version. |
| `$PKGHASH` | Content-addressable build hash. |
| `$PKGFAMILY` | Install family (empty if no family assigned). |
| `$ARCHITECTURE` | Real build-host architecture string. |
| `$EFFECTIVE_ARCHITECTURE` | `shared` for shared packages, otherwise same as `$ARCHITECTURE`. |
| `$SOURCE_COUNT` | Number of source archives (0 if no `sources:` field). |
| `$PATCH_COUNT` | Number of patch files (0 if no `patches:` field). |
| `$BITS_PROVIDERS` | URL or comma-separated list of URLs identifying the active provider repository set. Set from `BITS_PROVIDERS` env var, `providers` key in `bits.rc`, or built-in default. |

### Build and configuration variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `BITS_BRANDING` | `bits` | Tool branding string used in log output. |
| `BITS_ORGANISATION` | `ALICE` | Organisation name used in config lookup. |
| `BITS_PKG_PREFIX` | `VO_ALICE` | Package-name prefix shown by `bits q`. |
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
| `cvmfs://REPO/PATH` | CernVM File System | ✓ | — | None (read-only filesystem) |
| `rsync://HOST/PATH` or `/local/path` | rsync | ✓ | ✓ | SSH keys (`~/.ssh/`) or filesystem permissions |

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

Upload order is designed to avoid partial-artifact races: the main package symlink is written first (reserving the revision number), then all dependency-set symlinks are uploaded in parallel, and the final tarball is written last. A downloader that finds the symlink but not yet the tarball simply waits for the next build cycle.

#### CernVM File System (`cvmfs://`)

Read-only. Instead of unpacking a remote tarball, bits creates a small local tarball containing symlinks that point into the already-mounted CVMFS repository. The build environment is constructed from the CVMFS paths without copying data locally:

```bash
bits build --remote-store cvmfs://cvmfs.example.cern.ch/sw ROOT
```

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
| `CVMFSRemoteSync` | ✓ | — | Read-only filesystem mount; upload not supported. |

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

## 22a. Recipe Sandbox

Bits can run each recipe build script inside an isolated sandbox to limit the damage a malicious or buggy recipe can do. The sandbox wraps the actual `bash build.sh` execution — it does not affect source downloads, tarball extraction, or publishing.

### How it works

| Platform | Default sandbox | Mechanism |
|----------|-----------------|-----------|
| Linux (local build) | podman if available, otherwise `off` | Rootless `podman run` with `--userns=keep-id` |
| macOS (local build) | `sandbox-exec` if available, otherwise `off` | Built-in SBPL sandbox profile; no VM, no overhead |
| Any platform, `--docker` active | Nested podman inside the container, if available | `podman run` launched from inside the Docker build container |

The workDir is bind-mounted at the same absolute path inside the podman container so that all paths embedded in the build environment (`$WORK_DIR`, `$INSTALLROOT`, `$SOURCEDIR`, etc.) resolve correctly.

### Sandbox modes

Pass `--sandbox MODE` to `bits build`:

| Mode | Behaviour |
|------|-----------|
| `auto` | (default) Pick the best available option — podman on Linux, `sandbox-exec` on macOS, nested podman when `--docker` is active. Falls back to `off` with a warning if nothing is available. |
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

## 22b. Cross-compilation via QEMU

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

## 22c. bits verify — Deployment Verification

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

## 23. Forcing or Dropping the Revision Suffix (`force_revision`)

By default every installed package path and tarball filename includes a
**revision counter** assigned by bits, e.g.:

```
slc9_amd64/gcc/15.2.1-1
```

The trailing `-1` is the revision.  For some packages — notably CMS software
releases where the version string `CMSSW_13_0_0` is the authoritative label
used by downstream infrastructure — this suffix is undesirable.  The
`force_revision` feature lets you pin the revision to a specific value or drop
it entirely, **without touching the recipe file**.

---

### 23.1 Configuration mechanism

`force_revision` is set in a `defaults-*.sh` file, never in a recipe.  This
lets different groups reuse the same recipes while opting in or out
independently.

#### Per-package override

Use the `overrides:` block to target individual packages by regex:

```yaml
overrides:
  "cmssw_.*":
    force_revision: ""          # drop the revision suffix entirely
  "special-tool":
    force_revision: "rc1"       # pin to a literal string
```

When the regex matches a package name (case-insensitive), `spec["revision"]`
is set to the given value before any counter logic runs.

#### Global fallback

Add a top-level `force_revision:` field to apply to every package not already
matched by an override entry:

```yaml
# drops the revision suffix from every package in this defaults profile
force_revision: ""
```

A global value of `~` (YAML null) means "not set" and has no effect.

---

### 23.2 How the install path changes

| `force_revision` | Example install path |
|---|---|
| *(not set, default)* | `slc9_amd64/CMSSW_13_0_0/CMSSW_13_0_0-1` |
| `"1"` (pinned to 1) | `slc9_amd64/CMSSW_13_0_0/CMSSW_13_0_0-1` |
| `"rc1"` (literal) | `slc9_amd64/CMSSW_13_0_0/CMSSW_13_0_0-rc1` |
| `""` (empty, drop) | `slc9_amd64/CMSSW_13_0_0/CMSSW_13_0_0` |

The **content-addressed store path** (`TARS/<arch>/store/<h2>/<hash>/`) is
unaffected regardless of the value — binary integrity is always preserved via
the hash.

---

### 23.3 Risks and caveats

**Symlink overwrite risk (empty revision only)**

When `force_revision: ""` is used, two different builds of the same version
share the same install path.  The convenience symlinks (`latest`, `latest-*`)
will be silently overwritten by the later build.  The content-hash store entry
is NOT overwritten, so the binary itself is safe — but only the *last* build
will be accessible via the version-named path.

bits emits a runtime `WARNING` when it detects `force_revision: ""` on a
package.

**No `local` prefix protection**

Normally bits prefixes revision numbers with `local` (e.g. `local1`) when
there is no writable remote store, to avoid conflicts with a remote that might
assign the same integer revision.  When `force_revision` is set this prefix
logic is bypassed — the revision is used exactly as given.  If you use a
literal integer (e.g. `force_revision: "1"`) in a mixed local/remote workflow,
revision collision is possible.

**Shared across defaults profiles**

The `force_revision` value is read from the active defaults profile at build
time.  If you share a workspace between two groups that use different defaults
files — one with `force_revision: ""` and one without — the paths they install
to will differ.  Keep workspaces separate or agree on a common value.

---

### 23.4 Implementation notes

Internally bits computes the install-path segment with the helper:

```python
# bits_helpers/utilities.py
def ver_rev(spec):
    rev = spec.get("revision", "")
    return "{}-{}".format(spec["version"], rev) if rev else spec["version"]
```

Every place in the codebase that previously wrote
`"{version}-{revision}".format(**spec)` now calls `ver_rev(spec)` so that the
forced/dropped revision is honoured consistently across the install tree,
tarballs, symlinks, `init.sh`, dist trees, and all remote-store backends.

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

### Overview

The CVMFS publishing pipeline allows a package that has been built with
`bits build` to be pre-staged into CVMFS backend storage and published via
a fast, catalog-only transaction — instead of the conventional approach where
every file is compressed and hashed inside the transaction itself.

The key insight is that CVMFS content-addressed storage separates two
independent concerns: (a) ingesting file blobs into the backend and (b)
updating the SQLite catalog.  Only (b) requires an exclusive transaction.
By doing (a) ahead of time — in parallel, on separate hosts — the transaction
window shrinks to seconds regardless of package size.

**Pipeline stages and host responsibilities**

| Stage | Runs on | Tool |
|---|---|---|
| Build | Platform build host | `bits build` |
| Copy | Build host | `bits publish` (local rsync) |
| Relocate | Build host | `bits publish` → `relocate-me.sh` |
| Transfer | Build host → Ingestion host | `bits publish` (rsync + inotifywait) |
| Ingest | Ingestion host | `cvmfs-ingest` |
| Publish | Stratum-0 / publisher host | `cvmfs-publish.sh` |

The original INSTALLROOT produced by `bits build` is never modified.  All
relocation happens on a temporary copy that is discarded after transfer.

**Repositories**

- `bits` (this repository) — provides the `bits publish` command.
- [`bits-cvmfs-ingest`](https://github.com/bitsorg/bits-cvmfs-ingest) —
  provides the `cvmfs-ingest` Go daemon and `cvmfs-publish.sh`.
- `bits-workflows` — provides reusable GitHub Actions and GitLab CI pipeline
  definitions.

---

### bits publish

`bits publish` is a `bits` sub-command that orchestrates the build-host side
of the pipeline: copy, relocate, and stream to the ingestion spool.

```
bits publish PACKAGE [VERSION]
             --cvmfs-target PATH
             --spool [USER@HOST:]PATH
             [--work-dir WORKDIR]
             [--architecture ARCH]
             [--scratch-dir DIR]
             [--rsync-opts OPTS]
             [--no-relocate]
```

**Arguments**

| Argument / Flag | Required | Description |
|---|---|---|
| `PACKAGE` | yes | Package name, as used in the recipe (e.g. `absl`). |
| `VERSION` | no | Version string (e.g. `20230802.1-1`). Defaults to the latest build found under `WORKDIR`. |
| `--cvmfs-target PATH` | yes | Absolute path the package will occupy on CVMFS, e.g. `/cvmfs/sft.cern.ch/lcg/releases/absl/20230802.1/x86_64-el9`. This path is passed to `relocate-me.sh` as the new install prefix, unless `--no-relocate` is given. |
| `--spool` | yes | Ingestion spool root.  Either a local directory (`/var/spool/cvmfs-ingest`) or a remote rsync target (`user@host:/path`). |
| `--work-dir WORKDIR` | no | bits work directory.  Default: `sw` (or `$BITS_WORK_DIR`). |
| `--architecture ARCH` | no | Build architecture.  Default: auto-detected. |
| `--scratch-dir DIR` | no | Directory for the temporary CVMFS working copy.  Default: system temp dir. |
| `--rsync-opts OPTS` | no | Extra options passed verbatim to every `rsync` invocation, e.g. `"-e 'ssh -i ~/.ssh/my_key'"`. |
| `--no-relocate` | no | Skip the `relocate-me.sh` step and stream the installation tree to the spool as-is. Use this when the package was built with `--cvmfs-prefix` so its paths already match the deployment target. |

**What it does**

1. Locates the package's immutable INSTALLROOT under `WORKDIR` (via the
   `latest` symlink or by scanning for `VERSION`).
2. `rsync -a`-copies the INSTALLROOT to a scratch working copy.  The
   original is never touched again.
3. Starts an `inotifywait` watcher on the working copy (when available) so
   that files modified by relocation are queued for transfer immediately.
4. Runs `relocate-me.sh` in the working copy with `INSTALL_BASE` set to
   `--cvmfs-target`.  Relocation and transfer overlap in time.
5. Falls back to a single bulk rsync if `inotifywait` is unavailable.
6. Writes a `<pkg-id>.done` sentinel to `<spool>/incoming/`.  The sentinel
   carries the `pkg_id` and `cvmfs_target` so the ingestion daemon can
   operate without additional configuration.
7. Removes the scratch working copy.

**pkg-id format**

The package identifier used to name spool directories and manifests is:

```
<package>-<version_dir>-<arch_with_slashes_replaced_by_underscores>
```

Example: `absl-20230802.1-1-x86_64_el9`

**Example**

```bash
bits publish absl \
  --cvmfs-target /cvmfs/sft.cern.ch/lcg/releases/absl/20230802.1/x86_64-el9 \
  --spool ingestuser@ingest-host.example.com:/var/spool/cvmfs-ingest \
  --rsync-opts "-e 'ssh -i ~/.ssh/ingest_key'"
```

---

### bits-cvmfs-ingest — building from source

The ingestion daemon is a standalone Go project hosted at
[`github.com/bitsorg/bits-cvmfs-ingest`](https://github.com/bitsorg/bits-cvmfs-ingest).

**Prerequisites**

- Go 1.22 or newer (`go version` to check).
- Network access to download Go module dependencies (or a pre-populated
  module cache / GOPROXY).

**Clone and build**

```bash
git clone https://github.com/bitsorg/bits-cvmfs-ingest.git
cd bits-cvmfs-ingest
go mod tidy          # downloads and pins all dependencies; generates go.sum
go build ./cmd/cvmfs-ingest/
```

This produces a `cvmfs-ingest` binary in the current directory.

**Static binary for deployment**

The ingestion host typically runs a different Linux distribution from the
build host.  Build a fully static binary to avoid libc version mismatches:

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
  go build -o cvmfs-ingest ./cmd/cvmfs-ingest/
```

For AArch64 (e.g. an ARM ingestion node):

```bash
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 \
  go build -o cvmfs-ingest-aarch64 ./cmd/cvmfs-ingest/
```

**Install system-wide**

```bash
go install ./cmd/cvmfs-ingest/
# installs to $(go env GOPATH)/bin/cvmfs-ingest   (typically ~/go/bin/)
```

Add `$(go env GOPATH)/bin` to `PATH` or copy the binary to `/usr/local/bin`.

**Verify**

```bash
./cvmfs-ingest --help
```

---

### bits-cvmfs-ingest — configuration and running

`cvmfs-ingest` has no configuration file; all settings are passed as
command-line flags.

**Spool directory layout**

The daemon owns and manages these subdirectories under `--spool`:

```
<spool>/
  incoming/      ← rsync destination from build hosts
  processing/    ← package trees moved here atomically on .done arrival
  completed/     ← manifests (.manifest.json) and graft trees (.grafts/)
```

**Flags**

| Flag | Default | Description |
|---|---|---|
| `--spool PATH` | *(required)* | Root of the spool directory tree.  The daemon creates subdirectories automatically. |
| `--backend TYPE` | `local` | Backend type: `local` (filesystem) or `s3` (S3-compatible object store). |
| `--backend-path PATH` | *(required for local)* | Root path of the CVMFS backend filesystem, e.g. `/srv/cvmfs/sft.cern.ch`.  Blobs are written under `<path>/data/<hash[:2]>/<hash[2:]>`. |
| `--s3-bucket NAME` | *(required for s3)* | S3 bucket name. |
| `--s3-prefix PREFIX` | *(empty)* | Optional key prefix inside the bucket (no trailing slash). |
| `--s3-endpoint URL` | *(empty)* | Custom endpoint for S3-compatible stores (Ceph, MinIO, EOS S3). Leave empty for AWS S3. |
| `--s3-region REGION` | `us-east-1` | S3 region. |
| `--hash ALGO` | `sha1` | Content hash algorithm: `sha1` (CVMFS default) or `sha256`. Must match the repository's hash algorithm. |
| `--concurrency N` | `2×GOMAXPROCS` | Worker pool size for parallel compress+hash+upload. |
| `--once` | `false` | Process existing spool contents and exit without starting the watch loop.  Used by CI jobs. |
| `--log-level LEVEL` | `info` | Log verbosity: `debug`, `info`, `warn`, `error`. |

**S3 credentials** are read from the standard AWS credential chain:
environment variables (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`),
`~/.aws/credentials`, or an IAM instance role.

**Daemon mode — local backend**

```bash
cvmfs-ingest \
  --spool        /var/spool/cvmfs-ingest \
  --backend      local \
  --backend-path /srv/cvmfs/sft.cern.ch \
  --hash         sha1 \
  --concurrency  8 \
  --log-level    info
```

The daemon watches `incoming/` for `.done` sentinels and processes packages
as they arrive.  Send `SIGTERM` or `SIGINT` (Ctrl-C) for a clean shutdown.

**Daemon mode — S3 backend**

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

cvmfs-ingest \
  --spool        /var/spool/cvmfs-ingest \
  --backend      s3 \
  --s3-bucket    cvmfs-backend \
  --s3-prefix    sft.cern.ch \
  --s3-endpoint  https://s3.cern.ch \
  --hash         sha1 \
  --concurrency  16
```

**Once mode — for CI jobs**

```bash
cvmfs-ingest \
  --spool        /var/spool/cvmfs-ingest \
  --backend      local \
  --backend-path /srv/cvmfs/sft.cern.ch \
  --once
```

Processes all packages whose sentinel has arrived and exits with code `0` on
success or non-zero if any package failed.

**Restart safety**

On startup, the daemon scans `processing/` for any directories left by a
previously interrupted run and re-ingests them.  Blob uploads are idempotent
(existing blobs are detected via `HEAD` / `stat` and skipped), so re-running
on a partially-ingested package is safe.

**Output — completed manifest**

For each successfully ingested package, the daemon writes:

```
<spool>/completed/<pkg-id>.manifest.json   ← consumed by cvmfs-publish.sh
<spool>/completed/<pkg-id>.grafts/         ← graft sidecar tree
```

The manifest is a JSON document:

```json
{
  "pkg_id":          "absl-20230802.1-1-x86_64_el9",
  "cvmfs_target":    "/cvmfs/sft.cern.ch/lcg/releases/absl/20230802.1/x86_64-el9",
  "grafts_dir":      "/var/spool/cvmfs-ingest/completed/absl-20230802.1-1-x86_64_el9.grafts",
  "created_at":      "2026-04-12T14:23:00Z",
  "file_count":      1842,
  "total_size_bytes": 312456192,
  "files": [
    {
      "rel_path":        "lib/libabsl_base.so.2308021",
      "hash":            "a3f1...",
      "hash_algo":       "sha1",
      "size":            204800,
      "compressed_size": 98304,
      "blob_key":        "a3/f1..."
    }
  ]
}
```

---

### cvmfs-publish.sh — the publisher script

`cvmfs-publish.sh` is a shell script that opens a CVMFS transaction, places
the pre-staged graft tree into the repository mount point, and publishes.
It lives in the `bits-cvmfs-ingest` repository and must run on the
stratum-0 host (or a host with write access to the CVMFS transaction lock).

**Usage**

```bash
bash cvmfs-publish.sh \
  --repo     sft.cern.ch \
  --manifest /var/spool/cvmfs-ingest/completed/absl-20230802.1-1-x86_64_el9.manifest.json \
  [--dry-run]
```

| Flag | Required | Description |
|---|---|---|
| `--repo NAME` | yes | CVMFS repository name (e.g. `sft.cern.ch`). |
| `--manifest PATH` | yes | Path to the `.manifest.json` written by `cvmfs-ingest`. |
| `--dry-run` | no | Print what would happen without opening a transaction. |

**What it does**

1. Parses `cvmfs_target` and `grafts_dir` from the manifest.
2. Opens a `cvmfs_server transaction <repo>`.
3. `rsync`s the graft tree (empty file stubs and `.cvmfsgraft-*` sidecars —
   no bulk file content) into `<repo-mount>/<cvmfs_target>/`.
4. Calls `cvmfs_server publish <repo>`.  Because all blobs are already in
   the backend, the catalog update completes in seconds.
5. Aborts the transaction cleanly via `cvmfs_server abort -f` on any error.

**Batching multiple packages**

To minimise the number of transactions, call `cvmfs-publish.sh` once per
package in rapid succession or wrap multiple calls in a single transaction
manually.  The catalog update overhead per package is small once the
transaction is already open.

---

### CI/CD integration

Reusable workflow definitions are provided in the `bits-workflows` repository.

#### GitHub Actions

Add to your workflow:

```yaml
- uses: actions/checkout@v4
  with:
    repository: bitsorg/bits-workflows
    path: bits-workflows

# Or use the workflow directly via workflow_dispatch:
# .github/workflows/cvmfs-publish.yml in bits-workflows
```

The `cvmfs-publish.yml` workflow accepts these inputs via `workflow_dispatch`
(or the GitHub API / SPA web UI):

| Input | Description |
|---|---|
| `package` | Package name (e.g. `absl`). |
| `version` | Version string (optional — defaults to latest build). |
| `platform` | Runner label, e.g. `x86_64-el9`. |
| `cvmfs_target` | Final CVMFS install path. |
| `rebuild` | Force rebuild (`true`/`false`). |

Required repository **secrets**:

| Secret | Description |
|---|---|
| `SPOOL_SSH_KEY` | SSH private key for rsync to the ingestion host. |
| `SPOOL_USER` | SSH username on the ingestion host. |
| `SPOOL_HOST` | Ingestion host address. |
| `SPOOL_PATH` | Absolute spool root path on the ingestion host. |
| `CVMFS_REPO` | CVMFS repository name. |

Required repository **variables** (Settings → CI/CD → Variables):

| Variable | Default | Description |
|---|---|---|
| `CVMFS_BACKEND_TYPE` | `local` | `local` or `s3`. |
| `CVMFS_BACKEND_PATH` | — | Local backend root path. |
| `CVMFS_HASH_ALGO` | `sha1` | `sha1` or `sha256`. |
| `INGEST_CONCURRENCY` | `0` | Worker count (`0` = auto). |

**Self-hosted runner labels** that must be registered:

| Label | Used by |
|---|---|
| `bits-build-<platform>` | Build + publish job (e.g. `bits-build-x86_64-el9`) |
| `bits-ingest` | Ingestion job |
| `bits-cvmfs-publisher` | CVMFS transaction job |

#### GitLab CI

Include the pipeline from `bits-workflows`:

```yaml
# .gitlab-ci.yml in your project
include:
  - project: bitsorg/bits-workflows
    file: .gitlab/cvmfs-publish.yml
    ref: main
```

**Normal usage — bits-console.** The intended way to trigger this pipeline is through **[bits-console](https://bits-console.web.cern.ch)**. bits-console reads the community's `ui-config.yaml`, presents the package browser and platform selector in the browser, and calls the GitLab pipeline API on the user's behalf. The role distinction between production builds (`group-admin` / `bits-admin`) and personal-area builds (`group-user`) is enforced server-side by the pipeline based on `GITLAB_USER_LOGIN` against the `GROUP_ADMINS_<NAME>` CI variable — bits-console surfaces this as two separate buttons (**Build → Production** vs **Build → Personal area**).

**Programmatic or direct triggering.** For CI automation outside bits-console (e.g. a nightly cron or a downstream pipeline), the GitLab pipeline API can be called directly. The same role enforcement applies — the token owner's GitLab identity determines which targets are permitted:

```bash
curl --request POST \
     --form "token=$CI_JOB_TOKEN" \
     --form "ref=main" \
     --form "variables[PACKAGE]=absl" \
     --form "variables[PLATFORM]=x86_64-el9" \
     --form "variables[CVMFS_TARGET]=/cvmfs/sft.cern.ch/lcg/releases/absl/20230802.1/x86_64-el9" \
     "https://gitlab.cern.ch/api/v4/projects/<id>/trigger/pipeline"
```

---

### bits-console — web interface for the GitLab-driven pipeline

**bits-console** is a GitLab Pages single-page application that provides a browser-based interface to the CVMFS publishing pipeline. It is hosted at `https://bits-console.web.cern.ch` and backed by the private GitLab project `gitlab.cern.ch/bitsorg/bits-console`.

Instead of crafting raw API calls or navigating the GitLab web UI, operators and users interact with a purpose-built console that:

- Browses all packages in the community's recipe repositories (live, directly from GitHub).
- Shows the current CVMFS publication status of each package.
- Allows **production builds** (published to the community's `cvmfs_prefix`) for group-admins and bits-admins.
- Allows **personal-area builds** (published to `cvmfs_user_prefix/<username>/…`) for all authenticated users.
- Provides a pipeline log viewer, scheduled-build management, and per-community settings.

#### Architecture at a glance

```
bits-console (GitLab Pages SPA)
  │
  ├── communities/<name>/ui-config.yaml   ← per-community settings
  │
  └── triggers GitLab CI pipeline (.gitlab/cvmfs-publish.yml)
        │
        ├── Stage 1: bits build   (build runner, bits CLI, Docker)
        │     └── bits cleanup --disk-pressure-only  (pre-build guard)
        │     └── bits build --docker [--cvmfs-prefix] <PACKAGE>
        │     └── bits publish [--no-relocate]  → rsync → spool
        │
        ├── Stage 2: cvmfs-ingest  (ingestion host, bits-cvmfs-ingest daemon)
        │
        └── Stage 3: cvmfs-publish.sh  (stratum-0, CVMFS transaction)
```

#### The community configuration file (`ui-config.yaml`)

Each community's behaviour is driven by `communities/<name>/ui-config.yaml`. The key fields that control the build and cache pipeline are:

| Field | Default | Description |
|---|---|---|
| `cvmfs_prefix` | _(required)_ | Production CVMFS install prefix (e.g. `/cvmfs/sft.cern.ch/lcg/releases`). Passed as `--cvmfs-prefix` to `bits build` and as `--cvmfs-target` base to `bits publish`. |
| `cvmfs_user_prefix` | _(required)_ | Personal-area prefix for non-admin user builds. |
| `cvmfs_repo` | _(required)_ | CVMFS repository name (e.g. `sft.cern.ch`). |
| `platforms` | _(required)_ | Pipe-separated `<label>\|<docker-image>` pairs, one per line. The `<label>` becomes the runner tag (`bits-build-<label>`) and the platform selector in the UI. |
| `build_parallelism_enabled` | `"false"` | Enable parallel dependency-graph builds. |
| `build_parallelism_mode` | `"makeflow"` | `"makeflow"` (external Makeflow engine) or `"builders"` (built-in Python scheduler). |
| `build_parallelism_builders` | `"4"` | Number of simultaneous package builds when `mode` is `"builders"`. |
| `cache_max_age_days` | `"7"` | Passed to `bits cleanup --max-age`. Set to `"0"` to disable age-based eviction. |
| `cache_min_free_gb` | `"50"` | Free-space threshold for the pre-build disk-pressure cleanup pass. |

Full field reference and example configurations are in the bits-console `REFERENCE.md`, accessible at `https://bits-console.web.cern.ch/REFERENCE.md` or in the `bits-console` GitLab repository.

#### Pipeline variables used by the GitLab pipeline

When bits-console triggers a pipeline it passes the following variables to `cvmfs-publish.yml`:

| Variable | Description |
|---|---|
| `PACKAGE` | Package name to build and publish. |
| `VERSION` | Version string (optional; defaults to latest). |
| `PLATFORM` | Runner platform label (e.g. `x86_64-el9`). |
| `CVMFS_TARGET` | Final CVMFS install path for this package version. |
| `CVMFS_PREFIX` | Community CVMFS releases prefix. When set, `bits build` uses `--cvmfs-prefix` and `bits publish` uses `--no-relocate`. Leave unset for the traditional build-then-relocate workflow. |
| `REBUILD` | Set to `"true"` to force a rebuild even if the package is already installed. |
| `PARALLEL_BUILD` | Set to `"true"` to enable parallel dependency-graph builds. |
| `PARALLEL_MODE` | `"makeflow"` or `"builders"`. |
| `PARALLEL_BUILDERS` | Number of builders for `"builders"` mode. |
| `JOBS` | Compiler threads per package (`-j N`). `0` = auto-detect. |
| `CACHE_MIN_FREE_GB` | Free-space threshold for the pre-build cleanup guard. |

CI/CD project secrets (`SPOOL_SSH_KEY`, `SPOOL_USER`, `SPOOL_HOST`, `SPOOL_PATH`, `CVMFS_REPO`, etc.) are configured once in GitLab **Settings → CI/CD → Variables** and are never exposed to the browser.

#### Getting access and further documentation

Access is granted by a bits-admin via GitLab project membership (Reporter for browsing, Developer for triggering builds). Full documentation — including how to add a community, configure runners, manage the CVMFS path model, and administer the SPA — is in:

- **bits-console README** — quick-start, roles, runner setup, and community configuration.
- **bits-console REFERENCE** — complete `ui-config.yaml` field reference, JavaScript module API, deployment guide, and security model.

Both documents are maintained alongside the application code in the `bits-console` GitLab repository at `https://gitlab.cern.ch/bitsorg/bits-console`.
