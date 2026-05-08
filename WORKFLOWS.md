# bits Workflow Guide

This document describes the complete bits development-to-deployment workflow — from a first local build on a developer's workstation through group-wide CI and final publication to CVMFS.

---

## Overview

![bits development-to-deployment workflow](docs/bits-workflow.svg)

The diagram above captures the full picture. The key insight is that **a single, shared toolchain connects every developer's laptop to the experiment's CVMFS software repository**. There is no separate "local build system" and "CI build system". Every phase of a package's lifecycle — from the first `./configure` on your workstation to the CVMFS transaction that makes the software available to thousands of grid jobs — runs the same `bits build` command against the same recipes.

The following sections walk through the workflow phase by phase.

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

## Phase 2 — Local development mode

Suppose you want to modify `mylib`. Clone the source repository next to your recipe checkout:

```bash
# clone the package source alongside the recipe repo
git clone https://github.com/example/mylib.git

# now build — bits detects the local checkout automatically
bits build mylib
```

**How local mode works.** When `bits build` resolves the dependency graph it scans for directories adjacent to (or below) the recipe checkout that have the same name as a package. If it finds one it treats that directory as the source for that package instead of fetching from the upstream remote. All other packages in the graph continue to be resolved from the upstream recipe repository, so `mylib` is compiled against the exact same versions of ROOT, Geant4, and everything else that CI uses. The local override is transparent: there is no flag to pass, no environment variable to set, and no copy of the recipe to edit.

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

---

## Phase 3 — Full-stack local testing

With the local checkout in place you have a full, runnable software stack on your workstation. You can run the experiment software, integration tests, or any interactive workflow:

```bash
bits build ROOT                    # rebuilds ROOT with the updated mylib
bits enter ROOT/latest             # full stack available in a sub-shell
root -b -q 'myAnalysis.C'         # runs against your locally built mylib
```

You can also verify that the package builds correctly inside the same Docker image that CI will use, without leaving your workstation:

```bash
bits build --docker --architecture el9_x86-64 mylib
```

This spins up the official builder container, bind-mounts the current directory (including the local `mylib/` checkout), and runs the build inside it. If this succeeds, CI will succeed for the same reason.

---

## Phase 4 — Commit and share

When local testing passes you push the source changes to the package repository and update the recipe to point at the new commit or tag:

```bash
# In mylib/ — commit and push the source changes
cd mylib
git commit -am "Fix the covariance matrix initialisation"
git push origin my-fix-branch

# Back in alice.bits/ — update the recipe tag/commit
cd ../alice.bits
# Edit mylib.sh: update `tag:` or `commit:` field to the new revision
bits doctor mylib                  # verify the recipe is consistent
git commit -am "mylib: bump to my-fix-branch"
git push
```

Opening a merge request on the recipe repository initiates the standard peer-review cycle. Other developers can check out your recipe branch and run `bits build mylib` to reproduce the build themselves before approving.

---

## Phase 5 — CI build and CVMFS publication via bits-console

Once the recipe MR is merged, publication is driven entirely through **[bits-console](https://bits-console.web.cern.ch)** — a browser-based interface that triggers GitLab CI pipelines on registered build runners. There are no CLI commands to run at this stage: `bits build` and `bits publish` run inside the pipeline, not on your workstation.

The distinction between a group-wide production build and a personal-area build is **not a command-line flag**. It is determined by your role in the project, enforced server-side by the pipeline based on your GitLab identity, and surfaced in the UI as two separate buttons.

### Step 1 — Connect to bits-console

1. Navigate to **[bits-console.web.cern.ch](https://bits-console.web.cern.ch)**.
2. Select your community on the landing page (ALICE, LCG, LHCb, …). Your choice is remembered in the browser.
3. Click **Connect** and paste your GitLab personal access token.
   - Scope `read_api` is enough to browse packages and view CVMFS status.
   - Scope `api` (Developer access) is required to trigger builds.

### Step 2 — Browse and trigger a build

The **Package Browser** lists all packages from the community's configured recipe repos, with current version, build status, and CVMFS publication status. Click any package to open the build modal, then choose a target platform and click one of the two build buttons:

| Button | Who sees it | Where the result lands |
|--------|-------------|------------------------|
| **Build → Production** | `bits-admin` and `group-admin` only | `cvmfs_prefix` configured in the community profile, e.g. `/cvmfs/sft.cern.ch/lcg/releases/ROOT/6.30/x86_64-el9` |
| **Build → Personal area** | All users (`group-admin` and `group-user`) | `cvmfs_user_prefix/<your-username>/…`, e.g. `/cvmfs/sft.cern.ch/lcg/user/jsmith/ROOT/6.30/x86_64-el9` |

The pipeline enforces this independently of the UI. Even a manually crafted API call is rejected unless `GITLAB_USER_LOGIN` matches an entry in the `GROUP_ADMINS_<NAME>` CI variable on the GitLab project. There is no way to publish to the production namespace without the correct role.

### Production builds (group-admin)

Clicking **Build → Production** queues a GitLab CI pipeline that:

1. Runs `bits build --docker` on a registered build runner, with the workDir bind-mounted at the community's `cvmfs_prefix` path inside the container so binaries compile with their final deployment paths embedded.
2. Runs `bits publish --no-relocate` to upload content-addressed tarballs to the shared binary store. No relocation step is needed because the paths were already correct at compile time.
3. Ingests the tarballs via `bits-cvmfs-ingest`, opens a CVMFS transaction, and runs `cvmfs_server publish` on the stratum-0.

The result is available experiment-wide on the **production CVMFS namespace** — to all developers' `bits enter` sessions, to batch grid jobs at WLCG sites, and to downstream CI pipelines. Stratum-1 replicas propagate the change automatically.

### Personal-area builds (group-user)

Clicking **Build → Personal area** triggers the same pipeline, but the build is published to the user's **personal namespace** on CVMFS:

```
cvmfs_user_prefix/<username>/<package>/<version>/<platform>/
e.g.  /cvmfs/sft.cern.ch/lcg/user/jsmith/ROOT/6.30/x86_64-el9
```

This path is completely independent of the production namespace and of the group stack rebuild cycle. Any user with Developer access can publish packages here without waiting for a group-admin to approve a full stack rebuild. It is the natural path for personal builds, patch testing, and hotfixes that need to be shared with a specific analysis team before a production release.

### Step 3 — Monitor progress

The **Builds** tab in bits-console shows a live pipeline list with log streaming. The **CVMFS Status** tab updates once ingestion completes and the stratum-0 transaction is published.

### End-to-end summary

```
Developer workstation        bits-console (browser)           CVMFS
──────────────────────────────────────────────────────────────────────
git clone <mylib-repo>       Select community → sign in
  ↓ edit & test locally      Browse packages → open build modal
bits build mylib                ↓
  ↓ full-stack verified      [Build → Production]          production namespace
git push + recipe MR            → pipeline runs on CI runner  /cvmfs/.../releases/…
  ↓ peer review & merge       ↓ bits build --docker           available to all
                             [Build → Personal area]       personal namespace
                                → same pipeline, user prefix  /cvmfs/.../user/<me>/…
                             Monitor: Builds tab + CVMFS       accessible by you /
                             Status tab                        your analysis team
```

See the [bits-console documentation](repos/bits-console/README.md) for the full role reference, `ui-config.yaml` fields, runner setup, and scheduled build configuration.

---

## Related documentation

| Topic | Location |
|-------|----------|
| Full command reference (`bits build`, `bits publish`, …) | [REFERENCE.md §16](REFERENCE.md#16-command-line-reference) |
| Docker builds and `--cvmfs-prefix` | [REFERENCE.md §22](REFERENCE.md#22-docker-support) |
| Remote binary store backends (S3, HTTP, rsync) | [REFERENCE.md §21](REFERENCE.md#21-remote-binary-store-backends) |
| Repository provider feature | [REFERENCE.md §13](REFERENCE.md#13-repository-provider-feature) |
| CVMFS publishing pipeline & bits-cvmfs-ingest | [REFERENCE.md §26](REFERENCE.md#26-cvmfs-publishing-pipeline) |
| bits-console web interface | [REFERENCE.md §26](REFERENCE.md#bits-console--web-interface-for-the-gitlab-driven-pipeline) |
| Build manifest and `--from-manifest` replay | [REFERENCE.md §25](REFERENCE.md#25-build-manifest) |
| Writing recipes | [REFERENCE.md §17](REFERENCE.md#17-recipe-format-reference) |
