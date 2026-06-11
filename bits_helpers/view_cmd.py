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

import hashlib
import os
import sys

from bits_helpers.view import build_view

READY_STAMP = ".bits_view_ready"

# The additive, colon-separated path variables a view collapses. Each entry that
# lives under a loaded package prefix is remapped onto the merged view and
# deduplicated; entries outside any prefix (system dirs) are kept in place. These
# are *only* the list-valued vars — single-valued setenvs like ROOTSYS,
# <PKG>_ROOT and <PKG>_INCLUDE_DIR are deliberately left untouched, because
# software uses them to locate package-relative files (e.g. $ROOTSYS/etc) that
# the view does not merge.
COLLAPSE_VARS = (
    "PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "CMAKE_PREFIX_PATH",
    "PKG_CONFIG_PATH", "PYTHONPATH", "ROOT_INCLUDE_PATH", "CPATH",
)


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


def _remap_entry(entry, roots_longest_first, view_dir):
    """Map a single path entry onto the view: if it lives under a package root,
    replace that root with *view_dir*; otherwise (a system/foreign dir) return it
    unchanged. Roots are matched longest-first so a prefix root can't shadow a
    deeper one.
    """
    for root in roots_longest_first:
        if entry == root:
            return view_dir
        if entry.startswith(root + os.sep):
            return view_dir + entry[len(root):]
    return entry


def collapse_exports(env, cache_root, _ensure=ensure_view):
    """Return shell ``export`` lines that collapse the additive path variables
    onto a merged view of the loaded closure. Empty string when there are no
    package roots in *env*.

    Each variable's entries are remapped through the view and de-duplicated, so a
    variable that listed every dependency's directory becomes a single view entry
    (plus any system directories, kept in place). Set-valued environment is left
    entirely alone — the caller keeps the modulefiles' setenvs.
    """
    roots = collect_roots(env)
    if not roots:
        return ""
    view_dir = _ensure(roots, cache_root)
    roots_longest_first = sorted(roots, key=len, reverse=True)
    lines = []
    for var in COLLAPSE_VARS:
        if var not in env or not env[var]:
            continue
        out, seen = [], set()
        for entry in env[var].split(os.pathsep):
            if not entry:
                continue
            mapped = _remap_entry(entry, roots_longest_first, view_dir)
            if mapped not in seen:
                seen.add(mapped)
                out.append(mapped)
        if out:
            lines.append('export %s="%s"' % (var, os.pathsep.join(out)))
    return "\n".join(lines) + ("\n" if lines else "")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Emit shell that collapses the current loaded module "
                    "environment's path variables onto a cached merged view.")
    ap.add_argument("--cache", required=True,
                    help="Directory under which per-closure views are cached.")
    args = ap.parse_args(argv)
    sys.stdout.write(collapse_exports(dict(os.environ), args.cache))
    return 0


if __name__ == "__main__":
    sys.exit(main())
