# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Merged-view ("symlink farm") support.

A *view* unions a coherent set of package install prefixes into one directory,
so that a consumer's environment collapses to a single entry per variable:

    PATH=<view>/bin
    LD_LIBRARY_PATH=<view>/lib:<view>/lib64
    CMAKE_PREFIX_PATH=<view>
    PKG_CONFIG_PATH=<view>/lib/pkgconfig:<view>/lib64/pkgconfig
    PYTHONPATH=<view>/lib/pythonX.Y/site-packages

instead of one entry per dependency. This is the standard fix (LCG views,
Spack/Nix profiles) for the unbounded growth of `PATH`-type variables on a large
stack, and it also spares the dynamic loader from scanning N directories per
lookup.

The view is a farm of symlinks pointing back at the per-package trees; merging is
done at file granularity (directories are real directories so several packages
can contribute into the same one). This module is pure filesystem logic with no
bits dependencies, so it is exercised directly in tests; wiring it into the build
or publish path is separate.
"""

import json
import os

CATALOG_FILE = ".cvmfscatalog"


# The consumable subtrees to merge. Deliberately excludes per-package metadata
# (etc/profile.d/init.sh, etc/modulefiles, .meta.json, .build-hash, relocate-me.sh)
# which would otherwise collide on every package. `lib` carries pkgconfig, cmake
# config packages and Python site-packages, so a single `lib` merge covers
# PKG_CONFIG_PATH, CMake config-mode discovery and PYTHONPATH.
DEFAULT_SUBDIRS = ("bin", "lib", "lib64", "include", "share")


def _link_target(src, dest, relative):
    if relative:
        return os.path.relpath(src, os.path.dirname(dest))
    return src


def _merge(src, dest, view_dir, owner, result, relative, link):
    """Recursively merge *src* into *dest* (its counterpart inside the view)."""
    if os.path.isdir(src) and not os.path.islink(src):
        os.makedirs(dest, exist_ok=True)
        for child in sorted(os.listdir(src)):
            _merge(os.path.join(src, child), os.path.join(dest, child),
                   view_dir, owner, result, relative, link)
        return
    # Regular file, or any symlink (including a symlinked directory): link it
    # wholesale. First writer wins; later collisions are reported, not applied.
    relkey = os.path.relpath(dest, view_dir)
    if relkey in owner or os.path.lexists(dest):
        result["conflicts"].append((relkey, owner.get(relkey), src))
        return
    link(_link_target(src, dest, relative), dest)
    owner[relkey] = src
    result["linked"].append(relkey)


def build_view(roots, view_dir, subdirs=DEFAULT_SUBDIRS, relative=True,
               link=os.symlink):
    """Materialise a merged view of *roots* under *view_dir*.

    *roots* is an ordered list of package install prefixes; earlier entries win
    on a path collision (give the dependency closure in priority order). Only the
    *subdirs* present in each root are merged. Symlinks are relative by default so
    the view relocates with the trees it points at.

    Returns ``{"linked": [view-relative paths], "conflicts": [(path, winner_src,
    loser_src)]}``. A non-empty ``conflicts`` list means two packages provided the
    same file; the first (higher-priority) one is the one linked.
    """
    os.makedirs(view_dir, exist_ok=True)
    result = {"linked": [], "conflicts": []}
    owner = {}
    for root in roots:
        for sub in subdirs:
            src_base = os.path.join(root, sub)
            if not os.path.isdir(src_base):
                continue
            _merge(src_base, os.path.join(view_dir, sub),
                   view_dir, owner, result, relative, link)
    return result


def published_view_dirname(name, build_id):
    """Human-identifiable view directory name: ``<name>-<build_id>``."""
    return "%s-%s" % (name, build_id)


def find_published_view(store_root, build_id, architecture, views_dir="Views"):
    """Return the published view dir for *build_id* under ``<store_root>/<views_dir>``,
    or None. The view dir is named ``<name>-<build_id>`` (the publish-time *name*
    is unknown to a consumer), so it is matched by build_id suffix. *views_dir*
    defaults to ``Views`` and is overridden from the package metadata when the
    profile declares a non-default ``views_dir``.
    """
    views = os.path.join(store_root, views_dir)
    try:
        entries = os.listdir(views)
    except OSError:
        return None
    for entry in entries:
        if entry == build_id or entry.endswith("-" + build_id):
            cand = os.path.join(views, entry, architecture)
            if os.path.isdir(cand):
                return cand
    return None


def collect_build_id_roots(scan_root, build_id, architecture=None):
    """Return the install prefixes under *scan_root* whose ``.meta.json`` records
    *build_id* (and *architecture*, if given) — i.e. the packages built together.

    Each package tree has a ``.meta.json`` at its root; the walk stops descending
    once it finds one, so package contents are not rescanned.
    """
    roots = []
    for dirpath, dirs, files in os.walk(scan_root):
        if ".meta.json" not in files:
            continue
        try:
            with open(os.path.join(dirpath, ".meta.json")) as fh:
                meta = json.load(fh)
        except Exception:
            continue
        dirs[:] = []   # a package root: do not descend into its contents
        if meta.get("build_id") != build_id:
            continue
        if architecture is not None and meta.get("architecture") not in (None, architecture):
            continue
        roots.append(dirpath)
    return sorted(roots)


def build_published_view(roots, name, build_id, architecture, store_root,
                         subdirs=DEFAULT_SUBDIRS, link=os.symlink, views_dir="Views"):
    """Materialise the named per-build_id view under
    ``<store_root>/<views_dir>/<name>-<build_id>/<arch>`` and drop a CVMFS nested
    catalog at the ``<name>-<build_id>`` level.

    *roots* must already live under *store_root* (the deployed/staged tree), so the
    relative symlinks resolve once the whole tree is on CVMFS. *views_dir* (the
    prefix the release views live under, ``Views`` by default — the arch is
    appended after ``<name>-<build_id>``) comes from the profile's layout. Returns
    the :func:`build_view` result with ``view_dir`` added.
    """
    dirname = published_view_dirname(name, build_id)
    view_dir = os.path.join(store_root, views_dir, dirname, architecture)
    # Rebuild cleanly: a leftover view from a previous publish would otherwise make
    # build_view skip every pre-existing file as a conflict, yielding a stale view.
    if os.path.isdir(view_dir):
        import shutil
        shutil.rmtree(view_dir, ignore_errors=True)
    result = build_view(roots, view_dir, subdirs=subdirs, relative=True, link=link)
    catalog_dir = os.path.join(store_root, views_dir, dirname)
    os.makedirs(catalog_dir, exist_ok=True)
    open(os.path.join(catalog_dir, CATALOG_FILE), "w").close()
    result["view_dir"] = view_dir
    return result


def view_env(view_dir, lib_path_var="LD_LIBRARY_PATH", python_mm=None,
             only_existing=True):
    """Return the ``{VAR: value}`` environment for a built *view* — one entry per
    variable. With *only_existing* (default), a variable is included only when its
    backing directory exists in the view, so empty subtrees don't add dead paths.

    *lib_path_var* is ``DYLD_LIBRARY_PATH`` on macOS. *python_mm* (e.g. ``"3.13"``)
    selects the Python site-packages directory.
    """
    def joined(*parts):
        return os.pathsep.join(parts)

    candidates = {
        "PATH": [os.path.join(view_dir, "bin")],
        lib_path_var: [os.path.join(view_dir, "lib"),
                       os.path.join(view_dir, "lib64")],
        "CMAKE_PREFIX_PATH": [view_dir],
        "PKG_CONFIG_PATH": [os.path.join(view_dir, "lib", "pkgconfig"),
                            os.path.join(view_dir, "lib64", "pkgconfig")],
    }
    if python_mm:
        candidates["PYTHONPATH"] = [
            os.path.join(view_dir, "lib", "python" + python_mm, "site-packages")]

    env = {}
    for var, paths in candidates.items():
        if only_existing:
            paths = [p for p in paths if os.path.isdir(p) or p == view_dir]
        if paths:
            env[var] = joined(*paths)
    return env
