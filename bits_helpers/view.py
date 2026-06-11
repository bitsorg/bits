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

import os


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
