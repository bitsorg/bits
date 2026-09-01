#!/bin/bash
BITS_START_TIMESTAMP=$(date +%%s)
# Automatically generated build script
unset DYLD_LIBRARY_PATH
echo "bits: start building $PKGNAME-$PKGVERSION-$PKGREVISION at $BITS_START_TIMESTAMP"
get_file_from_configDir() {
  # B2 FIX: quote $BITS_CONFIG_DIR in dirname/basename and quote ${repo_dir}/${d}
  # in the test so that paths with spaces are not word-split.
  local repo_dir=$(dirname "$BITS_CONFIG_DIR")
  for d in ${BITS_PATH//,/ } "$(basename "$BITS_CONFIG_DIR" | sed 's|\.bits$||')" ; do
    [ -f "${repo_dir}/${d}.bits/$1" ] && echo "${repo_dir}/${d}.bits/$1" && return 0
  done
  return 1
}

run_hooks() {
  export hook_type="$1"
  %(BITS_HOOK_PARAMS)s
  export hooks_list
  export skip_list
  # B3 FIX: replace eval with bash indirect expansion (${!var}) to avoid
  # shell injection if hook_type ever contains unexpected characters.
  local _hv="${hook_type}_HOOKS" _sv="SKIP_${hook_type}_HOOKS"
  hooks_list="${!_hv}"
  skip_list="${!_sv}"
  if [[ "$PKGREVISION" != local* ]]; then
    [ -n "$skip_list" ] && echo "bits: skipping hooks if enabled not allowed while uploading. Aborting." && exit 1
  fi
  # B6 FIX: use 'while IFS= read -r' so hook names with glob chars don't expand.
  while IFS= read -r hook; do
    [[ -z "$hook" ]] && continue
    [[ ",$skip_list," == *",$hook,"* ]] && continue
    hook_script=$(get_file_from_configDir "hooks/$hook")
    echo "bits: running hook $hook ($hook_script)"
    bash -ex "$hook_script"
  done < <(echo "$hooks_list" | tr -d ' ' | tr ',' '\n')
}

cleanup() {
  local exit_code=$?
  BITS_END_TIMESTAMP=$(date +%%s)
  BITS_DELTA_TIME=$(($BITS_END_TIMESTAMP - $BITS_START_TIMESTAMP))
  echo "bits: done building $PKGNAME-$PKGVERSION-$PKGREVISION at $BITS_START_TIMESTAMP (${BITS_DELTA_TIME} s)"
  # Remove the per-build private source copy (legacy mode) on success or failure.
  if [ -n "${_bits_private_src:-}" ] && [ -d "${_bits_private_src:-}" ]; then
    rm -rf "$_bits_private_src" 2>/dev/null || true
  fi
  exit $exit_code
}

trap cleanup EXIT

# Cleanup variables which should not be exposed to user code
unset AWS_ACCESS_KEY_ID
unset AWS_SECRET_ACCESS_KEY

set -e
set +h

cversion=$(gcc --version | head -n 1)
cpath=$(which gcc)

function hash() { true; }

# bits_apply_patches [strip] : apply the patch files bits staged in $SOURCEDIR
# ($PATCH0 .. up to $PATCH_COUNT) in declaration order, with `patch -p<strip>`
# (default strip level 1). Use this from recipes built with `auto_patch: false`
# (or under --no-auto-patch), where bits stages the patches but does not apply
# them. A .bits_applied_patches sentinel makes repeated calls / incremental
# rebuilds a no-op, mirroring bits' own idempotency.
bits_apply_patches() {
  local _strip="${1:-1}" _i _vn _pf
  local _sentinel="$SOURCEDIR/.bits_applied_patches"
  [ -f "$_sentinel" ] && return 0
  [ "${PATCH_COUNT:-0}" -gt 0 ] || return 0
  ( cd "$SOURCEDIR" || exit 1
    for ((_i=0; _i<${PATCH_COUNT:-0}; _i++)); do
      _vn="PATCH${_i}"; _pf="${!_vn}"
      [ -n "$_pf" ] || continue
      echo "bits: applying patch $_pf (-p${_strip})"
      patch -p"${_strip}" --batch --input "$SOURCEDIR/$_pf"
    done
  ) || return $?
  : > "$_sentinel"
}

export WORK_DIR="${WORK_DIR_OVERRIDE:-%(workDir)s}"
export BITS_CONFIG_DIR="${BITS_CONFIG_DIR_OVERRIDE:-%(configDir)s}"

# Insert our own wrapper scripts into $PATH, patched to use the system OpenSSL,
# instead of the one we build ourselves.
export PATH=$WORK_DIR/wrapper-scripts:$PATH

# The following environment variables are setup by
# the bits script itself
#
# - ARCHITECTURE
# - EFFECTIVE_ARCHITECTURE
# - BITS_SCRIPT_DIR
# - BUILD_REQUIRES
# - CACHED_TARBALL
# - CAN_DELETE
# - COMMIT_HASH
# - DEPS_HASH
# - DEVEL_HASH
# - DEVEL_PREFIX
# - INCREMENTAL_BUILD_HASH
# - JOBS
# - PKGHASH
# - PKGNAME
# - PKGREVISION
# - PKGVERSION
# - REQUIRES
# - RUNTIME_REQUIRES
# - PKGDIR

export PKG_NAME="$PKGNAME"
export PKG_VERSION="$PKGVERSION"
export PKG_BUILDNUM="$PKGREVISION"

# _VERREV: version-revision segment for install paths.
# When force_revision is set to "" via defaults-*.sh PKGREVISION is empty, so
# the path component is just the version string (no trailing dash).
if [ -n "${PKGREVISION}" ]; then
  _VERREV="${PKGVERSION}-${PKGREVISION}"
else
  _VERREV="${PKGVERSION}"
fi

if [ -n "${PKGFAMILY:-}" ]; then
  export PKGPATH=${EFFECTIVE_ARCHITECTURE}/${PKGFAMILY}/${PKGNAME}/${_VERREV}
else
  export PKGPATH=${EFFECTIVE_ARCHITECTURE}/${PKGNAME}/${_VERREV}
fi
mkdir -p "$WORK_DIR/BUILD" "$WORK_DIR/SOURCES" "$WORK_DIR/TARS" \
         "$WORK_DIR/SPECS" "$WORK_DIR/INSTALLROOT"
# If we are in development mode, then install directly in $WORK_DIR/$PKGPATH,
# so that we can do "make install" directly into BUILD/$PKGPATH and have
# changes being propagated.
# Moreover, devel packages should always go in the official WORK_DIR
if [ -n "$DEVEL_HASH" ]; then
  export BITS_BUILD_WORK_DIR="${WORK_DIR}"
  export INSTALLROOT="$WORK_DIR/$PKGPATH"
else
  export INSTALLROOT="$WORK_DIR/INSTALLROOT/$PKGHASH/$PKGPATH"
  export BITS_BUILD_WORK_DIR="${BITS_BUILD_WORK_DIR:-$WORK_DIR}"
fi

export BUILDROOT="$BITS_BUILD_WORK_DIR/BUILD/$PKGHASH"
export SOURCEDIR="$WORK_DIR/SOURCES/$PKGNAME/$PKGVERSION/$COMMIT_HASH"
export BUILDDIR="$BUILDROOT/$PKGNAME"

# All caching for RECC should happen relative to $WORK_DIR
export RECC_PROJECT_ROOT=$WORK_DIR
export RECC_WORKING_DIR_PREFIX=$WORK_DIR
# Moreover we allow caching stuff across different builds of the same
# package, but not across packages.
export RECC_PREFIX_MAP=$BUILDDIR=/recc/BUILDDIR-$PKGNAME:$INSTALLROOT=/recc/INSTALLROOT-$PKGNAME:$SOURCEDIR=/recc/SOURCEDIR-$PKGNAME
#export RECC_PREFIX_MAP=$RECC_PREFIX_MAP:$(readlink $BUILDDIR)=/recc/BUILDDIR-$PKGNAME:$(readlink $INSTALLROOT)=/recc/INSTALLROOT-$PKGNAME:$(readlink $SOURCEDIR)=/recc/SOURCEDIR-$PKGNAME
# No point in mixing packages
export RECC_ACTION_SALT="$PKGNAME"

# Safety guards: validate WORK_DIR and PKGHASH before any destructive rm
# operation.  An empty WORK_DIR would expand "$WORK_DIR/INSTALLROOT/$PKGHASH"
# to "/INSTALLROOT/..." (catastrophic); an empty PKGHASH would wipe the entire
# INSTALLROOT tree.  BUILDROOT inherits the same risk.
if [[ -z "${WORK_DIR}" || ! "${WORK_DIR}" = /* ]]; then
  echo "ERROR: WORK_DIR is empty or not an absolute path ('${WORK_DIR:-}') — aborting." >&2
  exit 1
fi
if [[ -z "${PKGHASH}" ]]; then
  echo "ERROR: PKGHASH is empty — refusing to rm -fr '$WORK_DIR/INSTALLROOT/'" >&2
  exit 1
fi
rm -fr "$WORK_DIR/INSTALLROOT/$PKGHASH"
# We remove the build directory only if we are not in incremental mode.
if [[ "$INCREMENTAL_BUILD_HASH" == 0 ]] && ! rm -rf "$BUILDROOT"; then
  # Golang installs stuff without write permissions for ourselves sometimes.
  # This makes the `rm -rf` above fail, so give ourselves write permission.
  chmod -R o+w "$BUILDROOT" || :
  rm -rf "$BUILDROOT"
fi
mkdir -p "$INSTALLROOT" "$BUILDROOT" "$BUILDDIR" "$WORK_DIR/INSTALLROOT/$PKGHASH/$PKGPATH"

# Legacy (aliBuild) recipes patch their source in place. The shared SOURCES tree
# is mounted read-only to stop one build from poisoning the source another build
# (or another recipe repo) reuses, so give this build a PRIVATE writable copy and
# point SOURCEDIR at it. The copy carries the patch sentinels, so patches are not
# re-applied. cleanup() removes it on exit (success or failure). Only active in
# legacy mode (BITS_PRIVATE_SOURCE=1, set by build.py). Done HERE, after the
# BUILDROOT reset above (which would otherwise delete the copy) and before the
# recipe runs.
if [ "${BITS_PRIVATE_SOURCE:-}" = 1 ] && [ -n "$PKGHASH" ] && [ -d "$SOURCEDIR" ]; then
  _bits_private_src="$BUILDROOT/.source"
  rm -rf "$_bits_private_src"
  mkdir -p "$_bits_private_src"
  echo "bits: legacy mode — private source copy $SOURCEDIR -> $_bits_private_src"
  rsync -a "$SOURCEDIR/" "$_bits_private_src/"
  export SOURCEDIR="$_bits_private_src"
fi

cd "$WORK_DIR/INSTALLROOT/$PKGHASH"
cat > "$INSTALLROOT/.meta.json" <<\EOF
%(provenance)s
EOF

# Per-package NOTICE (attribution + corresponding-source info), written by bits
# only when the recipe's license/acknowledgment require it; empty otherwise so no
# file is created. It travels with the package into the S3 store and CVMFS.
%(notice_block)s

# Add "source" command for dependencies to init.sh.
# Install init.sh now, so that it is available for debugging in case the build fails.
mkdir -p "$INSTALLROOT/etc/profile.d"
rm -f "$INSTALLROOT/etc/profile.d/init.sh"
cat <<\EOF > "$INSTALLROOT/etc/profile.d/init.sh"
%(initdotsh_deps)s
EOF

# Apply dependency initialisation now, but skip setting the variables below until after the build.
. "$INSTALLROOT/etc/profile.d/init.sh"

# Add support for direnv https://github.com/direnv/direnv/
#
# This is beneficial for all the cases where the build step requires some
# environment to be properly setup in order to work. e.g. to support ninja or
# protoc.
cat << EOF > "$BUILDDIR/.envrc"
# Source the build environment which was used for this package
WORK_DIR=\${WORK_DIR:-$WORK_DIR} source "\${WORK_DIR:-$WORK_DIR}/${INSTALLROOT#$WORK_DIR/}/etc/profile.d/init.sh"
source_up
EOF

cd "$BUILDROOT"
ln -snf "$PKGHASH" "$BITS_BUILD_WORK_DIR/BUILD/$PKGNAME-latest"
if [[ $DEVEL_PREFIX ]]; then
  ln -snf "$PKGHASH" "$BITS_BUILD_WORK_DIR/BUILD/$PKGNAME-latest-$DEVEL_PREFIX"
fi

cd "$BUILDDIR"

# Actual build script, as defined in the recipe

# This actually does the build, taking in to account shortcuts like
# having a pre build tarball or having an incremental recipe (in the
# case of development mode).
#
# - If the build was never done and we do not have a cached tarball,
#   build everything as usual.
# - If the build was started, we do not have a tarball, and we
#   have a non trivial incremental recipe, use it to continue the build.
# - If the build was started, but we do not have a incremental build recipe,
#   simply rebuild as usual.
# - In case we have a cached tarball, we skip the build and expand it, change
#   the relocation script so that it takes into account the new location.

function Run() { # dummy function
    true
}

if [[ "$CACHED_TARBALL" == "" && ! -f $BUILDROOT/log ]]; then
  set -o pipefail;
  # Keep DYLD_LIBRARY_PATH (set from dependencies by init.sh above) so build-time
  # tools on macOS find their dependencies' dylibs, mirroring LD_LIBRARY_PATH on
  # Linux. Inherited contamination was already cleared before init.sh ran.
  { set -e
    set -x
    source "$WORK_DIR/SPECS/$PKGPATH/$PKGNAME.sh"
    if [[ $(type -t Run) == function ]]; then Run "$@"; fi
  } 2>&1 | tee "$BUILDROOT/log"
  rc=${PIPESTATUS[0]}; [ "$rc" -eq 0 ] || exit "$rc"   # read PIPESTATUS[0] BEFORE any command clobbers it
elif [[ "$CACHED_TARBALL" == "" && $INCREMENTAL_BUILD_HASH != "0" && -f "$BUILDDIR/.build_succeeded" ]]; then
    set -o pipefail
    (%(incremental_recipe)s) 2>&1 | tee "$BUILDROOT/log"
    rc=${PIPESTATUS[0]}; [ "$rc" -eq 0 ] || exit "$rc"   # propagate the real recipe exit code (was masked to 1)
elif [[ "$CACHED_TARBALL" == "" ]]; then
   set -o pipefail;
   # Keep DYLD_LIBRARY_PATH (from dependencies via init.sh) — see note above.
   { set -e
     set -x
     source "$WORK_DIR/SPECS/$PKGPATH/$PKGNAME.sh"
     if [[ $(type -t Run) == function ]]; then Run "$@"; fi
   } 2>&1 | tee "$BUILDROOT/log"
   rc=${PIPESTATUS[0]}; [ "$rc" -eq 0 ] || exit "$rc"   # read PIPESTATUS[0] BEFORE any command clobbers it
else
  # Unpack the cached tarball in the $INSTALLROOT and remove the unrelocated
  # files.
  rm -rf "$BUILDROOT/log"
  # B1 FIX: double-quote all path variables so that a WORK_DIR or INSTALLROOT
  # containing spaces does not cause word-splitting — especially dangerous on
  # 'rm -rf' lines which could otherwise delete multiple unintended paths.
  mkdir -p "$WORK_DIR/TMP/$PKGHASH"
  # Tar-slip guard: a recalled store tarball may be attacker-controlled when
  # signed reuse is disabled. Refuse member names that are absolute or contain
  # a '..' component (belt over tar's own protections, which vary by
  # implementation/version), and never restore ownership from the archive.
  if tar -tzf "$CACHED_TARBALL" | grep -qE '(^/|(^|/)\.\.(/|$))'; then
    echo "ERROR: $CACHED_TARBALL contains absolute or traversing member paths — refusing to unpack" >&2
    exit 1
  fi
  tar -xzf "$CACHED_TARBALL" --no-same-owner -C "$WORK_DIR/TMP/$PKGHASH"
  mkdir -p "$(dirname "$INSTALLROOT")"
  rm -rf "$INSTALLROOT"
  mv "$WORK_DIR/TMP/$PKGHASH/$PKGPATH" "$INSTALLROOT"
  pushd "$WORK_DIR/INSTALLROOT/$PKGHASH"
  if [ -w "$INSTALLROOT" ]; then
      WORK_DIR=$WORK_DIR /bin/bash -ex "$INSTALLROOT/relocate-me.sh"
  fi
  popd
  find "$INSTALLROOT" -name "*.unrelocated" -delete
  rm -rf "$WORK_DIR/TMP/$PKGHASH"
fi

# Regenerate init.sh, in case the package build clobbered it. This
# particularly happens in the AliEn-Runtime package, since it copies other
# packages into its installroot wholesale.
# Notice how we only do it if $INSTALLROOT is writeable. If it is
# not, we assume it points to a CVMFS store which should be left untouched.
if [ -w "$INSTALLROOT" ]; then  # B5 FIX: quote $INSTALLROOT in -w test
mkdir -p "$INSTALLROOT/etc/profile.d"
rm -f "$INSTALLROOT/etc/profile.d/init.sh"
cat <<\EOF > "$INSTALLROOT/etc/profile.d/init.sh"
%(initdotsh_full)s
EOF

cd "$WORK_DIR/INSTALLROOT/$PKGHASH"
# Replace the .envrc to point to the final installation directory.
cat << EOF > "$BUILDDIR/.envrc"
# Source the build environment which was used for this package
WORK_DIR=\${WORK_DIR:-$WORK_DIR} source ../../../$PKGPATH/etc/profile.d/init.sh
source_up
# On mac we build with the proper installation relative RPATH,
# so this is not actually used and it's actually harmful since
# startup time is reduced a lot by the extra overhead from the
# dynamic loader
unset DYLD_LIBRARY_PATH
EOF

cat > "$INSTALLROOT/.meta.json" <<\EOF
%(provenance)s
EOF

cd "$WORK_DIR/INSTALLROOT/$PKGHASH/$PKGPATH"
# Find which files need relocation.
{ grep -I -H -l -R "\($WORK_DIR\|[@][@]PKGREVISION[@]$PKGHASH[@][@]\)" . || true; } | sed -e 's|^\./||' > "$INSTALLROOT/etc/profile.d/.bits-relocate"

# Relocate script for <arch>/<pkgname>/<pkgver> structure

cat > "$INSTALLROOT/etc/profile.d/.bits-pkginfo" <<EoF
OP=${PKGPATH}
PP=\${PKGPATH:-${PKGPATH}}
PH=${PKGHASH}
PKG_DIR="$WORK_DIR"
EoF

install "${BITS_SCRIPT_DIR}/bits_helpers/relocate-me.sh" "$INSTALLROOT/"

# Always relocate the modulefile (if present) so that it works also in devel mode.
if [[ -f "$INSTALLROOT/etc/modulefiles/$PKGNAME" ]]; then
  echo "mv -f \$PP/etc/modulefiles/$PKGNAME \$PP/etc/modulefiles/${PKGNAME}.forced-relocation && sed -e \"s|[@][@]PKGREVISION[@]\$PH[@][@]|$PKGREVISION|g\" \$PP/etc/modulefiles/${PKGNAME}.forced-relocation > \$PP/etc/modulefiles/$PKGNAME" >> "$INSTALLROOT/relocate-me.sh"
fi

# Find libraries and executables needing relocation on macOS
if [[ ${ARCHITECTURE:0:3} == "osx" ]]; then
  otool_arch=$(echo "${ARCHITECTURE#osx_}" | tr - _)  # otool knows x86_64, not x86-64

  /usr/bin/find ${RELOCATE_PATHS:-bin lib lib64} -type d \( -name '*.dist-info' -o -path '*/pytz/zoneinfo' \) -prune -false -o -type f \
                -not -name '*.py' -not -name '*.pyc' -not -name '*.pyi' -not -name '*.pxd' -not -name '*.inc' -not -name '*.js' -not -name '*.json' \
                -not -name '*.xml' -not -name '*.xsl' -not -name '*.txt' -not -name '*.dat' -not -name '*.mat' -not -name '*.sav' -not -name '*.csv' \
                -not -name '*.wav' -not -name '*.png' -not -name '*.svg' -not -name '*.css' -not -name '*.html' -not -name '*.woff' -not -name '*.woff2' -not -name '*.ttf' \
                -not -name LICENSE -not -name COPYING -not -name '*.c' -not -name '*.cc' -not -name '*.cxx' -not -name '*.cpp' -not -name '*.h' -not -name '*.hpp' |
    while read -r BIN; do
      MACHOTYPE=$(set +o pipefail; otool -arch "$otool_arch" -h "$PWD/$BIN" 2> /dev/null | grep filetype -A1 | awk 'END{print $5}')

      # See mach-o/loader.h from XNU sources: 2 == executable, 6 == dylib, 8 == bundle
      if [[ $MACHOTYPE == 6 || $MACHOTYPE == 8 ]]; then
        # Only dylibs: relocate LC_ID_DYLIB
        if otool -arch "$otool_arch" -D "$PWD/$BIN" 2> /dev/null | tail -n1 | grep -q "$PKGHASH"; then
          cat <<EOF >> "$INSTALLROOT/relocate-me.sh"
install_name_tool -id "\$(otool -arch $otool_arch -D "\$PP/$BIN" | tail -n1 | sed -e "s|/[^ ]*INSTALLROOT/\$PH/\$OP|\$WORK_DIR/\$PP|g")" "\$PP/$BIN"
EOF
        elif otool -arch "$otool_arch" -D "$PWD/$BIN" 2> /dev/null | tail -n1 | grep -vq /; then
          cat <<EOF >> "$INSTALLROOT/relocate-me.sh"
install_name_tool -id "\$WORK_DIR/\$PP/$BIN" "\$PP/$BIN"
EOF
        fi
      fi

      if [[ $MACHOTYPE == 2 || $MACHOTYPE == 6 || $MACHOTYPE == 8 ]]; then
        # Both libs and binaries: relocate LC_RPATH
        if otool -arch "$otool_arch" -l "$PWD/$BIN" 2> /dev/null | grep -A2 LC_RPATH | grep path | grep -q "$PKGHASH"; then
          cat <<EOF >> "$INSTALLROOT/relocate-me.sh"
OLD_RPATHS=\$(otool -arch $otool_arch -l "\$PP/$BIN" | grep -A2 LC_RPATH | grep path | grep "\$PH" | sed -e 's|^.*path ||' -e 's| .*$||' | sort -u)
for OLD_RPATH in \$OLD_RPATHS; do
  NEW_RPATH=\${OLD_RPATH/#*INSTALLROOT\/\$PH\/\$OP/\$WORK_DIR/\$PP}
  install_name_tool -rpath "\$OLD_RPATH" "\$NEW_RPATH" "\$PP/$BIN"
done
EOF
        fi

        # Both libs and binaries: relocate LC_LOAD_DYLIB
        if otool -arch "$otool_arch" -l "$PWD/$BIN" 2> /dev/null | grep -A2 LC_LOAD_DYLIB | grep name | grep -q $PKGHASH; then
          cat <<EOF >> "$INSTALLROOT/relocate-me.sh"
OLD_LOAD_DYLIBS=\$(otool -arch $otool_arch -l "\$PP/$BIN" | grep -A2 LC_LOAD_DYLIB | grep name | grep "\$PH" | sed -e 's|^.*name ||' -e 's| .*$||' | sort -u)
for OLD_LOAD_DYLIB in \$OLD_LOAD_DYLIBS; do
  NEW_LOAD_DYLIB=\${OLD_LOAD_DYLIB/#*INSTALLROOT\/\$PH\/\$OP/\$WORK_DIR/\$PP}
  install_name_tool -change "\$OLD_LOAD_DYLIB" "\$NEW_LOAD_DYLIB" "\$PP/$BIN"
done
EOF
        fi
      fi
    done || true
fi

cat "$INSTALLROOT/relocate-me.sh"
fi
cd "$WORK_DIR/INSTALLROOT/$PKGHASH"

# Run post-install hooks
if [[ $PKGNAME != defaults-* ]]; then
  run_hooks "POST_INSTALL"
fi

# Relativise in-tree absolute symlinks so the artefact is relocatable.
# A build can install an absolute symlink into its own INSTALLROOT (e.g. bzip2's
# bin/bzless -> $INSTALLROOT/bin/bzmore). That target is a build-time path: it
# dangles once the tree is unpacked anywhere else, and CVMFS refuses absolute
# symlink targets outright. Rewrite any symlink whose target is absolute AND
# lands inside this build tree to a path relative to the link's own directory, so
# BOTH the tarball and the rsynced local install are relocatable everywhere — S3
# reuse and `bits publish` to CVMFS from the command line, not just the console
# publish loop. System / cross-tree symlinks (target outside this tree) are left
# untouched. Done here, before the rsync + tar below, so both see the fix.
_pack_root="$WORK_DIR/INSTALLROOT/$PKGHASH"
find "$_pack_root" -type l | while IFS= read -r _lnk; do
  _tgt="$(readlink "$_lnk")"
  case "$_tgt" in
    "$_pack_root"/*)
      if _rel="$(realpath -m --relative-to="$(dirname "$_lnk")" "$_tgt" 2>/dev/null)" \
         && [ -n "$_rel" ]; then
        ln -sfn "$_rel" "$_lnk"
      fi
      ;;
  esac
done
unset _pack_root

# Archive creation
# B7 FIX: replace backtick with $(...) and quote $PKGHASH; use -c (chars) consistently.
HASHPREFIX=$(echo "$PKGHASH" | cut -c1,2)
HASH_PATH=$EFFECTIVE_ARCHITECTURE/store/$HASHPREFIX/$PKGHASH
mkdir -p "${WORK_DIR}/TARS/$HASH_PATH" \
         "${WORK_DIR}/TARS/$EFFECTIVE_ARCHITECTURE/$PKGNAME"

PACKAGE_WITH_REV=$PKGNAME-${_VERREV}.$EFFECTIVE_ARCHITECTURE.tar.gz
# Copy and tar/compress (if applicable) in parallel.
# Use -H to match tar's behaviour of preserving hardlinks.
rsync -aH "$WORK_DIR/INSTALLROOT/$PKGHASH/" "$WORK_DIR" & rsync_pid=$!
if [ "$CAN_DELETE" = 1 ] && [ -z "$BITS_HAS_WRITE_STORE" ]; then
  # We're deleting the tarball anyway, so no point in creating a new one.
  # There might be an old existing tarball, and we should delete it.
  # (When a write store is configured the tarball is still needed for upload, so
  # we fall through and create it; doFinalSync removes it again after upload.)
  rm -f "$WORK_DIR/TARS/$HASH_PATH/$PACKAGE_WITH_REV"
elif [ -z "$CACHED_TARBALL" ]; then
  # Deterministic packaging (finding R1): the store tarball must be byte-identical
  # across build nodes, or two builds of the same hash record different
  # tarball_sha256 and certification fails. So: archive a SORTED member list with
  # zeroed numeric owner/group and a fixed mtime, and PIN the compressor. Default
  # is gzip -n (fully deterministic); a farm with a uniform pigz may override
  # BITS_TAR_COMPRESSOR (e.g. "pigz -n -p4") — never plain pigz, whose output
  # depends on the node's thread count. Byte-identity across nodes assumes a
  # uniform tar + compressor toolchain (same gzip/pigz version). Check a platform
  # with tools/verify-deterministic-tarball.sh.
  _comp=${BITS_TAR_COMPRESSOR:-gzip -n}
  _dst="$WORK_DIR/TARS/$HASH_PATH/$PACKAGE_WITH_REV.processing"
  # Prefer GNU tar: it normalises mtime/owner IN THE ARCHIVE (no on-disk change).
  if command -v gtar >/dev/null 2>&1; then _tar=gtar
  elif tar --version 2>/dev/null | grep -qi 'GNU tar'; then _tar=tar
  else _tar=; fi
  if [ -n "$_tar" ]; then
    "$_tar" --sort=name --owner=0 --group=0 --numeric-owner --mtime='@0' \
        -cC "$WORK_DIR/INSTALLROOT/$PKGHASH" . | $_comp -c > "$_dst"
  else
    # bsdtar (e.g. macOS without gtar): deterministic order + numeric zero owner.
    # bsdtar cannot set a uniform archive mtime, so packages are byte-reproducible
    # here only if file mtimes already match — install GNU tar (brew install
    # gnu-tar) on macOS build nodes for fully reproducible packages.
    echo "bits: WARNING: GNU tar not found; $PKGNAME tarball may not be byte-reproducible (install gnu-tar)." >&2
    ( cd "$WORK_DIR/INSTALLROOT/$PKGHASH" && find . -print | LC_ALL=C sort > "$_dst.list" )
    ( cd "$WORK_DIR/INSTALLROOT/$PKGHASH" && tar --no-recursion --uid 0 --gid 0 \
        --numeric-owner -T "$_dst.list" -cf - ) | $_comp -c > "$_dst"
    rm -f "$_dst.list"
  fi
  mv "$_dst" "$WORK_DIR/TARS/$HASH_PATH/$PACKAGE_WITH_REV"
  ln -nfs "../../$HASH_PATH/$PACKAGE_WITH_REV" \
     "$WORK_DIR/TARS/$EFFECTIVE_ARCHITECTURE/$PKGNAME/$PACKAGE_WITH_REV"
fi
wait "$rsync_pid"

# We've copied files into their final place; now relocate.
# Use $PKGPATH (= $EFFECTIVE_ARCHITECTURE[/$PKGFAMILY]/$PKGNAME/$_VERREV) so
# that PKGFAMILY packages (e.g. externals/foo, cms/bar) are found correctly.
cd "$WORK_DIR"
if [ -w "$WORK_DIR/$PKGPATH" ]; then
  /bin/bash -ex "$PKGPATH/relocate-me.sh"
fi

# Last package built gets a "latest" mark.
# dirname of $PKGPATH = $EFFECTIVE_ARCHITECTURE[/$PKGFAMILY]/$PKGNAME
# B4 FIX: quote ${_VERREV} and $(dirname ...) so spaces in PKGPATH don't split.
ln -snf "${_VERREV}" "$(dirname "$PKGPATH")/latest"

# Latest package built for a given devel prefix gets latest-$BUILD_FAMILY
if [[ $BUILD_FAMILY ]]; then
  ln -snf "${_VERREV}" "$(dirname "$PKGPATH")/latest-${BUILD_FAMILY}"
fi

# When the package is definitely fully installed, install the file that marks
# the package as successful.
if [ -w "$WORK_DIR/$PKGPATH" ]; then
  echo "$PKGHASH" > "$WORK_DIR/$PKGPATH/.build-hash"
fi
# Mark the build as successful with a placeholder. Allows running incremental
# recipe in case the package is in development mode.
echo "${DEVEL_HASH}${DEPS_HASH}" > "$BUILDDIR/.build_succeeded"

