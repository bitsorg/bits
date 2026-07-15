# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""``bits cvmfs-path`` — resolve a package's CVMFS publish path from the group's
templates, without building.

The publish pipeline records each group's path templates (declared once in
defaults-release.sh under system:) in every package's .meta.json and expands
them at publish time. But the pre-build *reserve* — which takes a gateway lease
on the target namespace before spending hours building — runs before any
.meta.json exists. Rather than re-deriving the path from scattered ui-config
keys (which could drift from what the build records), the reserve asks bits:
this command resolves the very same templates (cvmfs_layout.resolve_cvmfs_templates)
and prints the target path, so the reserved namespace and the published path
cannot diverge.

Authorization (who may publish where) stays in the pipeline: it decides admin
vs user and passes --admin/--login. This command only resolves paths.
"""

import os
import sys
from os.path import exists

import re

from bits_helpers.log import debug, dieOnError
from bits_helpers.utilities import parseDefaults, readDefaults, git
from bits_helpers.cvmfs_layout import (
    resolve_cvmfs_templates, resolve_release, path_release, bake_release)


# The placeholder set the publish pipeline's _expand_tmpl understands. {commit}
# and {revision} are not known before the build, so the reserve resolves them to
# empty — it is a best-effort, version-level pre-check (the reserve itself is
# documented as such). {tag} and {version} both map to the version segment.
def _expand(tmpl, subst):
    for key, val in subst.items():
        tmpl = tmpl.replace("{%s}" % key, val)
    return tmpl


def doCvmfsPath(args, parser):
    """Resolve and print the CVMFS publish path for one package.

    Returns True on success (path printed to stdout). Aborts via dieOnError when
    the group declares no CVMFS prefix or a non-admin path is requested without
    a login.
    """
    if not exists(args.configDir):
        from bits_helpers.repo_provider import cwd_is_recipe_dir
        _default_config_dir = os.environ.get("BITS_REPO_DIR", "alidist")
        if args.configDir == _default_config_dir and cwd_is_recipe_dir():
            debug("Recipe files detected in current directory; using '.' as config dir")
            args.configDir = "."
    dieOnError(not exists(args.configDir),
               'Cannot find recipes under directory "%s".\n'
               'Maybe you need to "cd" to the right directory or '
               'you forgot to run "bits init"?' % args.configDir)

    # Load the defaults profile exactly like `bits status` — only the group's
    # system: block (templates) is consulted; no recipe/version resolution.
    defaults_reader = lambda: readDefaults(
        args.configDir, args.defaults, parser.error, args.architecture)
    err, _overrides, _taps, defaults_meta = parseDefaults(
        args.disable, defaults_reader, debug, args.architecture, args.configDir)
    dieOnError(err, err)

    tmpls = resolve_cvmfs_templates(
        defaults_meta, getattr(args, "prefix", None) or None)
    dieOnError(tmpls is None,
               "no CVMFS root: the loaded defaults declare no system.prefix and "
               "no --prefix fallback was given; cannot resolve a publish path")

    # Effective root: admins publish under the group prefix; users publish under
    # <user_prefix>/<login>. This mirrors the publish loop (IS_ADMIN + user_prefix).
    if args.admin:
        root = tmpls["prefix"].rstrip("/")
    else:
        dieOnError(not args.login,
                   "--login is required to resolve a non-admin (user) path")
        root = tmpls["user_prefix"].rstrip("/") + "/" + args.login

    kind = args.kind or "releases"
    tmpl = {"releases": tmpls["path"],
            "modules":  tmpls["modules"],
            "shared":   tmpls["shared"]}[kind]

    # {release} is resolved exactly as the build does, so the reserved namespace
    # matches the published path. It is derived from the same inputs: an explicit
    # non-trunk release: in the defaults, else the recipe dir's branch name, else
    # main (which collapses out of the path). We read the branch the same way
    # build.py does (empty when detached / no branch, e.g. in CI) and hand the raw
    # basename to resolve_release, which strips -patches and applies the trunk rule.
    _, _value = git(("symbolic-ref", "-q", "HEAD"),
                    directory=args.configDir, check=False)
    _branch_basename = re.sub("refs/heads/", "", _value)
    tmpl = bake_release(
        tmpl, path_release(resolve_release(defaults_meta, _branch_basename)))

    # {family} is per-package and unknown before the build, so it collapses to
    # empty — the templates use the trailing-slash form {family}{pkg}.
    _fam = getattr(args, "family", None)
    subst = {
        "prefix":      root,
        "pkg":         args.package or "",
        "tag":         args.version or "",
        "version":     args.version or "",
        "revision":    "",
        "platform":    args.platform or "",
        "install_dir": args.installDir or "",
        "commit":      "",
        "user":        args.login or "",
        "family":      (_fam + "/") if _fam else "",
    }
    # Expand the remaining tokens literally — do NOT normalise/collapse slashes:
    # the publish pipeline expands the same .meta.json template literally, so the
    # reserve path this produces must match it byte-for-byte. ({release} is already
    # baked/collapsed above by the shared bake_release, exactly as the build does.)
    path = _expand(tmpl, subst)
    print(path)
    return True
