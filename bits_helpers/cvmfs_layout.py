# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Templated CVMFS layout resolved from the defaults profile.

Lets a single place (``defaults-release.sh``) declare where a build's packages
and modulefiles live on CVMFS, so the build / publish / reuse paths stop being
spelled out as scattered CLI flags. Three optional fields, each a template that
may reference ``%(architecture)s`` (the effective, combined arch string, e.g.
``ubuntu2510_x86-64-gcc15-dbg``):

    cvmfs_dir:    /cvmfs/sft.cern.ch/lcg/releases   # CVMFS root
    install_dir:  %(architecture)s/Packages         # relative to cvmfs_dir
    module_dir:   %(architecture)s/modules          # relative to cvmfs_dir
    shared_dir:   noarch                            # relative to cvmfs_dir (noarch)
    views_dir:    Views                             # relative to cvmfs_dir

Each has a sensible default when omitted (``%(architecture)s`` for packages,
``%(architecture)s/modules`` for modulefiles, ``noarch`` for architecture-
independent packages, ``Views`` for merged release views), so a profile can set
just ``cvmfs_dir`` and get the rest. The four are independent CVMFS directory
trees and are published separately. ``shared_dir`` is deliberately NOT arch-
scoped: noarch packages live in one place regardless of build architecture.

``install_path`` / ``module_path`` / ``shared_path`` / ``views_path`` are the resolved absolutes
(``cvmfs_dir`` joined with the relative dir). When the build runs in a docker
container, ``install_path`` is the natural value for ``--cvmfs-prefix`` (build
in place, no-op relocation); ``cvmfs_dir`` is the natural ``--remote-store
cvmfs://`` root for reusing already-deployed components.
"""

import os
import re

_VAR_RE = re.compile(r"%\((\w+)\)s")


def _render(template, subst):
    """Substitute %(NAME)s for known names; leave unknown placeholders intact."""
    if not template:
        return template
    return _VAR_RE.sub(lambda m: str(subst.get(m.group(1), m.group(0))), template)


def resolve_cvmfs_layout(defaults_meta, architecture):
    """Return the resolved CVMFS layout dict, or None when not configured.

    Keys: cvmfs_dir, install_dir, module_dir, shared_dir, views_dir,
    install_path, module_path, shared_path, views_path. Returns None when none of
    cvmfs_dir / install_dir / module_dir / shared_dir / views_dir is set, so
    builds that don't opt in are unaffected.
    """
    if not defaults_meta:
        return None
    cvmfs_dir = defaults_meta.get("cvmfs_dir")
    install_dir = defaults_meta.get("install_dir")
    module_dir = defaults_meta.get("module_dir")
    shared_dir = defaults_meta.get("shared_dir")
    views_dir = defaults_meta.get("views_dir")
    if not (cvmfs_dir or install_dir or module_dir or shared_dir or views_dir):
        return None

    subst = {"architecture": architecture}
    cvmfs_dir = _render(cvmfs_dir or "", subst)
    # Sensible defaults for each independent tree when only cvmfs_dir (or a
    # subset) was given. Packages/modules are arch-scoped; shared (noarch)
    # packages and views are NOT — they live in one place regardless of the
    # build architecture.
    install_dir = _render(install_dir or "%(architecture)s", subst)
    module_dir = _render(module_dir or "%(architecture)s/modules", subst)
    shared_dir = _render(shared_dir or "noarch", subst)
    views_dir = _render(views_dir or "Views", subst)

    def _abs(rel):
        return os.path.join(cvmfs_dir, rel) if cvmfs_dir else rel

    return {
        "cvmfs_dir": cvmfs_dir,
        "install_dir": install_dir,
        "module_dir": module_dir,
        "shared_dir": shared_dir,
        "views_dir": views_dir,
        "install_path": _abs(install_dir),
        "module_path": _abs(module_dir),
        "shared_path": _abs(shared_dir),
        "views_path": _abs(views_dir),
    }


# CVMFS *publish* path templates. Distinct from resolve_cvmfs_layout above:
# that resolves the build/reuse trees (arch-scoped dirs under a cvmfs_dir);
# this resolves the publish-target templates (per-package paths the publish
# pipeline expands and the pre-build reserve checks). A group declares them in
# defaults-release.sh under system:, and only `prefix` is required — the four
# templates fall back to a conventional layout. Recorded verbatim in each
# package's .meta.json (cvmfs_templates); `bits cvmfs-path` resolves the same
# templates pre-build so the reserve and the publish cannot diverge.
def resolve_release(defaults_meta, branch_stream=None, explicit=None):
    """Resolve the {release} path slot (the LCG-style release/version namespace).

    The release is a SINGLE source of truth: the same value names the CVMFS
    {release} slot AND (via ``overrides: lcg.bits: tag: "%(release)s"`` in the
    defaults) the lcg.bits recipe branch. Tagging stacks.bits therefore pins the
    release, which pins the recipe pool — the tag fully defines the build content.

    Precedence — highest first:
      1. explicit value (a ``--release`` override), when given.
      2. the ``release`` variable (``variables.release``) — the authoritative
         source, because it is the very value ``%(release)s`` feeds to the
         lcg.bits branch override; keeping the slot and the branch on one value
         is what makes them consistent. dev3/dev4 override this variable.
      3. a ``release:`` field under ``system:`` (or bare top-level) — accepted as
         a fallback for defaults that set it there instead of as a variable.
      4. ``branch_stream`` — the recipe repo's branch (minus ``-patches``), but
         ONLY when it is a real release name. ``master``/``main``/``HEAD``/empty
         are the development trunk (and CI is a detached HEAD → empty), so they
         are ignored rather than published as a literal ``master`` slot.
      5. ``"dev"`` — safe default so a build always has a slot.

    ``BITS_RELEASE`` env is intentionally NOT read here: overriding only the slot
    without also moving the lcg.bits branch would split the two apart. To build a
    different release, select the defaults that set ``release`` (e.g. dev3/dev4),
    so the branch and the slot move together.
    """
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    variables = (defaults_meta or {}).get("variables", {}) or {}
    val = variables.get("release")
    if not val:
        system = (defaults_meta or {}).get("system", {}) or {}
        val = system.get("release") or (defaults_meta or {}).get("release")
    if val:
        return str(val).strip()
    bs = (branch_stream or "").strip()
    if bs and bs.lower() not in ("master", "main", "head"):
        return bs
    return "dev"


def resolve_cvmfs_templates(defaults_meta, fallback_prefix=None):
    """Resolve the group's CVMFS publish-path templates from the defaults.

    Returns a dict {prefix, user_prefix, path, modules, shared} — templates with
    {prefix} left intact (resolved at publish time to the admin `prefix` or the
    user `user_prefix`/<login> root), except user_prefix whose own {prefix}
    back-reference is resolved to the base prefix. Returns None when the group
    declares no CVMFS root and no fallback is given.

    The root prefix comes from the group's defaults-release.sh (system.prefix)
    when declared; otherwise `fallback_prefix` is used. The fallback exists for
    groups whose recipe repo cannot declare a prefix (e.g. a third-party recipe
    set): bits-console supplies the prefix and the templates take the built-in
    defaults. A recipe-declared prefix always wins over the fallback.

    Only a prefix (recipe or fallback) is required; a group that sets any
    template but has no prefix at all is misconfigured (dieOnError).
    """
    from bits_helpers.log import dieOnError
    system = (defaults_meta or {}).get("system", {}) or {}

    def opt(key):
        # system: wins; a bare top-level key is still honoured (never hashed).
        if key in system:
            return system[key]
        return (defaults_meta or {}).get(key, None)

    # Recipe prefix wins; else the bits-console fallback (for recipe sets that
    # cannot declare their own prefix).
    root = opt("prefix") or opt("cvmfs_prefix") or (fallback_prefix or None)
    # cvmfs_releases_template is the current name; cvmfs_path_template is the
    # legacy alias, still accepted.
    rel = opt("cvmfs_releases_template") or opt("cvmfs_path_template")
    mod = opt("cvmfs_modules_template")
    shr = opt("cvmfs_shared_path_template")
    usr = opt("cvmfs_user_prefix")

    dieOnError(bool(rel or mod or shr or usr) and not root,
               "system.prefix is required for CVMFS publishing "
               "(a cvmfs_*_template is set but prefix is not)")
    if not root:
        return None

    # Built-in default layout; a group overrides any of these under system:.
    rel = rel or "{prefix}/{platform}/Packages/{pkg}/{tag}"
    mod = mod or "{prefix}/{platform}/Modules/modulefiles/{pkg}"
    shr = shr or "{prefix}/noarch/{pkg}/{tag}"
    usr = usr or "{prefix}/user"
    usr = usr.replace("{prefix}", root)
    return {
        "prefix":      root,
        "user_prefix": usr,
        "path":        rel,   # the .path key is fed by cvmfs_releases_template
        "modules":     mod,
        "shared":      shr,
    }
