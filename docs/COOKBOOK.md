# Bits — Cookbook

> **See also:** [User Guide](USERGUIDE.md) · [Reference Manual](REFERENCE.md) · [Workflows](WORKFLOWS.md)

Practical recipes for common bits tasks. Each entry is self-contained; refer to the [User Guide](USERGUIDE.md) for background and to the [Reference Manual](REFERENCE.md) for complete flag documentation.

---

### Build a complete stack from scratch

```bash
bits doctor ROOT            # verify system requirements first
bits build ROOT             # build everything
bits enter ROOT/latest      # drop into the built environment
```

### Inspect the dependency graph before building

```bash
bits deps --outgraph deps.pdf ROOT   # requires Graphviz
```

Useful for understanding which packages will be built and in what order before committing to a long build. The PDF shows the full transitive dependency tree rooted at the requested package.

### Build for a different OS or architecture (Docker)

```bash
# Different Linux version
bits build --docker --architecture ubuntu2004_x86-64 ROOT

# Cross-compile for ARM64 on an x86-64 host (requires QEMU binfmt handlers)
bits build --docker --architecture slc9_aarch64 MyAnalysis
```

The `--architecture` string selects both the OS image and the target CPU. When the target architecture differs from the host, bits automatically injects the matching Docker `--platform` flag so QEMU handles emulation transparently. See [§22.2 Cross-compilation via QEMU](REFERENCE.md#222-cross-compilation-via-qemu) for QEMU setup details.

### Develop and iterate on a single package

```bash
bits init libfoo            # create a writable source checkout
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

Give the dependency a **stable install label** so reused consumers keep finding it after it changes:

```yaml
package: libfoo
version: "2.3"
force_revision: "dev"   # install path stays …/libfoo/2.3-dev across edits
```

```bash
bits init libfoo        # writable checkout of the dependency
# … edit libfoo source …
bits build MyApp        # rebuilds libfoo only; MyApp is reused and relinks it
```

Only `libfoo` rebuilds; `MyApp` (and everything above it) keeps its identity hash and is reused, picking up the new `libfoo` through its `…/libfoo/2.3-dev` path.

**Caveats.** The consumer is *relinked, not recompiled*, so this is valid only while your change keeps `libfoo` ABI-compatible (same headers / soname). Any build whose closure includes an untracked dependency is recorded `provenance: loose` in `.meta.json` — still publishable, but you own the ABI decision. Without `force_revision` the dependency's path moves on each edit and reused consumers keep linking the previous build (bits warns). See [`untracked_requires`](REFERENCE.md#dependencies) in the reference.

### Debug a failed build

```bash
bits build --debug --keep-tmp my_package
# Build directory path is printed in the log
cd sw/BUILD/my_package-*/
cat log
# Re-run the failing command manually to iterate quickly
```

### Use the built environment

**Interactive sub-shell** — opens a new shell with all modules loaded; the prompt changes so it is clear you are inside a bits environment:

```bash
bits enter ROOT/latest
root -b
exit   # return to your normal shell
```

**Single command** — loads modules and exec's the command without spawning an interactive shell; exit code passes through unchanged:

```bash
bits setenv ROOT/v6-30 -c root -b
```

**Persistent load/unload in the current shell** — add the shell helper to `~/.bashrc`, `~/.zshrc`, or `~/.kshrc` once:

```bash
BITS_WORK_DIR=/path/to/sw
eval "$(bits shell-helper)"
```

Then in any shell session:

```bash
bits load ROOT/latest,Python/3.11-1   # load one or more modules
bits unload ROOT                       # unload (version can be omitted)
bits list                              # show currently loaded modules
```

Without `shell-helper`, use `eval` manually: `eval "$(bits load ROOT/latest)"`.

### Override a package version without editing the recipe

Defaults profiles can pin package versions globally without modifying recipe files. The `overrides:` block is a per-package patch merged into the spec after the recipe is parsed, so it can set `version`, `tag`, `source`, or any other field — and it takes precedence over the recipe's own values:

```yaml
# In defaults-myproject.sh
overrides:
  root:
    version: "6.32.02"
    tag: "v6-32-02"        # tarball URL %(version)s and the git tag both follow
  boost:
    version: "1.85.0"
```

Then build with:

```bash
bits build --defaults release::myproject MyStack
```

Keys are matched case-insensitively as `re.fullmatch` regexes, so `root` and `ROOT` both work and patterns such as `clhep|geant4` are valid. This is the recommended way to pin versions: useful for shared recipes where different projects need different versions, or for emergency pinning when a new version breaks downstream packages.

### Pin a dependency's version from within a recipe

A recipe can pin the version of one of its dependencies directly in the `requires` / `build_requires` list using the `name = version` form. The pin updates both `version` and `tag` of the named dependency:

```yaml
package: myanalysis
version: "1.0"
requires:
  - root = 6.32.02            # pin root for this whole build
  - boost = 1.85.0:slc7.*     # pin only on slc7 architectures
  - vdt = v0.4.4:defaults=dev4   # pin only under --defaults dev4
  - fmt                       # plain dependency, no pin
build_requires:
  - cmake
---
```

The optional `:matcher` suffix is the same architecture / `defaults=` condition used for conditional dependencies, so a pin can be made arch- or profile-specific. Only **one** version pin per dependency is allowed across the whole graph — two recipes pinning the same package to different versions is a fatal error. For most cases prefer the defaults `overrides:` block above; the in-recipe pin is handy when the constraint logically belongs to the consuming package.

### Pin the repository-provider (recipe-repo) version

A [repository provider](REFERENCE.md#13-repository-provider-feature) such as `lcg.bits` pulls an entire recipe repository from git. Which snapshot it pulls is controlled by the provider recipe's `tag:` field — a branch, tag, or commit hash:

```yaml
# bits-providers/lcg.bits.sh
package: lcg.bits
version: "1"
tag: "LCG_106"              # branch, tag, or commit; defaults to the repo's main branch
provides_repository: true
source: https://github.com/bitsorg/lcg.bits
---
```

To choose the snapshot at invocation time without editing the recipe, append `@<tag>` to the bits-providers URL:

```bash
export BITS_PROVIDERS="https://github.com/bitsorg/bits-providers@LCG_106"
# or per-invocation:
bits build --providers https://github.com/bitsorg/bits-providers@LCG_106 LCG
```

The provider's commit hash is folded into every dependent package's build hash, so changing the provider version triggers a rebuild of everything sourced from it. Note that provider repositories are cloned *before* defaults `overrides:` are applied, so an `overrides:` entry does **not** change which provider snapshot is fetched — use the `tag:` field or the `@<tag>` URL suffix instead.

### Use a private recipe repository alongside the defaults

Set `BITS_PATH` to prepend a custom repository to the search path:

```bash
BITS_PATH=myorg.bits bits build MyPackage
```

Or configure it persistently:

```bash
bits init --config-dir myorg.bits MyPackage
```

Useful for building private packages that depend on public recipes, or for maintaining a vendor-specific overlay (e.g. a fork of `gcc` with custom patches) without modifying the main recipe repository.

### Set up a project with a persistent binary store

Instead of passing `--remote-store` on every `bits build` invocation, write it once with `bits init`:

```bash
# One-time setup — writes bits.rc in the current directory
bits init --remote-store https://store.example.com/store \
          --write-store  b3://mybucket/store \
          --organisation MYORG

# Every subsequent invocation picks up the settings automatically
bits build ROOT
```

To check what will be written before touching the file system, add `--dry-run`. To update a single key in an existing `bits.rc` without replacing the whole file, add `--append`.

### Share pre-built artifacts over S3

```bash
# CI: build and upload (boto3 backend; ::rw sets both --remote-store and --write-store)
export AWS_ACCESS_KEY_ID=ci-key
export AWS_SECRET_ACCESS_KEY=ci-secret
bits build --remote-store b3://mybucket/bits-cache::rw ROOT

# Developer workstation: fetch from the same cache, never upload
bits build --remote-store b3://mybucket/bits-cache ROOT
```

See [§21 Remote binary store backends](REFERENCE.md#21-remote-binary-store-backends) for the full list of backends (HTTP, S3, boto3, rsync, CVMFS) and detailed CI/CD patterns.

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

Any mismatch or missing checksum aborts the build, catching supply-chain tampering or silent mirror corruption.

### Speed up large builds

**Built-in Python scheduler** — build up to N packages in parallel, each using M cores:

```bash
bits build --builders 4 --jobs 8 my_large_stack
```

The scheduler dispatches packages as soon as their dependencies are satisfied. Use `--resources` to declare per-package CPU and memory budgets and prevent overcommit (see [§5 Parallel build modes](REFERENCE.md#5-building-packages)).

**Makeflow** — hand the dependency graph to the external [CCTools Makeflow](https://ccl.cse.nd.edu/software/) engine:

```bash
bits build --makeflow my_large_stack

# Inspect what Makeflow generated if a build fails
cat sw/BUILD/*/makeflow/Makeflow
cat sw/BUILD/*/makeflow/log
```

**Pipelined upload** — overlap tarball upload with downstream builds and prefetch remote tarballs in the background (Makeflow only):

```bash
bits build --makeflow --pipeline \
           --write-store b3://mybucket/store \
           --prefetch-workers 4 \
           my_large_stack
```

`--pipeline` splits each rule into `.build` / `.tar` / `.upload` stages; `--prefetch-workers` hides network latency by fetching tarballs before the build loop needs them.

**Parallel source downloads** — fetch multiple source archives concurrently within each package:

```bash
bits build --parallel-sources 4 my_large_stack
```

Useful when a recipe lists several large `sources:` URLs.

### Build memory-hungry packages without exhausting RAM

For packages whose parallel builds risk OOM, limit concurrent builds and/or declare per-package resource budgets:

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

The Python scheduler will not start a new build unless the declared resources are free.

### Evict old packages to free disk space

```bash
# Evict packages not used in the last 14 days
bits cleanup --max-age 14

# Free space until at least 50 GiB is available, removing least-recently-used packages first
bits cleanup --min-free 50

# Dry run: show what would be removed without deleting anything
bits cleanup --max-age 7 --min-free 100 -n
```

Bits tracks a sentinel file for each installed package; `bits cleanup` sorts by last-touched time and evicts the oldest entries first. Combine both flags to enforce both a time limit and a disk-space floor in a single pass. See [§7 bits cleanup](USERGUIDE.md#7-cleaning-up) for full options.

### Verify a live deployment against a build manifest

After publishing to CVMFS (or any shared store), confirm that what is deployed matches what was built:

```bash
bits verify --from-manifest alice-o2-20260411.json \
            --cvmfs-root /cvmfs/alice.cern.ch
```

`bits verify` reads the SHA-256 digests and provider commit SHAs recorded in the manifest and checks them against the live files on disk. Any mismatch is reported as an error. The manifest is written automatically during `bits build` to `$WORK_DIR/MANIFESTS/`. See [§23 bits verify](REFERENCE.md#23-bits-verify--deployment-verification) for full options.

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

`build_template.sh` calls `Run "$@"` if the function is defined after sourcing the compiled recipe script.

### How bits-recipe-tools works

`bits-recipe-tools` ships include files — `CMakeRecipe`, `AutoToolsRecipe`, and others — each of which defines a `Run()` function that orchestrates the build in terms of five lifecycle hooks:

| Hook | Default behaviour |
|------|-------------------|
| `Prepare()` | Sets up the build directory and any pre-configure steps. |
| `Configure()` | Runs `cmake` (or `./configure`) with standard flags. |
| `Make()` | Runs `make -j$JOBS` (or `cmake --build`). |
| `MakeInstall()` | Runs `make install` (or `cmake --install`). |
| `PostInstall()` | Runs any post-install fixups (e.g. removing libtool archives). |

A recipe overrides only the hooks it needs to customise; all others run with sensible defaults. `bits-recipe-tools` must be listed as a `build_requires` of the recipe.

### MODULE_OPTIONS — controlling modulefile generation

Set `MODULE_OPTIONS` **before** sourcing the include file so the `PostInstall()` hook picks it up:

```bash
MODULE_OPTIONS="--bin --lib"
. $(bits-include CMakeRecipe)
```

| Flag | Effect on the modulefile |
|------|--------------------------|
| `--bin` | Prepends `$INSTALLROOT/bin` to `PATH`. |
| `--lib` | Prepends `$INSTALLROOT/lib` to `LD_LIBRARY_PATH`. |
| `--cmake` | Adds `$INSTALLROOT` to `CMAKE_PREFIX_PATH`. |
| `--root` | Defines `ROOT_<PACKAGE>` (uppercased name) as `$INSTALLROOT`. |

### Example — CMake library (cppgsl, header-only)

```yaml
package: cppgsl
version: "4.0.0"
source: https://github.com/microsoft/GSL.git
tag: "v4.0.0"
build_requires:
  - cmake
  - bits-recipe-tools
---
# Header-only: CMake discovery + ROOT variable, no runtime paths
MODULE_OPTIONS="--cmake --root"
. $(bits-include CMakeRecipe)

# Override only Configure to disable tests; everything else is inherited.
Configure() {
  cmake -S "$SOURCEDIR" -B "$BUILDDIR" \
        -DCMAKE_INSTALL_PREFIX="$INSTALLROOT" \
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
  - autotools
  - bits-recipe-tools
---
MODULE_OPTIONS="--bin --lib --root"
. $(bits-include AutotoolsRecipe)

# Default Configure() runs: "$SOURCEDIR/configure" --prefix="$INSTALLROOT"
# Override to add custom options.
Configure() {
  "$SOURCEDIR/configure" \
    --prefix="$INSTALLROOT" \
    --enable-shared \
    --disable-static
}
```

### Example — Custom post-install fixup

Override `PostInstall()` to remove files that should not be installed or to patch up the modulefile:

```yaml
package: mylib
version: "2.0.0"
source: https://example.com/mylib.git
tag: "v2.0.0"
build_requires:
  - cmake
  - bits-recipe-tools
---
MODULE_OPTIONS="--bin --lib --cmake"
. $(bits-include CMakeRecipe)

PostInstall() {
  # Remove static archives and libtool metadata left by make install
  find "$INSTALLROOT/lib" \( -name '*.a' -o -name '*.la' \) -delete
  # Call the default PostInstall to generate the modulefile
  defaults_PostInstall
}
```

Call `defaults_PostInstall` at the end of any `PostInstall()` override to ensure the modulefile is still generated correctly.

### Error handling in recipes — how failures are detected

bits runs every recipe body under `set -e` **and** `set -o pipefail`, captures the recipe's real exit code, and tees all output to the per-package `log`. When a build fails, the `BUILD FAILED` message now includes an **Error excerpt** — the matched high-signal error lines plus the tail of the log — so you usually do not need to open the log by hand. Re-run with `--debug` (and optionally `--keep-tmp`) to keep the build tree for manual iteration.

Because failure detection relies on a command exiting non-zero, two authoring pitfalls can let a real error slip through. Both are recipe bugs, not framework bugs.

**Pitfall 1 — a non-final `&&`/`||` chain hides its own failure.** `set -e` does *not* abort when the failing command is on the left of an `&&`/`||` list (only the command after the final operator is checked). So if such a chain is **not** the last statement in a function, a failure in it is silently swallowed and execution continues:

```bash
# BAD: if wget/tar/cmake fails, set -e is suppressed (left of &&) and
#      `make` still runs — on missing or half-prepared inputs.
Make() {
  wget "$url" && tar xf "$tarball" && cmake "$SOURCEDIR" && cp -r extra .
  make -j"$JOBS"
  make install
}

# GOOD: separate statements so set -e fires on the first failure.
Make() {
  wget "$url"
  tar xf "$tarball"
  cmake "$SOURCEDIR"
  cp -r extra .
  make -j"$JOBS"
  make install
}
```

**Pitfall 2 — `pipefail` turns a legitimately non-zero pipeline element into a build failure.** A common idiom such as `grep -rl PATTERN . | xargs sed ...` aborts the whole build when `grep` finds no match (exit 1) — even though "no match" is fine. Guard the element whose non-zero exit is acceptable:

```bash
# BAD: aborts if grep matches nothing.
grep -rl g77 . | xargs --no-run-if-empty sed -i 's/\bg77\b/gfortran/g'

# GOOD: tolerate the empty-match case.
{ grep -rl g77 . || true; } | xargs --no-run-if-empty sed -i 's/\bg77\b/gfortran/g'
```

**Known limitation — errors that exit 0 are not intercepted.** bits can only detect a failure that produces a non-zero exit code. A third-party `configure` or `make` that prints errors but still returns 0 (e.g. a broken shell test inside a vendored `configure`) will not be caught automatically. Inspect the **Error excerpt** in the failure message or the full `log`; if a package "succeeds" but is incomplete, search its log for `error:` / `command not found` and fix the upstream script or add a `MakeInstall()`/`PostInstall()` sanity check that fails loudly.

---

*Back to [User Guide](USERGUIDE.md) · [Reference Manual](REFERENCE.md)*
