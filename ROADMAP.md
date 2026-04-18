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

#### Honest overall characterisation

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

#### N3. Cross-compilation via QEMU + Docker

Register `qemu-user-static` binfmt handlers on build runners. An ARM64 builder image
then runs transparently on an x86-64 host, allowing a single runner to produce
tarballs for multiple architectures. No changes to bits itself are required; this is
pure runner and CI configuration.

**Actions:**
- Document the QEMU binfmt setup in `INSTALL.txt` under a new "Multi-architecture
  builds" section.
- Add `qemu_targets` field to `ui-config.yaml` to let a community opt into
  cross-architecture CI.
- Test and document the `--cvmfs-prefix` + QEMU combination (the prefix path must
  match the deployment path on CVMFS, which is architecture-specific).

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

#### M5. Environment reproducibility snapshot

Add `bits snapshot` to record the exact loaded environment (package versions,
hashes, and paths) to a file that can be shared and re-instantiated:

```bash
bits snapshot --output my-analysis-env.json
# later, on another machine:
bits restore my-analysis-env.json
```

This is conceptually similar to a `conda env export` / `conda env create` workflow,
targeted at physics analysis reproducibility rather than software stack maintenance.
The snapshot format is a subset of the build manifest.

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
| M5 | `bits snapshot` / environment restore | Medium | Medium | Medium |
| L1 | Federated multi-community store | Long | High | Very high |
| L2 | Incremental / distributed builds | Long | Medium | Very high |
| L3 | Web-based recipe editor | Long | Medium | High |
| L4 | Constraint-aware defaults profiles | Long | High | High |

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
