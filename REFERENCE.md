# Bits Build Tool — Reference Manual

## Table of Contents

### Part I — User Guide
1. [Introduction](#1-introduction)
2. [Installation & Prerequisites](#2-installation--prerequisites)
3. [Quick Start](#3-quick-start)
4. [Configuration](#4-configuration)
5. [Building Packages](#5-building-packages)
    - [Parallel build modes](#parallel-build-modes)
6. [Managing Environments](#6-managing-environments)
7. [Cleaning Up](#7-cleaning-up)
8. [Practical Scenarios](#8-practical-scenarios)

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
17. [Recipe Format Reference](#17-recipe-format-reference)
18. [Defaults Profiles](#18-defaults-profiles)
19. [Architecture-Independent (Shared) Packages](#19-architecture-independent-shared-packages)
20. [Environment Variables](#20-environment-variables)
21. [Remote Binary Store Backends](#21-remote-binary-store-backends)
    - [Supported backends](#supported-backends)
    - [Content-addressable tarball layout](#content-addressable-tarball-layout)
    - [Build lifecycle with a store](#build-lifecycle-with-a-store)
    - [CI/CD patterns](#cicd-patterns)
22. [Docker Support](#22-docker-support)
23. [Design Principles & Limitations](#23-design-principles--limitations)

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

Bits reads an optional INI-style configuration file at startup to set the working directory, recipe search paths, and other defaults. The file is never created automatically — it must be written by the user.

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

| Config key | Exported as | Default | Description |
|---|---|---|---|
| `organisation` | `BITS_ORGANISATION` | `ALICE` | Organisation name. Also selects the organisation-specific section in this file. |
| `pkg_prefix` | `BITS_PKG_PREFIX` | `VO_<organisation>` | Prefix prepended to package names in `bits q` output. |
| `repo_dir` | `BITS_REPO_DIR` | `alidist` | Root directory for recipe repositories. |
| `sw_dir` | `BITS_WORK_DIR` | `sw` | Output and work directory for built packages, source mirrors, and module files. |
| `search_path` | `BITS_PATH` | _(empty)_ | Comma-separated list of additional recipe search directories. Absolute paths are used directly; relative names have `.bits` appended. |

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

### Common options

| Option | Description |
|--------|-------------|
| `--defaults PROFILE` | Defaults profile(s) to load. Combines multiple files with `::` (e.g. `--defaults release::myproject`). Default: `release`, which loads `defaults-release.sh`. |
| `-j N`, `--jobs N` | Parallel compilation jobs per package. Default: CPU count. |
| `--builders N` | Number of packages to build simultaneously using the Python scheduler. Default: 1 (serial). Mutually exclusive with `--makeflow`. |
| `--makeflow` | Hand the entire dependency graph to the external [Makeflow](https://ccl.cse.nd.edu/software/makeflow/) workflow engine instead of the built-in Python scheduler. Mutually exclusive with `--builders N`. |
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

### How a build proceeds

1. **Recipe discovery** — Bits locates `<package>.sh` in each directory on `search_path` (appending `.bits` to each name). Repository-provider packages (see [§13](#13-repository-provider-feature)) are cloned first to extend the search path before the main resolution pass.
2. **Dependency resolution** — `requires`, `build_requires`, and `runtime_requires` fields are read recursively, forming a DAG. Cycles are reported as errors.
3. **Hash computation** — A hash is computed for each package from its recipe text, source commit, dependency hashes, and environment. Packages with a matching hash in a store are downloaded instead of rebuilt.
4. **Source fetching** — Source repositories are cloned into a local mirror and then checked out into a build area. Up to 8 repositories are fetched in parallel.
5. **Build execution** — Each package's Bash script runs in an isolated environment with sanitised locale and only its declared dependencies visible.
6. **Post-build** — A modulefile and a versioned tarball are written; the tarball may be uploaded to a write store.

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
| `test_args.py` | CLI argument parsing |
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
| `--defaults PROFILE` | Defaults profile(s); use `::` to combine (e.g. `release::myproject`). Default: `release`. |
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
| `--defaults PROFILE` | Defaults profile(s); use `::` to combine (e.g. `release::myproject`). Default: `release`. |

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

### Combining multiple profiles with `::`

Two or more profiles can be combined in a single `--defaults` value using `::` as a separator:

```
bits build --defaults dev::gcc13 MyPackage
```

This loads `defaults-dev.sh` and `defaults-gcc13.sh` (in that order) and deep-merges their YAML headers into a single configuration. The merge follows the same left-to-right rules as specifying separate profiles: scalars from the later file win, lists are concatenated, dicts are recursively merged.

> **Note:** `defaults-release.sh` is **not** automatically prepended when you use `::`. If you want the release baseline plus a project overlay, write `--defaults release::myproject` explicitly.

### Profile names and the `defaults-release` dependency slot

Internally, bits rewrites all specified profiles to satisfy the universal `defaults-release` auto-dependency. When you write `--defaults gcc13`, the `defaults-gcc13.sh` file is loaded, its content is merged, and the result is presented to every other package as its `defaults-release` dependency — regardless of the actual file name on disk. This ensures that the hash of `defaults-release` is the same across all packages that share the same defaults configuration.

### Role in the build pipeline

Defaults processing happens in two phases:

**Phase 1 — `readDefaults()` + `parseDefaults()`** runs before package resolution. Bits loads each named profile file, merges their YAML headers into a single `defaultsMeta` dict, optionally overlays an architecture-specific file (e.g. `defaults-slc9_x86-64.sh`), then extracts:

- `disable` — packages to exclude from the build graph entirely.
- `env` — environment variables propagated to every package's `init.sh` (injected via the `defaults-release` pseudo-dependency).
- `overrides` — per-package YAML patches applied after the recipe is parsed (see below).
- `package_family` — optional install grouping (see [Package families](#package-families) below).
- `requires` / `build_requires` — repository providers (packages with `provides_repository: true`) to clone and add to `BITS_PATH` for builds using this profile. These are consumed by the Phase 2 provider scan and are **not** added as regular build dependencies (to avoid a dependency cycle — see [Triggering providers from a defaults file](#triggering-providers-from-a-defaults-file) in §13).

**Phase 2 — per-package application** happens inside `getPackageList()` as each recipe is parsed. The merged `overrides` dict is checked against the package name (case-insensitive regex match); matching entries are merged into the spec with `spec.update(override)`. This means a defaults file can change any recipe field — version, `requires`, `env`, `prefer_system`, etc. — for targeted packages.

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

### YAML fields specific to defaults files

| Field | Description |
|-------|-------------|
| `env` | Key-value pairs exported into every package's `init.sh` (via `defaults-release` auto-dependency). Equivalent to setting the same `env:` in every recipe. |
| `disable` | List of package names to exclude from the dependency graph. |
| `overrides` | Dict keyed by package name or regex. Each value is a YAML fragment merged into that package's spec after it is parsed. Keys are matched case-insensitively as `re.fullmatch` patterns, so regex metacharacters work. |
| `valid_defaults` | Restricts which profiles this recipe is compatible with. Each component of the `::` list is checked independently; bits aborts if any component is absent from the list. |
| `package_family` | Optional install grouping; see [Package families](#package-families) below. |
| `qualify_arch` | Set to `true` to append the defaults combination to the install architecture string; see [Qualifying the install architecture](#qualifying-the-install-architecture) below. |

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

### Merge semantics

When the `::` list contains more than one name (e.g. `--defaults release::alice`), `readDefaults()` processes them left to right and merges their YAML headers using `merge_dicts()`, which performs a deep merge:

- Scalar values: later profile wins.
- Lists: concatenated.
- Dicts: recursively merged.

This lets a project-level profile (`alice`) layer on top of a base profile (`release`) without duplicating common settings. Bits also validates that each component in the `::` list is present in any `valid_defaults` list found in the loaded recipes; it aborts with a clear error message if any component is incompatible.

### Architecture-specific overlay

If a file named `defaults-<architecture>.sh` exists in the recipe repository (e.g. `defaults-osx_arm64.sh`), bits silently loads it and merges its header on top of the already-merged profile, skipping the `package` key to avoid a name clash. This is the mechanism for per-platform tweaks such as disabling packages that do not build on a particular OS.

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

---

## 23. Design Principles & Limitations

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
