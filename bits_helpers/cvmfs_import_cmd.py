# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""`bits import` — import a foreign CVMFS deployment into a bits reuse overlay.

Harvest each deployed module's *resolved* environment (via `modulecmd display`)
or read an equivalent manifest, classify it, closure-check the set, stamp it with
one deterministic ``build_id``, and generate a per-build_id overlay
(``MODULES/<build_id>/<arch>/``: bits modulefiles + build-sufficient ``init.sh``
+ module-side ``.meta.json`` + a ``.cvmfscatalog`` subcatalog) that relaxed reuse
can graft without recompiling. See ADR-0001.

This is the thin CLI driver; all transform logic lives in
``bits_helpers.cvmfs_import`` (stdlib-only, fully unit-tested). The only
non-deterministic step is the ``modulecmd`` shell-out during harvest.
"""

import json
import os

from bits_helpers.log import error, info, warning
from bits_helpers.cvmfs_import import (
    AliasMap, harvest_display, corpus_from_manifest, import_release,
)

_MODULERC_NAMES = ("modulerc", ".modulerc", ".version")


def _list_modules(modulepath):
    """Enumerate ``<name>/<version>`` module ids under a modulepath directory.

    Hidden files and modulerc/version control files are skipped; the directory
    tree below *modulepath* maps to the module name, the leaf file to the version.
    """
    ids = []
    for root, _dirs, files in os.walk(modulepath):
        rel = os.path.relpath(root, modulepath)
        name = "" if rel == "." else rel
        for fname in files:
            if fname.startswith(".") or fname in _MODULERC_NAMES:
                continue
            ids.append("%s/%s" % (name, fname) if name else fname)
    return sorted(ids)


def doImport(args, parser):
    """Drive `bits import`. Returns True on success, False on refusal/error."""
    out_root = getattr(args, "importOut", None) or os.path.join(
        args.workDir, "MODULES")
    arch = args.architecture or "unknown"
    label = getattr(args, "importLabel", None) or "import"
    alias_path = getattr(args, "importAliases", None)
    alias = AliasMap.load(alias_path) if alias_path else AliasMap()
    force = bool(getattr(args, "importForce", False))

    manifest = getattr(args, "importManifest", None)
    modulepath = getattr(args, "importModulepath", None)

    if manifest:
        try:
            with open(manifest) as fh:
                corpus = corpus_from_manifest(json.load(fh))
        except Exception as exc:
            error("import: cannot read manifest %s: %s", manifest, exc)
            return False
        if not corpus:
            error("import: manifest %s contained no packages", manifest)
            return False
    elif modulepath:
        if not os.path.isdir(modulepath):
            error("import: modulepath %s does not exist", modulepath)
            return False
        corpus = {}
        for mid in _list_modules(modulepath):
            entry = harvest_display(mid, modulepath)
            if entry is not None:
                corpus[mid] = entry
        if not corpus:
            error("import: no modules harvested from %s "
                  "(is environment-modules installed?)", modulepath)
            return False
    else:
        error("import: provide --manifest <file> or --modulepath <dir>")
        return False

    result = import_release(corpus, label, arch, out_root, alias=alias,
                            abi_tag=(args.architecture or ""), force=force)

    if result["dangling"] and not force:
        error("import: release is not closed; these dependencies are "
              "missing from the set: %s", ", ".join(result["dangling"]))
        info("Import the missing packages too, add aliases, or re-run with "
             "--force to stamp an open set anyway.")
        return False

    info("import: build_id %s", result["build_id"])
    info("import: wrote %d module(s) under %s",
         len(result["written"]),
         os.path.join(out_root, result["build_id"], arch))

    if alias_path:
        gaps = alias.unmapped([mid.split("/", 1)[0] for mid in corpus])
        if gaps:
            warning("import: %d name(s) had no bits alias and were passed "
                    "through unchanged: %s", len(gaps), ", ".join(gaps))
    return True
