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

_PRELOAD_DEF = re.compile(r'(?m)^[ \t]*(?:function[ \t]+)?Preload[ \t]*\([ \t]*\)')
# open("/path", ...) = <ret> / openat(AT_FDCWD, "/path", ...) = <ret> — capture
# the path AND the syscall return so failed probes (= -1 ENOENT) are dropped.
_OPEN_RE = re.compile(
    r'(?:\bopen\(|\bopenat\(AT_FDCWD,\s*)"([^"]+)"[^=\n]*=\s*(-?\d+)')


def has_preload(recipe_body):
    """True if a recipe's bash body defines a ``Preload()`` function."""
    return bool(recipe_body and _PRELOAD_DEF.search(recipe_body))


def preload_triggers(recipe_body):
    """Extract ``(exe, [args])`` for each ``cvmfs_preload`` call in ``Preload()``.

    We parse the calls rather than source the recipe body: sourcing arbitrary
    build-time bash (with its ``bits-include`` and top-level side effects) is
    unsafe and unnecessary — a ``Preload()`` in practice just lists
    ``cvmfs_preload <exe> [args...]`` invocations. The ``Preload()`` block is
    isolated by brace matching; each ``cvmfs_preload`` line is shlex-split.
    Lines that will not shlex-parse are skipped.
    """
    if not has_preload(recipe_body):
        return []
    m = _PRELOAD_DEF.search(recipe_body)
    body = recipe_body[m.end():]
    open_brace = body.find("{")
    if open_brace < 0:
        return []
    depth, i, n = 0, open_brace, len(body)
    while i < n:                                    # find the matching close brace
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    block = body[open_brace + 1:i]
    triggers = []
    for line in block.replace(";", "\n").splitlines():
        line = line.strip()
        if not line.startswith("cvmfs_preload"):
            continue
        try:
            toks = shlex.split(line)
        except ValueError:
            continue
        if len(toks) >= 2:                          # cvmfs_preload <exe> [args...]
            triggers.append((toks[1], toks[2:]))
    return triggers


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
