# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Relaxed-reuse matcher (ADR-0001, Stage 1b).

Find a package already deployed in a blessed CVMFS release that can be grafted by
(name, architecture, build_id) — without the exact content-hash match that
``strict`` reuse requires. This is the read-only lookup the dependency resolver
calls when ``--reuse-policy relaxed`` is in effect; it never mutates the store.

The deployed layout mirrors what ``CVMFSRemoteSync`` consumes:
``<store_root>/<architecture>/Packages/<package>/<version>/.meta.json``, where
``.meta.json`` carries the ``build_id`` written by ``create_provenance_info``
(Stage 0). A deployment that predates Stage 0 (no ``build_id``) simply never
matches, so relaxed reuse degrades to a normal build — safe by default.
"""

import json
import os
from glob import glob


def _read_meta(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def available_build_ids(package, architecture, store_root):
    """Return ``{build_id: (version_dir, mtime)}`` for *package* on the store.

    Scans ``<store_root>/<architecture>/Packages/<package>/*/.meta.json`` and
    keeps, per build_id, the version directory with the newest ``.meta.json``
    mtime (the recency proxy used for "latest" — there is no build-time field in
    the metadata). Defensive: a missing store or unreadable meta yields ``{}``.
    """
    out = {}
    if not (package and architecture and store_root):
        return out
    pkg_dir = os.path.join(store_root, architecture, "Packages", package)
    if not os.path.isdir(pkg_dir):
        return out
    for ver_dir in glob(os.path.join(pkg_dir, "*")):
        if not os.path.isdir(ver_dir):
            continue
        meta = _read_meta(os.path.join(ver_dir, ".meta.json"))
        if not isinstance(meta, dict):
            continue
        bid = meta.get("build_id")
        if not bid:
            continue
        try:
            mtime = os.path.getmtime(os.path.join(ver_dir, ".meta.json"))
        except OSError:
            mtime = 0.0
        if bid not in out or mtime > out[bid][1]:
            out[bid] = (ver_dir, mtime)
    return out


def select_build_id(packages, architecture, store_root, strategy="latest"):
    """Pick a build_id for relaxed reuse across the requested *packages*.

    ``latest``        : newest build_id available for the anchor (the last
                        requested package — the build target). A coherent release
                        shares one build_id across its whole closure, so the
                        target's newest build_id already covers its dependencies.
    ``latest-common`` : newest build_id present for EVERY requested package.

    Only the top-level requested packages are known here (the resolved
    dependency closure is not available until after this selection feeds the
    resolver), so "common" is across the requested targets. Returns
    ``(build_id | None, coverage)`` where coverage maps build_id -> set(packages).
    """
    per_pkg = {p: available_build_ids(p, architecture, store_root) for p in packages}
    coverage, newest = {}, {}
    for pkg, ids in per_pkg.items():
        for bid, (_vd, mtime) in ids.items():
            coverage.setdefault(bid, set()).add(pkg)
            newest[bid] = max(newest.get(bid, 0.0), mtime)
    if not coverage:
        return None, coverage
    if strategy == "latest-common":
        want = len(set(packages))
        common = [b for b, pkgs in coverage.items() if len(pkgs) == want]
        if not common:
            return None, coverage
        return max(common, key=lambda b: newest[b]), coverage
    # 'latest': anchor on the build target (last requested package)
    anchor_ids = per_pkg.get(packages[-1], {}) if packages else {}
    if not anchor_ids:
        return None, coverage
    return max(anchor_ids, key=lambda b: anchor_ids[b][1]), coverage


def graftable_match(package, architecture, build_id, store_root):
    """Return a match descriptor for *package* under *build_id*, or None.

    Scans ``<store_root>/<architecture>/Packages/<package>/<version>/.meta.json``
    and returns the first version whose recorded ``build_id`` matches and whose
    ``architecture`` (when recorded) agrees:

        {"package", "version", "path", "hash", "build_id"}

    Defensive throughout: a missing store, an unreadable or legacy ``.meta.json``,
    or any mismatch yields None (→ the package is built normally).
    """
    if not (package and architecture and build_id and store_root):
        return None
    pkg_dir = os.path.join(store_root, architecture, "Packages", package)
    if not os.path.isdir(pkg_dir):
        return None
    # Deterministic order so repeated resolutions agree; one version per package
    # is expected within a coherent release, but be explicit anyway.
    for ver_dir in sorted(glob(os.path.join(pkg_dir, "*"))):
        if not os.path.isdir(ver_dir):
            continue
        meta = _read_meta(os.path.join(ver_dir, ".meta.json"))
        if not isinstance(meta, dict):
            continue
        if meta.get("build_id") != build_id:
            continue
        meta_arch = meta.get("architecture")
        if meta_arch and meta_arch != architecture:
            continue
        pkg_info = meta.get("package") if isinstance(meta.get("package"), dict) else {}
        # Take version/revision from the deployed .meta.json (authoritative), NOT
        # from the directory basename — that is "<version>-<revision>" and cannot
        # be split unambiguously (versions contain dashes). The consumer's reuse
        # decision matches the deployed tarball by "<package>-<version>-…", so the
        # spec's version MUST equal the deployed version for the graft to fire.
        return {
            "package": package,
            "version": pkg_info.get("version") or os.path.basename(ver_dir.rstrip("/")),
            "revision": pkg_info.get("revision"),
            "path": ver_dir,
            "hash": pkg_info.get("hash"),
            "build_id": build_id,
        }
    return None
