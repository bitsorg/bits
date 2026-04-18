# bits — Roadmap

bits is a purpose-built build and distribution system for large scientific software
stacks. Understanding where it stands relative to alternatives is essential for
prioritising future development.

### Landscape

The meaningful comparison set is not general-purpose build tools (CMake, Make, Meson)
but package managers that orchestrate multi-package source builds for scientific
computing:

| System | Primary audience | Core model | Dependency resolution |
|--------|-----------------|------------|-----------------------|
| **Spack** | HPC/scientific | Spec language + variants | `clingo` SAT solver |
| **EasyBuild** | HPC/scientific | Easyconfig files + toolchains | Manual pinning |
| **Nix / NixOS** | General, reproducibility-focused | Purely functional derivations | Closed-form, per-derivation |
| **Guix** | General, reproducibility-focused | Scheme-defined packages | Same as Nix |
| **Conda / Mamba** | Data science / scientific | Binary-first, env-scoped | SAT solver (libmamba) |
| **aliBuild** | ALICE / HEP | Recipe files + tarball store | Manual pinning |
| **lcgcmake** | LCG / CERN | CMake-driven | Manual pinning |
| **bits** | HEP experiments / CVMFS | Recipe files + tarball store + CVMFS pipeline | Manual pinning (version ranges: roadmap) |

### Strengths and weaknesses

#### Where bits leads

**CVMFS-native publishing pipeline.** No other general-purpose build system has a
first-class, integrated publishing path to CVMFS. Spack has community-contributed
CVMFS support, but it is bolted on externally. The bits `--cvmfs-prefix` +
`bits publish` + `bits-cvmfs-ingest` + `cvmfs-publish.sh` stack is a cohesive
end-to-end pipeline from recipe to mounted filesystem. For the O(10⁴) users of CERN
experiment software on CVMFS this is the central value proposition.

**Developer workflow: local checkout shadowing.** The ability to have one or more
locally checked-out packages seamlessly shadow their counterparts in the central
recipe repository — without configuration files or path surgery — is the single most
important differentiator for interactive development. A physicist who checks out
`ROOT` runs `bits build MyAnalysis` and immediately gets their local version picked
up, with all downstream packages rebuilt consistently. This use case was directly
evaluated in Spack and found unworkable in our environment: Spack's development-mode
workflow does not compose naturally with stacks of O(100) interdependent packages, and
the concretiser's full recomputation on every dev-package change makes interactive
iteration too slow for practical use.

**Recipe simplicity.** YAML header + plain bash. A physicist who can write a Makefile
can write a bits recipe without learning a domain-specific language. Spack's spec
language and variant system are powerful but carry a steep learning curve. EasyBuild's
easyconfig format is verbose and structured. Nix and Guix require fluency in
functional languages. bits has the lowest barrier to entry of any system in this set
for scientists writing their own recipes.

**Repository providers.** Pulling a recipe repository dynamically from git — keyed on
a recipe that sets `provides_repository: true` — and merging it into the search path
lets experiment groups maintain independent recipe repositories without forking or
patching a central registry. The bits-providers registry already contains entries for
`alice.bits`, `common.bits`, `lcg.bits`, `lhcb.bits`, and `key4hep.bits`. Spack has a
similar "repos" concept but the activation is more manual and is not part of the
default dependency-graph traversal.

**Standard module file output.** Every bits-installed package produces a
module-compatible environment description that is consumed by `bits q`, `bits enter`,
and `bits print` for interactive environment management. Crucially, these module files
are not bits-specific: once a stack is deployed on CVMFS they can be loaded directly
by standard Environment Modules or Lmod outside of bits, with no bits installation
required on the user's machine. A researcher who `module load ROOT` on an HPC cluster
that mounts the relevant CVMFS repository gets a correctly configured environment
built by bits. This is a genuine bridge to the HPC community that is currently
underappreciated and underdocumented.

**bits-console + GitLab CI pipeline integration.** The browser-based build console
with role-based access control, community-scoped `ui-config.yaml` configuration, and
a triggered build → ingest → publish pipeline is unique in this space. Spack and
EasyBuild assume HPC cluster job schedulers (SLURM, PBS); bits targets GitLab CI
runners, which is where CERN experiments and the broader open-science community
already operate.

#### Where gaps exist

**No dependency constraint solver.** All dependency versions are pinned in recipes.
There is no mechanism for expressing "ROOT >= 6.28" or for automatically resolving
version conflicts across simultaneously active stacks. EasyBuild has the same
limitation. Spack's `clingo`-based concretiser and Conda's SAT solver both handle this
correctly. For large stacks with many independent release lines this is the most
significant technical gap in bits today.

**Package count comparisons are misleading — but ecosystem coverage is still limited.**
Raw recipe counts favour systems that compile every Python package, Perl module, and R
library individually. bits deliberately defers to native binary distribution
mechanisms: a single recipe installs dozens of Python wheels through `pip`, preserving
version coherence without recipe proliferation. Spack's ~8,000 packages and
Conda-forge's ~25,000 are substantially inflated by individual scripting-language
module recipes and by the Cartesian product of compiler × architecture × OS × variant
combinations generating redundant entries. The directly comparable count — compiled,
non-trivial C++/Fortran/CUDA packages — is much closer. That said, outside the CERN
experiment portfolio, recipe coverage is sparse. Growing the shared recipe base is a
prerequisite for broader adoption.

**Binary stores exist but need wider adoption.** bits supports two complementary
binary distribution modes: a local CI store (build once, reuse on subsequent jobs) and
a shared community store (S3-compatible, pre-built tarballs downloadable by any user
on a supported architecture). The technical foundation is complete. The gap is
coverage — which architectures and stacks are pre-built — and discoverability. An end
user who discovers bits and tries `bits build ROOT` on an unsupported architecture
faces a full compilation that takes hours. This perception problem is addressable
without new features.

**Build isolation is opt-in, not the default.** The recipe sandbox (`--sandbox=auto`)
uses rootless podman on Linux and `sandbox-exec` on macOS, but falls back silently to
no isolation when podman is not installed. In contrast, Nix achieves true hermetic
isolation unconditionally — no implicit host-library leakage, no ambient `$PATH`
contamination. A bits recipe can accidentally depend on a host library not declared in
its recipe and still build successfully, masking a portability bug. Making sandboxing
the default for CI pipelines (the controlled environment where it matters most) is
technically straightforward and is on the roadmap.

**Reproducibility is strong but not hermetic.** bits achieves reproducibility through
content-addressed tarballs, checksum enforcement, and build manifests. This is
practically solid but does not reach Nix/Guix-level isolation, where the build
environment itself (compiler, libc, every tool in `$PATH`) is hashed and reproducible
from a single root derivation. For most HEP use cases — reproducible physics analysis
on a shared CVMFS installation — bits' model is sufficient. For bit-for-bit
reproducible builds independent of the host OS, Nix/Guix remain stronger.

**Cross-compilation is not yet documented or automated.** bits has no built-in
cross-compilation toolchain management. However, QEMU user-space emulation combined
with the existing `--docker` mechanism makes cross-architecture builds possible without
any changes to bits itself: registering `qemu-aarch64-static` as a `binfmt` handler on
an x86-64 runner lets a standard ARM64 builder image run transparently. This path needs
documentation and CI configuration support, not new engineering.

**`prefer_system` detection is artisanal.** Using a cluster's vendor-optimised MPI,
BLAS, or CUDA requires a per-recipe detection script written by the recipe author.
There is no standard library of detection snippets and no automated fallback policy
when detection fails. EasyBuild has similar weaknesses; Spack's external packages
mechanism is more systematic.

#### Conclusion

bits is the right tool for anyone deploying large C++/Fortran/CUDA scientific software
stacks to CVMFS via GitLab CI, especially when interactive development with local
package checkouts is part of the workflow. Within that scope it is mature, cohesive,
and not well-matched by any alternative in the comparison set.

Outside that scope — pure HPC without CVMFS, general open-source software distribution,
or environments where end users install from binary packages without a build step —
bits does not compete with Spack (larger ecosystem, better constraint resolution),
Conda (binary-first, data science ecosystem), or Nix/Guix (hermetic reproducibility).
The most meaningful comparison is with **EasyBuild**: similar audience, similar
recipe-file model, similar absence of a constraint solver. bits has better CVMFS and
GitLab tooling; EasyBuild has broader HPC cluster adoption and a larger community
outside CERN.

The primary growth lever is not adding features to match other systems but making the
features that already work — binary stores, sandbox, developer workflow — visible,
documented, and on by default.


---

## Roadmap

The roadmap is organised into three horizons. Items within each horizon are ordered
by priority (highest first). The primary target audience is CERN experiments and their
O(10⁴) users, plus any project deploying software via CVMFS.

---

### Near term — within 6 months

These are either already implemented (pending release) or require only focused
engineering effort with no architectural changes.

#### N1. Aggressive binary store promotion and coverage expansion

The shared binary store is the single highest-leverage item for reducing the barrier
to entry for new users. Currently, pre-built tarballs exist for a handful of
architectures. The goal is to cover every architecture in the CERN experiment portfolio
(`slc9_x86-64`, `slc9_aarch64`, `ubuntu2204_x86-64`, `ubuntu2404_x86-64`,
`osx_x86-64`, `osx_arm64`) for the most-requested stacks.

**Actions:**
- Register build runners for each target architecture attached to the central
  bits-console instance.
- Define a `release` build schedule that rebuilds and uploads the full stack on every
  merged commit to the main recipe branch.
- Add a `bits doctor --check-store` command that tells the user whether a pre-built
  tarball is available for their platform before they commit to a full compilation.
- Document the store configuration more prominently in the quick-start guide.

#### N2. Unconditional sandbox as a recommended default

The `--sandbox=auto` mode is currently advisory and falls back silently to `off` when
podman is unavailable. Flip this for CI environments: the bits-console-generated
pipeline YAML should always pass `--sandbox=podman` when the `bits-build-*` runner has
podman available. Local developer builds keep `auto` as the default.

**Actions:**
- Add a `sandbox` field to `ui-config.yaml` so community maintainers can mandate
  sandboxing for their CI pipelines.
- Document the podman installation requirement in `INSTALL.txt` as a recommended (not
  optional) step for build runners.
- Add a `bits doctor` check that reports podman availability and the effective sandbox
  mode.

#### N3. Cross-compilation via QEMU + Docker ✓ *implemented*

A single x86-64 build runner can now produce tarballs for additional CPU
architectures (aarch64, ppc64le, s390x, riscv64) using QEMU user-mode emulation.
Docker pulls the matching image variant and QEMU transparently executes the foreign
ELF binaries — recipes require no modification.

**What was implemented:**

- `docker_platform_for_arch()` in `bits_helpers/utilities.py`: maps bits
  architecture strings (e.g. `slc9_aarch64`) to Docker `--platform` values
  (e.g. `linux/arm64`).
- `DockerRunner` in `bits_helpers/cmd.py`: accepts a `platform` parameter and
  injects `--platform` into the long-running helper container.
- `finaliseArgs` in `bits_helpers/args.py`: automatically derives and injects the
  platform when the target architecture differs from the host; no manual flag
  required for the common case.
- `--docker-platform PLATFORM` CLI flag: explicit override; `native` suppresses
  automatic injection for runners that are already native.
- Per-package `docker run` build command string in `bits_helpers/build.py`: also
  receives `--platform` when cross-compiling.
- Warning emitted when cross-compiling with `--sandbox` enabled (nested QEMU +
  podman requires `--security-opt seccomp=unconfined` and may fail).
- `INSTALL.txt` §5: full multi-architecture runner setup guide with QEMU binfmt
  registration, builder image verification, and bits-console `qemu_targets` field.
- `REFERENCE.md` §22b and `docs/docs/user.md`: complete cross-compilation
  documentation including architecture mapping table, performance expectations,
  CVMFS prefix interaction, and sandbox caveats.

**Scope and performance note:** QEMU user-mode emulation runs at 20–50 % of native
speed. The intended scope is personal analysis overlays (a few packages, minutes of
build time) and validation builds confirming that a recipe compiles on a target
architecture before scheduling a native-runner CI job for the full stack. Full
experiment stacks (ROOT, Geant4) require a native runner of the target architecture.

#### N4. `bits doctor` hardening

`bits doctor` currently checks system requirements for a given package. Extend it to
verify the full runner environment: store connectivity, podman availability, CVMFS
mount health, GitLab runner registration, and disk space.

**Actions:**
- Add `bits doctor --runner` mode that validates the complete build-runner setup
  against the checklist in `INSTALL.txt`.
- Emit machine-readable JSON (`--output json`) so the bits-console health panel can
  consume and display runner status.

#### N5. Developer workflow documentation and tooling

The local-checkout shadowing capability is bits' most important differentiator for
interactive development but is the least documented feature. Researchers moving from
other build systems will not discover it without explicit guidance.

**Actions:**
- Expand the "Develop and iterate on a single package" cookbook section with a
  realistic multi-package scenario (e.g. modifying O2Physics with a local O2 also in
  development).
- Add `bits status` subcommand: list which packages are being taken from local
  checkouts, which from the binary store, and which will be compiled from source.
- Improve `bits init` to detect common checkout layouts and offer to configure the
  development environment interactively.

---

### Medium term — 6 to 18 months

These require more significant engineering but do not require architectural changes
to the core.

#### M1. Lightweight dependency pinning and version ranges

The current model pins every dependency to an exact version in the recipe. This is
correct for reproducibility but makes it hard to maintain a recipe repository that
serves multiple experiment configurations simultaneously (e.g. ROOT 6.28 for LHCb
Run 3 and ROOT 6.32 for future upgrades).

Introduce a simple `version_range` field on dependencies that constrains but does not
over-specify the version. The resolver uses the most recent satisfying version in
the known recipe set, without requiring a full SAT solver. This covers 80% of the
use cases (minimum-version constraints, exclusion of known-broken ranges) without the
complexity of Spack's concretiser.

```yaml
requires:
  - ROOT:>=6.28
  - boost:>=1.75,<1.84
```

#### M2. `prefer_system` standard library

Replace the current per-recipe artisanal detection scripts with a shared library of
detection snippets for common system-provided components: MPI implementations
(OpenMPI, MPICH, Intel MPI), BLAS/LAPACK (OpenBLAS, MKL, ATLAS), CUDA, HDF5, and
the most common Python packages. A recipe author writes:

```yaml
prefer_system_check: !include system-checks/openmpi.sh
```

rather than writing MPI detection from scratch. This dramatically lowers the quality
bar for `prefer_system` usage on HPC clusters where vendor-optimised libraries are
essential for performance.

#### M3. Reproducible build attestation

The `--from-manifest` replay and `--store-integrity` features provide good
*verification* of stored artefacts but do not provide *attestation* — a signed
statement that a given tarball was produced from a specific recipe at a specific commit
on a trusted runner. Introduce optional SLSA-level 2 provenance attestation:

- The bits-console pipeline signs each completed tarball with the runner's identity
  (a short-lived GitLab CI token or a project-specific signing key).
- `bits build --verify-provenance` checks the signature before using a remote tarball.
- The manifest records the attestation alongside the SHA-256.

This is particularly important for software deployed to CVMFS and used in physics
analyses: a supply-chain attack on the build infrastructure must be detectable.

#### M4. Community onboarding wizard

Reduce time-to-first-build for a new community from days to under two hours. The
`bits init` command (config mode) already writes `bits.rc`; extend it into a guided
setup flow:

```
bits setup-community
```

This command walks through: selecting a base recipe repository, configuring the remote
store, generating a `ui-config.yaml` template, registering the first GitLab runner,
and performing a smoke-build of a simple package to validate the setup end-to-end.
Output is a filled-in `ui-config.yaml` and a `INSTALL.txt`-style setup log.

#### M5. Manifest-driven CVMFS deployment verification

**CVMFS is the primary binary distribution channel for bits.**  Once a package is
built and published to CVMFS via `bits publish`, every authorised user gains
transparent access through the globally distributed SQUID proxy network already
operated by CERN and partner sites.  No additional download infrastructure is
needed: CVMFS handles caching, lazy fetching, and integrity verification at scale,
and does so far more efficiently than any purpose-built binary store could for the
O(10⁴)-user CERN audience.

Introducing a parallel binary cache distribution channel (proxy servers, signed
package registries, P2P stores) would reproduce a subset of CVMFS's capabilities
at significant operational cost with no meaningful benefit for deployments that
already sit inside the CERN network or WLCG.  The local tarball store that bits
maintains in `$WORK_DIR` is appropriate for CI pipelines and individual developer
machines, where it avoids recompilation within a single site — not for
cross-institution software distribution.

What the manifest *does* enable, and what is not yet implemented, is **deployment
verification**: given a manifest produced at build time and a live CVMFS mount,
confirm that every installed package matches the recorded `hash` and
`tarball_sha256`, and that every source was built from the exact `commit_hash`
declared in the manifest.

```bash
# Verify the live CVMFS environment at /cvmfs/alice.cern.ch matches this manifest
bits verify --from-manifest alice-o2-20260411.json --cvmfs-root /cvmfs/alice.cern.ch
```

This is valuable for:
- physics analyses that must demonstrate reproducibility for a journal submission,
- audit trails required by experiment computing boards,
- catching silent divergence when a CVMFS repository is rolled back or hotpatched.

The technical foundation — content-addressed hashes, manifest SHA-256 fields, and
the `source_checksums` now embedded inline — is complete.  The remaining work is
the `bits verify` command and the documentation that frames the manifest as the
bits supply-chain audit record.

#### M6. Personal analysis overlay via S3 tarball cache

For individual analysts building a small number of packages on top of a shared
experiment stack, the full CVMFS publication pipeline is too heavyweight: ingestion
latency is measured in minutes, write access to the experiment's Stratum-0 is
restricted, and the overhead is disproportionate for a handful of personal packages
that may iterate daily.  Yet batch jobs running on WLCG worker nodes need those
packages available before execution, with no build capability on the worker node.

The solution is a two-layer model: the experiment stack is served from CVMFS as
always, and the personal analysis overlay is distributed through an S3-compatible
object store (CERN EOS via S3 API, MinIO, or any AWS-compatible endpoint) that the
analyst already has write access to.

**Workflow**

```bash
# After a local or CI build of the personal analysis packages:
bits push --manifest \
    s3://cern-eos-personal/pbuncic/analysis-v3.json

# In the WLCG batch job prolog (HTCondor, ARC, DIRAC):
bits fetch s3://cern-eos-personal/pbuncic/analysis-v3.json
# Downloads only the personal overlay tarballs, verifies SHA-256 against the
# manifest, unpacks into the local work directory.
# Packages already present on CVMFS are skipped entirely.

bits enter MyAnalysis   # environment is now complete
```

**What gets pushed and fetched**

The manifest's `outcome` field distinguishes packages that were actually compiled
locally (`built_from_source`) from those that were drawn from CVMFS or the shared
store (`already_installed`, `from_store`).  `bits push` uploads only the former —
typically 2–10 tarballs totalling tens to hundreds of MB — together with the
manifest JSON.  `bits fetch` downloads and verifies them on the worker node, then
layers them on top of the CVMFS mount using the same environment-variable mechanism
as `bits enter`.

**Architecture matching is mandatory**

Binary tarballs are not portable: a package built on `slc9_x86-64` against a
specific GCC and glibc version will not run on `slc9_aarch64` or on a node with a
different OS baseline.  The manifest already records `architecture` at the top
level.  `bits fetch` must verify that the manifest's architecture string matches the
executing node before unpacking anything, and abort with a clear diagnostic if it
does not:

```
ERROR: manifest architecture slc9_x86-64 does not match this node (slc9_aarch64).
       Request worker nodes matching the build architecture in your job description.
```

The corollary is that job submission must constrain worker node selection to
architectures that match the build.  In practice this means adding an architecture
requirement to the batch job description:

```
# HTCondor ClassAd
Requirements = (TARGET.OpSysAndVer == "CentOS9") && (TARGET.Arch == "X86_64")

# DIRAC JDL
SystemConfig = x86_64-slc9-gcc13-opt
```

For analysts who need to run on multiple architectures (e.g. `x86-64` for GRID,
`aarch64` for ARM-based opportunistic resources), `bits push` can emit one manifest
and tarball set per architecture if the user has built for multiple targets, and the
job system selects the appropriate manifest via an environment variable or job
parameter.  The N3 roadmap item (QEMU cross-compilation) is the enabling technology
for building the `aarch64` overlay on an `x86-64` developer machine.

**Why this does not compete with CVMFS**

CVMFS serves the stable, shared, heavily-used experiment stack — software that
thousands of analysts use simultaneously and that justifies the publication
overhead.  The S3 overlay serves the fast-moving personal top layer: code that
changes with every analysis iteration and that only a single user or small group
needs.  The two coexist naturally because bits already models environments as
layered package trees; no architectural change is required.  The S3 bucket is
ephemeral and personal — it is not global distribution infrastructure — and access
control is per-bucket, so analysts can share a bucket URL with collaborators or
batch-system job descriptions without any CVMFS repository permissions.

**Implementation**

The tarball store already uses a content-addressed layout on the local filesystem.
Adding an S3 backend is a contained change: a storage driver that reads and writes
`s3://bucket/prefix/<hash>/<tarball>`, with the manifest URL passed as a job
parameter.  The `tarball_sha256` field already embedded in the manifest provides
end-to-end verification with no additional metadata.

#### M7. ABI constraint exports (`abi_exports`)

Borrowed from Conda's `run_exports` concept, this addresses a class of silent runtime
failures that bits currently has no defence against. When a package is built against a
specific version of a library with a non-stable ABI (OpenSSL, Python C API, MPI wire
protocol, ROOT's ABI across major versions), any downstream package built against an
incompatible version will fail at runtime with a hard-to-diagnose symbol or version
mismatch.

Allow a recipe to declare the ABI constraints it propagates to packages that depend
on it:

```yaml
package: openssl
version: "3.3.1"
abi_exports:
  - openssl>=3.0,<4
```

When package B lists `openssl` in its `requires:`, bits automatically adds
`openssl>=3.0,<4` to B's effective constraint set. A binary tarball for B downloaded
from the store is rejected if the installed OpenSSL does not satisfy the constraint,
rather than loading silently and crashing at runtime.

This is particularly important for the shared binary store model: pre-built tarballs
are compiled against specific library versions on the build runner and must not be
served to an environment with incompatible versions. ABI exports make this check
automatic and recipe-driven rather than relying on recipe authors to get version pins
right in every downstream recipe.

The initial target set is small — the five or six packages in the HEP stack that are
genuine ABI pinch-points: OpenSSL, Python (C extension API), ROOT (class dictionary),
the active MPI implementation, and the C++ standard library version baked in by the
compiler toolchain.

#### M8. Shell-function environment activation (`bits activate`)

`bits enter` launches a correctly configured subshell; switching environments requires
exiting it. For users who switch frequently between two or more environments — a common
pattern in analysis work — this is workable but friction-heavy.

Add `bits activate` and `bits deactivate` as shell functions (sourced into the user's
shell, not a subprocess) that modify `PATH`, `LD_LIBRARY_PATH`, and the other
environment variables in place, analogously to `conda activate`. The implementation
sets a `BITS_ACTIVE_ENV` variable so `bits deactivate` knows exactly which variables
to unset or restore.

```bash
source $(bits shell-init)   # once, in .bashrc

bits activate ROOT/6.32.0   # modifies current shell in place
bits activate Geant4/11.2   # stacks on top
bits deactivate             # restores previous state
```

For users on Lmod-enabled systems this is already available through the module files
bits produces — `module load ROOT/6.32.0` does exactly this. `bits activate` provides
the same behaviour for users who are not on Lmod systems, without requiring any new
infrastructure beyond a thin shell wrapper around the existing module file generation.

---

### Long term — 18 months and beyond

These are higher-risk or higher-effort items whose value justifies the investment
given sustained community growth.

#### L1. Multi-community bits-console with federated stores

The current bits-console is designed for a single GitLab instance and a single
community (or a small set of communities on the same instance). As adoption grows
beyond CERN, communities will operate independent GitLab instances with independent
binary stores. A federated model allows:

- A community to declare that it trusts tarballs from another community's store
  (e.g. LHCb trusts the LCG store for ROOT, Geant4, and compiler toolchains).
- The local store to serve as a transparent cache in front of upstream stores.
- bits-console to present a unified view of build status across federated communities.

This requires a trust model (public key infrastructure for store signing) and a
canonical resolution order for federated package lookups. The repository provider
feature is a foundation: a community's `defaults` file can already reference another
community's recipe repository.

#### L2. Incremental and distributed builds

Today, each package is an atomic unit: it is either fully cached (tarball present)
or fully rebuilt from source. For packages with very long build times (LLVM, Geant4),
a finer-grained caching model — caching individual build *steps* (configure, compile,
link) rather than the full package — would dramatically reduce iteration time in
development.

This is architecturally complex because it requires tracking intermediate artefacts
and invalidating them selectively on recipe changes. A pragmatic first step is
**distributed compilation** via `distcc` or `icecc`: the existing `--docker` +
`--makeflow` infrastructure already parallelises across packages; adding compiler
distribution within a package addresses the complementary bottleneck.

#### L3. Web-based recipe editor and validation

Lower the barrier to contributing new recipes by providing a browser-based editor
(integrated into bits-console) that:

- Validates the YAML header syntax as you type.
- Resolves and displays the dependency graph.
- Checks that all `sources:` URLs are reachable and optionally computes their checksums.
- Runs a dry-build (dependency resolution and download only, no compilation) in a
  sandboxed CI job triggered from the browser.

This brings the recipe authoring experience closer to what Spack's `spack create`
and Conda-forge's staged-recipes automation provide, without requiring a local bits
installation.

#### L4. Constraint-aware defaults profiles

Extend the defaults profile system to express build constraints that the resolver
can check — not a full SAT solver, but a curated compatibility matrix:

```yaml
# defaults-run3-analysis.sh
compatible_with:
  ROOT: ">=6.30"
  Geant4: ">=11.2"
  python: ">=3.11"
incompatible_with:
  clang: "<16"
```

The build would fail fast with a clear diagnostic if the active recipe repository
does not satisfy these constraints, rather than silently building an incompatible
combination and failing at link time or runtime. This covers the most important
practical cases without the full generality (and associated complexity) of Spack's
concretiser.

---

## Summary table

| ID | Item | Horizon | Impact | Effort |
|----|------|---------|--------|--------|
| N1 | Binary store coverage and promotion | Near | High | Medium |
| N2 | Sandbox as recommended CI default | Near | High | Low |
| N3 | QEMU cross-compilation | Near | Medium | Low |
| N4 | `bits doctor --runner` | Near | Medium | Low |
| N5 | Developer workflow docs + `bits status` | Near | High | Low |
| M1 | Version ranges on dependencies | Medium | High | Medium |
| M2 | `prefer_system` standard library | Medium | Medium | Medium |
| M3 | Reproducible build attestation (SLSA L2) | Medium | High | High |
| M4 | Community onboarding wizard | Medium | High | Medium |
| M5 | Manifest-driven CVMFS deployment verification (`bits verify`) | Medium | High | Low |
| M6 | Personal analysis overlay via S3 tarball cache (`bits push/fetch`) | Medium | High | Medium |
| M7 | ABI constraint exports (`abi_exports`) | Medium | High | Medium |
| M8 | Shell-function activation (`bits activate`) | Medium | Medium | Low |
| L1 | Federated multi-community store | Long | High | Very high |
| L2 | Incremental / distributed builds | Long | Medium | Very high |
| L3 | Web-based recipe editor | Long | Medium | High |
| L4 | Constraint-aware defaults profiles | Long | High | High |

---

## Collaboration with EasyBuild

EasyBuild and bits are not direct competitors. EasyBuild's primary constituency is
HPC centre administrators at sites like JSC, CSCS, FZJ, and VSC — people who build
software once for a shared cluster and care about toolchain hierarchies, Lmod module
trees, and job scheduler integration. bits' primary constituency is experiment
software coordinators who build stacks for deployment to CVMFS and need interactive
developer workflows. The communities overlap at the intersection of HPC centres that
run CERN experiments and, most concretely, in the **EESSI project** (European
Environment for Scientific Software Installations), which uses EasyBuild to build
software and distributes it on CVMFS — exactly the same deployment mechanism as bits.

### Where bits and EasyBuild are already closer than they appear

**Module files are a shared interface, not a gap.** Every bits-installed package
produces a standard module-compatible environment description used by `bits q`,
`bits enter`, and `bits print`. These files are not bits-specific: once a stack is
deployed on CVMFS they can be loaded directly by Environment Modules or Lmod on any
HPC cluster that mounts the repository, with no bits installation required. A user
who runs `module load ROOT/6.32.0` on an EESSI-enabled cluster can be loading a
bits-built package without knowing or caring. This is a genuine, working bridge to
the EasyBuild/HPC community that should be documented explicitly and promoted as
part of any collaboration discussion. The common assumption that bits requires a
separate module system is incorrect.

### What bits would gain from collaboration

**A `prefer_system` standard library.** EasyBuild has years of accumulated knowledge
about detecting and integrating vendor-provided system packages — MPI implementations
(OpenMPI, MPICH, Intel MPI, Cray MPICH), BLAS/LAPACK (OpenBLAS, MKL, BLIS), CUDA,
HDF5 — across dozens of HPC environments. bits' `prefer_system` detection is
currently written per recipe by each recipe author. Harvesting EasyBuild's external
package detection patterns into a shared snippet library would immediately make bits
more useful on HPC clusters without any new compilation. This is the most immediately
actionable technical benefit and maps to roadmap item M2.

**EESSI as a binary source.** EESSI publishes a curated common software stack on
CVMFS at `/cvmfs/software.eessi.io`. For packages that appear in both the EESSI stack
and a bits community's recipe repository — ROOT, Geant4, Boost, compilers — bits could
optionally treat the EESSI CVMFS installation as a `prefer_system` source. A bits
build on an EESSI-enabled cluster would then download, not compile, the common
infrastructure packages. This is particularly valuable for new communities onboarding
to bits: instead of a multi-hour full compilation, their first build completes quickly
by leaning on EESSI for the foundation.

**HPC centre reach.** EasyBuild has deep institutional relationships at European HPC
centres that are also natural users of CERN software. A bits deployment that integrates
cleanly into an EasyBuild-managed environment — through the existing module file
compatibility and improved `prefer_system` detection — opens a path to those users
that bits cannot reach independently today.

**Recipe knowledge base.** EasyBuild's ~3,000 easyconfigs are not directly usable as
bits recipes (the toolchain model and format differ too much), but they are a
high-quality reference for build flags, patch files, known version incompatibilities,
and configure-time workarounds for the same packages bits recipes also cover. For
packages maintained in both repositories, a lightweight cross-reference would save
recipe authors significant time.

### What EasyBuild would gain

**CVMFS publishing pipeline.** EasyBuild builds software but has no integrated,
automated path from a completed installation to a CVMFS stratum-0 transaction. EESSI
has built this infrastructure independently; the bits `bits publish` +
`bits-cvmfs-ingest` + `cvmfs-publish.sh` stack is a production-tested implementation
of exactly this workflow. HPC centres or communities that want to publish their own
CVMFS repositories — rather than depending solely on EESSI — could use bits' publishing
tooling directly.

**GitLab CI integration and bits-console.** HPC centres that operate GitLab (common
at CERN and many European research institutions) and want a managed build-and-publish
workflow could use bits-console as an orchestration frontend. A hybrid model where
EasyBuild provides the recipe knowledge and build execution, and bits-console provides
the pipeline management, role-based access control, and CVMFS publishing, is
technically straightforward.

**Developer workflow.** The local-checkout shadowing capability — where a developer's
local package revision transparently overrides the recipe-repository version, with all
downstream packages rebuilt consistently — has no equivalent in EasyBuild. Spack's
development-mode workflow was evaluated in the CERN environment and found unworkable
for stacks of O(100) interdependent packages. Contributing this concept to EasyBuild's
development roadmap, even as documentation of the pattern, would benefit EasyBuild
users.

### Where collaboration is harder

**Toolchain model.** EasyBuild's toolchain hierarchy (`foss`, `intel`, `gompi`,
`GCCcore`, ...) is a structured, versioned graph of compiler and MPI combinations
against which every package is built. bits' approach is simpler: a defaults profile
applies version overrides to a package set. These philosophies are different enough
that a common recipe format is not achievable. Collaboration at this level means bits
adopting EasyBuild toolchain *naming conventions* for cross-referencing, not
integrating the machinery.

**Python package strategy.** EasyBuild compiles Python extensions from source against
the toolchain's Python. bits uses pip with native wheels. Both approaches have merits
and neither community is likely to change its strategy. The practical resolution is
the one bits already applies: coarse-grained recipes that install a coherent set of
Python packages via pip, rather than individual compiled recipes.

**Community governance.** EasyBuild is governed by a consortium of HPC centres with a
formal release process and a large reviewer pool. Any technical collaboration requires
agreeing on whose decisions prevail when community priorities diverge. This is a
social and governance question as much as a technical one.

### Concrete near-term steps

1. **Document module file compatibility explicitly.** Add a section to both the
   `REFERENCE.md` and the bits-console `INSTALL.txt` explaining that bits-generated
   module files work with standard Environment Modules / Lmod installations and are
   deployed to CVMFS as a first-class output, not an implementation detail. This costs
   nothing to implement and directly addresses the most common misconception about bits
   in the HPC community.

2. **EESSI `prefer_system` integration.** Add EESSI CVMFS paths to the `prefer_system`
   search logic for packages that EESSI provides. This maps directly to roadmap item
   M2 and requires agreement with the EESSI infrastructure team on a stable API for
   querying available packages and their CVMFS paths.

3. **`prefer_system` snippet library drawing on EasyBuild conventions.** Harvest
   EasyBuild's external packages documentation and detection patterns. Invite EasyBuild
   community members to contribute snippets. This is roadmap item M2 with an explicit
   upstream attribution and collaboration channel.

4. **Joint CVMFS publishing documentation.** Co-author a guide with the EESSI
   infrastructure team on running `bits-cvmfs-ingest` and `cvmfs-publish.sh` for
   communities that want their own CVMFS repository alongside or independently of
   EESSI. Primarily documentation and relationship-building; no new engineering.

5. **Cross-list packages.** For packages maintained in both repositories, establish a
   lightweight process for sharing build knowledge — patch files, configure flags,
   known version incompatibilities — without attempting to unify recipe formats.

---

## What bits is not trying to become

To keep focus, it is worth being explicit about scope boundaries:

- **Not a general-purpose Linux package manager.** System libraries, kernel modules,
  and distribution-level packages are not in scope. `prefer_system` is the correct
  answer for those.
- **Not a replacement for pip/conda for pure Python environments.** Python packages
  that do not require compilation against experiment-specific libraries belong in pip
  or Conda. bits recipes for Python components should remain coarse-grained (a single
  recipe that installs a coherent set of analysis packages via pip).
- **Not a build *tool*.** bits orchestrates *when* and *in what order* CMake, Meson,
  autotools, or other build systems run. It does not replace them.
- **Not a job scheduler.** On HPC clusters, bits builds run as GitLab CI jobs or
  interactively. Batch submission to SLURM/PBS is out of scope; the
  `--makeflow` integration covers intra-build parallelism.
