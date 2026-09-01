# bits Workflow Guide

This document describes the complete bits development-to-deployment workflow — from a first local build on a developer's workstation through group-wide CI and final publication to CVMFS.

---

## Overview

```
 WORKSTATION                  RECIPE REPO            CI (bits-console)         CVMFS
 ───────────                  ───────────            ─────────────────         ─────

 [Phase 1 — Setup]
 git clone alice.bits ──────── shared across ────────────────────────────────────────
 bits build ROOT ←──────────── all developers ←── binary store (pre-built tarballs)
 bits enter ROOT/latest

 [Phase 2 — Develop]
 git clone <pkg-source>
 bits status <pkg>            (preview what will rebuild)
 bits build <pkg>             (local checkout detected automatically)
 bits build --docker <pkg>    (validate in the CI image before pushing)
 bits clean                   (reclaim workDir disk space)

 [Phase 3 — Share]
 git push <source branch>
 update recipe tag: ──────────→ MR → peer review → merge
 bits build --write-checksums <pkg>

 [Phase 4 — Publish]                        Build → Production ───→ /cvmfs/.../releases/
                                            Build → Personal   ───→ /cvmfs/.../user/me/

 [Phase 5 — Verify]
 bits verify --from-manifest <manifest.json> --cvmfs-root /cvmfs/alice.cern.ch
```

The key insight is that **a single, shared toolchain connects every developer's laptop to the experiment's CVMFS software repository**. There is no separate "local build system" and "CI build system". Every phase of a package's lifecycle — from the first `./configure` on your workstation to the CVMFS transaction that makes the software available to thousands of grid jobs — runs the same `bits build` command against the same recipes.

---

## Phase 1 — Local setup from shared recipes

Everything starts by cloning the community recipe repository and letting bits build the full software stack for your platform:

```bash
git clone https://github.com/bitsorg/alice.bits.git
cd alice.bits
bits build ROOT        # resolves the dependency graph and builds everything
bits enter ROOT/latest # opens a sub-shell with the environment loaded
```

Bits resolves the complete dependency graph, downloads sources, and compiles in the right topological order. Because the recipe repository is shared across the whole group, every developer and every CI runner starts from the same definition of "the full stack". There is no per-person or per-machine configuration that can silently diverge.

Remote binary stores ([§21 of REFERENCE.md](REFERENCE.md#21-remote-binary-store-backends)) mean that pre-built artifacts are downloaded when available and a local build is only triggered for packages that are missing or have changed. The first `bits build` on a fresh workstation typically takes a few minutes for the last few packages and seconds for everything else.

Multiple recipe repositories can be composed. For example, `alice.bits` builds the ALICE software stack on top of packages from `lcg.bits` (the LHC Computing Grid release stack) and `community.bits` (HEP common libraries). The [repository provider feature](REFERENCE.md#13-repository-provider-feature) lets bits load additional recipe repos dynamically at dependency-resolution time, with no manual configuration required.

---

## Phase 2 — Local development and validation

Suppose you want to modify `mylib`. Clone the source repository next to your recipe checkout:

```bash
git clone https://github.com/example/mylib.git
```

### Preview what will rebuild

Before committing to a build, use `bits status` to see how bits will classify every package in the dependency tree — what comes from the store, what will be rebuilt from source, and what will use your local checkout:

```bash
bits status mylib
```

This runs the full resolver without building anything. Packages already installed show as `already_installed`; those that need compiling show as `build_from_source`; `mylib` itself shows as `local_checkout` because a matching directory was found.

### Build with the local checkout

```bash
bits build mylib
```

When `bits build` resolves the dependency graph it scans for directories adjacent to (or below) the recipe checkout that share a name with a package. If it finds one, it treats that directory as the source for that package instead of fetching from the upstream remote. All other packages in the graph continue to resolve from the recipe repository, so `mylib` is compiled against the exact same versions of ROOT, Geant4, and everything else that CI uses. The local override is transparent: there is no flag to pass, no environment variable to set, and no copy of the recipe to edit.

```
alice.bits/       ← recipe repo (bits looks here for recipes)
mylib/            ← local source checkout (bits detects and uses this automatically)
sw/               ← bits workDir (output of all builds)
  el9_x86-64/
    ROOT/6.30.00/
    Geant4/11.2.0/
    mylib/local/  ← your build, overrides any upstream version
```

Because the build environment — compiler, flags, dependency versions — is identical to CI, a package that builds and passes tests locally will behave the same in CI. **"Works on my machine" is a meaningful guarantee**, not a lucky coincidence.

### Validate in the CI image

Once the local build passes, verify it inside the same Docker image that CI will use:

```bash
bits build --docker --architecture el9_x86-64 mylib
```

This spins up the official builder container, bind-mounts the current directory (including the local `mylib/` checkout), and runs the build inside it. If this succeeds, CI will succeed for the same reason.

You can then run integration tests or an interactive analysis workflow against the full locally-built stack:

```bash
bits enter ROOT/latest
root -b -q 'myAnalysis.C'
exit
```

### Clean up the workDir

After iterating on several packages, the workDir accumulates stale build directories and source mirrors. Reclaim disk space with:

```bash
bits clean            # remove stale BUILD/ directories and TMP/ staging area
bits prune --max-age 14   # evict packages not used in the last 14 days (was `bits cleanup`)
```

---

## Phase 3 — Commit and share

When local testing passes, push the source changes and update the recipe to point at the new commit or tag:

```bash
# In mylib/ — commit and push the source changes
cd mylib
git commit -am "Fix the covariance matrix initialisation"
git push origin my-fix-branch

# Back in alice.bits/ — update the recipe tag/commit
cd ../alice.bits
# Edit mylib.sh: update `tag:` or `commit:` field to the new revision

# Recompute and record checksums for the new source
bits build --write-checksums mylib

bits doctor mylib                  # verify the recipe is consistent
git commit -am "mylib: bump to my-fix-branch"
git push
```

Opening a merge request on the recipe repository initiates the standard peer-review cycle. Other developers can check out your recipe branch and run `bits build mylib` to reproduce the build themselves before approving.

---

## Phase 4 — CI build and CVMFS publication via bits-console

Once the recipe MR is merged, publication is driven entirely through **[bits-console](https://bits-console.web.cern.ch)** — a browser-based interface that triggers GitLab CI pipelines on registered build runners. There are no CLI commands to run at this stage.

The distinction between a group-wide production build and a personal-area build is **not a command-line flag**. It is determined by your role in the project, enforced server-side by the pipeline based on your GitLab identity, and surfaced in the UI as two separate buttons.

### Step 1 — Connect to bits-console

Navigate to **[bits-console.web.cern.ch](https://bits-console.web.cern.ch)**, select your community, and sign in with your GitLab personal access token. See the [bits-console documentation](https://gitlab.cern.ch/bitsorg/bits-console) for required token scopes and role setup.

### Step 2 — Browse and trigger a build

The **Package Browser** lists all packages from the community's configured recipe repos, with current version, build status, and CVMFS publication status. Click any package to open the build modal, then choose a target platform and click one of the two build buttons:

| Button | Who sees it | Where the result lands |
|--------|-------------|------------------------|
| **Build → Production** | `bits-admin` and `group-admin` only | Production CVMFS namespace, e.g. `/cvmfs/sft.cern.ch/lcg/releases/ROOT/6.30/x86_64-el9` |
| **Build → Personal area** | All users with Developer access | Personal CVMFS namespace, e.g. `/cvmfs/sft.cern.ch/lcg/user/jsmith/ROOT/6.30/x86_64-el9` |

The pipeline enforces this independently of the UI. Even a manually crafted API call is rejected unless `GITLAB_USER_LOGIN` matches an entry in the `GROUP_ADMINS_<NAME>` CI variable on the GitLab project.

**Personal-area builds** are the natural path for patch testing and hotfixes that need to be shared with a specific analysis team before a production release. Any user with Developer access can publish here without waiting for a group-admin to approve a full stack rebuild.

### Step 3 — Monitor progress

The **Builds** tab in bits-console shows a live pipeline list with log streaming. The **CVMFS Status** tab updates once ingestion completes and the stratum-0 transaction is published. Production builds become available experiment-wide — to all developers' `bits enter` sessions, to batch grid jobs at WLCG sites, and to downstream CI pipelines. Stratum-1 replicas propagate the change automatically.

### Pipeline internals (for runner admins)

The following is relevant to teams setting up or maintaining build runners, not to developers triggering builds through the UI.

Clicking **Build → Production** queues a GitLab CI pipeline that runs `bits build --docker` on a registered runner, with the workDir bind-mounted at the community's `cvmfs_prefix` path inside the container so binaries compile with their final deployment paths already embedded. The resulting tarballs are then ingested into CVMFS via one of two paths depending on the community's `publish_pipeline` setting:

- **cvmfs-prepub path** (`publish_pipeline: .gitlab/cvmfs-prepub-publish.yml`): the build host POSTs a tar directly to the `cvmfs-prepub` REST API, which handles CAS ingestion and the CVMFS gateway transaction. No dedicated ingest runners are required.
- **Legacy spool path** (`publish_pipeline: .gitlab/cvmfs-publish.yml`): tarballs are spooled to a staging area, a CVMFS transaction is opened on the stratum-0, and `cvmfs_server publish` is run on a dedicated publisher runner. This path is retained for communities that have not yet migrated to cvmfs-prepub.

See the [bits-console repository](https://gitlab.cern.ch/bitsorg/bits-console) for the full `ui-config.yaml` reference, runner registration guide, and scheduled build configuration.

---

## Phase 5 — Verify the deployment

After a production build completes, confirm that what is deployed on CVMFS matches what was built:

```bash
bits verify --from-manifest alice-o2-20260411.json \
            --cvmfs-root /cvmfs/alice.cern.ch
```

`bits verify` reads the SHA-256 digests and provider commit SHAs recorded in the manifest (written automatically to `$WORK_DIR/MANIFESTS/` during the build) and checks them against the live files on the CVMFS mount. Any mismatch is reported with PASS/FAIL/MISS status and a non-zero exit code. This is particularly useful for:

- physics analyses that must demonstrate reproducibility for a journal submission,
- audit trails required by experiment computing boards,
- catching silent divergence when a CVMFS repository is rolled back or hotpatched.

See [§23 bits verify](REFERENCE.md#23-bits-verify--deployment-verification) for full options including `--no-providers` and `--json` output.

---

## End-to-end summary

```
Developer workstation            bits-console (browser)            CVMFS
──────────────────────────────────────────────────────────────────────────
git clone <recipe-repo>          Sign in → select community
bits build ROOT                  Browse packages
bits enter ROOT/latest

git clone <pkg-source>
bits status <pkg>
bits build <pkg>

bits build --docker <pkg>        open build modal
git push + recipe MR ──────────→ merge triggers ──→ [Build → Production] → /releases/…
bits build --write-checksums                       [Build → Personal]   → /user/me/…
                                 Monitor: Builds + CVMFS Status tabs

bits verify --from-manifest …                                            ← confirm ✓
```

---

## Related documentation

| Topic | Location |
|-------|----------|
| Full command reference (`bits build`, `bits status`, `bits verify`, …) | [REFERENCE.md §16](REFERENCE.md#16-command-line-reference) |
| Docker builds and `--cvmfs-prefix` | [REFERENCE.md §22](REFERENCE.md#22-docker-support) |
| Remote binary store backends (S3, HTTP, rsync) | [REFERENCE.md §21](REFERENCE.md#21-remote-binary-store-backends) |
| Repository provider feature | [REFERENCE.md §13](REFERENCE.md#13-repository-provider-feature) |
| CVMFS publishing pipeline | [REFERENCE.md §26](REFERENCE.md#26-cvmfs-publishing-pipeline) |
| Build manifest and `--from-manifest` replay | [REFERENCE.md §25](REFERENCE.md#25-build-manifest) |
| Deployment verification (`bits verify`) | [REFERENCE.md §23](REFERENCE.md#23-bits-verify--deployment-verification) |
| Writing recipes | [REFERENCE.md §17](REFERENCE.md#17-recipe-format-reference) |
| bits-console web interface | [bits-console repository](https://gitlab.cern.ch/bitsorg/bits-console) |
