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
    """Build the view for *roots* once and cache it; return its directory.

    The ready-stamp's mtime is refreshed on every use, so it doubles as a
    last-used marker for age-based garbage collection (see :func:`prune_views`).
    """
    view_dir = view_dir_for(roots, cache_root)
    stamp = os.path.join(view_dir, READY_STAMP)
    if not os.path.exists(stamp):
        _build(roots, view_dir)
        os.makedirs(view_dir, exist_ok=True)
        with open(stamp, "w") as fh:
            fh.write("\n".join(roots))
    else:
        os.utime(stamp, None)   # touch: mark recently used
    return view_dir


def prune_views(cache_root, ttl_days, now=None):
    """Garbage-collect cached views not used within *ttl_days* days.

    "Used" = the ready-stamp's mtime, refreshed by :func:`ensure_view` on every
    access, so a view that is still being entered survives. A view dir without a
    ready-stamp is skipped (it may be mid-build). ``ttl_days <= 0`` disables GC.
    Returns the list of removed view names. Best-effort: errors are ignored.
    """
    import shutil
    import time
    if not ttl_days or ttl_days <= 0:
        return []
    cutoff = (now if now is not None else time.time()) - ttl_days * 86400.0
    removed = []
    try:
        names = os.listdir(cache_root)
    except OSError:
        return []
    for name in names:
        stamp = os.path.join(cache_root, name, READY_STAMP)
        try:
            used = os.path.getmtime(stamp)
        except OSError:
            continue   # no stamp → incomplete / not a view; leave it
        if used < cutoff:
            shutil.rmtree(os.path.join(cache_root, name), ignore_errors=True)
            removed.append(name)
    return removed


def _assign(var, value, shell):
    """Render a shell assignment for *var*. ``csh`` → setenv, otherwise sh-family."""
    if shell == "csh":
        return 'setenv %s "%s";' % (var, value)
    return 'export %s="%s"' % (var, value)


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


def collapse_exports(env, cache_root, shell="sh", _ensure=ensure_view):
    """Return shell assignments that collapse the additive path variables onto a
    merged view of the loaded closure. Empty string when there are no package
    roots in *env*.

    Each variable's entries are remapped through the view and de-duplicated, so a
    variable that listed every dependency's directory becomes a single view entry
    (plus any system directories, kept in place). Set-valued environment is left
    entirely alone — the caller keeps the modulefiles' setenvs. *shell* selects
    the output syntax (``sh`` export vs ``csh`` setenv) for `printenv`/`load`.
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
            lines.append(_assign(var, os.pathsep.join(out), shell))
    return "\n".join(lines) + ("\n" if lines else "")


# Views unused for this many days are garbage-collected on the next view-mode
# invocation. Overridable via $BITS_VIEW_TTL_DAYS (0 disables GC).
DEFAULT_TTL_DAYS = 7


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(
        description="Emit shell that collapses the current loaded module "
                    "environment's path variables onto a cached merged view, and "
                    "garbage-collect stale views.")
    ap.add_argument("--cache", required=True,
                    help="Directory under which per-closure views are cached.")
    ap.add_argument("--shell", default="sh", choices=["sh", "csh"],
                    help="Output syntax: sh export (default) or csh setenv.")
    ap.add_argument("--gc-only", action="store_true",
                    help="Only garbage-collect stale views; emit nothing.")
    ap.add_argument("--ttl-days", type=float,
                    default=float(os.environ.get("BITS_VIEW_TTL_DAYS",
                                                 DEFAULT_TTL_DAYS)),
                    help="GC views unused for this many days (0 disables).")
    args = ap.parse_args(argv)
    if not args.gc_only:
        sys.stdout.write(collapse_exports(dict(os.environ), args.cache, args.shell))
    prune_views(args.cache, args.ttl_days)
    return 0


if __name__ == "__main__":
    sys.exit(main())
