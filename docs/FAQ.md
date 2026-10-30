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

### Can I use rootless podman instead of docker for --docker builds? Do I still need gcc on the host?

Yes: the host needs docker, or podman run by your user (`podman-docker` provides the `docker` command); bits adapts its container options to rootless podman and `bits doctor` says which cgroup controllers to delegate. No compiler or `-devel` packages are needed on the host for a container build.

See: [Building in a container](USERGUIDE.md#building-in-a-container).

### What do I need to install on AlmaLinux 9 to build natively?

Install `dnf-plugins-core` and `epel-release`, enable CRB with `sudo dnf config-manager --set-enabled crb` (on 8 it is `powertools`), then install the long `dnf install` list in the guide (build tools such as bison, flex, make, gcc, gcc-c++, gcc-gfortran, environment-modules, git, plus `-devel` libraries such as krb5-devel, openssl-devel and the X11/GL ones). On RHEL itself, enable `codeready-builder-for-rhel-N-$(arch)-rpms` with `subscription-manager` and install EPEL from its release RPM. The lists are those the builder images are made from, so installing them makes the host match its platform's image.

See: [Building natively: what to install](USERGUIDE.md#building-natively-what-to-install).

### Ubuntu 24.04 native build - which apt packages?

Run `sudo apt-get update` and then the `sudo apt-get install -y --no-install-recommends ...` list from the guide (build tools such as bison, flex, make, gcc, g++, gfortran, environment-modules, git, python3-venv and `-dev` libraries such as libkrb5-dev, uuid-dev, libssl-dev and the X11/GL ones). Also install `linux-tools-generic` for perf, which some recipes check for. The same list applies to Ubuntu 22.04, 24.04 and 26.04.

See: [Building natively: what to install](USERGUIDE.md#building-natively-what-to-install).

### Does bits work on Debian?

bits runs on Debian on x86-64, but Debian is not a builder platform. The Ubuntu package list mostly applies (perf is `linux-perf` there), and nothing pre-built is reused, so everything is built.

See: [Building natively: what to install](USERGUIDE.md#building-natively-what-to-install), [Platforms](USERGUIDE.md#platforms).

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

### I passed --architecture x86_64-el9 but stuff ends up in sw/x86_64-el9-gcc15-opt, is that right?

Yes. A build adds its compiler and build type from the defaults (`append_arch`) to the platform name, so `--architecture x86_64-el9 --defaults gcc15::opt` installs under `sw/x86_64-el9-gcc15-opt/`. That full architecture names the directory everything is installed in.

See: [Platforms](USERGUIDE.md#platforms), [How bits works](USERGUIDE.md#how-bits-works).

### Can I build aarch64 packages on my x86 laptop?

Yes, through QEMU: `bits build --docker --architecture aarch64-el9 ...` on an x86-64 host needs the binfmt handlers, set up once with `docker run --rm --privileged multiarch/qemu-user-static --reset -p yes`. It is slow.

See: [Building in a container](USERGUIDE.md#building-in-a-container), [Docker & Remote Builds](../README.md#docker--remote-builds).

### I built with --architecture x86_64-el9 on my Ubuntu machine. Can I run it here?

What is built runs where its platform runs: an `x86_64-el9` build made on an Ubuntu host is for EL9 machines, and you `bits enter` it on an EL9 host. To run the result on this machine, build for the platform matching its OS and CPU.

See: [Building in a container](USERGUIDE.md#building-in-a-container).

### ALICE build with --docker fails pulling gitlab-registry.cern.ch/bits/containers/x86_64-slc9

That image does not exist: ALICE builds under its own names (`slc9_x86-64`, ...) in its own images (`registry.cern.ch/alisw/<distro>-builder`). Build with the `aliBuild` wrapper, which picks those images, or give `--docker-image registry.cern.ch/alisw/slc9-builder`.

See: [Platforms](USERGUIDE.md#platforms).

### How do I see which packages will be downloaded and which built, before building?

Run a dry run with the same options as the build, in the recipe repository, e.g. `bits build --dry-run --docker --architecture x86_64-el9 --defaults gcc15::opt ROOT`. It computes every hash and prints per package installed, local tarball, from store, from overlay, or build, followed by a summary; nothing is downloaded or installed. With `--docker` it needs docker and pulls the builder image the first time, to read its fingerprint.

See: [Previewing a build: `--dry-run`](USERGUIDE.md#previewing-a-build---dry-run), [Finding a platform with pre-built binaries](USERGUIDE.md#finding-a-platform-with-pre-built-binaries).

### Dry run shows "build (unsigned)" for packages that are in the store - why won't it reuse them?

Remote reuse is fail-closed: a tarball from a remote store is only reused when a verified signed manifest vouches for it, and `build (unsigned)` means the package is in the store but no signed manifest does. `--no-require-signed-reuse` turns this off, e.g. to bootstrap a new store. Local and CVMFS artifacts are unaffected.

See: [Sharing binaries through a store](USERGUIDE.md#sharing-binaries-through-a-store), [Previewing a build: `--dry-run`](USERGUIDE.md#previewing-a-build---dry-run).

### I ran bits build ROOT in my home directory and it just stops. What am I missing?

bits always builds from a community recipe repository and uses the current directory as its recipe directory; in a directory without recipes a build stops. Clone your community's repository and run bits inside it, e.g. `git clone https://github.com/bitsorg/stacks.bits && cd stacks.bits`, or `bits init stacks.bits && cd stacks.bits` to resolve it in the bits-providers registry. Community repositories are `stacks.bits`, `alice.bits`, `atlas.bits`, `cms.bits`, `key4hep.bits`, `lhcb.bits` and `ship.bits`.

See: [1. Check out your community's recipe repository](USERGUIDE.md#1-check-out-your-communitys-recipe-repository).

### What are the steps for a first build of ROOT?

Inside the community repository, run `bits doctor`, then `bits build --dry-run --docker --architecture x86_64-el9 --defaults gcc15::opt ROOT` and `bits build --docker --architecture x86_64-el9 --defaults gcc15::opt ROOT`, which in a container on the CI's platform reuses what the CI built. Natively, with the system packages installed, use `bits doctor --defaults gcc15 ROOT` and `bits build --defaults gcc15 ROOT`. Then `bits enter ROOT/latest`, run `root -b`, and `exit`.

See: [2. Check, then build](USERGUIDE.md#2-check-then-build).

### Where do the ~1100 LCG recipes come from if stacks.bits only has a few?

The community repository holds its settings and a few recipes and names the shared recipe repositories it builds on (`lcg.bits`, with about 1,100 LCG recipes; `alidist` for ALICE). bits finds those in its registry, bits-providers, and clones them itself on demand at the branch the community asks for, adding them to the search path.

See: [How bits works](USERGUIDE.md#how-bits-works), [Repositories: hierarchy, discovery & reuse](../README.md#repositories-hierarchy-discovery--reuse).

### How does bits decide whether a package needs rebuilding?

Every package gets a hash computed from its recipe, its sources, the hashes of the packages it needs and the build settings. A package whose hash was built before is taken, not rebuilt: from your `sw/`, from a binary store, or, when asked with `--reuse-from`, from what is already published on CVMFS. Change one recipe and only that package and what depends on it are rebuilt.

See: [How bits works](USERGUIDE.md#how-bits-works).

### What does --defaults gcc15::opt mean?

Build settings such as compiler, build type and publish location come from `defaults-<name>.sh` files, chained with `::`. `--defaults gcc15::opt` is GCC 15, optimised; `release` is always the base and comes first (`--defaults gcc15` means `release::gcc15`), and the default is `release`. A few values are set with `--set`, e.g. `--set release=LCG_110`.

See: [How bits works](USERGUIDE.md#how-bits-works), [Common options](USERGUIDE.md#common-options).

### I'm tired of typing --docker --architecture ... on every command. Is there a config file?

Record the options once per directory with `bits use`, e.g. `bits use build --docker --architecture x86_64-el9 --defaults gcc15::opt`; options without a section name go to `[common]`, applied to every architecture-aware command. The profile is saved in `./.bitsuse` and inserted before your own arguments, so an explicit flag still wins. Each `bits use SECTION ...` replaces that whole section, `bits use` alone shows the active profile and `bits use --clear [SECTION]` removes it.

See: [4. Configuration](USERGUIDE.md#4-configuration), [Finding a platform with pre-built binaries](USERGUIDE.md#finding-a-platform-with-pre-built-binaries).

### My .bitsuse file seems to be ignored

A local `.bitsuse` is only honoured when it is owned by you; otherwise the record under `~/.bits/use/` is used. That record is also where the profile goes when the current directory is not writeable or not owned by you. Run `bits use` to show the active profile and where it lives.

See: [4. Configuration](USERGUIDE.md#4-configuration).

### My old .bitsrc settings stopped working after upgrading

`bits.rc` is retired: `bits.rc`, `.bitsrc` and `~/.bitsrc` are no longer read. Move per-directory settings into a `bits use` profile and global ones into environment variables such as `$BITS_WORK_DIR`.

See: [4. Configuration](USERGUIDE.md#4-configuration).

### Which wins, the command line, bits use, environment variables or site config?

A setting is taken from the first that gives it: the command line, the `bits use` profile, the environment, the community's defaults (`system:` block of `defaults-*.sh`), the site configuration (`NN-<name>.yaml`), then bits' own `00-bits.yaml`. The exception is `$BITS_PATH`, which when set overrides `--search-path`.

See: [Where defaults come from](USERGUIDE.md#where-defaults-come-from).

### We're not at CERN. How do I point bits at our own S3 store and console?

CERN's values live in the site file `10-CERN.yaml` shipped with bits. Put an empty `10-CERN.yaml` in `/etc/bits/site.d/` to switch them off and add a file of your own, e.g. `/etc/bits/site.d/10-mylab.yaml` with `store:` (`read`, `operations`, `s3_endpoint`) and `console:` (`backend_url`) keys. A host or a person can override single keys in a later file (`50-...` or `90-...`), and `bits doctor` lists the files in effect.

See: [Where defaults come from](USERGUIDE.md#where-defaults-come-from).

### How do I upload my builds to our binary store?

Use `--remote-store URL::rw` to reuse from and upload to the same store, e.g. `bits build --remote-store b3://mybucket::rw ROOT`; without `::rw` the store is only read. `--write-store URL` pushes newly built tarballs; given alone it is also the read store, except where a default remote store applies, where you use `--remote-store URL::rw`. To upload a single package you already built, use `bits store upload PKG`.

See: [Sharing binaries through a store](USERGUIDE.md#sharing-binaries-through-a-store), [Common options](USERGUIDE.md#common-options).

### How to build several packages in parallel?

Use `--parallel N` (bare `--parallel` uses 4; `--builders` is an alias). Each package then gets a share of `-j` so the concurrent jobs stay within one machine's cores, and the final top-level package gets the full `-j` again, e.g. `bits build --parallel 8 -j 32 ROOT`. Without it packages build one after another; the `--makeflow` / `--pipeline` modes have been removed.

See: [Building several packages at once](USERGUIDE.md#building-several-packages-at-once), [Common options](USERGUIDE.md#common-options).

### I built ROOT, how do I actually use it?

Run `bits enter ROOT/latest`: a sub-shell opens with ROOT and all its dependencies set up, and the prompt is prefixed with the loaded modules, e.g. `[ROOT/latest]`. Type `exit` to return to your normal shell. `bits q [REGEXP]` lists the available modules; nesting `bits enter` inside another bits environment is blocked.

See: [Enter a sub-shell with modules loaded](USERGUIDE.md#enter-a-sub-shell-with-modules-loaded), [Load / unload in the current shell](USERGUIDE.md#load--unload-in-the-current-shell).

### Bits load ROOT/latest just prints stuff and my environment doesn't change

`bits load` prints commands that must be `eval`'d: `eval "$(bits load ROOT/latest)"`. Or add the shell helper to your `.bashrc` or `.zshrc` once (`BITS_WORK_DIR=/path/to/sw` and `eval "$(bits shell-helper)"`), after which `bits load` and `bits unload` change the current shell directly.

See: [Load / unload in the current shell](USERGUIDE.md#load--unload-in-the-current-shell), [Basic Commands](../README.md#basic-commands).

### Can I run one command with the ROOT environment without opening a shell, e.g. in a script?

Use `bits setenv ROOT/latest -c root -b`. Everything after `-c` is executed as-is and the exit code is preserved; `bits setenv` loads the modules into the current process environment and then `exec`s the command, so no new shell is spawned.

See: [Run a single command in a module environment](USERGUIDE.md#run-a-single-command-in-a-module-environment).

### What's the difference between bits clean and bits prune? My disk is full.

`bits clean` removes temporary build artifacts (`TMP/`, `INSTALLROOT/`, unreferenced `BUILD/` directories and installed versions no `latest*` symlink points to); `--aggressive-cleanup` also removes `SOURCES/` and the store tarballs, keeping `MIRROR/`. `bits prune` evicts packages from a persistent workDir, by age (`--max-age DAYS`, default 7) or until free space is reached (`--min-free GIB`); it is meant for persistent CI build caches. Both take `-n`/`--dry-run` to show what would be removed.

See: [bits clean — remove temporary build artifacts](USERGUIDE.md#bits-clean--remove-temporary-build-artifacts), [bits prune — evict packages from a persistent workDir](USERGUIDE.md#bits-prune--evict-packages-from-a-persistent-workdir).

### Build stops before starting and complains about a missing system requirement

When a recipe's system requirement is missing, bits stops before building and lists it. Run `bits doctor <package>` to see that package's system requirements with each recipe's install hint, and install the native packages listed in the guide for your platform. Building with `--docker` avoids the need for host `-devel` packages.

See: [Building natively: what to install](USERGUIDE.md#building-natively-what-to-install), [Building in a container](USERGUIDE.md#building-in-a-container).

### What does bits doctor check?

With no package, `bits doctor` checks that this machine is set up to run bits: Python modules, git and compiler, docker or rootless podman, disk and stores; it also lists the site configuration files in effect. `bits doctor <package>` checks that package's system requirements, and `bits doctor --runner` validates a full CI build-runner environment (Docker, podman, QEMU binfmt, CVMFS mounts, disk, store, prepub).

See: [Validating Builds and Deployments](../README.md#validating-builds-and-deployments), [Building natively: what to install](USERGUIDE.md#building-natively-what-to-install), [Where defaults come from](USERGUIDE.md#where-defaults-come-from).

## The console, scheduled builds and CI

### How do I build the full alice software stack on my laptop?

Clone the community recipe repository and build from inside it: `git clone https://github.com/bitsorg/alice.bits.git`, `cd alice.bits`, then `bits build ROOT`, which resolves the dependency graph and builds everything. Afterwards `bits enter ROOT/latest` opens a sub-shell with the environment loaded.

See: [Phase 1 — Local setup from shared recipes](WORKFLOWS.md#phase-1--local-setup-from-shared-recipes).

### Can I see what bits is going to rebuild before actually starting a build?

Yes, run `bits status <pkg>`, e.g. `bits status mylib`. It runs the full resolver without building anything and classifies each package as `already_installed`, `build_from_source`, or `local_checkout` when a matching local source directory was found.

See: [Preview what will rebuild](WORKFLOWS.md#preview-what-will-rebuild).

### I cloned mylib next to my alice.bits checkout. What flag tells bits build to use my local copy?

None is needed. `bits build mylib` scans for directories adjacent to (or below) the recipe checkout that share a name with a package and uses such a directory as that package's source instead of the upstream remote; there is no flag to pass, no environment variable to set and no copy of the recipe to edit. All other packages still resolve from the recipe repository, so `mylib` is compiled against the same dependency versions CI uses.

See: [Build with the local checkout](WORKFLOWS.md#build-with-the-local-checkout).

### How can I be sure my change will build in CI before I push it?

Run the build inside the builder image of the platform CI builds, e.g. `bits build --docker --architecture x86_64-el9 mylib`. This spins up the official builder container, bind-mounts the current directory (including your local `mylib/` checkout) and runs the build inside it; the docs say that if this succeeds, CI will succeed for the same reason.

See: [Validate in the CI image](WORKFLOWS.md#validate-in-the-ci-image).

### I pushed a fix to mylib's repo. What do I do in the recipe repo so everyone gets it?

In the recipe repository edit `mylib.sh` and update its `tag:` or `commit:` field to the new revision, run `bits build --write-checksums mylib` to recompute the checksums, and `bits doctor mylib` to check the recipe is consistent. Then commit, push and open a merge request on the recipe repository for peer review; reviewers can check out your branch and run `bits build mylib` to reproduce the build.

See: [Phase 3 — Commit and share](WORKFLOWS.md#phase-3--commit-and-share).

### How do I log in to bits-console?

Go to bits-console.web.cern.ch, select your community, and sign in with your GitLab personal access token. The bits-console documentation lists the required token scopes and role setup.

See: [Step 1 — Connect to bits-console](WORKFLOWS.md#step-1--connect-to-bits-console).

### I only see 'Build → Personal area' in the build dialog, where is Build → Production?

Build → Production is shown only to `bits-admin` and `group-admin` users; everyone with Developer access gets Build → Personal area, which publishes to a personal CVMFS namespace such as `/cvmfs/sft.cern.ch/lcg/user/jsmith/ROOT/6.30/x86_64-el9`. The choice is not a command-line flag but your role in the project, enforced server-side: even a crafted API call is rejected unless your `GITLAB_USER_LOGIN` is listed in the project's `GROUP_ADMINS_<NAME>` CI variable.

See: [Step 2 — Browse and trigger a build](WORKFLOWS.md#step-2--browse-and-trigger-a-build), [Phase 4 — CI build and CVMFS publication via bits-console](WORKFLOWS.md#phase-4--ci-build-and-cvmfs-publication-via-bits-console).

### Where can I follow my console build and know when it's on CVMFS?

The Builds tab in bits-console shows a live pipeline list with log streaming. The CVMFS Status tab updates once ingestion completes and the stratum-0 transaction is published; stratum-1 replicas then propagate the change automatically.

See: [Step 3 — Monitor progress](WORKFLOWS.md#step-3--monitor-progress).

### How do I set up a nightly build in the console?

On the Schedules tab click + New slot, fill it in and Save, or set up the build in the Build dialog and click 💾 Save as pipeline. A slot saved from the Build dialog is paused, runs every day at 02:00 and has no variables or followed branches, so Edit it first and then Resume it. If the slot certifies, also click Approve and then Use your passkey.

See: [Step 4 — Schedule nightly builds](WORKFLOWS.md#step-4--schedule-nightly-builds).

### Can any user create or approve a nightly slot?

No. Everyone signed in can see the slots on the Schedules tab, but only the community's admins make, change and approve them; for a certifying slot, that means the admins of every group it certifies for.

See: [Step 4 — Schedule nightly builds](WORKFLOWS.md#step-4--schedule-nightly-builds).

### Where do nightly builds with dev3 end up? and what if one starts after midnight?

The slot's Defaults chain decides how it is built and where its builds go: with stacks.bits' `dev3`, builds go to `…/nightlies/dev3/{day}/…`. The orchestrator gives each build its night's weekday as `{day}`, so a build that starts after midnight still lands in its night's directory. A blank defaults chain means `release`.

See: [Step 4 — Schedule nightly builds](WORKFLOWS.md#step-4--schedule-nightly-builds).

### How long does approving a certifying nightly slot last? Do I have to re-approve after changing it?

An approval covers 30 nights of every cell, but at most 100 builds (so fewer nights for a slot of more than 3 cells), or 30 days, whichever ends first; ▶ Run now uses it up too. A night it cannot cover whole, or a slot without an approval, still builds but certifies nothing. Changing what the slot builds or where needs a new approval; changing its time, days or followed branches does not.

See: [Step 4 — Schedule nightly builds](WORKFLOWS.md#step-4--schedule-nightly-builds).

### My nightly cell says 'not run' - why would a nightly be skipped?

At its time the orchestrator queues a build per cell, and they start as build hosts become free. A cell whose last build is still running skips the night, and a build that finds no free host for 12 hours is not run; a night missed by less than 6 hours still runs. The nightly matrix shows 'not run' together with the reason.

See: [Step 4 — Schedule nightly builds](WORKFLOWS.md#step-4--schedule-nightly-builds).

### Where do I see last night's nightly results?

In the nightly matrix: the console's Nightlies page shows 14 nights (a build opens its log) and the phone app's Nightlies tab shows 7. There is a row per slot and a cell per platform, variant and night with its state: queued, running, passed, failed and where, or not run and why. In the app, a slot's page has Run again for the failed cells of its last night.

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

### We run our own console instance. What has to be configured for nightly slots and commit CI?

Slots need the orchestrator's build queue, `BITS_ORCH_QUEUE=1`. The instance's time zone is the site setting `console.time_zone`, `BITS_ORCH_WATCH_S` (120 s, at least 30) sets how often branch heads are read, and the worker must reach the repositories' hosts over HTTPS. Details are in the Nightly schedules and Commit CI sections of `console-backend/README.md`.

See: [Step 5 — Build on each push (CI)](WORKFLOWS.md#step-5--build-on-each-push-ci).

### How can I check that what's on CVMFS is exactly what was built?

Run `bits verify --from-manifest <manifest.json> --cvmfs-root /cvmfs/alice.cern.ch`. The manifest is written automatically during `bits build` to `$WORK_DIR/MANIFESTS/`, and bits verify recomputes each package tarball's SHA-256 and compares it with the manifest. A mismatch is reported as FAIL (exit code 1) and a missing tarball as MISS (exit code 2).

See: [Phase 5 — Verify the deployment](WORKFLOWS.md#phase-5--verify-the-deployment), [Verify a live deployment against a build manifest](COOKBOOK.md#verify-a-live-deployment-against-a-build-manifest).

### How do I run one command in the bits environment from a script, without an interactive shell?

Use `bits setenv ROOT/latest -c root -b`: it loads the modules and execs the command without spawning an interactive shell, and the command's exit code passes through unchanged. For an interactive sub-shell use `bits enter ROOT/latest` and `exit` to return.

See: [Use the built environment](COOKBOOK.md#use-the-built-environment).

### How do I load and unload bits modules in my current shell instead of a sub-shell?

Add `export BITS_WORK_DIR=/path/to/sw` and `eval "$(bits shell-helper)"` once to `~/.bashrc`, `~/.zshrc` or `~/.kshrc`. Then use `bits load ROOT/latest,Python/3.11-1`, `bits unload ROOT` (the version can be omitted) and `bits list` to show what is loaded. Without the shell helper, use `eval "$(bits load ROOT/latest)"`.

See: [Use the built environment](COOKBOOK.md#use-the-built-environment).

### Can I build an aarch64 version on my x86 machine?

Yes, with Docker: `bits build --docker --architecture aarch64-el9 MyAnalysis`. This needs QEMU binfmt handlers on the host; when the target architecture differs from the host, bits automatically injects the matching Docker `--platform` flag so QEMU handles the emulation. The `--architecture` string selects both the OS image and the target CPU.

See: [Build for a different OS or architecture (Docker)](COOKBOOK.md#build-for-a-different-os-or-architecture-docker).

### Bits builds my packages one after another, how do I build several at once?

Use the built-in scheduler, e.g. `bits build --parallel 4 --jobs 8 my_large_stack` builds up to 4 packages at a time sharing 8 compile jobs, and the final top-level package gets all 8 (memory permitting). `--parallel` on its own uses 4, and without it the build is serial (`--builders` is a kept alias). To avoid running out of memory, declare per-package CPU and memory budgets with `--resources <file>` or reuse a previous run's statistics with `--auto-resources`.

See: [Speed up large builds](COOKBOOK.md#speed-up-large-builds), [Build memory-hungry packages without exhausting RAM](COOKBOOK.md#build-memory-hungry-packages-without-exhausting-ram).

### BUILD FAILED for my package - where's the log and how do I debug it?

The BUILD FAILED message includes an Error excerpt (the matched error lines plus the tail of the log) and prints the path of the build directory, which is kept after a failure. There you find the full `log` (e.g. `sw/BUILD/my_package-latest/log`); the build itself runs in its `my_package/` subdirectory, where you can re-run the failing command by hand. Re-run with `bits build --debug my_package` for more verbose bits output.

See: [Debug a failed build](COOKBOOK.md#debug-a-failed-build), [Error handling in recipes — how failures are detected](COOKBOOK.md#error-handling-in-recipes--how-failures-are-detected).

### How do I build a different ROOT version without editing the recipe?

Add an `overrides:` block to a defaults profile, e.g. in `defaults-myproject.sh` give `root:` a `version: "6.32.02"` and `tag: "v6-32-02"`; it is merged into the spec after the recipe is parsed and takes precedence over the recipe's values. Then build with `bits build --defaults release::myproject MyStack`. Keys are case-insensitive patterns that must match the whole package name, so `root` and `ROOT` both work.

See: [Override a package version without editing the recipe](COOKBOOK.md#override-a-package-version-without-editing-the-recipe).

### How do I use my own private recipes together with the public recipe repo?

Set `BITS_PATH` to add a repository to the recipe search path, e.g. `BITS_PATH=myorg bits build MyPackage` (a plain name means `<recipe dir>/myorg.bits`, an absolute path is used as is), or record it with `bits use build --search-path myorg`. These directories are searched after the main recipe directory, so a recipe the main directory already has is not replaced this way.

See: [Use a private recipe repository alongside the defaults](COOKBOOK.md#use-a-private-recipe-repository-alongside-the-defaults).

### How do I make bits refuse sources whose checksum doesn't match?

First record checksums with `bits build --write-checksums MyPackage`, which writes `checksums/mypackage.checksum` in the recipe directory. Then build with `bits build --enforce-checksums MyPackage`, or set `checksum_mode: enforce` in a defaults profile to make it the site default. Any mismatch or missing checksum then aborts the build.

See: [Enforce reproducible source downloads with checksums](COOKBOOK.md#enforce-reproducible-source-downloads-with-checksums).

### I'm tired of passing --remote-store on every build. Can I save it?

Yes, record it once inside your community repository, e.g. `bits use build --remote-store https://store.example.com/store --write-store b3://mybucket/store`; every later `bits build` from that directory picks it up. The settings go into the profile's `[build]` section (`./.bitsuse`, or a record under `~/.bits/use/` when the directory is not writeable), and `bits use` with no arguments shows what is saved.

See: [Set up a project with a persistent binary store](COOKBOOK.md#set-up-a-project-with-a-persistent-binary-store).

### Is there still a bits cleanup command? I need to free disk space in sw/

`bits cleanup` was renamed `bits prune`; the old name still works as a deprecated alias that warns and forwards. `bits prune --max-age 14` evicts packages not used in the last 14 days, `bits prune --min-free 50 --disk-pressure-only` frees space until 50 GiB is available, least-recently-used first and with no age limit, and `-n` does a dry run. Separately, `bits clean` removes stale BUILD/ directories and the TMP/ staging area.

See: [Evict old packages to free disk space](COOKBOOK.md#evict-old-packages-to-free-disk-space), [Clean up the workDir](WORKFLOWS.md#clean-up-the-workdir).

### A command in my Configure() hook failed but the build carried on as if nothing happened - why?

Inside bits-recipe-tools hooks `set -e` does not apply: the include files run the hooks as one chain (`Prepare && Configure && Make && …`) and bash switches `set -e` off in functions called from such a chain. Only the last command of a hook decides whether it fails, so add `|| return 1` to every earlier command that can fail.

See: [Error handling in recipes — how failures are detected](COOKBOOK.md#error-handling-in-recipes--how-failures-are-detected).

### Every time I change libfoo the whole stack above it rebuilds. Can I avoid that while iterating?

In each consumer that can tolerate it, list the dependency under `untracked_requires:` instead of `requires:`, and give the dependency a stable install label with `force_revision` (e.g. `"dev"`). Then `bits build MyApp` rebuilds only libfoo and reuses MyApp, which picks up the new libfoo at run time. The consumer is neither recompiled nor relinked, so this is valid only while libfoo stays binary-compatible, and such builds are recorded as `provenance: loose`.

See: [Iterate on a dependency without rebuilding the stack above it](COOKBOOK.md#iterate-on-a-dependency-without-rebuilding-the-stack-above-it).

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

### My build uses a custom tool and the console just says "Running build recipe" the whole time. Can I show configure/compile/install stages?

Yes. bits detects stages from commands for Autotools, CMake, Make, Ninja, Meson, Python builds and the `Configure`, `Make` and `MakeInstall` hooks; a recipe without a recognizable command or marker keeps the `Running build recipe` label until installation. Recipes using custom tools can mark their own transitions by printing `echo bits:stage:configure`, `echo bits:stage:compile` and `echo bits:stage:install` before the corresponding commands.

See: [Build pipeline](REFERENCE.md#build-pipeline).

### Is there a less boilerplate way to write CMake or autotools recipes?

The optional `bits-recipe-tools` package provides reusable shell function hooks such as `CMakeRecipe` and `AutoToolsRecipe`. Instead of writing a flat Bash build script, you override only the lifecycle hooks that differ from the defaults (`Prepare`, `Configure`, `Make`, `MakeInstall`, `PostInstall`). Worked examples are in the Cookbook section "Writing Recipes with bits-recipe-tools".

See: [Function-based recipes with bits-recipe-tools](REFERENCE.md#function-based-recipes-with-bits-recipe-tools).

### What is a repository provider and how do I make one?

A repository provider is a recipe that, instead of a package to build, describes another recipe repository to load at dependency-resolution time. Add `provides_repository: true` to a recipe's YAML header, with `source` pointing to a git repository whose top level contains `*.sh` recipe files, and `tag` selecting the branch, tag or commit (default: `version`). You can also set `repository_position` (append by default).

See: [Defining a repository provider](REFERENCE.md#defining-a-repository-provider).

### How do I make sure a provider tag hasn't been moved to a different commit?

Add an optional `commit:` integrity pin to the provider recipe, with the full hash or a prefix of at least 7 characters. The build fails if `tag` no longer resolves to that commit.

See: [Defining a repository provider](REFERENCE.md#defining-a-repository-provider).

### I set repository_position: prepend in my provider but I get a warning and it's appended anyway

By default every provider checkout is appended to `BITS_PATH`, whatever `repository_position` says; provider recipes cannot self-elevate. A `prepend` without a policy is downgraded to `append` with a warning naming the required entry. Whoever runs the build must grant it, e.g. `bits build --provider-policy myorg-extras:prepend MyPackage` (entries `name:prepend|append`, comma-separated).

See: [How position is resolved](REFERENCE.md#how-position-is-resolved), [Configuration](REFERENCE.md#configuration).

### How can I use my patched zlib.sh instead of the one from the upstream recipes?

Put the modified `zlib.sh` in a provider (e.g. `myorg-patches`) and grant it prepend access: `bits build --provider-policy myorg-patches:prepend ROOT`. bits then warns that the provider will shadow `zlib`; that warning is informational and expected. Note that the primary config directory (`-c`/`--config-dir`) is always first in the search order and cannot be shadowed by any provider.

See: [Example: patching a default recipe](REFERENCE.md#example-patching-a-default-recipe), [How position is resolved](REFERENCE.md#how-position-is-resolved).

### I don't want to type --provider-policy every time. Can I save it?

Yes, persist it for the current directory with `bits use build --provider-policy …`. Only `bits build` takes this flag.

See: [Configuration](REFERENCE.md#configuration).

### What's the best way to give my whole team a curated set of approved recipes?

Use a provider recipe with both `provides_repository: true` and `always_load: true` in the primary config directory (`-c`/`--config-dir`). It is cloned and added to `BITS_PATH` at startup, before the dependency graph is traversed, so its recipes are visible without any package declaring a dependency on it. The text calls this the recommended way to distribute a curated set of approved recipes across a team.

See: [Always-on providers (`always_load: true`)](REFERENCE.md#always-on-providers-always_load-true).

### How do I use my own provider registry instead of bitsorg/bits-providers?

Set `BITS_PROVIDERS`, e.g. `export BITS_PROVIDERS=https://github.com/myorg/my-recipes.git@main`, or prefix a single command with it. The `@tag` suffix is optional (default `main`); everything after the first `@` is taken as the tag, so use an `https://` URL, as SSH URLs like `git@github.com:org/repo` are not supported. An empty value falls back to the default; native `bits` cannot switch the registry off.

See: [The `bits-providers` standard repository](REFERENCE.md#the-bits-providers-standard-repository).

### Why does aliBuild not load the bits-providers registry like bits does?

The `aliBuild` wrapper emulates legacy aliBuild: its providers default is empty, so no registry is loaded and recipes come from a local `alidist` checkout (`aliBuild init` clones `alisw/alidist`). Native `bits` defaults `$BITS_PROVIDERS` to the official registry. An explicit `$BITS_PROVIDERS` overrides the default in either mode.

See: [Front-end choice: native `bits` (provider path) vs `aliBuild` (legacy path)](REFERENCE.md#front-end-choice-native-bits-provider-path-vs-alibuild-legacy-path).

### How do I check out the alice recipes to work on them and build ROOT?

Name the recipe repository on `bits init`: `bits init alice.bits` resolves it in the registry and clones it into `./alice.bits`. Then `bits init -c alice.bits ROOT` checks out a package for development beside it (even one whose recipe lives in a required provider repo such as `alidist.bits`), and `bits build -c alice.bits ROOT` builds it.

See: [Bootstrapping a recipe repository from the registry](REFERENCE.md#bootstrapping-a-recipe-repository-from-the-registry).

### Can my defaults file pull in our organisation's recipe repo automatically?

Yes. Declare the provider in a top-level `requires` or `build_requires` of the defaults file; its recipe (e.g. `myorg-recipes.sh`, with `provides_repository: true`) must be findable on the search path. These fields only seed the provider scan and do not become build dependencies. Unlike `always_load: true`, which fires on every build, this loads the provider only when that defaults profile is active.

See: [Triggering providers from a defaults file](REFERENCE.md#triggering-providers-from-a-defaults-file).

### I'm editing lcg.bits locally but bits keeps cloning the remote provider. How do I use my local copy?

Before cloning a declared provider `<pkg>`, bits looks for `<config_dir>/<pkg>/`; if it contains `*.sh` recipes it is used instead of a clone, with its git `HEAD` recorded (plus `-dirty` for uncommitted changes). So put the checkout there, e.g. `lcg.bits/` next to `lcg.bits.sh`. `--force-tracked` disables this, and an undeclared local sub-repo must be put on `BITS_PATH` with `--search-path NAMES`.

See: [How providers are discovered (two-phase)](REFERENCE.md#how-providers-are-discovered-two-phase).

### We pushed a new tag to our provider repo. Do I need to clear a cache for bits to see it?

No. Once a provider has a cached checkout, bits refreshes its git mirror on every run (even with `--no-fetch-repos`), so tag advances are detected and picked up on the next build without a manual cache purge. Checkouts are cached under `$BITS_WORK_DIR/REPOS/<package-lower>/<short_commit_hash>/`; a new commit gets a fresh checkout alongside the old one.

See: [Cache layout and staleness](REFERENCE.md#cache-layout-and-staleness).

### If the provider repo gets a new commit, will every package from it be rebuilt?

No. A provider's commit does not enter the build hash; each package is hashed from its own recipe text, sources, patches and its dependencies' hashes. A new provider commit rebuilds only packages whose recipes changed and their dependents. The provider name and commit are still recorded in the build manifest for provenance.

See: [Effect on build hashes](REFERENCE.md#effect-on-build-hashes).

### How do I mark a package whose licence forbids redistribution so it never gets uploaded?

Set `redistributable:` in the recipe front-matter to `all` (default), `binaries`, `sources` or `none`; with `none` neither binaries nor source archives leave the host, but the package is still built and usable locally. Licence fields (`license:`, `acknowledgment:`, `redistributable:`) are hash-excluded, so editing them causes no rebuilds. An unrecognised value fails closed as `none` with a warning.

See: [Licence compliance and redistribution policy](REFERENCE.md#licence-compliance-and-redistribution-policy).

### What lint check should I run before submitting a patch to bits? Can I run ruff format?

CI runs `ruff check .` in check-only mode, configured under `[tool.ruff]` in `pyproject.toml`; install it with `pip install ruff` and run the same check. Do not run `ruff format` or `ruff check --fix`: the hand-aligned 2-space style is kept on purpose and `--fix` would strip deliberate import-availability probes.

See: [10. Setting Up a Development Environment](REFERENCE.md#10-setting-up-a-development-environment).

## Recipes, defaults and environment

### Bits says "recipe has no '---' front-matter terminator line". What's wrong with my recipe?

A recipe is a YAML header, a line containing only `---`, then the Bash build script. The header ends at the first line that holds only `---` (surrounding spaces or tabs are allowed); a `---` sharing its line with other text, such as a comment or a value, does not count. Put `---` alone on its own line between the header and the script. The same rule applies to `defaults-*.sh` files.

See: [File layout](REFERENCE.md#file-layout).

### How should I name the recipe file for a package called ROOT?

The file name must be the `package:` name in lower case plus `.sh`, so `ROOT` goes in `root.sh`. Packages that depend on it must spell the name exactly as written in the `package:` field (here `ROOT`).

See: [Identity](REFERENCE.md#identity).

### Can I share a chunk of bash between several recipes instead of copy-pasting it?

Yes. In the Bash body, a line consisting only of `#!include <path.sh>` (resolved under the recipes repository root) or `#!include "path.sh"` (relative to the recipe's own directory) is replaced by that file's content before variable substitution and hashing, as if written inline. Includes may nest, but cycles, absolute paths and paths escaping their base with `..` are rejected. Ordinary `#include` lines are left alone.

See: [File layout](REFERENCE.md#file-layout).

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

### My patch was made with bare paths and needs -p0. Do I have to turn off auto patching?

No. Declare the level on the entry, e.g. `- generator-fix.patch:strip=0`; bits then applies it with `patch -p0` instead of `-p1`. `strip=N` may stand alone or be `&&`-joined with matcher clauses, but not combined with `||`, and the declared level is part of the build hash.

See: [Controlling patch application](REFERENCE.md#controlling-patch-application).

### How do I apply a patch only for one upstream version?

Add a matcher suffix to the entry, e.g. `"gaudi-GaudiToolbox.cmake.patch:version=v40r2"`. It is evaluated against the resolved version (after defaults `overrides:` and `requires:` pins), versions compare in natural (`sort -V`) order, and atoms combine with `&&` and `||`. Inactive patches are dropped before hashing, checkout and application, so they never affect the build hash.

See: [Conditional patches](REFERENCE.md#conditional-patches).

### What does --flavour do, e.g. --flavour cuda?

A flavour tunes a single build without editing defaults files: `NAME` sets it to `true`, `NAME=VALUE` to that value, `!NAME` to `false` (repeatable, comma-separated). It is merged into the defaults `variables:` (so `(?NAME)` matchers see it) and into `env:` (exported as `$NAME` in every recipe's build and part of the package hash), unless the variable is declared `export: false`. Flavours are global to the build and changing one re-hashes the affected packages, triggering a rebuild.

See: [Flavours](REFERENCE.md#flavours).

### My --flavour use-openloops gate never fires. Why?

Flavour and defaults `variables:` names must be plain identifiers (`[A-Za-z_][A-Za-z0-9_]*`); hyphens are not allowed. A hyphenated name is silently treated as an architecture regex, so its gate never fires, and it could not be exported as an env var either. Use `use_openloops` instead.

See: [Flavours](REFERENCE.md#flavours).

### How do I add a dependency only on macOS, and its Linux counterpart?

Use the predefined platform variables: `pkg:(?osx)` is a macOS-only dependency and `pkg:(?linux)` its counterpart. Note that `pkg:(?!osx)` is a negative-lookahead regex matched against the architecture string, not variable negation; there is no variable-negation atom.

See: [Defaults `variables:` and predefined platform variables](REFERENCE.md#defaults-variables-and-predefined-platform-variables).

### I defined a value under variables: but $NAME is empty in my build script. Why?

`variables:` is text templating only: it is used as `%(NAME)s` and never becomes a shell variable. `env:` is a shell variable only (`$NAME`). To use one name as both, pass it as a `--flavour` or define it in both `variables:` and `env:`.

See: [`variables` vs `env` vs `flavours` — quick comparison](REFERENCE.md#variables-vs-env-vs-flavours--quick-comparison).

### What's the variable for the install path of my dependency common.bits?

For every dependency bits exports `<PKG>_ROOT`, `<PKG>_VERSION`, `<PKG>_REVISION`, `<PKG>_HASH` and `<PKG>_COMMIT`, where `<PKG>` is the package name with every non-alphanumeric character turned into `_` and upper-cased. So `common.bits` gives `$COMMON_BITS_ROOT` (and `boost` gives `$BOOST_ROOT`).

See: [`variables` vs `env` vs `flavours` — quick comparison](REFERENCE.md#variables-vs-env-vs-flavours--quick-comparison).

### What's the difference between requires and build_requires?

`requires` lists runtime plus build-time dependencies; `build_requires` lists build-time-only ones such as `cmake` or `ninja`. `runtime_requires` is not a recipe input: bits fills it from the resolved `requires:` list, so a value written in a recipe is ignored.

See: [Dependencies](REFERENCE.md#dependencies).

### I'm iterating on a library I control and don't want every change to rebuild everything above it. Is there a way?

List it under `untracked_requires`: it is left out of the consuming package's identity hash, so changing it rebuilds only the dependency itself. You are responsible for ABI compatibility, since a reused consumer is not recompiled; such builds are recorded as `provenance: loose`. Give the dependency an explicit `force_revision` to keep its install path stable (without one bits warns, and under `revision_policy: "hash"` it stops).

See: [Dependencies](REFERENCE.md#dependencies).

### How do I let bits use the system's package instead of building it?

Set `prefer_system` to an architecture regex and give a `prefer_system_check` Bash snippet (it sees `$REQUESTED_VERSION`). When the regex matches, or with `--always-prefer-system`, bits runs the check: exit 0 uses the system package, non-zero builds it.

See: [System-package integration](REFERENCE.md#system-package-integration).

### Why is $JOBS so much lower than my core count when I build with --parallel?

bits caps `$JOBS` so concurrent builders don't oversubscribe the machine: at `ceil(requested × oversubscribe ÷ builders)` for CPU, and by memory (available memory split across builders, times `mem_utilisation`, divided by `mem_per_job`, which defaults to 2 GiB via `mem_per_job_default`). The result is at least 1. The final top-level package is exempt from the CPU split by default for `--parallel > 1`.

See: [Memory-aware parallelism](REFERENCE.md#memory-aware-parallelism).

### Pip install fails in my recipe with no network. How do I allow it?

Set `sandbox_network: off` in the recipe header; `off` allows network access, `on` blocks it. Without the field, `--sandbox-network` or `sandbox_network:` under the defaults `system:` block decides, else it is `on`. It is ignored with `--sandbox=off`.

See: [Build sandbox](REFERENCE.md#build-sandbox).

### How do I add a sha256 checksum to a source URL in a recipe?

Append it after a comma: `<url>,sha256:<hexdigest>`. The algorithm may be `sha256`, `sha512`, `sha1` or `md5`, and only a suffix matching `<algo>:<hex>` after the last comma is treated as a checksum, so URLs with commas in query parameters are safe. bits strips it before setting `$SOURCE0`, … .

See: [Checksum verification](REFERENCE.md#checksum-verification).

### I added checksums but a wrong one doesn't fail the build. Why?

The default mode is `off`: checksums are stored but never evaluated. Use `--check-checksums` (warn on mismatch) or `--enforce-checksums` (abort on mismatch, and also on a missing checksum), set `checksum_mode:` in the defaults profile, or `enforce_checksums: true` in the recipe. Precedence: `--print-checksums` > `--enforce-checksums` > `--check-checksums` > recipe `enforce_checksums: true` > defaults `checksum_mode:` > `off`.

See: [Checksum verification](REFERENCE.md#checksum-verification).

### How do I generate checksums for my recipes the first time?

Run `bits checksums --write` in the recipe repository to record every recipe's checksums without building, or `bits build --write-checksums <package>` to record those of the packages a build fetched. They go into `checksums/<pkgname>.checksum` sidecar files, which win over inline values; both commands add new entries and never overwrite one that disagrees. `--print-checksums` instead just prints the SHA-256 of every source and patch after the build.

See: [External checksum files](REFERENCE.md#external-checksum-files).

### Which defaults profile is used if I don't pass --defaults?

`release`, loading `defaults-release.sh`. Any other selection is layered on top of it: `--defaults dev` behaves like `release::dev` (a missing `defaults-release.sh` is skipped).

See: [Selecting a profile](REFERENCE.md#selecting-a-profile).

### How do I combine two defaults profiles, like dev and gcc13?

Join them with `::`, e.g. `bits build --defaults dev::gcc13 MyPackage`. This loads `defaults-release.sh`, `defaults-dev.sh` and `defaults-gcc13.sh` in that order and deep-merges their headers left to right: later scalars win, lists are concatenated, dicts are recursively merged.

See: [Combining multiple profiles with `::`](REFERENCE.md#combining-multiple-profiles-with-).

### How do I change the version of ROOT for my group without editing root.sh?

Use the `overrides:` map in a defaults profile, keyed by package name or regex (matched case-insensitively against the whole name), e.g. `ROOT:` with `version: "6-30-06"`. Each field in the entry replaces that field of the recipe; lists and maps are not merged with the recipe's.

See: [YAML fields specific to defaults files](REFERENCE.md#yaml-fields-specific-to-defaults-files).

### Building with gcc12 and gcc13 profiles overwrites the same install tree. How do I keep them apart?

Qualify the install architecture. Add `append_arch: -gcc13` to `defaults-gcc13.sh` (used verbatim, include the separator) so only files that opt in add a suffix, e.g. `sw/slc7_x86-64-gcc13/`. Alternatively `qualify_arch: true` appends every non-`release` default name in the chain; if any file uses `append_arch`, `qualify_arch` is ignored.

See: [Qualifying the install architecture](REFERENCE.md#qualifying-the-install-architecture), [Selective qualification with `append_arch`](REFERENCE.md#selective-qualification-with-append_arch).

### Bits enter can't find my package built with a gcc13 profile. What should I do?

The module frontend auto-detects only the raw architecture, so pass the combined one, e.g. `bits -a slc7_x86-64-dev-gcc13 enter MyPackage/1.2.3-1`; the success banner prints the exact command. You can also `export BITS_ARCHITECTURE=slc7_x86-64-dev-gcc13` for the session. To clean such a tree, use `bits clean -a <combined arch>`.

See: [Entering a qualified-architecture build](REFERENCE.md#entering-a-qualified-architecture-build), [Cleaning up](REFERENCE.md#cleaning-up).

### How do I install CMSSW_13_0_0 without the -1 revision at the end of the path?

Set `force_revision: ""` in a defaults profile, per package under `overrides:` (e.g. `"cmssw_.*": force_revision: ""`) or as a top-level fallback for every package. Beware: two builds of the same version then share one install path, `latest` symlinks are silently overwritten (bits warns), and the `local` prefix protection is bypassed.

See: [Forcing or Dropping the Revision Suffix (`force_revision`)](REFERENCE.md#forcing-or-dropping-the-revision-suffix-force_revision), [Risks and caveats](REFERENCE.md#risks-and-caveats).

### Why are my packages installed as version-local1 instead of version-1?

bits prefixes revision numbers with `local` (`local1`, `local2`, …) when there is no writable remote store. Setting `force_revision` bypasses this prefix logic.

See: [Risks and caveats](REFERENCE.md#risks-and-caveats), [Package identity](REFERENCE.md#package-identity).

### We're not at CERN. How do I stop bits using CERN's stores and console?

Mask CERN's site file and add your own: e.g. an empty `/etc/bits/site.d/10-CERN.yaml` plus `/etc/bits/site.d/10-mylab.yaml` with your stores and console. A file in a later directory replaces the one of the same name. `bits doctor` lists the site files in effect and where the main values come from.

See: [Site configuration](REFERENCE.md#site-configuration).

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

### How do I build in docker with my own image?

Use `bits build --docker-image IMAGE ...`, which implies `--docker`; without it the image is derived from the architecture and registry. Extra `docker run` arguments go in `--docker-extra-args ARGS` (bits always adds `--network=host` and, unless given, `--cpuset-cpus=<host CPUs>`), and `-v VOLUME` mounts an additional volume (repeatable).

See: [bits build](REFERENCE.md#bits-build).

### I'm on a laptop with no network. Will bits build still work?

By default (`-u`, `--fetch-repos`) bits fetches the source mirrors, but a mirror that cannot be fetched is used as it is, with a warning. An `http(s)://` or `b3://` read store that does not answer is noticed once at the start and the build goes on without it, with a warning (not when a write store is set). `--no-fetch-repos` skips fetching so a branch builds the commit its mirror already has (missing mirrors are still cloned), and `--no-remote-store` disables the default remote store.

See: [bits build](REFERENCE.md#bits-build).

### How do I upload the tarballs I build to my own bucket?

Add `--write-store URL`; several stores, comma-separated, all receive every built package, and an upload failure to any fails the package. Given alone it is also the read store, except where a default remote store applies; then use `--remote-store URL::rw` to read from and upload to that single store. `::rw` cannot be combined with `--write-store`.

See: [bits build](REFERENCE.md#bits-build).

### I want to build just the top of my stack on top of an LCG release already on CVMFS. Which options?

Use `--reuse-from PATH|cvmfs` (the literal `cvmfs` takes the location from the defaults `system:` layout) with `--reuse-policy relaxed`, or `--reuse-from cvmfs::relaxed`; relaxed accepts any deployed build of the same version, so only the top of the stack is built. Relaxed builds have loose provenance and cannot be published: combining them with `--write-store` is refused. To still build some packages yourself (e.g. one you need patched), list them with `--build-local PKG[,PKG…]`.

See: [bits build](REFERENCE.md#bits-build).

### After a successful build the BUILD directory and its log are gone. How do I keep them?

`BUILD/` directories are removed after a successful build unless `--no-auto-cleanup` is given, so run `bits build --no-auto-cleanup ...`. Development packages from `bits init` keep theirs anyway.

See: [bits build](REFERENCE.md#bits-build), [Work Directory Layout](REFERENCE.md#work-directory-layout).

### What's the difference between --check-checksums and --enforce-checksums?

`--check-checksums` warns on a source/patch checksum mismatch and continues the build; `--enforce-checksums` aborts on a mismatch or a missing checksum; `--print-checksums` prints them in YAML after the build. These three are mutually exclusive, and the command-line flag beats a recipe's `enforce_checksums: true` and the defaults' `checksum_mode:`. `--write-checksums` (adds them to `checksums/<package>.checksum`) is independent and can be combined with any of them.

See: [bits build](REFERENCE.md#bits-build).

### How do I switch on a flavour for a build?

Use `--flavour NAME[=VALUE]` (aliases `--flavor`, `--set`), repeatable or comma-separated: `NAME` sets it to `true`, `NAME=VALUE` to VALUE and `!NAME` to `false`. It gates `(?NAME)` conditional requires/sources/patches, is exported into the build environment (not for a variable declared `export: false`) and overrides a defaults `variables:` entry of the same name.

See: [bits build](REFERENCE.md#bits-build).

### My b3:// store is a MinIO bucket, not CERN S3. What do I have to set?

Set the endpoint with `--s3-endpoint URL` (or `$S3_ENDPOINT_URL`); the default is `https://s3.cern.ch`. MinIO usually needs `--s3-addressing-style path`. Give credentials preferably as `$AWS_ACCESS_KEY_ID` / `$AWS_SECRET_ACCESS_KEY` rather than `--s3-access-key` / `--s3-secret-key`, so the secret is not on the command line.

See: [bits build](REFERENCE.md#bits-build).

### REMOTE_STORE is set as a GitLab CI/CD variable. How can one gitlab-runner use a different store?

Set `BITS_REMOTE_STORE` in that runner's `config.toml` `environment`: bits consults the `BITS_`-prefixed name before the plain one, because GitLab makes a CI/CD variable win over a same-named runner entry. The order is the command-line flag, then `BITS_<NAME>`, then `<NAME>`, then (for the store URL) `remote_store:` in the defaults' `system:` block, then the built-in default. The same works for `WRITE_STORE`, `S3_ENDPOINT_URL`, `AWS_ACCESS_KEY_ID` and the other S3 settings.

See: [S3 store: common CI/CD config with per-runner overrides](REFERENCE.md#s3-store-common-cicd-config-with-per-runner-overrides).

### Using a b3:// store with bits build --docker: does boto3 need to be in my build image?

No, it must be on the build host: bits runs the remote-store sync in its own host-side Python process even for `bits build --docker`; the container only runs the compile steps. Install it with e.g. `sudo apt install python3-boto3` (Ubuntu/Debian) or `sudo dnf install python3-boto3` from EPEL (AlmaLinux/RHEL/Fedora), or `pip3 install --break-system-packages boto3`. `s3://` stores instead need the `s3cmd` binary and an `~/.s3cfg`.

See: [S3 store: common CI/CD config with per-runner overrides](REFERENCE.md#s3-store-common-cicd-config-with-per-runner-overrides).

### Bits deps wants Graphviz. Can I get the dependency tree without it?

Yes: `bits deps --outmake FILE PACKAGE` writes the dependency tree as Makefile rules (`pkg: dep1 dep2`) and does not require Graphviz; `--outgraph FILE` is the PDF graph that needs it. At least one of `--outgraph`/`--outmake` is required, and `--runtime-only` (with `--outmake`) leaves out build-only dependencies.

See: [bits deps](REFERENCE.md#bits-deps).

### How do I check my machine is set up to run bits?

Run `bits doctor` with no package (setup mode): it checks the Python bits runs with and its modules, running as root, git, a C++ compiler, the container engine behind `docker`, disk space, and each `--remote-store`/`--write-store`. Each check is PASS / WARN / FAIL / SKIP with a one-line fix, and any FAIL gives exit code 1; `--json` is supported.

See: [bits doctor](REFERENCE.md#bits-doctor).

### Bits doctor --runner fails on my workstation because there's no Docker daemon

`--runner` mode is meant for CI build hosts: a missing Docker daemon is a FAIL there, and it does not check Python modules or the write store. On a workstation use setup mode, i.e. `bits doctor` without `--runner`.

See: [bits doctor](REFERENCE.md#bits-doctor).

### Before a long build, how can I see which packages are already pre-built in the store?

`bits doctor --check-store PACKAGE ...` computes the expected tarball hash of each package bits would build and probes the remote store (it needs `--remote-store` or the architecture's default); the report is informational and always exits 0. Only `http(s)://` and local-directory stores can be probed, others are SKIP. For branch builds the commit hash is approximated, so if every package shows FAIL, re-run with `bits status --check-store` for accurate hashes.

See: [bits doctor](REFERENCE.md#bits-doctor).

### Bits status reports hash_unknown for some packages

`hash_unknown` means the git refs are unavailable because the mirror is not populated yet, e.g. with `--no-fetch-repos`, which uses only the refs already cached. Run `bits status` without `--no-fetch-repos`: the default `-u` / `--fetch-repos` fetches the mirrors first (it needs network access).

See: [bits status](REFERENCE.md#bits-status).

### Bits status says every package will be built from source, but my --docker build reuses them

Give `bits status` the same `--docker` / `--docker-image IMAGE` as the build: system packages are then checked in the image and `own_hash` packages (the toolchain) hash its container fingerprint; without it every package is built. To see tarballs that are only in the remote store (`from_remote_store`), add `--check-store` with `--remote-store URL`, which is only consulted with `--check-store`.

See: [bits status](REFERENCE.md#bits-status).

### Bits verify exited with code 2, what does it mean?

`bits verify --from-manifest FILE` checks a live deployment against a build manifest; exit 2 is MISS, a tarball was not found. 0 means consistent, 1 is FAIL (hash/commit mismatch) and 3 means the manifest is unreadable.

See: [bits verify](REFERENCE.md#bits-verify).

### How do I find out who built, published and approved a library on CVMFS?

Run `bits trace /cvmfs/.../lib/libCore.so`: from the package's `.meta.json` it shows package, version, content hash, the build, the recipes' commit and bits version, and the console backend names the publishing pipeline and, for each pipeline, who requested it and who approved it with their passkey. It authenticates with your GitLab token (`~/.bits/gitlab-token` or `$BITS_GITLAB_TOKEN`); without one it shows the package only. The path need not be mounted.

See: [bits trace](REFERENCE.md#bits-trace).

### How do I make my build's binaries trusted so others can reuse them?

Use `bits certify`: it uploads what the store is missing, gets a passkey approval through bits-console and opens the merge request (certification merge requests come from `bits certify`, not `bits publish`). `bits sign` signs a build manifest; it is the former `bits certify`. `bits certify --help` and `bits sign --help` list every option.

See: [bits certify / bits sign](REFERENCE.md#bits-certify--bits-sign), [bits publish](REFERENCE.md#bits-publish).

### I exported an upgrade plan CSV from the console. How do I apply it to my recipes?

Run `bits upgrade PACKAGE ... plan.csv` (without a file it takes the newest `upgrade-plan-*.csv` in the current directory); `--wave N` limits it to waves (0 known exploited, 1 within the major version, 2 major upgrades), `--line LINE` picks a release line from the converged plan, and `--to PKG=VER` adds or overrides versions. `bits -n upgrade ...` only reports what it would change. Then build, fix what is left, and commit the edited and copied files to a branch.

See: [bits upgrade](REFERENCE.md#bits-upgrade).

### Which packages took longest or used most memory in my last parallel build?

Run `bits stats`; it summarises the data recorded with `--resource-monitoring` (on by default for `--parallel > 1`). `--sort time|rss|cpu` sorts by wall time, peak memory or peak CPU, `--top N` sets the number of packages (default 10), and `--package NAME` shows one package's timeline.

See: [bits stats](REFERENCE.md#bits-stats).

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

### Can I find out where on CVMFS a package will be published before building it?

Yes: `bits cvmfs-path --package NAME` (required) prints the absolute `/cvmfs/<repo>/<path>` the package will be published to, resolved from the path templates in the defaults `system:` block, without building. Add `--version VER`, `--kind {releases,packages,modules,shared}` and the same `--defaults`, `-a` and `--set` as the build; `--day` defaults to the current UTC weekday, so pass the same `--day` as the build.

See: [bits cvmfs-path](REFERENCE.md#bits-cvmfs-path).

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

### Uploads to my b3 store seem to use different S3 keys than the ones in my ~/.bits/s3keys. Which credentials win?

The precedence is: `--s3-*` flags, then the environment (as set by CI), then the `~/.bits/s3keys` file, then the built-in default. So keys in the environment override the file; CI-injected credentials are never overridden by it.

See: [S3-compatible via `boto3` (`b3://`)](REFERENCE.md#s3-compatible-via-boto3-b3).

### Can I upload build tarballs to an https:// store?

No. The HTTP/HTTPS backend is read-only: bits downloads tarballs from it with automatic retries (up to four attempts) but cannot upload. Pair it with a writable `--write-store` (rsync, `s3://` or `b3://`) if you need uploads.

See: [HTTP / HTTPS](REFERENCE.md#http--https).

### Bits build --remote-store cvmfs://... fails with an error. How do I reuse what's already on CVMFS?

`cvmfs://` is not a `--remote-store` backend; `--remote-store` is the tarball store, and a `cvmfs://` value is rejected with an error pointing at `--reuse-from`. To reuse components already deployed on CVMFS, use `--reuse-from` with the release's modules tree or the literal `cvmfs`.

See: [Supported backends](REFERENCE.md#supported-backends), [Reusing deployed components (`--reuse-from`)](REFERENCE.md#reusing-deployed-components---reuse-from).

### I already built my stack locally without --write-store. How do I push it to the S3 store now?

Use `bits publish`: with no package name it reads the latest build manifest and uploads each package's tarball to the S3 store. Choose the bucket with `--remote-store` (e.g. `bits publish --remote-store https://s3.cern.ch/lcgapp-bits-testing`) or a specific manifest with `--from-manifest`. Rebuilding does not help, because nothing re-uploads on a cached rebuild.

See: [Publishing an existing local build to S3 (`bits publish`)](REFERENCE.md#publishing-an-existing-local-build-to-s3-bits-publish).

### Can I see what bits publish would upload before actually pushing?

Yes: `bits publish --dry-run` (or `-n`) lists exactly what would be uploaded and to which store without contacting S3, so no credentials or network are needed.

See: [Publishing an existing local build to S3 (`bits publish`)](REFERENCE.md#publishing-an-existing-local-build-to-s3-bits-publish).

### Bits publish --to s3 doesn't work anymore. How do I upload a single package to the store?

The single-package S3-store upload is now `bits store upload`, which replaces the removed `bits publish --to s3`, e.g. `bits store upload ROOT --remote-store b3://lcgapp-bits-testing`. `bits publish PACKAGE --cvmfs-target … --prepub-url …` is for publishing one package to CVMFS.

See: [Publishing an existing local build to S3 (`bits publish`)](REFERENCE.md#publishing-an-existing-local-build-to-s3-bits-publish).

### After bits publish, I don't see a manifest under MANIFESTS/ in the bucket, and some packages are missing. Why?

A bulk publish uploads a trimmed copy of the manifest (one per architecture) under `MANIFESTS/` only if every package uploaded, so any failed upload leaves it out. Packages whose `redistributable:` forbids binary redistribution are skipped.

See: [Publishing an existing local build to S3 (`bits publish`)](REFERENCE.md#publishing-an-existing-local-build-to-s3-bits-publish).

### I built with --write-store but some packages were not uploaded. Which packages does bits skip?

Packages recalled from a store, packages with `local` revisions, and packages whose `redistributable:` forbids binary redistribution are not uploaded. Only packages built from source in this run are uploaded.

See: [Build lifecycle with a store](REFERENCE.md#build-lifecycle-with-a-store).

### Why do my packages get revisions like local1 instead of 1?

Builds without a write store, and development packages (`bits init`), get `local` revisions (`local1`, `local2`, …). They are never uploaded, so in-progress work cannot reach the shared cache. With a write store, a new hash gets the next free integer revision.

See: [Revision numbering](REFERENCE.md#revision-numbering).

### My build aborted with a store integrity mismatch showing expected and actual SHA-256. What should I do?

A mismatch is always fatal: the tarball from the store differs from the digest recorded at upload time. Delete the local tarball (`rm -rf $WORK_DIR/TARS/<arch>/store/<h2>/<hash>/`), fetch it from an independent source, compute `sha256sum`, and compare with the ledger file under `$WORK_DIR/STORE_CHECKSUMS/`. If the independent copy matches the ledger but the store does not, treat the store as compromised (rotate credentials, audit access logs, rebuild from source); if the change is confirmed benign, delete the `.sha256` ledger file and the next build re-records it.

See: [Investigating a mismatch](REFERENCE.md#investigating-a-mismatch).

### How do I turn on checksum verification of tarballs downloaded from the store?

The store integrity ledger is off by default. Enable it per run with `bits build --store-integrity ...`, or persistently for the directory with `bits use build --store-integrity`. Set `BITS_STRICT_STORE_INTEGRITY=1` to also make a tarball with no ledger entry fatal (useful in CI).

See: [Enabling store integrity verification](REFERENCE.md#enabling-store-integrity-verification), [Strict mode for CI (no unverified tarballs)](REFERENCE.md#strict-mode-for-ci-no-unverified-tarballs).

### The upstream URL for a tarball in my recipe's sources: has disappeared. Can bits still build it?

Yes, if it was archived before: when a remote store is configured, files fetched through `sources:` are archived in it automatically under `SOURCES/cache/`, and bits looks in the local cache, then the store, before going to the upstream URL. Uploading the archive needs a write store (an `http(s)://` store alone is read-only), and it is skipped when the recipe's `redistributable:` forbids redistributing sources.

See: [Source archive caching](REFERENCE.md#source-archive-caching), [How it works](REFERENCE.md#how-it-works), [Enabling source archive caching](REFERENCE.md#enabling-source-archive-caching).

### How do I build just my package on top of an LCG release that's already on CVMFS?

Use `--reuse-from` pointing at the release's published modules tree, or the literal `cvmfs` to resolve it from the defaults. Reused components are sourced in place from `/cvmfs` and logged as 'Reusing CVMFS overlay module … (not built)'; only the top of the stack is compiled. Add `--build-local PKG[,PKG…]` to force specific packages to build locally anyway.

See: [Reusing deployed components (`--reuse-from`)](REFERENCE.md#reusing-deployed-components---reuse-from).

### Why is --reuse-policy relaxed rejected when I also pass --write-store?

Relaxed reuse takes any version present in the release overlay, so the result has loose provenance and is not reproducible from the hash alone; the publish path therefore refuses it. The default `--reuse-policy strict` reuses a component only on an exact content-hash match, which keeps the build reproducible and publishable.

See: [Reusing deployed components (`--reuse-from`)](REFERENCE.md#reusing-deployed-components---reuse-from).

### A package I built with --reuse-from doesn't work when moved to another machine or prefix. Why?

Reused components are used in place, so a package built on them references their `/cvmfs/...` paths (in `init.sh`, and possibly RPATHs and config files) and needs that repository mounted at run time; relocation moves only the package's own prefix. For a self-contained, relocatable work directory, or to publish to a different repository or prefix, build without `--reuse-from`. Also make sure the builder image matches the reused release's OS and compiler.

See: [Reusing deployed components (`--reuse-from`)](REFERENCE.md#reusing-deployed-components---reuse-from).

### Bits build --docker on an EL9 host tries to pull an x86_64-slc9 image that doesn't exist. What's going on?

Without `--docker-image`, the image name is derived from the architecture (OS, CPU and any `cuda` part). The default name bits gives an EL9 host, `slc9_x86-64`, asks for `x86_64-slc9`, which bits-containers does not publish; it publishes `x86_64-el8/el9/el10`, `aarch64-el9` and `x86_64-ubuntu2204/2404/2604`. Use an architecture whose image exists (e.g. `x86_64-el9`) or pass `--docker-image` explicitly.

See: [22. Docker Support](REFERENCE.md#22-docker-support).

### Fetching a git repo over SSH fails inside the --docker build. Are my SSH keys available in the container?

No: `~/.ssh` is not mounted into the container. bits mounts the work directory (with `SOURCES/` read-only), the recipe directory (read-only), the bits installation and any `-v` volumes, plus `/cvmfs` read-only when `--reuse-from` is used.

See: [22. Docker Support](REFERENCE.md#22-docker-support).

### Pip install fails in my recipe when building with the sandbox on. How do I allow network access?

The sandbox blocks outgoing network by default. Add `sandbox_network: off` to the recipe header to allow it for that recipe; a build-wide default can be set with `bits build --sandbox-network on|off` or `sandbox_network:` in the defaults, but the recipe's own field always wins. The field is ignored when the sandbox is off.

See: [Per-recipe network control](REFERENCE.md#per-recipe-network-control).

### Can I build aarch64 packages on my x86-64 machine?

Yes, with `--docker` and QEMU user-mode emulation. Register the QEMU binfmt handlers once on the host (e.g. `docker run --rm --privileged multiarch/qemu-user-static --reset -p yes`), then run e.g. `bits build MyAnalysis -a aarch64-el9 --docker`; bits injects the matching `--platform` automatically. Expect roughly 20-50 % of native speed, so it suits small analysis packages and validation builds, not full stacks like ROOT or Geant4.

See: [22.2 Cross-compilation via QEMU](REFERENCE.md#222-cross-compilation-via-qemu), [Automatic platform injection](REFERENCE.md#automatic-platform-injection), [Performance expectations](REFERENCE.md#performance-expectations).

### How can I check that what's deployed on CVMFS matches what was built?

Run `bits verify --from-manifest <manifest.json> --cvmfs-root /cvmfs/<repo> --work-dir <dir>`. For each package it locates the content-addressed tarball (first under `--cvmfs-root`, then the work directory) and compares its SHA-256 with `tarball_sha256` in the manifest; it also checks provider commits and the architecture, reporting PASS, FAIL, MISS or SKIP. Add `--json` for machine-readable output.

See: [23. bits verify — Deployment Verification](REFERENCE.md#23-bits-verify--deployment-verification), [What is checked](REFERENCE.md#what-is-checked), [Search order](REFERENCE.md#search-order).

### Bits verify says Architecture FAIL even though I'm on the right kind of node. Why?

The manifest's `architecture` is compared as an exact string with the architecture bits detects on the host. A manifest whose architecture carries a defaults suffix (for example `slc9_x86-64-gcc13`) therefore reports FAIL even on a matching host, and the FAIL counts toward the exit code.

See: [What is checked](REFERENCE.md#what-is-checked).

### What do the bits verify exit codes mean? My CI job exits with 2.

0 means everything verifiable matches; 1 means at least one FAIL (checksum or commit mismatch); 2 means at least one MISS (a tarball not found, so consistency is unknown) and no FAIL; 3 means the manifest file is missing, unreadable or not valid JSON. If there are both FAILs and MISSes, exit code 1 takes precedence.

See: [Exit codes](REFERENCE.md#exit-codes).

### Where does bits build write its manifest?

Every `bits build` writes a JSON manifest to `$WORK_DIR/MANIFESTS/` as `bits-manifest-<package>-<UTC timestamp>.json`, and keeps the symlink `bits-manifest-latest.json` pointing at the newest one; the file name is also printed in the success banner. It is written incrementally, so a failed build still leaves a partial manifest with `status: failed` and the `failed_package`.

See: [25. Build Manifest](REFERENCE.md#25-build-manifest), [Manifest location and naming](REFERENCE.md#manifest-location-and-naming), [Manifest schema reference](REFERENCE.md#manifest-schema-reference).

### Does bits build --from-manifest rebuild exactly what the manifest recorded?

No. A replay takes only the package list (`requested_packages`) from the manifest; the architecture, `--defaults` and the recipe repository come from the current invocation and checkout, versions and hashes are not pinned, and recalled tarballs are not checked against `tarball_sha256`. For an exact replay, check out the manifest's `config_commit` (and provider commits), pass the recorded `architecture` and `defaults`, and use `bits verify` to check tarballs.

See: [Replaying a build with `--from-manifest`](REFERENCE.md#replaying-a-build-with---from-manifest).

### Does bits work on Windows, and can recipes fetch sources from SVN or Mercurial?

No. bits runs on Linux and macOS (Intel and Apple Silicon) only, and version-controlled sources must come from Git or Sapling. Plain archives can still be downloaded through a recipe's `sources:` list (HTTP(S), FTP(S) or local `file:` URLs).

See: [Current limitations](REFERENCE.md#current-limitations).

## Communities

### How do I build O2Physics for ALICE with bits?

In an empty directory (e.g. `mkdir alice && cd alice`) run `bits init alice.bits`, record the options once with `bits use build -c alice.bits --defaults alidist::o2`, then run `bits build O2Physics`; alidist is fetched automatically. Afterwards `bits enter O2Physics/latest` opens a sub-shell with O2Physics loaded (leave with `exit`). `O2` builds the same way.

See: [Getting started with native bits](https://github.com/bitsorg/alice.bits/blob/main/README.md#getting-started-with-native-bits).

### Which --architecture do I use for ALICE on EL9? Is it x86_64-el9 like the other experiments?

No. ALICE uses aliBuild-style names because the alidist recipes only recognise `slc*` and `ubuntu*` values of `$ARCHITECTURE`: `slc9_x86-64` (EL9 x86-64), `slc9_aarch64` (EL9 ARM64), `slc8_x86-64` (EL8 x86-64), `ubuntu2204_x86-64` and `ubuntu2404_x86-64`. On a matching host bits detects them itself. EL10 and Ubuntu 26.04 are not supported by alidist yet.

See: [Prerequisites](https://github.com/bitsorg/alice.bits/blob/main/README.md#prerequisites).

### How do I build ALICE O2 with the o2-epn defaults instead of o2?

Replace `o2` with the alidist flavour you want, e.g. `--defaults alidist::o2-epn`. `alidist::<flavour>` loads `release` and the `alidist` variant from alice.bits plus alidist's defaults for that flavour. Packages reject flavours they do not list in their `valid_defaults`.

See: [Getting started with native bits](https://github.com/bitsorg/alice.bits/blob/main/README.md#getting-started-with-native-bits).

### Where do ALICE packages end up on CVMFS?

Under `/cvmfs/bits.cern.ch/alice/<install-dir>/Packages/<pkg>/<version>`, with modulefiles in `/cvmfs/bits.cern.ch/alice/<install-dir>/Modules/modulefiles/<pkg>` and architecture-independent packages under `/cvmfs/bits.cern.ch/alice/noarch/`. The install directory follows the OS-first ALICE convention (`el9-x86_64`, `el9-aarch64`, `ubuntu2404_x86_64`), not the architecture string. To check where a package would land, use `bits cvmfs-path`.

See: [Notes for ALICE users](https://github.com/bitsorg/alice.bits/blob/main/README.md#notes-for-alice-users).

### I want to change compiler flags or a version pin for ALICE O2. Do I edit alice.bits?

No. Compiler flags, disabled packages and version pins come from the alidist flavour (`defaults-o2.sh`, ...), so change them in alidist. alice.bits holds only the CVMFS layout and the bits-specific build settings; its `defaults-release.sh` must stay, because it hides alidist's legacy ROOT 5 file of the same name.

See: [Notes for ALICE users](https://github.com/bitsorg/alice.bits/blob/main/README.md#notes-for-alice-users).

### Should I use aliBuild or native bits to build ALICE software?

Native `bits` with `alice.bits` is recommended and is what the bits-console CI uses; it loads alidist (at branch `master`) through the provider registry together with the fixes and CVMFS layout of alice.bits. The `aliBuild` front-end with a local `alidist` checkout is the legacy path: no registry, legacy `init.sh` and the `alisw/<distro>-builder` images for `--docker`; alice.bits is not used there.

See: [Two ways to build ALICE software](https://github.com/bitsorg/alice.bits/blob/main/README.md#two-ways-to-build-alice-software).

### Why doesn't the CVMFS bits reuse published ALICE packages? I get a warning that reuse is skipped.

Reuse from CVMFS, and the published CVMFS modules, do not work yet for `alice` (nor for `cms`): the hand-written community table has no prefix for them. For alice, bits cannot expand the `{install_dir}` in alice.bits' modules template (cms.bits declares no CVMFS layout), so their builds skip reuse with a warning.

See: [Open points](https://github.com/bitsorg/bits.bits/blob/main/README.md#open-points).

### How do I build Athena for ATLAS with bits?

Run `bits init atlas.bits && cd atlas.bits`, record the settings with `bits use build --docker --architecture x86_64-el9 --defaults atlas::gcc15::opt --set release=LCG_110 --set athena_release=release/25.0.75`, then `bits build Athena`, which builds the LCG base, AthenaExternals and Athena. `bits enter Athena/latest` gives you a shell with it; the build architecture is `x86_64-el9-gcc15-opt`.

See: [Getting started](https://github.com/bitsorg/atlas.bits/blob/main/README.md#getting-started).

### Which platforms have pre-built ATLAS binaries? Can I build Athena on Ubuntu?

The ATLAS CI builds `x86_64-el9` (ATLAS targets `x86_64-el9-gcc15-opt`), `x86_64-el8`, `x86_64-el10` and `aarch64-el9`; Ubuntu is not a supported Athena platform. Pass the platform as `--architecture`, because by itself bits names an EL9 machine `slc9_x86-64`, and build with `--docker`, the only way to reuse what the ATLAS CI built.

See: [Prerequisites](https://github.com/bitsorg/atlas.bits/blob/main/README.md#prerequisites).

### Building Athena for ATLAS, lcg-view stops with an error. What's wrong?

Most likely no release was given: `lcg-view` stops with an error on the default `main`, because the ATLAS manifest needs an LCG release number. Always pass the release on the command line, `--set release=LCG_110`, or record it with `bits use`; a release chosen any other way hashes differently, so nothing built by the other groups is reused.

See: [Notes for ATLAS users](https://github.com/bitsorg/atlas.bits/blob/main/README.md#notes-for-atlas-users).

### How do I pick which Athena release is built for ATLAS, and can I build an older one?

Set `athena_release` (default `main`) to the athena ref, e.g. `--set athena_release=release/25.0.75`; it is used by both `Athena` and `atlasexternals`. The ref must use the LCG that `lcg-view` provides (`LCG_110_ATLAS_5`: 25.0.71 and later), or the build stops. Athena 25.0.74 and later need gcc15; build an older ref with the compiler it was released with, e.g. `--defaults atlas::gcc14::opt`.

See: [Notes for ATLAS users](https://github.com/bitsorg/atlas.bits/blob/main/README.md#notes-for-atlas-users), [Getting started](https://github.com/bitsorg/atlas.bits/blob/main/README.md#getting-started).

### How do I build the Key4hep stack with bits?

Run `bits init key4hep.bits && cd key4hep.bits`, then `bits use build --docker --architecture x86_64-el9 --defaults key4hep::gcc15::opt --set release=LCG_110`, and `bits build key4hep` for the complete stack (`bits build --dry-run key4hep` first shows what would be reused). `bits enter key4hep/latest` opens a shell with it. A single package builds the same way, e.g. `bits build DD4hep`.

See: [Getting started](https://github.com/bitsorg/key4hep.bits/blob/main/README.md#getting-started).

### I build Key4hep on my EL9 machine but nothing is reused from CI. Which platforms have binaries?

The Key4hep CI builds `x86_64-el8`, `x86_64-el9`, `x86_64-el10`, `aarch64-el9`, `x86_64-ubuntu2204`, `x86_64-ubuntu2404` and `x86_64-ubuntu2604`. Pass the platform as `--architecture` (by itself bits names an EL9 machine `slc9_x86-64`, which is not one of them) and build with `--docker`; without `--docker` bits uses this machine's compilers and reuses nothing the CI built.

See: [Prerequisites](https://github.com/bitsorg/key4hep.bits/blob/main/README.md#prerequisites), [Getting started](https://github.com/bitsorg/key4hep.bits/blob/main/README.md#getting-started).

### How do I use the Key4hep software published on CVMFS without building anything?

Run `/cvmfs/bits.cern.ch/bits/bin/bits --community key4hep -a x86_64-el9-gcc15-opt enter <pkg>/<tag>`. In a grid job use `... setenv <pkg>/<tag> -c ./run.sh` instead; no work directory is needed.

See: [Notes for Key4hep users](https://github.com/bitsorg/key4hep.bits/blob/main/README.md#notes-for-key4hep-users).

### How do I set up nightly or on-push CI builds for Key4hep?

They are set up in bits-console, on the Key4hep community's Schedules page (steps 4 and 5 of Phase 4 of the bits workflow guide). The binary store, certification group and manifests repository are supplied by bits-console in CI, not set in key4hep.bits.

See: [Notes for Key4hep users](https://github.com/bitsorg/key4hep.bits/blob/main/README.md#notes-for-key4hep-users).

### How do I build the LHCb externals with bits?

Run `bits init lhcb.bits && cd lhcb.bits`, then `bits use build --docker --architecture x86_64-el9 --defaults lhcb::gcc14::opt --set release=LCG_110` and `bits build lcg-view`, which builds all LHCb externals, then the manifest and toolchain. `bits enter lcg-view/latest` opens a sub-shell with the externals loaded. To build only the externals, without manifest and toolchain, build `lcg-externals` instead.

See: [Getting started](https://github.com/bitsorg/lhcb.bits/blob/main/README.md#getting-started).

### How do I build LHCb projects like Gaudi or LHCb against the bits-built externals?

After the usual LbEnv setup (`source /cvmfs/lhcb.cern.ch/lib/LbEnv`), run `source setupLHCb.sh` from lhcb.bits. It takes `sw/<arch>/lcg-view/latest`, or the newest `lcg-view` published on CVMFS when nothing is built locally, and sets `BINARY_TAG`, `LCG_VERSION`, `LCG_RELEASE_BASE` and `CMAKE_PREFIX_PATH`. Then build with LbDevTools or lb-stack-setup as usual, in native mode (`useDocker false`), not in its Docker container.

See: [Building LHCb software against the result](https://github.com/bitsorg/lhcb.bits/blob/main/README.md#building-lhcb-software-against-the-result).

### Why don't the LHCb externals built with bits have the LCG_110 versions?

lhcb.bits builds the externals at the versions of LHCb's own LCG flavour (now `LCG_108c` `LHCB_11`), not those of the LCG release the recipes come from. They are carried as overrides in `defaults-lhcb.sh`, generated by `tools/lcg-deltas.py` from LHCb's lcg-toolchains; to move to another flavour, regenerate them with that script.

See: [LHCb versions](https://github.com/bitsorg/lhcb.bits/blob/main/README.md#lhcb-versions).

### How do I build FairShip for SHiP?

Run `bits init ship.bits && cd ship.bits`, then `bits use build --docker --architecture x86_64-el9 --defaults ship::gcc15::opt --set release=LCG_110` (on macOS leave out `--docker` and `--architecture`), `bits build FairShip` and `bits enter FairShip/latest`. The build architecture is `x86_64-el9-gcc15-opt`.

See: [Getting started](https://github.com/bitsorg/ship.bits/blob/main/README.md#getting-started).

### Why does the SHiP build rebuild ROOT instead of reusing the LCG one?

ship.bits has its own ROOT recipe (v6.38.00, built with Pythia8, because FairShip uses TPythia8) that replaces the lcg.bits one (v6.40.02, Pythia8 off) for the whole SHiP build; `pythia6` and `GENIE` (FairShip needs GENIE 3) are replaced the same way. Each replacement changes the hash of that package and of everything depending on it, so those are rebuilt for SHiP; everything below them is still reused.

See: [Notes for SHiP users](https://github.com/bitsorg/ship.bits/blob/main/README.md#notes-for-ship-users).

### Where is SHiP software published on CVMFS? Is there a release view?

Under `/cvmfs/bits.cern.ch/ship`, with the packages and modulefiles templates of the shared stacks.bits layout. SHiP has no release views (the release and view templates are empty), so the path does not depend on the release. Preview a path with `bits cvmfs-path -c . --defaults ship::gcc15::opt --admin --package <pkg> --version <v> --platform <arch>`.

See: [Notes for SHiP users](https://github.com/bitsorg/ship.bits/blob/main/README.md#notes-for-ship-users).

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

### I pushed to a branch of lcg.bits. Will that CI build be published to CVMFS?

No. Nightly builds and builds on each push to a branch of lcg.bits are set up as schedules in bits-console; a build on a push goes to the binary store only, and is neither certified nor published. Publishing and certifying the LCG stack is done by bits-console.

See: [CI and publishing](https://github.com/bitsorg/lcg.bits/blob/main/README.md#ci-and-publishing).

### Can I use bits without installing it, straight from CVMFS?

Yes: run `/cvmfs/bits.cern.ch/bits/bin/bits`, which brings its own Python, Tcl and Environment Modules runtime. Given a community (`--community key4hep`, `BITS_COMMUNITY=LHCB`, or a link named e.g. `bits-key4hep`) it builds with that community's recipes and reuses what the community has published on CVMFS. It runs on glibc Linux: el8 and later on x86_64, el9 and later on aarch64, and current Ubuntu, but not on musl systems such as Alpine.

See: [bits.bits](https://github.com/bitsorg/bits.bits/blob/main/README.md#bitsbits), [Building and publishing](https://github.com/bitsorg/bits.bits/blob/main/README.md#building-and-publishing).

### The CVMFS bits reuses my community's published packages. How do I turn that off?

For a community listed in `etc/communities` the entry point sets `BITS_REUSE_FROM=cvmfs` unless the variable is already set, so a build reuses what the community published on CVMFS by exact content hash and builds the rest. Set `BITS_REUSE_FROM=` (empty) to turn it off; an explicit `--reuse-from`, also from a `bits use` profile, wins.

See: [The entry point](https://github.com/bitsorg/bits.bits/blob/main/README.md#the-entry-point).

### How do I tell bits which experiment's recipes to use?

Select the community with `BITS_COMMUNITY=<NAME> bits build <pkg>` or `bits --community <NAME> build <pkg>` (`BITS_ORGANISATION`, its former name, still works), or by having your stack `requires:` it. bits then reads the matching provider recipe in bits-providers (e.g. `ALICE` gives `alice.bits`), clones it into `sw/REPOS/` and follows its `requires:` to the shared pools, e.g. `lhcb.bits → stacks.bits → lcg.bits`.

See: [Adding a provider](https://github.com/bitsorg/bits-providers/blob/main/README.md#adding-a-provider), [How bits uses this repository](https://github.com/bitsorg/bits-providers/blob/main/README.md#how-bits-uses-this-repository).

### How can I test publishing an LHCb or ALICE build without touching the production CVMFS repository?

Use the `testbed` overlay from testbed.bits, which changes only `system.cvmfs_repository` to `test.cvmfs.io`, so the tree moves from `/cvmfs/bits.cern.ch/<group>/...` to `/cvmfs/test.cvmfs.io/<group>/...`. It must be the last element of the defaults chain, e.g. `--defaults lhcb::gcc14::opt::testbed` (for ALICE `alidist::o2::testbed`). Locally, put testbed.bits on the search path (`--search-path`) and check what it resolves to with `bits build --dry-run` or `bits cvmfs-path`. The publish itself runs in the bits-console Testbed community, which appends `testbed` to the chain automatically and only publishes under `/cvmfs/test.cvmfs.io`; the testbed services must be running for it to succeed.

See: [testbed.bits](https://github.com/bitsorg/testbed.bits/blob/main/README.md#testbedbits), [Getting started: trying the overlay locally](https://github.com/bitsorg/testbed.bits/blob/main/README.md#getting-started-trying-the-overlay-locally), [How it is used in testing](https://github.com/bitsorg/testbed.bits/blob/main/README.md#how-it-is-used-in-testing).

### Will adding the testbed overlay make bits rebuild the whole stack?

No. A package's hash comes from its recipe, sources, patches, dependencies and the merged `env:` of the defaults, not from where it is published, and the `system:` block the overlay changes is never hashed. The hashes are the same as in the production build, so the certified store is reused and only the publish destination changes.

See: [Why the overlay changes nothing else](https://github.com/bitsorg/testbed.bits/blob/main/README.md#why-the-overlay-changes-nothing-else).
