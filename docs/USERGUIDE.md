# Bits — User Guide

> **See also:** [Cookbook](COOKBOOK.md) · [Reference Manual](REFERENCE.md) · [Workflows](WORKFLOWS.md) · [Roadmap](ROADMAP.md)

## Table of Contents
1. [Introduction](#1-introduction)
   - [How bits works](#how-bits-works)
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

### How bits works

What happens when you run `bits build ROOT` in a community's recipe repository, in
one page. The rest of this guide gives the details.

**Recipes.** Each package is described by a recipe, `<package>.sh`: a short YAML header
(version, where the source comes from, which packages it needs) and a Bash script that
builds and installs it.

**Recipe repositories.** Recipes live in git repositories named `<name>.bits`. You work
in your community's repository: `stacks.bits` for the LCG stack, `alice.bits`,
`atlas.bits`, `cms.bits`, `key4hep.bits`, `lhcb.bits` or `ship.bits`. It holds the
community's settings and a few recipes of its own, and names the shared recipe
repositories it builds on (`lcg.bits`, with about 1,100 LCG recipes; `alidist` for
ALICE). bits finds those in its registry, [bits-providers](https://github.com/bitsorg/bits-providers),
and clones them itself, at the branch your community asks for:

```
key4hep.bits ─┐
atlas.bits   ─┤
lhcb.bits    ─┼─▶ stacks.bits ─▶ lcg.bits        alice.bits ─▶ alidist
ship.bits    ─┘   (settings)     (recipes)        (settings)    (recipes)
```

**Settings: defaults profiles.** How a build is made, its compiler, build type and
where it is published, comes from `defaults-<name>.sh` files, chained with `::`:
`--defaults gcc15::opt` is GCC 15, optimised (`release`, the base, is always first). A
few values are set with `--set`, e.g. `--set release=LCG_110`, the LCG release. Record
the options once per checkout with `bits use build …`.

**Platform.** Every build is for one platform, the CPU and operating system
(`x86_64-el9`). The settings add the compiler and build type, giving the architecture
`x86_64-el9-gcc15-opt`, which names the directory everything is installed in,
`sw/<architecture>/`.

**Hashes and reuse.** bits gives every package a hash computed from its recipe, its
sources, the hashes of the packages it needs and the build settings. A package whose
hash was built before is taken, not rebuilt: from your `sw/`, from a binary store, or,
when asked (`--reuse-from`), from what is already published on CVMFS. Change one recipe
and only that package and what depends on it are rebuilt. A binary from a store is
used only when a signed list of builds (a manifest) vouches for it. The community's CI
fills the store, so a build on the CI's platform, in its container, reuses it.

**Containers.** On Linux, `--docker` runs every build in the platform's builder image,
which has the compilers, build tools and system libraries (ALICE uses its own images,
see [Platforms](#platforms)). Without it, bits builds with what the machine has
installed ([§2](#2-installation--prerequisites)).

**The work directory.** `sw/` holds the sources (`SOURCES/`), the recipe repositories
bits cloned (`REPOS/`), the build areas (`BUILD/`, removed after a successful build),
the installations (`sw/<architecture>/<package>/<version>-<revision>/`) and a tarball of
each (`TARS/`, what stores share).

**Using what was built.** Each package gets an Environment Modules file. `bits enter
ROOT/latest` opens a shell with ROOT and everything it needs set up; `bits q` lists
what is there ([§6](#6-managing-environments)).

**Publishing.** Communities publish to CVMFS (`/cvmfs/bits.cern.ch/<community>`) from
their CI, run from the bits-console web interface: the same `bits build`, then a
publish, and a certification that signs what was built so that others reuse it.
Nightly builds and builds on each push are set up there too ([WORKFLOWS](WORKFLOWS.md)).

To start: prepare the machine ([§2](#2-installation--prerequisites)), then follow the
[Quick Start](#3-quick-start).

Key capabilities at a glance:

- Automatic topological dependency resolution and ordering
- Content-addressable incremental builds — only rebuilds what changed
- Parallel package builds and multi-core compilation
- Remote binary stores (HTTP, S3, rsync) to share pre-built artifacts, plus reuse of packages already deployed on CVMFS (`--reuse-from`)
- Docker-based builds for cross-compilation or reproducible CI environments
- Git and Sapling SCM support
- Dynamic recipe repositories loaded at dependency-resolution time
- Deterministic package tarballs, signed manifests and passkey-approved certification for trusted binary reuse
- SBOM export (CycloneDX / SPDX) and recipe-wide source checksums

The same `bits build` command that a developer runs interactively also drives the CI pipeline that publishes packages to CVMFS for the entire community — there is no separate local and CI toolchain. The full development-to-deployment workflow is described in [WORKFLOWS.md](WORKFLOWS.md).

---

## 2. Installation & Prerequisites

bits itself needs only Python 3.8+ and git ([Installing Bits](#installing-bits)). What
a *build* needs depends on where it runs:

- **In a container** (`bits build --docker`, recommended on Linux): the platform's
  builder image has the compilers, build tools and system libraries; the host needs
  docker or rootless podman. See [Building in a container](#building-in-a-container).
- **On this machine**: the host must have the build tools and system `-devel`
  libraries the recipes expect. See [Building natively](#building-natively-what-to-install).

Outside ALICE, only a container build reuses what the community's CI built: see
[Finding a platform with pre-built binaries](#finding-a-platform-with-pre-built-binaries).

### Platforms

"Supported" means three different things for a platform:

| | Where it is listed | Platforms today |
|---|---|---|
| **bits runs there** | the architectures `bits build` accepts (others need `--force-unknown-architecture`; it prints examples) | RHEL/AlmaLinux/Rocky, any version, on x86-64, aarch64 and ppc64le; Ubuntu, Debian and Fedora on x86-64; macOS on Intel and Apple silicon |
| **a builder image exists** | [bits-containers](https://github.com/bitsorg/bits-containers) `platforms.yaml` (images `gitlab-registry.cern.ch/bits/containers/<platform>`) | `x86_64-el8`, `x86_64-el9`, `x86_64-el10`, `aarch64-el9`, `x86_64-ubuntu2204`, `x86_64-ubuntu2404`, `x86_64-ubuntu2604` |
| **pre-built binaries exist** | the community's platforms: its console's Build dialog, or `platforms:` in its bits-console `ui-config.yaml` | the seven above for LCG, CMS, Key4hep, LHCb and SHiP; ATLAS: the four EL ones; ALICE: five, under its own names (below) |

A platform name is the CPU and the OS (`x86_64-el9`). A build adds its compiler and
build type from the defaults (`append_arch`): `--architecture x86_64-el9 --defaults
gcc15::opt` installs under `sw/x86_64-el9-gcc15-opt/`. `bits architecture` prints the
name bits gives this machine when no `--architecture` is given: `slc9_x86-64` for
RHEL, AlmaLinux and Rocky 9, `ubuntu2404_x86-64` for Ubuntu 24.04. These older names
are not the CI's, so a build under them reuses nothing the CI built.

ALICE is the exception: its platforms are built under these names (the last field of
its `ui-config.yaml` platforms: `slc9_x86-64`, `slc9_aarch64`, `ubuntu2404_x86-64`, …),
in its own images (`registry.cern.ch/alisw/<distro>-builder`), and read from ALICE's
store. For ALICE, build with the `aliBuild` wrapper, which picks those images, or give
`--docker-image registry.cern.ch/alisw/slc9-builder` (`--docker` alone would ask for
`gitlab-registry.cern.ch/bits/containers/x86_64-slc9`, which does not exist).

### Building in a container

```bash
bits doctor                      # checks docker / rootless podman (cgroup delegation), git, Python
bits build --docker --architecture x86_64-el9 --defaults gcc15::opt ROOT
```

- The host needs Python 3.8+, git, and docker, or podman run by your user
  (`podman-docker` provides the `docker` command); bits adapts its container options to
  rootless podman, and `bits doctor` says which cgroup controllers to delegate. No
  compiler or `-devel` packages are needed on the host.
- The image is chosen from the platform: `x86_64-el9` builds in
  `gitlab-registry.cern.ch/bits/containers/x86_64-el9:latest`, pulled when missing;
  `--docker-image` names another. See [REFERENCE §22](REFERENCE.md#22-docker-support).
- Another CPU builds through QEMU: on an x86-64 host, `--architecture aarch64-el9`
  needs the binfmt handlers (once: `docker run --rm --privileged
  multiarch/qemu-user-static --reset -p yes`), and is slow; see
  [Cross-compilation](REFERENCE.md#222-cross-compilation-via-qemu).
- What is built runs where its platform runs: an `x86_64-el9` build made on an
  Ubuntu host is for EL9 machines, and `bits enter` it on an EL9 host.

### Building natively: what to install

A native build uses the host's compiler to build the stack's own (GCC-Toolchain), the
host's build tools, and a set of system libraries the recipes link against instead of
building them (`prefer_system` and `system_requirement` recipes: krb5 for curl and
xrootd, libuuid, OpenSSL, X11 and GL for ROOT, …). The lists below are those the
builder images are made from (bits-containers `packages/build-tools.*.txt` and
`packages/dev-libs.*.txt`, the source of truth); installing them makes a host match
the image of its platform. `bits doctor <package>` reports what one build still lacks.

**AlmaLinux / Rocky 8, 9 and 10** (EPEL and CRB, called PowerTools on 8, are needed for
some; on RHEL itself enable `codeready-builder-for-rhel-N-$(arch)-rpms` with
`subscription-manager` and install EPEL from its release RPM):

```bash
sudo dnf install -y dnf-plugins-core epel-release
sudo dnf config-manager --set-enabled crb          # on 8: --set-enabled powertools
sudo dnf install -y \
  bison flex make autoconf automake libtool patch perl perl-core perl-devel tar gzip unzip \
  texinfo gettext-devel m4 swig pkgconf-pkg-config gcc gcc-c++ gcc-gfortran strace \
  environment-modules perf git rsync hostname which libcap-devel python3 \
  krb5-devel libuuid-devel openssl-devel zlib-devel bzip2-devel xz-devel libpng-devel \
  freetype-devel libxml2-devel lz4-devel ncurses-devel readline-devel libX11-devel \
  libXt-devel motif-devel libXpm-devel libXext-devel libXft-devel mesa-libGLU-devel \
  libXcursor-devel libXrandr-devel libXinerama-devel libXi-devel libXrender-devel \
  apr-devel apr-util-devel cyrus-sasl-devel subversion-devel elfutils-devel snappy-devel \
  xerces-c-devel sqlite-devel libuv-devel libunwind-devel libtirpc-devel
```

**Ubuntu 22.04, 24.04 and 26.04:**

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  bison flex make autoconf automake libtool libtool-bin patch perl libperl-dev tar gzip \
  unzip texinfo gettext autopoint m4 swig pkg-config gcc g++ gfortran strace \
  environment-modules git rsync hostname libcap-dev python3 python3-venv \
  libkrb5-dev uuid-dev libssl-dev zlib1g-dev libbz2-dev liblzma-dev libpng-dev \
  libfreetype-dev libxml2-dev liblz4-dev libncurses-dev libreadline-dev libx11-dev \
  libmotif-dev libxpm-dev libxext-dev libxft-dev libglu1-mesa-dev libxcursor-dev \
  libxrandr-dev libxinerama-dev libxi-dev libxrender-dev libapr1-dev libaprutil1-dev \
  libsasl2-dev libsvn-dev libdw-dev libsnappy-dev libxerces-c-dev libsqlite3-dev \
  libuv1-dev libunwind-dev
sudo apt-get install -y linux-tools-generic     # perf, for the recipes that check for it
```

Debian is not a builder platform: the Ubuntu list mostly applies (perf is
`linux-perf` there), and nothing pre-built is reused.

**macOS** (Intel or Apple silicon; there is no container build): the Xcode command-line
tools, then the Homebrew formulae the recipes declare (`homebrew_formula:`), which bits
writes into `sw/<arch>/Brewfile`:

```bash
xcode-select --install
brew install modules gnu-tar                 # gnu-tar: byte-reproducible tarballs
bits build ROOT                              # first run: writes sw/<arch>/Brewfile and stops
brew bundle --file sw/<arch>/Brewfile        # install the listed formulae
bits build ROOT                              # now builds

bits build --brew ROOT                       # alternative: install missing formulae on demand
bits brew                                    # write the Brewfile without resolving a build
```

See [REFERENCE.md — macOS Homebrew system layer](REFERENCE.md#macos-homebrew-system-layer).

**One package's requirements.** When a recipe's system requirement is missing, bits
stops before building and lists it; `bits doctor <package>` prints each recipe's
install hint:

```bash
bits doctor               # this machine: Python modules, git/compiler, docker or rootless podman, disk, stores
bits doctor <package>     # the system requirements of that package
```

### Finding a platform with pre-built binaries

A package is reused from the store when a build of the same hash is there and a
signed manifest vouches for it. The hash covers the recipes, the defaults and, for the
compiler toolchain and so for everything built with it, the builder image
(`/opt/bits/container-fingerprint.hash`). So the CI's binaries are reused by a build
that names the CI's platform, uses its defaults and runs in its image:

1. **List the community's platforms**: its console's Build dialog, or `platforms:` in
   its `ui-config.yaml` (e.g. `x86_64-el9`; for ALICE the last field, e.g.
   `slc9_x86-64`), and the defaults it builds with (e.g. `gcc15::opt`). On a machine
   with CVMFS, `bits cvmfs platforms`, run in the recipe repository, lists what is
   published under its prefix (`cvmfs.yaml`), by full architecture
   (`x86_64-el9-gcc15-opt`); what is in the store can differ.
2. **Pick the one to build for.** To run the result on this machine, the one of its
   OS and CPU; to build for elsewhere, any (another CPU through QEMU).
3. **Check before building**: in the recipe repository, a dry run with the same
   options shows, per package, **from store** or **build** (**build (unsigned)**: in
   the store, but no signed manifest vouches for it). It needs docker, and pulls the
   builder image the first time, as it reads the image's fingerprint:

   ```bash
   bits build --dry-run --docker --architecture x86_64-el9 --defaults gcc15::opt ROOT
   ```

4. **Build**, and record the options once for this checkout:

   ```bash
   bits use build --docker --architecture x86_64-el9 --defaults gcc15::opt
   bits build ROOT
   ```

The store is the site's (at CERN `https://s3.cern.ch/lcgapp-bits-testing`, where the
bits CI publishes; ALICE's architectures read ALICE's store); `--remote-store` names
another. Outside ALICE, a native build, or one under the name bits gives the machine
(`slc9_x86-64`), hashes differently and builds everything.

### Installing Bits

```bash
git clone https://github.com/bitsorg/bits.git
cd bits
export PATH=$PWD:$PATH
python3 -m venv .venv && source .venv/bin/activate   # activate it in every new shell; recent distributions refuse a system-wide pip install
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

In a container, on the CI's platform (see [Platforms](#platforms)), which reuses what
the CI built:

```bash
bits doctor                             # check that this machine is set up to run bits
bits build --dry-run --docker --architecture x86_64-el9 --defaults gcc15::opt ROOT
bits build --docker --architecture x86_64-el9 --defaults gcc15::opt ROOT
```

Or natively, with [the system packages](#building-natively-what-to-install) installed:

```bash
bits doctor --defaults gcc15 ROOT       # check ROOT's system requirements
bits build --dry-run --defaults gcc15 ROOT  # optional: what would be reused and what built
bits build --defaults gcc15 ROOT        # resolves and builds ROOT and all dependencies

bits enter ROOT/latest                  # open a sub-shell with the environment loaded
root -b
exit                                    # return to your normal shell
```

For another community the steps are the same, e.g.
`git clone https://github.com/bitsorg/lhcb.bits && cd lhcb.bits && bits build DaVinci`.
Record per-directory options (work directory, stores) once with `bits use build …`
inside the repository (see [Configuration](#4-configuration)).

### Getting help

`bits help` lists the commands by group, and `bits help COMMAND` (or
`bits COMMAND --help`) one command's options; `bits help enter` covers all the
module commands (`q`, `enter`, `setenv`, `load`, …).

### ALICE: the aliBuild workflow

The `aliBuild` wrapper keeps ALICE's classic workflow, working from an `alidist` checkout:

```bash
aliBuild init            # check out alidist
aliBuild build O2
```

---

## 4. Configuration

Record per-directory settings once with `bits use`, so you do not repeat them on every command. It saves command-line options in a profile and inserts them into later commands. A leading section name says which command they belong to (`build`, `deps`, `publish`, …); without one they go to `[common]`, which is applied to every architecture-aware command, so keep it to options all of them accept (`--architecture`).

```bash
bits use --architecture x86_64-el9-gcc14-opt                 # [common]: every arch-aware command
bits use build --work-dir /path/to/sw --parallel 4 \
               --remote-store https://s3.cern.ch/swift/v1/mybucket   # [build]: bits build only
bits use                                                     # show the active profile and where it lives
bits use --clear build                                       # clear one section (no SECTION: clear all)
```

Each `bits use SECTION …` replaces that whole section, so give all of a section's options in one call. Saved arguments are inserted before your own, so an explicit flag on the command line still wins. The flags of any command that reads the profile (`build`, `deps`, `doctor`, `status`, `clean`, `prune`, `publish`, `certify`, `sign`, `compliance`, `import`, `q`, `enter`, `load`, …) can be saved this way, e.g. `bits use build --docker --store-integrity` (SHA-256 verification of every recalled tarball). For a work directory shared by every command, including `bits enter` and `bits q`, set `$BITS_WORK_DIR` instead.

`bits init` with configuration options and no PACKAGE writes the same profile for the settings it knows: `--architecture` to `[common]`; `--remote-store`, `--write-store`, `--defaults`, `-c/--config-dir`, `-w/--work-dir` and `--reference-sources` to `[build]` (replacing those sections, as `bits use` does). Add `--dry-run` to see what it would save:

```bash
bits init --work-dir /path/to/sw --remote-store https://s3.cern.ch/swift/v1/mybucket
```

The profile is `./.bitsuse` in the current directory, or a record under `~/.bits/use/` when the directory is not writeable or not owned by you. A local `.bitsuse` is only honoured when it is owned by you; otherwise the `~/.bits/use/` record is used. `bits use --help` lists these forms.

> **`bits.rc` is retired.** Earlier versions read `bits.rc` / `.bitsrc` / `~/.bitsrc`; those files are no longer read. Move per-directory settings into a `bits use` profile and global ones into the environment variables below.

Global settings come from environment variables:

| Variable | Related flag | Description |
|----------|--------------|-------------|
| `$BITS_COMMUNITY` | — | Community name (uppercase), e.g. `LHCB` (`$BITS_ORGANISATION`, its former name, still works). Used only when `-c`/`--config-dir` names a directory that does not exist: bits then clones that community's recipe repository from the registry and uses it. The `aliBuild` wrapper sets `ALICE`; `bits --community NAME <sub-command>` sets it too. |
| `$BITS_WORK_DIR` | `-w` / `--work-dir` | Output directory for built packages (default: `sw`). |
| `$BITS_REPO_DIR` | `-c` / `--config-dir` | Root directory for recipe repositories. |
| `$BITS_PROVIDERS` | — | URL of the bits-providers registry, optionally `@tag` (default `https://github.com/bitsorg/bits-providers`; off under the `aliBuild` wrapper). |
| `$BITS_PATH` | `--search-path` | Recipe search path. |
| `$BITS_S3_STORE` | `--remote-store` (store ops) | Default S3 store for `bits store` (`gc`/`stats`/`upload`), `certify`, `publish`, `compliance`. |

`$BITS_COMMUNITY` is set **uppercase** (`ALICE`, `LHCB`, …). Bits lowercases it internally when resolving the community recipe repository from bits-providers (e.g. `LHCB` → `lhcb.bits.sh` → `https://github.com/bitsorg/lhcb.bits`). Normally you do not need it: check out the community repository and run bits inside it.

### Where defaults come from

A setting is taken from the first of these that gives it:

1. the command line;
2. the `bits use` profile (inserted before your own options, so those still win);
3. the environment (`$BITS_WORK_DIR`, `$BITS_S3_STORE`, `$BITS_CONSOLE_URL`, …);
4. the community's defaults: the `system:` block of the recipe repository's `defaults-*.sh` (its `remote_store`, `certify_group`, `console_url`, …), which is publish policy and part of no package hash;
5. the **site configuration**: `NN-<name>.yaml` files describing an installation (its stores, container images, console, providers repository, SBOM namespace and a few paths);
6. bits' own site-neutral `00-bits.yaml`.

The exception is `$BITS_PATH`, which, when set, overrides `--search-path`.

The CERN stores, images and console URL, formerly built into bits, are in the site file `10-CERN.yaml`, shipped with bits next to `00-bits.yaml`. Files are read from bits' own `site.d/`, then `/etc/bits/site.d/` (a host or runner), `~/.config/bits/site.d/` and `$BITS_SITE_DIR`; a file replaces an earlier one of the same name, then all of them are merged by file name (not directory), the last winning. So:

- **At CERN** there is nothing to do.
- **Elsewhere**, an empty `10-CERN.yaml` in `/etc/bits/site.d/` switches CERN's values off, and a file of your own sets yours:

  ```yaml
  # /etc/bits/site.d/10-mylab.yaml
  store:
    read: b3://mylab-bits
    operations: b3://mylab-bits
    s3_endpoint: https://s3.mylab.example.org
  console:
    backend_url: https://bits.mylab.example.org
  ```
- **A runner or a person** can override single keys in a later file (`50-…` for a host, `90-…` for yourself).

`bits doctor` lists the files in effect and the store, container and providers values they give. The keys are in [REFERENCE.md — Site configuration](REFERENCE.md#site-configuration); for every environment variable, see [REFERENCE.md §20](REFERENCE.md#20-environment-variables) and [REFERENCE.md — bits init](REFERENCE.md#bits-init).

---

## 5. Building Packages

```bash
bits build [options] PACKAGE [PACKAGE ...]
```

Bits resolves the full transitive dependency graph of each requested package, computes a content-addressable hash for every node, downloads any pre-built artifacts that already exist in a remote store, and builds the rest in topological order.

### How a build proceeds

1. **Recipe discovery** — Bits locates `<package>.sh` in the recipe directory, then in each `BITS_PATH` entry (`--search-path`; a relative NAME means `<config-dir>/NAME.bits`, an absolute path is used as is). Repository-provider packages (see [§13](REFERENCE.md#13-repository-provider-feature)) are cloned first to extend the search path before the main resolution pass.
2. **Dependency resolution** — `requires`, `build_requires`, and `runtime_requires` fields are read recursively, forming a DAG. Cycles are reported as errors.
3. **Hash computation** — A hash is computed for each package from its recipe text, source commit, dependency hashes, and environment. Packages with a matching hash in a store are downloaded instead of rebuilt.
4. **Source fetching** — Source repositories are cloned into a local mirror and then checked out into a build area. Up to 8 repositories are fetched in parallel.
5. **Build execution** — Each package's Bash script runs in an isolated environment with sanitised locale and only its declared dependencies visible.
6. **Post-build** — A modulefile and a versioned tarball are written; the tarball may be uploaded to a write store.

### Common options

| Option | Description |
|--------|-------------|
| `--defaults PROFILE` | Defaults profile(s) to load, combined with `::`; `release` is always the base (`--defaults gcc15` means `release::gcc15`). Default: `release`. |
| `--set NAME[=VALUE]` | Set a build-wide flavour variable (alias of `--flavour`); gates conditional `(?NAME)` requires/sources/patches and overrides a defaults `variables:` entry. |
| `-j N`, `--jobs N` | Parallel compilation jobs per package. Default: CPU count. |
| `--parallel [N]` | Number of packages to build simultaneously. Bare `--parallel` uses 4; omit it for serial (the default). With N>1 each build's `$JOBS` is divided across the builders (`-j ÷ N`) so the concurrent jobs together stay within one machine's worth of cores. (`--builders` is a kept alias.) |
| `--build-nice` | Stagger concurrent builders across OS priority levels so CPU contention degrades gracefully — one build runs at full speed, the others are backed off, and the freed top slot is taken over as builds finish. Native builds use `nice`; `--docker` builds use `docker run --cpu-shares`. Opt-in; only affects `--parallel > 1`. Memory is still capped separately. |
| `--build-nice-step N` | Priority spread between concurrent build slots for `--build-nice` (slot *k* → nice `min(k×N, 19)`). `N=1` is a gentle ladder; larger separates slots more. Default: 5. |
| `--prefetch-workers N` | Background threads that fetch remote tarballs and sources ahead of the build loop. Default: auto (`min(builders, 4)`); `0` disables. |
| `--fetch-build-requires` | Also fetch or build the build-only dependencies of reused packages (by default they are not needed and skipped). |
| `-u`, `--fetch-repos` | Update all source mirrors before building (the default), so a branch builds its current commit. |
| `--no-fetch-repos` | Build what the mirrors already have (offline). |
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

Computes every package's hash exactly as the build would and prints, in build order, where each one would come from — **installed**, **local tarball**, **from store**, **from overlay** — or that it would be **built** (**build (unsigned)**: in the store, but no verified signed manifest vouches for it; **build (development)**: a local checkout), followed by a summary. The recipe repository most packages come from is named once, above the table; a package from another shows its own. The store is checked for all packages at once, 16 requests at a time, with a progress line. A real build prints the same plan before it starts. A package that only reused packages need to build (their `build_requires`) is **not needed**: it is neither fetched nor built (`--fetch-build-requires` fetches it anyway). Nothing is downloaded or installed; http(s)://, s3:// and b3:// stores are only listed, and rsync stores are not checked. `bits status --check-store` uses the same store listing. On macOS a dry run also writes `sw/<arch>/Brewfile`.

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

Bits uses the standard [Environment Modules](https://modules.sourceforge.net/) system (`modulecmd`) to manage runtime environments. A *module* corresponds to one built package version. The `bits` shell script discovers `modulecmd` automatically — on `$PATH`, next to `envml`, or via Homebrew's `modules` formula. If it cannot be found, it prints the appropriate install command.

### Enter a sub-shell with modules loaded

```bash
bits enter ROOT/latest
# A new sub-shell opens with ROOT and all its dependencies in PATH etc.
exit   # return to your normal shell
```

`bits enter` prefixes the shell prompt with the loaded modules, e.g. `[ROOT/latest]` (sh/bash/ksh/zsh, unless `--shellrc` is given), so it is always clear when inside a bits environment. Nesting `bits enter` inside another bits environment is blocked.

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
| `-w DIR` | Work directory to clean. Default: `$BITS_WORK_DIR` (or `$ALICE_WORK_DIR`), else `sw`. |
| `-C DIR`, `--chdir DIR` | Change to DIR first (or `$BITS_CHDIR`). |
| `-a ARCH` | Restrict to this architecture. |
| `--aggressive-cleanup` | Also remove `SOURCES/` (source checkouts and download cache) and the tarballs under `TARS/<arch>/store` and `TARS/share/store`. Git mirrors (`MIRROR/`) are kept. |
| `-n`, `--dry-run` | Show what would be removed without deleting. |

The default (non-aggressive) clean removes the `TMP/` and `INSTALLROOT/` staging areas, `BUILD/` directories no `*-latest*` symlink points to, and installed versions (for the selected architecture and `share/`) no `latest*` symlink points to. Aggressive cleanup additionally removes `SOURCES/` and the tarballs under `TARS/<arch>/store` and `TARS/share/store`; tarball symlinks and `MIRROR/` are kept. Use `bits clean` after temporary or experimental builds to reclaim disk space without affecting the persistent package cache.

### bits prune — evict packages from a persistent workDir

> **Renamed to `bits prune`.** `bits cleanup` still works as a deprecated alias that
> warns and forwards; use `bits prune` in new scripts.

`bits prune` manages a long-lived, shared workDir by evicting packages that have not been used recently or when disk space falls below a threshold. It is intended for **persistent CI build caches** where packages accumulate over time.

```bash
bits prune [options]
```

| Option | Default | Description |
|--------|---------|-------------|
| `-w DIR`, `--work-dir DIR` | `$BITS_WORK_DIR` (or `$ALICE_WORK_DIR`), else `sw` | workDir to manage. |
| `-a ARCH`, `--architecture ARCH` | auto-detected | Architecture to evict packages for. |
| `--max-age DAYS` | `7.0` | Evict packages whose sentinel has not been touched in more than `DAYS` days. Set to `0` to disable age-based eviction. |
| `--min-free GIB` | _(none)_ | Evict the least-recently-used packages until at least `GiB` GiB of free disk space is available. |
| `--disk-pressure-only` | — | Run only the disk-pressure eviction pass; skip age-based eviction. |
| `--retain` | — | Manifest-rooted sweep over all architectures: keep the packages of the newest `--keep-builds N` (default 2) build manifests and anything certified but not yet on CVMFS; evict what is safely upstream (stored, in the verified signed manifest and published to CVMFS). Signed manifests come from `--remote-store` and/or `--trust-manifest`; an architecture whose manifest cannot be verified is skipped. |
| `--grace-days DAYS` | `1.0` | With `--retain`: never evict anything modified more recently than DAYS days ago. |
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

- With no PACKAGE, `bits publish` uploads every package of the latest build manifest to the S3 store (`--manifest FILE` picks another), together with the release's NOTICE, GPL source offer and SBOMs. To upload **one** package to the S3 store use `bits store upload PKG`.
- When a `cvmfs_packages_template` is set, the pipeline's `bits cvmfs publish` sends each package's identity path and hash, so prepub skips a duplicate that is already queued or published. With `--replace-on-conflict` a package another build published (a different hash) is replaced, if prepub allows it (`replace_on_conflict`); bits checks before uploading. The ingest path also refuses up front a tarball larger than prepub's advertised `max_tar_size`.
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

`bits certify` is what a user runs: it uploads anything still missing from the store, asks bits-console for a passkey approval (scan the QR code, compare the code, approve on your phone) and then opens the certification merge request in the manifests repository. Where bits-orchestrator owns the store's certified set (`MANIFESTS/orchestrator.json` in the store), it stops after the approval: the orchestrator certifies the build as soon as it is in the store, and `bits sign` refuses that store. `bits sign` — the command formerly called `certify` — is run by the manifests-repo CI to merge the approved build manifests into the signed common manifest. Group, manifests repo and console URL default from the defaults' `system:` block. See [REFERENCE.md — Publishing and certifying a build](REFERENCE.md#publishing-and-certifying-a-build--bits-publish-bits-certify) and [Signing — bits sign](REFERENCE.md#signing--bits-sign).

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

Git tags are pinned to their commit; branches are reported as moving and never pinned. The exit status is 1 on any mismatch or failure. See [REFERENCE.md — bits checksums](REFERENCE.md#bits-checksums).

### Produce an LCG release view

`bits overlay lcg` writes an lcgcmake-style LCG release over a built closure: `LCG_<num><postfix>/LCG_externals_<platform>.txt` (plus an `LCG_generators_<platform>.txt` placeholder) under `--out`. With `--build-view DIR` it also builds a merged symlink-farm view in DIR, with a `setup.sh` that puts the view on `PATH`, `LD_LIBRARY_PATH` and friends:

```bash
bits overlay lcg -a x86_64-el9-gcc15 --platform x86_64-el9-gcc15-opt \
                 --version-number 110 --out /path/to/releases --build-view /path/to/view
```

`bits lcg-view` is the deprecated former name. See `bits overlay lcg --help`.

---

## Next Steps

- **[Cookbook](COOKBOOK.md)** — practical recipes for common tasks
- **[Reference Manual](REFERENCE.md)** — command-line flags, recipe format, environment variables, Docker, stores, CVMFS pipeline, developer guide
- **[Workflows](WORKFLOWS.md)** — development-to-deployment walkthrough
- **[Roadmap](ROADMAP.md)** — planned features and priorities
