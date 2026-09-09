#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""``bits lcg-view`` — emit an lcgcmake-style LCG release view over a built bits
closure, so ATLAS's ``find_package(LCG <n> EXACT)`` resolves against the bits
install tree instead of an lcgcmake release.

Runs POST-build: it scans the installed packages under ``<work-dir>/<arch>`` and
reads each package's ``.meta.json`` (authoritative name/version/revision/hash +
direct runtime deps, written by build.create_provenance_info), then writes under
``<out>``::

    LCG_<num><postfix>/LCG_externals_<platform>.txt
    LCG_<num><postfix>/LCG_generators_<platform>.txt

Each externals line is ``name;hash;version;dir;deps`` where ``dir`` is the
absolute local install prefix and ``deps`` is a comma-joined ``name-version``
list. AtlasLCG (LCGConfig.cmake) uses fields 0-3 (name/id/version/dir) and sets
``<NAME>_LCGROOT`` from ``dir``. ``lcg_setup_release`` iterates the components
``externals;generators`` and sets ``LCG_FOUND=false`` if either file is MISSING,
so the generators file is always written (currently empty — every LCGROOT is set
from the externals file; a proper externals/generators split is a later
refinement, as is the ``COMPILER:`` line, which AtlasLCG does not consume).

The view records absolute LOCAL install paths and is therefore machine/CWD-bound
— it is the local, pre-publish view. A relocatable/CVMFS view is the (not yet
implemented) ``--cvmfs`` mode.

Wiring: dispatched early in the ``bits`` entry script (like ``preload``/``cvmfs``)
because it needs none of the build/defaults argument machinery.

NOTE (verify on a real built tree): ``.meta.json`` is assumed to live at the
install-prefix root (``<work-dir>/<arch>/<pkg>/<ver>-<rev>/.meta.json``). Confirm
the path/depth against a real build host tree and adjust the glob if it differs.
"""

import argparse
import glob
import json
import os
import sys

# Field/line delimiters the manifest and its CMake consumer (list semantics)
# reserve; a value carrying one would silently shift every subsequent list(GET).
_FORBIDDEN = (";", "\n", "\r")


def _load_meta(meta_path):
    with open(meta_path, encoding="utf-8") as handle:
        return json.load(handle)


def collect(work_dir, arch):
    """Scan ``<work_dir>/<arch>`` for installed packages.

    Returns ``(records, warnings, errors)``:
      * records  — dict name -> (install_dir, meta); on duplicate package names
        the newest ``.meta.json`` (by mtime) wins.
      * warnings — non-fatal notes (e.g. duplicate names shadowed).
      * errors   — unreadable/invalid meta files. These are FATAL: a dropped
        package is a missing node in the build closure, so the caller must not
        write a view that silently omits it.
    """
    root = os.path.join(work_dir, arch)
    chosen = {}          # name -> (install_dir, meta, mtime, meta_path)
    warnings = []
    errors = []
    for meta_path in sorted(glob.glob(os.path.join(root, "*", "*", ".meta.json"))):
        try:
            meta = _load_meta(meta_path)
            mtime = os.path.getmtime(meta_path)
        except (OSError, ValueError) as exc:
            errors.append("%s: %s" % (meta_path, exc))
            continue
        pkg = meta.get("package") or {}
        name, version = pkg.get("name"), pkg.get("version")
        if not name or not version:
            errors.append("%s: .meta.json has no package name/version" % meta_path)
            continue
        install_dir = os.path.dirname(meta_path)
        prev = chosen.get(name)
        if prev is None:
            chosen[name] = (install_dir, meta, mtime, meta_path)
        else:
            keep, drop = ((install_dir, meta, mtime, meta_path), prev) \
                if mtime > prev[2] else (prev, (install_dir, meta, mtime, meta_path))
            chosen[name] = keep
            warnings.append("duplicate package %r: using %s, ignoring %s"
                            % (name, keep[0], drop[0]))
    records = {name: (v[0], v[1]) for name, v in chosen.items()}
    return records, warnings, errors


def manifest_line(name, install_dir, meta):
    """Return one ``name;hash;version;dir;deps`` line. Raises ValueError if any
    delimiter-reserved character would corrupt the manifest."""
    pkg = meta["package"]
    version = pkg["version"]
    pkg_hash = pkg.get("hash", "")
    prefix = os.path.abspath(install_dir)
    for label, value in (("name", name), ("version", version),
                         ("hash", pkg_hash), ("dir", prefix)):
        if any(ch in str(value) for ch in _FORBIDDEN):
            raise ValueError(
                "package %r: %s %r contains ';' or a newline — cannot encode "
                "it into the manifest" % (name, label, value))
    runtime = (meta.get("dependencies") or {}).get("direct", {}).get("runtime") or []
    dep_tokens = []
    for dep in runtime:
        dep_name = dep.get("name")
        if not dep_name:
            continue
        dep_ver = dep.get("version")
        dep_tokens.append("%s-%s" % (dep_name, dep_ver) if dep_ver else dep_name)
    return "%s;%s;%s;%s;%s" % (name, pkg_hash, version, prefix, ",".join(dep_tokens))


def _atomic_write(path, text):
    """Write via a temp file + os.replace so a reader never sees a truncated or
    half-written manifest (and an interrupted run leaves the old file intact)."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="bits lcg-view",
        description="Emit an lcgcmake LCG release view over a built bits closure.")
    parser.add_argument("-a", "--architecture", required=True,
                        help="bits arch subtree under <work-dir> (e.g. x86_64-el9-gcc15).")
    parser.add_argument("-w", "--work-dir", dest="work_dir",
                        default=os.environ.get("BITS_WORK_DIR", "sw"),
                        help="bits work dir holding the install tree (default: %(default)s).")
    parser.add_argument("--platform", required=True,
                        help="LCG platform string for the manifest filename "
                             "(e.g. x86_64-el9-gcc15-opt).")
    parser.add_argument("--version-number", dest="version_number", required=True,
                        help="LCG version number for the release dir (e.g. 110).")
    parser.add_argument("--postfix", default="",
                        help="LCG version postfix (e.g. _ATLAS_5). Default: none.")
    parser.add_argument("--out", default=".",
                        help="LCG_RELEASE_BASE root to write LCG_<num><postfix>/ under "
                             "(default: current directory).")
    parser.add_argument("--cvmfs", action="store_true",
                        help="(not implemented) emit relocatable CVMFS paths.")
    args = parser.parse_args(argv)

    if args.cvmfs:
        sys.stderr.write("lcg-view: --cvmfs is not implemented yet — refusing rather "
                         "than emit local paths under a CVMFS request.\n")
        return 2

    records, warnings, errors = collect(args.work_dir, args.architecture)
    for warning in warnings:
        sys.stderr.write("lcg-view: warning: %s\n" % warning)
    if errors:
        for err in errors:
            sys.stderr.write("lcg-view: error: %s\n" % err)
        sys.stderr.write("lcg-view: refusing to write a partial view (%d unreadable "
                         ".meta.json) — the closure would be incomplete.\n" % len(errors))
        return 1
    if not records:
        sys.stderr.write("lcg-view: no installed packages with .meta.json under %s/%s\n"
                         % (args.work_dir, args.architecture))
        return 1

    try:
        lines = sorted(manifest_line(name, install_dir, meta)
                       for name, (install_dir, meta) in records.items())
    except ValueError as exc:
        sys.stderr.write("lcg-view: error: %s\n" % exc)
        return 1

    release = "LCG_%s%s" % (args.version_number, args.postfix)
    dest = os.path.join(args.out, release)
    externals = os.path.join(dest, "LCG_externals_%s.txt" % args.platform)
    generators = os.path.join(dest, "LCG_generators_%s.txt" % args.platform)
    try:
        os.makedirs(dest, exist_ok=True)
        _atomic_write(externals, "\n".join(lines) + "\n")
        # Both component files must exist or lcg_setup_release sets LCG_FOUND=false;
        # generators are currently emitted into the externals file.
        _atomic_write(generators,
                      "# Generators are emitted into LCG_externals_%s.txt for now.\n"
                      "# Kept present so find_package(LCG) keeps LCG_FOUND true.\n"
                      % args.platform)
    except OSError as exc:
        sys.stderr.write("lcg-view: error: could not write the view under %s: %s\n"
                         % (dest, exc))
        return 1

    sys.stderr.write("lcg-view: wrote %d packages to %s\n" % (len(lines), externals))
    print(externals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
