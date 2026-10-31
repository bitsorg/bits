# Find what you need

What people most often want to do with bits, and where the docs explain it. The
[FAQ](FAQ.md) has short answers to more specific questions.

## Getting started

### Install bits and check my machine
Install bits, then let `bits doctor` check the machine and say what is missing.
See: [Installing bits](USERGUIDE.md#installing-bits),
[Building natively: what to install](USERGUIDE.md#building-natively-what-to-install),
[bits doctor](REFERENCE.md#bits-doctor).

### Choose the platform to build for (`--architecture`)
Pass a platform name such as `x86_64-el9`; the compiler and build type come from
`--defaults`. Pick one with pre-built binaries to reuse them.
See: [Before you start: pick the platform to build for](../README.md#before-you-start-pick-the-platform-to-build-for),
[Platforms](USERGUIDE.md#platforms),
[Finding a platform with pre-built binaries](USERGUIDE.md#finding-a-platform-with-pre-built-binaries).

### Build my community's software
Check out your community's recipe repository and build inside it. Each community's
README has the exact commands.
See: [Check out your community's recipe repos](USERGUIDE.md#1-check-out-your-communitys-recipe-repository),
[Check, then build](USERGUIDE.md#2-check-then-build),
[ALICE](https://github.com/bitsorg/alice.bits#getting-started-with-native-bits),
[the LCG stack](https://github.com/bitsorg/stacks.bits#getting-started),
[ATLAS](https://github.com/bitsorg/atlas.bits#readme),
[LHCb](https://github.com/bitsorg/lhcb.bits#readme),
[Key4hep](https://github.com/bitsorg/key4hep.bits#readme),
[SHiP](https://github.com/bitsorg/ship.bits#readme).

### Use what I built (load its environment)
`bits enter` opens a shell with the packages loaded; `bits load` changes the current
shell (with `eval`, or the shell helper); `bits setenv` runs one command.
See: [Use the built environment](COOKBOOK.md#use-the-built-environment),
[Enter a sub-shell with modules loaded](USERGUIDE.md#enter-a-sub-shell-with-modules-loaded),
[Load / unload in the current shell](USERGUIDE.md#load--unload-in-the-current-shell).

### Stop repeating the same options
`bits use` records options such as the platform, defaults and store, so later
commands take them.
See: [Configuration](USERGUIDE.md#4-configuration),
[Config mode — record persistent settings with bits use](REFERENCE.md#config-mode--record-persistent-settings-with-bits-use),
[Where defaults come from](USERGUIDE.md#where-defaults-come-from).

## Building

### See what will be built or downloaded, before building
See: [Previewing a build: --dry-run](USERGUIDE.md#previewing-a-build---dry-run),
[bits status](REFERENCE.md#bits-status),
[Preview what will rebuild](WORKFLOWS.md#preview-what-will-rebuild).

### Understand why a package is rebuilt (or reused)
A package is reused when its hash matches: the hash covers its recipe, sources,
dependencies and settings.
See: [How bits works](USERGUIDE.md#how-bits-works),
[Hashing and reproducibility](REFERENCE.md#hashing-and-reproducibility).

### Build faster, or several packages at once
See: [Building several packages at once](USERGUIDE.md#building-several-packages-at-once),
[Speed up large builds](COOKBOOK.md#speed-up-large-builds),
[Build memory-hungry packages without exhausting RAM](COOKBOOK.md#build-memory-hungry-packages-without-exhausting-ram).

### Build for another Linux, or for ARM (`--docker`)
See: [Building in a container](USERGUIDE.md#building-in-a-container),
[Build for a different OS or architecture (Docker)](COOKBOOK.md#build-for-a-different-os-or-architecture-docker),
[Cross-compilation via QEMU](REFERENCE.md#222-cross-compilation-via-qemu).

### Debug a failed build
The failed build's directory and its full log are kept.
See: [Debug a failed build](COOKBOOK.md#debug-a-failed-build),
[Error handling in recipes — how failures are detected](COOKBOOK.md#error-handling-in-recipes--how-failures-are-detected).

### Work on a package's source, or on a local copy of recipes
See: [Develop and iterate on a single package](COOKBOOK.md#develop-and-iterate-on-a-single-package),
[Build with the local checkout](WORKFLOWS.md#build-with-the-local-checkout),
[Check out a recipe repository and develop against it](COOKBOOK.md#check-out-a-recipe-repository-and-develop-against-it).

### Free disk space
See: [Cleaning up](USERGUIDE.md#7-cleaning-up),
[Evict old packages to free disk space](COOKBOOK.md#evict-old-packages-to-free-disk-space).

## Recipes and defaults

### Write a recipe
A recipe is `<package>.sh`, in lower case: a YAML header, a line with `---`, then the
build script.
See: [Writing Recipes](REFERENCE.md#12-writing-recipes),
[Minimal recipe](REFERENCE.md#minimal-recipe),
[Recipe Format Reference](REFERENCE.md#17-recipe-format-reference),
[Writing Recipes with bits-recipe-tools](COOKBOOK.md#writing-recipes-with-bits-recipe-tools).

### Build another version without editing the recipe
See: [Override a package version without editing the recipe](COOKBOOK.md#override-a-package-version-without-editing-the-recipe),
[Defaults Profiles](REFERENCE.md#18-defaults-profiles).

### Choose compiler and build type (`--defaults`)
Defaults profiles (`defaults-<name>.sh`) are chained with `::`, on top of `release`.
See: [Defaults Profiles](REFERENCE.md#18-defaults-profiles),
[Composing profiles (LCG)](https://github.com/bitsorg/stacks.bits#composing-profiles).

### Choose which experiment's recipes bits uses
Recipe repositories come through repository providers, listed in bits-providers.
See: [How bits uses this repository](https://github.com/bitsorg/bits-providers#how-bits-uses-this-repository),
[Repository Provider Feature](REFERENCE.md#13-repository-provider-feature),
[Defining a repository provider](REFERENCE.md#defining-a-repository-provider).

### Make source downloads reproducible (checksums)
See: [Enforce reproducible source downloads with checksums](COOKBOOK.md#enforce-reproducible-source-downloads-with-checksums),
[Checksum verification](REFERENCE.md#checksum-verification),
[bits checksums](REFERENCE.md#bits-checksums).

## Binary stores

### Reuse pre-built binaries, or share mine
See: [Sharing binaries through a store](USERGUIDE.md#sharing-binaries-through-a-store),
[Set up a project with a persistent binary store](COOKBOOK.md#set-up-a-project-with-a-persistent-binary-store),
[Remote Binary Store Backends](REFERENCE.md#21-remote-binary-store-backends).

### Upload my builds to my own bucket
See: [Share pre-built artifacts over S3](COOKBOOK.md#share-pre-built-artifacts-over-s3),
[Publishing an existing local build to S3](REFERENCE.md#publishing-an-existing-local-build-to-s3-bits-publish).

### Check that downloaded tarballs are not tampered with
See: [Store integrity verification](REFERENCE.md#store-integrity-verification),
[Enabling store integrity verification](REFERENCE.md#enabling-store-integrity-verification).

## CVMFS

### Publish to CVMFS
See: [Publish to CVMFS](USERGUIDE.md#publish-to-cvmfs),
[Find where a package will land on CVMFS](USERGUIDE.md#find-where-a-package-will-land-on-cvmfs),
[CVMFS Publishing Pipeline](REFERENCE.md#26-cvmfs-publishing-pipeline).

### Try publishing without touching production
See: [testbed.bits: trying the overlay locally](https://github.com/bitsorg/testbed.bits#getting-started-trying-the-overlay-locally).

### Check a deployment against its build
See: [Verify a live deployment against a build manifest](COOKBOOK.md#verify-a-live-deployment-against-a-build-manifest),
[Verify the deployment](WORKFLOWS.md#phase-5--verify-the-deployment).

## The console and CI

### Build and publish from bits-console
See: [Connect to bits-console](WORKFLOWS.md#step-1--connect-to-bits-console),
[Browse and trigger a build](WORKFLOWS.md#step-2--browse-and-trigger-a-build),
[Getting access](https://gitlab.cern.ch/buncic/bits-console/-/blob/main/README.md#getting-access).

### Build every night, or on each push
See: [Schedule nightly builds](WORKFLOWS.md#step-4--schedule-nightly-builds),
[Build on each push (CI)](WORKFLOWS.md#step-5--build-on-each-push-ci),
[Setting up a nightly slot](https://gitlab.cern.ch/buncic/bits-console/-/blob/main/README.md#setting-up-a-nightly-slot).

### Find out why a build failed in the console
The console's Failures page and **Explain** on a failed build say what went wrong.
See: [Debug a failed build](COOKBOOK.md#debug-a-failed-build),
[Builds, certification and SBOMs](https://gitlab.cern.ch/buncic/bits-console/-/blob/main/README.md#builds-certification-and-sboms).

### Add or configure a community in the console
See: [Adding a new community](https://gitlab.cern.ch/buncic/bits-console/-/blob/main/README.md#adding-a-new-community-bitsadmins-only),
[Configuring a community](https://gitlab.cern.ch/buncic/bits-console/-/blob/main/README.md#configuring-a-community-group-admins).
