# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""`bits preload` — post-publish CVMFS filebundle generator.

Given a list of packages and a deployed CVMFS path, for each package that
declares a ``Preload()`` in its recipe: locate it under the tree, set up its
deployed environment, strace the recipe's trigger (via a tool-provided
``cvmfs_preload`` so ``bits-recipe-tools`` is untouched), turn the captured
``/cvmfs`` opens into ``.cvmfsbundle-<trigger>`` spec files, tar them, and
publish that one tar into CVMFS.

This module keeps the pure, unit-testable steps (recipe `Preload` detection,
locating a package on the tree, parsing the tracer output, assembling bundles
and the tar). The strace, environment setup and publish are shelled out and are
validated on a host with a real mounted repo — they cannot run in CI.
"""

import os
import re
import shlex
import tarfile

from bits_helpers import preload_bundle as B

# open("/path", ...) = <ret> / openat(AT_FDCWD, "/path", ...) = <ret> — capture
# the path AND the syscall return so failed probes (= -1 ENOENT) are dropped.
_OPEN_RE = re.compile(
    r'(?:\bopen\(|\bopenat\(AT_FDCWD,\s*)"([^"]+)"[^=\n]*=\s*(-?\d+)')


def _norm_tests(raw):
    """Normalise a ``preload:`` / config ``tests:`` list to ``[(exe, [args]), …]``.

    Accepts either mapping entries (``{exe: bin/x, args: [--v]}``) or bare
    strings (``"bin/x --v"``, shlex-split). Entries without an exe are skipped.
    """
    out = []
    for item in raw or []:
        if isinstance(item, dict):
            exe = item.get("exe")
            args = item.get("args") or []
            if isinstance(args, str):
                args = shlex.split(args)
            args = [str(a) for a in args]
        elif isinstance(item, str):
            try:
                toks = shlex.split(item)
            except ValueError:
                continue
            exe, args = (toks[0] if toks else None), toks[1:]
        else:
            continue
        if exe:
            out.append((str(exe), args))
    return out


def recipe_tests(spec):
    """Tests from a recipe's hash-excluded ``preload:`` section (via ``parseRecipe``).

    *spec* is the parsed YAML front-matter; ``spec['preload']`` is the test list.
    Returns ``[(exe, [args]), …]`` (empty when the recipe declares none).
    """
    if not isinstance(spec, dict):
        return []
    return _norm_tests(spec.get("preload"))


def load_config(yaml_text):
    """Parse ``config/preload.yaml`` into a normalised sweep config.

    Returns ``{arch: [..]|None, docker: bool, update: bool, packages: {name:
    {tests: [(exe,[args])], versions: [..]}}}``. ``packages`` accepts a bare list
    (names defer to the recipe), a mapping, or a list mixing names and single-key
    mappings; ``arch`` accepts a scalar or list (None ⇒ discover). An empty/{}
    ``packages`` means "all packages that carry a recipe preload:".
    """
    from bits_helpers.utilities import yamlLoad
    data = yamlLoad(yaml_text) or {}
    arch = data.get("arch")
    if isinstance(arch, str):
        arch = [arch]
    pkgs = {}

    def _add(name, spec):
        spec = spec or {}
        pkgs[name] = {"tests": _norm_tests(spec.get("tests")),
                      "versions": list(spec.get("versions") or [])}

    raw = data.get("packages")
    if isinstance(raw, dict):
        for name, spec in raw.items():
            _add(name, spec)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                _add(item, {})
            elif isinstance(item, dict) and len(item) == 1:
                (name, spec), = item.items()
                _add(name, spec)
    return {"arch": arch or None,
            "docker": bool(data.get("docker", False)),
            "update": bool(data.get("update", False)),
            "packages": pkgs}


def resolve_tests(cfg_pkg, recipe_spec):
    """Tests for a package: the config's ``tests`` if any, else the recipe's.

    *cfg_pkg* is a ``load_config`` package entry (or None); *recipe_spec* the
    package's parsed recipe front-matter (or None).
    """
    if cfg_pkg and cfg_pkg.get("tests"):
        return list(cfg_pkg["tests"])
    return recipe_tests(recipe_spec)


def bundle_exists(pkg_dir, exe):
    """True if ``<pkg_dir>/<dir(exe)>/.cvmfsbundle-<base(exe)>`` already exists."""
    return os.path.exists(os.path.join(pkg_dir, B.bundle_path_for(exe)))


def discover_archs(cvmfs_root):
    """Deployed platforms under *cvmfs_root* (reuses cvmfs_inspect)."""
    from bits_helpers import cvmfs_inspect as I
    return I.list_platforms(cvmfs_root)


def package_dir(cvmfs_root, arch, pkg, verrev):
    """``<cvmfs_root>/<arch>/Packages/<pkg>/<verrev>`` — a deployed package dir."""
    return os.path.join(cvmfs_root, arch, "Packages", pkg, verrev)


def package_versions(cvmfs_root, arch, pkg, want=None):
    """Deployed ``<verrev>`` dirs for *pkg* (reuses cvmfs_inspect), newest last.

    *want* is an optional list of version globs (from config ``versions:``); a
    bare version matches its ``<version>-<rev>`` dir too (``5.9.1`` ⇒ ``5.9.1-1``).
    """
    import fnmatch
    from bits_helpers import cvmfs_inspect as I
    vers = I.list_packages(cvmfs_root, arch).get(pkg, [])
    if want:
        vers = [v for v in vers
                if any(fnmatch.fnmatch(v, w) or fnmatch.fnmatch(v, w + "-*")
                       for w in want)]
    return vers


def parse_strace_opens(strace_text):
    """Absolute paths SUCCESSFULLY opened in ``strace -e open,openat`` output.

    Keeps only opens whose syscall returned a valid fd (``= N`` with N >= 0), so
    the dynamic loader's failed probes (``= -1 ENOENT`` in ``glibc-hwcaps/``,
    ``tls/`` and arch subdirs) are excluded — otherwise the bundle lists files
    that do not exist. First-seen order, de-duplicated; relative paths (from an
    already-chdir'd process) are dropped as they cannot be mapped to the repo.
    """
    out, seen = [], set()
    for path, ret in _OPEN_RE.findall(strace_text or ""):
        if ret.startswith("-"):                 # failed syscall (-1 ENOENT, …)
            continue
        if path.startswith("/") and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def locate_package(cvmfs_path, pkg, ver=None):
    """Deployed package directory ``…/<pkg>/<verrev>`` under *cvmfs_path*.

    Finds a directory named *pkg* that itself contains a version directory; when
    *ver* is given, the version dir must equal it or start with ``ver + '-'``
    (so ``6.24.06`` matches ``6.24.06-4``). Returns the newest matching verrev's
    absolute path, or None. Does not follow symlinks out of the tree.
    """
    hits = []
    for dirpath, dirnames, _ in os.walk(cvmfs_path):
        if os.path.basename(dirpath) != pkg:
            continue
        for v in sorted(dirnames):
            if ver and not (v == ver or v.startswith(ver + "-")):
                continue
            hits.append(os.path.join(dirpath, v))
        dirnames[:] = []                 # a <pkg> dir's children are versions, stop
    if not hits:
        return None
    return sorted(hits)[-1]


def assemble_bundles(traces, repo_root, staging_dir):
    """Stage a ``.cvmfsbundle-*`` file for every trace block that yields deps.

    *traces* is the parsed tracer output; each ``(trigger, opens)`` becomes a
    bundle via :func:`preload_bundle.build_bundle`, written into *staging_dir*
    at its repo-relative path. Returns the sorted list of staged tar-relative
    paths (empty when nothing under the repo was opened).
    """
    staged = []
    for trigger_abs, opened_abs in traces:
        tar_rel, spec = B.build_bundle(trigger_abs, opened_abs, repo_root)
        if not tar_rel:
            continue
        B.stage_bundle(staging_dir, tar_rel, spec)
        staged.append(tar_rel)
    return sorted(staged)


def make_tar(staging_dir, out_tar):
    """Tar the staged bundle tree (paths relative to *staging_dir*) into *out_tar*.

    Deterministic ordering. Returns *out_tar*. An empty staging tree yields an
    empty tar, which the caller should not publish.
    """
    entries = []
    for dp, _dn, fns in os.walk(staging_dir):
        for f in fns:
            full = os.path.join(dp, f)
            entries.append((full, os.path.relpath(full, staging_dir)))
    with tarfile.open(out_tar, "w") as tf:
        for full, arc in sorted(entries, key=lambda e: e[1]):
            tf.add(full, arcname=arc, recursive=False)
    return out_tar
