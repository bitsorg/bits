# Bits - Quick Start Guide

Bits is a build orchestration tool for complex software stacks. It fetches sources, resolves dependencies, and builds packages in a reproducible, parallel environment.

> Full documentation is available in [docs/USERGUIDE.md](docs/USERGUIDE.md), [docs/COOKBOOK.md](docs/COOKBOOK.md), and [docs/REFERENCE.md](docs/REFERENCE.md). This guide covers only the essentials.

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

**Requirements**: Python 3.8+, git, and [Environment Modules](https://modules.sourceforge.net/) (`modulecmd`).  
On macOS: `brew install modules`  
On Debian/Ubuntu: `apt-get install environment-modules`  
On RHEL/CentOS: `yum install environment-modules`

---

## Quick Start

### ALICE (default community — no configuration needed)

```bash
# In any empty directory: bits auto-bootstraps the ALICE recipe repo
bits build ROOT

# Enter the built environment and run
bits enter ROOT/latest
root -b
exit
```

### Check system requirements before building

```bash
bits doctor ROOT
```

---

## Basic Commands

| Command | Description |
|---------|-------------|
| `bits build <pkg>` | Build a package and its dependencies. |
| `bits enter <pkg>/latest` | Spawn a subshell with the package environment loaded. |
| `bits load <pkg>` | Print commands to load a module (must be `eval`'d). |
| `bits q [regex]` | List available modules. |
| `bits clean` | Remove stale build artifacts from a temporary build area. |
| `bits cleanup` | Evict old or infrequently used packages from a persistent workDir. |
| `bits doctor <pkg> [<pkg>...]` | Check that the system satisfies all recipe requirements before building. |
| `bits doctor --runner` | Validate the full build-runner environment (compiler, git, Docker, podman, CVMFS, disk, store). |
| `bits verify --from-manifest FILE` | Confirm a live deployment matches the build manifest (SHA-256 and provider commits). |

[Full command reference](docs/REFERENCE.md#16-command-line-reference)

---

## Configuration

Run `bits init` with configuration options (and no package) to record them as a
per-directory profile, so you do not repeat them on every build:

```bash
bits init --work-dir /path/to/sw \
          --remote-store https://mybucket/builds
```

The profile is stored in `./.bitsuse` (or under `~/.bits/use/` when the current
directory is not writeable). `--architecture` is saved to its `[common]` section;
`--remote-store`, `--write-store`, `--defaults`, `-c/--config-dir`,
`-w/--work-dir` and `--reference-sources` are saved to `[build]`. `bits use`
records the same kind of profile from any command's flags (e.g.
`bits use build --docker`, or `bits use build --store-integrity` to enable
SHA-256 verification of every recalled tarball).

Global settings come from environment variables:

| Variable | Related flag | Description |
|----------|--------------|-------------|
| `$BITS_ORGANISATION` | `--organisation` | Community name (uppercase). Used to auto-bootstrap the recipe repo. |
| `$BITS_WORK_DIR` | `-w` / `--work-dir` | Output directory for built packages (default: `sw`). |
| `$BITS_REPO_DIR` | `-c` / `--config-dir` | Root directory for recipe repositories. |
| `$BITS_PROVIDERS` | `--providers` | Repository provider set URL(s). |
| `$BITS_PATH` | `--search-path` | Recipe search path. |
| `$BITS_S3_STORE` | `--remote-store` (store ops) | Default S3 store for `gc` / `certify` / `publish` / `store-stats` / `compliance`. |
| `$BITS_PREREQUISITES_URL` | — | URL shown when `bits doctor` cannot find the C++ compiler or git. |
| `$BITS_CVMFS_REPOS` | `--cvmfs-repos` | Comma-separated CVMFS mount paths checked by `bits doctor --runner`. |

`$BITS_ORGANISATION` is set **uppercase** (`ALICE`, `LHCB`, …).  Bits lowercases it
internally when resolving the community recipe repository from bits-providers
(e.g. `LHCB` → `lhcb.bits.sh` → `https://github.com/bitsorg/lhcb.bits`).

[Configuration details](docs/USERGUIDE.md#4-configuration)

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
bits cleanup --max-age 14         # evict packages not used in the last 14 days
bits cleanup --min-free 100       # free space until at least 100 GiB available
bits cleanup -n                   # dry-run: show what would be removed
```

[Cleaning options](docs/USERGUIDE.md#7-cleaning-up)

---

## Docker & Remote Builds

```bash
# Build inside a Docker container for a specific Linux version
bits build --docker --architecture ubuntu2004_x86-64 ROOT

# Cross-compile for ARM64 on an x86-64 host (requires QEMU binfmt handlers)
bits build --docker --architecture slc9_aarch64 MyAnalysis

# Use a remote binary store (S3, HTTP, rsync) to share pre-built artifacts
bits build --remote-store s3://mybucket/builds ROOT
```

The `--cvmfs-prefix` flag (which embeds the final CVMFS deployment path at compile time so no relocation is needed at publish time) and `bits publish --no-relocate` are used by the **bits-console-triggered CI pipeline** on the build runners — they are not normally typed by end users. See [WORKFLOWS.md Phase 5](docs/WORKFLOWS.md#phase-5--ci-build-and-cvmfs-publication-via-bits-console) for the user-facing workflow and [docs/REFERENCE.md §22](docs/REFERENCE.md#22-docker-support) for the flag reference.

[Docker support](docs/REFERENCE.md#22-docker-support) | [Cross-compilation via QEMU](docs/REFERENCE.md#222-cross-compilation-via-qemu) | [Remote stores](docs/REFERENCE.md#21-remote-binary-store-backends)

---

## Validating Builds and Deployments

```bash
# Check runner environment before first use (compiler, git, Docker, podman, disk…)
bits doctor --runner --cvmfs-repos /cvmfs/alice.cern.ch

# Machine-readable runner report for CI / bits-console health panel
bits doctor --runner --json

# Verify that a live CVMFS deployment matches a recorded build manifest
bits verify --from-manifest alice-o2-20260411.json \
            --cvmfs-root /cvmfs/alice.cern.ch
```

[bits doctor reference](docs/REFERENCE.md#bits-doctor) | [bits verify reference](docs/REFERENCE.md#23-bits-verify--deployment-verification)

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

