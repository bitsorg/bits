# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate CVMFS filebundle specs from a package's preload sidecars.

Background
----------
`PreloadRecipe` traces an application's startup with strace and drops a sidecar
per traced executable at::

    <pkgroot>/.bits-preload/<trigger-base>.paths

The sidecar is dumb, deliberately: every line is a path the launch opened,
written relative to the architecture install base
(``<pkg>/<verrev>/<rest>`` — or the grouped ``<family>/<pkg>/<verrev>/<rest>``),
so it is portable across the build-time and publish-time path prefixes. Line 1
is the trigger executable itself (same coordinate system), so it can both locate
the bundle and be excluded from its own dependency list. Dependency files appear
as other packages' entries, which is the whole point (prefetch the closure, not
just this package).

This module turns those sidecars into the CVMFS filebundle spec files:

    <dir>/.cvmfsbundle-<trigger-base>

a versioned JSON document listing the dependencies as repository-root-absolute
paths (see https://cvmfs.readthedocs.io/en/stable/cpt-file-bundles/)::

    { "name": "CVMFS_BUNDLE", "version": "1.0.0", "encoding": "UTF-8",
      "dependencies": [ "/el9/Packages/Boost/1.90.0/lib/libboost.so", ... ] }

Because a dependency file belongs to a *different* package that publishes to its
own CVMFS path, each entry is resolved through its owning package (found by
walking up to the directory that holds a ``.meta.json``) and that package's
resolved repo path — a release-aware step, but self-contained: every dependency's
``.meta.json`` is present in the work tree at publish time.
"""

import json
import os

# CVMFS filebundle spec envelope (cpt-file-bundles).
SPEC_NAME = "CVMFS_BUNDLE"
SPEC_VERSION = "1.0.0"
SPEC_ENCODING = "UTF-8"

SIDECAR_DIR = ".bits-preload"


def parse_sidecar(path):
    """Return ``(trigger_rel, [file_rel, ...])`` from a sidecar file.

    All lines are arch-base-relative; line 1 is the trigger, the rest are the
    opened files. ``#`` comments and blank lines are ignored. Order is preserved
    and duplicates dropped. Returns ``(None, [])`` on a read error or an empty
    sidecar — a bad sidecar must never abort a publish.
    """
    trigger, files, seen = None, [], set()
    try:
        with open(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if trigger is None:
                    trigger = line
                    continue
                if line not in seen:
                    seen.add(line)
                    files.append(line)
    except OSError:
        return None, []
    return trigger, files


def _owning_pkg_root(rel, meta_exists):
    """Longest leading prefix of *rel* that is a package root (holds .meta.json).

    Handles both the 2-level ``<pkg>/<verrev>`` and grouped 3-level
    ``<family>/<pkg>/<verrev>`` layouts by asking *meta_exists(prefix)* for
    growing prefixes and taking the longest that matches. Returns
    ``(pkg_root, rest)`` or ``(None, None)`` when no ancestor is a package.
    """
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    best = None
    for i in range(1, len(parts)):            # need at least one trailing component
        prefix = "/".join(parts[:i])
        if meta_exists(prefix):
            best = i                           # keep the LONGEST matching prefix
    if best is None:
        return None, None
    return "/".join(parts[:best]), "/".join(parts[best:])


def _is_safe_rel(rel):
    """True if *rel* is a plain, in-tree relative path (no abs, no ``..``, no NUL)."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        return False
    return ".." not in [p for p in rel.split("/")]


def _warn(fmt, *args):
    """Best-effort warning; never raise (this runs on the publish path)."""
    try:
        from bits_helpers.log import warning
        warning(fmt, *args)
    except Exception:                          # pragma: no cover
        pass


def build_dependencies(files_rel, resolve_repo, meta_exists, skip=None):
    """Map arch-base-relative files to repo-root-absolute bundle entries.

    *resolve_repo(pkg_root)* returns the owning package's repo-relative CVMFS
    path (e.g. ``"el9/Packages/Boost/1.90.0"``); *meta_exists(prefix)* reports
    whether *prefix* is a package root. Entries that are unsafe, whose owner
    cannot be found or resolved, or that equal *skip* (the trigger's own file)
    are dropped. Result is sorted and de-duplicated.
    """
    skip = skip or set()
    out = set()
    for rel in files_rel:
        if rel in skip or not _is_safe_rel(rel):
            continue
        pkg_root, rest = _owning_pkg_root(rel, meta_exists)
        if not pkg_root or not rest:
            continue
        repo_path = resolve_repo(pkg_root)
        if not repo_path:
            continue
        out.add("/" + repo_path.strip("/") + "/" + rest)
    return sorted(out)


def render_spec(dependencies):
    """The versioned filebundle JSON document for *dependencies* (verbatim keys)."""
    return {
        "name": SPEC_NAME,
        "version": SPEC_VERSION,
        "encoding": SPEC_ENCODING,
        "dependencies": list(dependencies),
    }


def bundle_path_for(trigger_rel):
    """``<dir>/.cvmfsbundle-<base>`` for a trigger's package-relative path."""
    d, base = os.path.split(trigger_rel)
    name = ".cvmfsbundle-" + base
    return os.path.join(d, name) if d else name


def _generate_one(pkgroot, sidecar_path, resolve_repo, meta_exists):
    """Write one bundle from one sidecar; return its pkgroot-relative path or None.

    None when the sidecar is unreadable/empty, its trigger owner cannot be
    resolved, or it yields no dependencies (no empty bundle is written).
    """
    trigger_rel, files_rel = parse_sidecar(sidecar_path)
    if not trigger_rel or not _is_safe_rel(trigger_rel):
        return None
    # The trigger is arch-base-relative too; its in-package location is the part
    # after its own <pkg>/<verrev> root, which is where the bundle goes.
    _pkg_root, trigger_rest = _owning_pkg_root(trigger_rel, meta_exists)
    if not trigger_rest:
        return None
    deps = build_dependencies(files_rel, resolve_repo, meta_exists,
                              skip={trigger_rel})
    if not deps:
        return None
    rel = bundle_path_for(trigger_rest)
    dest = os.path.join(pkgroot, rel)
    os.makedirs(os.path.dirname(dest) or pkgroot, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(render_spec(deps), fh, indent=2)
        fh.write("\n")
    return rel


def generate_for_package(pkgroot, resolve_repo, meta_exists):
    """Turn every sidecar under ``<pkgroot>/.bits-preload/`` into a bundle file.

    Writes ``<pkgroot>/<dir>/.cvmfsbundle-<base>`` next to each trigger, then
    removes the ``.bits-preload`` directory so it is not published. *resolve_repo*
    and *meta_exists* provide the owning-package resolution. Returns the list of
    bundle paths written (pkgroot-relative).

    Fail-safe: one bad sidecar must never abort a publish, and the sidecar dir is
    always removed — so a per-sidecar failure is logged and skipped, and the
    cleanup runs in a ``finally`` even if something raises.
    """
    import shutil
    sdir = os.path.join(pkgroot, SIDECAR_DIR)
    if not os.path.isdir(sdir):
        return []
    written = []
    try:
        for name in sorted(os.listdir(sdir)):
            if not name.endswith(".paths"):
                continue
            try:
                rel = _generate_one(pkgroot, os.path.join(sdir, name),
                                    resolve_repo, meta_exists)
            except Exception as exc:           # never let one sidecar abort publish
                _warn("preload bundle: skipping %s: %s", name, exc)
                continue
            if rel:
                written.append(rel)
    finally:
        shutil.rmtree(sdir, ignore_errors=True)    # always drop sidecars
    return written
