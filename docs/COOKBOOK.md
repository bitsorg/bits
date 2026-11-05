# Bits — Cookbook

> **See also:** [User Guide](USERGUIDE.md) · [Reference Manual](REFERENCE.md) · [Workflows](WORKFLOWS.md)

Practical recipes for common bits tasks. Each entry is self-contained; refer to the [User Guide](USERGUIDE.md) for background and to the [Reference Manual](REFERENCE.md) for complete flag documentation.

- [Building and using a stack](#building-and-using-a-stack)
- [Developing packages](#developing-packages)
- [Versions, sources and recipe repositories](#versions-sources-and-recipe-repositories)
- [Binary stores and CI](#binary-stores-and-ci)
- [Disk space and deployments](#disk-space-and-deployments)
- [Writing Recipes with bits-recipe-tools](#writing-recipes-with-bits-recipe-tools)

---

## Building and using a stack

### Build a complete stack from scratch

```bash
bits doctor ROOT            # verify system requirements first
bits build ROOT             # build everything
bits enter ROOT/latest      # drop into the built environment
```

### Use the built environment

Once a package such as ROOT is built, use it by loading its environment.

**Interactive sub-shell** — opens a new shell with all modules loaded; the prompt changes so it is clear you are inside a bits environment:

```bash
bits enter ROOT/latest
root -b
exit   # return to your normal shell
```

**Single command** — loads modules and exec's the command without spawning an interactive shell; exit code passes through unchanged:

```bash
bits setenv ROOT/latest -c root -b
```

**Persistent load/unload in the current shell** — add the shell helper to `~/.bashrc`, `~/.zshrc`, or `~/.kshrc` once:

```bash
export BITS_WORK_DIR=/path/to/sw
eval "$(bits shell-helper)"
```

Then in any shell session:

```bash
bits load ROOT/latest,Python/3.11-1   # load one or more modules
bits unload ROOT                       # unload (version can be omitted)
bits list                              # show currently loaded modules
```

Without `shell-helper`, use `eval` manually: `eval "$(bits load ROOT/latest)"`.

### Inspect the dependency graph before building

```bash
bits deps --outgraph deps.pdf ROOT   # requires Graphviz
```

Useful for understanding which packages will be built and in what order before committing to a long build. The PDF shows the full transitive dependency tree rooted at the requested package.

### Build for a different OS or architecture (Docker)

```bash
# Different Linux version
bits build --docker --architecture x86_64-ubuntu2404 ROOT

# Build aarch64 (ARM64) packages on an x86-64 machine (requires QEMU binfmt handlers)
bits build --docker --architecture aarch64-el9 MyAnalysis
```

The `--architecture` string selects both the OS image and the target CPU. When the target architecture differs from the host, bits automatically injects the matching Docker `--platform` flag so QEMU handles emulation transparently. See [§22.2 Cross-compilation via QEMU](REFERENCE.md#222-cross-compilation-via-qemu) for QEMU setup details.

### Speed up large builds

**Built-in scheduler** — `--parallel 4 --jobs 8` builds up to 4 packages at a time, sharing 8 compile jobs between them; the final top-level package gets all 8 (memory permitting). `--parallel` on its own uses 4; omit it entirely for a serial build (`--builders` is a kept alias):

```bash
bits build --parallel 4 --jobs 8 my_large_stack
```

The scheduler dispatches packages as soon as their dependencies are satisfied. Use `--resources` to declare per-package CPU and memory budgets and prevent overcommit (see [Building several packages at once](USERGUIDE.md#building-several-packages-at-once) and the `--resources` option of [`bits build`](REFERENCE.md#bits-build)).

**Prefetch remote tarballs** — when a binary store is in use (`--remote-store`/`--write-store`, or the platform default), bits fetches pre-built tarballs and source archives in the background while packages compile, with as many workers as `--parallel` (at most 4); `--prefetch-workers N` sets the number and `0` turns it off:

```bash
bits build --parallel 4 \
           --write-store b3://mybucket/store \
           --prefetch-workers 4 \
           my_large_stack
```

The `--parallel` scheduler also overlaps uploads with downstream builds.

**Parallel source downloads** — fetch multiple source archives concurrently within each package:

```bash
bits build --parallel-sources 4 my_large_stack
```

Useful when a recipe lists several large `sources:` URLs.

### Build memory-hungry packages without exhausting RAM

For packages whose parallel builds risk OOM, limit concurrent builds and/or declare per-package resource budgets:

```bash
# Option 1: reduce concurrent package builds (serial is the default; omit --parallel)
bits build --jobs 8 my_stack

# Option 2: use a resource file
bits build --parallel 4 --resources my_resources.json my_stack
```

Where `my_resources.json` has the format of the build statistics a monitored run writes to `sw/LOGS/<arch>/bits_build_stats.json`: `cpu` is about 100 per core, `rss` is in bytes, `time` is in seconds, `resources` is the machine total and `defaults` applies to packages not listed. Package names are matched case-insensitively. `--auto-resources` reuses a previous run's statistics instead of a file you write. The example is for a 16-core, 64 GiB machine:

```json
{
  "resources": {"cpu": 1600, "rss": 68719476736},
  "packages": {"build": {
    "gcc":  {"cpu": 400, "rss": 1073741824, "time": 1800},
    "llvm": {"cpu": 800, "rss": 4294967296, "time": 3600}
  }},
  "known": [],
  "defaults": {"cpu": [100], "rss": [536870912], "time": [60]}
}
```

The scheduler will not start a new build unless the declared resources are free.

### Debug a failed build

```bash
bits build --debug my_package
# A failed build's directory is kept; the BUILD FAILED message prints its path
cd sw/BUILD/my_package-latest/
cat log
# The build itself runs in the my_package/ subdirectory; re-run the failing command there
```

## Developing packages

### Develop and iterate on a single package

```bash
bits init -c . libfoo       # writable source checkout (run inside your recipe repository)
# … edit source in the libfoo/ directory …
bits build libfoo           # rebuilds only libfoo (devel mode)
eval "$(bits load libfoo/latest)"
```

### Iterate on a dependency without rebuilding the stack above it

Editing a low-level dependency normally re-hashes every package that depends on it, forcing a full rebuild of the stack. To rebuild **only the dependency** and reuse its consumers, list it under `untracked_requires:` instead of `requires:` in each consumer that can tolerate it:

```yaml
package: MyApp
version: "1.0"
requires:
  - ROOT
untracked_requires:
  - libfoo          # linked at runtime, but changes to it don't re-hash MyApp
```

A reused consumer's `init.sh` names the dependency by the `<version>-<revision>` it was built with. Give the dependency a **fixed install label** so that path stays the same and reused consumers pick up each new build:

```yaml
package: libfoo
version: "2.3"
force_revision: "dev"   # install path stays …/libfoo/2.3-dev across edits
```

```bash
bits init -c . libfoo   # writable checkout of the dependency
# … edit libfoo source …
bits build MyApp        # rebuilds libfoo only; MyApp is reused as is
```

Only `libfoo` rebuilds; `MyApp` (and everything above it) keeps its identity hash and is reused, picking up the new `libfoo` at run time through its `…/libfoo/2.3-dev` path.

**Caveats.** The consumer is *neither recompiled nor relinked*, so this is valid only while your change keeps `libfoo` binary-compatible (same headers and library version). Any build that includes an untracked dependency is recorded as `provenance: loose` in `.meta.json`, meaning its hash does not cover everything it was built against; it is still publishable, but the compatibility decision is yours.

Without a fixed label (bits warns), each change gives the dependency a new `<version>-<revision>`. A reused consumer set up on its own then keeps the revision it was built with, which must still be installed; it gets the new build only when that is set up first (its `<DEP>_REVISION` makes the consumer's `init.sh` skip the old path). A package that requires both, and tracks the dependency, sets the new one up first. The hash revision policy does not override an explicit `force_revision`. See [`untracked_requires`](REFERENCE.md#dependencies) in the reference.

## Versions, sources and recipe repositories

### Override a package version without editing the recipe

Defaults profiles can pin package versions globally without modifying recipe files. The `overrides:` block is a per-package patch merged into the spec after the recipe is parsed, so it can set `version`, `tag`, `source`, or any other field — and it takes precedence over the recipe's own values:

```yaml
# In defaults-myproject.sh
overrides:
  root:
    version: "6.32.02"
    tag: "v6-32-02"        # git tag to check out; %(version)s in source URLs follows version
  boost:
    version: "1.85.0"
```

Then build with:

```bash
bits build --defaults release::myproject MyStack
```

Keys are case-insensitive patterns that must match the whole package name, so `root` and `ROOT` both work and `clhep|geant4` matches both packages. This is the recommended way to pin versions: useful for shared recipes where different projects need different versions, or for emergency pinning when a new version breaks downstream packages.

### Pin a dependency's version from within a recipe

A recipe can pin the version of one of its dependencies directly in the `requires` / `build_requires` list using the `name = version` form. The pin sets both `version` and `tag` of the named dependency to the same string, so use it only for packages whose git tag equals their version (such as `fmt`); for ROOT-style tags (`v6-32-02`) use `overrides:` with both `version:` and `tag:`. Unlike `overrides:` keys, the name must be spelt exactly as in the dependency's `package:` field (e.g. `CMake` in lcg.bits):

```yaml
package: myanalysis
version: "1.0"
requires:
  - fmt = 11.1.4                  # pin fmt for this whole build
  - libfoo = 1.2.3:slc7.*         # pin only on slc7 architectures
  - libbar = 2.0:defaults=dev4    # pin only under --defaults dev4
  - ROOT                          # plain dependency, no pin
build_requires:
  - CMake
---
```

The optional `:matcher` suffix is the same architecture / `defaults=` condition used for conditional dependencies, so a pin can be made arch- or profile-specific. Only **one** version pin per dependency is allowed across the whole graph — two recipes pinning the same package to different versions is a fatal error. For most cases prefer the defaults `overrides:` block above; the in-recipe pin is handy when the constraint logically belongs to the consuming package.

### Pin the repository-provider (recipe-repo) version

A [repository provider](REFERENCE.md#13-repository-provider-feature) such as `lcg.bits` pulls an entire recipe repository from git. Which snapshot it pulls is controlled by the provider recipe's `tag:` field — a branch, tag, or commit hash:

```yaml
# bits-providers/lcg.bits.sh
package: lcg.bits
version: "1"
tag: "LCG_106"              # branch, tag, or commit; if omitted, `version` is used
provides_repository: true
source: https://github.com/bitsorg/lcg.bits
---
```

To choose the snapshot without editing the provider recipe, override its `tag:` (or `source:`) from a defaults profile; the key is the provider's name, matched exactly (case-insensitive, not as a pattern):

```yaml
# defaults-myproject.sh
overrides:
  lcg.bits:
    tag: "LCG_106"
```

Appending `@<tag>` to the bits-providers URL pins the registry repository itself (and so the `tag:` its `lcg.bits.sh` records), not `lcg.bits` directly:

```bash
export BITS_PROVIDERS="https://github.com/bitsorg/bits-providers@<registry-tag>"
```

A provider's commit does **not** enter the build hash: moving to a new snapshot rebuilds only the packages whose own recipes or inputs changed (and what depends on them). The commit used is recorded in the build manifest.

### Check out a recipe repository and develop against it

Native `bits` uses the provider path: `bits init <group>.bits` resolves the named recipe repository in the [bits-providers registry](REFERENCE.md#13-repository-provider-feature) and clones it into the current directory, so you can develop its packages — including ones whose recipes live in a *required* provider repository — beside it:

```bash
bits init alice.bits              # clone the alice.bits recipe repo into ./alice.bits
bits init -c alice.bits ROOT      # check out ROOT's source for development, beside it
# … edit ROOT under ./ROOT …
bits build -c alice.bits ROOT     # build with the local alice.bits recipes + your ROOT
```

`bits init -c alice.bits ROOT` loads the provider chain (e.g. `alice.bits` `requires: [alidist.bits]`), so a package whose recipe lives in a required provider repo is still found.

The **aliBuild** front-end is the legacy path instead: `aliBuild init` checks out `alisw/alidist`, and `aliBuild build ROOT` uses it directly with no provider registry and the legacy build-time `init.sh` (see [Repository Provider Feature](REFERENCE.md#13-repository-provider-feature)).

### Use a private recipe repository alongside the defaults

Set `BITS_PATH` to add a repository to the recipe search path. A plain name such as `myorg` means `<recipe dir>/myorg.bits`; an absolute path is used as is. These directories are searched *after* the main recipe directory:

```bash
BITS_PATH=myorg bits build MyPackage
```

Or record it for this directory (`--search-path` is the flag form of `BITS_PATH`; an exported `BITS_PATH` takes precedence):

```bash
bits use build --search-path myorg
```

Useful for building private packages that depend on public recipes without modifying the main recipe repository. Because the main recipe directory is searched first, a recipe it already has (e.g. `gcc`) is not replaced this way; a defaults `overrides:` block can change its fields (`version`, `tag`, `source`, …) but not its build script.

### Enforce reproducible source downloads with checksums

First, compute and write checksums for all sources:

```bash
bits build --write-checksums MyPackage
```

This creates or updates `checksums/mypackage.checksum` (lower-case package name) in the recipe directory. To record a whole recipe repository without building, run `bits checksums -c lcg.bits --write` (and `bits checksums -c stacks.bits --write --defaults all --recipes lcg.bits` for a repository of defaults profiles, for the sources their overrides introduce). Then enforce them on all future builds:

```bash
bits build --enforce-checksums MyPackage
```

Or make it the site default in a defaults profile:

```yaml
# defaults-production.sh
checksum_mode: enforce
```

Any mismatch or missing checksum aborts the build, catching supply-chain tampering or silent mirror corruption.

## Binary stores and CI

### Set up a project with a persistent binary store

Instead of passing `--remote-store` on every `bits build` invocation, record it once with `bits use`:

```bash
# One-time setup, inside your community repository — records a bits use profile
bits use build --remote-store https://store.example.com/store \
               --write-store  b3://mybucket/store

# Every later invocation from this directory picks up the settings
bits build ROOT
```

The store settings are saved to the profile's `[build]` section (`./.bitsuse`, or a record under `~/.bits/use/` when the directory is not writeable or not owned by you); `bits use` with no arguments shows what is saved.

### Share pre-built artifacts over S3

```bash
# CI: build and upload (boto3 backend; ::rw sets both --remote-store and --write-store)
export AWS_ACCESS_KEY_ID=ci-key
export AWS_SECRET_ACCESS_KEY=ci-secret
bits build --remote-store b3://mybucket/bits-cache::rw ROOT

# Developer workstation: fetch from the same cache, never upload
bits build --remote-store b3://mybucket/bits-cache ROOT
```

See [§21 Remote binary store backends](REFERENCE.md#21-remote-binary-store-backends) for the full list of backends (HTTP, S3 via s3cmd, S3-compatible via boto3, rsync or a local path; reuse from CVMFS is `--reuse-from`) and detailed CI/CD patterns.

### CI/CD: build and publish only on the main branch

Use conditional logic in CI to upload binaries only for production builds:

```bash
if [ "$CI_COMMIT_BRANCH" = "main" ]; then
  bits build --remote-store b3://mybucket/store \
             --write-store  b3://mybucket/store MyStack
else
  # Feature branches: download cached binaries but never upload
  bits build --remote-store b3://mybucket/store MyStack
fi
```

This ensures PR builds benefit from the shared cache without polluting the production store.

## Disk space and deployments

### Evict old packages to free disk space

```bash
# Evict packages not used in the last 14 days
bits prune --max-age 14

# Free space until at least 50 GiB is available, removing least-recently-used packages first
# (--max-age defaults to 7 days; --disk-pressure-only turns the age limit off)
bits prune --min-free 50 --disk-pressure-only

# Dry run: show what would be removed without deleting anything
bits prune --max-age 7 --min-free 100 -n
```

Bits tracks a sentinel file for each installed package; `bits prune` (formerly `bits cleanup`) sorts by last-touched time and evicts the oldest entries first. Combine both flags to enforce both a time limit and a disk-space floor in a single pass. See [§7 Cleaning up](USERGUIDE.md#7-cleaning-up) for full options.

### Verify a live deployment against a build manifest

After publishing to CVMFS (or any shared store), check that what is deployed is exactly what was built:

```bash
bits verify --from-manifest alice-o2-20260411.json \
            --cvmfs-root /cvmfs/alice.cern.ch
```

`bits verify` recomputes the SHA-256 of each package tarball in the store under `--cvmfs-root` (then `--work-dir`) and compares it with the manifest; each provider's recorded commit is compared with its local checkout, when there is one. A mismatch is reported as FAIL (exit code 1) and a missing tarball as MISS (exit code 2); running it on a machine of a different architecture than the manifest's is also a FAIL. The manifest is written automatically during `bits build` to `$WORK_DIR/MANIFESTS/`. See [§23 bits verify](REFERENCE.md#23-bits-verify--deployment-verification) for full options.

---

## Writing Recipes with bits-recipe-tools

[`bits-recipe-tools`](https://github.com/bitsorg/bits-recipe-tools) is an optional package that provides a higher-level recipe authoring style built around reusable shell function hooks. Instead of writing a flat Bash build script, the recipe author overrides only the steps that differ from the standard template.

### Plain Run() function (no external package needed)

Any recipe may define a `Run()` function directly. This is the simplest way to get clearly separated named phases without any dependencies:

```bash
Run() {
  cmake -S "$SOURCEDIR" -B "$BUILDDIR" \
        -DCMAKE_INSTALL_PREFIX="$INSTALLROOT"
  cmake --build "$BUILDDIR" --parallel "$JOBS"
  cmake --install "$BUILDDIR"
}
```

After sourcing the recipe, `build_template.sh` calls `Run "$@"`; a recipe without `Run()` gets a do-nothing default.

### How bits-recipe-tools works

`bits-recipe-tools` ships include files — `CMakeRecipe`, `AutoToolsRecipe`, `MesonRecipe`, `PythonRecipe` and others — each of which provides a `Run()` function that calls these hooks (shell functions) in order:

| Hook | Default behaviour |
|------|-------------------|
| `Prepare()` | Copies the source into a private build directory (the shared source tree is never modified). |
| `Configure()` | Runs `cmake` (or `./configure`) with standard flags. |
| `Make()` | Runs `make -j$JOBS` (or `cmake --build`). |
| `UnitTest()` | Does nothing. |
| `MakeInstall()` | Runs `make install` (or `cmake --install`). |
| `MakeModule()` | Writes the modulefile (see `MODULE_OPTIONS` below). |
| `PostInstall()` | Does nothing; override it for post-install fixups. |
| `Clean()` | Removes libtool `*.la` files from `$INSTALLROOT/lib`. |

A recipe overrides a hook by defining a function with the same name **after** the `. $(bits-include …)` line; all other hooks keep their defaults. `bits-recipe-tools` must be listed as a `build_requires` of the recipe.

`PythonRecipe` differs: its `PostInstall()` rewrites script shebangs (keep that logic if you override it), a `Preload` step follows, and its `Clean()` does nothing. It builds with `pip install --no-deps --no-build-isolation`, so the build backend, its tools and the package's runtime dependencies must all come from the recipe's requirements: for a meson-python package such as scikit-learn, `meson_python` in `requires` and `ninja` in `build_requires`.

### MODULE_OPTIONS — controlling modulefile generation

Set `MODULE_OPTIONS` **after** sourcing the include file, which resets it to `--bin --lib` (also the value you get if you do not set it). The `MakeModule` step reads it and writes the modulefile to `$INSTALLROOT/etc/modulefiles/<package>`:

```bash
. $(bits-include CMakeRecipe)
MODULE_OPTIONS="--bin --lib"
```

| Flag | Effect on the modulefile |
|------|--------------------------|
| `--bin` | Prepends `$INSTALLROOT/bin` to `PATH`, and defines `<PACKAGE>_ROOT` (as `--root`). |
| `--lib` | Prepends `$INSTALLROOT/lib` and `lib64` to `LD_LIBRARY_PATH` (`DYLD_LIBRARY_PATH` on macOS), and defines `<PACKAGE>_INCLUDE_DIR`. |
| `--cmake` | Adds `$INSTALLROOT` to `CMAKE_PREFIX_PATH`. |
| `--root` | Defines `<PACKAGE>_ROOT` (name uppercased, `-` turned into `_`) as `$INSTALLROOT`. |
| `--inc` | Defines `<PACKAGE>_INCLUDE_DIR` as `$INSTALLROOT/include`. |
| `--pkgconfig` | Prepends `lib/pkgconfig` and `lib64/pkgconfig` to `PKG_CONFIG_PATH`. |
| `--python` | Prepends `lib/pythonX.Y/site-packages` to `PYTHONPATH` (needs `PYTHON_MAJOR_MINOR`, e.g. from `PythonRecipe`). |
| `--pylib` | Prepends `$INSTALLROOT/lib` to `PYTHONPATH`. |
| `--pysite` | Prepends `lib/python/site-packages` to `PYTHONPATH`. |
| `--root-inc` | Prepends `$INSTALLROOT/include` to `ROOT_INCLUDE_PATH` (where ROOT looks for headers). |

PATH-style entries (bin, lib, lib64, pkgconfig, site-packages, `ROOT_INCLUDE_PATH`) are added only if the directory exists; the `_ROOT`, `_INCLUDE_DIR` and `CMAKE_PREFIX_PATH` settings are always written.

### Example — CMake library (cppgsl, header-only)

```yaml
package: cppgsl
version: "4.0.0"
source: https://github.com/microsoft/GSL.git
tag: "v4.0.0"
build_requires:
  - CMake
  - bits-recipe-tools
---
# Header-only: CMake discovery + ROOT variable, no runtime paths
. $(bits-include CMakeRecipe)
MODULE_OPTIONS="--cmake --root"

# Override only Configure to disable tests; everything else is inherited.
# Use $BITS_CMAKE_SRC / $BITS_CMAKE_BUILD: the default Make() and
# MakeInstall() work in $BITS_CMAKE_BUILD.
Configure() {
  cmake -S "$BITS_CMAKE_SRC" -B "$BITS_CMAKE_BUILD" \
        -DCMAKE_INSTALL_PREFIX="$INSTALLROOT" \
        -DCMAKE_INSTALL_LIBDIR=lib \
        ${CMAKE_PREFIX_PATH:+-DCMAKE_PREFIX_PATH="${CMAKE_PREFIX_PATH}"} \
        -DGSL_TEST=OFF \
        -DCMAKE_BUILD_TYPE=Release
}
```

### Example — Autotools library

```yaml
package: libfoo
version: "1.4.2"
source: https://example.com/libfoo.git
tag: "v1.4.2"
build_requires:
  - autoconf
  - automake
  - libtool
  - bits-recipe-tools
---
. $(bits-include AutoToolsRecipe)
MODULE_OPTIONS="--bin --lib --root"

# Default Configure() runs ./autogen.sh (if present), then ./configure --prefix="$INSTALLROOT",
# in the private copy of the source. Override to add custom options.
Configure() {
  if [ -f autogen.sh ]; then ./autogen.sh || return 1; fi
  ./configure \
    --prefix="$INSTALLROOT" \
    --enable-shared \
    --disable-static
}
```

### Example — Custom post-install fixup

Override `PostInstall()` to remove files that should not be installed:

```yaml
package: mylib
version: "2.0.0"
source: https://example.com/mylib.git
tag: "v2.0.0"
build_requires:
  - CMake
  - bits-recipe-tools
---
. $(bits-include CMakeRecipe)
MODULE_OPTIONS="--bin --lib --cmake"

PostInstall() {
  # Remove static archives and libtool metadata left by make install
  find "$INSTALLROOT/lib" \( -name '*.a' -o -name '*.la' \) -delete
}
```

There is nothing to call back into: the default `PostInstall()` does nothing, and `MakeModule` has already written the modulefile.

### Error handling in recipes — how failures are detected

bits runs every recipe body under `set -e` **and** `set -o pipefail`, captures the recipe's real exit code, and tees all output to the per-package `log`. When a build fails, the `BUILD FAILED` message includes an **Error excerpt** — the matched high-signal error lines plus the tail of the log — so you usually do not need to open the log by hand. The build directory is kept after a failure (its path is printed under **Build Directory**), so you can inspect it and iterate there by hand; add `--debug` for more verbose bits output.

Because failure detection relies on a command exiting non-zero, two authoring pitfalls can let a real error slip through. Both are recipe bugs, not framework bugs.

**Pitfall 1 — a non-final `&&`/`||` chain hides its own failure.** `set -e` does *not* abort when the failing command is on the left of an `&&`/`||` list (only the command after the final operator is checked). So if such a chain is **not** the last statement in a function, a failure in it is silently swallowed and execution continues:

```bash
# BAD: if wget/tar/cmake fails, set -e is suppressed (left of &&) and
#      `make` still runs — on missing or half-prepared inputs.
Run() {
  wget "$url" && tar xf "$tarball" && cmake "$SOURCEDIR" && cp -r extra .
  make -j"$JOBS"
  make install
}

# GOOD: separate statements so set -e fires on the first failure.
Run() {
  wget "$url"
  tar xf "$tarball"
  cmake "$SOURCEDIR"
  cp -r extra .
  make -j"$JOBS"
  make install
}
```

**Inside bits-recipe-tools hooks, `set -e` does not apply.** The include files run the hooks as one chain (`Prepare && Configure && Make && …`), and bash switches `set -e` off inside functions called from such a chain. In `Prepare()`, `Configure()`, `Make()` and the other hooks only the **last** command decides whether the hook fails, so add `|| return 1` to every earlier command that can fail:

```bash
Make() {
  wget "$url" || return 1
  tar xf "$tarball" || return 1
  make -j"$JOBS"
}
```

**Pitfall 2 — `pipefail` turns a legitimately non-zero pipeline element into a build failure.** A common idiom such as `grep -rl PATTERN . | xargs sed ...` aborts the whole build when `grep` finds no match (exit 1) — even though "no match" is fine. Guard the element whose non-zero exit is acceptable:

```bash
# BAD: aborts if grep matches nothing.
grep -rl g77 . | xargs --no-run-if-empty sed -i 's/\bg77\b/gfortran/g'

# GOOD: tolerate the empty-match case.
{ grep -rl g77 . || true; } | xargs --no-run-if-empty sed -i 's/\bg77\b/gfortran/g'
```

**Known limitation — errors that exit 0 are not intercepted.** bits can only detect a failure that produces a non-zero exit code. A third-party `configure` or `make` that prints errors but still returns 0 (e.g. a broken shell test inside a vendored `configure`) will not be caught automatically. Inspect the **Error excerpt** in the failure message or the full `log`; if a package "succeeds" but is incomplete, search its log for `error:` / `command not found` and fix the upstream script or make a sanity check the last command of `PostInstall()` so it fails loudly (e.g. `test -f "$INSTALLROOT/lib/libfoo.so"`).

---

*Back to [User Guide](USERGUIDE.md) · [Reference Manual](REFERENCE.md)*
