#!/bin/bash -e
THISDIR="$(cd -P "$(dirname "$0")" && pwd)"
. ${THISDIR}/etc/profile.d/.bits-pkginfo
# Default INSTALL_BASE to where the package is unpacked (THISDIR minus the
# arch/pkg/ver suffix) — correct for a local install. A caller that stages the
# tree in a temp dir but wants the files to reference their FINAL location (the
# CI CVMFS publish, which unpacks to a scratch dir but must bake the CVMFS path)
# exports INSTALL_BASE explicitly; honour it instead of the scratch path.
INSTALL_BASE="${INSTALL_BASE:-$(echo "$THISDIR" | sed "s|/$PP$||")}"
if [[ -s ${THISDIR}/etc/profile.d/.bits-relocate ]] ; then
  # R1 FIX: use 'while IFS= read -r' instead of 'for f in $(cat …)' so that
  # filenames containing spaces, tabs, or glob characters are handled correctly.
  # Local install keeps the arch/pkg/ver ($PP) suffix — the package lives at
  # INSTALL_BASE/$PP — so only the …/INSTALLROOT/$PH prefix is rewritten. A caller
  # deploying to a layout that does NOT preserve $PP (the CI CVMFS publish: content
  # goes to <pkg_path> directly, no arch/pkg/ver wrapper) sets
  # BITS_RELOCATE_STRIP_PP=1 so the full …/INSTALLROOT/$PH/$PP prefix collapses to
  # INSTALL_BASE and the paths don't gain a doubled $PP.
  _pp_strip=""
  [ -n "${BITS_RELOCATE_STRIP_PP:-}" ] && _pp_strip="s|${PKG_DIR}/INSTALLROOT/$PH/$PP|$INSTALL_BASE|g;"
  while IFS= read -r f; do
    [[ -z "$f" ]] && continue
    sed -i.unrelocated -e "${_pp_strip}s|${PKG_DIR}/INSTALLROOT/$PH|$INSTALL_BASE|g;s|${PKG_DIR}|$INSTALL_BASE|g" "${THISDIR}/$f"
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
