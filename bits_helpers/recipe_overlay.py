# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Recipe overlays: a directory of edited recipes and defaults profiles that a
build searches before every recipe repository (``bits build --recipe-overlay DIR``, or
``$BITS_RECIPE_OVERLAY``), to try changes, such as an upgrade (``bits upgrade``), before
they go into the repositories.

An overlay file keeps its origin: its patches are looked up in the overlay's
``patches/`` first, then next to the recipe it shadows, and a profile keeps its
repository's layout and checksums. ``overlay.yaml`` records, per file, the
repository, path and commit it was copied from.

A build made with an overlay is a trial: it never uploads to a store, and its
packages are never published, certified nor uploaded later (``refuse``).
"""

import hashlib
import os

ENV = "BITS_RECIPE_OVERLAY"
INDEX = "overlay.yaml"


def directory():
    """The active overlay directory (absolute), or ""."""
    d = os.environ.get(ENV, "").strip()
    return os.path.abspath(d) if d else ""


def activate(path):
    """Make *path* the active overlay; returns its absolute path. Raises
    ValueError when it is not an overlay (no overlay.yaml)."""
    d = os.path.abspath(path)
    if not os.path.isfile(os.path.join(d, INDEX)):
        raise ValueError("%s is not a recipe overlay (no %s)" % (path, INDEX))
    os.environ[ENV] = d
    return d


def inside(path):
    """Is *path* a file of the active overlay?"""
    d = directory()
    return bool(d) and os.path.abspath(path).startswith(d + os.sep)


def home(path, search_dirs):
    """The directory an overlay file stands for: the next of *search_dirs*
    holding a file of the same name (as spelt there: it keys other tables), else
    the overlay itself. Any other path: its own directory."""
    if not inside(path):
        return os.path.dirname(os.path.abspath(path))
    name, d = os.path.basename(path), directory()
    for s in search_dirs:
        if os.path.abspath(s) != d and os.path.exists(os.path.join(s, name)):
            return s
    return d


def patch_file(spec, name):
    """The file of patch *name* of *spec*: the overlay's, for a recipe read from
    the overlay that has one, else the recipe repository's patches/."""
    d = directory()
    if d and spec.get("recipe_overlay"):
        mine = os.path.join(d, "patches", name)
        if os.path.isfile(mine):
            return mine
    return os.path.join(spec.get("pkgdir", ""), "patches", name)


def files_of(d):
    """{relative path: sha256} of the files of overlay *d* (not .git)."""
    out = {}
    for root, dirs, names in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x != ".git")
        for n in sorted(names):
            p = os.path.join(root, n)
            with open(p, "rb") as f:
                out[os.path.relpath(p, d)] = hashlib.sha256(f.read()).hexdigest()
    return out


def files():
    """files_of the active overlay, for the build manifest."""
    d = directory()
    return files_of(d) if d else {}


def is_trial(manifest):
    """Was the build *manifest* (a dict: a manifest, a BOM or one package entry)
    made with a recipe overlay?"""
    if not isinstance(manifest, dict):
        return False
    entries = manifest.get("packages") if isinstance(manifest.get("packages"), list) else []
    return bool(manifest.get("recipe_overlay")
                or any(isinstance(e, dict) and e.get("recipe_overlay") for e in entries))


def refuse(manifest, what):
    """An error message if *manifest* is a trial's (is_trial): *what* is not for trials."""
    if not is_trial(manifest):
        return None
    return ("refusing to %s: this build used a recipe overlay (a trial build); "
            "build again without --recipe-overlay once its changes are in the recipe "
            "repositories" % what)
