# Moldable-DAG scheduling: literature review and recommended algorithm

*Companion to [ADR-0002](0002-moldable-scheduling.md). Research notes backing the choice of scheduling algorithm for `--builders`. Status: informational.*

## 1. What this problem is called

Scheduling *n* dependent jobs (a DAG) on *m* identical threads, where each job *i* can be given *k* threads and its runtime `t_i(k)` is known in advance, choosing *k* once per job and fixing it for that job's duration, to minimize the makespan — is **moldable task scheduling under precedence constraints**, written **P|moldable,prec|C_max** (older notation **P|fctn,prec|C_max**). The three relevant job classes (Feitelson & Rudolph 1996; Drozdowski 2009):

- **Rigid** — thread count fixed by the user a priori (`P|size_j|C_max`).
- **Moldable** — the *scheduler* picks the thread count at start, fixed for the run. **This is our case** (`make -j k`, *k* chosen at launch).
- **Malleable** — the count may change *during* execution (`P|var|C_max`).

## 2. Complexity — what "optimal" can mean

The problem is **strongly NP-hard**: it generalizes both rigid parallel-task scheduling (strongly NP-complete already at *m* = 4 — Henning, Jansen, Rau & Schmarje 2020) and ordinary precedence scheduling `P|prec|C_max` (NP-hard — Ullman 1975). There is **no PTAS for general DAGs**, and a hard **3/2 lower bound** on any polynomial approximation (the unit-time, no-precedence case is exactly bin packing). So "optimal" is unattainable in polynomial time; the literature gives **constant-factor approximations** and **practical heuristics**. Best known offline guarantees for moldable/malleable + precedence:

| Result | Ratio | Note |
|---|---|---|
| Lepère–Trystram–Woeginger 2002 | **3+√5 ≈ 5.236** (general DAG); **(3+√5)/2 ≈ 2.618** (series-parallel / bounded width) | first constant factor with precedence |
| Jansen–Zhang 2006 | **≈ 4.73**; **≈ 3.29** (concave model) | LP-rounding allotment |
| Chen 2018 (iterative) | **≈ 3.42** | current best general DAG |
| Graham list scheduling (baseline) | **2 − 1/m** | survives precedence (`P\|prec\|C_max`) |

The take-away: a well-built **list scheduler driven by good per-job thread allotments lands within a small constant of optimal**, and in practice far better than the worst case. Chasing the optimum exactly is not worth it.

## 3. The principle every algorithm uses: two lower bounds

For a chosen allotment `k = (k_i)`, define **work/area** `W_i(k_i) = k_i · t_i(k_i)` (core-seconds). Any schedule's makespan obeys

```
OPT ≥ max(  Σ_i W_i(k_i) / m   ,   longest path of t_i(k_i) through the DAG  )
              └──── AREA ────┘       └──────── CRITICAL PATH ────────┘
```

These two terms *are* the whole design tension:

- **Wide frontier** (many ready jobs, machine saturated) → **area-bound**; give each job **few** threads so many run at once.
- **Narrow frontier** (few ready jobs, cores idle) → **critical-path-bound**; **concentrate** threads on the running job(s).
- **Limiting case**: a single ready job should get **all useful threads** — which is exactly your sink-gets-all-cores requirement (§5).

Every good algorithm is two-phase: **(1) pick the allotment `k_i`** to balance area against critical path, **(2) list-schedule** the DAG (Turek–Wolf–Yu 1992; Ludwig–Tiwari 1994 reduce moldable→rigid this way; Mounié–Rapine–Trystram 2007 give the 3/2 dual-approximation via a two-shelf knapsack). The **monotonic-work assumption** — `t_i(k)` non-increasing, `W_i(k)` non-decreasing in *k* — underlies all the strong results and holds for compilation (more threads never help past a point, and parallel overhead only grows area).

## 4. Recommended algorithm for bits

The directly applicable, implementable family is **CPA → CPR/IAES** (two-phase, area-vs-critical-path), with a **critical-path-priority list scheduler** as phase 2. This is also exactly what **Ninja's production scheduler** does (longest-weighted-path priority, edge weights = last runtime from `.ninja_log`) — and bits already has the equivalent telemetry in `bits_build_stats.json`.

### Cost model from history (calibrated serially — see ADR-0002)
Per package, from a serial run: `W_i` = `cpu_seconds` (work area, ~thread-invariant), `p̄_i` = `peak_threads` (**parallelism ceiling**), `m_i` = `mem_per_thread`. Model `t_i(k) ≈ W_i / min(k, p̄_i)` (optionally `+ d_i` serial tail). The ceiling matters: "all threads" always means **`min(m, p̄_i)`** — beyond `p̄_i` you only add area, never speed (Perotin & Sun 2023, `p_j^max = min(P, p̄_j, √(W_j/c_j))`).

### Phase 1 — allotment (CPA loop, Radulescu & van Gemund 2001)
```
for each job i:  k_i = 1
repeat:
    T_CP = longest path of t_i(k_i) through the DAG          # critical-path bound
    T_A  = ( Σ_i k_i · t_i(k_i) ) / m                        # area bound
    if T_CP <= T_A: break                                    # bounds have met → stop
    pick the job i* on the critical path, with k_i* < min(m, p̄_i*),
        whose +1 thread most reduces t_i*                    # steepest speedup
    k_i* += 1
list-schedule with the k_i fixed (Phase 2)
```
The stop condition `T_CP ≤ T_A` is the heart of CPA: keep funnelling threads into the critical path until shortening it no longer helps relative to the average per-thread work. Cap each `k_i` at `p̄_i` (useful ceiling) and at the memory budget (§6).

*If you can afford it,* replace the analytic stop with **CPR** (Radulescu et al. 2001): tentatively add a thread, **re-run the list-schedule simulation**, and keep the increment only if the *measured* makespan drops. CPR/IAES (Wang et al. 2016) are the best-quality variants in published comparisons (ranking on irregular graphs: CPA < MCPA ≈ MCPA2 < CPR < IAES). With *n* ≈ 1000s this offline simulation is cheap.

### Phase 2 — dispatch (critical-path-priority list scheduling)
Maintain a ready queue ordered by **bottom-level / longest remaining path to the sink** (= Ninja's priority, = HEFT's upward rank). At each event: dispatch the highest-priority ready job whose `k_i` threads (and memory) currently fit; **if cores sit idle and no waiting job needs them, let a running/ready critical job expand toward `min(m, p̄_i)`** ("unleash" — funnel spare cores to the critical path). This is the dynamic generalization of the current `--unleash-final`.

## 5. The sink-gets-all-cores property and the sequential reduction

Your hard requirement — the terminal job gets all *m* threads, and `--builders 1` ≡ sequential DAG execution — is not an ad-hoc rule; it is what the theory says is *optimal* when the frontier is narrow, and it falls out of the algorithm above:

- **The sink is provably alone.** It (transitively) depends on every other job, so it is scheduled last with nothing else running → the dispatcher gives it `min(m, p̄_sink)` threads. (This is exactly today's `--unleash-final`, generalized.)
- **Lone-task ⇒ all cores is optimal.** Prasanna & Musicus (1996), via optimal control of the continuous allocation problem, show that when only one task is active the time-minimizing allocation gives it the **entire** processor share. Perotin & Sun (2023, **Lemma 3**) formalize the discrete online version: whenever utilization is below full, a job on the critical path is always running and the schedule's progress equals that path's per-task times.
- **Clean reduction to sequential.** With *m* = all cores and the "lone ready job takes all cores" rule, a serial chain runs each job at full width, so the makespan is `Σ_i t_i(m)` — plain topological execution at full `-j`. CPA reproduces this automatically: on a chain the critical path *is* the whole graph, so the loop pushes every job to `min(m, p̄_i)` and `T_A` never binds. **Setting `--builders 1` (one ready job at a time) is therefore exactly this degenerate case.**

> **Design caveat.** The *guaranteed* approximations (LTW, Perotin–Sun) cap each job at `μ·m ≤ m/2` threads so multiple jobs always pack — which would *forbid* the sink from taking all cores. We deliberately **drop that cap** in favour of the unleash rule. The cost is only the worst-case guarantee in adversarial wide-then-narrow graphs; CPR/IAES already forgo formal guarantees and win in practice, and the unleash rule is provably optimal precisely in the narrow-frontier regime where it fires. This is the right trade for a build system.

## 6. Memory as a second resource

Treat memory as a second budgeted resource (Perotin et al. 2024, *multi-resource moldable*, algorithm MRSA): at every instant `Σ_running R_i ≤ RAM_budget` and per job `k_i · m_i ≤` headroom, enforced as **admission control** at dispatch (precisely how Bazel's `ResourceManager` gates `{CPU, RAM}` and how bits' `mem_per_job` cap already works). Theory warns the guarantee degrades ~linearly per added resource (Garey & Graham 1975: `(s+2−(2s+1)/m)·OPT` for *s* resources; MRSA: `≈1.619d + 2.545√d + 1` for *d* resources, with a matching lower bound of *d*). Practically: keep the **memory cap authoritative** (an over-commit means OOM/swap, not just slowdown), and let it clamp `k_i` after the area/critical-path allotment.

## 7. Bottom line

There is no tractable exact optimum (strongly NP-hard, no PTAS). The right answer is a **two-phase moldable list scheduler**: a **CPA/CPR allotment** (balance critical-path against area, capped at each job's `p̄_i` and the memory budget) followed by **critical-path-priority list scheduling** with spare cores funnelled to the critical path. It is within a small constant of optimal in theory, best-in-class in practice (CPR/IAES), **reduces cleanly to sequential execution at `--builders 1`**, and is the same design Ninja ships — for which bits already records the necessary per-package history. Phase it as in ADR-0002: critical-path ordering first (cheap, safe), dynamic thread allotment second (the real win), measured against a serial-calibration baseline.

## References

1. Feitelson, Rudolph (1996). *Toward Convergence in Job Schedulers for Parallel Supercomputers.* JSSPP, LNCS 1162. doi:10.1007/BFb0022284
2. Drozdowski (2009). *Scheduling for Parallel Processing*, ch. 5 "Parallel Tasks." Springer. doi:10.1007/978-1-84882-310-5
3. Dutot, Mounié, Trystram (2004). *Scheduling Parallel Tasks: Approximation Algorithms.* Handbook of Scheduling, ch. 26. hal-00003126
4. Graham (1966). *Bounds on Multiprocessing Timing Anomalies.* SIAM J. Appl. Math. 17(2). doi:10.1137/0117039
5. Garey, Graham (1975). *Bounds for Multiprocessor Scheduling with Resource Constraints.* SIAM J. Comput. 4(2):187–200. doi:10.1137/0204015
6. Du, Leung (1989). *Complexity of Scheduling Parallel Task Systems.* SIAM J. Discrete Math. 2(4). doi:10.1137/0402042
7. Henning, Jansen, Rau, Schmarje (2020). *Complexity and Inapproximability Results for Parallel Task Scheduling and Strip Packing.* Theory Comput. Syst. 64(1). arXiv:1705.04587
8. Turek, Wolf, Yu (1992). *Approximate Algorithms for Scheduling Parallelizable Tasks.* SPAA. doi:10.1145/140901.141909
9. Ludwig, Tiwari (1994). *Scheduling Malleable and Nonmalleable Parallel Tasks.* SODA. dl.acm.org/doi/10.5555/314464.314491
10. Mounié, Rapine, Trystram (2007). *A 3/2-Approximation Algorithm for Scheduling Independent Monotonic Malleable Tasks.* SIAM J. Comput. 37(2):401–412. doi:10.1137/S0097539701385995
11. Lepère, Trystram, Woeginger (2002). *Approximation Algorithms for Scheduling Malleable Tasks Under Precedence Constraints.* Int. J. Found. Comput. Sci. 13(4):613–627. doi:10.1142/S0129054102001308
12. Jansen, Zhang (2006). *An Approximation Algorithm for Scheduling Malleable Tasks Under General Precedence Constraints.* ACM Trans. Algorithms 2(3):416–434. doi:10.1145/1159892.1159899
13. Chen (2018). *An Improved Approximation for Scheduling Malleable Tasks with Precedence Constraints via Iterative Method.* IEEE TPDS. doi:10.1109/TPDS.2018.2813387
14. Radulescu, van Gemund (2001). *A Low-Cost Approach towards Mixed Task and Data Parallel Scheduling* (CPA). ICPP. doi:10.1109/ICPP.2001.952047
15. Radulescu, Nicolescu, van Gemund, Jonker (2001). *CPR: Mixed Task and Data Parallel Scheduling for Distributed Systems.* IPDPS. doi:10.1109/IPDPS.2001.924998
16. Bansal, Kumar, Singh (2006). *An improved two-step algorithm…* (MCPA). Parallel Computing 32(10):759–774. doi:10.1016/j.parco.2006.08.004
17. Hunold (2010). *Low-Cost Tuning of Two-Step Algorithms…* (MCPA2). CCGrid. doi:10.1109/CCGRID.2010.52
18. Wang et al. (2016). *An iterative expanding and shrinking process for processor allocation…* (IAES). SpringerPlus 5:1138. doi:10.1186/s40064-016-2808-y
19. Topcuoglu, Hariri, Wu (2002). *Performance-effective and low-complexity task scheduling for heterogeneous computing* (HEFT). IEEE TPDS 13(3):260–274. doi:10.1109/71.993206
20. Benoit, Perotin, Robert, Sun (2022). *Online Scheduling of Moldable Task Graphs under Common Speedup Models.* ICPP (best paper). doi:10.1145/3545008.3545049
21. Perotin, Sun (2023). *Improved Online Scheduling of Moldable Task Graphs under Common Speedup Models.* ACM TOPC 10(4). arXiv:2304.14127, doi:10.1145/3630052
22. Marchal, Simon, Sinnen, Vivien (2018). *Malleable Task-Graph Scheduling with a Practical Speed-Up Model.* IEEE TPDS 29(6):1357–1370. doi:10.1109/TPDS.2018.2793886
23. Prasanna, Musicus (1996). *Generalized Multiprocessor Scheduling and Applications to Matrix Computations.* IEEE TPDS 7(6):650–664. doi:10.1109/71.506703
24. Perotin, Kandaswamy, Sun, Raghavan (2024). *Multi-resource scheduling of moldable workflows.* JPDC 184:104792. doi:10.1016/j.jpdc.2023.104792
25. Marchal, Nagy, Simon, Vivien (2018). *Parallel scheduling of DAGs under memory constraints.* IPDPS. hal-01828312
26. Ninja critical-path scheduler — github.com/ninja-build/ninja PR #2019, #2177; pools: ninja-build.org/manual.html
27. GNU make jobserver — gnu.org/software/make/manual/html_node/POSIX-Jobserver.html
28. Bazel local resources — bazel.build/docs/user-manual; jmmv.dev/2019/12/bazel-local-resources.html
