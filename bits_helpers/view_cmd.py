"""Collapse a loaded module environment onto a merged view (interactive `bits`).

Used by `bits enter`/`load`/`printenv` in *view* mode: after the modules are
loaded normally, this reads the resulting environment, builds (and caches) a
merged view of the loaded package prefixes, and emits shell that **replaces** the
long path-list variables with the view's single entries. Everything else the
modulefiles set — every ``<PKG>_ROOT``, ``ROOTSYS``, recipe ``env:`` variables —
is left untouched, since a view can only union directory contents, not setenvs.

The view is cached under a directory keyed on the exact set of loaded prefixes,
so the (potentially large) symlink farm is built once per closure and reused.
"""

import glob
import hashlib
import os
import sys

from bits_helpers.view import build_view, view_env

# Path-list variables a view collapses. PATH keeps a system tail so basic tools
# survive; the rest become pure single view entries.
READY_STAMP = ".bits_view_ready"


def collect_roots(env):
    """Return the install prefixes from ``<PKG>_ROOT`` vars in *env*.

    Each loaded modulefile exports its package root as ``<NAME>_ROOT``; the union
    of those (that still exist on disk) is exactly the loaded closure to merge.
    """
    return sorted({v for k, v in env.items()
                   if k.endswith("_ROOT") and v and os.path.isdir(v)})


def view_dir_for(roots, cache_root):
    """Deterministic cache directory for a set of *roots*."""
    key = hashlib.sha256("\n".join(roots).encode("utf-8")).hexdigest()[:16]
    return os.path.join(cache_root, key)


def ensure_view(roots, cache_root, _build=build_view):
    """Build the view for *roots* once and cache it; return its directory."""
    view_dir = view_dir_for(roots, cache_root)
    stamp = os.path.join(view_dir, READY_STAMP)
    if not os.path.exists(stamp):
        _build(roots, view_dir)
        os.makedirs(view_dir, exist_ok=True)
        with open(stamp, "w") as fh:
            fh.write("\n".join(roots))
    return view_dir


def _python_path(view_dir):
    hits = sorted(glob.glob(os.path.join(view_dir, "lib", "python*", "site-packages")))
    unversioned = os.path.join(view_dir, "lib", "python", "site-packages")
    if os.path.isdir(unversioned):
        hits.append(unversioned)
    return os.pathsep.join(hits)


def collapse_exports(env, cache_root, lib_path_var="LD_LIBRARY_PATH",
                     system_path="/usr/bin:/bin", _ensure=ensure_view):
    """Return shell ``export`` lines that replace the path-list vars with the
    view's single entries. Empty string when there is nothing to collapse.
    """
    roots = collect_roots(env)
    if not roots:
        return ""
    view_dir = _ensure(roots, cache_root)
    ve = view_env(view_dir, lib_path_var=lib_path_var)
    lines = []
    if "PATH" in ve:
        tail = (os.pathsep + system_path) if system_path else ""
        lines.append('export PATH="%s%s"' % (ve["PATH"], tail))
    for var in (lib_path_var, "CMAKE_PREFIX_PATH", "PKG_CONFIG_PATH"):
        if var in ve:
            lines.append('export %s="%s"' % (var, ve[var]))
    pythonpath = _python_path(view_dir)
    if pythonpath:
        lines.append('export PYTHONPATH="%s"' % pythonpath)
    return "\n".join(lines) + ("\n" if lines else "")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Emit shell that collapses the current loaded module "
                    "environment's path variables onto a cached merged view.")
    ap.add_argument("--cache", required=True,
                    help="Directory under which per-closure views are cached.")
    ap.add_argument("--lib-var", default="LD_LIBRARY_PATH",
                    help="Library path variable (DYLD_LIBRARY_PATH on macOS).")
    ap.add_argument("--system-path", default="/usr/bin:/bin",
                    help="System PATH tail to keep after the view's bin.")
    args = ap.parse_args(argv)
    sys.stdout.write(collapse_exports(dict(os.environ), args.cache,
                                      args.lib_var, args.system_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
