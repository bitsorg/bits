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
        return {
            "package": package,
            "version": os.path.basename(ver_dir.rstrip("/")),
            "path": ver_dir,
            "hash": pkg_info.get("hash"),
            "build_id": build_id,
        }
    return None
