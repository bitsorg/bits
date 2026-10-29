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
                                            Nightly slot       ───→ /cvmfs/.../nightlies/<day>/
 git push <recipe or source branch> ──────→ CI build of the slot (store only)

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

Navigate to **[bits-console.web.cern.ch](https://bits-console.web.cern.ch)**, select your community, and sign in with your GitLab personal access token. See the [bits-console documentation](https://gitlab.cern.ch/buncic/bits-console) for required token scopes and role setup.

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

### Step 4 — Schedule nightly builds

A nightly build is a **slot**: a build defined once, run by the orchestrator on
given days at a given time, and approved in advance. Slots are on the console's
**Schedules** tab; everyone signed in sees them, the community's admins make,
change and approve them (a certifying slot: admins of every group it certifies
for).

| Field | What it sets |
|-------|--------------|
| Package, Version | What is built (a blank version: the recipe's). |
| Defaults chain | How it is built and **where its builds go**. With stacks.bits' `dev3`, builds go to `…/nightlies/dev3/{day}/…`; the orchestrator gives each build its night's weekday as `{day}`, so a build that starts after midnight still lands in its night's directory. Blank: `release`. |
| Variants | Defaults chains put before it, one a line: `gcc15::opt` and `clang::opt` with `dev3` build `gcc15::opt::dev3` and `clang::opt::dev3`. |
| Platforms | Each platform × each variant is one build of the night, a *cell* (64 at most). |
| Variables, Extra recipe repositories, Extra build arguments, Jobs | As in the Build dialog: `--set` (e.g. `release=LCG_110`), more recipe repositories, more `bits build` options (not `--defaults`), `-j`. |
| Days, Time | When it runs, in the instance's time zone (shown beside the time). |
| Publish to CVMFS, Certify, Groups it certifies for | Where the result goes after the build. |
| Build on each commit to | The branches it also builds on, see Step 5. |

To set one up:

1. On **Schedules**, click **+ New slot**, fill it in and **Save**. Or set up a
   build in the Build dialog (package, platforms, defaults) and click
   **💾 Save as pipeline**: a full platforms × defaults matrix (2 to 16 chains)
   becomes one slot with the chains as its variants, any other one slot per
   chain. It is saved paused, every day at 02:00, without variables or followed
   branches: **Edit** it first.
2. If it certifies, click **Approve**, then **Use your passkey**. The approval
   covers 30 nights of every cell, but 100 builds at most (fewer nights for a
   slot of more than 3 cells), or 30 days, whichever ends first; **▶ Run now**
   uses it up too. A night it cannot cover whole, and a slot without one, builds
   but certifies nothing. A change of what it builds or where needs a new
   approval; a new time, days or followed branches do not.
3. **Resume** a slot saved paused.

At its time the orchestrator queues a build per cell, and they start as build
hosts become free. A night missed by less than 6 hours still runs; a cell whose
last build still runs skips the night; a build that finds no free host for 12
hours is not run. **▶ Run now** builds every cell not still running, as
today's night; **Pause**, **Edit** and **✕** (delete) do what they say.

Each night's results are in the nightly matrix, on the console's **Nightlies**
page (14 nights; a build opens its log) and the phone app's **Nightlies** tab
(7): a row per slot, a cell per platform, variant and night (queued, running,
passed, failed and where, or not run and why). A slot's page in the app has
**Run again** for the failed cells of its last night.

### Step 5 — Build on each push (CI)

A slot can also build whenever a branch it follows gets a new commit. In its
**Build on each commit to**, list up to 8 `https://host/path.git@branch` entries,
separated by `;`: the recipe repository at its release branch, and the sources of
packages built from git, e.g. `https://github.com/root-project/root.git@master`.
The list only decides *when* the slot builds: the build takes its recipes and
versions as a nightly does, so following a source branch helps only when the
slot's recipes build that branch (as dev3 builds ROOT `master`).
*Follow its recipe repositories* replaces the list with the community's recipe
repository at the branch the slot's `release` variable names (else `main`), and
those of its extra recipe repositories that name a branch.

- The orchestrator reads each branch's head every 2 minutes: no webhook and no
  token in the repository. Only public repositories can be followed.
- A branch just added is not built; its next commit is. A paused slot follows
  nothing, and after **Resume** the heads it finds count as just added.
- Pushes come in bursts, so it waits until the branches have not moved for
  5 minutes (30 minutes at most after the first new commit), then builds every
  cell of the slot once for the burst.
- A slot runs one CI build at a time. Commits pushed meanwhile are built
  together when it ends, and nothing is cancelled. Nightlies and CI builds do
  not wait for each other.
- A CI build goes to the binary store only: it is never certified nor published
  to CVMFS. It shows whether the branches build.
- Each build fetches its recipes and sources when it starts, so it may hold
  commits newer than the heads recorded when it fired. A commit that changes no package's hash still makes a short
  pipeline, which finds everything built.

The Schedules tab shows each followed branch's head, marked *new: builds soon*
until it is built. CI builds have a matrix of their own, apart from the
nightlies: the console's **CI builds** page shows each such slot's latest 10, a
column per build with the heads when it fired and a row per cell, and the phone
app's **CI** tab the same, its latest 7.

**For operators:** slots need the orchestrator's build queue
(`BITS_ORCH_QUEUE=1`). The instance's time zone is the site setting
`console.time_zone`; `BITS_ORCH_WATCH_S` (120 s, at least 30) sets how often
heads are read, and the worker must reach the repositories' hosts over HTTPS.
See *Nightly schedules* and *Commit CI* in `console-backend/README.md`.

### Pipeline internals (for runner admins)

The following is relevant to teams setting up or maintaining build runners, not to developers triggering builds through the UI.

Clicking **Build → Production** queues the build; with the orchestrator's build queue on, it waits there (not in GitLab) for a free build host, and then starts a GitLab CI pipeline pinned to that host. The pipeline runs `bits build --docker` on a registered runner, with the workDir bind-mounted at the community's `cvmfs_prefix` path inside the container so binaries compile with their final deployment paths already embedded. The resulting tarballs are then ingested into CVMFS via one of two paths depending on the community's `publish_pipeline` setting:

- **cvmfs-prepub path** (`publish_pipeline: .gitlab/cvmfs-prepub-publish.yml`): the build host POSTs a tar directly to the `cvmfs-prepub` REST API, which handles CAS ingestion and the CVMFS gateway transaction. No dedicated ingest runners are required.
- **Legacy spool path** (`publish_pipeline: .gitlab/cvmfs-publish.yml`): tarballs are spooled to a staging area, a CVMFS transaction is opened on the stratum-0, and `cvmfs_server publish` is run on a dedicated publisher runner. This path is retained for communities that have not yet migrated to cvmfs-prepub.

See the [bits-console repository](https://gitlab.cern.ch/buncic/bits-console) for the full `ui-config.yaml` reference, runner registration guide, and scheduled build configuration.

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
                                 Schedules: a slot ──────────────→ nightlies/<day>/…
git push (a followed branch) ──→ the slot's CI build ─→ store only

bits verify --from-manifest …                                            ← confirm ✓
```

---

## Related documentation

| Topic | Location |
|-------|----------|
| Nightly slots and CI builds (the orchestrator) | `console-backend/README.md`: *Nightly schedules*, *Commit CI* |
| Full command reference (`bits build`, `bits status`, `bits verify`, …) | [REFERENCE.md §16](REFERENCE.md#16-command-line-reference) |
| Docker builds and `--cvmfs-prefix` | [REFERENCE.md §22](REFERENCE.md#22-docker-support) |
| Remote binary store backends (S3, HTTP, rsync) | [REFERENCE.md §21](REFERENCE.md#21-remote-binary-store-backends) |
| Repository provider feature | [REFERENCE.md §13](REFERENCE.md#13-repository-provider-feature) |
| CVMFS publishing pipeline | [REFERENCE.md §26](REFERENCE.md#26-cvmfs-publishing-pipeline) |
| Build manifest and `--from-manifest` replay | [REFERENCE.md §25](REFERENCE.md#25-build-manifest) |
| Deployment verification (`bits verify`) | [REFERENCE.md §23](REFERENCE.md#23-bits-verify--deployment-verification) |
| Writing recipes | [REFERENCE.md §17](REFERENCE.md#17-recipe-format-reference) |
| bits-console web interface | [bits-console repository](https://gitlab.cern.ch/buncic/bits-console) |
