"""Templated CVMFS layout resolved from the defaults profile.

Lets a single place (``defaults-release.sh``) declare where a build's packages
and modulefiles live on CVMFS, so the build / publish / reuse paths stop being
spelled out as scattered CLI flags. Three optional fields, each a template that
may reference ``%(architecture)s`` (the effective, combined arch string, e.g.
``ubuntu2510_x86-64-gcc15-dbg``):

    cvmfs_dir:    /cvmfs/sft.cern.ch/lcg/releases   # CVMFS root
    install_dir:  %(architecture)s/Packages         # relative to cvmfs_dir
    module_dir:   %(architecture)s/modules          # relative to cvmfs_dir
    views_dir:    Views                             # relative to cvmfs_dir

Each has a sensible default when omitted (``%(architecture)s`` for packages,
``%(architecture)s/modules`` for modulefiles, ``Views`` for merged release
views), so a profile can set just ``cvmfs_dir`` and get the rest. The three are
independent CVMFS directory trees and are published separately.

``install_path`` / ``module_path`` / ``views_path`` are the resolved absolutes
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

    Keys: cvmfs_dir, install_dir, module_dir, views_dir, install_path,
    module_path, views_path. Returns None when none of cvmfs_dir / install_dir /
    module_dir / views_dir is set, so builds that don't opt in are unaffected.
    """
    if not defaults_meta:
        return None
    cvmfs_dir = defaults_meta.get("cvmfs_dir")
    install_dir = defaults_meta.get("install_dir")
    module_dir = defaults_meta.get("module_dir")
    views_dir = defaults_meta.get("views_dir")
    if not (cvmfs_dir or install_dir or module_dir or views_dir):
        return None

    subst = {"architecture": architecture}
    cvmfs_dir = _render(cvmfs_dir or "", subst)
    # Sensible defaults for each of the three independent trees when only
    # cvmfs_dir (or a subset) was given. Packages/modules are arch-scoped;
    # views group by release (<name>-<build_id>/<arch>) so the dir is just Views.
    install_dir = _render(install_dir or "%(architecture)s", subst)
    module_dir = _render(module_dir or "%(architecture)s/modules", subst)
    views_dir = _render(views_dir or "Views", subst)

    def _abs(rel):
        return os.path.join(cvmfs_dir, rel) if cvmfs_dir else rel

    return {
        "cvmfs_dir": cvmfs_dir,
        "install_dir": install_dir,
        "module_dir": module_dir,
        "views_dir": views_dir,
        "install_path": _abs(install_dir),
        "module_path": _abs(module_dir),
        "views_path": _abs(views_dir),
    }
