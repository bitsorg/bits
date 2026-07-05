# ADR-0002: History-driven, moldable critical-path scheduling for `--builders`

**Status:** Proposed
**Date:** 2026-06-10
**Deciders:** Predrag Buncic; bits maintainers
**Related:** [ADR-0001](0001-cvmfs-relaxed-reuse.md) (build_id as a coherence token); `--unleash-final` (a static special case of this — see below); `--build-nice` ladder; container resource detection.

## Context

On a multi-core host, `bits build --builders N` keeps the pipeline full in the
middle but wastes cores at the **tail** (and at startup and at dependency
bottlenecks): whenever the ready set drains below `N`, the running jobs still get
only their static per-builder `-j` share (`ceil(jobs / N)`), so the single
largest compile can finish on a fraction of the machine. `--unleash-final`
patches the very last package; it does not address the general drain.

The telemetry to do better **already exists but is not used for scheduling**:

- The resource monitor samples every package at 1 Hz; `build_stats.py` writes
  per-package `{cpu, rss, time}` to `bits_build_stats.json`, and `stats.py`
  derives `cpu_seconds`, `peak_threads`, `mem_per_thread`, `duration` per trace.
- `autoload_stats_path()` reloads the file on the next run and `ResourceManager`
  consumes it — but only for greedy "does this job fit right now" RSS/CPU
  packing, never for DAG ordering or thread allocation.
- The scheduler's dispatch priority is `100000 - spec.requiredBy`, but
  `requiredBy` is **never set** on build specs, so today every ready job has
  equal priority and dispatch order is effectively arbitrary.
- `JOBS` (`-j`) is **baked into the build command string at spec-construction
  time** (`build.py`), so it cannot vary per job at dispatch.

**Key correctness constraint (the reason this ADR exists in two halves):** a
job's *clean* cost signal is only observable when nothing else competes for CPU
or memory. Under `--builders`, concurrent jobs contend, so measured wall time and
CPU% per job are polluted and unreliable. Therefore statistics can only be
trusted when collected in **serial mode** (`--builders 1`, no oversubscription),
and `--builders` mode must treat the stats file as **read-only input**.

## Decision

Split the system into a **calibrator** and an **optimizer**, sharing one
on-disk model (`bits_build_stats.json`):

1. **Serial mode is the sole calibrator and the sole writer.** A `--builders 1`
   run (with resource monitoring on, ideally at `-j = ncpu`) is the only context
   that records or updates per-package statistics. In `--builders > 1` mode,
   stats are read but **never written** — contention-polluted numbers must not
   overwrite clean ones. (Today stats are written regardless; this gate is new.)

2. **`--builders` mode is a moldable, critical-path-aware optimizer.** Using the
   read-only model it chooses, at each scheduling tick, *how many* jobs run
   concurrently and *how many threads* each gets, to **minimize total wall time
   (makespan)** subject to: keep all cores busy, never exceed the memory budget,
   and preserve the responsiveness/`nice` ladder. When no history exists for a
   package, fall back to the current static behaviour (median defaults already in
   the stats file) — cold start must never be worse than today.

### Cost model (grounded in serially-observed quantities)

History gives ~one observation per package, not a speedup curve, so the model is
deliberately two-parameter and recoverable from a single serial run:

- **Work** `W_i = cpu_seconds_i` — the core-seconds integral; ~invariant to `-j`.
- **Parallelism ceiling** `p_i = peak_threads_i` (or `ceil(avg_cpu/100)`) — beyond
  this the job cannot use more cores.
- **Memory** `m_i = mem_per_thread_i`, `R_i = peak_rss_i`.

Predicted wall at `n` threads: `d_i(n) ≈ W_i / min(n, p_i) (+ optional serial
tail)`. A serial run at `-j = ncpu` observes all of `W_i, p_i, m_i` directly and
self-consistently (`duration ≈ W_i / p_i` when `p_i < ncpu`).

### Optimizer, concretely

At each tick, over the ready set:

1. Weight each node by `d_i` and compute the **longest remaining path to the
   sink** (critical path, in time). Order candidates by greatest remaining path.
2. Admit jobs and assign threads to fill `ncpu`, **capping each job at `p_i`**
   (no point giving ROOT 32 threads it can't use) and at the memory bound
   (`Σ R_i ≤ budget`, `n_i · m_i ≤ headroom`), biased toward critical-path jobs;
   spare cores left by capped jobs backfill to whoever can use them.
3. Apply the existing `nice` ladder for responsiveness when oversubscribed.

This **subsumes both current knobs**: `--unleash-final` is the degenerate
tail case (sink alone, on the critical path, gets all usable cores), and
`--oversubscribe` becomes unnecessary because idle-core backfill is computed
rather than guessed.

## Options Considered

### Option A: Critical-path ordering only (no `-j` change)
Set `requiredBy`/critical-path weight from recalled durations and feed the
scheduler's existing priority. Keep static per-builder `-j`.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| Risk | Low (ordering only; output unchanged) |
| Payoff | Starts long poles earlier; partial tail relief |
| Refactor | None to the `JOBS` path |

**Pros:** small, safe, ships behind a flag, fixes the dead priority hook.
**Cons:** does not fill idle cores during drains — the core waste remains.

### Option B: Full moldable allocator (dynamic `-j` at dispatch)
Template `JOBS` instead of baking it; allocate threads per the model above.

| Dimension | Assessment |
|-----------|------------|
| Complexity | High |
| Risk | Medium (touches dispatch + command construction) |
| Payoff | The real makespan win; keeps CPU saturated |
| Refactor | `JOBS` must move from spec-construction to dispatch |

**Pros:** delivers the stated goal. **Cons:** the `JOBS`-templating refactor is
the riskiest change; needs A/B validation against Option A.

### Option C: Closed-loop autotune
On top of B: auto-apply (`--auto-resources` already exists), add the serial-tail
term, and self-correct from each new serial calibration.
**Pros:** hands-off. **Cons:** premature before B is measured; risk of feedback
instability. Defer.

### Rejected alternative: collect stats in `--builders` mode
Tempting (no separate calibration run), but contention makes per-job wall/CPU
unreliable, which would poison the model. **Serial-only calibration is a hard
rule, not a convenience.**

## Trade-off Analysis

The makespan win lives almost entirely in Option B (filling drains), but B is
also the only change that alters the live build's resource profile, so it must be
gated and A/B-measured. Option A is cheap insurance that helps on its own and is
a prerequisite (the critical-path weights B needs). The serial/parallel split
adds one operational requirement — a calibration run per stack per machine class
— but that cost is amortized across every subsequent `--builders` build and is
the price of a trustworthy model. `JOBS` never feeds a package hash, so all of
this is **non-hashed build-host policy**: safe to iterate, A/B, and ship
default-off.

## Consequences

**Easier:** faster whole-stack builds on big hosts; `--oversubscribe` and
`--unleash-final` become emergent rather than hand-tuned; the existing telemetry
finally pays for itself.

**Harder / to revisit:**
- Operational model now distinguishes "calibration build" (serial) from
  "production build" (`--builders`); docs and CI must reflect it.
- Cold-start / new-package handling needs a defined fallback (median defaults).
- The `JOBS`-at-dispatch refactor changes a long-stable code path.

**Open decisions (need a call before Phase 2):**
1. **Calibration ergonomics** — opportunistic (any `--builders 1` run updates
   stats) vs. an explicit `--calibrate` convenience that forces serial +
   monitoring at `-j = ncpu`. Recommendation: opportunistic, plus the flag.
2. **Write-gate exact condition** — `builders == 1` only, or also require
   `oversubscribe == 1.0` and `nice`-ladder off? Recommendation: all three.
3. **Cold-start policy in `--builders`** — treat unknown packages as median
   (neutral) or as critical (pessimistic, front-load)? Recommendation: median.
4. **Responsiveness mechanism** — keep the `nice` ladder as-is, or reserve a core
   / cap utilisation fraction? Recommendation: keep the ladder; add a utilisation
   cap only if interactivity suffers.

## Action Items

1. [ ] **Phase 1 (Option A):** compute critical-path weights from
   `bits_build_stats.json`; set `requiredBy`/priority in the scheduler; fix the
   dead priority hook; topological fallback on no-history. Behind a flag.
2. [ ] **Calibration gate:** only write/update stats when `builders == 1` (+ the
   conditions from open-decision #2). Add `--calibrate` convenience if chosen.
3. [ ] **Makespan harness:** record before/after wall time on a ROOT-sized stack
   (BITS_TIMING + `bits_build_stats.json` diffs) to justify Phase 2.
4. [ ] **Phase 2 (Option B):** move `JOBS` to dispatch-time templating; implement
   the thread allocator (ceiling + memory + critical-path bias + backfill).
5. [ ] **Phase 2 validation:** A/B against Phase 1; ship default-off, promote on
   evidence.
6. [ ] **Phase 3 (Option C):** auto-apply + serial-tail refinement, only if 4
   measures out.
