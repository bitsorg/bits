"""`bits view-publish` — generate the merged view for a published release.

Run against the deployed/staged CVMFS tree (where the packages already sit at
their final paths). It finds every package carrying *build_id* in its
``.meta.json``, unions them into ``<store>/Views/<build_id>/<arch>/`` with
relative symlinks (so they resolve once on CVMFS), and drops a nested
``.cvmfscatalog`` at the build_id level. Clients then get a ready-made,
single-entry environment for that release with no per-node view build (see
``bits enter --view``); arbitrary/dev closures still fall back to a client-side
view.
"""

import os

from bits_helpers.log import error, info, warning
from bits_helpers.view import collect_build_id_roots, build_published_view


def doViewPublish(args, parser):
    """Build the per-build_id published view. Returns True on success."""
    store = args.viewStore
    build_id = args.viewBuildId
    arch = args.architecture

    if not store or not os.path.isdir(store):
        error("view-publish: store directory %s does not exist", store)
        return False
    if not build_id:
        error("view-publish: --build-id is required")
        return False

    roots = collect_build_id_roots(store, build_id, architecture=arch)
    if not roots:
        error("view-publish: no packages with build_id %s found under %s",
              build_id, store)
        return False

    result = build_published_view(roots, build_id, arch, store)
    info("view-publish: %d package(s) -> %s (%d link(s))",
         len(roots), result["view_dir"], len(result["linked"]))
    if result["conflicts"]:
        warning("view-publish: %d file conflict(s) (first writer kept); e.g. %s",
                len(result["conflicts"]),
                ", ".join(c[0] for c in result["conflicts"][:5]))
    return True
