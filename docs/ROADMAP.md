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
CVMFS support, but it is bolted on externally. The bits `--cvmfs-prefix` flag
combined with the bits-console build pipeline produces tarballs with final CVMFS
paths already embedded, which are ingested directly by the CVMFS publisher with no
relocation step. For the O(10⁴) users of CERN experiment software on CVMFS this is
the central value proposition.

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

**Build isolation is opt-in, not the default.** The recipe sandbox (`--sandbox=auto`)
uses `sandbox-exec` on macOS and nested podman under `--docker`, but on a plain local
Linux build it resolves to `off` — podman is only engaged with `--docker` or an explicit
`--sandbox=podman` / `--sandbox-image`. In contrast, Nix achieves true hermetic
isolation unconditionally — no implicit host-library leakage, no ambient `$PATH`
contamination. A bits recipe can accidentally depend on a host library not declared in
its recipe and still build successfully, masking a portability bug. CI pipelines now
use `--sandbox=podman` by default via bits-console, but local developer builds remain
unguarded.

**Reproducibility is strong but not hermetic.** bits achieves reproducibility through
content-addressed tarballs, checksum enforcement, and build manifests. This is
practically solid but does not reach Nix/Guix-level isolation, where the build
environment itself (compiler, libc, every tool in `$PATH`) is hashed and reproducible
from a single root derivation. For most HEP use cases — reproducible physics analysis
on a shared CVMFS installation — bits' model is sufficient. For bit-for-bit
reproducible builds independent of the host OS, Nix/Guix remain stronger.

#### Conclusion

bits is the right tool for anyone deploying large C++/Fortran/CUDA scientific software
stacks to CVMFS via GitLab CI, especially when interactive development with local
package checkouts is part of the workflow. 


## Roadmap

The roadmap is organised into three horizons. Items within each horizon are ordered
by priority (highest first). The primary target audience is CERN experiments and their
O(10⁴) users, plus any project deploying software via CVMFS.
---

### Near term — within 6 months

These are either already partially implemented or require only focused engineering
effort with no architectural changes.

#### N1. Binary store coverage expansion

The shared binary store is the single highest-leverage item for reducing the barrier
to entry for new users. `bits doctor --check-store` (implemented) already tells the
user whether a pre-built tarball is available for their platform before they commit to
a full compilation. The remaining work is supply-side:

- Register build runners for each target architecture in the CERN experiment portfolio
  (`slc9_x86-64`, `slc9_aarch64`, `ubuntu2204_x86-64`, `ubuntu2404_x86-64`,
  `osx_x86-64`, `osx_arm64`).
- Define a `release` build schedule that rebuilds and uploads the full stack on every
  merged commit to the main recipe branch.
- Make pre-built store availability more prominent in the quick-start documentation so
  new users know to expect a fast path before attempting a full build.

#### N2. Developer workflow documentation

The local-checkout shadowing capability is bits' most important differentiator for
interactive development but is the least documented feature. `bits status` (implemented)
already classifies every package in the dependency tree — whether it will come from the
store, be rebuilt, or be shadowed by a local checkout — without running a build.
Remaining work:

- Expand the Cookbook with a realistic multi-package scenario (e.g. developing O2Physics
  with a local O2 also in flight) that shows `bits status` output at each step.
- Improve `bits init` to detect common checkout layouts and offer to configure the
  development environment interactively, reducing the setup steps for a new contributor
  from five commands to one.

---

### Medium term — 6 to 18 months

These require more significant engineering but do not require architectural changes
to the core.

#### M1. Lightweight dependency pinning and version ranges

The current model pins every dependency to an exact version in the recipe. This is
correct for reproducibility but makes it hard to maintain a recipe repository that
serves multiple experiment configurations simultaneously. The planned approach has two
layers:

**Recipe-level constraints** — a `version_range` field on `requires:` entries that
constrains but does not over-specify the version. The resolver uses the most recent
satisfying version in the known recipe set without a full SAT solver, covering the
80 % case (minimum-version constraints, exclusion of known-broken ranges):

```yaml
requires:
  - ROOT:>=6.28
  - boost:>=1.75,<1.84
```

**Defaults profile constraints** — extend the defaults profile system to express a
compatibility matrix that the resolver checks at build time:

```yaml
# defaults-run3-analysis.sh
compatible_with:
  ROOT: ">=6.30"
  Geant4: ">=11.2"
incompatible_with:
  clang: "<16"
```

The build would fail fast with a clear diagnostic if the active recipe repository does
not satisfy these constraints, rather than silently building an incompatible combination
and failing at link or runtime. Both layers share the same resolver extension and can
be implemented together.

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

#### M4. Personal analysis overlay via S3 tarball cache

For individual analysts building a small number of packages on top of a shared
experiment stack, the full CVMFS publication pipeline is too heavyweight: ingestion
latency is minutes, write access to the experiment's Stratum-0 is restricted, and the
overhead is disproportionate for personal packages that iterate daily. Yet batch jobs
on WLCG worker nodes need those packages available before execution with no build
capability on the node.

The solution is a two-layer model: the experiment stack served from CVMFS as always,
and the personal analysis overlay distributed through an S3-compatible object store
(CERN EOS, MinIO, or any AWS-compatible endpoint) that the analyst already has write
access to:

```bash
# After a local build of personal packages:
bits push --manifest s3://cern-eos-personal/pbuncic/analysis-v3.json

# In the WLCG batch job prolog:
bits fetch s3://cern-eos-personal/pbuncic/analysis-v3.json
bits enter MyAnalysis
```

`bits push` uploads only packages built locally (`built_from_source` in the manifest),
skipping anything already on CVMFS. `bits fetch` downloads, verifies SHA-256, and
unpacks them, then exits with a clear diagnostic if the manifest architecture does not
match the executing node. The tarball store already uses a content-addressed layout;
adding an S3 driver is a contained change.

#### M5. ABI constraint exports (`abi_exports`)

When a package is built against a library with a non-stable ABI (OpenSSL, Python C
API, MPI wire protocol, ROOT dictionary), any downstream package built against an
incompatible version will fail at runtime. Allow a recipe to declare the ABI
constraints it propagates:

```yaml
package: openssl
version: "3.3.1"
abi_exports:
  - openssl>=3.0,<4
```

When package B lists `openssl` in its `requires:`, bits automatically adds
`openssl>=3.0,<4` to B's effective constraint set. A binary tarball for B downloaded
from the store is rejected if the installed OpenSSL does not satisfy the constraint,
rather than loading silently and crashing at runtime. The initial target set is the
five or six packages in the HEP stack that are genuine ABI pinch-points: OpenSSL,
Python (C extension API), ROOT (class dictionary), the active MPI implementation, and
the C++ standard library baked in by the compiler toolchain.

---

### Long term — 18 months and beyond

These are higher-risk or higher-effort items whose value justifies the investment
given sustained community growth.

#### L1. Multi-community bits-console with federated stores

The current bits-console is designed for a single GitLab instance and a small set of
communities on the same instance. A federated model allows:

- A community to declare that it trusts tarballs from another community's store
  (e.g. LHCb trusts the LCG store for ROOT, Geant4, and compiler toolchains).
- The local store to serve as a transparent cache in front of upstream stores.
- bits-console to present a unified view of build status across federated communities.

This requires a trust model (public key infrastructure for store signing) and a
canonical resolution order for federated package lookups. The repository provider
feature is a foundation: a community's `defaults` file can already reference another
community's recipe repository.

#### L2. Web-based recipe editor and validation

Lower the barrier to contributing new recipes by providing a browser-based editor
integrated into bits-console that:

- Validates the YAML header syntax as you type.
- Resolves and displays the dependency graph.
- Checks that all `sources:` URLs are reachable and optionally computes their checksums.
- Runs a dry-build (dependency resolution and download only, no compilation) in a
  sandboxed CI job triggered from the browser.

This brings the recipe authoring experience closer to what Spack's `spack create`
and Conda-forge's staged-recipes automation provide, without requiring a local bits
installation.
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

