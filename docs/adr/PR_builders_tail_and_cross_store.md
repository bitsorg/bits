# `--builders` tail utilisation, cross-backend stores, and two robustness fixes

## Summary

Four small, self-contained commits plus two design records. The theme is making
`--builders` builds finish faster and fail louder, and unblocking a real
"recall from CVMFS, publish to S3" workflow:

1. **`feat(builders)`** — the final package now uses the full `-j` instead of the
   per-builder share, since it always builds alone.
2. **`fix(cli)`** — a malformed defaults/recipe can no longer make bits exit
   non-zero with no message.
3. **`fix(sync)`** — `--write-store` (S3) was silently dropped when
   `--remote-store` was `cvmfs://`; freshly-built packages now upload.
4. **`fix(build)`** — the "build order" banner showed `%(version)s` unsubstituted.

Each commit is independent and cherry-pickable. Everything is **non-hashed
build-host policy or display/robustness** — no package hash changes, no forced
rebuild. Full suite: **1148 passed, 2 skipped** (up from 1140; +8 sync tests).

The two ADRs ([0002](adr/0002-moldable-scheduling.md),
[0001-provenance-on-restore](adr/0001-provenance-on-restore.md)) are **Proposed,
no code** — they record the direction the first two fixes point toward.

## What's included

### 1. `feat(builders)`: unleash the final package to full `-j` (`6d64434`)

Under `--builders N`, `effective_jobs` divided every package's `-j` by `N`. But
the final (top-level) target depends transitively on every other package, so it
is always scheduled **last and alone** — dividing its `-j` needlessly starved the
single largest compile of the run (e.g. ROOT getting `-j7` on a 32-core host).

It now builds as if `builders = 1`: the full `-j`, with the `mem_per_job` cap
still applied (and now computed against the full free RAM, since nothing else is
running). New tri-state `--unleash-final` / `--no-unleash-final`, plus a
`system.build_unleash_final` defaults knob; **default on** for `--builders > 1`,
no-op for serial builds. `JOBS` never enters a package hash, so this is
wall-time-only policy.

*Files:* `bits_helpers/args.py`, `bits_helpers/build.py`.

### 2. `fix(cli)`: never exit non-zero without a message (`fec3303`)

A malformed defaults/recipe could make bits exit `1` with no output. Root cause:
a bare `sys.exit()` in the setup path raises `SystemExit`, which is a
`BaseException` and so escaped `bitsBuild`'s `except Exception` (and any logging).
`bitsBuild` now also catches `SystemExit`: for a non-zero code that argparse
didn't already explain it logs an `ERROR` pointing at the likely cause, then
re-raises to preserve the exit code. A companion `parseDefaults` bug
(`dieOnError(err, None, None)` — three args to a two-arg function, `None`
message) is fixed so bad defaults actually report the parse error.

*Files:* `bitsBuild`, `bits_helpers/utilities.py`.

### 3. `fix(sync)`: upload freshly-built packages when reading from CVMFS (`28c6989`)

`bits build ... --remote-store cvmfs:///cvmfs/.../bits/ --write-store b3://bucket`
uploaded nothing. `remote_from_url()` chose the backend from the **read** scheme
only, and the `cvmfs://` branch passed `write_url=None`, so the read-only
`CVMFSRemoteSync` was used and the write store was silently dropped. (A
second-order effect: with no write store visible, packages got a `local`
revision, which the upload site skips — hence fully silent.)

New `DualRemoteSync` pairs a read-only reader with a writer built from the write
URL: reads (`fetch_*`) go to the reader, uploads (`upload_*`) to the writer.
**Only freshly-built packages are uploaded** — a recalled package carries a
non-empty `spec["cachedTarball"]` (for CVMFS, only a synthetic tarball of
symlinks into `/cvmfs`), so uploading it would publish a stub or duplicate an
artifact already on the read store. This matches build.py's own
`built_from_source` vs `from_store` distinction.

Scope is **strict reuse only**; loose-provenance (relaxed) builds remain barred
from any write store by the existing guard. Same-backend stores are unchanged.

*Files:* `bits_helpers/sync.py`, `tests/test_sync.py` (+8 tests).

### 4. `fix(build)`: resolve `%(version)s` in the build-order banner (`882ad22`)

The "Packages will be built in the following order" list printed
`specs[x]["tag"]` verbatim, but per-spec tag expansion runs later, so a templated
tag like `v%(version)s` showed raw. It now resolves the tag for display via
`resolve_spec_data(..., strict=False)` — best-effort: known placeholders expand,
unknown ones are left as-is, and it never aborts (it's only a banner).
Display-only.

*Files:* `bits_helpers/build.py`.

## Design records (Proposed, no code)

- **[ADR-0002 — moldable critical-path scheduling](adr/0002-moldable-scheduling.md).**
  Generalises fix #1: use the per-package timing already recorded in
  `bits_build_stats.json` to order the DAG by critical path and vary `-j` per job
  so `--builders` keeps the machine busy through the tail. Hard constraint:
  stats are trustworthy **only** when collected serially (contention pollutes
  them), so serial mode is the sole calibrator/writer and `--builders` mode is a
  read-only optimizer. Phased: critical-path ordering first (low risk), dynamic
  `-j` at dispatch second (the real win, A/B-gated).

- **[ADR-0001 stage — provenance survives store→restore](adr/0001-provenance-on-restore.md).**
  Surfaced by fix #3: provenance (`pure`/`loose`) is computed at build time and
  never re-read on recall, so contagion breaks across a store→restore boundary.
  Decision: make provenance a property of the artifact, re-read on recall, and
  **weak by default** — strong is earned only by an explicit `provenance: "pure"`
  with a verifiable closure; segregate loose stores; strong publishes refuse
  loose inputs. Only then may relaxed builds write to a (loose) store.

## Docs

`docs/REFERENCE.md` updated: the `--unleash-final` flag and the final-package
`-j` exemption in [Memory- and load-aware parallelism], and a "Mixing a read-only
remote with a separate write store" note in the stores section.

## Testing

- `tests/test_sync.py`: +8 tests — cross-backend dispatch (`cvmfs` read + `b3`
  write → `DualRemoteSync`), read/write routing, the freshly-built upload gate,
  `writeStore` disable propagation, `cvmfs://` write-target rejection, and that
  same-backend / no-write cases are unchanged.
- Full suite: **1148 passed, 2 skipped.**

> **Not yet validated end-to-end:** the `--write-store b3://` upload against a
> real S3 bucket + CVMFS mount (no credentials/mount in CI). The dispatch and
> routing are unit-tested; the upload itself goes through the existing,
> separately-tested Boto3 path. A build-host run is wanted before relying on it.

## Notes for reviewers

- Suggested order: `sync.py` (`remote_from_url` + `_writer_from_url` +
  `DualRemoteSync`) → `build.py` (`effective_jobs` call site + banner) →
  `bitsBuild`/`utilities.py` → the two ADRs.
- The only behavioural change to existing flows is fix #1 (final package `-j`);
  it is default-on but trivially disabled (`--no-unleash-final`) and cannot
  oversubscribe (the package provably runs alone).
