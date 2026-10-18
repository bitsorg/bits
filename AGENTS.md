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

Useful checks from the repository root:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q tests
ruff check .
git diff --check
```

Disabling pytest plugin autoload keeps unrelated globally installed plugins out of local runs. Tests are primarily `unittest` based; a focused run can also use:

```bash
python -m unittest discover -s tests -p 'test_build_progress.py'
```

CI uses `tox`, which includes unit tests and integration commands that clone recipes and build packages. See `tox.ini` for the appropriate interpreter environment and external requirements. Use a small fixture package for local build smoke checks; check installed content and failure exit codes, not just successful command startup.

Ruff is a check-only gate with a deliberately limited ruleset. Preserve surrounding style, including the existing two-space indentation in many modules. Do not run a repository-wide formatter or blanket automatic fixes as part of an unrelated change. Inspect generated package metadata after editable installs and keep unintended generated changes out of commits.

## Build correctness

- Changes to build environments, generated initialization scripts, relocation, or installed layouts can affect every artifact. Review `bits_helpers/build_epoch.py`: bump `BUILD_EPOCH` when artifact contents or compatibility change. `tests/test_build_epoch.py` checks the fingerprint of covered files.
- A reporting-only change to a covered file still needs a refreshed `COVERED_SHA256`, but does not require an epoch bump. Compute the fingerprint with `python -c 'from bits_helpers.build_epoch import covered_sha256; print(covered_sha256())'` and document the decision.
- `bits_helpers/build_template.sh` is formatted with Python's `%` operator. Escape literal percent signs as `%%`, preserve the named placeholders, and validate a generated script when changing shell behavior.
- Preserve real recipe exit codes across `tee` pipelines. Read `${PIPESTATUS[0]}` immediately after the pipeline, before another command overwrites it.
- Exercise source builds, cached archive reuse, and incremental builds when changing shared build-script paths. A successful source build alone does not cover all branches.
- Keep raw source architecture separate from effective installation architecture. Architecture-conditional source entries are resolved with the raw architecture; shared package paths may use an effective architecture.

## Progress and source reporting

- Package counters follow the selected dependency plan, include reused packages, and exclude `defaults-release`. Apply numbering after selections such as `--only-deps`. Parallel messages can arrive out of plan order.
- Use `bits_helpers.log` and the existing progress callbacks so terminal, debug, parallel, and redirected output remain consistent. Serial terminal builds retain compiler percentages; parallel builds should emit labeled messages without competing spinners.
- Explicit `bits:stage:*` lines delimit recipe execution. Within recipes, the existing Bash trace identifies common build commands and lifecycle hooks. Do not infer stage changes from arbitrary text mentioning `make` or `install`, or from the traced `echo` of a marker.
- Custom tools can emit `bits:stage:configure`, `bits:stage:compile`, and `bits:stage:install`. Preserve full command output under `--debug`. Post-install hooks, packaging, and relocation share the installation label.
- Report source origins at the retrieval decision: upstream URL, repository and ref, local checkout/cache, or remote store. Preserve fallback order, architecture filtering, checksum verification, and source redistribution rules when changing these messages.
- `tests/test_build_progress.py` covers reporting behavior; source-cache, asynchronous-build, build-epoch, and workarea tests cover the surrounding paths.

## Preparing changes for review

Check the current branch and the requested PR base before publishing. This repository can have substantial work on non-default branches, so compare against the intended base to keep unrelated commits out of a PR. Describe observed behavior, relevant validation, and any environment limits accurately; distinguish illustrative output from captured build output.
