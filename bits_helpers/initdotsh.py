# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Generate a package's ``etc/profile.d/init.sh`` — the shell fragment sourced to
put the package and its dependency closure on PATH/LD_LIBRARY_PATH etc. Split out
of build.py; pure string generation from resolved specs, no build side effects."""

from os.path import abspath
from shlex import quote

from bits_helpers.arch import SHARED_ARCH
from bits_helpers.log import dieOnError
from bits_helpers.utilities import (asList, pkg_to_shell_id, resolve_spec_data,
                                    topological_sort, ver_rev)

def generate_initdotsh(package, specs, architecture, workDir="sw", post_build=False,
                       from_modules=False, cmake_prefix_env=False,
                       reuse_cvmfs_base=None):
  """Return the contents of the given package's etc/profile/init.sh as a string.

  If post_build is true, also generate variables pointing to the package
  itself; else, only generate variables pointing at it dependencies.

  If from_modules is true (the --initdotsh-from-modules build mode), the
  post_build self-environment additionally exposes the development/build
  variables the runtime modulefile carries but the legacy init.sh omits
  (<PKG>_INCLUDE_DIR, Python site-packages on PYTHONPATH), generated from the
  package root and guarded on existence. Off by default, so the generated text
  is byte-identical to before when the mode is not active.

  If cmake_prefix_env is true (legacy/alidist builds that opt in via the
  hashed defaults env knob BITS_LEGACY_CMAKE_PREFIX_PATH), each package root is
  also exported on the ':'-separated CMAKE_PREFIX_PATH environment variable,
  which CMake's find_package() reads natively on Unix. Off by default so the
  text stays byte-identical to aliBuild's when the knob is not set.
  """
  spec = specs[package]
  # Allow users to override BITS_ARCH_PREFIX if they manually source
  # init.sh. This is useful for development off CVMFS, since we have a
  # slightly different directory hierarchy there.
  lines = [': "${BITS_ARCH_PREFIX:=%s}"' % architecture]
  lines.extend([
    'if [ -z "${WORK_DIR}" ]; then',
    '    WORK_DIR=%s' % abspath(workDir),
    'fi',
  ])
  # Generate the part which sources the environment for all the dependencies.
  # We guarantee that a dependency is always sourced before the parts
  # depending on it, but we do not guarantee anything for the order in which
  # unrelated components are activated.
  # These variables are also required during the build itself, so always
  # generate them.
  def _arch_prefix_expr(dep_spec):
    """Return the shell expression for the install-tree root of *dep_spec*.

    Arch-specific packages use the runtime variable ``$BITS_ARCH_PREFIX`` so
    that the same init.sh works when relocated (e.g. off CVMFS).
    Shared packages (``architecture: shared``) always live under the literal
    directory ``shared/``, so we embed that string directly.
    """
    if dep_spec.get("architecture") == SHARED_ARCH:
      return '"$WORK_DIR/shared"'
    return '"$WORK_DIR/$BITS_ARCH_PREFIX"'

  def _dep_init_path(dep):
    dep_spec = specs[dep]
    family = dep_spec.get("pkg_family", "")
    family_seg = (quote(family) + "/") if family else ""
    arch_prefix = _arch_prefix_expr(dep_spec)
    # ver_rev(dep_spec) is used instead of "{version}-{revision}" so that
    # dependencies whose revision was forced or dropped via force_revision in
    # defaults are sourced from the correct path in the generated init.sh.
    # Using the raw revision string here would produce a trailing dash
    # ("8.5.0-") when force_revision is set to "" (empty), breaking the
    # environment for every downstream package.
    return (
      '[ -n "${{{bigpackage}_REVISION}}" ] || '
      '. {arch_prefix}/{family}{package}/{ver_rev}/etc/profile.d/init.sh'
    ).format(
      bigpackage=pkg_to_shell_id(dep),
      arch_prefix=arch_prefix,
      family=family_seg,
      package=quote(dep_spec["package"]),
      ver_rev=quote(ver_rev(dep_spec)),
    )
  # A dependency satisfied from a reused CVMFS release is set up by sourcing its
  # DEPLOYED init.sh from CVMFS — the same mechanism as a local dep, just from
  # the deployment. The deployed init.sh resolves paths via "$WORK_DIR/
  # $BITS_ARCH_PREFIX", so we point those at the CVMFS Packages base while
  # sourcing (and restore after) so its own and its transitive deps' paths land
  # on CVMFS. Per-DEPENDENCY, so a legacy-built package can consume a reused dep.
  # Needs /cvmfs mounted in the build container (no modulecmd required).
  _reqs = list(spec.get("requires", ()))
  _reused_set = {d for d in _reqs
                 if reuse_cvmfs_base and specs[d].get("reuse_module_id")}

  def _reused_dep_lines(d):
    # Point the deployed init.sh's "$WORK_DIR/$BITS_ARCH_PREFIX" at the CVMFS
    # Packages base. BITS_ARCH_PREFIX MUST be non-null (the deployed init.sh's
    # `: "${BITS_ARCH_PREFIX:=<arch>}"` would otherwise re-add the arch); "." is
    # a harmless no-op segment (<base>/./<pkg> == <base>/<pkg>). Save/restore so
    # locally-built deps keep the local WORK_DIR.
    dep_spec = specs[d]
    verrev = dep_spec["reuse_module_id"].split("/", 1)[1]
    return [
      '_bits_swd="${WORK_DIR:-}"; _bits_sap="${BITS_ARCH_PREFIX:-}"',
      'WORK_DIR="%s"; BITS_ARCH_PREFIX="."' % reuse_cvmfs_base,
      '[ -n "${%s_REVISION}" ] || . "%s/%s/%s/etc/profile.d/init.sh"'
      % (pkg_to_shell_id(d), reuse_cvmfs_base, dep_spec["package"], verrev),
      'WORK_DIR="${_bits_swd}"; BITS_ARCH_PREFIX="${_bits_sap}"; '
      'unset _bits_swd _bits_sap',
    ]

  if _reused_set:
    # Emit deps in topological order (prerequisites first) so a dep set up
    # before a reused dep whose deployed init.sh transitively references it —
    # e.g. a locally-built bits-recipe-tools before a reused CMake — sets its
    # _REVISION first, and the deployed init.sh's guard skips the re-source
    # (which would look on CVMFS where a local-only build does not exist).
    _req_set = set(_reqs)
    _order = [d for d in topological_sort(specs) if d in _req_set]
    for d in _order:
      if d in _reused_set:
        lines.extend(_reused_dep_lines(d))
      else:
        lines.append(_dep_init_path(d))
    # A reused CVMFS package may ship a pkg-config .pc whose baked `prefix=` does
    # not match its deployed location (publish-time relocation can misplace it),
    # breaking find_package via pkg-config for a consumer (e.g. xrootd → Davix).
    # The reuse anchoring already resolved each dep's real root into <PKG>_ROOT,
    # so stage corrected .pc copies (prefix rewritten to that root) in a writable
    # dir and prepend it to PKG_CONFIG_PATH. Reads from read-only /cvmfs, writes
    # under $WORK_DIR; a no-op for reused deps that ship no .pc.
    _reused_roots = " ".join('"${%s_ROOT:-}"' % pkg_to_shell_id(d)
                             for d in _order if d in _reused_set)
    lines.extend([
      '_bits_rpc="${WORK_DIR:-.}/reuse-pkgconfig"; mkdir -p "$_bits_rpc"',
      'for _bits_root in %s; do' % _reused_roots,
      '  [ -n "$_bits_root" ] || continue',
      '  for _bits_pcd in "$_bits_root/lib64/pkgconfig" "$_bits_root/lib/pkgconfig"; do',
      '    [ -d "$_bits_pcd" ] || continue',
      '    for _bits_pc in "$_bits_pcd"/*.pc; do',
      '      [ -e "$_bits_pc" ] || continue',
      '      sed "s|^prefix=.*|prefix=$_bits_root|" "$_bits_pc" > "$_bits_rpc/${_bits_pc##*/}"',
      '    done',
      '  done',
      'done',
      # Prepend once — init.sh may be sourced repeatedly; avoid unbounded growth.
      'case ":${PKG_CONFIG_PATH:-}:" in',
      '  *":$_bits_rpc:"*) ;;',
      '  *) export PKG_CONFIG_PATH="$_bits_rpc${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}" ;;',
      'esac',
      'unset _bits_rpc _bits_root _bits_pcd _bits_pc',
    ])
  else:
    lines.extend(_dep_init_path(dep) for dep in _reqs)

  if post_build:
    bigpackage = pkg_to_shell_id(package)

    # Set standard variables related to the package itself. These should only
    # be set once the build has actually completed.
    self_family = spec.get("pkg_family", "")
    self_family_seg = (quote(self_family) + "/") if self_family else ""
    self_arch_prefix = _arch_prefix_expr(spec)
    lines.extend(line.format(
      bigpackage=bigpackage,
      arch_prefix=self_arch_prefix,
      family=self_family_seg,
      package=quote(spec["package"]),
      version=quote(spec["version"]),
      # ver_rev() produces "version-revision" or just "version" when
      # force_revision is set to "" via defaults; the ROOT export path must
      # match the actual install directory produced by _pkg_install_path().
      ver_rev=quote(ver_rev(spec)),
      revision=quote(spec["revision"]),
      hash=quote(spec["hash"]),
      commit_hash=quote(spec["commit_hash"]),
    ) for line in (
      'export {bigpackage}_ROOT={arch_prefix}/{family}{package}/{ver_rev}',
      'export RECC_PREFIX_MAP="${bigpackage}_ROOT=/recc/{bigpackage}_ROOT:$RECC_PREFIX_MAP"',
      "export {bigpackage}_VERSION={version}",
      "export {bigpackage}_REVISION={revision}",
      "export {bigpackage}_HASH={hash}",
      "export {bigpackage}_COMMIT={commit_hash}",
    ))

    # Generate the part which sets the environment variables related to the
    # package itself. This can be variables set via the "env" keyword in the
    # metadata or paths which get concatenated via the "{append,prepend}_path"
    # keys. These should only be set once the build has actually completed,
    # since the paths referred to will only exist then.

    # First, output a sensible error message if types are wrong.
    for key in ("env", "append_path", "prepend_path"):
      dieOnError(not isinstance(spec.get(key, {}), dict),
                 "Tag `{}' in {} should be a dict.".format(key, package))

    # Set "env" variables.
    # We only put the values in double-quotes, so that they can refer to other
    # shell variables or do command substitution (e.g. $(brew --prefix ...)).
    lines.extend('export {}="{}"'.format(key, resolve_spec_data(spec, value, ""))
                 for key, value in spec.get("env", {}).items())

    # Append paths to variables, if requested using append_path.
    # Again, only put values in double quotes so that they can refer to other variables.
    lines.extend('export {key}="${key}:{value}"'
                 .format(key=key, value=":".join(asList(value)))
                 for key, value in spec.get("append_path", {}).items())

    # First convert all values to list, so that we can use .setdefault().insert() below.
    prepend_path = {key: [resolve_spec_data(spec, dir, "") for dir in asList(value)]
                    for key, value in spec.get("prepend_path", {}).items()}
    # By default we add the .../bin directory to PATH, .../lib to LD_LIBRARY_PATH
    # and .../lib*/pkgconfig to PKG_CONFIG_PATH.  Prepend to these paths, so that
    # our packages win against system ones.
    #
    # PKG_CONFIG_PATH is added generically here so that the *build-time*
    # environment mirrors what each package's runtime modulefile exposes via the
    # ModuleRecipe `--pkgconfig` flag: a downstream recipe's ./configure or cmake
    # then finds every dependency's .pc files without the recipe having to declare
    # `prepend_path: { PKG_CONFIG_PATH: ... }` by hand.  Each entry is guarded by a
    # directory-existence test below, so adding it for every dependency is safe
    # (it is a no-op for packages that ship no pkgconfig directory).
    #
    # CMAKE_PREFIX_PATH is deliberately NOT added here: CMake recipes pass it on
    # the cmake command line as a `;`-separated -D argument (built by CMakeRecipe's
    # _SetBuildEnvBase), whereas an environment variable would need `:` separators
    # on Unix.  Mixing the two on the same name corrupts the list, so build-time
    # CMAKE_PREFIX_PATH stays owned by CMakeRecipe.
    # The dynamic-loader search path is platform-specific: macOS dyld uses
    # DYLD_LIBRARY_PATH (and ignores LD_LIBRARY_PATH), Linux uses LD_LIBRARY_PATH.
    # Emit only the relevant one so build-time tools find their dependencies'
    # shared libraries — on macOS this is what lets e.g. protoc -> Abseil work
    # after the install-time rpath is stripped. The build environment must NOT
    # unset this variable after sourcing init.sh (see build_template.sh).
    _lib_path_var = "DYLD_LIBRARY_PATH" if architecture.startswith("osx") else "LD_LIBRARY_PATH"
    for key, value in (("PATH", "bin"),
                       (_lib_path_var, "lib"), (_lib_path_var, "lib64"),
                       ("PKG_CONFIG_PATH", "lib/pkgconfig"), ("PKG_CONFIG_PATH", "lib64/pkgconfig")):
      prepend_path.setdefault(key, []).insert(0, f"${bigpackage}_ROOT/{value}")
    lines.extend('[ ! -d "{value}" ] || export {key}="{value}${{{key}+:${key}}}"'
                 .format(key=key, value=dir)
                 for key, value in prepend_path.items()
                 for dir in value)

    # Legacy/alidist builds, opted in via the hashed defaults env knob
    # BITS_LEGACY_CMAKE_PREFIX_PATH: expose each package root on the
    # ':'-separated CMAKE_PREFIX_PATH ENVIRONMENT variable. This mirrors at
    # build time what the runtime modulefiles already provide
    # (alibuild-generate-module --cmake emits `prepend-path CMAKE_PREFIX_PATH`),
    # the same build/runtime-parity rationale as the generic PKG_CONFIG_PATH
    # above. Needed because aliBuild's init.sh sets only <PKG>_ROOT, which
    # CMake ignores for packages whose cmake_minimum_required predates
    # CMP0074/CMP0144 (e.g. VecGeom's builtin VecCore 0.8.0 requiring 3.9
    # cannot find Vc under CMake 4). Gated off in from_modules mode, which
    # already emits its own CMAKE_PREFIX_PATH entry.
    if cmake_prefix_env and not from_modules:
      _cpp_root = "${%s_ROOT}" % bigpackage
      lines.append('[ ! -d "%s" ] || export '
                   'CMAKE_PREFIX_PATH="%s${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"'
                   % (_cpp_root, _cpp_root))

    if from_modules:
      # --initdotsh-from-modules: also expose the development/build environment
      # the runtime modulefile provides but the legacy init.sh omits — the
      # package's own headers (<PKG>_INCLUDE_DIR) and Python site-packages on
      # PYTHONPATH. Each package sets only its own; a consumer that sources the
      # dependency chain therefore accumulates the whole closure, matching what
      # loading the modulefile chain would yield. Everything is generated from
      # the package root bits already knows and guarded on directory existence,
      # so it is a no-op for packages that ship no headers / Python modules.
      # CMAKE_PREFIX_PATH is set as the ':'-separated environment variable, which
      # CMake's find_package() reads natively on Unix (in addition to any
      # ';'-separated -D cache value). So CMakeRecipe's reconstruction is gated
      # off under this mode (it would otherwise overwrite this with a ';'-list).
      root = "${%s_ROOT}" % bigpackage
      lines.append('[ ! -d "%s/include" ] || export %s_INCLUDE_DIR="%s/include"'
                   % (root, bigpackage, root))
      lines.append('[ ! -d "%s" ] || export '
                   'CMAKE_PREFIX_PATH="%s${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"'
                   % (root, root))
      lines.append(
        'for _bits_sp in "%s"/lib/python*/site-packages '
        '"%s"/lib/python/site-packages; do [ -d "$_bits_sp" ] && export '
        'PYTHONPATH="$_bits_sp${PYTHONPATH:+:$PYTHONPATH}"; done; unset _bits_sp'
        % (root, root))

  # Return string without a trailing newline, since we expect call sites to
  # append that (and the obvious way to inesrt it into the build template is by
  # putting the "%(initdotsh_*)s" on its own line, which has the same effect).
  return "\n".join(lines)
