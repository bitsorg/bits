# Bits - Quick Start Guide

Bits is a build orchestration tool for complex software stacks. It fetches sources, resolves dependencies, and builds packages in a reproducible, parallel environment.

> Full documentation is available in [REFERENCE.md](REFERENCE.md). This guide covers only the essentials.

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

## Quick Start (Building ROOT)

```bash
# 1. Clone a recipe repository
git clone https://github.com/bitsorg/alice.bits.git
cd alice.bits

# 2. Check that your system is ready
bits doctor ROOT

# 3. Build ROOT and all its dependencies
bits build ROOT

# 4. Enter the built environment
bits enter ROOT/latest

# 5. Run the software
root -b

# 6. Exit the environment
exit
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
| `bits doctor <pkg>` | Verify system requirements. |

[Full command reference](REFERENCE.md#16-command-line-reference)

---

## Configuration

Create a `bits.rc` file (INI format) to set defaults:

```ini
[bits]
organisation = ALICE

[ALICE]
sw_dir       = /path/to/sw          # output directory
repo_dir     = /path/to/recipes     # recipe repository root
search_path  = common,extra         # additional recipe dirs (appended .bits)
```

Bits looks for `bits.rc` in: `--config FILE` → `./bits.rc` → `./.bitsrc` → `~/.bitsrc`.  
[Configuration details](REFERENCE.md#4-configuration)

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

[Complete recipe reference](REFERENCE.md#17-recipe-format-reference)

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

[Cleaning options](REFERENCE.md#7-cleaning-up)

---

## Docker & Remote Builds

```bash
# Build inside a Docker container for a specific Linux version
bits build --docker --architecture ubuntu2004_x86-64 ROOT

# Build with the workDir bind-mounted at the final CVMFS path inside the
# container — packages compile with their deployment paths already embedded,
# so no relocation step is needed at publish time.
bits build --docker --cvmfs-prefix /cvmfs/sft.cern.ch/lcg/releases ROOT

# Use a remote binary store (S3, HTTP, rsync) to share pre-built artifacts
bits build --remote-store s3://mybucket/builds ROOT
```

[Docker support](REFERENCE.md#22-docker-support) | [Remote stores](REFERENCE.md#21-remote-binary-store-backends)

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

[Developer guide](REFERENCE.md#part-ii--developer-guide)

---

## The bits Workflow: From Local Dev to CVMFS

bits uses a single toolchain from your laptop to experiment-wide CVMFS. Clone a package source next to your recipe checkout and bits detects it automatically, building your local version while resolving all other dependencies from the shared recipe repo. Once tested locally, the change follows an unbroken path: commit → recipe MR → CI build → `bits publish` → CVMFS. Group admins publish full experiment stacks; individual users can publish single packages to a separate namespace — both paths use the same commands and the same recipes.

See **[WORKFLOWS.md](WORKFLOWS.md)** for the full phase-by-phase walkthrough and workflow diagram.

---

## Next Steps

- [Development-to-deployment workflow & diagram](WORKFLOWS.md)
- [Environment management (`bits enter`, `load`, `unload`)](REFERENCE.md#6-managing-environments)
- [Dependency graph visualisation](REFERENCE.md#bits-deps)
- [Repository provider feature (dynamic recipe repos)](REFERENCE.md#13-repository-provider-feature)
- [Defaults profiles](REFERENCE.md#18-defaults-profiles)
- [Design principles & limitations](REFERENCE.md#24-design-principles--limitations)
- [CVMFS publishing pipeline & bits-console](REFERENCE.md#26-cvmfs-publishing-pipeline)

---

**Note**: Bits is under active development. For the most up-to-date information, see the full [REFERENCE.md](REFERENCE.md).
```
