#!/bin/bash -e
THISDIR="$(cd -P "$(dirname "$0")" && pwd)"
. ${THISDIR}/etc/profile.d/.bits-pkginfo
INSTALL_BASE=$(echo "$THISDIR" | sed "s|/$PP$||")
if [[ -s ${THISDIR}/etc/profile.d/.bits-relocate ]] ; then
  # R1 FIX: use 'while IFS= read -r' instead of 'for f in $(cat …)' so that
  # filenames containing spaces, tabs, or glob characters are handled correctly.
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    sed -i.unrelocated -e "s|${PKG_DIR}/INSTALLROOT/$PH|$INSTALL_BASE|g;s|${PKG_DIR}|$INSTALL_BASE|g" "${THISDIR}/$f"
    rm -f "${THISDIR}/${f}.unrelocated"
  done < "${THISDIR}/etc/profile.d/.bits-relocate"
fi
# R2 FIX: keep $INSTALL_BASE inside double quotes so it is not subject to
# word-splitting; escape the literal quotes in the sed replacement string.
sed -i.unrelocated -e "s|^PKG_DIR=.*|PKG_DIR=\"${INSTALL_BASE}\"|" "$THISDIR/etc/profile.d/.bits-pkginfo"
rm -f "$THISDIR/etc/profile.d/.bits-pkginfo.unrelocated"

case "$PKGNAME" in
    defaults-*)
    ;;
    *)
    if [ -f "$WORK_DIR/$PP/etc/profile.d/post-relocate.sh" ]
    then
      export PP
      # MODULES_STAGING: when set by the caller (e.g. the CI pipeline), post-relocate.sh
      # should write module files to this directory instead of the live CVMFS path.
      # When unset (direct command-line use), post-relocate.sh falls back to its own
      # default path — full backward compatibility is preserved.
      export MODULES_STAGING
      bash -ex "$WORK_DIR/$PP/etc/profile.d/post-relocate.sh"
    fi
    ;;
esac
