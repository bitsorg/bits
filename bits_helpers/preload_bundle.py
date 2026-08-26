# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""CVMFS filebundle spec emitter for the post-publish `bits preload` tool.

The tool straces a trigger binary that already lives on a deployed CVMFS tree,
so every opened path is already an absolute ``/cvmfs/<repo>.cern.ch/…`` path.
Turning that into a filebundle spec (per
https://cvmfs.readthedocs.io/en/stable/cpt-file-bundles/) is therefore just:

  * keep the opens under the repo mount (drop system files, /proc, …);
  * strip the mount to get repository-root-absolute paths (``/lcg/releases/…``);
  * emit ``<dir>/.cvmfsbundle-<trigger>`` next to the trigger, listing those
    paths as ``dependencies`` (the trigger itself excluded).

No relocation and no cross-package resolution: the deployed paths are already
final. This module is the pure, unit-testable core; the tool driver
(`preload_cmd`) handles recipe parsing, env setup, strace, tar and publish.
"""

import json
import os


SPEC_NAME = "CVMFS_BUNDLE"
SPEC_VERSION = "1.0.0"
SPEC_ENCODING = "UTF-8"


def repo_root_of(cvmfs_path):
    """The repo mount root of a ``/cvmfs/<repo>/…`` path, i.e. ``/cvmfs/<repo>``.

    E.g. ``/cvmfs/sft.cern.ch/lcg/releases`` -> ``/cvmfs/sft.cern.ch``. Returns
    None when *cvmfs_path* is not under ``/cvmfs/<repo>/``.
    """
    parts = [p for p in (cvmfs_path or "").split("/") if p]
    if len(parts) >= 2 and parts[0] == "cvmfs":
        return "/cvmfs/" + parts[1]
    return None


def to_repo_absolute(abs_path, repo_root):
    """``/cvmfs/<repo>/a/b`` -> ``/a/b``; None if *abs_path* is not under the repo.

    The result is repository-root-absolute (leading slash), exactly the form the
    filebundle spec's ``dependencies`` want.
    """
    root = (repo_root or "").rstrip("/")
    if not root or abs_path == root or not abs_path.startswith(root + "/"):
        return None
    return abs_path[len(root):]            # keeps the leading '/'


def _is_safe_rel(rel):
    """True if *rel* is a plain, in-tree relative path (no abs, no ``..``, no NUL)."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        return False
    return ".." not in rel.split("/")


def render_spec(dependencies):
    """The versioned filebundle JSON document for *dependencies* (verbatim keys)."""
    return {
        "name": SPEC_NAME,
        "version": SPEC_VERSION,
        "encoding": SPEC_ENCODING,
        "dependencies": list(dependencies),
    }


def bundle_path_for(path):
    """``<dir>/.cvmfsbundle-<base>`` for a trigger path (any form: abs or rel)."""
    d, base = os.path.split(path)
    name = ".cvmfsbundle-" + base
    return (d + "/" + name) if d else name


def build_bundle(trigger_abs, opened_abs, repo_root):
    """Build one bundle from a trigger and the files its launch opened.

    Returns ``(tar_relpath, spec_dict)`` — *tar_relpath* is the bundle's location
    relative to the repo root (no leading slash), for placement in the staging
    tar; *spec_dict* is the filebundle JSON. Returns ``(None, None)`` when the
    trigger is not under the repo or nothing under the repo was opened (no empty
    bundle). Opens outside the repo (system libs, /proc) and the trigger itself
    are excluded; the result is sorted and de-duplicated.
    """
    trig_rel = to_repo_absolute(trigger_abs, repo_root)
    if not trig_rel:
        return None, None
    deps = set()
    for p in opened_abs:
        if p == trigger_abs:
            continue
        r = to_repo_absolute(p, repo_root)
        if r:
            deps.add(r)
    if not deps:
        return None, None
    bundle_abs = bundle_path_for(trig_rel)     # '/…/bin/.cvmfsbundle-root'
    return bundle_abs.lstrip("/"), render_spec(sorted(deps))


def stage_bundle(staging_dir, tar_relpath, spec):
    """Write *spec* as JSON to ``<staging_dir>/<tar_relpath>`` (dirs created).

    *tar_relpath* must be a safe in-tree relative path. Returns the file path
    written. The staging tree mirrors the repo layout so a single tar of it
    drops each bundle next to its trigger on publish.
    """
    if not _is_safe_rel(tar_relpath):
        raise ValueError("unsafe bundle path: %r" % (tar_relpath,))
    dest = os.path.join(staging_dir, tar_relpath)
    os.makedirs(os.path.dirname(dest) or staging_dir, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, indent=2)
        fh.write("\n")
    return dest
