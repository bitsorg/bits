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


def split_reuse_policy(reuse_from):
    """Split an optional trailing ``::<policy>`` off a ``--reuse-from`` value.

    Sugar so ``--reuse-from cvmfs::relaxed`` (or ``<path>::relaxed``) can set the
    reuse policy alongside the source. Returns ``(source, policy)`` where policy
    is ``"strict"``/``"relaxed"`` or ``None`` when no valid suffix is present.
    Absolute paths never contain ``::``, so splitting on the last ``::`` is
    unambiguous. Pure; the caller reconciles it with an explicit --reuse-policy.
    """
    if not reuse_from:
        return reuse_from, None
    head, sep, tail = reuse_from.rpartition("::")
    if sep and tail.strip().lower() in ("strict", "relaxed"):
        return head, tail.strip().lower()
    return reuse_from, None


def resolve_reuse_from(reuse_from, layout):
    """Resolve ``--reuse-from`` to an absolute modules-tree path, or None.

    ``None``/``""`` → None (no module reuse). The literal ``"cvmfs"`` →
    ``layout["module_path"]`` (raises ValueError if there is no layout /
    module_path). Any other value must be an absolute path (raises otherwise).
    Pure and side-effect free so the caller decides how to report the error.
    """
    if not reuse_from:
        return None
    if reuse_from == "cvmfs":
        module_path = layout.get("module_path") if layout else None
        if not module_path:
            raise ValueError(
                "--reuse-from cvmfs needs a modules layout (module_dir/cvmfs_dir) "
                "in the defaults system: section; none is configured.")
        return module_path
    if not os.path.isabs(reuse_from):
        raise ValueError(
            "--reuse-from must be an absolute path or the literal 'cvmfs' "
            "(got %r)." % reuse_from)
    return reuse_from


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
# Trunk names: a release equal to one of these means "no dedicated release" — it
# selects the lcg.bits trunk branch and collapses out of the install path (so the
# default/main line keeps the pre-release layout).
_TRUNK_RELEASES = ("main", "master", "head")


def _declared_release(defaults_meta):
    """The release explicitly declared in the defaults, or "" if none.

    Looked up as the ``release`` variable first (the value ``%(release)s`` feeds
    to the lcg.bits branch override), then a ``release:`` field under ``system:``
    or bare top-level. dev3/dev4 set this; the base sets ``release: main``.
    """
    variables = (defaults_meta or {}).get("variables", {}) or {}
    val = variables.get("release")
    if not val:
        system = (defaults_meta or {}).get("system", {}) or {}
        val = system.get("release") or (defaults_meta or {}).get("release")
    return str(val).strip() if val else ""


def resolve_release(defaults_meta, branch=None):
    """Resolve the release LABEL — the SINGLE value that names both the lcg.bits
    recipe branch (via ``overrides: lcg.bits: tag: "%(release)s"``) and the CVMFS
    {release} path slot. Tagging stacks.bits pins it, so the tag pins the recipe
    pool and the publish location together.

    Precedence — highest first:
      1. an explicitly declared, non-trunk ``release`` (dev3/dev4/LCG_NNN): the
         deliberate choice of a dedicated release.
      2. the working-directory branch name, when it is not a trunk name: a build
         on a recipe branch tracks the matching lcg.bits branch and publishes
         under that branch's namespace. A trailing ``-patches`` is stripped, so a
         ``LCG_107-patches`` branch resolves to the ``LCG_107`` release (the
         aliBuild/LCG "stream"); a plain branch name is used verbatim.
      3. ``"main"`` — the default. Reproduces the old behaviour: the lcg.bits
         ``main`` branch, and (because ``main`` collapses out of the path, see
         ``path_release``) no ``/{release}/`` segment.

    A declared ``main``/``master``/``HEAD`` is a trunk sentinel (means "no
    dedicated release"), so it does NOT block branch derivation. ``branch`` is the
    raw branch name (as read from ``git symbolic-ref``); pass the basename, NOT the
    already-emptied ``branch_stream`` (which is blank for non-``-patches`` branches
    and would suppress derivation for a plain branch).
    """
    declared = _declared_release(defaults_meta)
    if declared and declared.lower() not in _TRUNK_RELEASES:
        return declared
    b = re.sub(r"-patches$", "", (branch or "").strip())
    if b and b.lower() not in _TRUNK_RELEASES:
        return b
    return "main"


def path_release(release):
    """The {release} value for the install PATH. The resolved release drives the
    path, except a trunk value (main/master/head) collapses to "" so the path has
    no release level — preserving the pre-release layout for the default/main line
    while branch- and explicitly-named releases get their own ``/{release}/``.
    """
    r = (release or "").strip()
    return "" if r.lower() in _TRUNK_RELEASES else r


def bake_release(template, release):
    """Substitute {release} in a path template. When ``release`` is empty, remove
    the whole ``{release}/`` segment so the path collapses cleanly (``…/releases/
    {pkg}/…``) instead of leaving a stray double slash — this is what makes the
    default/main layout byte-identical between the build's baked template and the
    pre-build reserve.
    """
    if not template:
        return template
    if release:
        return template.replace("{release}", release)
    return (template.replace("{release}/", "")
                    .replace("/{release}", "")
                    .replace("{release}", ""))


def resolve_cvmfs_templates(defaults_meta, injected_prefix=None):
    """Resolve the group's CVMFS publish-path templates from the defaults.

    Returns a dict {prefix, user_prefix, path, modules, shared} — templates with
    {prefix} left intact (resolved at publish time to the admin `prefix` or the
    user `user_prefix`/<login> root), except user_prefix whose own {prefix}
    back-reference is resolved to the base prefix. Returns None when no CVMFS root
    is available at all.

    The root prefix is an AUTHORIZATION boundary (which namespace a build may write
    to), so `injected_prefix` — supplied by the trusted, change-controlled source
    (bits-console's per-community ui-config.yaml, threaded in as BITS_CVMFS_PREFIX /
    --prefix) — is AUTHORITATIVE and wins. A recipe-declared `system.prefix` is only
    honoured as a fallback for local dev builds with no injected prefix; it can NOT
    override the injected one, so a recipe (or a user who can edit/point at one)
    cannot redirect a build into another group's CVMFS tree.

    Only a prefix is required; a group that sets any template but has no prefix at
    all is misconfigured (dieOnError).
    """
    from bits_helpers.log import dieOnError
    system = (defaults_meta or {}).get("system", {}) or {}

    def opt(key):
        # system: wins; a bare top-level key is still honoured (never hashed).
        if key in system:
            return system[key]
        return (defaults_meta or {}).get(key, None)

    # The prefix is an AUTHORIZATION boundary. bits-console injects the authoritative
    # value (BITS_CVMFS_PREFIX); a recipe/defaults `prefix` is a declared copy that
    # MUST agree with it. If both are set and differ, refuse (fail-closed) rather than
    # silently publishing into the wrong namespace. With no injection (local dev) the
    # declared prefix is used as-is.
    recipe_prefix = opt("prefix") or opt("cvmfs_prefix")
    dieOnError(
        bool(injected_prefix) and bool(recipe_prefix)
        and injected_prefix.rstrip("/") != recipe_prefix.rstrip("/"),
        "CVMFS prefix mismatch: the defaults/recipe prefix %r disagrees with the "
        "authoritative bits-console prefix %r (communities/<group>/ui-config.yaml: "
        "cvmfs_prefix). Reconcile the two — a build will not publish while they "
        "differ." % (recipe_prefix, injected_prefix))
    root = (injected_prefix or None) or recipe_prefix
    # cvmfs_releases_template is the current name; cvmfs_path_template is the
    # legacy alias, still accepted.
    rel = opt("cvmfs_releases_template") or opt("cvmfs_path_template")
    mod = opt("cvmfs_modules_template")
    shr = opt("cvmfs_shared_path_template")
    usr = opt("cvmfs_user_prefix")

    dieOnError(bool(rel or mod or shr or usr) and not root,
               "a CVMFS prefix is required for publishing (an injected/community "
               "prefix, or a local recipe system.prefix) but none is set")
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
