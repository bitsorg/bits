# Bits - Quick Start Guide

Bits is a build orchestration tool for complex software stacks. It fetches sources, resolves dependencies, and builds packages in a reproducible, parallel environment.

Beyond building, bits covers the path to deployment:

- **Binary reuse** from a content-addressed S3 store, trusted through signed manifests (`bits certify` / `bits sign`).
- **CVMFS publishing** where each package is published once per build architecture and releases are views over it (symlink releases plus an LCG-style merged view with a self-locating `setup.sh`).
- **Supply chain**: deterministic tarballs, CycloneDX/SPDX SBOMs (`bits sbom`), and source/patch checksums with git commit pins (`bits checksums`).
- **Per-directory profiles** (`bits use`) so repeated options stay out of every command line.

> Full documentation is available in [docs/USERGUIDE.md](docs/USERGUIDE.md), [docs/COOKBOOK.md](docs/COOKBOOK.md), and [docs/REFERENCE.md](docs/REFERENCE.md); short answers to common questions are in [docs/FAQ.md](docs/FAQ.md). This guide covers only the essentials.

---

## Installation

```bash
git clone https://github.com/bitsorg/bits.git
cd bits
export PATH=$PWD:$PATH          # add bits to your PATH
python -m venv .venv
source .venv/bin/activate
pip install -e .                # install Python dependencies
```

**Requirements** depend on where you build:

- **In a container** (`--docker`, recommended on Linux): Python 3.8+, git, and docker or
  rootless podman. The builder image brings the compilers, build tools and system
  libraries. See [Building in a container](docs/USERGUIDE.md#building-in-a-container).
- **On this machine**: the build tools and system `-devel` libraries the recipes
  expect, which the builder images list per platform. See
  [Building natively](docs/USERGUIDE.md#building-natively-what-to-install) for the
  install command for EL 8/9/10, Ubuntu 22.04/24.04/26.04 and macOS.

`bits doctor` checks either setup. Which platforms are supported, and which have
pre-built binaries, is in [Platforms](docs/USERGUIDE.md#platforms).

---

## Quick Start

### Before you start: pick the platform to build for

Pre-built binaries are reused only for a platform the community's CI builds, built
the same way. On Linux, build in that platform's builder image with `--docker` and
name the platform with `--architecture`: the one matching your machine, if you want to
run the result here (the compiler and build type come from `--defaults`):

```bash
bits architecture      # this machine's own name, e.g. slc9_x86-64 on AlmaLinux 9
```

The CI platforms are `x86_64-el8`, `x86_64-el9`, `x86_64-el10`, `aarch64-el9`,
`x86_64-ubuntu2204`, `x86_64-ubuntu2404` and `x86_64-ubuntu2604` (a community may
build fewer; ALICE builds under the older names such as `slc9_x86-64`). The name
bits gives an EL9 machine by itself, `slc9_x86-64`, is not one of them. See [Finding a platform with pre-built binaries](docs/USERGUIDE.md#finding-a-platform-with-pre-built-binaries).

### Check out a community recipe repository, then build inside it

bits builds from a community recipe repository: a `*.bits` repository with the
community's defaults, CVMFS layout and recipes. Clone one and run bits inside it
(bits uses the current directory as its recipe directory):

```bash
git clone https://github.com/bitsorg/stacks.bits && cd stacks.bits
#   or: bits init stacks.bits && cd stacks.bits   (resolved in the bits-providers registry)
bits build --dry-run --docker --architecture x86_64-el9 --defaults gcc15::opt ROOT  # per package: from store, or build
bits build --docker --architecture x86_64-el9 --defaults gcc15::opt ROOT   # on an EL9 machine
#   or natively, with the system packages installed: bits build --defaults gcc15 ROOT

# Enter the built environment and run
bits enter ROOT/latest
root -b
exit
```

Community repositories: `stacks.bits` (LCG-based stacks), `alice.bits`, `atlas.bits`,
`cms.bits`, `key4hep.bits`, `lhcb.bits`, `ship.bits`. They pull shared recipe pools
(`lcg.bits`, `common.bits`, `alidist.bits`) on demand; `bits init <name>.bits` clones a
community repository from the [bits-providers](https://github.com/bitsorg/bits-providers)
registry. ALICE's classic workflow keeps working through the `aliBuild` wrapper:
`aliBuild init` checks out `alidist`, then `aliBuild build O2`.

### Check the machine and the plan before building

```bash
bits doctor                  # is this machine set up to run bits? (Python modules, git, compiler, container engine, disk, stores)
bits doctor ROOT             # are ROOT's system requirements satisfied?
bits build --dry-run ROOT    # per-package plan: installed, local tarball, remote store, reuse overlay or build
bits build --parallel 4 ROOT # build up to 4 independent packages at once (--builders is an alias)
```

---

## Basic Commands

`bits help` lists every command by group; `bits help COMMAND` (the same as
`bits COMMAND --help`) prints one command's options.

| Command | Description |
|---------|-------------|
| `bits build <pkg>` | Build a package and its dependencies (`--dry-run` prints the reuse plan instead). |
| `bits enter <pkg>/latest` | Spawn a subshell with the package environment loaded. |
| `bits load <pkg>` | Print commands to load a module (must be `eval`'d). |
| `bits q [regex]` | List available modules. |
| `bits clean` | Remove stale build artifacts from a temporary build area. |
| `bits prune` | Evict old or infrequently used packages from a persistent workDir (was `bits cleanup`, still accepted as a deprecated alias). |
| `bits doctor` | With no package, check that this machine is set up to run bits. |
| `bits doctor <pkg> [<pkg>...]` | Check that the system satisfies all recipe requirements before building. |
| `bits doctor --runner` | Validate the full build-runner environment (compiler, git, Docker, podman, CVMFS, disk, store). |
| `bits deps <pkg>` | Show the dependency graph (`--outmake FILE` writes it as Makefile rules). |
| `bits use [<cmd> <args>]` | Save options per directory (`.bitsuse`) so you do not repeat them; with no arguments, show the active profile. |
| `bits brew` | macOS: write the Homebrew `Brewfile` for the stack (`sw/<arch>/Brewfile`). |
| `bits publish` | Relocate a built package (or, with `--release-view`, a release view) and hand it to CVMFS via cvmfs-prepub. |
| `bits certify` | Make a build trusted for reuse: upload what is missing, approve with a passkey via bits-console, open the certification MR (where the orchestrator owns the store's certified set, it certifies the approved build and no MR is opened). |
| `bits sign` | Merge build manifests into a signed common manifest (run by the manifests CI after `bits certify`; refused for a store the orchestrator signs). |
| `bits sbom <manifest>` | Export a build manifest as CycloneDX 1.6 and/or SPDX 2.3 JSON. |
| `bits checksums` | Hash every tarball and patch of a recipe repository and pin git tags (`--write` records them). |
| `bits overlay lcg` | Emit an LCG release view (`LCG_externals` + merged `setup.sh`) over a built closure (was `bits lcg-view`). |
| `bits verify --from-manifest FILE` | Confirm a live deployment matches the build manifest (SHA-256 and provider commits). |

Two admin/CI groups act on shared infrastructure: **`bits store`** (`ls`, `rm`,
`verify`, `cat`, `gc`, `stats`, `upload`) manages the S3 binary store, and **`bits cvmfs`**
(`platforms`, `show`, `summary`, `stage`, `publish`) inspects a deployed CVMFS tree
and drives producer-side publishing. `bits publish` publishes packages to CVMFS (bare
`bits publish` bulk-uploads the latest build manifest to the S3 store); single-package
S3 uploads are `bits store upload`. The old spellings `bits store-stats`,
`bits cvmfs-stage` and `bits cvmfs-publish` still work but are deprecated.

[Full command reference](docs/REFERENCE.md#16-command-line-reference)

---

## Configuration

Record per-directory settings with `bits use`, so you do not repeat them on every
command. Options after a section name apply to that command; without one they go to
`[common]`, applied to every architecture-aware command:

```bash
bits use --architecture x86_64-el9-gcc14-opt
bits use build --work-dir /path/to/sw --remote-store https://mybucket/builds --docker
```

The profile is stored in `./.bitsuse` (or under `~/.bits/use/` when the current
directory is not writeable). Each `bits use SECTION …` replaces that section;
`bits use` alone shows the active profile, `bits use --clear [SECTION]` removes it and
`bits use --help` lists the forms. A `.bitsuse` file is only honoured when it is owned
by you. Profiles replace the retired `bits.rc` file.

`bits init` with configuration options and no package
(e.g. `bits init --work-dir /path/to/sw --remote-store URL`) writes the same profile:
`--architecture` to `[common]`; the store, defaults, config-dir, work-dir and
reference-sources options to `[build]`, replacing those sections.

Global settings come from environment variables:

| Variable | Related flag | Description |
|----------|--------------|-------------|
| `$BITS_COMMUNITY` | — | Community name (uppercase), e.g. `LHCB` (`$BITS_ORGANISATION`, its former name, still works). Used only when `-c`/`--config-dir` names a directory that does not exist: bits then clones that community's recipe repository from the registry and uses it. The `aliBuild` wrapper sets `ALICE`; `bits --community NAME <sub-command>` sets it too. |
| `$BITS_WORK_DIR` | `-w` / `--work-dir` | Output directory for built packages (default: `sw`). |
| `$BITS_REPO_DIR` | `-c` / `--config-dir` | Root directory for recipe repositories. |
| `$BITS_PROVIDERS` | — | URL of the bits-providers registry, optionally `@tag` (default `https://github.com/bitsorg/bits-providers`; off under the `aliBuild` wrapper). |
| `$BITS_PATH` | `--search-path` | Recipe search path. |
| `$BITS_S3_STORE` | `--remote-store` (store ops) | Default S3 store for `bits store` (`gc`/`stats`/`upload`), `certify`, `publish`, `compliance`. |
| `$BITS_PREREQUISITES_URL` | — | URL shown when `bits doctor` cannot find the C++ compiler or git. |
| `$BITS_CVMFS_REPOS` | `--cvmfs-repos` | Comma-separated CVMFS mount paths checked by `bits doctor --runner`. |

`$BITS_COMMUNITY` is set **uppercase** (`ALICE`, `LHCB`, …).  Bits lowercases it
internally when resolving the community recipe repository from bits-providers
(e.g. `LHCB` → `lhcb.bits.sh` → `https://github.com/bitsorg/lhcb.bits`). Normally you
do not need it: check out the community repository and run bits inside it.

Installation-wide defaults (stores, container images, the console) come from
**site configuration** files, `site.d/NN-<name>.yaml`. bits ships CERN's as
`10-CERN.yaml`; elsewhere, mask it with an empty `/etc/bits/site.d/10-CERN.yaml`
and add your own. The command line, the `bits use` profile, the environment and the
community's `system:` defaults all come first
([where defaults come from](docs/USERGUIDE.md#where-defaults-come-from)).

[Configuration details](docs/USERGUIDE.md#4-configuration)

---

## Repositories: hierarchy, discovery & reuse

bits keeps the tool, the policy, and the recipes in **separate versioned
repositories**. The registry [`bits-providers`](https://github.com/bitsorg/bits-providers)
maps a community name to its repo; a *community/policy* repo such as
[`stacks.bits`](https://github.com/bitsorg/stacks.bits) sets defaults and CVMFS layout and
`requires:` a shared *recipe pool* like [`lcg.bits`](https://github.com/bitsorg/lcg.bits)
(~1100 recipes). Any recipe with `provides_repository: true` is cloned on demand and added
to the search path, so a build pulls in the pools it needs automatically — and because the
binary store is content-addressed, matching artifacts are reused across communities.

```bash
# Build an LCG-stack package: run from the community repo; bits auto-pulls lcg.bits
git clone https://github.com/bitsorg/stacks.bits && cd stacks.bits
bits build ROOT --defaults gcc15
```

The entry point is `defaults-release.sh` (composition: `stacks.bits` →
`defaults-release.sh` → `lcg.bits`). For the full model see
[`bits-providers`](https://github.com/bitsorg/bits-providers) and each `*.bits` repository
(e.g. [`stacks.bits`](https://github.com/bitsorg/stacks.bits),
[`alice.bits`](https://github.com/bitsorg/alice.bits)).

---

## Writing a Recipe

Create a file `<package>.sh` inside a `*.bits` directory with:

```yaml
package: mylib
version: "1.0"
source: https://github.com/example/mylib.git
tag: v1.0
requires:
  - zlib
---
./configure --prefix="$INSTALLROOT"
make -j${JOBS:-1}
make install
```

[Complete recipe reference](docs/REFERENCE.md#17-recipe-format-reference)

---

## Cleaning Up

```bash
bits clean                # remove temporary build directories
bits clean --aggressive-cleanup   # also remove source mirrors and tarballs

# Persistent workDir cache management (evict old / low-disk-space packages)
bits prune --max-age 14           # evict packages not used in the last 14 days
bits prune --min-free 100         # free space until at least 100 GiB available
bits prune -n                     # dry-run: show what would be removed
```

[Cleaning options](docs/USERGUIDE.md#7-cleaning-up)

---

## Docker & Remote Builds

```bash
# Build inside the builder image of a platform (gitlab-registry.cern.ch/bits/containers/x86_64-ubuntu2404)
bits build --docker --architecture x86_64-ubuntu2404 ROOT

# Cross-compile for ARM64 on an x86-64 host (requires QEMU binfmt handlers)
bits build --docker --architecture aarch64-el9 MyAnalysis

# Use a remote binary store (S3, HTTP, rsync) to share pre-built artifacts
bits build --remote-store s3://mybucket/builds ROOT

# Read from and upload to the same writable store
bits build --remote-store s3://mybucket/builds::rw ROOT
```

`--write-store URL` alone only uploads; it is also the read store where no default
remote store applies. `--store` is a deprecated spelling of `--remote-store` on `bits publish`,
`certify`, `sign`, `compliance`, `prune` and `bits store upload` (not on `bits build`).

The `--cvmfs-prefix` flag (which embeds the final CVMFS deployment path at compile time so no relocation is needed at publish time) and `bits publish --no-relocate` are used by the **bits-console-triggered CI pipeline** on the build runners — they are not normally typed by end users. See [WORKFLOWS.md Phase 4](docs/WORKFLOWS.md#phase-4--ci-build-and-cvmfs-publication-via-bits-console) for the user-facing workflow and [docs/REFERENCE.md §22](docs/REFERENCE.md#22-docker-support) for the flag reference.

[Docker support](docs/REFERENCE.md#22-docker-support) | [Cross-compilation via QEMU](docs/REFERENCE.md#222-cross-compilation-via-qemu) | [Remote stores](docs/REFERENCE.md#21-remote-binary-store-backends)

---

## Validating Builds and Deployments

```bash
# Check this machine is set up for bits (Python modules, git, compiler, container engine, stores)
bits doctor

# On a CI build runner: Docker daemon, podman, QEMU binfmt, CVMFS mounts, disk, store, prepub
bits doctor --runner --cvmfs-repos /cvmfs/alice.cern.ch [--prepub-url URL] [--json]

# Verify that a live CVMFS deployment matches a recorded build manifest
bits verify --from-manifest alice-o2-20260411.json \
            --cvmfs-root /cvmfs/alice.cern.ch
```

[bits doctor reference](docs/REFERENCE.md#bits-doctor) | [bits verify reference](docs/REFERENCE.md#23-bits-verify--deployment-verification)

---

## Publishing, Certification & SBOMs

```bash
# Make a build trusted for binary reuse (upload missing tarballs, passkey approval, certification MR)
bits certify

# Publish a release to CVMFS as a view over packages published once per architecture
bits publish --release-view MyStack

# Software Bill of Materials of a build (CycloneDX 1.6 + SPDX 2.3)
bits sbom sw/MANIFESTS/bits-manifest-<build>.json -o sbom/

# Check (and with --write, record) checksums and git commit pins of a recipe repository
bits checksums -c lcg.bits
```

A community's CVMFS layout comes from its defaults: `cvmfs_packages_template` places
each package once per build architecture (skipped when the same build hash is already
published) and `cvmfs_views_template` adds a merged `bin/ lib/ include/ …` view with a
self-locating `setup.sh`; templates accept `{arch}` and `{day}` tokens. `--view` is the
deprecated spelling of `--release-view`.

[Publishing and certifying](docs/REFERENCE.md#publishing-and-certifying-a-build--bits-publish-bits-certify) | [bits sbom](docs/REFERENCE.md#bits-sbom) | [bits checksums](docs/REFERENCE.md#bits-checksums)

---

## Development & Testing (Contributing)

```bash
git clone https://github.com/bitsorg/bits.git
cd bits
python -m venv .venv
source .venv/bin/activate
pip install -e .[test]

# Run tests
tox                     # full suite on Linux
tox -e darwin           # reduced suite on macOS
pytest                  # fast unit tests only
```

[Developer guide](docs/REFERENCE.md#part-i--developer-guide)

---

## The bits Workflow: From Local Dev to CVMFS

bits uses a single toolchain from your laptop to experiment-wide CVMFS. Clone a package source next to your recipe checkout and bits detects it automatically, building your local version while resolving all other dependencies from the shared recipe repo. Once tested locally, the change follows an unbroken path: commit → recipe MR → CI build → `bits publish` → CVMFS. Group admins publish full experiment stacks; individual users can publish single packages to a separate namespace — both paths use the same commands and the same recipes.

See **[WORKFLOWS.md](docs/WORKFLOWS.md)** for the full phase-by-phase walkthrough and workflow diagram.

---

## Next Steps

- **[User Guide](docs/USERGUIDE.md)** — installation, configuration, building, environments, cleaning up
- **[Cookbook](docs/COOKBOOK.md)** — practical recipes for common tasks
- **[Reference Manual](docs/REFERENCE.md)** — command-line flags, recipe format, environment variables, Docker, stores, CVMFS pipeline, developer guide
- **[Workflows](docs/WORKFLOWS.md)** — development-to-deployment walkthrough and diagram
- **[Roadmap](docs/ROADMAP.md)** — planned features and priorities

---

**Note**: Bits is under active development. For the most up-to-date information, see the full [docs/REFERENCE.md](docs/REFERENCE.md).

## Licensing

The bits ecosystem spans several repositories under two licenses, chosen by
provenance rather than preference.

## Why two licenses

`bits` and its recipe repositories descend from ALICE's **aliBuild** and
**alidist**, both licensed under **GPL-3.0**. Under the GPL's copyleft, these
derivative works must remain GPL-3.0-or-later.

The newer services written from scratch for the CVMFS publish chain — the Go
publisher (`cvmfs-bits` / cvmfs-prepub) and its deployment example
(`cvmfs-testbed`) — contain no aliBuild code, so they use the permissive
**Apache-2.0** license. Apache-2.0 is one-way compatible *into* GPL-licensed
combinations, so these components can still be combined with the GPL parts.

## Per-component licenses

| Component | License | SPDX identifier | Provenance |
|-----------|---------|-----------------|------------|
| `bits` (core) | GPL-3.0-or-later | `GPL-3.0-or-later` | derived from aliBuild |
| `common.bits`, `lcg.bits`, `stacks.bits` | GPL-3.0-or-later | `GPL-3.0-or-later` | recipes, derived from alidist |
| `bits-recipe-tools` | GPL-3.0-or-later | `GPL-3.0-or-later` | recipe helper snippets |
| `bits-providers` | GPL-3.0-or-later | `GPL-3.0-or-later` | provider/registry data |
| `bits-console` | GPL-3.0-or-later | `GPL-3.0-or-later` | web UI |
| `cvmfs-bits` (cvmfs-prepub) | Apache-2.0 | `Apache-2.0` | bits/CVMFS pipeline  |
| `cvmfs-testbed` | Apache-2.0 | `Apache-2.0` | deployment example |

Each licensed source file carries an `SPDX-License-Identifier` header (Python
modules and CLI scripts in `bits`; Go in `cvmfs-bits`; JS/config in
`bits-console`; shell/compose in `cvmfs-testbed`). Two deliberate exceptions
keep build hashes and generated output stable, and are governed by their
repository-level `LICENSE`/`COPYRIGHT` instead:

- the `bits-recipe-tools` recipe snippets (sourced and hashed by bits); and
- the `bits` build harness sourced into per-package builds or copied into
  tarballs (`bits_helpers/build_template.sh`, `tar_template.sh`,
  `relocate-me.sh`) and the Jinja scaffolding templates (`templates/*.jnj`).

The recipe repositories (`lcg.bits`, `common.bits`, `stacks.bits`) and the
`bits-providers` data repository are likewise covered by their repository-level
`LICENSE`/`COPYRIGHT` only — recipes are content-addressed, so per-file headers
are omitted to keep their hashes stable.

## Copyright & contributions

Copyright (C) CERN and the bits
project contributors. Work produced by CERN personnel is owned by CERN; please
involve CERN Knowledge Transfer before changing any license.

Contributions are accepted under the **Developer Certificate of Origin (DCO)**:
sign off your commits with `git commit -s`.

