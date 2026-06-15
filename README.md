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

Use `bits init` to write persistent settings to `bits.rc` (created in the current directory):

```bash
bits init --organisation LHCB \
          --work-dir /path/to/sw \
          --remote-store https://mybucket/builds
```

Or write `bits.rc` by hand (INI format, `[bits]` section):

```ini
[bits]
organisation      = LHCB
work_dir          = /path/to/sw
remote_store      = https://s3.cern.ch/swift/v1/alibuild-repo
prerequisites_url = https://lhcb-software.web.cern.ch/
cvmfs_repos       = /cvmfs/lhcbdev.cern.ch,/cvmfs/sft.cern.ch
```

`organisation` is written **uppercase** (`ALICE`, `LHCB`, …).  Bits lowercases it
internally when resolving the community recipe repository from bits-providers
(e.g. `LHCB` → `lhcb.bits.sh` → `https://github.com/bitsorg/lhcb.bits`).

Bits looks for `bits.rc` in: `--rc-file FILE` → `./bits.rc` → `./.bitsrc` → `~/.bitsrc`.

Useful `[bits]` keys:

| Key | CLI flag | Description |
|-----|----------|-------------|
| `organisation` | `--organisation` | Community name (uppercase). Used to auto-bootstrap the recipe repo. |
| `work_dir` | `-w` / `--work-dir` | Output directory for built packages (default: `sw`). |
| `remote_store` | `--remote-store` | Binary store URL for pre-built tarball retrieval. |
| `write_store` | `--write-store` | Binary store URL for uploading newly built tarballs. |
| `prerequisites_url` | — | URL shown when `bits doctor` cannot find the C++ compiler or git. |
| `cvmfs_repos` | — | Comma-separated CVMFS mount paths checked by `bits doctor --runner`. |
| `provider_policy` | — | `name:prepend\|append` pairs controlling `BITS_PATH` insertion order. |
| `store_integrity` | `--store-integrity` | `true` to enable SHA-256 verification of every recalled tarball. |

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
```
# Licensing

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
| `common.bits`, `lcg.bits` | GPL-3.0-or-later | `GPL-3.0-or-later` | recipes, derived from alidist |
| `bits-recipe-tools` | GPL-3.0-or-later | `GPL-3.0-or-later` | recipe helper snippets |
| `bits-console` | GPL-3.0-or-later | `GPL-3.0-or-later` | web UI |
| `cvmfs-bits` (cvmfs-prepub) | Apache-2.0 | `Apache-2.0` | new Go service |
| `cvmfs-testbed` | Apache-2.0 | `Apache-2.0` | deployment example |

Repositories that do not yet carry a `LICENSE` file (`bits-providers`,
`stacks.bits`, `remote-runner`) should adopt **GPL-3.0-or-later** to match the
recipe/core tooling.

Each licensed source file carries an `SPDX-License-Identifier` header. The
`bits-recipe-tools` snippets are a deliberate exception: they are hashed by
bits for content addressing, so per-file headers are omitted there to keep
build hashes stable — that repository's `LICENSE` and `COPYRIGHT` govern all
its files.

## Copyright & contributions

Copyright (C) CERN (European Organization for Nuclear Research) and the bits
project contributors. Work produced by CERN personnel is owned by CERN; please
involve CERN Knowledge Transfer before changing any license.

Contributions are accepted under the **Developer Certificate of Origin (DCO)**:
sign off your commits with `git commit -s`.

