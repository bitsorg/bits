# ADR-0006: Codebase reduction — remove dead and duplicate functionality

Status: **Proposed** — establishes a standing reduction effort and records its
first concrete candidate (`--makeflow`). Individual removals land as their own
reviewed, tested commits referencing this ADR.
Date: 2026-07-23

## Context

bits has accreted overlapping mechanisms as it evolved from its aliBuild
ancestry into the current build/publish tool. Several capabilities now exist in
two forms — one older, one newer and better-tuned — with the old form kept only
for backward compatibility. Each such duplicate is a standing cost: extra code
paths in the hot build loop, extra branches to reason about, a larger
security-review surface, and extra external dependencies to ship in the builder
images.

We want to deliberately shrink the codebase by auditing for (a) **dead code** —
paths no production caller reaches — and (b) **duplicate functionality** — two
implementations of the same capability where one is strictly preferred. This ADR
records the policy and its first worked example so the effort has a durable
anchor; further candidates are appended to the backlog below and removed under
this ADR.

### Removal criteria

A capability is a removal candidate when all of these hold:

1. It is opt-in and **not exercised by any production caller** (the bits-console
   CI pipelines, the recipe repos, or documented operator workflows).
2. A **preferred alternative covers its behaviour** with no capability loss.
3. Removing it **reduces** the maintenance / review / dependency surface.

Anything failing (2) is ported to the preferred path first, or kept.

## Decision

Adopt a periodic dead-code / duplicate-functionality audit, and remove
candidates that meet the criteria above as individual tested commits.

**First candidate: the `--makeflow` parallel-build backend** (and its
makeflow-only companions `--pipeline` and `--makeflow-jobs`), superseded by
`--builders`.

### Why `--makeflow` qualifies

- **Opt-in and unused in production.** `--makeflow` defaults off (`args.py`),
  and no bits-console CI pipeline or recipe passes it — every community build
  runs through `--builders` (`build_parallelism_mode: builders`). `--builders`
  is the actively tuned path (moldable/critical-path scheduling, the nice
  ladder, per-package resource monitoring, container-aware resource detection).
- **No capability is lost.** The two overlaps makeflow provided are already in
  the `--builders` path:
  - *Download overlap* — the scheduler's `download` tasks plus
    `--prefetch-workers` / `--parallel-downloads` compile ready packages while
    others' sources are still fetching.
  - *Upload overlap* — `runBuildCommand` calls `doFinalSync` (tar + upload) at
    the end of each **per-package** scheduler task, so one package's upload runs
    concurrently with other builders' compiles. This is exactly what
    `--pipeline`'s split `.build`/`.tar`/`.upload` targets achieved.
- **Drops an external dependency.** makeflow is a CCTools binary that must be
  installed in every builder image; removing the backend removes that
  requirement.
- **Simplifies the hot path.** The core dispatch in `build.py` branches
  `if not args.makeflow … elif args.makeflow …`; removing makeflow collapses the
  many `if not args.makeflow` guards into the single builders/serial path, and
  deletes the Makeflow-file generation, the `Makeflow.jnj` template, and the
  makeflow-specific injection tests.

### Coupling to remove together

`--pipeline` is makeflow-only ("silently ignored without --makeflow") and
`--makeflow-jobs` (`--max-local N`) configures only the makeflow run, so both go
with `--makeflow`. `--prefetch-workers`, `--parallel-downloads` and
`--parallel-sources` are **not** makeflow-specific (they work in all modes) and
stay.

### Estimated footprint

Source: `build.py` (~38 refs, the dispatch), `args.py` (3 options),
`sync.py`, `checkout_runner.py`, `upload_cmd.py`, `tar_template.sh`,
`workarea.py`, `build_template.sh` (tendrils). Tests: `test_async_build.py`,
`test_security.py`, `test_build.py`, `test_download_sentinels.py`. Docs:
COOKBOOK, REFERENCE, USERGUIDE, and a mention in ADR-0005. Plus the
`Makeflow.jnj` template. Moderate, concentrated in the build dispatch — one
focused commit with the full suite green.

## Consequences

- One parallel-build backend (`--builders`) instead of two; a smaller, more
  auditable build loop and one fewer image dependency.
- `--makeflow`, `--pipeline`, `--makeflow-jobs` disappear from the CLI. Since no
  production caller sets them, no pipeline changes. A stale script passing them
  would error on an unknown flag rather than silently degrade — acceptable, and
  arguably safer than silent no-ops.
- Minor efficiency nuance, not a capability: makeflow's split `.tar`/`.upload`
  could free a build slot *during* a slow upload, whereas `--builders` holds a
  builder slot until its package's upload finishes. Only matters when uploads
  dominate build time; if it ever does, decouple upload into its own scheduler
  task class rather than reviving makeflow.

## Audit backlog (append candidates here)

- [first] `--makeflow` / `--pipeline` / `--makeflow-jobs` — remove (this ADR).
- Arch-resolution: four separate arch-fallback/resolution sites that should
  agree (long-standing) — de-duplicate into one helper.
- (Add further dead-code / duplication findings from the full-codebase pass here,
  each with: what, why it's dead/duplicate, preferred replacement, footprint.)

## Open questions

- **Deprecate-first vs. straight removal.** The conservative norm is to make
  `--makeflow` warn "use --builders" for one release before deleting. Given it
  is internal and absent from CI, a clean removal with a changelog note is
  defensible; pick one at execution time.
- Scope of the broader pass: whole-repo grep for unreferenced functions/flags,
  or a targeted review of the modules most changed recently. To be decided when
  the audit runs.
