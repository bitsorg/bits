#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""``bits lcg-view`` — emit an lcgcmake-style LCG release view over a built bits
closure, so ATLAS's ``find_package(LCG <n> EXACT)`` resolves against the bits
install tree instead of an lcgcmake release.

Runs POST-build: it scans the installed packages under ``<work-dir>/<arch>`` and
reads each package's ``.meta.json`` (authoritative name/version/revision/hash +
direct runtime deps + the recorded CVMFS templates), then writes under ``<out>``::

    LCG_<num><postfix>/LCG_externals_<platform>.txt
    LCG_<num><postfix>/LCG_generators_<platform>.txt

Each externals line is ``name;hash;version;dir;deps``. AtlasLCG (LCGConfig.cmake)
uses fields 0-3 (name/id/version/dir) and sets ``<NAME>_LCGROOT`` from ``dir``.
``lcg_setup_release`` iterates the components ``externals;generators`` and sets
``LCG_FOUND=false`` if either file is MISSING, so the generators file is always
written (currently empty — every LCGROOT is set from the externals file; a proper
externals/generators split and the ``COMPILER:`` line, which AtlasLCG does not
consume, are later refinements).

``dir`` is the absolute LOCAL install prefix. Files carrying the build work dir
are relocated like any other package; paths of OTHER packages on a CVMFS publish
are only known at publish time, so a recipe that needs them rewrites the manifest
then (see lhcb.bits/lcg-view.sh).

Wiring: dispatched early in the ``bits`` entry script (like ``preload``/``cvmfs``).

NOTE (verify on a real built tree): ``.meta.json`` is assumed to live at the
install-prefix root (``<work-dir>/<arch>/<pkg>/<ver>-<rev>/.meta.json``).
"""

import argparse
import glob
import json
import os
import sys

from bits_helpers.view import build_view, view_env

DESCRIPTION = "LCG release view: LCG_externals manifest over the built closure (+ a merged view with setup.sh, --build-view)"

# Field/line delimiters the manifest and its CMake consumer (list semantics)
# reserve; a value carrying one would silently shift every subsequent list(GET).
_FORBIDDEN = (";", "\n", "\r")


def _load_meta(meta_path):
    with open(meta_path, encoding="utf-8") as handle:
        return json.load(handle)


def closure_of(meta_path):
    """{name: (version, revision)} of the dependency closure in a .meta.json."""
    deps = (_load_meta(meta_path).get("dependencies") or {}).get("recursive") or {}
    return {d["name"]: (str(d.get("version", "")), str(d.get("revision") or ""))
            for d in (deps.get("build") or []) + (deps.get("runtime") or []) if d.get("name")}


def collect(work_dir, arch, closure=None):
    """Scan ``<work_dir>/<arch>`` for installed packages (``<pkg>/<ver>`` or,
    with a family, ``<family>/<pkg>/<ver>``). With *closure* (closure_of), only
    those packages, at those versions: the work dir may hold others.

    Returns ``(records, warnings, errors)``:
      * records  — dict name -> (install_dir, meta); newest ``.meta.json`` wins
        on duplicate package names.
      * warnings — non-fatal notes (duplicate names shadowed).
      * errors   — unreadable/invalid meta files. FATAL: a dropped package is a
        missing node in the closure, so the caller must not write a partial view.
    """
    root = os.path.join(work_dir, arch)
    chosen = {}          # name -> (install_dir, meta, mtime)
    warnings = []
    errors = []
    # <pkg>/<ver>, or <family>/<pkg>/<ver> for a package with a family.
    flat = glob.glob(os.path.join(root, "*", "*", ".meta.json"))
    fam = set(glob.glob(os.path.join(root, "*", "*", "*", ".meta.json")))
    for meta_path in sorted(flat + list(fam)):
        if os.path.islink(os.path.dirname(meta_path)):
            continue   # <pkg>/latest* links: the real version dir is scanned too
        try:
            meta = _load_meta(meta_path)
            mtime = os.path.getmtime(meta_path)
        except (OSError, ValueError) as exc:
            if meta_path not in fam:
                errors.append("%s: %s" % (meta_path, exc))
            continue
        pkg = (meta.get("package") if isinstance(meta, dict) else None) or {}
        # Three deep is a package only when its family is that directory;
        # anything else is a file inside some package's own tree: ignore it.
        if meta_path in fam and pkg.get("pkg_family") != os.path.basename(
                os.path.dirname(os.path.dirname(os.path.dirname(meta_path)))):
            continue
        name, version = pkg.get("name"), pkg.get("version")
        if not name or not version:
            errors.append("%s: .meta.json has no package name/version" % meta_path)
            continue
        install_dir = os.path.dirname(meta_path)
        if closure is not None and closure.get(name) != (
                str(version), str(pkg.get("revision") or "")):
            continue
        prev = chosen.get(name)
        if prev is None or mtime > prev[2]:
            if prev is not None:
                warnings.append("duplicate package %r: using %s, ignoring %s"
                                % (name, install_dir, prev[0]))
            chosen[name] = (install_dir, meta, mtime)
        else:
            warnings.append("duplicate package %r: using %s, ignoring %s"
                            % (name, prev[0], install_dir))
    # Not under this arch (own_hash, share, reused from CVMFS) or not built here.
    for name in sorted(set(closure or ()) - set(chosen)):
        warnings.append("%s %s-%s of the closure is not under %s" % (
            (name,) + closure[name] + (root,)))
    return {n: (v[0], v[1]) for n, v in chosen.items()}, warnings, errors


def manifest_line(name, dir_path, meta):
    """Return one ``name;hash;version;dir;deps`` line. Raises ValueError if any
    delimiter-reserved character would corrupt the manifest."""
    pkg = meta["package"]
    version = pkg["version"]
    pkg_hash = pkg.get("hash", "")
    for label, value in (("name", name), ("version", version),
                         ("hash", pkg_hash), ("dir", dir_path)):
        if any(ch in str(value) for ch in _FORBIDDEN):
            raise ValueError(
                "package %r: %s %r contains ';' or a newline — cannot encode it "
                "into the manifest" % (name, label, value))
    runtime = (meta.get("dependencies") or {}).get("direct", {}).get("runtime") or []
    dep_tokens = []
    for dep in runtime:
        dep_name = dep.get("name")
        if not dep_name:
            continue
        dep_ver = dep.get("version")
        dep_tokens.append("%s-%s" % (dep_name, dep_ver) if dep_ver else dep_name)
    return "%s;%s;%s;%s;%s" % (name, pkg_hash, version, dir_path, ",".join(dep_tokens))


def _atomic_write(path, text):
    """Temp file + os.replace so a reader never sees a truncated manifest and an
    interrupted run leaves the previous file intact."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="bits overlay lcg",
        description="Emit an lcgcmake LCG release view over a built bits closure.")
    parser.add_argument("-a", "--architecture", required=True,
                        help="bits arch subtree under <work-dir> (e.g. x86_64-el9-gcc15).")
    parser.add_argument("-w", "--work-dir", dest="work_dir",
                        default=os.environ.get("BITS_WORK_DIR", "sw"),
                        help="bits work dir holding the install tree (default: %(default)s).")
    parser.add_argument("--platform", required=True,
                        help="LCG platform string for the manifest FILENAME "
                             "(e.g. x86_64-el9-gcc15-opt).")
    parser.add_argument("--version-number", dest="version_number", required=True,
                        help="LCG version number for the release dir (e.g. 110).")
    parser.add_argument("--postfix", default="",
                        help="LCG version postfix (e.g. _ATLAS_5). Default: none.")
    parser.add_argument("--out", default=".",
                        help="LCG_RELEASE_BASE root to write LCG_<num><postfix>/ under "
                             "(default: current directory).")
    parser.add_argument("--build-view", dest="build_view", default="",
                        help="Also materialise the merged symlink-farm view over the "
                             "scanned closure into this directory, with a setup.sh that "
                             "collapses PATH/LD_LIBRARY_PATH/... onto it (native "
                             "bits_helpers.view — no lcgcmake create_lcg_view needed).")
    parser.add_argument("--closure", default="",
                        help="A .meta.json (e.g. the view package's own): list only "
                             "the packages of its dependency closure.")
    parser.add_argument("--lib-path-var", dest="lib_path_var", default="LD_LIBRARY_PATH",
                        help="Loader path variable for the view setup.sh "
                             "(DYLD_LIBRARY_PATH on macOS). Default: %(default)s.")
    args = parser.parse_args(argv)

    records, warnings, errors = collect(
        args.work_dir, args.architecture,
        closure_of(args.closure) if args.closure else None)
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

    lines = []
    try:
        for name, (install_dir, meta) in records.items():
            lines.append(manifest_line(name, os.path.abspath(install_dir), meta))
    except ValueError as exc:
        sys.stderr.write("lcg-view: error: %s\n" % exc)
        return 1
    lines.sort()

    release = "LCG_%s%s" % (args.version_number, args.postfix)
    dest = os.path.join(args.out, release)
    externals = os.path.join(dest, "LCG_externals_%s.txt" % args.platform)
    generators = os.path.join(dest, "LCG_generators_%s.txt" % args.platform)
    try:
        os.makedirs(dest, exist_ok=True)
        _atomic_write(externals, "\n".join(lines) + "\n")
        _atomic_write(generators,
                      "# Generators are emitted into LCG_externals_%s.txt for now.\n"
                      "# Kept present so find_package(LCG) keeps LCG_FOUND true.\n"
                      % args.platform)
    except OSError as exc:
        sys.stderr.write("lcg-view: error: could not write the view under %s: %s\n"
                         % (dest, exc))
        return 1

    sys.stderr.write("lcg-view: wrote %d packages to %s\n" % (len(lines), externals))

    if args.build_view:
        rc = _build_view_and_setup(records, args.build_view, args.lib_path_var)
        if rc:
            return rc

    print(externals)
    return 0


def _build_view_and_setup(records, view_dir, lib_path_var):
    """Materialise the merged view over the scanned closure and write setup.sh.

    Uses the native bits view primitives (build_view + view_env): the symlink
    farm collapses PATH/LD_LIBRARY_PATH/CMAKE_PREFIX_PATH/PKG_CONFIG_PATH/
    PYTHONPATH to one entry each, exactly like an lcgcmake LCG view, without
    vendoring create_lcg_view.py."""
    # Local install prefixes, sorted for determinism (conflicts are reported).
    roots = sorted(os.path.abspath(install_dir) for install_dir, _ in records.values())
    try:
        res = build_view(roots, view_dir)
    except OSError as exc:
        sys.stderr.write("lcg-view: error: could not build the view under %s: %s\n"
                         % (view_dir, exc))
        return 1
    for path, winner, loser in res.get("conflicts", []):
        sys.stderr.write("lcg-view: view conflict on %s: kept %s, dropped %s\n"
                         % (path, winner, loser))
    # Detect the Python site-packages minor version from the merged tree.
    python_mm = None
    for _libdir in ("lib", "lib64"):
        for site in sorted(glob.glob(os.path.join(view_dir, _libdir, "python*", "site-packages"))):
            m = os.path.basename(os.path.dirname(site))  # pythonX.Y
            python_mm = m[len("python"):] or None
            break
        if python_mm:
            break
    env = view_env(view_dir, lib_path_var=lib_path_var, python_mm=python_mm)
    # setup.sh PREPENDS the view's single entries to any inherited value.
    setup = os.path.join(view_dir, "setup.sh")
    def _sh_squote(val):
        # single-quoted literal: close-quote, escaped-quote, reopen for each '
        return "'" + val.replace("'", "'\\''") + "'"
    body = ["#!/bin/bash",
            "# Auto-generated by `bits overlay lcg --build-view`: collapse the release",
            "# environment onto this merged view. Source it (after setupViews.sh).",
            "export LCG_VIEW=%s" % _sh_squote(os.path.abspath(view_dir))]
    for var in sorted(env):
        # single-quote the value; the ${VAR:+:$VAR} prepend suffix stays outside
        body.append("export %s=%s${%s:+:$%s}" % (var, _sh_squote(env[var]), var, var))
    _atomic_write(setup, "\n".join(body) + "\n")
    os.chmod(setup, 0o755)
    sys.stderr.write("lcg-view: built view (%d links) + setup.sh at %s\n"
                     % (len(res.get("linked", [])), view_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
