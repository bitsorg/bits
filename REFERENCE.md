# Bits Build Tool — Reference Manual

## Table of Contents

### Part I — User Guide
1. [Introduction](#1-introduction)
2. [Installation & Prerequisites](#2-installation--prerequisites)
3. [Quick Start](#3-quick-start)
4. [Configuration](#4-configuration)
5. [Building Packages](#5-building-packages)
6. [Managing Environments](#6-managing-environments)
7. [Cleaning Up](#7-cleaning-up)
8. [Practical Scenarios](#8-practical-scenarios)

### Part II — Developer Guide
9. [Architecture Overview](#9-architecture-overview)
10. [Setting Up a Development Environment](#10-setting-up-a-development-environment)
11. [Key Source Files](#11-key-source-files)
12. [Writing Recipes](#12-writing-recipes)
13. [Repository Provider Feature](#13-repository-provider-feature)
14. [Writing and Running Tests](#14-writing-and-running-tests)
15. [Contributing](#15-contributing)

### Part III — Reference Guide
16. [Command-Line Reference](#16-command-line-reference)
17. [Recipe Format Reference](#17-recipe-format-reference)
18. [Environment Variables](#18-environment-variables)
19. [Remote Binary Store Backends](#19-remote-binary-store-backends)
20. [Docker Support](#20-docker-support)
21. [Design Principles & Limitations](#21-design-principles--limitations)

---

# Part I — User Guide

## 1. Introduction

**Bits** is a build orchestration and dependency management tool for complex software stacks. It originated from `aliBuild`, developed for the ALICE/ALFA software at CERN, and is designed for communities that need to build and maintain large collections of interdependent packages with reproducibility, parallelism, and minimal overhead.

Bits is **not** a traditional package manager like `apt` or `conda`. Instead it automates fetching sources, resolving dependencies, building, and installing software in a controlled, reproducible environment. Each package is described by a *recipe* — a plain-text file with a YAML metadata header and a Bash build script — stored in a version-controlled recipe repository.

Key capabilities at a glance:

- Automatic topological dependency resolution and ordering
- Content-addressable incremental builds — only rebuilds what changed
- Parallel package builds and multi-core compilation
- Remote binary stores (HTTP, S3, CVMFS, rsync) to share pre-built artifacts
- Docker-based builds for cross-compilation or reproducible CI environments
- Git and Sapling SCM support
- Dynamic recipe repositories loaded at dependency-resolution time

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

## 4. Configuration

Bits reads an INI-style configuration file at startup, searching in this order:

1. File given via `--config=FILE`
2. `bits.rc` in the current directory
3. `.bitsrc` in the current directory
4. `~/.bitsrc` in the home directory

### Example configuration

```ini
[bits]
  organisation = ALICE

[ALICE]
  # Prefix shown when listing packages with 'bits q'
  pkg_prefix   = VO_ALICE

  # Root directory for all build products
  sw_dir       = sw

  # Directory that contains the checked-out recipe repositories
  repo_dir     = repositories

  # Comma-separated list of recipe repository names to search.
  # Each name is resolved to <repo_dir>/<name>.bits on disk.
  search_path  = alice,bits,general,simulation,hepmc,analysis,ml
```

Every setting can also be overridden by an environment variable — see [§18 Environment Variables](#18-environment-variables) for the full list.

---

## 5. Building Packages

```bash
bits build [options] PACKAGE [PACKAGE ...]
```

Bits resolves the full transitive dependency graph of each requested package, computes a content-addressable hash for every node, downloads any pre-built artifacts that already exist in a remote store, and builds the rest in topological order.

### Common options

| Option | Description |
|--------|-------------|
| `--defaults PROFILE` | Defaults profile (recipe `defaults-PROFILE.sh`). Default: `release`. |
| `-j N`, `--jobs N` | Parallel compilation jobs per package. Default: CPU count. |
| `--builders N` | Number of packages to build simultaneously. Default: 1. |
| `-u`, `--fetch-repos` | Update all source mirrors before building. |
| `-w DIR`, `--work-dir DIR` | Work/output directory. Default: `sw`. |
| `--remote-store URL` | Binary store to pull pre-built tarballs from. |
| `--write-store URL` | Binary store to push newly-built tarballs to. |
| `--force` | Rebuild even if the package hash already exists. |
| `--docker` | Build inside a Docker container. |
| `--debug` | Verbose debug output. |
| `--dry-run` | Print what would happen without executing. |
| `--keep-tmp` | Preserve build directories after success (useful for debugging). |

### How a build proceeds

1. **Recipe discovery** — Bits locates `<package>.sh` in each directory on `search_path` (appending `.bits` to each name). Repository-provider packages (see [§13](#13-repository-provider-feature)) are cloned first to extend the search path before the main resolution pass.
2. **Dependency resolution** — `requires`, `build_requires`, and `runtime_requires` fields are read recursively, forming a DAG. Cycles are reported as errors.
3. **Hash computation** — A hash is computed for each package from its recipe text, source commit, dependency hashes, and environment. Packages with a matching hash in a store are downloaded instead of rebuilt.
4. **Source fetching** — Source repositories are cloned into a local mirror and then checked out into a build area. Up to 8 repositories are fetched in parallel.
5. **Build execution** — Each package's Bash script runs in an isolated environment with sanitised locale and only its declared dependencies visible.
6. **Post-build** — A modulefile and a versioned tarball are written; the tarball may be uploaded to a write store.

---

## 6. Managing Environments

Bits uses the standard Environment Modules system (`modulecmd`) to manage runtime environments. A *module* corresponds to one built package version.

### Enter a sub-shell with modules loaded

```bash
bits enter ROOT/latest
# A new sub-shell opens with ROOT and all its dependencies in PATH etc.
exit   # return to your normal shell
```

Options for `bits enter`:
- `--shellrc` — source your shell startup file (`.bashrc`, `.zshrc`) in the new shell.
- `--dev` — also load development-mode variables from `etc/profile.d/init.sh`.

### Load / unload in the current shell

```bash
# Integrate once in ~/.bashrc or ~/.zshrc:
BITS_WORK_DIR=/path/to/sw
eval "$(bits shell-helper)"

# Then in any shell session:
bits load ROOT/latest        # adds ROOT to the current environment
bits unload ROOT             # removes it
bits list                    # show currently loaded modules
bits q [REGEXP]              # list all available modules
```

Without `shell-helper` you must use `eval`:

```bash
eval "$(bits load ROOT/latest)"
eval "$(bits unload ROOT)"
```

### Run a single command in a module environment

```bash
bits setenv ROOT/latest -c root -b
```

---

## 7. Cleaning Up

```bash
bits clean [options]
```

| Option | Description |
|--------|-------------|
| `-w DIR` | Work directory to clean. Default: `sw`. |
| `-a ARCH` | Restrict to this architecture. |
| `--aggressive-cleanup` | Also remove source mirrors and distribution tarballs. |
| `-n`, `--dry-run` | Show what would be removed without deleting. |

The default (non-aggressive) clean removes the `TMP/` staging area, stale `BUILD/` directories (those without a `latest` symlink), and stale versioned installation directories. Aggressive cleanup additionally removes source mirrors and `TARS/` content.

---

## 8. Practical Scenarios

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
# CI: build and upload
bits build --write-store s3://mybucket/builds ROOT

# Developer: download instead of rebuilding
bits build --remote-store s3://mybucket/builds ROOT
```

### Parallel build

```bash
bits build --builders 4 --jobs 8 my_large_stack
# 4 independent packages built at once, each using 8 cores
```

### Build for a different Linux version (Docker)

```bash
bits build --docker --architecture ubuntu2004_x86-64 ROOT
```

### Generate a dependency graph

```bash
bits deps --outgraph deps.pdf ROOT   # requires Graphviz
```

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
| `bits_helpers/clean.py` | `bits clean` — stale artifact removal |
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

### How providers are discovered

Before the main `getPackageList` call, `bits build` runs `fetch_repo_providers_iteratively`:

1. Walk the dependency graph from the requested packages.
2. When a package with `provides_repository: true` is encountered for the first time, clone its source repository into the cache and add the checkout to `BITS_PATH`.
3. Restart the walk — recipes newly visible on the extended path (including further providers) are now reachable.
4. Repeat until stable (no new providers found) or until `MAX_PROVIDER_ITERATIONS` (20) is reached.

This naturally handles **nested providers**: a provider whose own recipe repository contains a further provider recipe.

### Cache layout

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
| `test_args.py` | CLI argument parsing |
| `test_build.py` | `doBuild` integration, hash computation, build script generation |
| `test_clean.py` | Stale-artifact detection and removal |
| `test_cmd.py` | `DockerRunner` and subprocess helpers |
| `test_deps.py` | Dependency graph generation |
| `test_git.py` | Git SCM wrapper |
| `test_sync.py` | Remote store backends (requires `botocore` for S3 tests) |
| `test_repo_provider.py` | Repository provider: `getConfigPaths` absolute paths, `_add_to_bits_path`, `clone_or_update_provider` caching, iterative discovery, nested providers, hash propagation |

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
| `--defaults PROFILE` | Defaults profile (`defaults-PROFILE.sh`). Default: `release`. |
| `-a ARCH`, `--architecture ARCH` | Target architecture. Default: auto-detected. |
| `--force-unknown-architecture` | Proceed even if architecture is unrecognised. |
| `-j N`, `--jobs N` | Parallel compilation jobs per package. Default: CPU count. |
| `--builders N` | Packages to build simultaneously. Default: 1. |
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
| `--docker-image IMAGE` | Docker image to use. |
| `--docker-extra-args ARGS` | Extra arguments for `docker run`. |
| `--force` | Rebuild even if the package hash already exists. |
| `--keep-tmp` | Keep temporary build directories after success. |
| `--resource-monitoring` | Enable per-package CPU/memory monitoring. |
| `--resources FILE` | JSON resource-utilisation file for scheduling. |
| `--check-checksums` | Verify checksums declared in `sources`/`patches` entries; emit a warning on mismatch but continue the build. |
| `--enforce-checksums` | Verify checksums declared in `sources`/`patches` entries; abort the build on any mismatch or if a checksum is missing for a file. |
| `--print-checksums` | Compute and print the checksum of every downloaded source/patch file (useful for populating recipes). No verification is performed. |
| `--write-checksums` | After downloading sources and patches, write (or update) `checksums/<package>.checksum` in the recipe directory. Also records the pinned git commit SHA for packages using `source:` + `tag:`. Independent of the `--*-checksums` verification flags. |

The three `--*-checksums` flags are mutually exclusive. `--print-checksums` has the highest precedence when determining the active mode, followed by `--enforce-checksums`, then `--check-checksums`. A per-recipe `enforce_checksums: true` field (see [§17](#17-recipe-format-reference)) acts like `--enforce-checksums` for that package only. `--write-checksums` is independent and can be combined with any of the above.

---

### bits deps

Generate a visual dependency graph for a package (requires Graphviz).

```bash
bits deps [options] PACKAGE
```

| Option | Description |
|--------|-------------|
| `--outgraph FILE` | Output PDF file (required). |
| `--defaults PROFILE` | Defaults profile to use. |
| `-a ARCH` | Architecture for dependency resolution. |
| `--disable PACKAGE` | Exclude PACKAGE from the graph (repeatable). |
| `--prefer-system` | Mark system-provided packages differently. |
| `--no-system` | Treat all packages as needing to be built. |

Colour coding in the generated graph: **gold** = requested top-level package; **green** = runtime-only dependency; **purple** = build-only dependency; **tomato** = both runtime and build dependency.

---

### bits doctor

Check that the system satisfies all requirements for the requested packages.

```bash
bits doctor [options] PACKAGE [PACKAGE ...]
```

Evaluates each package's `system_requirement` and `prefer_system` snippets and reports results with colour-coded pass/warn/fail output.

---

### bits init

Create a writable local source checkout for development work.

```bash
bits init [options] PACKAGE[@VERSION][,PACKAGE[@VERSION]...]
```

| Option | Description |
|--------|-------------|
| `--dist REPO@TAG` | Recipe repository. Default: `alisw/alidist@master`. |
| `-z PREFIX`, `--devel-prefix PREFIX` | Directory for development checkouts. |
| `--reference-sources DIR` | Mirror directory to speed up cloning. |
| `-a ARCH` | Architecture. |
| `--defaults PROFILE` | Defaults profile. |

After `bits init`, the created directory is automatically used as the source for subsequent `bits build` invocations of that package.

---

### bits clean

Remove stale build artifacts.

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

### bits enter / load / unload / setenv

```bash
bits enter [--shellrc] [--dev] MODULE[,MODULE2...]
eval "$(bits load MODULE[,MODULE2...])"
eval "$(bits unload MODULE)"
bits setenv MODULE[,MODULE2...] -c COMMAND [ARGS...]
```

All four commands drive `modulecmd` behind the scenes. `bits enter` spawns a new interactive sub-shell; `bits load` / `bits unload` print shell code that must be `eval`'d (or used with `bits shell-helper`).

---

### bits query / list / avail

```bash
bits q [REGEXP]    # list available modules (optionally filtered)
bits list          # show currently loaded modules
bits avail         # show all modules via modulecmd avail
```

---

### bits shell-helper

```bash
# Add once to ~/.bashrc or ~/.zshrc:
BITS_WORK_DIR=<path_to_sw_dir>
eval "$(bits shell-helper)"
```

After this, `bits load` and `bits unload` modify the current shell's environment directly, without requiring `eval`.

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
| `source` | Git or Sapling repository URL. |
| `tag` | Tag, branch, or commit to check out. Supports date substitutions. |
| `sources` | List of source archive URLs to download. Each entry may optionally carry an inline checksum (see [Checksum verification](#checksum-verification) below). |
| `patches` | List of patch file names to apply (relative to `patches/`). Each entry may optionally carry an inline checksum. |

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

When `provides_repository: true` is set, the package's `source` URL must point to a git repository containing recipe files. It will be cloned before the main build and its directory added to `BITS_PATH`. See [§13](#13-repository-provider-feature) for full details.

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

### Build-time environment variables

These variables are set automatically inside each package's Bash build script:

| Variable | Purpose |
|----------|---------|
| `$INSTALLROOT` | Install all files here (the final installation prefix). |
| `$BUILDDIR` | Temporary build directory. |
| `$SOURCEDIR` | Checked-out source directory. |
| `$JOBS` | Number of parallel compilation jobs (from `-j`). |
| `$PKGNAME` | Package name. |
| `$PKGVERSION` | Package version. |
| `$PKGHASH` | Unique content-addressable build hash. |
| `$ARCHITECTURE` | Target architecture string (e.g. `ubuntu2204_x86-64`). |

---

## 18. Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `BITS_BRANDING` | `bits` | Tool branding string used in log output. |
| `BITS_ORGANISATION` | `ALICE` | Organisation name used in config lookup. |
| `BITS_PKG_PREFIX` | `VO_ALICE` | Package-name prefix shown by `bits q`. |
| `BITS_REPO_DIR` | `alidist` | Root directory for recipe repositories. |
| `BITS_WORK_DIR` | `sw` | Output and work directory. |
| `BITS_PATH` | _(empty)_ | Comma-separated list of additional recipe search directories. Absolute paths are used directly; relative names have `.bits` appended and are resolved under `BITS_REPO_DIR`. |

---

## 19. Remote Binary Store Backends

| URL scheme | Backend | Access |
|------------|---------|--------|
| `http://` or `https://` | HTTP | Read-only; exponential-backoff retries |
| `s3://BUCKET/PATH` | Amazon S3 (AWS CLI) | Read and write |
| `b3://BUCKET/PATH` | S3-compatible via `boto3` | Read and write |
| `cvmfs://REPO/PATH` | CernVM File System | Read-only |
| `rsync://HOST/PATH` or local path | rsync | Read and write |

The path layout under the store root mirrors the local `TARS/` directory:

```
<store-root>/TARS/<architecture>/store/<hash[:2]>/<hash>/<tarball>
```

### Usage

```bash
# Fetch during build (read store)
bits build --remote-store https://buildserver/tarballs ROOT

# Build and upload (write store)
bits build --remote-store s3://mybucket/builds \
           --write-store  s3://mybucket/builds ROOT
```

---

## 20. Docker Support

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

---

## 21. Design Principles & Limitations

### Principles

1. **Reproducibility** — Stripping the shell environment and pinning exact git commits ensures the same inputs always produce the same build.
2. **Incrementalism** — The content-addressable hash scheme rebuilds only what has changed, keeping iteration fast even on large stacks.
3. **Isolation** — Each package builds in its own directory with a sanitised environment (locale forced to `C`, `BASH_ENV` unset, only declared dependencies visible).
4. **Parallelism** — Both inter-package (via the `Scheduler`) and intra-package (via `$JOBS`) parallelism are supported.
5. **Simplicity** — Build scripts are plain Bash, not a new DSL; the YAML header is metadata only.
6. **Portability** — Runs on any modern Linux distribution and on macOS (Intel and Apple Silicon).
7. **Extensibility** — The repository provider mechanism allows recipe sets to be composed dynamically from versioned git repositories without modifying the main configuration.

### Current limitations

- **Git and Sapling only** — No Subversion, Mercurial, or plain-tarball sources (except via `sources:` with `file://` URLs).
- **Linux and macOS only** — Windows is not supported.
- **Environment Modules required** for `bits enter / load / unload` — the `modulecmd` binary must be installed separately.
- **Active development** — The recipe format and Python APIs may change between versions. Evaluate thoroughly before adopting in production pipelines.
