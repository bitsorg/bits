# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Standalone checkout runner for Makeflow pipeline mode.

Called as::

    python3 -m bits_helpers.checkout_runner --spec-json PATH [--work-dir ...]

by the Makeflow ``.checkout`` rule so that source cloning / archive downloads
run as fully independent, parallel Makeflow tasks instead of sequentially
in the Python preparation phase.

All spec fields required by :func:`~bits_helpers.workarea.checkout_sources`
are serialised to a JSON file in the SPECS directory by ``build.py`` at
Makeflow-generation time.  The ``scm`` object is reconstructed here from the
``scm_type`` string (``"git"`` or ``"sapling"``).
"""
from __future__ import annotations
import argparse
import json
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Checkout / download sources for one package (Makeflow helper)"
    )
    ap.add_argument("--spec-json", required=True,
                    help="Path to the spec_checkout.json written by build.py")
    ap.add_argument("--work-dir", required=True,
                    help="Build work directory (WORK_DIR)")
    ap.add_argument("--reference-sources", default="",
                    help="Mirror / reference sources directory")
    ap.add_argument("--enforce-mode", default="off",
                    help="Checksum enforce mode: off / warn / enforce")
    ap.add_argument("--parallel-sources", type=int, default=1,
                    help="Concurrent source-URL downloads per package")
    args = ap.parse_args(argv)

    with open(args.spec_json) as fh:
        spec = json.load(fh)

    # Reconstruct the SCM object from the serialised type name.
    scm_type = spec.pop("scm_type", "git")
    if scm_type == "sapling":
        from bits_helpers.sl import Sapling
        spec["scm"] = Sapling()
    else:
        from bits_helpers.git import Git
        spec["scm"] = Git()

    from bits_helpers.workarea import checkout_sources
    checkout_sources(
        spec,
        args.work_dir,
        args.reference_sources,
        False,        # containerised_build — never in Makeflow mode
        enforce_mode=args.enforce_mode,
        sync_helper=None,   # no remote sync; prefetch workers handle that
        parallel_sources=args.parallel_sources,
    )


if __name__ == "__main__":
    main()
