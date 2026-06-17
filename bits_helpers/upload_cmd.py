#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""upload_cmd.py -- upload a built package's tarball and symlinks to S3 (boto3).

This is a thin CLI wrapper around Boto3RemoteSync.upload_symlinks_and_tarball()
so that the Makeflow .upload rule can invoke it as a subprocess without needing
an in-process Python call.

Package identity is read from environment variables so that the Makeflow rule
can pass them naturally via the environment block:

  PKGNAME       -- package name
  PKGVERSION    -- package version
  PKGREVISION   -- package revision (may be empty when force_revision="")
  PKGHASH       -- content-addressable package hash
  EFFECTIVE_ARCHITECTURE -- resolved target architecture (e.g. "slc7_x86-64")
  BUILD_ARCH    -- the native build architecture (may differ from EFFECTIVE_ARCHITECTURE)

CLI arguments carry the store configuration that is only known to the Python
build driver (not baked into the environment):

  --remote-store  b3://<bucket>   (read store URL, with or without b3:// prefix)
  --write-store   b3://<bucket>   (write store URL, with or without b3:// prefix)
  --work-dir      <path>          (build root directory, e.g. sw/)
  --architecture  <arch>          (native build architecture)

Exit code: 0 on success, 1 on failure.

Usage (from a Makeflow rule shell block):
  PKGNAME=foo PKGVERSION=1.0 PKGREVISION=1 PKGHASH=abc123 \\
  EFFECTIVE_ARCHITECTURE=slc7_x86-64 BUILD_ARCH=slc7_x86-64 \\
    python3 -m bits_helpers.upload_cmd \\
      --remote-store b3://mybucket \\
      --write-store  b3://mybucket \\
      --work-dir     sw/ \\
      --architecture slc7_x86-64
"""

import argparse
import os
import re
import sys


def _parse_args():
    p = argparse.ArgumentParser(
        description="Upload a built package's tarball and dist symlinks to S3 via boto3.",
    )
    p.add_argument("--remote-store", required=True,
                   help="S3 read bucket URL (b3://bucket or just bucket name)")
    p.add_argument("--write-store", required=True,
                   help="S3 write bucket URL (b3://bucket or just bucket name)")
    p.add_argument("--work-dir", required=True,
                   help="Build root directory (e.g. sw/)")
    p.add_argument("--architecture", required=True,
                   help="Native build architecture (e.g. slc7_x86-64)")
    return p.parse_args()


def _require_env(name):
    val = os.environ.get(name, "")
    if not val:
        print("upload_cmd: error: environment variable %s is not set" % name, file=sys.stderr)
        sys.exit(1)
    return val


def main():
    args = _parse_args()

    pkgname    = _require_env("PKGNAME")
    pkgversion = _require_env("PKGVERSION")
    pkghash    = _require_env("PKGHASH")
    # PKGREVISION may legitimately be empty (force_revision=""), so we don't
    # require it to be non-empty; we just read it.
    pkgrevision = os.environ.get("PKGREVISION", "")
    eff_arch    = _require_env("EFFECTIVE_ARCHITECTURE")

    # Build a minimal spec dict that mirrors what the Python build loop uses.
    # effective_arch(spec, build_arch) returns SHARED_ARCH when
    # spec["architecture"] == "shared", otherwise returns build_arch.
    # We reconstruct this from EFFECTIVE_ARCHITECTURE: if it equals "shared"
    # (i.e. SHARED_ARCH), mark the spec accordingly so that the upload goes to
    # the correct path.
    from bits_helpers.utilities import SHARED_ARCH
    spec = {
        "package":      pkgname,
        "version":      pkgversion,
        "revision":     pkgrevision,
        "hash":         pkghash,
        # Preserve the "shared" architecture flag so effective_arch() returns
        # the right value inside upload_symlinks_and_tarball.
        "architecture": SHARED_ARCH if eff_arch == SHARED_ARCH else "",
    }

    # Import the sync backend.  We import here (not at module level) so that a
    # missing boto3 gives a clear error at runtime rather than import time.
    try:
        from bits_helpers.sync import Boto3RemoteSync
    except ImportError as exc:
        print("upload_cmd: error: cannot import Boto3RemoteSync: %s" % exc, file=sys.stderr)
        sys.exit(1)

    sync = Boto3RemoteSync(
        remoteStore=args.remote_store,
        writeStore=args.write_store,
        architecture=args.architecture,
        workdir=args.work_dir,
    )

    print("upload_cmd: uploading %s-%s (%s) to %s" %
          (pkgname, pkgversion, pkghash, args.write_store), flush=True)

    try:
        sync.upload_symlinks_and_tarball(spec)
    except SystemExit:
        raise
    except Exception as exc:
        print("upload_cmd: error: upload failed: %s" % exc, file=sys.stderr)
        sys.exit(1)

    print("upload_cmd: done uploading %s-%s" % (pkgname, pkgversion), flush=True)


if __name__ == "__main__":
    main()
