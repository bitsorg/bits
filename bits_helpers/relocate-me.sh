#!/bin/bash -e
THISDIR="$(cd -P "$(dirname "$0")" && pwd)"
source ${THISDIR}/etc/profile.d/.bits-pkginfo
INSTALL_BASE=$(echo $THISDIR | sed "s|/$PP$||")
if [[ -s ${THISDIR}/etc/profile.d/.bits-relocate ]] ; then
  for f in $(cat ${THISDIR}/etc/profile.d/.bits-relocate) ; do
    sed -i -e "s|${PKG_DIR}/INSTALLROOT/$PH|$INSTALL_BASE|g;s|${PKG_DIR}|$INSTALL_BASE|g" "${THISDIR}/$f"
  done
fi
sed -i -e "s|^PKG_DIR=.*|PKG_DIR="${INSTALL_BASE}"|" $THISDIR/etc/profile.d/.bits-pkginfo

