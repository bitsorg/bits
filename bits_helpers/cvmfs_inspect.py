# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read-only inspection of a deployed CVMFS bits tree: discover platforms, mark
host compatibility, and present a package's build/provenance from .meta.json
without jq. Prototype — runs standalone as::

    python3 -m bits_helpers.cvmfs_inspect platforms --cvmfs <root>
    python3 -m bits_helpers.cvmfs_inspect show Davix --cvmfs <root> --arch <a> --deps
    python3 -m bits_helpers.cvmfs_inspect summary --cvmfs <root> --arch <a>

`<root>` is the directory holding the per-arch trees, i.e. it contains
``<arch>/Packages/<pkg>/<verrev>/.meta.json``.
"""

import argparse
import json
import os
import sys

try:
    from bits_helpers.arch import (
        detectArchComponents, arch_machine_token, arch_distro_token)
except Exception:                       # standalone / partial import fallback
    detectArchComponents = None
    arch_machine_token = arch_distro_token = lambda s: None

# Compatibility marks (state -> glyph); ASCII-safe fallbacks are easy to add.
_MARK = {"native": "✓", "container": "⚠", "incompatible": "✗"}


# ── discovery ────────────────────────────────────────────────────────────────

def list_platforms(cvmfs_root):
    """<arch> dirs under *cvmfs_root* that actually hold a Packages/ tree."""
    out = []
    try:
        for name in sorted(os.listdir(cvmfs_root)):
            if os.path.isdir(os.path.join(cvmfs_root, name, "Packages")):
                out.append(name)
    except OSError:
        pass
    return out


def _packages_root(cvmfs_root, arch):
    return os.path.join(cvmfs_root, arch, "Packages")


def list_packages(cvmfs_root, arch):
    """{pkg: [verrev, ...]} under <arch>/Packages (sorted)."""
    root = _packages_root(cvmfs_root, arch)
    out = {}
    try:
        for pkg in sorted(os.listdir(root)):
            pdir = os.path.join(root, pkg)
            if not os.path.isdir(pdir):
                continue
            out[pkg] = sorted(v for v in os.listdir(pdir)
                              if os.path.isdir(os.path.join(pdir, v)))
    except OSError:
        pass
    return out


def resolve_verrev(cvmfs_root, arch, pkg, ver=None):
    """Pick a verrev: prefix-match *ver* if given, else the newest present."""
    vers = list_packages(cvmfs_root, arch).get(pkg, [])
    if not vers:
        return None
    if ver:
        for v in vers:
            if v == ver or v.startswith(ver + "-"):
                return v
        return None
    return vers[-1]


def read_meta(cvmfs_root, arch, pkg, verrev):
    with open(os.path.join(_packages_root(cvmfs_root, arch), pkg, verrev,
                           ".meta.json")) as fh:
        return json.load(fh)


def _legacy_meta(arch, pkg, verrev):
    """Minimal record inferred from the path for a pre-bits tree with no
    .meta.json. Best-effort guess from directory names, marked as legacy; only
    name, architecture, and the verrev dir (kept in `version`) are real —
    everything else is unknown/blank."""
    return {
        "architecture": arch, "abi_tag": "", "build_id": "",
        "provenance": "legacy (no .meta.json)", "reuse_policy": "",
        "defaults": [], "bits_version": "", "dist": {},
        "package": {"name": pkg, "version": verrev, "revision": "", "hash": ""},
        "dependencies": {"direct": {"build": [], "runtime": []},
                         "recursive": {"build": [], "runtime": []}},
        "_legacy": True,
    }


def meta_or_legacy(cvmfs_root, arch, pkg, verrev):
    """Read .meta.json; when it is simply absent, synthesize a legacy record from
    the path. `lexists` so only a truly-missing file is legacy: a present-but-
    unreadable file, or a dangling symlink, still raises (open fails) and is
    surfaced as an error rather than masked as legacy."""
    path = os.path.join(_packages_root(cvmfs_root, arch), pkg, verrev, ".meta.json")
    if not os.path.lexists(path):
        return _legacy_meta(arch, pkg, verrev)
    with open(path) as fh:
        return json.load(fh)


# ── host compatibility (advisory, three-state) ───────────────────────────────

def _norm_machine(tok):
    return (tok or "").replace("-", "_")


def classify_platform(arch, host=None):
    """(state, note) with state in {native, container, incompatible}.

    Honest and layered: machine must match (else incompatible); a differing distro
    token means the binaries were built for another OS/toolchain and want a
    matching container (the gcc15-in-alma9 lesson), not the bare host.
    """
    host = host or (detectArchComponents() if detectArchComponents else {})
    h_machine = _norm_machine(host.get("_machine") or host.get("machine"))
    h_os = host.get("os") or ""
    p_machine = _norm_machine(arch_machine_token(arch))
    p_os = arch_distro_token(arch) or ""
    if p_machine and h_machine and p_machine != h_machine:
        return "incompatible", "needs %s (host is %s)" % (p_machine, h_machine)
    if p_os and h_os and p_os != h_os:
        return "container", ("built for %s; host is %s — run in a matching image"
                             % (p_os, h_os))
    return "native", "machine/OS match host"


# ── presentation ─────────────────────────────────────────────────────────────

def _verrev(pkg):
    """version-revision, dropping the dash when there is no revision (legacy)."""
    ver, rev = pkg.get("version", "?"), pkg.get("revision", "")
    return "%s-%s" % (ver, rev) if rev else ver


def format_meta(meta, deps=False, provenance_only=False):
    pkg = meta.get("package", {}) or {}
    L = ["%s  %s" % (pkg.get("name", "?"), _verrev(pkg))]
    L.append("  hash:         %s" % pkg.get("hash", ""))
    L.append("  architecture: %s" % meta.get("architecture", ""))
    L.append("  abi_tag:      %s" % meta.get("abi_tag", ""))
    L.append("  build_id:     %s" % meta.get("build_id", ""))
    L.append("  provenance:   %s" % meta.get("provenance", ""))
    L.append("  reuse_policy: %s" % meta.get("reuse_policy", ""))
    L.append("  defaults:     %s" % " ".join(meta.get("defaults", []) or []))
    L.append("  bits:         %s   dist %s"
             % (meta.get("bits_version", ""),
                ((meta.get("dist") or {}).get("commit", "") or "")[:12]))
    if provenance_only:
        return "\n".join(L)
    if deps:
        d = meta.get("dependencies", {}) or {}
        for scope in ("direct", "recursive"):
            sd = d.get(scope, {}) or {}
            for kind in ("build", "runtime"):
                items = sd.get(kind, []) or []
                if items:
                    L.append("  %s %s (%d):" % (scope, kind, len(items)))
                    for it in items:
                        L.append("    - %s %s-%s" % (it.get("name"),
                                 it.get("version"), it.get("revision")))
    return "\n".join(L)


# ── summary ──────────────────────────────────────────────────────────────────

def summarize(cvmfs_root, arch):
    """(pkgs, build_ids): pkgs={pkg:[verrev]}, build_ids={bid:[pkg/verrev]}."""
    pkgs = list_packages(cvmfs_root, arch)
    build_ids = {}
    for pkg, vers in pkgs.items():
        for v in vers:
            try:
                m = read_meta(cvmfs_root, arch, pkg, v)
            except (OSError, ValueError):
                continue
            build_ids.setdefault(m.get("build_id") or "(none)", []).append(
                "%s/%s" % (pkg, v))
    return pkgs, build_ids


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cmd_platforms(a):
    plats = list_platforms(a.cvmfs)
    if a.json:
        print(json.dumps([{"arch": p, "state": classify_platform(p)[0]}
                          for p in plats], indent=2))
        return 0
    if not plats:
        print("no platforms found under %s" % a.cvmfs); return 1
    for p in plats:
        state, note = classify_platform(p)
        print("  %s  %-32s %s" % (_MARK.get(state, "?"), p, note))
    print("\n  %s native   %s needs container   %s incompatible"
          % (_MARK["native"], _MARK["container"], _MARK["incompatible"]))
    return 0


def _show_one(a, arch, pkg, ver, verrev=None):
    """Full build/provenance detail for one platform."""
    verrev = verrev or resolve_verrev(a.cvmfs, arch, pkg, ver)
    if not verrev:
        print("%s%s not found under %s/%s/Packages"
              % (pkg, "/" + ver if ver else "", a.cvmfs, arch)); return 1
    try:
        meta = meta_or_legacy(a.cvmfs, arch, pkg, verrev)
    except (OSError, ValueError) as exc:
        print("cannot read %s/%s/%s/.meta.json: %s" % (arch, pkg, verrev, exc))
        return 1
    if a.json:
        print(json.dumps(meta, indent=2)); return 0
    print(format_meta(meta, deps=a.deps, provenance_only=a.provenance))
    if meta.get("_legacy"):
        print("  (legacy tree: no .meta.json — fields inferred from the path)")
    return 0


def _show_across(a, pkg, hits):
    """One compact line per platform that has *pkg*; --arch drills into detail."""
    rows = []
    for arch, verrev in hits:
        try:
            m = meta_or_legacy(a.cvmfs, arch, pkg, verrev)
        except (OSError, ValueError):
            # Present but unreadable/corrupt — keep it visible, don't drop it.
            rows.append((arch, verrev, "", "", "unreadable .meta.json"))
            continue
        p = m.get("package", {}) or {}
        rows.append((arch, _verrev(p),
                     m.get("build_id", "") or "", (p.get("hash", "") or "")[:8],
                     m.get("provenance", "") or ""))
    if a.json:
        print(json.dumps([{"arch": r[0], "verrev": r[1], "build_id": r[2],
                           "hash": r[3], "provenance": r[4]} for r in rows], indent=2))
        return 0
    print("%s — %d platform%s" % (pkg, len(rows), "" if len(rows) == 1 else "s"))
    for arch, verrev, bid, h, prov in rows:
        print("  %-30s %-10s %-38s %-8s %s" % (arch, verrev, bid, h, prov))
    print("  (add --arch <platform> for full build/provenance + --deps)")
    return 0


def _cmd_show(a):
    pkg, _, ver = a.package.partition("/")
    ver = ver or None
    if a.arch:
        return _show_one(a, a.arch, pkg, ver)
    # No --arch: don't silently pick the first platform — show the package on
    # every platform that has it, so provenance across the tree is visible.
    hits = []
    for arch in list_platforms(a.cvmfs):
        verrev = resolve_verrev(a.cvmfs, arch, pkg, ver)
        if verrev:
            hits.append((arch, verrev))
    if not hits:
        print("%s%s not found on any platform under %s"
              % (pkg, "/" + ver if ver else "", a.cvmfs)); return 1
    # JSON keeps one stable shape for no-arch — always the per-platform list,
    # regardless of hit count. Text shows full detail when there's a single hit.
    if not a.json and len(hits) == 1:
        return _show_one(a, hits[0][0], pkg, ver, hits[0][1])
    return _show_across(a, pkg, hits)


def _cmd_summary(a):
    arch = a.arch or (list_platforms(a.cvmfs) or [None])[0]
    if not arch:
        print("no arch (pass --arch)"); return 1
    pkgs, build_ids = summarize(a.cvmfs, arch)
    if a.json:
        print(json.dumps({"arch": arch, "packages": len(pkgs),
                          "build_ids": {b: len(v) for b, v in build_ids.items()}},
                         indent=2)); return 0
    print("Platform %s: %d packages, %d build_id(s)"
          % (arch, len(pkgs), len(build_ids)))
    for bid, members in sorted(build_ids.items()):
        print("  %s  (%d packages)" % (bid, len(members)))
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)

    # Producer-side ops are folded into this group (`bits cvmfs stage|publish`).
    # They have their own arg sets (no --cvmfs), so dispatch before argparse.
    if argv and argv[0] == "stage":
        from bits_helpers.cvmfs_stage_cmd import main as _stage_main
        return _stage_main(argv[1:])
    if argv and argv[0] == "publish":
        from bits_helpers.cvmfs_publish import main as _publish_main
        return _publish_main(argv[1:])

    # Shared options live on a parent parser so they work AFTER the subcommand
    # too (`bits cvmfs platforms --cvmfs ROOT`), not only before it.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cvmfs", required=True,
                        help="root holding <arch>/Packages/<pkg>/<verrev>/.meta.json")
    common.add_argument("--json", action="store_true", help="machine-readable output")

    ap = argparse.ArgumentParser(
        prog="bits cvmfs",
        description="Inspect a deployed CVMFS bits tree.",
        epilog="producer-side ops (own --help): stage, publish "
               "(e.g. `bits cvmfs stage --help`).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("platforms", parents=[common],
                   help="list platforms + host compatibility")
    sp = sub.add_parser("show", parents=[common],
                        help="print a package's build/provenance")
    sp.add_argument("package", help="PKG or PKG/VERSION")
    sp.add_argument("--arch", help="platform (default: all platforms that have it)")
    sp.add_argument("--deps", action="store_true", help="include dependency tree")
    sp.add_argument("--provenance", action="store_true", help="provenance fields only")
    ss = sub.add_parser("summary", parents=[common],
                        help="per-platform package + build_id summary")
    ss.add_argument("--arch", help="platform (default: first found)")
    a = ap.parse_args(argv)
    return {"platforms": _cmd_platforms, "show": _cmd_show,
            "summary": _cmd_summary}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
