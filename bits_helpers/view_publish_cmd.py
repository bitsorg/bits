# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""`bits publish --view <name>` — publish the merged view for a release.

Unions every package of one release (one ``build_id``) into
``<cvmfs-target>/Views/<name>-<build_id>/<arch>/`` with relative symlinks + a
nested ``.cvmfscatalog``, so consumers get a single-entry environment for the
release with no per-node view build (``bits enter --view`` prefers it).

The ``build_id`` is *not* given on the command line — it is read from the
packages' ``.meta.json``: from the named package's metadata when a package is
given, otherwise auto-detected from the build area (and, if the area holds more
than one build, you are asked to disambiguate by naming the top package).
"""

import json
import os

from bits_helpers.log import debug, error, info, warning
from bits_helpers.view import collect_build_id_roots, build_published_view


def _build_id_of_package(work_dir, architecture, package):
    """Read the build_id from a named package's .meta.json under the work area.

    *package* may be ``name`` or ``name/version``; the first installed match wins.
    Matching is on the recorded package name (``.meta.json``'s ``package.name``),
    not the directory layout.
    """
    name = package.split("/", 1)[0]
    base = os.path.join(work_dir, architecture)
    for dirpath, dirs, files in os.walk(base):
        if ".meta.json" not in files:
            continue
        dirs[:] = []   # a package root: do not descend into its contents
        try:
            with open(os.path.join(dirpath, ".meta.json")) as fh:
                meta = json.load(fh)
        except Exception:
            continue
        pkg = meta.get("package")
        pkg_name = pkg.get("name") if isinstance(pkg, dict) else pkg
        if pkg_name == name and meta.get("build_id"):
            return meta["build_id"]
    return None


def _layout_views_dir(roots):
    """The ``views_dir`` recorded in the release's package metadata (default
    ``Views``), so the published view honours a non-default profile layout."""
    for root in roots:
        try:
            with open(os.path.join(root, ".meta.json")) as fh:
                layout = json.load(fh).get("cvmfs_layout")
        except Exception:
            continue
        if isinstance(layout, dict) and layout.get("views_dir"):
            return layout["views_dir"]
    return "Views"


def _build_ids_in_area(work_dir, architecture):
    """Return the set of build_ids present in the work area for this arch."""
    base = os.path.join(work_dir, architecture)
    ids = set()
    for dirpath, dirs, files in os.walk(base):
        if ".meta.json" not in files:
            continue
        dirs[:] = []
        try:
            with open(os.path.join(dirpath, ".meta.json")) as fh:
                bid = json.load(fh).get("build_id")
        except Exception:
            continue
        if bid:
            ids.add(bid)
    return ids


def _resolve_build_id(args, work_dir, architecture):
    """Derive the build_id to publish a view for, or None on an error already
    reported to the user."""
    package = getattr(args, "package", None)
    if package:
        bid = _build_id_of_package(work_dir, architecture, package)
        if not bid:
            error("publish --view: no build_id found for package %s under %s/%s",
                  package, work_dir, architecture)
        return bid
    ids = _build_ids_in_area(work_dir, architecture)
    if not ids:
        error("publish --view: no packages with a build_id found under %s/%s",
              work_dir, architecture)
        return None
    if len(ids) > 1:
        error("publish --view: %d build_ids in the build area: %s. Name the "
              "release's top package to pick one (e.g. `bits publish --view %s "
              "ROOT/<ver>`).", len(ids), ", ".join(sorted(ids)),
              getattr(args, "publishView", "<name>"))
        return None
    return ids.pop()


def doPublishView(args, parser):
    """Build and place the named release view. Returns True on success."""
    name = args.publishView
    architecture = args.architecture
    work_dir = os.path.abspath(args.workDir)
    # The view's symlinks must resolve where the packages finally live, so they
    # are collected from the deployed target, not the raw build area.
    store = getattr(args, "cvmfsTarget", None) or work_dir

    build_id = _resolve_build_id(args, work_dir, architecture)
    if not build_id:
        return False

    roots = collect_build_id_roots(store, build_id, architecture=architecture)
    if not roots:
        error("publish --view: no deployed packages for build_id %s under %s "
              "(publish the packages first).", build_id, store)
        return False

    views_dir = _layout_views_dir(roots)
    result = build_published_view(roots, name, build_id, architecture, store,
                                  views_dir=views_dir)
    # Compliance obligations live at the release root: place NOTICE and the
    # GPL source offer in the published view, generated from this build's
    # manifest. Best-effort — never fails the view publish.
    try:
        from bits_helpers.certify import load_build_manifests
        from bits_helpers.notice import write_release_compliance
        from bits_helpers.provenance import build_id_from_manifest
        for man in load_build_manifests(os.path.join(work_dir, "MANIFESTS")):
            if build_id_from_manifest(man) == build_id:
                write_release_compliance(result["view_dir"],
                                         man.get("packages") or [], build_id)
                break
        else:
            debug("publish --view: no local manifest for %s — NOTICE skipped",
                  build_id)
    except Exception as exc:              # pylint: disable=broad-except
        warning("publish --view: could not write NOTICE/source-offer: %s", exc)
    info("publish --view: '%s' (%s) — %d package(s) -> %s (%d link(s))",
         name, build_id, len(roots), result["view_dir"], len(result["linked"]))
    if result["conflicts"]:
        warning("publish --view: %d file conflict(s), first writer kept; e.g. %s",
                len(result["conflicts"]),
                ", ".join(c[0] for c in result["conflicts"][:5]))
    return True
