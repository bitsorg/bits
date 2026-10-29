# Frequently asked questions

Short answers, each with a link to where the docs explain it in full. The help chat
in bits-console and the phone app answers from the same docs.

## Getting started

### How do I install bits?

Clone it with `git clone https://github.com/bitsorg/bits.git`, `cd bits`, add it to your PATH with `export PATH=$PWD:$PATH`, create and activate a virtual environment (`python3 -m venv .venv && source .venv/bin/activate`) and run `pip install -e .`. Activate the venv in every new shell; recent distributions refuse a system-wide pip install.

See: [Installing Bits](USERGUIDE.md#installing-bits), [Installation](../README.md#installation).

### What do I need installed to use bits?

bits itself needs only Python 3.8+ and git. What a build needs depends on where it runs: in a container (`bits build --docker`, recommended on Linux) the host needs docker or rootless podman and the builder image brings compilers, build tools and system libraries; for a native build the host must have the build tools and system `-devel` libraries the recipes expect. `bits doctor` checks either setup.

See: [2. Installation & Prerequisites](USERGUIDE.md#2-installation--prerequisites), [Installation](../README.md#installation).

### Should I build with --docker or natively on my Linux box?

Building in a container with `--docker` is recommended on Linux. Outside ALICE, only a container build reuses what the community's CI built; a native build hashes differently and builds everything. A native build is possible if you install the system packages the recipes expect.

See: [2. Installation & Prerequisites](USERGUIDE.md#2-installation--prerequisites), [Finding a platform with pre-built binaries](USERGUIDE.md#finding-a-platform-with-pre-built-binaries).

### How do I build on my Mac? The first bits build just wrote a Brewfile and stopped.

On macOS (Intel or Apple silicon) there is no container build: install the Xcode command-line tools (`xcode-select --install`) and `brew install modules gnu-tar`. The first `bits build ROOT` writes `sw/<arch>/Brewfile` and stops; install the listed formulae with `brew bundle --file sw/<arch>/Brewfile` and run `bits build ROOT` again. Alternatively `bits build --brew ROOT` installs missing formulae on demand, and `bits brew` writes the Brewfile without resolving a build.

See: [Building natively: what to install](USERGUIDE.md#building-natively-what-to-install).

### Which platforms does bits support?

Supported means three things. bits runs on RHEL/AlmaLinux/Rocky (any version) on x86-64, aarch64 and ppc64le, Ubuntu, Debian and Fedora on x86-64, and macOS on Intel and Apple silicon. Builder images exist for `x86_64-el8`, `x86_64-el9`, `x86_64-el10`, `aarch64-el9`, `x86_64-ubuntu2204`, `x86_64-ubuntu2404` and `x86_64-ubuntu2604`. Pre-built binaries exist for the platforms a community builds: all seven for LCG, CMS, Key4hep, LHCb and SHiP, the four EL ones for ATLAS, and five under its own names for ALICE.

See: [Platforms](USERGUIDE.md#platforms).

### What should I pass to --architecture?

Name the platform (CPU and OS) the community's CI builds: `x86_64-el8`, `x86_64-el9`, `x86_64-el10`, `aarch64-el9`, `x86_64-ubuntu2204`, `x86_64-ubuntu2404` or `x86_64-ubuntu2604` (a community may build fewer; ALICE uses older names such as `slc9_x86-64`). To run the result on this machine, pick the one matching its OS and CPU; the compiler and build type come from `--defaults`. The community's console Build dialog or `platforms:` in its `ui-config.yaml` lists its platforms.

See: [Before you start: pick the platform to build for](../README.md#before-you-start-pick-the-platform-to-build-for), [Finding a platform with pre-built binaries](USERGUIDE.md#finding-a-platform-with-pre-built-binaries).

### Bits architecture says slc9_x86-64 and my build rebuilds everything from scratch, why?

`slc9_x86-64` (and `ubuntu2404_x86-64` on Ubuntu 24.04) is the name bits gives the machine when no `--architecture` is given. These older names are not the CI's, so a build under them reuses nothing the CI built (ALICE is the exception). Build with `--docker --architecture x86_64-el9` and the community's defaults instead.

See: [Platforms](USERGUIDE.md#platforms), [Before you start: pick the platform to build for](../README.md#before-you-start-pick-the-platform-to-build-for).

### How do I see which packages will be downloaded and which built, before building?

Run a dry run with the same options as the build, in the recipe repository, e.g. `bits build --dry-run --docker --architecture x86_64-el9 --defaults gcc15::opt ROOT`. It computes every hash and prints per package installed, local tarball, from store, from overlay, or build, followed by a summary; nothing is downloaded or installed. With `--docker` it needs docker and pulls the builder image the first time, to read its fingerprint.

See: [Previewing a build: `--dry-run`](USERGUIDE.md#previewing-a-build---dry-run), [Finding a platform with pre-built binaries](USERGUIDE.md#finding-a-platform-with-pre-built-binaries).

### Dry run shows "build (unsigned)" for packages that are in the store - why won't it reuse them?

Remote reuse is fail-closed: a tarball from a remote store is only reused when a verified signed manifest vouches for it, and `build (unsigned)` means the package is in the store but no signed manifest does. `--no-require-signed-reuse` turns this off, e.g. to bootstrap a new store. Local and CVMFS artifacts are unaffected.

See: [Sharing binaries through a store](USERGUIDE.md#sharing-binaries-through-a-store), [Previewing a build: `--dry-run`](USERGUIDE.md#previewing-a-build---dry-run).

### I ran bits build ROOT in my home directory and it just stops. What am I missing?

bits always builds from a community recipe repository and uses the current directory as its recipe directory; in a directory without recipes a build stops. Clone your community's repository and run bits inside it, e.g. `git clone https://github.com/bitsorg/stacks.bits && cd stacks.bits`, or `bits init stacks.bits && cd stacks.bits` to resolve it in the bits-providers registry. Community repositories are `stacks.bits`, `alice.bits`, `atlas.bits`, `cms.bits`, `key4hep.bits`, `lhcb.bits` and `ship.bits`.

See: [1. Check out your community's recipe repository](USERGUIDE.md#1-check-out-your-communitys-recipe-repository).

### How does bits decide whether a package needs rebuilding?

Every package gets a hash computed from its recipe, its sources, the hashes of the packages it needs and the build settings. A package whose hash was built before is taken, not rebuilt: from your `sw/`, from a binary store, or, when asked with `--reuse-from`, from what is already published on CVMFS. Change one recipe and only that package and what depends on it are rebuilt.

See: [How bits works](USERGUIDE.md#how-bits-works).

### What does --defaults gcc15::opt mean?

Build settings such as compiler, build type and publish location come from `defaults-<name>.sh` files, chained with `::`. `--defaults gcc15::opt` is GCC 15, optimised; `release` is always the base and comes first (`--defaults gcc15` means `release::gcc15`), and the default is `release`. A few values are set with `--set`, e.g. `--set release=LCG_110`.

See: [How bits works](USERGUIDE.md#how-bits-works), [Common options](USERGUIDE.md#common-options).

### I'm tired of typing --docker --architecture ... on every command. Is there a config file?

Record the options once per directory with `bits use`, e.g. `bits use build --docker --architecture x86_64-el9 --defaults gcc15::opt`; options without a section name go to `[common]`, applied to every architecture-aware command. The profile is saved in `./.bitsuse` and inserted before your own arguments, so an explicit flag still wins. Each `bits use SECTION ...` replaces that whole section, `bits use` alone shows the active profile and `bits use --clear [SECTION]` removes it.

See: [4. Configuration](USERGUIDE.md#4-configuration), [Finding a platform with pre-built binaries](USERGUIDE.md#finding-a-platform-with-pre-built-binaries).

### Which wins, the command line, bits use, environment variables or site config?

A setting is taken from the first that gives it: the command line, the `bits use` profile, the environment, the community's defaults (`system:` block of `defaults-*.sh`), the site configuration (`NN-<name>.yaml`), then bits' own `00-bits.yaml`. The exception is `$BITS_PATH`, which when set overrides `--search-path`.

See: [Where defaults come from](USERGUIDE.md#where-defaults-come-from).

### How do I upload my builds to our binary store?

Use `--remote-store URL::rw` to reuse from and upload to the same store, e.g. `bits build --remote-store b3://mybucket::rw ROOT`; without `::rw` the store is only read. `--write-store URL` pushes newly built tarballs; given alone it is also the read store, except where a default remote store applies, where you use `--remote-store URL::rw`. To upload a single package you already built, use `bits store upload PKG`.

See: [Sharing binaries through a store](USERGUIDE.md#sharing-binaries-through-a-store), [Common options](USERGUIDE.md#common-options).

### I built ROOT, how do I actually use it?

Run `bits enter ROOT/latest`: a sub-shell opens with ROOT and all its dependencies set up, and the prompt is prefixed with the loaded modules, e.g. `[ROOT/latest]`. Type `exit` to return to your normal shell. `bits q [REGEXP]` lists the available modules; nesting `bits enter` inside another bits environment is blocked.

See: [Enter a sub-shell with modules loaded](USERGUIDE.md#enter-a-sub-shell-with-modules-loaded), [Load / unload in the current shell](USERGUIDE.md#load--unload-in-the-current-shell).

### Bits load ROOT/latest just prints stuff and my environment doesn't change

`bits load` prints commands that must be `eval`'d: `eval "$(bits load ROOT/latest)"`. Or add the shell helper to your `.bashrc` or `.zshrc` once (`BITS_WORK_DIR=/path/to/sw` and `eval "$(bits shell-helper)"`), after which `bits load` and `bits unload` change the current shell directly.

See: [Load / unload in the current shell](USERGUIDE.md#load--unload-in-the-current-shell), [Basic Commands](../README.md#basic-commands).

### What's the difference between bits clean and bits prune? My disk is full.

`bits clean` removes temporary build artifacts (`TMP/`, `INSTALLROOT/`, unreferenced `BUILD/` directories and installed versions no `latest*` symlink points to); `--aggressive-cleanup` also removes `SOURCES/` and the store tarballs, keeping `MIRROR/`. `bits prune` evicts packages from a persistent workDir, by age (`--max-age DAYS`, default 7) or until free space is reached (`--min-free GIB`); it is meant for persistent CI build caches. Both take `-n`/`--dry-run` to show what would be removed.

See: [bits clean — remove temporary build artifacts](USERGUIDE.md#bits-clean--remove-temporary-build-artifacts), [bits prune — evict packages from a persistent workDir](USERGUIDE.md#bits-prune--evict-packages-from-a-persistent-workdir).

## The console, scheduled builds and CI

### How do I build the full alice software stack on my laptop?

Clone the community recipe repository and build from inside it: `git clone https://github.com/bitsorg/alice.bits.git`, `cd alice.bits`, then `bits build ROOT`, which resolves the dependency graph and builds everything. Afterwards `bits enter ROOT/latest` opens a sub-shell with the environment loaded.

See: [Phase 1 — Local setup from shared recipes](WORKFLOWS.md#phase-1--local-setup-from-shared-recipes).

### I cloned mylib next to my alice.bits checkout. What flag tells bits build to use my local copy?

None is needed. `bits build mylib` scans for directories adjacent to (or below) the recipe checkout that share a name with a package and uses such a directory as that package's source instead of the upstream remote; there is no flag to pass, no environment variable to set and no copy of the recipe to edit. All other packages still resolve from the recipe repository, so `mylib` is compiled against the same dependency versions CI uses.

See: [Build with the local checkout](WORKFLOWS.md#build-with-the-local-checkout).

### How can I be sure my change will build in CI before I push it?

Run the build inside the builder image of the platform CI builds, e.g. `bits build --docker --architecture x86_64-el9 mylib`. This spins up the official builder container, bind-mounts the current directory (including your local `mylib/` checkout) and runs the build inside it; the docs say that if this succeeds, CI will succeed for the same reason.

See: [Validate in the CI image](WORKFLOWS.md#validate-in-the-ci-image).

### How do I log in to bits-console?

Go to bits-console.web.cern.ch, select your community, and sign in with your GitLab personal access token. The bits-console documentation lists the required token scopes and role setup.

See: [Step 1 — Connect to bits-console](WORKFLOWS.md#step-1--connect-to-bits-console).

### I only see 'Build → Personal area' in the build dialog, where is Build → Production?

Build → Production is shown only to `bits-admin` and `group-admin` users; everyone with Developer access gets Build → Personal area, which publishes to a personal CVMFS namespace such as `/cvmfs/sft.cern.ch/lcg/user/jsmith/ROOT/6.30/x86_64-el9`. The choice is not a command-line flag but your role in the project, enforced server-side: even a crafted API call is rejected unless your `GITLAB_USER_LOGIN` is listed in the project's `GROUP_ADMINS_<NAME>` CI variable.

See: [Step 2 — Browse and trigger a build](WORKFLOWS.md#step-2--browse-and-trigger-a-build), [Phase 4 — CI build and CVMFS publication via bits-console](WORKFLOWS.md#phase-4--ci-build-and-cvmfs-publication-via-bits-console).

### How do I set up a nightly build in the console?

On the Schedules tab click + New slot, fill it in and Save, or set up the build in the Build dialog and click 💾 Save as pipeline. A slot saved from the Build dialog is paused, runs every day at 02:00 and has no variables or followed branches, so Edit it first and then Resume it. If the slot certifies, also click Approve and then Use your passkey.

See: [Step 4 — Schedule nightly builds](WORKFLOWS.md#step-4--schedule-nightly-builds).

### My nightly cell says 'not run' - why would a nightly be skipped?

At its time the orchestrator queues a build per cell, and they start as build hosts become free. A cell whose last build is still running skips the night, and a build that finds no free host for 12 hours is not run; a night missed by less than 6 hours still runs. The nightly matrix shows 'not run' together with the reason.

See: [Step 4 — Schedule nightly builds](WORKFLOWS.md#step-4--schedule-nightly-builds).

### Can a slot build automatically whenever someone pushes to ROOT master?

Yes. In the slot's Build on each commit to field, list up to 8 `https://host/path.git@branch` entries separated by `;`, e.g. `https://github.com/root-project/root.git@master`. The list only decides when the slot builds: the build still takes its recipes and versions as a nightly does, so following a source branch helps only when the slot's recipes build that branch (as dev3 builds ROOT `master`). Follow its recipe repositories replaces the list with the community's recipe repository at the branch the slot's `release` variable names (else `main`), plus those of its extra recipe repositories that name a branch.

See: [Step 5 — Build on each push (CI)](WORKFLOWS.md#step-5--build-on-each-push-ci).

### I pushed to a followed branch but no CI build has started

The orchestrator reads each branch's head every 2 minutes and waits until the branches have not moved for 5 minutes (30 minutes at most after the first new commit), then builds every cell once for the burst. A slot runs one CI build at a time, so commits pushed meanwhile are built together when it ends. Also, a branch just added is not built (its next commit is), a paused slot follows nothing, and only public repositories can be followed; no webhook or token is used.

See: [Step 5 — Build on each push (CI)](WORKFLOWS.md#step-5--build-on-each-push-ci).

### Do CI builds on push get certified or published to CVMFS?

No. A CI build goes to the binary store only; it is never certified nor published to CVMFS and just shows whether the branches build. Its results have their own matrix: the console's CI builds page shows each slot's latest 10 builds and the phone app's CI tab its latest 7.

See: [Step 5 — Build on each push (CI)](WORKFLOWS.md#step-5--build-on-each-push-ci).

### How can I check that what's on CVMFS is exactly what was built?

Run `bits verify --from-manifest <manifest.json> --cvmfs-root /cvmfs/alice.cern.ch`. The manifest is written automatically during `bits build` to `$WORK_DIR/MANIFESTS/`, and bits verify recomputes each package tarball's SHA-256 and compares it with the manifest. A mismatch is reported as FAIL (exit code 1) and a missing tarball as MISS (exit code 2).

See: [Phase 5 — Verify the deployment](WORKFLOWS.md#phase-5--verify-the-deployment), [Verify a live deployment against a build manifest](COOKBOOK.md#verify-a-live-deployment-against-a-build-manifest).

### How do I load and unload bits modules in my current shell instead of a sub-shell?

Add `export BITS_WORK_DIR=/path/to/sw` and `eval "$(bits shell-helper)"` once to `~/.bashrc`, `~/.zshrc` or `~/.kshrc`. Then use `bits load ROOT/latest,Python/3.11-1`, `bits unload ROOT` (the version can be omitted) and `bits list` to show what is loaded. Without the shell helper, use `eval "$(bits load ROOT/latest)"`.

See: [Use the built environment](COOKBOOK.md#use-the-built-environment).

### Bits builds my packages one after another, how do I build several at once?

Use the built-in scheduler, e.g. `bits build --parallel 4 --jobs 8 my_large_stack` builds up to 4 packages at a time sharing 8 compile jobs, and the final top-level package gets all 8 (memory permitting). `--parallel` on its own uses 4, and without it the build is serial (`--builders` is a kept alias). To avoid running out of memory, declare per-package CPU and memory budgets with `--resources <file>` or reuse a previous run's statistics with `--auto-resources`.

See: [Speed up large builds](COOKBOOK.md#speed-up-large-builds), [Build memory-hungry packages without exhausting RAM](COOKBOOK.md#build-memory-hungry-packages-without-exhausting-ram).

### BUILD FAILED for my package - where's the log and how do I debug it?

The BUILD FAILED message includes an Error excerpt (the matched error lines plus the tail of the log) and prints the path of the build directory, which is kept after a failure. There you find the full `log` (e.g. `sw/BUILD/my_package-latest/log`); the build itself runs in its `my_package/` subdirectory, where you can re-run the failing command by hand. Re-run with `bits build --debug my_package` for more verbose bits output.

See: [Debug a failed build](COOKBOOK.md#debug-a-failed-build), [Error handling in recipes — how failures are detected](COOKBOOK.md#error-handling-in-recipes--how-failures-are-detected).

### How do I build a different ROOT version without editing the recipe?

Add an `overrides:` block to a defaults profile, e.g. in `defaults-myproject.sh` give `root:` a `version: "6.32.02"` and `tag: "v6-32-02"`; it is merged into the spec after the recipe is parsed and takes precedence over the recipe's values. Then build with `bits build --defaults release::myproject MyStack`. Keys are case-insensitive patterns that must match the whole package name, so `root` and `ROOT` both work.

See: [Override a package version without editing the recipe](COOKBOOK.md#override-a-package-version-without-editing-the-recipe).

### How do I make bits refuse sources whose checksum doesn't match?

First record checksums with `bits build --write-checksums MyPackage`, which writes `checksums/mypackage.checksum` in the recipe directory. Then build with `bits build --enforce-checksums MyPackage`, or set `checksum_mode: enforce` in a defaults profile to make it the site default. Any mismatch or missing checksum then aborts the build.

See: [Enforce reproducible source downloads with checksums](COOKBOOK.md#enforce-reproducible-source-downloads-with-checksums).

### Is there still a bits cleanup command? I need to free disk space in sw/

`bits cleanup` was renamed `bits prune`; the old name still works as a deprecated alias that warns and forwards. `bits prune --max-age 14` evicts packages not used in the last 14 days, `bits prune --min-free 50 --disk-pressure-only` frees space until 50 GiB is available, least-recently-used first and with no age limit, and `-n` does a dry run. Separately, `bits clean` removes stale BUILD/ directories and the TMP/ staging area.

See: [Evict old packages to free disk space](COOKBOOK.md#evict-old-packages-to-free-disk-space), [Clean up the workDir](WORKFLOWS.md#clean-up-the-workdir).

### A command in my Configure() hook failed but the build carried on as if nothing happened - why?

Inside bits-recipe-tools hooks `set -e` does not apply: the include files run the hooks as one chain (`Prepare && Configure && Make && …`) and bash switches `set -e` off in functions called from such a chain. Only the last command of a hook decides whether it fails, so add `|| return 1` to every earlier command that can fail.

See: [Error handling in recipes — how failures are detected](COOKBOOK.md#error-handling-in-recipes--how-failures-are-detected).

## Writing recipes and repositories

### How do I write a new recipe? What should the file be called and what goes in it?

A recipe is a file named after its package in lower case, `<package>.sh` (e.g. `python.sh` for `Python`), placed in a recipe repository, by convention a `*.bits` directory. It has two sections separated by a line containing only `---`: a YAML header with package metadata, dependencies and environment, and a Bash build script with the actual build steps.

See: [12. Writing Recipes](REFERENCE.md#12-writing-recipes).

### In my recipe can I just run ./configure and make inside $SOURCEDIR?

No. The build script runs in `$BUILDDIR`, and you should build from a copy, never inside `$SOURCEDIR`. The minimal recipe copies the sources first with `rsync -a --delete --exclude '**/.git' "$SOURCEDIR"/ ./`, then runs `./configure --prefix="$INSTALLROOT"`, `make -j${JOBS:-1}` and `make install`.

See: [Minimal recipe](REFERENCE.md#minimal-recipe).

### I set env: FOO_INCLUDE: "$INSTALLROOT/include" in my recipe but packages that depend on it get a wrong path

`env`, `prepend_path` and `append_path` values are written into the package's `etc/profile.d/init.sh` and evaluated when that file is sourced, for example while a dependent package builds. Refer to the install location as `$<PACKAGE>_ROOT` (e.g. `$BOOST_ROOT`, which bits exports automatically), not `$INSTALLROOT`, which is only valid while the package itself builds.

See: [Annotated Boost recipe (showing environment fields)](REFERENCE.md#annotated-boost-recipe-showing-environment-fields).

### Do I need to add my package's bin and lib dirs to PATH and LD_LIBRARY_PATH with prepend_path?

No. bits already prepends the package's `bin`, `lib`/`lib64` and `lib*/pkgconfig` directories to `PATH`, `LD_LIBRARY_PATH` (`DYLD_LIBRARY_PATH` on macOS), `LIBRARY_PATH` and `PKG_CONFIG_PATH`, so these need not be listed. Use `prepend_path` for other variables, such as `ROOT_INCLUDE_PATH` in the Boost example.

See: [Annotated Boost recipe (showing environment fields)](REFERENCE.md#annotated-boost-recipe-showing-environment-fields).

### What is a repository provider and how do I make one?

A repository provider is a recipe that, instead of a package to build, describes another recipe repository to load at dependency-resolution time. Add `provides_repository: true` to a recipe's YAML header, with `source` pointing to a git repository whose top level contains `*.sh` recipe files, and `tag` selecting the branch, tag or commit (default: `version`). You can also set `repository_position` (append by default).

See: [Defining a repository provider](REFERENCE.md#defining-a-repository-provider).

### I set repository_position: prepend in my provider but I get a warning and it's appended anyway

By default every provider checkout is appended to `BITS_PATH`, whatever `repository_position` says; provider recipes cannot self-elevate. A `prepend` without a policy is downgraded to `append` with a warning naming the required entry. Whoever runs the build must grant it, e.g. `bits build --provider-policy myorg-extras:prepend MyPackage` (entries `name:prepend|append`, comma-separated).

See: [How position is resolved](REFERENCE.md#how-position-is-resolved), [Configuration](REFERENCE.md#configuration).

### How can I use my patched zlib.sh instead of the one from the upstream recipes?

Put the modified `zlib.sh` in a provider (e.g. `myorg-patches`) and grant it prepend access: `bits build --provider-policy myorg-patches:prepend ROOT`. bits then warns that the provider will shadow `zlib`; that warning is informational and expected. Note that the primary config directory (`-c`/`--config-dir`) is always first in the search order and cannot be shadowed by any provider.

See: [Example: patching a default recipe](REFERENCE.md#example-patching-a-default-recipe), [How position is resolved](REFERENCE.md#how-position-is-resolved).

### What's the best way to give my whole team a curated set of approved recipes?

Use a provider recipe with both `provides_repository: true` and `always_load: true` in the primary config directory (`-c`/`--config-dir`). It is cloned and added to `BITS_PATH` at startup, before the dependency graph is traversed, so its recipes are visible without any package declaring a dependency on it. The text calls this the recommended way to distribute a curated set of approved recipes across a team.

See: [Always-on providers (`always_load: true`)](REFERENCE.md#always-on-providers-always_load-true).

### How do I use my own provider registry instead of bitsorg/bits-providers?

Set `BITS_PROVIDERS`, e.g. `export BITS_PROVIDERS=https://github.com/myorg/my-recipes.git@main`, or prefix a single command with it. The `@tag` suffix is optional (default `main`); everything after the first `@` is taken as the tag, so use an `https://` URL, as SSH URLs like `git@github.com:org/repo` are not supported. An empty value falls back to the default; native `bits` cannot switch the registry off.

See: [The `bits-providers` standard repository](REFERENCE.md#the-bits-providers-standard-repository).

### How do I check out the alice recipes to work on them and build ROOT?

Name the recipe repository on `bits init`: `bits init alice.bits` resolves it in the registry and clones it into `./alice.bits`. Then `bits init -c alice.bits ROOT` checks out a package for development beside it (even one whose recipe lives in a required provider repo such as `alidist.bits`), and `bits build -c alice.bits ROOT` builds it.

See: [Bootstrapping a recipe repository from the registry](REFERENCE.md#bootstrapping-a-recipe-repository-from-the-registry).

### Can my defaults file pull in our organisation's recipe repo automatically?

Yes. Declare the provider in a top-level `requires` or `build_requires` of the defaults file; its recipe (e.g. `myorg-recipes.sh`, with `provides_repository: true`) must be findable on the search path. These fields only seed the provider scan and do not become build dependencies. Unlike `always_load: true`, which fires on every build, this loads the provider only when that defaults profile is active.

See: [Triggering providers from a defaults file](REFERENCE.md#triggering-providers-from-a-defaults-file).

### We pushed a new tag to our provider repo. Do I need to clear a cache for bits to see it?

No. Once a provider has a cached checkout, bits refreshes its git mirror on every run (even with `--no-fetch-repos`), so tag advances are detected and picked up on the next build without a manual cache purge. Checkouts are cached under `$BITS_WORK_DIR/REPOS/<package-lower>/<short_commit_hash>/`; a new commit gets a fresh checkout alongside the old one.

See: [Cache layout and staleness](REFERENCE.md#cache-layout-and-staleness).

## Recipes, defaults and environment

### My version 3.10 shows up as 3.1. Why?

YAML reads an unquoted `3.10` as the number 3.1. `version` must be a YAML string, and values in `variables:` that look like numbers should be quoted too, e.g. `version: "3.10"`.

See: [Miscellaneous](REFERENCE.md#miscellaneous), [Identity](REFERENCE.md#identity).

### Do I need to untar the archives listed under sources: myself?

No. bits downloads each file to `$SOURCEDIR` and unpacks archives (`.tar.gz`, `.tgz`, `.tar.bz2`, `.tbz2`, `.tar.xz`, `.txz`, `.tar.zst`, `.zip`) in place, stripping the top-level directory common to all their entries, so `$SOURCEDIR` holds the source tree. Other files are left as they are, and the names stay available as `$SOURCE0`, `$SOURCE1`, … with `$SOURCE_COUNT` giving the number of entries.

See: [Source](REFERENCE.md#source).

### Where do patch files go and how are they applied?

List the file names under `patches:`; they must exist in the `patches/` subdirectory of the recipe repository. bits copies them to `$SOURCEDIR` and by default applies them there in order with `patch -p1` before the recipe body runs, writing a `.bits_patched` sentinel so incremental rebuilds don't apply them twice.

See: [Source](REFERENCE.md#source), [Controlling patch application](REFERENCE.md#controlling-patch-application).

### I need to apply a patch after a code generation step. How do I stop bits applying patches automatically?

Add `auto_patch: false` to that recipe's header; bits still stages the patch files in `$SOURCEDIR` and exports `$PATCH0`, … and `$PATCH_COUNT`. Then call the `bits_apply_patches` helper where you need it in the body (it applies every staged patch in order with `patch -p1`, or e.g. `bits_apply_patches 0` for another strip level, and is idempotent across incremental rebuilds). The `--no-auto-patch` flag or `auto_patch: false` in a defaults profile turns it off for every package instead.

See: [Controlling patch application](REFERENCE.md#controlling-patch-application).

### How do I apply a patch only for one upstream version?

Add a matcher suffix to the entry, e.g. `"gaudi-GaudiToolbox.cmake.patch:version=v40r2"`. It is evaluated against the resolved version (after defaults `overrides:` and `requires:` pins), versions compare in natural (`sort -V`) order, and atoms combine with `&&` and `||`. Inactive patches are dropped before hashing, checkout and application, so they never affect the build hash.

See: [Conditional patches](REFERENCE.md#conditional-patches).

### My --flavour use-openloops gate never fires. Why?

Flavour and defaults `variables:` names must be plain identifiers (`[A-Za-z_][A-Za-z0-9_]*`); hyphens are not allowed. A hyphenated name is silently treated as an architecture regex, so its gate never fires, and it could not be exported as an env var either. Use `use_openloops` instead.

See: [Flavours](REFERENCE.md#flavours).

### How do I add a dependency only on macOS, and its Linux counterpart?

Use the predefined platform variables: `pkg:(?osx)` is a macOS-only dependency and `pkg:(?linux)` its counterpart. Note that `pkg:(?!osx)` is a negative-lookahead regex matched against the architecture string, not variable negation; there is no variable-negation atom.

See: [Defaults `variables:` and predefined platform variables](REFERENCE.md#defaults-variables-and-predefined-platform-variables).

### I defined a value under variables: but $NAME is empty in my build script. Why?

`variables:` is text templating only: it is used as `%(NAME)s` and never becomes a shell variable. `env:` is a shell variable only (`$NAME`). To use one name as both, pass it as a `--flavour` or define it in both `variables:` and `env:`.

See: [`variables` vs `env` vs `flavours` — quick comparison](REFERENCE.md#variables-vs-env-vs-flavours--quick-comparison).

### Pip install fails in my recipe with no network. How do I allow it?

Set `sandbox_network: off` in the recipe header; `off` allows network access, `on` blocks it. Without the field, `--sandbox-network` or `sandbox_network:` under the defaults `system:` block decides, else it is `on`. It is ignored with `--sandbox=off`.

See: [Build sandbox](REFERENCE.md#build-sandbox).

### I added checksums but a wrong one doesn't fail the build. Why?

The default mode is `off`: checksums are stored but never evaluated. Use `--check-checksums` (warn on mismatch) or `--enforce-checksums` (abort on mismatch, and also on a missing checksum), set `checksum_mode:` in the defaults profile, or `enforce_checksums: true` in the recipe. Precedence: `--print-checksums` > `--enforce-checksums` > `--check-checksums` > recipe `enforce_checksums: true` > defaults `checksum_mode:` > `off`.

See: [Checksum verification](REFERENCE.md#checksum-verification).

### How do I generate checksums for my recipes the first time?

Run `bits checksums --write` in the recipe repository to record every recipe's checksums without building, or `bits build --write-checksums <package>` to record those of the packages a build fetched. They go into `checksums/<pkgname>.checksum` sidecar files, which win over inline values; both commands add new entries and never overwrite one that disagrees. `--print-checksums` instead just prints the SHA-256 of every source and patch after the build.

See: [External checksum files](REFERENCE.md#external-checksum-files).

### How do I combine two defaults profiles, like dev and gcc13?

Join them with `::`, e.g. `bits build --defaults dev::gcc13 MyPackage`. This loads `defaults-release.sh`, `defaults-dev.sh` and `defaults-gcc13.sh` in that order and deep-merges their headers left to right: later scalars win, lists are concatenated, dicts are recursively merged.

See: [Combining multiple profiles with `::`](REFERENCE.md#combining-multiple-profiles-with-).

### Building with gcc12 and gcc13 profiles overwrites the same install tree. How do I keep them apart?

Qualify the install architecture. Add `append_arch: -gcc13` to `defaults-gcc13.sh` (used verbatim, include the separator) so only files that opt in add a suffix, e.g. `sw/slc7_x86-64-gcc13/`. Alternatively `qualify_arch: true` appends every non-`release` default name in the chain; if any file uses `append_arch`, `qualify_arch` is ignored.

See: [Qualifying the install architecture](REFERENCE.md#qualifying-the-install-architecture), [Selective qualification with `append_arch`](REFERENCE.md#selective-qualification-with-append_arch).

### Bits enter can't find my package built with a gcc13 profile. What should I do?

The module frontend auto-detects only the raw architecture, so pass the combined one, e.g. `bits -a slc7_x86-64-dev-gcc13 enter MyPackage/1.2.3-1`; the success banner prints the exact command. You can also `export BITS_ARCHITECTURE=slc7_x86-64-dev-gcc13` for the session. To clean such a tree, use `bits clean -a <combined arch>`.

See: [Entering a qualified-architecture build](REFERENCE.md#entering-a-qualified-architecture-build), [Cleaning up](REFERENCE.md#cleaning-up).

### How do I install CMSSW_13_0_0 without the -1 revision at the end of the path?

Set `force_revision: ""` in a defaults profile, per package under `overrides:` (e.g. `"cmssw_.*": force_revision: ""`) or as a top-level fallback for every package. Beware: two builds of the same version then share one install path, `latest` symlinks are silently overwritten (bits warns), and the `local` prefix protection is bypassed.

See: [Forcing or Dropping the Revision Suffix (`force_revision`)](REFERENCE.md#forcing-or-dropping-the-revision-suffix-force_revision), [Risks and caveats](REFERENCE.md#risks-and-caveats).

### How do I build a pure data package once for all platforms?

Add `architecture: share` to the recipe header. It is installed under `<work_dir>/share/…` instead of the architecture directory, and if all its dependencies are shared too it has the same hash on every platform, so one store artifact serves all of them. The old spelling `architecture: shared` is no longer recognised and builds as a normal per-architecture package.

See: [19. Architecture-Independent (Shared) Packages](REFERENCE.md#19-architecture-independent-shared-packages).

### Where should my recipe install files, and where is the source?

Install everything into `$INSTALLROOT`, the final installation prefix that bits creates before the recipe runs. The source is in `$SOURCEDIR`, and `$BUILDDIR` is a temporary build directory created automatically. Use `$JOBS` for parallelism, e.g. `make -j$JOBS`.

See: [Core build paths](REFERENCE.md#core-build-paths).

## Commands

### How do I see the options of just one bits command?

Run `bits help COMMAND`, which is the same as `bits COMMAND --help` (e.g. `bits help cvmfs stage`). `bits help` or `bits -h` on its own lists all commands by group.

See: [16. Command-Line Reference](REFERENCE.md#16-command-line-reference).

### Bits -a slc9_x86-64 build ROOT doesn't pick up my architecture, why?

A command's own options come after the command: write `bits build -a ARCH PKG`, not `bits -a ARCH build PKG`. Options such as `--community NAME` are the exception and go first, before the command.

See: [16. Command-Line Reference](REFERENCE.md#16-command-line-reference).

### Can I see what bits build is going to do before it starts compiling?

Yes: `bits build -n` (`--dry-run`) prints the build plan a real build prints before it starts: for each package whether it is installed, from a local tarball, from the remote store, from the reuse overlay, built, or not needed, plus its recipe source and a summary. Nothing is downloaded; the remote store is only listed, which is possible for `http(s)://`, `s3://` and `b3://` stores. `bits status PACKAGE` also shows what `bits build` would do for each package without building.

See: [16. Command-Line Reference](REFERENCE.md#16-command-line-reference), [bits status](REFERENCE.md#bits-status).

### How do I use my project's defaults on top of release?

Pass `--defaults myproject`: `release` is prepended automatically, so this is the same as `--defaults release::myproject` (profiles are combined with `::` and deep-merged left to right). Without `--defaults` the profile is `release`.

See: [bits build](REFERENCE.md#bits-build).

### Which option builds several packages at the same time?

`--parallel [N]` sets how many packages build at the same time; bare `--parallel` uses 4, and without it the build is serial (the default). `-j N` / `--jobs N` is the number of compilation jobs per package (default: CPU count); with N>1 each build's `$JOBS` is divided across the builders so they do not oversubscribe the machine.

See: [bits build](REFERENCE.md#bits-build).

### How do I upload the tarballs I build to my own bucket?

Add `--write-store URL`; several stores, comma-separated, all receive every built package, and an upload failure to any fails the package. Given alone it is also the read store, except where a default remote store applies; then use `--remote-store URL::rw` to read from and upload to that single store. `::rw` cannot be combined with `--write-store`.

See: [bits build](REFERENCE.md#bits-build).

### I want to build just the top of my stack on top of an LCG release already on CVMFS. Which options?

Use `--reuse-from PATH|cvmfs` (the literal `cvmfs` takes the location from the defaults `system:` layout) with `--reuse-policy relaxed`, or `--reuse-from cvmfs::relaxed`; relaxed accepts any deployed build of the same version, so only the top of the stack is built. Relaxed builds have loose provenance and cannot be published: combining them with `--write-store` is refused. To still build some packages yourself (e.g. one you need patched), list them with `--build-local PKG[,PKG…]`.

See: [bits build](REFERENCE.md#bits-build).

### Using a b3:// store with bits build --docker: does boto3 need to be in my build image?

No, it must be on the build host: bits runs the remote-store sync in its own host-side Python process even for `bits build --docker`; the container only runs the compile steps. Install it with e.g. `sudo apt install python3-boto3` (Ubuntu/Debian) or `sudo dnf install python3-boto3` from EPEL (AlmaLinux/RHEL/Fedora), or `pip3 install --break-system-packages boto3`. `s3://` stores instead need the `s3cmd` binary and an `~/.s3cfg`.

See: [S3 store: common CI/CD config with per-runner overrides](REFERENCE.md#s3-store-common-cicd-config-with-per-runner-overrides).

### How do I check my machine is set up to run bits?

Run `bits doctor` with no package (setup mode): it checks the Python bits runs with and its modules, running as root, git, a C++ compiler, the container engine behind `docker`, disk space, and each `--remote-store`/`--write-store`. Each check is PASS / WARN / FAIL / SKIP with a one-line fix, and any FAIL gives exit code 1; `--json` is supported.

See: [bits doctor](REFERENCE.md#bits-doctor).

### Before a long build, how can I see which packages are already pre-built in the store?

`bits doctor --check-store PACKAGE ...` computes the expected tarball hash of each package bits would build and probes the remote store (it needs `--remote-store` or the architecture's default); the report is informational and always exits 0. Only `http(s)://` and local-directory stores can be probed, others are SKIP. For branch builds the commit hash is approximated, so if every package shows FAIL, re-run with `bits status --check-store` for accurate hashes.

See: [bits doctor](REFERENCE.md#bits-doctor).

### How do I find out who built, published and approved a library on CVMFS?

Run `bits trace /cvmfs/.../lib/libCore.so`: from the package's `.meta.json` it shows package, version, content hash, the build, the recipes' commit and bits version, and the console backend names the publishing pipeline and, for each pipeline, who requested it and who approved it with their passkey. It authenticates with your GitLab token (`~/.bits/gitlab-token` or `$BITS_GITLAB_TOKEN`); without one it shows the package only. The path need not be mounted.

See: [bits trace](REFERENCE.md#bits-trace).

### How do I stop typing --remote-store on every build?

Run `bits init --remote-store URL` with no PACKAGE: it saves only the options you name as a `bits use` profile (`./.bitsuse`, or a record under `~/.bits/use/` when the directory is not writeable) and exits. `bits init --dry-run ...` previews what would be saved, and flags you give explicitly on a later command still win. `bits use build …` is the general way to record such settings.

See: [Config mode — record persistent settings with `bits use`](REFERENCE.md#config-mode--record-persistent-settings-with-bits-use).

### My ~/.bitsrc settings are being ignored

`bits.rc` has been retired: `bits.rc`, `.bitsrc` and `~/.bitsrc` are no longer read. Per-directory settings now live in a `bits use` profile, and global settings come from environment variables such as `$BITS_WORK_DIR`, `$BITS_REPO_DIR` (config dir), `$BITS_COMMUNITY` and `$BITS_PATH`.

See: [Config mode — record persistent settings with `bits use`](REFERENCE.md#config-mode--record-persistent-settings-with-bits-use).

### How do I get an editable checkout of a package's source to develop on?

Run `bits init PACKAGE[@VERSION]` (several comma-separated): it clones the upstream source repository into a writable local directory, under `-z` / `--devel-prefix` (default `.`). Subsequent `bits build` invocations of that package then use that directory as its source automatically.

See: [Clone mode — create a writable source checkout](REFERENCE.md#clone-mode--create-a-writable-source-checkout).

### What's the difference between bits clean and bits prune?

`bits clean` deletes build leftovers: `TMP/`, `INSTALLROOT/`, `BUILD/` trees and installed versions that no `latest` link points to (`--aggressive-cleanup` also deletes `SOURCES/` and the tarball store, never the git mirrors). `bits prune` evicts packages from a persistent workDir by last-use age (`--max-age DAYS`, default 7), disk pressure (`--min-free GIB`) or retention rules, and is meant for shared CI build caches. Both accept `-n` / `--dry-run`.

See: [Confusing command pairs](REFERENCE.md#confusing-command-pairs), [bits clean](REFERENCE.md#bits-clean), [bits prune](REFERENCE.md#bits-prune).

### How do I get a shell with ROOT loaded?

`bits enter MODULE1[,MODULE2,...]` spawns a new interactive sub-shell with those modules loaded; `exit` returns to the original environment. Your `.bashrc`/`.zshrc` is not sourced unless you add `--shellrc`, `-q` silences the module-load messages, and nesting `bits enter` inside another bits environment is blocked.

See: [bits enter](REFERENCE.md#bits-enter).

### Bits load ROOT just prints commands and my environment doesn't change

`bits load` (alias `printenv`) only prints the shell commands; they must be `eval`'d: `eval "$(bits load MODULE)"`. Alternatively put `eval "$(bits shell-helper)"` in your `~/.bashrc`, `~/.zshrc` or `~/.kshrc`; after that `bits load` and `bits unload` change the current shell directly.

See: [bits load / printenv](REFERENCE.md#bits-load--printenv), [bits shell-helper](REFERENCE.md#bits-shell-helper).

### How do I run a script with a published release's environment in a grid job?

Use `bits setenv MODULE -c COMMAND [ARGS...]`: it loads the modules into the current process and `exec`s the command, with no new shell, preserving its exit code. Through the CVMFS entry point select the community first, e.g. `/cvmfs/bits.cern.ch/bits/bin/bits --community key4hep setenv key4hep/2026-10-01 -c ./run.sh`.

See: [bits setenv](REFERENCE.md#bits-setenv), [16. Command-Line Reference](REFERENCE.md#16-command-line-reference).

### What's the difference between bits q, bits list and bits avail?

`bits q [REGEXP]` lists the available modules, optionally filtered by a case-insensitive extended regex, straight from the install tree (no module refresh, no `modulecmd`), so it stays fast. `bits list` shows the currently loaded modules. `bits avail` refreshes the modules directory and prints the raw `modulecmd avail` output.

See: [bits query / list / avail](REFERENCE.md#bits-query--list--avail).

### Should I use bits publish or bits cvmfs publish?

`bits publish` publishes one package (or a release view) from your local work dir to CVMFS through the cvmfs-prepub service (`bits publish PACKAGE --cvmfs-target PATH --prepub-url URL`). `bits cvmfs publish` publishes every package of a build manifest from its store tarballs; that is the CI publish pipeline. Writing a single package's tarball to the S3 reuse store is `bits store upload`.

See: [Confusing command pairs](REFERENCE.md#confusing-command-pairs), [bits publish](REFERENCE.md#bits-publish).

## Stores, Docker and CVMFS

### I uploaded my build to our S3 store, but other machines still rebuild everything instead of downloading it. Why?

Reuse is signed by default: a remote tarball is reused only once a signature-verified manifest lists it, so until the build is certified (`bits certify`) and signed (`bits sign`), other builds rebuild those packages. Uploading only needs valid S3 keys. `--no-require-signed-reuse` turns the check off, but it is insecure and bits warns.

See: [Publishing an existing local build to S3 (`bits publish`)](REFERENCE.md#publishing-an-existing-local-build-to-s3-bits-publish), [CLI options](REFERENCE.md#cli-options).

### We use a shared NFS directory as --remote-store /nfs/shared/bits-cache::rw but nothing is ever reused from it. What's wrong?

bits cannot find a signed manifest in an rsync or local-filesystem store, so nothing is reused from it by default. Pass `--trust-manifest` to name a manifest to trust, or `--no-require-signed-reuse` for a store you control.

See: [rsync / local filesystem](REFERENCE.md#rsync--local-filesystem), [Local filesystem cache for team NFS](REFERENCE.md#local-filesystem-cache-for-team-nfs).

### How can I see why a package was built from source instead of being taken from the store?

At the end of a build bits prints a reuse summary from the build manifest: how many packages were already installed, taken from the store and built, and the built ones by name with the reason. The same reason is stored per package in the manifest field `rebuilt_because` (for example `store tarball not certified`), when it is known.

See: [25. Build Manifest](REFERENCE.md#25-build-manifest), [What is recorded](REFERENCE.md#what-is-recorded).

### What decides the package hash? I changed something small and the whole thing rebuilt.

The hash is a 40-character SHA-1 of the recipe text (comments stripped), package name, version and family, the source commit, `sources:` and patches, `env`/path settings, hooks, and the hashes of its dependencies (on macOS also the relocation paths). Changing anything in this set gives a new hash and therefore a new cache entry, and because dependency hashes are included, a change to a dependency changes the hashes of the packages that depend on it.

See: [Content-addressable tarball layout](REFERENCE.md#content-addressable-tarball-layout).

### What's the difference between --remote-store and --write-store, and what does ::rw do?

`--remote-store URL` is where bits fetches pre-built tarballs before deciding whether to build; `--write-store URL` is where each newly built tarball is uploaded after a successful build (it may be the same URL). `--remote-store URL::rw` sets both to the same URL in one flag and cannot be combined with `--write-store`.

See: [CLI options](REFERENCE.md#cli-options).

### My bucket is on AWS (or MinIO). Should I use s3:// or b3://?

Use `b3://`. The `s3://` backend uses `s3cmd` with `~/.s3cfg` and bits pins its host to CERN S3 (`s3.cern.ch`). `b3://` uses `boto3`, is the preferred S3 backend and works with any S3-compatible endpoint (CERN S3 by default; set `--s3-endpoint` otherwise).

See: [S3 via `s3cmd` (`s3://`)](REFERENCE.md#s3-via-s3cmd-s3), [S3-compatible via `boto3` (`b3://`)](REFERENCE.md#s3-compatible-via-boto3-b3).

### Where do I put my S3 access keys for a b3:// store?

Set `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the environment, or, to keep them out of the environment, put them in a private file, by default `~/.bits/s3keys` with mode 600 (override the path with `$BITS_AWS_KEYS_FILE`). The file accepts plain `KEY=value`, `export`-prefixed, quoted, and AWS credentials INI forms, and may also set `S3_ENDPOINT_URL` and `AWS_DEFAULT_REGION`.

See: [S3-compatible via `boto3` (`b3://`)](REFERENCE.md#s3-compatible-via-boto3-b3).

### I already built my stack locally without --write-store. How do I push it to the S3 store now?

Use `bits publish`: with no package name it reads the latest build manifest and uploads each package's tarball to the S3 store. Choose the bucket with `--remote-store` (e.g. `bits publish --remote-store https://s3.cern.ch/lcgapp-bits-testing`) or a specific manifest with `--from-manifest`. Rebuilding does not help, because nothing re-uploads on a cached rebuild.

See: [Publishing an existing local build to S3 (`bits publish`)](REFERENCE.md#publishing-an-existing-local-build-to-s3-bits-publish).

### I built with --write-store but some packages were not uploaded. Which packages does bits skip?

Packages recalled from a store, packages with `local` revisions, and packages whose `redistributable:` forbids binary redistribution are not uploaded. Only packages built from source in this run are uploaded.

See: [Build lifecycle with a store](REFERENCE.md#build-lifecycle-with-a-store).

### My build aborted with a store integrity mismatch showing expected and actual SHA-256. What should I do?

A mismatch is always fatal: the tarball from the store differs from the digest recorded at upload time. Delete the local tarball (`rm -rf $WORK_DIR/TARS/<arch>/store/<h2>/<hash>/`), fetch it from an independent source, compute `sha256sum`, and compare with the ledger file under `$WORK_DIR/STORE_CHECKSUMS/`. If the independent copy matches the ledger but the store does not, treat the store as compromised (rotate credentials, audit access logs, rebuild from source); if the change is confirmed benign, delete the `.sha256` ledger file and the next build re-records it.

See: [Investigating a mismatch](REFERENCE.md#investigating-a-mismatch).

### The upstream URL for a tarball in my recipe's sources: has disappeared. Can bits still build it?

Yes, if it was archived before: when a remote store is configured, files fetched through `sources:` are archived in it automatically under `SOURCES/cache/`, and bits looks in the local cache, then the store, before going to the upstream URL. Uploading the archive needs a write store (an `http(s)://` store alone is read-only), and it is skipped when the recipe's `redistributable:` forbids redistributing sources.

See: [Source archive caching](REFERENCE.md#source-archive-caching), [How it works](REFERENCE.md#how-it-works), [Enabling source archive caching](REFERENCE.md#enabling-source-archive-caching).

### How do I build just my package on top of an LCG release that's already on CVMFS?

Use `--reuse-from` pointing at the release's published modules tree, or the literal `cvmfs` to resolve it from the defaults. Reused components are sourced in place from `/cvmfs` and logged as 'Reusing CVMFS overlay module … (not built)'; only the top of the stack is compiled. Add `--build-local PKG[,PKG…]` to force specific packages to build locally anyway.

See: [Reusing deployed components (`--reuse-from`)](REFERENCE.md#reusing-deployed-components---reuse-from).

### Bits build --docker on an EL9 host tries to pull an x86_64-slc9 image that doesn't exist. What's going on?

Without `--docker-image`, the image name is derived from the architecture (OS, CPU and any `cuda` part). The default name bits gives an EL9 host, `slc9_x86-64`, asks for `x86_64-slc9`, which bits-containers does not publish; it publishes `x86_64-el8/el9/el10`, `aarch64-el9` and `x86_64-ubuntu2204/2404/2604`. Use an architecture whose image exists (e.g. `x86_64-el9`) or pass `--docker-image` explicitly.

See: [22. Docker Support](REFERENCE.md#22-docker-support).

### Pip install fails in my recipe when building with the sandbox on. How do I allow network access?

The sandbox blocks outgoing network by default. Add `sandbox_network: off` to the recipe header to allow it for that recipe; a build-wide default can be set with `bits build --sandbox-network on|off` or `sandbox_network:` in the defaults, but the recipe's own field always wins. The field is ignored when the sandbox is off.

See: [Per-recipe network control](REFERENCE.md#per-recipe-network-control).

### How can I check that what's deployed on CVMFS matches what was built?

Run `bits verify --from-manifest <manifest.json> --cvmfs-root /cvmfs/<repo> --work-dir <dir>`. For each package it locates the content-addressed tarball (first under `--cvmfs-root`, then the work directory) and compares its SHA-256 with `tarball_sha256` in the manifest; it also checks provider commits and the architecture, reporting PASS, FAIL, MISS or SKIP. Add `--json` for machine-readable output.

See: [23. bits verify — Deployment Verification](REFERENCE.md#23-bits-verify--deployment-verification), [What is checked](REFERENCE.md#what-is-checked), [Search order](REFERENCE.md#search-order).

### Where does bits build write its manifest?

Every `bits build` writes a JSON manifest to `$WORK_DIR/MANIFESTS/` as `bits-manifest-<package>-<UTC timestamp>.json`, and keeps the symlink `bits-manifest-latest.json` pointing at the newest one; the file name is also printed in the success banner. It is written incrementally, so a failed build still leaves a partial manifest with `status: failed` and the `failed_package`.

See: [25. Build Manifest](REFERENCE.md#25-build-manifest), [Manifest location and naming](REFERENCE.md#manifest-location-and-naming), [Manifest schema reference](REFERENCE.md#manifest-schema-reference).

### Does bits build --from-manifest rebuild exactly what the manifest recorded?

No. A replay takes only the package list (`requested_packages`) from the manifest; the architecture, `--defaults` and the recipe repository come from the current invocation and checkout, versions and hashes are not pinned, and recalled tarballs are not checked against `tarball_sha256`. For an exact replay, check out the manifest's `config_commit` (and provider commits), pass the recorded `architecture` and `defaults`, and use `bits verify` to check tarballs.

See: [Replaying a build with `--from-manifest`](REFERENCE.md#replaying-a-build-with---from-manifest).

## Communities

### How do I build O2Physics for ALICE with bits?

In an empty directory (e.g. `mkdir alice && cd alice`) run `bits init alice.bits`, record the options once with `bits use build -c alice.bits --defaults alidist::o2`, then run `bits build O2Physics`; alidist is fetched automatically. Afterwards `bits enter O2Physics/latest` opens a sub-shell with O2Physics loaded (leave with `exit`). `O2` builds the same way.

See: [Getting started with native bits](https://github.com/bitsorg/alice.bits/blob/main/README.md#getting-started-with-native-bits).

### Which --architecture do I use for ALICE on EL9? Is it x86_64-el9 like the other experiments?

No. ALICE uses aliBuild-style names because the alidist recipes only recognise `slc*` and `ubuntu*` values of `$ARCHITECTURE`: `slc9_x86-64` (EL9 x86-64), `slc9_aarch64` (EL9 ARM64), `slc8_x86-64` (EL8 x86-64), `ubuntu2204_x86-64` and `ubuntu2404_x86-64`. On a matching host bits detects them itself. EL10 and Ubuntu 26.04 are not supported by alidist yet.

See: [Prerequisites](https://github.com/bitsorg/alice.bits/blob/main/README.md#prerequisites).

### Where do ALICE packages end up on CVMFS?

Under `/cvmfs/bits.cern.ch/alice/<install-dir>/Packages/<pkg>/<version>`, with modulefiles in `/cvmfs/bits.cern.ch/alice/<install-dir>/Modules/modulefiles/<pkg>` and architecture-independent packages under `/cvmfs/bits.cern.ch/alice/noarch/`. The install directory follows the OS-first ALICE convention (`el9-x86_64`, `el9-aarch64`, `ubuntu2404_x86_64`), not the architecture string. To check where a package would land, use `bits cvmfs-path`.

See: [Notes for ALICE users](https://github.com/bitsorg/alice.bits/blob/main/README.md#notes-for-alice-users).

### Should I use aliBuild or native bits to build ALICE software?

Native `bits` with `alice.bits` is recommended and is what the bits-console CI uses; it loads alidist (at branch `master`) through the provider registry together with the fixes and CVMFS layout of alice.bits. The `aliBuild` front-end with a local `alidist` checkout is the legacy path: no registry, legacy `init.sh` and the `alisw/<distro>-builder` images for `--docker`; alice.bits is not used there.

See: [Two ways to build ALICE software](https://github.com/bitsorg/alice.bits/blob/main/README.md#two-ways-to-build-alice-software).

### How do I build Athena for ATLAS with bits?

Run `bits init atlas.bits && cd atlas.bits`, record the settings with `bits use build --docker --architecture x86_64-el9 --defaults atlas::gcc15::opt --set release=LCG_110 --set athena_release=release/25.0.75`, then `bits build Athena`, which builds the LCG base, AthenaExternals and Athena. `bits enter Athena/latest` gives you a shell with it; the build architecture is `x86_64-el9-gcc15-opt`.

See: [Getting started](https://github.com/bitsorg/atlas.bits/blob/main/README.md#getting-started).

### Which platforms have pre-built ATLAS binaries? Can I build Athena on Ubuntu?

The ATLAS CI builds `x86_64-el9` (ATLAS targets `x86_64-el9-gcc15-opt`), `x86_64-el8`, `x86_64-el10` and `aarch64-el9`; Ubuntu is not a supported Athena platform. Pass the platform as `--architecture`, because by itself bits names an EL9 machine `slc9_x86-64`, and build with `--docker`, the only way to reuse what the ATLAS CI built.

See: [Prerequisites](https://github.com/bitsorg/atlas.bits/blob/main/README.md#prerequisites).

### How do I build the Key4hep stack with bits?

Run `bits init key4hep.bits && cd key4hep.bits`, then `bits use build --docker --architecture x86_64-el9 --defaults key4hep::gcc15::opt --set release=LCG_110`, and `bits build key4hep` for the complete stack (`bits build --dry-run key4hep` first shows what would be reused). `bits enter key4hep/latest` opens a shell with it. A single package builds the same way, e.g. `bits build DD4hep`.

See: [Getting started](https://github.com/bitsorg/key4hep.bits/blob/main/README.md#getting-started).

### How do I use the Key4hep software published on CVMFS without building anything?

Run `/cvmfs/bits.cern.ch/bits/bin/bits --community key4hep -a x86_64-el9-gcc15-opt enter <pkg>/<tag>`. In a grid job use `... setenv <pkg>/<tag> -c ./run.sh` instead; no work directory is needed.

See: [Notes for Key4hep users](https://github.com/bitsorg/key4hep.bits/blob/main/README.md#notes-for-key4hep-users).

### How do I build the LHCb externals with bits?

Run `bits init lhcb.bits && cd lhcb.bits`, then `bits use build --docker --architecture x86_64-el9 --defaults lhcb::gcc14::opt --set release=LCG_110` and `bits build lcg-view`, which builds all LHCb externals, then the manifest and toolchain. `bits enter lcg-view/latest` opens a sub-shell with the externals loaded. To build only the externals, without manifest and toolchain, build `lcg-externals` instead.

See: [Getting started](https://github.com/bitsorg/lhcb.bits/blob/main/README.md#getting-started).

### How do I build LHCb projects like Gaudi or LHCb against the bits-built externals?

After the usual LbEnv setup (`source /cvmfs/lhcb.cern.ch/lib/LbEnv`), run `source setupLHCb.sh` from lhcb.bits. It takes `sw/<arch>/lcg-view/latest`, or the newest `lcg-view` published on CVMFS when nothing is built locally, and sets `BINARY_TAG`, `LCG_VERSION`, `LCG_RELEASE_BASE` and `CMAKE_PREFIX_PATH`. Then build with LbDevTools or lb-stack-setup as usual, in native mode (`useDocker false`), not in its Docker container.

See: [Building LHCb software against the result](https://github.com/bitsorg/lhcb.bits/blob/main/README.md#building-lhcb-software-against-the-result).

### How do I build FairShip for SHiP?

Run `bits init ship.bits && cd ship.bits`, then `bits use build --docker --architecture x86_64-el9 --defaults ship::gcc15::opt --set release=LCG_110` (on macOS leave out `--docker` and `--architecture`), `bits build FairShip` and `bits enter FairShip/latest`. The build architecture is `x86_64-el9-gcc15-opt`.

See: [Getting started](https://github.com/bitsorg/ship.bits/blob/main/README.md#getting-started).

### How do I build ROOT from the LCG stack with bits?

Build from stacks.bits: `bits init stacks.bits && cd stacks.bits`, then `bits use build --docker --architecture x86_64-el9 --defaults gcc14::opt --set release=LCG_110` (on macOS without `--docker` and `--architecture`), `bits build ROOT` and `bits enter ROOT/latest`. `bits build externals` builds every external and `bits build generators` the MC generators.

See: [Getting started](https://github.com/bitsorg/stacks.bits/blob/main/README.md#getting-started).

### How do --defaults profiles combine for the LCG stack? What does gcc14::opt mean?

Profiles are joined with `::`; `release` is always the base and is prepended automatically, so you name only the overlays. Pick one compiler (`gcc13`, `gcc14`, `gcc15`, `clang`) and then one build type (`opt`, `dbg`), in that order, optionally adding `cuda`; each appends a suffix so the architecture matches the LCG platform name, e.g. `x86_64-el9-gcc14-opt`. A compiler profile is needed for a complete build.

See: [Composing profiles](https://github.com/bitsorg/stacks.bits/blob/main/README.md#composing-profiles).

### Which LCG releases can I build with bits, and how do I select one?

Pass `--set release=LCG_110`; this is preferred because the value enters the package hashes and matches what the other groups build. Otherwise bits takes a `release:` from a chosen profile, else the branch of the stacks.bits checkout (`LCG_110-patches` gives `LCG_110`), else `main`. The release must exist as an lcg.bits branch: currently `main`, `LCG_110`, `LCG_110b` (LCG_110 with security upgrades of some packages) and `devel`.

See: [Choosing a release](https://github.com/bitsorg/stacks.bits/blob/main/README.md#choosing-a-release).

### Where is the LCG stack published on CVMFS?

Under the prefix `/cvmfs/bits.cern.ch/lcg`: packages in `{prefix}/{arch}/Packages/{pkg}/{tag}`, modulefiles in `{prefix}/{arch}/Modules/modulefiles/{pkg}`, release symlink trees in `{prefix}/releases/{release}/{family}{pkg}/{version}/{arch}` and merged views in `{prefix}/views/{release}/{arch}`. `{release}` collapses for `main`, and MC generators get an `MCGenerators/` family level. Preview a path with `bits cvmfs-path --defaults gcc14::opt --set release=LCG_110 --admin --kind releases --package ROOT --version v6.40.02`.

See: [CVMFS layout](https://github.com/bitsorg/stacks.bits/blob/main/README.md#cvmfs-layout), [Mapping to lcgcmake](https://github.com/bitsorg/stacks.bits/blob/main/README.md#mapping-to-lcgcmake).

### Can I use bits without installing it, straight from CVMFS?

Yes: run `/cvmfs/bits.cern.ch/bits/bin/bits`, which brings its own Python, Tcl and Environment Modules runtime. Given a community (`--community key4hep`, `BITS_COMMUNITY=LHCB`, or a link named e.g. `bits-key4hep`) it builds with that community's recipes and reuses what the community has published on CVMFS. It runs on glibc Linux: el8 and later on x86_64, el9 and later on aarch64, and current Ubuntu, but not on musl systems such as Alpine.

See: [bits.bits](https://github.com/bitsorg/bits.bits/blob/main/README.md#bitsbits), [Building and publishing](https://github.com/bitsorg/bits.bits/blob/main/README.md#building-and-publishing).
