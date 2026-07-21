# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""bits status — dry-run build-plan resolver.

Classifies every package in the dependency tree without building anything.

States
------
local_checkout            A directory matching the package name exists in cwd;
                          the package will be (re)compiled from the local sources.
local_checkout_unchanged  Devel package whose devel_hash + deps_hash has not
                          changed; bits build would skip the rebuild.
already_installed         The installed .build-hash matches the expected hash;
                          nothing will happen.
from_store                A matching tarball exists in the local TARS store;
                          will be unpacked (fast path, no compilation).
from_remote_store         Tarball only in the remote store; will be downloaded
                          then unpacked (requires --check-store to detect).
build_from_source         Nothing found; will compile from scratch.
hash_unknown              Git refs unavailable; state cannot be determined
                          accurately. Run with --fetch-repos to resolve.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from collections import OrderedDict
from glob import glob
from os.path import abspath, basename, dirname, exists, join
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from bits_helpers.git import Git
from bits_helpers.sl import Sapling
from bits_helpers.log import debug, info, warning, banner
from bits_helpers.utilities import (
    SHARED_ARCH,
    compute_combined_arch,
    effective_arch,
    getPackageList,
    parseDefaults,
    prunePaths,
    readDefaults,
    resolve_tag,
    topological_sort,
    ver_rev,
    detectArch,
    validateDefaults,
)
from bits_helpers.workarea import updateReferenceRepoSpec

# NOTE: bits_helpers.build is imported lazily inside doStatus() to avoid pulling
# in jinja2, analytics, and other heavy initialisation at import time (which
# would slow down `bits status` startup and break test isolation).


# ── Inlined helpers (copied from build.py to avoid the heavy import) ───────────

def _readHashFile(fn: str) -> str:
    """Read a .build-hash sentinel file; return "0" if absent."""
    try:
        return open(fn).read().strip("\n")
    except OSError:
        return "0"


def _pkg_install_path_local(work_dir: str, architecture: str, spec: dict) -> str:
    """Return ``<workDir>/<arch>/<pkg>/<version>-<revision>`` for *spec*."""
    family = spec.get("pkg_family", "")
    if family:
        return join(work_dir, architecture, family,
                    spec["package"], ver_rev(spec))
    return join(work_dir, architecture, spec["package"], ver_rev(spec))


# ── State constants ────────────────────────────────────────────────────────────

LOCAL_CHECKOUT           = "local_checkout"
LOCAL_CHECKOUT_UNCHANGED = "local_checkout_unchanged"
ALREADY_INSTALLED        = "already_installed"
FROM_STORE               = "from_store"
FROM_REMOTE_STORE        = "from_remote_store"
BUILD_FROM_SOURCE        = "build_from_source"
HASH_UNKNOWN             = "hash_unknown"

# Human-readable labels and terminal colours for each state
_STATE_LABEL: Dict[str, str] = {
    LOCAL_CHECKOUT:           "local checkout   (will rebuild)",
    LOCAL_CHECKOUT_UNCHANGED: "local checkout   (up to date)",
    ALREADY_INSTALLED:        "already installed",
    FROM_STORE:               "from store       (local tarball)",
    FROM_REMOTE_STORE:        "from store       (remote tarball)",
    BUILD_FROM_SOURCE:        "build from source",
    HASH_UNKNOWN:             "unknown          (no ref cache)",
}

_ANSI: Dict[str, str] = {
    LOCAL_CHECKOUT:           "\033[33m",   # yellow
    LOCAL_CHECKOUT_UNCHANGED: "\033[32m",   # green
    ALREADY_INSTALLED:        "\033[32m",   # green
    FROM_STORE:                "\033[36m",   # cyan
    FROM_REMOTE_STORE:         "\033[36m",   # cyan
    BUILD_FROM_SOURCE:         "\033[31m",   # red
    HASH_UNKNOWN:              "\033[35m",   # magenta
}
_RESET = "\033[0m"


# ── Internal helpers ───────────────────────────────────────────────────────────

def _try_populate_refs(spec: dict, reference_sources: str, package: str) -> None:
    """Populate spec["scm_refs"] from the local mirror without any network I/O.

    If the reference repository mirror does not exist on disk, spec["scm_refs"]
    is set to an empty dict, which will cause commit_hash to fall back to the
    tag string — correct for tagged releases, approximate for branch builds.
    """
    reference_repo = join(abspath(reference_sources), package.lower())
    if not os.path.isdir(reference_repo):
        spec.setdefault("scm_refs", {})
        return
    # Use the same path the full build would use as a reference repo.
    spec["reference"] = reference_repo
    scm = spec.get("scm", Git())
    try:
        output = scm.exec(
            scm.listRefsCmd(reference_repo),
            directory=".",
            check=False,
            prompt=False,
        )[1]
        spec["scm_refs"] = scm.parseRefs(output)
    except Exception:
        spec.setdefault("scm_refs", {})


def _resolve_commit_hash(spec: dict, default_vars=None) -> None:
    """Set spec["commit_hash"] from scm_refs, falling back to the tag string."""
    if "tag" not in spec:
        spec["tag"] = spec["version"]
    # Expand date-based tags (%(year)s etc.) and defaults/recipe variables, so
    # `bits status` resolves the same tag the build does.
    try:
        spec["tag"] = resolve_tag(spec, default_vars)
    except (KeyError, ValueError):
        pass
    if "source" not in spec:
        # Package has no source (e.g. a meta-package); commit_hash is "0".
        spec.setdefault("commit_hash", "0")
        return
    scm_refs = spec.get("scm_refs", {})
    # Prefer branch head; fall back to literal tag / commit hash string.
    spec["commit_hash"] = (
        scm_refs.get("refs/heads/" + spec["tag"])
        or spec["tag"]
    )


def _fetch_refs_with_clone(spec: dict, reference_sources: str,
                           package: str) -> None:
    """Populate spec["scm_refs"] by cloning / fetching the reference repo.

    Only used when --fetch-repos is given; equivalent to what doBuild does.
    """
    try:
        updateReferenceRepoSpec(reference_sources, package, spec,
                                fetch=True, allowGitPrompt=False)
        scm = spec.get("scm", Git())
        ref_repo = spec.get("reference", "")
        if ref_repo and os.path.isdir(ref_repo):
            output = scm.exec(
                scm.listRefsCmd(ref_repo),
                directory=".",
                check=False,
                prompt=False,
            )[1]
            spec["scm_refs"] = scm.parseRefs(output)
        else:
            spec.setdefault("scm_refs", {})
    except SystemExit:
        spec.setdefault("scm_refs", {})


def _scan_local_tars(spec: dict, work_dir: str, architecture: str) -> bool:
    """Return True if a matching tarball exists in the local TARS symlink tree."""
    spec_arch = effective_arch(spec, architecture)
    links_regex = re.compile(
        r"{package}-{version}(?:-(?:local)?[0-9]+)?\.{arch}\.tar\.gz".format(
            package=re.escape(spec["package"]),
            version=re.escape(spec["version"]),
            arch=re.escape(spec_arch),
        )
    )
    symlink_dir = join(work_dir, "TARS", spec_arch, spec["package"])
    try:
        entries = os.listdir(symlink_dir)
    except OSError:
        return False

    for name in entries:
        if not links_regex.fullmatch(name):
            continue
        full = join(symlink_dir, name)
        if not os.path.isfile(full):
            continue   # dangling symlink
        real = os.readlink(full)
        # Extract the hash from the store path:
        # ../../{arch}/store/{hash[:2]}/{hash}/{tarball}
        match = re.match(
            r"../../{arch}/store/[0-9a-f]{{2}}/([0-9a-f]+)/".format(
                arch=re.escape(spec_arch)
            ),
            real,
        )
        if not match:
            continue
        rev_hash = match.group(1)
        if "local" in name:
            if rev_hash in spec.get("local_hashes", []):
                return True
        else:
            if rev_hash in spec.get("remote_hashes", []):
                return True
    return False


def _is_already_installed(spec: dict, work_dir: str, architecture: str) -> bool:
    """Return True if the package's install path already has the expected hash."""
    hash_path = _pkg_install_path_local(work_dir, effective_arch(spec, architecture), spec)
    hash_file = hash_path + "/.build-hash"
    # CVMFS symlink → trust it
    if os.path.islink(hash_path) and os.path.isdir(hash_path):
        return True
    return _readHashFile(hash_file) == spec.get("hash", "")


def _classify(spec: dict, work_dir: str, architecture: str,
              sync_helper=None) -> str:
    """Return the state string for one resolved package spec."""
    pkg = spec["package"]

    if spec.get("is_devel_pkg"):
        old_devel_hash = _readHashFile(
            join(work_dir, "BUILD", spec.get("hash", "X"),
                 pkg, ".build_succeeded")
        )
        if spec.get("devel_hash", "") + spec.get("deps_hash", "") == old_devel_hash:
            return LOCAL_CHECKOUT_UNCHANGED
        return LOCAL_CHECKOUT

    # Non-devel: check in order of cost
    if _is_already_installed(spec, work_dir, architecture):
        return ALREADY_INSTALLED
    if _scan_local_tars(spec, work_dir, architecture):
        return FROM_STORE
    # Remote store probe (opt-in)
    if sync_helper is not None:
        try:
            sync_helper.fetch_tarball(spec)
            tar_hash_dir = join(
                work_dir,
                "TARS", effective_arch(spec, architecture),
                "store", spec["hash"][:2], spec["hash"],
            )
            tarballs = [t for t in glob(join(tar_hash_dir, "*gz"))
                        if os.path.isfile(t)]
            if tarballs:
                return FROM_REMOTE_STORE
        except Exception:
            pass
    return BUILD_FROM_SOURCE


# ── Output formatters ──────────────────────────────────────────────────────────

def _tty_colour() -> bool:
    """Return True when stdout supports ANSI colour."""
    return sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""


def _emit_table(rows: List[dict], architecture: str) -> None:
    """Print a human-readable coloured table."""
    if not rows:
        print("No packages to report.")
        return

    use_colour = _tty_colour()
    col_w = [max(len(r[k]) for r in rows) for k in ("package", "version", "state_label")]
    col_w[0] = max(col_w[0], len("Package"))
    col_w[1] = max(col_w[1], len("Version"))
    col_w[2] = max(col_w[2], len("State"))

    header = "  {:<{w0}}  {:<{w1}}  {:<{w2}}".format(
        "Package", "Version", "State",
        w0=col_w[0], w1=col_w[1], w2=col_w[2]
    )
    sep = "  " + "-" * (col_w[0] + 2 + col_w[1] + 2 + col_w[2])
    print("\nBuild plan for architecture: {}\n".format(architecture))
    print(header)
    print(sep)
    for r in rows:
        state = r["state"]
        label = r["state_label"]
        if use_colour:
            colour = _ANSI.get(state, "")
            line = "  {:<{w0}}  {:<{w1}}  {colour}{:<{w2}}{reset}".format(
                r["package"], r["version"], label,
                colour=colour, reset=_RESET,
                w0=col_w[0], w1=col_w[1], w2=col_w[2],
            )
        else:
            line = "  {:<{w0}}  {:<{w1}}  {:<{w2}}".format(
                r["package"], r["version"], label,
                w0=col_w[0], w1=col_w[1], w2=col_w[2],
            )
        print(line)
    print()

    # Summary line
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    parts = []
    for state in (ALREADY_INSTALLED, FROM_STORE, FROM_REMOTE_STORE,
                  LOCAL_CHECKOUT_UNCHANGED, LOCAL_CHECKOUT, BUILD_FROM_SOURCE,
                  HASH_UNKNOWN):
        n = counts.get(state, 0)
        if n:
            parts.append("{} {}".format(n, _STATE_LABEL[state].split()[0].split("(")[0].strip()))
    print("Summary: " + ", ".join(parts))


def _emit_json(rows: List[dict], architecture: str) -> None:
    """Print a machine-readable JSON report."""
    report = {
        "architecture": architecture,
        "packages": [
            {
                "package": r["package"],
                "version": r["version"],
                "hash": r.get("hash", ""),
                "state": r["state"],
            }
            for r in rows
        ],
    }
    print(json.dumps(report, indent=2))


# ── Main entry point ───────────────────────────────────────────────────────────

def doStatus(args, parser) -> None:
    """Resolve the dependency tree and report the build state of each package."""
    # Deferred heavy imports (build.py pulls in jinja2, analytics, etc.)
    from bits_helpers.build import storeHashes, storeHook, hash_local_changes
    from bits_helpers.log import dieOnError
    from bits_helpers.git import git

    packages = args.pkgname
    specs: dict = {}
    buildOrder: list = []
    work_dir = abspath(args.workDir)
    prunePaths(work_dir)

    if not exists(args.configDir):
      from bits_helpers.repo_provider import cwd_is_recipe_dir
      _default_config_dir = os.environ.get("BITS_REPO_DIR", "alidist")
      if args.configDir == _default_config_dir and cwd_is_recipe_dir():
        debug("Recipe files detected in current directory; using '.' as config dir")
        args.configDir = "."
    dieOnError(not exists(args.configDir),
               'Cannot find recipes under directory "%s".\n'
               'Maybe you need to "cd" to the right directory or '
               'you forgot to run "bits init"?' % args.configDir)

    # ── Defaults and overrides ─────────────────────────────────────────────────
    defaults_reader = lambda: readDefaults(
        args.configDir, args.defaults, parser.error, args.architecture
    )
    err, overrides, taps, defaults_meta = parseDefaults(
        args.disable, defaults_reader, debug, args.architecture, args.configDir
    )
    dieOnError(err, err)

    raw_architecture = args.architecture
    args.architecture = compute_combined_arch(defaults_meta, args.defaults, raw_architecture)

    # ── Package list resolution ────────────────────────────────────────────────
    # Use no-op lambdas for prefer_check and requirement_check: bits status does
    # not run any external commands to test system package compatibility.
    _noop_check = lambda pkg, cmd: (0, "")

    os.makedirs(join(work_dir, "SPECS"), exist_ok=True)

    systemPackages, ownPackages, failed, validDefaults = getPackageList(
        packages                = packages,
        specs                   = specs,
        configDir               = args.configDir,
        preferSystem            = False,
        noSystem                = "*",
        architecture            = raw_architecture,
        disable                 = args.disable,
        force_rebuild           = args.force_rebuild,
        defaults                = args.defaults,
        performPreferCheck      = _noop_check,
        performRequirementCheck = _noop_check,
        performValidateDefaults = lambda spec: validateDefaults(spec, args.defaults),
        overrides               = overrides,
        taps                    = taps,
        log                     = debug,
        provider_dirs           = {},
        defaults_meta           = defaults_meta,
    )

    if failed:
        warning(
            "The following packages are listed as system requirements "
            "(status check continues without them): %s",
            ", ".join(sorted(failed))
        )

    for x in specs.values():
        x["requires"]         = [r for r in x["requires"]         if r not in args.disable]
        x["build_requires"]   = [r for r in x["build_requires"]   if r not in args.disable]
        x["runtime_requires"] = [r for r in x["runtime_requires"] if r not in args.disable]

    buildOrder = list(topological_sort(specs))

    # ── Devel package detection ────────────────────────────────────────────────
    if args.forceTracked:
        devel_pkgs: set = set()
    else:
        devel_candidates = (
            {basename(d) for d in glob("*") if os.path.isdir(d)}
            - frozenset(args.noDevel)
        )
        devel_pkgs = frozenset(buildOrder) & devel_candidates
        # Warn on case mismatches (mirroring build.py's dieOnError check)
        upper_candidates = {d.upper() for d in devel_candidates}
        upper_pkgs = {p for p in buildOrder if p.upper() in upper_candidates}
        mismatched = upper_pkgs - devel_pkgs
        if mismatched:
            warning(
                "Development packages with wrong spelling (will not be "
                "treated as local checkouts): %s", ", ".join(sorted(mismatched))
            )

    for pkg, spec in specs.items():
        spec["is_devel_pkg"] = pkg in devel_pkgs
        if spec["is_devel_pkg"]:
            spec["source"] = str(Path.cwd() / pkg)
        # Initialise SCM (Sapling or Git)
        use_sapling = False
        if "source" in spec:
            source_path = Path(spec["source"])
            has_sl = ((source_path / ".sl").exists()
                      or (source_path / ".git" / "sl").exists())
            if has_sl and shutil.which("sl"):
                use_sapling = True
        spec["scm"] = Sapling() if use_sapling else Git()
        spec["commit_hash"] = "0"

    # ── Reference sources ──────────────────────────────────────────────────────
    reference_sources = getattr(args, "referenceSources",
                                join(work_dir, "MIRROR"))

    # ── Per-package hash computation ───────────────────────────────────────────
    # Processed in topological order so dependency hashes are available.
    rows: List[dict] = []

    # Optional remote store for --check-store probing
    sync_helper = None
    if getattr(args, "checkStore", False) and getattr(args, "remoteStore", ""):
        try:
            from bits_helpers.sync import remote_from_url
            sync_helper = remote_from_url(
                args.remoteStore, "", args.architecture, work_dir,
                getattr(args, "insecure", False)
            )
        except Exception as exc:
            warning("Cannot initialise remote store for --check-store: %s", exc)

    hash_error_pkgs: List[str] = []

    for p in buildOrder:
        spec = specs[p]

        # Populate git refs (offline by default, or with clone/fetch if requested)
        if "source" in spec and not spec["is_devel_pkg"]:
            if getattr(args, "fetchRepos", False):
                _fetch_refs_with_clone(spec, reference_sources, p)
            else:
                _try_populate_refs(spec, reference_sources, p)
        elif spec["is_devel_pkg"]:
            # Devel packages: read refs from the local checkout directly
            try:
                scm = spec["scm"]
                out = scm.exec(
                    scm.listRefsCmd(spec["source"]),
                    directory=".",
                    check=False,
                    prompt=False,
                )[1]
                spec["scm_refs"] = scm.parseRefs(out)
            except Exception:
                spec.setdefault("scm_refs", {})
        else:
            spec.setdefault("scm_refs", {})

        # Resolve commit hash
        _resolve_commit_hash(spec, defaults_meta.get("variables"))

        # Devel package: compute devel_hash from local changes
        if spec["is_devel_pkg"]:
            try:
                out = spec["scm"].checkedOutCommitName(directory=spec["source"])
                spec["commit_hash"] = out.strip()
                local_hash, _ = hash_local_changes(spec)
                spec["devel_hash"] = spec["commit_hash"] + local_hash
                out = spec["scm"].branchOrRef(directory=spec["source"])
                dev_branch = out.replace("/", "-")
                spec["tag"] = (getattr(args, "develPrefix", None) or dev_branch)
                spec["commit_hash"] = "0"
            except Exception as exc:
                debug("Could not compute devel_hash for %s: %s", p, exc)
                spec.setdefault("devel_hash", "")

        # Compute build hashes (same as doBuild main loop)
        consider_relocation = (
            raw_architecture.startswith("osx")
            and spec.get("architecture") != SHARED_ARCH
        )
        try:
            storeHook(p, specs, args.defaults[0])
            storeHashes(p, specs, considerRelocation=consider_relocation)
        except Exception as exc:
            debug("Hash computation failed for %s: %s", p, exc)
            hash_error_pkgs.append(p)
            rows.append({
                "package":     spec.get("package", p),
                "version":     spec.get("version", "?"),
                "hash":        "",
                "state":       HASH_UNKNOWN,
                "state_label": _STATE_LABEL[HASH_UNKNOWN],
            })
            continue

        # Assign the final hash (mirrors the logic in doBuild after symlink scan)
        if "force_revision" in spec:
            spec["hash"] = spec["remote_revision_hash"]
        else:
            # For status purposes, assume remote revision (no local suffix)
            spec["hash"] = spec["remote_revision_hash"]
            spec["revision"] = "1"

        state = _classify(spec, work_dir, args.architecture, sync_helper)
        version_str = spec.get("version", "?")
        if spec["is_devel_pkg"]:
            version_str = "{} (dev)".format(version_str)

        rows.append({
            "package":     spec.get("package", p),
            "version":     version_str,
            "hash":        spec.get("hash", ""),
            "state":       state,
            "state_label": _STATE_LABEL[state],
        })

    if hash_error_pkgs:
        warning(
            "Hash computation failed for %d package(s): %s\n"
            "These packages are listed as '%s'.\n"
            "Re-run with --fetch-repos to populate the ref cache.",
            len(hash_error_pkgs), ", ".join(hash_error_pkgs), HASH_UNKNOWN,
        )

    if getattr(args, "json_output", False):
        _emit_json(rows, args.architecture)
    else:
        _emit_table(rows, args.architecture)
