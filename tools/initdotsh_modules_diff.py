#!/usr/bin/env python3
"""Compare each built package's on-disk init.sh environment against the
environment produced by loading its modulefile (the "from modules" env).

Motivation (ADR-0001 follow-up / --initdotsh-from-modules): bits builds today
source a per-package build-time ``init.sh`` whose env is a *subset* of what the
runtime/development modulefile exposes — it omits CMAKE_PREFIX_PATH and the
Python site-packages PYTHONPATH, which is why ~hundreds of recipes hand-rebuild
them (bits_pythonpath_from_deps, CMakeRecipe's CMAKE_PREFIX_PATH reconstruction,
inline -DCMAKE_PREFIX_PATH). If the modulefile env is build-sufficient, those
hacks are redundant. This script measures the gap empirically, per package.

It does NOT modify anything. For each installed package it captures, in a clean
subshell:

  * the **legacy** env  — by sourcing  <tree>/etc/profile.d/init.sh
  * the **modules** env — by loading the package's modulefile (default:
    ``MODULEPATH=<work>/MODULES/<arch> modulecmd bash load <pkg>/<ver>``;
    override with --from-modules-cmd, e.g. to use ``bits printenv``)

and reports, per package and in aggregate, which *functional* variables the
modules env adds (the recipe hacks that become redundant), which it is missing
relative to init.sh (potential regressions to inspect), and which differ.

Run it on the build host where the install tree and environment-modules live;
it cannot run in a clean sandbox. Examples:

    tools/initdotsh_modules_diff.py -w sw -a el9_x86-64-gcc13
    tools/initdotsh_modules_diff.py -w sw -a el9_x86-64-gcc13 --only ROOT,vecgeom
    tools/initdotsh_modules_diff.py -w sw -a el9_x86-64-gcc13 \
        --from-modules-cmd 'bits printenv {module}'
"""

import argparse
import os
import subprocess
import sys


# Variables that carry *functional* build/runtime environment — the ones whose
# presence or absence actually affects a downstream compile/link.
FUNCTIONAL = (
    "PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "CMAKE_PREFIX_PATH",
    "PYTHONPATH", "PKG_CONFIG_PATH", "ROOT_INCLUDE_PATH", "CPATH",
    "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH",
)
# Path-like variables compared as ordered ':'-separated lists rather than scalars.
PATHLIKE = set(FUNCTIONAL) - {""}
# Per-package bookkeeping init.sh sets that the modulefile legitimately does not
# (or vice-versa) — not a functional difference, reported separately/suppressed.
BOOKKEEPING_SUFFIXES = ("_VERSION", "_REVISION", "_HASH", "_COMMIT")
BOOKKEEPING_EXACT = {"RECC_PREFIX_MAP"}
# Shell / modules machinery that is noise for this comparison.
NOISE = {
    "_", "SHLVL", "PWD", "OLDPWD", "HOME", "BASEDIR", "WORK_DIR", "ARCHITECTURE",
    "BITS_ARCH_PREFIX", "LOADEDMODULES", "_LMFILES_", "MODULEPATH", "MODULESHOME",
    "ENV", "BASH_FUNC",
}
NOISE_PREFIXES = ("MODULES_", "__MODULES", "BASH_FUNC", "_ModuleRaw")


def classify_var(name):
    """Return 'functional' | 'root' | 'bookkeeping' | 'noise' for a var name.

    'root' covers the ``<PKG>_ROOT`` / ``<PKG>_INCLUDE_DIR`` family the modulefile
    and init.sh both set — functional but per-package, grouped on its own.
    """
    if name in NOISE or any(name.startswith(p) for p in NOISE_PREFIXES):
        return "noise"
    if name in FUNCTIONAL:
        return "functional"
    if name.endswith("_ROOT") or name.endswith("_INCLUDE_DIR"):
        return "root"
    if name in BOOKKEEPING_EXACT or name.endswith(BOOKKEEPING_SUFFIXES):
        return "bookkeeping"
    return "functional"   # recipe `env:` vars (e.g. ROOTSYS) count as functional


def parse_env0(text):
    """Parse the NUL-separated output of ``env -0`` into {name: value}.

    NUL separation is used so that values containing newlines are handled.
    """
    env = {}
    for entry in text.split("\0"):
        if not entry:
            continue
        name, sep, value = entry.partition("=")
        if sep:
            env[name] = value
    return env


def _as_paths(value):
    return [p for p in value.split(":") if p]


def diff_pathlike(legacy_val, modules_val):
    """Return (added, removed) entries for a ':'-separated path var."""
    leg = _as_paths(legacy_val or "")
    mod = _as_paths(modules_val or "")
    legset, modset = set(leg), set(mod)
    added = [p for p in mod if p not in legset]
    removed = [p for p in leg if p not in modset]
    return added, removed


def diff_env(legacy, modules):
    """Compare two captured environments.

    Returns a dict with keys:
      added_functional   {var: value}     — functional var only in modules
      missing_functional {var: value}     — functional var only in legacy
      changed_path       {var: (added, removed)} — path var present in both, differs
      changed_scalar     {var: (legacy, modules)} — scalar functional var differs
    Only functional/root vars are considered; bookkeeping and noise are dropped.
    """
    def interesting(name):
        return classify_var(name) in ("functional", "root")

    names = {n for n in set(legacy) | set(modules) if interesting(n)}
    out = {"added_functional": {}, "missing_functional": {},
           "changed_path": {}, "changed_scalar": {}}
    for name in sorted(names):
        in_leg, in_mod = name in legacy, name in modules
        if in_mod and not in_leg:
            out["added_functional"][name] = modules[name]
        elif in_leg and not in_mod:
            out["missing_functional"][name] = legacy[name]
        elif legacy[name] != modules[name]:
            if name in PATHLIKE:
                added, removed = diff_pathlike(legacy[name], modules[name])
                if added or removed:
                    out["changed_path"][name] = (added, removed)
            else:
                out["changed_scalar"][name] = (legacy[name], modules[name])
    return out


def summarize(results):
    """Aggregate per-package diffs. *results* is {pkg: diff_env-dict}.

    Returns counts of packages that gain CMAKE_PREFIX_PATH / PYTHONPATH from the
    modules env (the redundant-hack signal) and packages with any functional var
    missing in the modules env (the regression signal).
    """
    gains_cmake = gains_python = missing_any = clean = 0
    regressions = {}
    for pkg, d in results.items():
        add = d["added_functional"]
        if "CMAKE_PREFIX_PATH" in add:
            gains_cmake += 1
        if "PYTHONPATH" in add:
            gains_python += 1
        miss = d["missing_functional"]
        if miss:
            missing_any += 1
            for v in miss:
                regressions[v] = regressions.get(v, 0) + 1
        if not miss and not d["changed_scalar"]:
            clean += 1
    return {
        "packages": len(results),
        "gain_cmake_prefix_path": gains_cmake,
        "gain_pythonpath": gains_python,
        "with_missing_functional": missing_any,
        "clean": clean,
        "missing_by_var": dict(sorted(regressions.items(),
                                      key=lambda kv: -kv[1])),
    }


# ── Environment capture (shells out; not unit-tested) ─────────────────────────

def _capture(command, base_env):
    """Run *command* in bash under exactly *base_env* and return its env -0 dump."""
    proc = subprocess.run(
        ["bash", "-c", command + "\nenv -0"],
        env=base_env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        universal_newlines=True)
    return parse_env0(proc.stdout)


def capture_legacy(init_sh, work_dir, arch):
    base = {"HOME": os.environ.get("HOME", "/tmp"),
            "PATH": "/usr/bin:/bin",
            "WORK_DIR": work_dir, "BITS_ARCH_PREFIX": arch}
    return _capture('. "%s" >/dev/null 2>&1 || true' % init_sh, base)


def capture_modules(module, work_dir, arch, from_modules_cmd):
    base = {"HOME": os.environ.get("HOME", "/tmp"),
            "PATH": "/usr/bin:/bin",
            "WORK_DIR": work_dir, "ARCHITECTURE": arch,
            "MODULEPATH": os.path.join(work_dir, "MODULES", arch)}
    cmd = from_modules_cmd.format(module=module)
    return _capture('eval "$(%s 2>/dev/null)" >/dev/null 2>&1 || true' % cmd, base)


def discover_packages(work_dir, arch):
    """Yield (module_name, init_sh_path) for every installed package that ships a
    runtime init.sh, where module_name is ``<pkg>/<ver_rev>`` (as the modulefile
    cache names it). Handles both ``<arch>/<pkg>/<ver>`` and family-nested
    ``<arch>/<family>/<pkg>/<ver>`` layouts.
    """
    root = os.path.join(work_dir, arch)
    found = []
    for dirpath, _dirs, files in os.walk(root):
        if dirpath.endswith(os.path.join("etc", "profile.d")) and "init.sh" in files:
            # <...>/<pkg>/<ver_rev>/etc/profile.d
            ver_rev = os.path.basename(os.path.dirname(os.path.dirname(dirpath)))
            pkg = os.path.basename(os.path.dirname(os.path.dirname(
                os.path.dirname(dirpath))))
            found.append(("%s/%s" % (pkg, ver_rev),
                          os.path.join(dirpath, "init.sh")))
    return sorted(set(found))


def _fmt_diff(pkg, d):
    lines = []
    for var, val in d["added_functional"].items():
        lines.append("    + %-20s (modules adds; init.sh lacked)  %s" % (var, val))
    for var, val in d["missing_functional"].items():
        lines.append("    - %-20s (init.sh had; modules lacks)    %s" % (var, val))
    for var, (added, removed) in d["changed_path"].items():
        if added:
            lines.append("    ~ %-20s +%s" % (var, ":".join(added)))
        if removed:
            lines.append("    ~ %-20s -%s" % (var, ":".join(removed)))
    for var, (leg, mod) in d["changed_scalar"].items():
        lines.append("    ~ %-20s %r -> %r" % (var, leg, mod))
    header = "%s" % pkg if lines else "%s  (functional env matches)" % pkg
    return header + ("\n" + "\n".join(lines) if lines else "")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-w", "--work-dir", default=os.environ.get("WORK_DIR", "sw"),
                    help="bits work directory (default: %(default)s).")
    ap.add_argument("-a", "--architecture", required=True,
                    help="Architecture subdir under the work dir (e.g. el9_x86-64-gcc13).")
    ap.add_argument("--only", default=None,
                    help="Comma-separated package names to restrict to (e.g. ROOT,vecgeom).")
    ap.add_argument("--from-modules-cmd",
                    default="modulecmd bash load {module}",
                    help="Command (with {module}) that emits the module env as shell. "
                         "Default uses modulecmd + MODULEPATH; alt: 'bits printenv {module}'.")
    ap.add_argument("--quiet-matches", action="store_true",
                    help="Only print packages with a functional difference.")
    args = ap.parse_args(argv)

    pkgs = discover_packages(args.work_dir, args.architecture)
    if not pkgs:
        sys.exit("No installed packages with init.sh found under %s/%s"
                 % (args.work_dir, args.architecture))
    if args.only:
        wanted = {p.strip() for p in args.only.split(",") if p.strip()}
        pkgs = [(m, p) for (m, p) in pkgs if m.split("/")[0] in wanted]

    results = {}
    for module, init_sh in pkgs:
        legacy = capture_legacy(init_sh, args.work_dir, args.architecture)
        modules = capture_modules(module, args.work_dir, args.architecture,
                                  args.from_modules_cmd)
        d = diff_env(legacy, modules)
        results[module] = d
        differs = any(d[k] for k in
                      ("added_functional", "missing_functional",
                       "changed_path", "changed_scalar"))
        if differs or not args.quiet_matches:
            print(_fmt_diff(module, d))

    s = summarize(results)
    print("\n" + "=" * 64)
    print("Summary over %d package(s):" % s["packages"])
    print("  gain CMAKE_PREFIX_PATH from modules : %d  (CMakeRecipe rebuild redundant)"
          % s["gain_cmake_prefix_path"])
    print("  gain PYTHONPATH from modules        : %d  (bits_pythonpath_from_deps redundant)"
          % s["gain_pythonpath"])
    print("  functional var MISSING in modules   : %d  (inspect before rebuild)"
          % s["with_missing_functional"])
    print("  functional env already matches      : %d" % s["clean"])
    if s["missing_by_var"]:
        print("  missing-in-modules by var: " +
              ", ".join("%s×%d" % (v, n) for v, n in s["missing_by_var"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
