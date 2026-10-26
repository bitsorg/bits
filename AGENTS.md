# Working on bits

## Repository map

- `bits`, `bitsBuild`, and the compatibility wrappers expose the CLI. Some environment commands have shell fast paths in `bits`; check those as well as Python argument parsing when changing CLI behavior.
- `bits_helpers/build.py` coordinates resolution, reuse, source checkout, scheduling, and final synchronization. The generated package build script comes from `bits_helpers/build_template.sh`.
- `bits_helpers/workarea.py` handles source checkout, extraction, and patches; `bits_helpers/download.py` handles downloads and source caches; `bits_helpers/sync.py` implements store backends.
- `bits_helpers/build_progress.py` owns package labels and human-readable stage reporting. `bits_helpers/progress.py` handles CI progress separately.
- Package recipes normally live in separate `*.bits` repositories. Use `tests/testdist` for local recipe fixtures.
- `console-backend/` is a separate Python project with its own README, dependencies, and tests. Root CLI tests do not validate backend changes.
- User documentation lives in `docs/USERGUIDE.md`, `docs/COOKBOOK.md`, and `docs/REFERENCE.md`; keep examples consistent with actual CLI behavior.

## Development and validation

Support Python 3.8 and newer. Check `pyproject.toml`, `tox.ini`, and `.github/workflows/pr-check.yml` before using newer APIs or changing dependencies. Linux and macOS are both supported.

Use an isolated virtual environment with the project's dependencies installed:

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e . pytest ruff
```

The runtime dependencies include `boto3` and its `botocore` dependency. Missing them can break test collection as well as store tests. An unrelated installed top-level `tests` package can also shadow this repository's test modules in subprocesses; prefer a clean virtual environment when diagnosing `tests.test_provenance` import errors.

Run the full suite before every commit, not only the tests of the code you changed: a shared mock or fixture can break elsewhere. From the repository root:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests
ruff check .
git diff --check
```

The console backend has its own suite, which the root one does not run:

```bash
cd console-backend && PYTHONPATH=..:.:tests python3 -m unittest discover -s tests
```

Disabling pytest plugin autoload keeps unrelated globally installed plugins out of local runs. Tests are primarily `unittest` based; a focused run can also use:

```bash
python -m unittest discover -s tests -p 'test_build_progress.py'
```

CI uses `tox`, which includes unit tests and integration commands that clone recipes and build packages. See `tox.ini` for the appropriate interpreter environment and external requirements. Use a small fixture package for local build smoke checks; check installed content and failure exit codes, not just successful command startup.

Ruff is a check-only gate with a deliberately limited ruleset. Preserve surrounding style, including the existing two-space indentation in many modules. Do not run a repository-wide formatter or blanket automatic fixes as part of an unrelated change. Never run `ruff check --fix` on the tree: it removes imports kept only to probe availability (such as `psutil` in `args.py`); mark those `# noqa: F401`. Inspect generated package metadata after editable installs and keep unintended generated changes out of commits.

## Build correctness

- Changes to build environments, generated initialization scripts, relocation, or installed layouts can affect every artifact. Review `bits_helpers/build_epoch.py`: bump `BUILD_EPOCH` when artifact contents or compatibility change. `tests/test_build_epoch.py` checks the fingerprint of covered files.
- A reporting-only change to a covered file still needs a refreshed `COVERED_SHA256`, but does not require an epoch bump. Compute the fingerprint with `python -c 'from bits_helpers.build_epoch import covered_sha256; print(covered_sha256())'` and document the decision.
- `bits_helpers/build_template.sh` is formatted with Python's `%` operator. Escape literal percent signs as `%%`, preserve the named placeholders, and validate a generated script when changing shell behavior.
- Preserve real recipe exit codes across `tee` pipelines. Read `${PIPESTATUS[0]}` immediately after the pipeline, before another command overwrites it.
- Exercise source builds, cached archive reuse, and incremental builds when changing shared build-script paths. A successful source build alone does not cover all branches.
- Keep raw source architecture separate from effective installation architecture. Architecture-conditional source entries are resolved with the raw architecture; shared package paths may use an effective architecture.
- A plain build (`bits build`, or aliBuild with a local alidist, no new options) must keep its package hashes, build graph and outputs: no forced rebuild. New behavior is opt-in. New keys in manifests, `.meta.json` and other JSON are additive and read with a fallback.
- An option that changes what a recipe produces must change the package hash, for example through the defaults `env:`, which is hashed; when the option is off it adds nothing, so existing hashes stay the same. How the build host runs a build (sandbox, oversubscription) belongs under the defaults `system:`, which is not hashed.
- `bits-recipe-tools` is a build dependency of nearly every package: a change there rebuilds the whole stack. Fix one package in its own recipe.
- Reuse only what the signed store holds and a signed manifest vouches for. Do not add other reuse sources, such as caches a provider points to.
- Shell code runs on Linux and macOS: use what both GNU and BSD tools accept (`sed -i`, `stat`, `date`).
- Code that deletes state (store objects, buckets, spool directories) runs only on an explicit command or option, never as a side effect of another command.

## Progress and source reporting

- Report build progress through `bits_helpers/build_progress.py` and `bits_helpers.log`, so terminal, debug, parallel and redirected output stay consistent; its module docstring describes the stage markers and package numbering, and `tests/test_build_progress.py` covers them.
- Keep the full command output under `--debug`, and report where sources come from at the point they are retrieved, without changing fallback order, checksum checks or redistribution rules.

## Making changes

- Keep it simple: the smallest change that solves the problem, no design for cases nobody has. Code comments are at most three lines.
- Give long-lived things (modules, tables, environment variables, events) names that say what they are; temporary names only for what will be removed.
- One behavior per commit. Before changing what a file, variable or trigger means, find everything that reads it.
- A new test must be seen to fail without the change. Test code where it runs (a container, a privileged process, macOS); if you could not, say so in the commit message.
- Get an independent review of every code change, from a reviewer who sees the diff but not your reasoning.
- Measure before optimizing: `BITS_TIMING=1` times the phases of `bits load`/`enter`; time other paths the same way rather than guessing.
- Commit messages are at most five lines and say why the change was needed. Do not refer to ADRs: they are not kept in this repository.

## Preparing changes for review

A change in behavior updates the user documentation (`docs/USERGUIDE.md`, `docs/COOKBOOK.md`, `docs/REFERENCE.md`) and `CHANGELOG.md`: one entry under Unreleased, tagged **[Feature]**, **[Fix]** or **[Improvement]**, crediting a contribution as "(from #N, by @user)". New first-party source files start with the SPDX header used elsewhere (`GPL-3.0-or-later`, CERN), except the recipe helper snippets bits hashes.

Check the current branch and the requested PR base before publishing. This repository can have substantial work on non-default branches, so compare against the intended base to keep unrelated commits out of a PR. Describe observed behavior, relevant validation, and any environment limits accurately; distinguish illustrative output from captured build output.
