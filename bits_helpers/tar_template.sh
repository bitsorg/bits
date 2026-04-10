#!/usr/bin/env bash
# tar_template.sh -- create the tarball and main dist symlink for a package.
#
# This script is used by the Makeflow .tar rule when --pipeline is active.
# It runs concurrently with the downstream .build rules, so that tarball
# creation does not block the next package from starting.
#
# Required environment variables (set by the .tar Makeflow rule):
#   WORK_DIR               -- root build directory (e.g. sw/)
#   PKGNAME                -- package name
#   PKGVERSION             -- package version
#   PKGREVISION            -- package revision (may be empty when force_revision="")
#   PKGHASH                -- content-addressable hash of the build
#   EFFECTIVE_ARCHITECTURE -- e.g. "slc7_x86-64"
#   CACHED_TARBALL         -- non-empty when a prebuilt tarball was used; in that
#                             case this script is a no-op (tarball already exists)
#
# Exit code: non-zero on any failure.

set -e

# Reconstruct _VERREV exactly as build_template.sh does so that the tarball
# filename is consistent with what build_template.sh put on disk.
if [ -n "${PKGREVISION}" ]; then
  _VERREV="${PKGVERSION}-${PKGREVISION}"
else
  _VERREV="${PKGVERSION}"
fi

PACKAGE_WITH_REV="${PKGNAME}-${_VERREV}.${EFFECTIVE_ARCHITECTURE}.tar.gz"
HASHPREFIX=$(echo "$PKGHASH" | cut -c1,2)
HASH_PATH="${EFFECTIVE_ARCHITECTURE}/store/${HASHPREFIX}/${PKGHASH}"

# Nothing to do if a prebuilt tarball was already expanded by build_template.sh.
if [ -n "$CACHED_TARBALL" ]; then
  echo "bits: tar: skipping tarball creation for $PKGNAME (cached tarball used)"
  exit 0
fi

echo "bits: tar: creating tarball for $PKGNAME-${_VERREV} ($PKGHASH)"

mkdir -p "${WORK_DIR}/TARS/${HASH_PATH}" \
         "${WORK_DIR}/TARS/${EFFECTIVE_ARCHITECTURE}/${PKGNAME}"

# Use pigz for multi-core compression when available, fall back to gzip.
gzip=$(command -v pigz) || gzip=$(command -v gzip)

tar -cC "${WORK_DIR}/INSTALLROOT/${PKGHASH}" . |
  $gzip -c > "${WORK_DIR}/TARS/${HASH_PATH}/${PACKAGE_WITH_REV}.processing"
mv "${WORK_DIR}/TARS/${HASH_PATH}/${PACKAGE_WITH_REV}.processing" \
   "${WORK_DIR}/TARS/${HASH_PATH}/${PACKAGE_WITH_REV}"

# Create the "main" dist symlink so that upload_shell_command can find the
# tarball via the standard TARS/<arch>/<pkg>/<tarball> path.
ln -nfs "../../${HASH_PATH}/${PACKAGE_WITH_REV}" \
   "${WORK_DIR}/TARS/${EFFECTIVE_ARCHITECTURE}/${PKGNAME}/${PACKAGE_WITH_REV}"

echo "bits: tar: done creating tarball for $PKGNAME-${_VERREV}"
