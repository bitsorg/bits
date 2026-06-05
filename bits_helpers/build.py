from os.path import abspath, exists, basename, dirname, join, realpath
from os import makedirs, unlink, readlink, rmdir
from pathlib import Path
from bits_helpers import __version__
from bits_helpers.analytics import report_event
from bits_helpers.log import debug, info, banner, warning
from bits_helpers.log import dieOnError
from bits_helpers.repo_provider import fetch_repo_providers_iteratively, load_always_on_providers
from bits_helpers.memory import effective_jobs
from bits_helpers.checksum import (parse_entry as parse_checksum_entry,
                                    enforcement_mode as checksum_enforcement_mode,
                                    write_checksums_enabled,
                                    checksum_file as compute_checksum_file)
from bits_helpers.checksum_store import write_checksum_file as write_pkg_checksum_file
from bits_helpers.cmd import execute, DockerRunner, BASH, install_wrapper_script, getstatusoutput
from bits_helpers.sandbox import wrap_build_command
from bits_helpers.utilities import prunePaths, symlink, call_ignoring_oserrors, topological_sort, detectArch
from bits_helpers.utilities import resolve_store_path, effective_arch, SHARED_ARCH, compute_combined_arch, pkg_to_shell_id, ver_rev
from bits_helpers.utilities import parseDefaults, readDefaults
from bits_helpers.utilities import getPackageList, asList
from bits_helpers.utilities import validateDefaults
from bits_helpers.utilities import Hasher
from bits_helpers.utilities import resolve_tag, resolve_version, short_commit_hash, resolve_spec_data, resolveLocalPath
from bits_helpers.git import Git, git
from bits_helpers.sl import Sapling
from bits_helpers.scm import SCMError
from bits_helpers.sync import remote_from_url
from bits_helpers.workarea import logged_scm, updateReferenceRepoSpec, checkout_sources
try:
  from bits_helpers.resource_monitor import run_monitor_on_command
except:
  pass
from bits_helpers.log import ProgressPrint, log_current_package
from glob import glob
from collections import OrderedDict
from shlex import quote
from textwrap import dedent
import tempfile

import concurrent.futures
import importlib
import json
import socket
import os
import re
import shutil
import shlex
import sys
import time
import subprocess

from jinja2.sandbox import SandboxedEnvironment

def writeAll(fn, txt) -> None:
  f = open(fn, "w")
  f.write(txt)
  f.close()


def _generate_create_links_sh(spec, specs, args) -> str:
  """Generate a self-contained shell script that recreates the dist symlink trees.

  Used by the Makeflow .build rule (--pipeline --makeflow) so that dist-link
  creation runs inside the build rule instead of requiring Python's ``specs``
  dict later.  The generated script bakes in all dependency information at
  Python build time.
  """
  from bits_helpers.utilities import effective_arch, ver_rev, resolve_links_path
  lines = ["#!/usr/bin/env bash", "set -e", ""]
  for repo_type, requires_key in [
    ("dist",         "full_requires"),
    ("dist-direct",  "requires"),
    ("dist-runtime", "full_runtime_requires"),
  ]:
    target_dir = (
      "{work_dir}/TARS/{arch}/{repo}/{package}/{package}-{ver_rev}"
      .format(
        work_dir=args.workDir, arch=args.architecture,
        repo=repo_type, ver_rev=ver_rev(spec), **spec,
      )
    )
    lines.append("# -- %s --" % repo_type)
    # FIX: quote() prevents spaces, semicolons, or other shell metacharacters in
    # workDir or package names from being interpreted when the generated script runs.
    lines.append("rm -rf %s" % quote(target_dir))
    lines.append("mkdir -p %s" % quote(target_dir))
    for pkg in [spec["package"]] + list(spec[requires_key]):
      dep_spec = specs[pkg]
      dep_arch = effective_arch(dep_spec, args.architecture)
      dep_tarball = (
        "../../../../../TARS/{arch}/store/{short_hash}/{hash}/{package}-{ver_rev}.{arch}.tar.gz"
        .format(arch=dep_arch, short_hash=dep_spec["hash"][:2],
                ver_rev=ver_rev(dep_spec), **dep_spec)
      )
      lines.append('ln -nfs %s %s/' % (quote(dep_tarball), quote(target_dir)))
    lines.append("")
  return "\n".join(lines)


def _prefetch_package(spec, sync_helper, work_dir, build_arch) -> None:
  """Background task: prefetch the prebuilt tarball + all source archives.

  Uses the sentinel-file mechanism (``<path>.downloading`` files; see
  ``bits_helpers.download``) so that the main build loop and Makeflow shell
  rules can detect in-progress downloads and wait for completion.

  Sentinel for the tarball: ``<tar_hash_dir>.downloading``.
  Sentinels for source archives: ``<source_file>.downloading`` (managed inside
  ``download()`` via ``_acquire_download``/``_wait_for_sentinel``).

  This function is designed to be run in a thread pool; any exception is
  propagated to the executor framework.
  """
  from bits_helpers.download import _acquire_download, _wait_for_sentinel, download
  from bits_helpers.checksum import parse_entry as _pe

  arch = effective_arch(spec, build_arch)
  tar_hash_dir = os.path.join(work_dir, resolve_store_path(arch, spec["hash"]))

  # --- Tarball prefetch -------------------------------------------------------
  if not spec.get("is_devel_pkg"):
    # Try to atomically claim the tarball download slot.
    # sentinel path: tar_hash_dir + ".downloading"
    if _acquire_download(tar_hash_dir):
      try:
        os.makedirs(tar_hash_dir, exist_ok=True)
        sync_helper.fetch_tarball(spec)
      finally:
        # Always remove the sentinel so the main loop is never left waiting.
        sentinel = tar_hash_dir + ".downloading"
        try:
          os.unlink(sentinel)
        except OSError:
          pass
    else:
      # Another thread is already fetching this tarball; just wait.
      _wait_for_sentinel(tar_hash_dir)

  # --- Source archive prefetch ------------------------------------------------
  # download() already uses _acquire_download/_wait_for_sentinel internally, so
  # concurrent prefetch threads coordinate automatically.
  source_parent = os.path.join(work_dir, "SOURCES", spec["package"], spec["version"])
  checksums = spec.get("source_checksums") or {}
  for s in spec.get("sources", []):
    url, inline_checksum = _pe(s)
    src_checksum = checksums.get(url) or inline_checksum
    try:
      download(url, source_parent, work_dir, checksum=src_checksum,
               enforce_mode="off", sync_helper=sync_helper)
    except Exception:
      # Prefetch is best-effort: log the error but don't abort.
      debug("Prefetch: error downloading %s for %s (will retry at build time)",
            url, spec.get("package", "?"))


def readHashFile(fn):
  try:
    return open(fn).read().strip("\n")
  except OSError:
    return "0"


def update_git_repos(args, specs, buildOrder):
    """Update and/or fetch required git repositories in parallel.

    If any repository fails to be fetched, then it is retried, while allowing the
    user to input their credentials if required.
    """

    def update_repo(package, git_prompt):
        # Note: spec["scm"] should already be initialized before this is called
        # This function just updates the repository and fetches refs
        assert "scm" in specs[package], f"specs[{package!r}] has no scm key"
        updateReferenceRepoSpec(args.referenceSources, package, specs[package],
                                fetch=args.fetchRepos, allowGitPrompt=git_prompt)

        # Retrieve git heads
        output = logged_scm(specs[package]["scm"], package, args.referenceSources,
                            specs[package]["scm"].listRefsCmd(specs[package].get("reference", specs[package]["source"])),
                            ".", prompt=git_prompt, logOutput=False)
        specs[package]["scm_refs"] = specs[package]["scm"].parseRefs(output)

    progress = ProgressPrint("Updating repositories")
    requires_auth = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        future_to_download = {
          executor.submit(update_repo, package, git_prompt=False): package
          for package in buildOrder if "source" in specs[package]
        }
        for i, future in enumerate(concurrent.futures.as_completed(future_to_download)):
            futurePackage = future_to_download[future]
            progress("[%d/%d] Updating repository for %s",
                     i, len(future_to_download), futurePackage)
            try:
                future.result()
            except SCMError:
                # The SCM failed. Let's assume this is because the user needs
                # to supply a password.
                debug("%r requires auth; will prompt later", futurePackage)
                requires_auth.add(futurePackage)
            except Exception as exc:
                progress.end("error", error=True)
                dieOnError(True, "Error on fetching %r: %s. Aborting." %
                           (futurePackage, exc))
            else:
                debug("%r package updated: %d refs found", futurePackage,
                      len(specs[futurePackage]["scm_refs"]))
    progress.end("done")

    # Now execute git commands for private packages one-by-one, so the user can
    # type their username and password without multiple prompts interfering.
    for package in requires_auth:
        banner("If prompted now, enter your username and password for %s below\n"
               "If you are prompted too often, see: "
               "https://github.com/bitsorg/bits/blob/main/docs/troubleshooting.markdown"
               "#bits-keeps-asking-for-my-password",
               specs[package]["source"])
        update_repo(package, git_prompt=True)
        debug("%r package updated: %d refs found", package,
              len(specs[package]["scm_refs"]))


# Creates a directory in the store which contains symlinks to the package
# and its direct / indirect dependencies
def createDistLinks(spec, specs, args, syncHelper, repoType, requiresType):
  # At the point we call this function, spec has a single, definitive hash.
  # Use the caller's real architecture for the dist-link directory: dist links
  # are per-build-platform even when the package itself is shared.
  #
  # ver_rev() is used here (and for each dependency below) so that packages
  # with force_revision set in the defaults profile produce dist-tree directory
  # names and tarball symlink targets that match the actual install paths.
  target_dir = "{work_dir}/TARS/{arch}/{repo}/{package}/{package}-{ver_rev}" \
    .format(work_dir=args.workDir, arch=args.architecture, repo=repoType,
            ver_rev=ver_rev(spec), **spec)
  shutil.rmtree(target_dir.encode("utf-8"), ignore_errors=True)
  makedirs(target_dir, exist_ok=True)
  for pkg in [spec["package"]] + list(spec[requiresType]):
    dep_spec = specs[pkg]
    dep_arch = effective_arch(dep_spec, args.architecture)
    # ver_rev(dep_spec) accounts for each dependency's own force_revision
    # setting, which may differ from the top-level package's setting.
    dep_tarball = "../../../../../TARS/{arch}/store/{short_hash}/{hash}/{package}-{ver_rev}.{arch}.tar.gz" \
      .format(arch=dep_arch, short_hash=dep_spec["hash"][:2],
              ver_rev=ver_rev(dep_spec), **dep_spec)
    symlink(dep_tarball, target_dir)

def storeHook(package, specs, defaults) -> bool:
    spec = specs.get(package)
    if not spec or package == f"defaults-{defaults}":
        return False

    defaults_key = f"defaults-{defaults}"
    default_spec = specs.get(defaults_key, {})
    default_hook = default_spec.get("hook", {})
    default_params = default_spec.get("hook_params", {})

    pkg_hook = spec.get("hook")

    # Handle disabled hooks
    if type(pkg_hook) is str and pkg_hook == "disable":
        spec["hook"] = {}
        spec["hook_params"] = {}
        return False

    # Set hook (inherit from defaults if package has none)
    spec["hook"] = pkg_hook if pkg_hook else default_hook.copy() if default_hook else {}

    # Merge params (package params override defaults)
    spec["hook_params"] = {**default_params, **spec.get("hook_params", {})}

    return bool(spec["hook"])

def storeHashes(package, specs, considerRelocation):
  """Calculate various hashes for package, and store them in specs[package].

  Assumes that all dependencies of the package already have a definitive hash.
  """
  spec = specs[package]
  "If hooks are used, store them as part of package spec so we can include them in the hash."

  if "remote_revision_hash" in spec and "local_revision_hash" in spec:
    # We've already calculated these hashes before, so no need to do it again.
    # This also works around a bug, where after the first hash calculation,
    # some attributes of spec are changed (e.g. append_path and prepend_path
    # entries are turned from strings into lists), which changes the hash on
    # subsequent calculations.
    return

  # For now, all the hashers share data -- they'll be split below.
  h_all = Hasher()

  if spec.get("force_rebuild", False):
    h_all(str(time.time()))

  for key in ("recipe", "version", "package"):
    h_all(spec.get(key, "none"))

  # pkg_family changes the installation path (ARCH/FAMILY/PKG/VER vs
  # ARCH/PKG/VER), so tarballs built with different family settings are
  # not interchangeable.  Include it in the hash so they get distinct
  # identities and a family-tagged build never silently reuses a tarball
  # that was uploaded without a family (which would break relocation).
  # Empty string is used when no family is set, preserving backward
  # compatibility with existing tarballs.
  h_all(spec.get("pkg_family", ""))

  # commit_hash could be a commit hash (if we're not building a tag, but
  # instead e.g. a branch or particular commit specified by its hash), or it
  # could be a tag name (if we're building a tag). We want to calculate the
  # hash for both cases, so that if we build some commit, we want to be able to
  # reuse tarballs from other builds of the same commit, even if it was
  # referred to differently in the other build.
  debug("Base git ref is %s", spec["commit_hash"])
  h_default = h_all.copy()
  h_default(spec["commit_hash"])
  try:
    # If spec["commit_hash"] is a tag, get the actual git commit hash.
    real_commit_hash = spec["scm_refs"]["refs/tags/" + spec["commit_hash"]]
  except KeyError:
    # If it's not a tag, assume it's an actual commit hash.
    real_commit_hash = spec["commit_hash"]
  # Get any other git tags that refer to the same commit. We do not consider
  # branches, as their heads move, and that will cause problems.
  debug("Real commit hash is %s, storing alternative", real_commit_hash)
  h_real_commit = h_all.copy()
  h_real_commit(real_commit_hash)
  h_alternatives = [(spec.get("tag", "0"), spec["commit_hash"], h_default),
                    (spec.get("tag", "0"), real_commit_hash, h_real_commit)]
  for ref, git_hash in spec.get("scm_refs", {}).items():
    if ref.startswith("refs/tags/") and git_hash == real_commit_hash:
      tag_name = ref[len("refs/tags/"):]
      debug("Tag %s also points to %s, storing alternative",
            tag_name, real_commit_hash)
      hasher = h_all.copy()
      hasher(tag_name)
      h_alternatives.append((tag_name, git_hash, hasher))

  # Now that we've split the hasher with the real commit hash off from the ones
  # with a tag name, h_all has to add the data to all of them separately.
  def h_all(data):  # pylint: disable=function-redefined
    for _, _, hasher in h_alternatives:
      hasher(data)

  modifies_full_hash_dicts = ["env", "append_path", "prepend_path"]
  if not spec["is_devel_pkg"] and "track_env" in spec:
    modifies_full_hash_dicts.append("track_env")

  # If this recipe was sourced from a repository provider, fold the provider's
  # commit hash into every hash variant.  This ensures that upgrading a
  # provider (which changes its commit hash) triggers a rebuild of every
  # package whose recipe came from that provider, even if the recipe text
  # itself did not change.
  if "recipe_provider_hash" in spec:
    h_all("recipe_provider:" + spec["recipe_provider_hash"])
    debug("Folding provider hash %s into hash for %s",
          spec["recipe_provider_hash"][:10], package)

  for key in modifies_full_hash_dicts:
    if key not in spec:
      h_all("none")
    else:
      # spec["env"] is of type OrderedDict[str, str].
      # spec["*_path"] are of type OrderedDict[str, list[str]].
      assert isinstance(spec[key], OrderedDict), \
        "spec[{!r}] was of type {!r}".format(key, type(spec[key]))

      # Python 3.12 changed the string representation of OrderedDicts from
      # OrderedDict([(key, value)]) to OrderedDict({key: value}), so to remain
      # compatible, we need to emulate the previous string representation.
      h_all("OrderedDict([")
      h_all(", ".join(
        # XXX: We still rely on repr("str") being "'str'",
        # and on repr(["a", "b"]) being "['a', 'b']".
        "({!r}, {!r})".format(key, value)
        for key, value in spec[key].items()
      ))
      h_all("])")

  for tag, commit_hash, hasher in h_alternatives:
    # If the commit hash is a real hash, and not a tag, we can safely assume
    # that's unique, and therefore we can avoid putting the repository or the
    # name of the branch in the hash.
    if commit_hash == tag:
      hasher(spec.get("source", "none"))
      if "source" in spec:
        hasher(tag)
  if "sources" in spec:
    for src in spec["sources"]:
      if src.startswith("file://"):
        with open(src.removeprefix("file:/")) as ref:
          file_content = "".join(ref.readlines())
          h_all(file_content)
      else:
        h_all(src)
  if "patches" in spec:
    for patch in spec["patches"]:
      h_all(patch)
      with open(os.path.join(spec["pkgdir"], "patches", patch)) as ref:
        patch_content = "".join(ref.readlines())
        h_all(patch_content)
  
  if not package.startswith("defaults-"):
    for hook_name in sorted(spec.get("hook", {})):
      h_all("hook:" + hook_name + "=" + str(spec["hook"][hook_name]))
    for hook_name in sorted(spec.get("hook_params", {})):
      h_all("hook_params:" + hook_name + "=" + str(spec["hook_params"][hook_name]))

  dh = Hasher()
  for dep in spec.get("requires", []):
    # At this point, our dependencies have a single hash, local or remote, in
    # specs[dep]["hash"].
    hash_and_devel_hash = specs[dep]["hash"] + specs[dep].get("devel_hash", "")
    # If this package is a dev package, and it depends on another dev pkg, then
    # this package's hash shouldn't change if the other dev package was
    # changed, so that we can just rebuild this one incrementally.
    h_all(specs[dep]["hash"] if spec["is_devel_pkg"] else hash_and_devel_hash)
    # The deps_hash should always change, however, so we actually rebuild the
    # dependent package (even if incrementally).
    dh(hash_and_devel_hash)

  if spec["is_devel_pkg"] and "incremental_recipe" in spec:
    h_all(spec["incremental_recipe"])
    ih = Hasher()
    ih(spec["incremental_recipe"])
    spec["incremental_hash"] = ih.hexdigest()
  elif spec["is_devel_pkg"]:
    h_all(spec["devel_hash"])

  if considerRelocation and "relocate_paths" in spec:
    h_all("relocate:"+" ".join(sorted(spec["relocate_paths"])))

  spec["deps_hash"] = dh.hexdigest()
  spec["remote_revision_hash"] = h_default.hexdigest()
  # Store hypothetical hashes of this spec if we were building it using other
  # tags that refer to the same commit that we're actually building. These are
  # later used when fetching from the remote store. The "primary" hash should
  # be the first in the list, so it's checked first by the remote stores.
  spec["remote_hashes"] = [spec["remote_revision_hash"]] + \
    list({h.hexdigest() for _, _, h in h_alternatives} - {spec["remote_revision_hash"]})
  # The local hash must differ from the remote hash to avoid conflicts where
  # the remote has a package with the same hash as an existing local revision.
  h_all("local")
  spec["local_revision_hash"] = h_default.hexdigest()
  spec["local_hashes"] = [spec["local_revision_hash"]] + \
    list({h.hexdigest() for _, _, h, in h_alternatives} - {spec["local_revision_hash"]})


def hash_local_changes(spec):
  """Produce a hash of all local changes in the given git repo.

  If there are untracked files, this function returns a unique hash to force a
  rebuild, and logs a warning, as we cannot detect changes to those files.
  """
  directory = spec["source"]
  scm = spec["scm"]
  untrackedFilesDirectories = []
  class UntrackedChangesError(Exception):
    """Signal that we cannot detect code changes due to untracked files."""
  h = Hasher()
  if "track_env" in spec:
    assert isinstance(spec["track_env"], OrderedDict), \
        "spec[{!r}] was of type {!r}".format("track_env", type(spec["track_env"]))

    # Python 3.12 changed the string representation of OrderedDicts from
    # OrderedDict([(key, value)]) to OrderedDict({key: value}), so to remain
    # compatible, we need to emulate the previous string representation.
    h("OrderedDict([")
    h(", ".join(
        # XXX: We still rely on repr("str") being "'str'",
        # and on repr(["a", "b"]) being "['a', 'b']".
        "({!r}, {!r})".format(key, value) for key, value in spec["track_env"].items()))
    h("])")
  def hash_output(msg, args):
    lines = msg % args
    # `git status --porcelain` indicates untracked files using "??".
    # Lines from `git diff` never start with "??".
    if any(scm.checkUntracked(line) for line in lines.split("\n")):
      raise UntrackedChangesError()
    h(lines)
  cmd = scm.diffCmd(directory)
  try:
    err = execute(cmd, hash_output)
    debug("Command %s returned %d", cmd, err)
    dieOnError(err, "Unable to detect source code changes.")
  except UntrackedChangesError:
    untrackedFilesDirectories = [directory]
    warning("You have untracked changes in %s, so bits cannot detect "
            "whether it needs to rebuild the package. Therefore, the package "
            "is being rebuilt unconditionally. Please use 'git add' and/or "
            "'git commit' to track your changes in git.", directory)
    # If there are untracked changes, always rebuild (hopefully incrementally)
    # and let CMake figure out what needs to be rebuilt. Force a rebuild by
    # changing the hash to something basically random.
    h(str(time.time()))
  return (h.hexdigest(), untrackedFilesDirectories)


def better_tarball(spec, old, new):
  """Return which tarball we should prefer to reuse."""
  if not old: return new
  if not new: return old
  old_rev, old_hash, _ = old
  new_rev, new_hash, _ = new
  old_is_local, new_is_local = old_rev.startswith("local"), new_rev.startswith("local")
  # If one is local and one is remote, return the remote one.
  if old_is_local and not new_is_local: return new
  if new_is_local and not old_is_local: return old
  # Finally, return the one that appears in the list of hashes earlier.
  hashes = spec["local_hashes" if old_is_local else "remote_hashes"]
  return old if hashes.index(old_hash) < hashes.index(new_hash) else new


def _pkg_install_path(workDir, architecture, spec):
  """Return the path ``<workDir>/<arch>[/<family>]/<pkg>/<ver>[-<rev>]``.

  *architecture* should already be the *effective* architecture for *spec*
  (i.e. the result of ``effective_arch(spec, build_arch)``).  Callers are
  responsible for that substitution so that shared packages (``architecture:
  shared``) install under ``sw/shared/…`` rather than the build platform.

  When ``spec["pkg_family"]`` is also set the family directory is inserted
  between the architecture and the package name.  When it is empty the legacy
  two-level layout ``<arch>/<pkg>/<version>-<revision>`` is preserved.

  Uses :func:`ver_rev` so that packages with ``force_revision: ""`` in their
  defaults profile install under ``<version>/`` rather than
  ``<version>-<revision>/``.
  """
  family = spec.get("pkg_family", "")
  if family:
    return join(workDir, architecture, family, spec["package"], ver_rev(spec))
  return join(workDir, architecture, spec["package"], ver_rev(spec))


def generate_initdotsh(package, specs, architecture, workDir="sw", post_build=False):
  """Return the contents of the given package's etc/profile/init.sh as a string.

  If post_build is true, also generate variables pointing to the package
  itself; else, only generate variables pointing at it dependencies.
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
  lines.extend(_dep_init_path(dep) for dep in spec.get("requires", ()))

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
                 for key, value in spec.get("env", {}).items()
                 if key != "DYLD_LIBRARY_PATH")

    # Append paths to variables, if requested using append_path.
    # Again, only put values in double quotes so that they can refer to other variables.
    lines.extend('export {key}="${key}:{value}"'
                 .format(key=key, value=":".join(asList(value)))
                 for key, value in spec.get("append_path", {}).items()
                 if key != "DYLD_LIBRARY_PATH")

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
    for key, value in (("PATH", "bin"), ("LD_LIBRARY_PATH", "lib"), ("LD_LIBRARY_PATH", "lib64"),
                       ("PKG_CONFIG_PATH", "lib/pkgconfig"), ("PKG_CONFIG_PATH", "lib64/pkgconfig")):
      prepend_path.setdefault(key, []).insert(0, f"${bigpackage}_ROOT/{value}")
    lines.extend('[ ! -d "{value}" ] || export {key}="{value}${{{key}+:${key}}}"'
                 .format(key=key, value=dir)
                 for key, value in prepend_path.items()
                 if key != "DYLD_LIBRARY_PATH"
                 for dir in value)

  # Return string without a trailing newline, since we expect call sites to
  # append that (and the obvious way to inesrt it into the build template is by
  # putting the "%(initdotsh_*)s" on its own line, which has the same effect).
  return "\n".join(lines)


def create_provenance_info(package, specs, args):
  """Return a metadata record for storage in the package's install directory."""

  def spec_info(spec):
    return {
      "name": spec["package"],
      "tag": spec.get("tag"),
      "source": spec.get("source"),
      "version": spec["version"],
      "revision": spec["revision"],
      "hash": spec["hash"],
    }

  def dependency_list(key):
    return [spec_info(specs[dep]) for dep in specs[package].get(key, ())]

  return json.dumps({
    "comment": args.annotate.get(package),
    "bits_version": __version__,
    "dist": {
      "commit": os.environ["BITS_DIST_HASH"],
    },
    "architecture": args.architecture,
    "defaults": args.defaults,
    "package": spec_info(specs[package]),
    "dependencies": {
      "direct": {
        "build": dependency_list("build_requires"),
        "runtime": dependency_list("runtime_requires"),
      },
      "recursive": {  # includes direct deps and deps' deps
        "build": dependency_list("full_build_requires"),
        "runtime": dependency_list("full_runtime_requires"),
      },
    },
  })


# High-signal patterns that usually pinpoint the proximate cause of a build
# failure. Used to surface a short excerpt in the failure message so users do
# not have to open and grep the full (often huge) log by hand.
_ERROR_PATTERNS = re.compile(
    r"(?i)("
    r"error:|"                        # gcc/clang "error:" and "CMake Error:"
    r"fatal error:|"                  # missing headers etc.
    r"configure: error:|"             # autotools
    r"CMake Error|"                   # cmake (non-colon form)
    r"undefined reference|"           # link errors
    r"collect2: error|"               # linker driver
    r"ld: (error|cannot|symbol)|"     # linker (avoid matching e.g. "build:")
    r"No such file or directory|"
    r"command not found|"
    r"Permission denied|"
    r"Traceback \(most recent call last\)|"
    r"ModuleNotFoundError|ImportError:|"
    r"\*\*\* \[.*\] Error [0-9]|"      # make: *** [target] Error N
    r"make(\[[0-9]+\])?: \*\*\*|"      # make: *** / make[1]: ***
    r"recipe for target|"
    r"Could NOT find|"                # cmake find_package failure reason
    r"missing: |"                     # cmake "(missing: VAR ...)" detail
    r"Configuring incomplete"         # cmake configure summary
    r")"
)


def _extract_error_excerpt(log_path, max_match=15, tail=12, scan_limit=20000):
  """Return a short, high-signal excerpt from a build log to speed up triage.

  Collects the last `max_match` lines matching known error patterns (scanning
  only the final `scan_limit` lines, since the proximate cause is near the end)
  plus the final `tail` lines (the actual failure point). Best-effort only:
  returns "" if the log is missing/empty/unreadable and never raises.
  """
  try:
    with open(log_path, "r", errors="replace") as f:
      lines = f.readlines()
  except (OSError, IOError):
    return ""
  if not lines:
    return ""
  window = lines[-scan_limit:]
  matched = []
  for ln in window:
    if _ERROR_PATTERNS.search(ln):
      s = ln.rstrip("\n")
      if not matched or matched[-1] != s:  # drop consecutive duplicates
        matched.append(s)
  matched = matched[-max_match:]
  tail_lines = [ln.rstrip("\n") for ln in lines[-tail:]]
  out = []
  if matched:
    out.append("  Matched error lines (last %d):" % len(matched))
    out.extend("    " + m for m in matched)
  if tail_lines:
    out.append("  Last %d lines of log:" % len(tail_lines))
    out.extend("    " + t for t in tail_lines)
  return "\n".join(out)


def write_failure_summary(work_dir, scheduler):
  """Write a concise per-run failure summary for a --builders build.

  The full per-package error messages collected by the scheduler are verbose
  (log paths, environment, next-steps, ...), so a whole-stack failure produces
  an unreadable wall of text.  This distils, into ``<work_dir>/build-summary.log``:
    * each package that *directly* failed to build, with its log path and the
      proximate error excerpt (the matched error lines);
    * the count of packages skipped only because a dependency failed.
  Also writes the full, verbose per-action errors to
  ``<work_dir>/build-errors-full.log`` so there is a single combined log to
  consult (the concise summary points at the individual per-package logs).

  Returns ``(summary_path, full_path)`` (either element may be None), or
  ``(None, None)`` if there were no failures.
  """
  fails = getattr(scheduler, "buildFailures", None) or []
  errors = getattr(scheduler, "errors", {}) or {}
  direct_names = {f["package"].split("@", 1)[0] for f in fails}
  cascaded = []
  for action, msg in errors.items():
    if "could not complete" in str(msg):
      pkg = str(action).split(":", 1)[-1]
      if pkg not in direct_names:
        cascaded.append(pkg)
  if not fails and not cascaded:
    return (None, None)
  _ansi = re.compile(r"\033\[[0-9;]*m")
  full_path = os.path.join(work_dir, "build-errors-full.log")
  try:
    with open(full_path, "w") as fh:
      for action, msg in errors.items():
        fh.write("* %s\n%s\n\n" % (action, _ansi.sub("", str(msg))))
  except OSError as exc:
    warning("Could not write full error log %s: %s", full_path, exc)
    full_path = None
  path = os.path.join(work_dir, "build-summary.log")
  try:
    with open(path, "w") as fh:
      fh.write("BUILD FAILURE SUMMARY\n=====================\n\n")
      fh.write("%d package(s) failed to build; %d skipped because a dependency failed.\n\n"
               % (len(fails), len(cascaded)))
      if fails:
        fh.write("Failed to build:\n")
        for f in sorted(fails, key=lambda x: x["package"].lower()):
          fh.write("  - %s\n" % f["package"])
        fh.write("\n")
      for f in sorted(fails, key=lambda x: x["package"].lower()):
        fh.write("-" * 72 + "\n")
        fh.write("FAILED: %s\n" % f["package"])
        fh.write("  log: %s\n" % f.get("log", "?"))
        if f.get("install_root"):
          fh.write("  install root: %s\n" % f["install_root"])
        if f.get("excerpt"):
          fh.write("\n")
          for line in f["excerpt"].splitlines():
            fh.write("  " + line + "\n")
        fh.write("\n")
      if cascaded:
        fh.write("-" * 72 + "\n")
        fh.write("Skipped (a dependency failed to build), %d package(s):\n  %s\n"
                 % (len(cascaded), ", ".join(sorted(cascaded))))
  except OSError as exc:
    warning("Could not write failure summary %s: %s", path, exc)
    return (None, full_path)
  return (path, full_path)


def runBuildCommand(scheduler, p, specs, args, build_command, cachedTarball, scriptDir, workDir, syncHelper):
  spec = specs[p]
  debug("Build command: %s", build_command)
  progress = debug
  if args.builders==1:
    progress_msg = "Unpacking %s@%s" if cachedTarball else "Compiling %s@%s"
    if not cachedTarball and not args.debug:
      progress_msg += " (use --debug for full output)"
    progress = ProgressPrint(
      progress_msg %
      (spec["package"],
      args.develPrefix if "develPrefix" in args and spec["is_devel_pkg"] else spec["version"])
    )
  else:
    scheduler.log (
      ("Unpacking %s@%s" if cachedTarball else
      "Compiling %s@%s (use --debug for full output)") %
      (spec["package"],
      args.develPrefix if "develPrefix" in args and spec["is_devel_pkg"] else spec["version"])
    )
  # Optional nice-ladder: claim a priority slot for this concurrent build so CPU
  # contention degrades gracefully (lead build at full speed, others backed off).
  # No-op unless --build-nice is set (nice_ladder stays None).
  #   * Native builds: wrap in `nice -n N /bin/sh -c <cmd>` so the whole build
  #     process tree inherits the niceness.  Robust for compound commands and
  #     thread-safe (no preexec_fn fork hazard in the scheduler's workers).
  #   * Docker/podman builds: each builder is a separate container (cgroup), so
  #     niceness inside one cannot rank it against the others — the host ranks
  #     the *containers* by cgroup CPU weight.  Inject `--cpu-shares=W` (the
  #     container-level equivalent, derived from the same ladder) into `… run`.
  nice_ladder = getattr(scheduler, "nice_ladder", None) if scheduler is not None else None
  nice_token = None
  if nice_ladder is not None:
    nice_token, nice_level = nice_ladder.acquire()
    if nice_level:  # nice 0 / default shares need no change
      if getattr(args, "docker", False):
        from bits_helpers.nice_ladder import cpu_shares_for_nice
        shares = cpu_shares_for_nice(nice_level)
        # Name the build container (unique per build) so the straggler watchdog
        # can later restore its cpu-shares via `docker update <name>`.
        cname = "bits-build-%s-%s" % (re.sub(r'[^a-zA-Z0-9_.-]', '-', p), os.urandom(4).hex())
        build_command, _subbed = re.subn(
            r'\b(docker|podman)\s+run\s',
            r'\1 run --cpu-shares=%d --name %s ' % (shares, cname),
            build_command, count=1)
        if _subbed:
          _wd = getattr(scheduler, "renice_watchdog", None) if scheduler is not None else None
          if _wd is not None:
            _dbin = "podman" if build_command.lstrip().startswith("podman") else "docker"
            _wd.register_container(cname, _dbin)
        else:
          debug("build-nice: could not inject --cpu-shares/--name (no 'docker/podman run' "
                "in command); container build runs unthrottled this slot.")
      else:
        build_command = "nice -n %d /bin/sh -c %s" % (nice_level, shlex.quote(build_command))
  try:
    if args.resourceMonitoring:
      err = run_monitor_on_command(build_command, "{}/{}.json".format(scriptDir, p), printer=progress)
    else:
      err = execute(build_command, printer=progress)
  finally:
    if nice_ladder is not None:
      nice_ladder.release(nice_token)
  if args.builders==1:
    progress.end("failed" if err else "done", err)
  report_event("BuildError" if err else "BuildSuccess", spec["package"], " ".join((
  args.architecture,
  spec["version"],
  spec["commit_hash"],
  os.environ["BITS_DIST_HASH"][:10],
  )))

  # We do not use the override for devel packages, because we
  # want to avoid having to rebuild things when the /tmp gets cleaned.
  if spec["is_devel_pkg"]:
      buildWorkDir = args.workDir
  else:
      buildWorkDir = os.environ.get("BITS_BUILD_WORK_DIR", args.workDir)

  # Determine paths
  devSuffix = "-" + args.develPrefix if "develPrefix" in args and spec["is_devel_pkg"] else ""
  log_path = f"{buildWorkDir}/BUILD/{spec['package']}-latest{devSuffix}/log"
  log_abs_path = log_path  # keep the absolute path; log_path may become relative below
  build_dir = f"{buildWorkDir}/BUILD/{spec['package']}-latest{devSuffix}/{spec['package']}"
  # Staging install prefix ($INSTALLROOT in the recipe): where the package's
  # files are installed before being tarred. Useful for inspecting a partial
  # install after a failure.
  try:
    install_root = _pkg_install_path(
      join(buildWorkDir, "INSTALLROOT", spec["hash"]),
      effective_arch(spec, args.architecture), spec)
  except Exception:  # pylint: disable=broad-except
    install_root = None

  # Use relative paths if we're inside the work directory
  try:
    from os.path import relpath
    log_path = relpath(log_path, os.getcwd())
    build_dir = relpath(build_dir, os.getcwd())
    if install_root:
      install_root = relpath(install_root, os.getcwd())
  except (ValueError, OSError):
    pass  # Keep absolute paths if relpath fails

  # Color codes for error message (if TTY)
  bold = "\033[1m" if sys.stderr.isatty() else ""
  red = "\033[31m" if sys.stderr.isatty() else ""
  reset = "\033[0m" if sys.stderr.isatty() else ""

  # Build the error message
  devel_note = " (development package)" if spec["is_devel_pkg"] else ""
  buildErrMsg = f"{red}{bold}BUILD FAILED:{reset} {spec['package']}@{spec['version']}{devel_note}\n"
  buildErrMsg += "=" * 70 + "\n\n"

  buildErrMsg += f"{bold}Log File:{reset}\n"
  buildErrMsg += f"  {log_path}\n\n"

  buildErrMsg += f"{bold}Build Directory:{reset}\n"
  buildErrMsg += f"  {build_dir}\n"

  if install_root:
    buildErrMsg += f"{bold}Install Root:{reset}\n"
    buildErrMsg += f"  {install_root}\n"

  # Surface the proximate error so the user does not have to open the full log.
  excerpt = ""
  if err:
    excerpt = _extract_error_excerpt(log_abs_path) or ""
    if excerpt:
      buildErrMsg += f"\n{bold}Error excerpt:{reset}\n"
      buildErrMsg += excerpt + "\n"

  updatablePkgs = [dep for dep in spec["requires"] if specs[dep]["is_devel_pkg"]]
  if spec["is_devel_pkg"]:
    updatablePkgs.append(spec["package"])

  # Gather build info for the error message
  try:
    detected_arch = detectArch()

    # Only show safe arguments (no tokens/secrets) in CLI-usable format
    safe_args = {
      "pkgname", "defaults", "architecture", "forceUnknownArch",
      "develPrefix", "jobs", "noSystem", "noDevel", "forceTracked", "plugin",
      "disable", "annotate", "onlyDeps", "docker"
    }
    
    cli_args = []
    for k, v in vars(args).items():
      if not v or k not in safe_args:
        continue
      
      # Format based on type for CLI usage
      if isinstance(v, bool):
        if v:  # Only show if True
          cli_args.append(f"--{k}")
      elif isinstance(v, list):
        if v:  # Only show non-empty lists
          # For lists, use multiple --flag=val entries; deduplicate while
          # preserving first-seen order (duplicates can arise from the
          # prefer_system / system_requirement resolution loop).
          seen = set()
          for item in v:
            if item not in seen:
              seen.add(item)
              cli_args.append(f"--{k}={quote(str(item))}")
      else:
        # Quote if needed
        cli_args.append(f"--{k}={quote(str(v))}")

    args_str = " ".join(cli_args)

    buildErrMsg += f"\n{bold}Environment:{reset}\n"
    buildErrMsg += f"  OS: {detected_arch}\n"
    buildErrMsg += f"  bits: {__version__ or 'unknown'} (bits@{os.environ['BITS_DIST_HASH'][:10]})\n"

    if detected_arch.startswith("osx"):
      xcode_info = getstatusoutput("xcodebuild -version")[1]
      # Combine XCode version lines into one
      xcode_lines = xcode_info.strip().split('\n')
      if len(xcode_lines) >= 2:
        xcode_str = f"{xcode_lines[0]} ({xcode_lines[1]})"
      else:
        xcode_str = xcode_lines[0] if xcode_lines else "Unknown"
      buildErrMsg += f"  XCode: {xcode_str}\n"

    buildErrMsg += f"  Arguments: {args_str}\n"

  except Exception as exc:
    warning("Failed to gather build info", exc_info=exc)

  # Add note about development packages if applicable
  if updatablePkgs:
    buildErrMsg += f"\n{bold}Development Packages:{reset}\n"
    buildErrMsg += "  Development sources are not updated automatically.\n"
    buildErrMsg += "  This may be due to outdated sources. To update:\n"
    buildErrMsg += "".join(f"\n    ( cd {dp} && git pull --rebase )" for dp in updatablePkgs)
    buildErrMsg += "\n"

  # Add Next Steps section
  buildErrMsg += f"\n{bold}Next Steps:{reset}\n"
  buildErrMsg += f"  • View error log:          cat {log_path}\n"
  if not args.debug:
    buildErrMsg += f"  • Rebuild with debug:      bitsBuild build {spec['package']} --debug\n"
  buildErrMsg += f"  • Please upload the full log to CERNBox/Dropbox if you intend to request support.\n"

  if err and args.builders>1:
    # Record a concise entry for the end-of-run failure summary (write_failure_summary).
    fails = getattr(scheduler, "buildFailures", None)
    if fails is not None:
      try:
        with scheduler.buildFailuresLock:
          fails.append({"package": "%s@%s" % (spec["package"], spec["version"]),
                        "log": log_path, "install_root": install_root,
                        "excerpt": excerpt})
      except Exception:  # pylint: disable=broad-except
        pass
    return buildErrMsg.strip()
  else:
    dieOnError(err, buildErrMsg.strip())

  updatablePkgs = [dep for dep in spec["requires"] if specs[dep]["is_devel_pkg"]]
  if spec["is_devel_pkg"]:
    updatablePkgs.append(spec["package"])

  if updatablePkgs:
    buildErrMsg += dedent("""
    Note that you have packages in development mode.
    Devel sources are not updated automatically, you must do it by hand.\n
    This problem might be due to one or more outdated devel sources.
    To update all development packages required for this build it is usually sufficient to do:
    """)
    buildErrMsg += "".join("\n  ( cd %s && git pull --rebase )" % dp for dp in updatablePkgs)

    # Gather build info for the error message
    try:
      safe_args = {
        "pkgname", "defaults", "architecture", "forceUnknownArch",
        "develPrefix", "jobs", "noSystem", "noDevel", "forceTracked", "plugin",
        "disable", "annotate", "onlyDeps", "docker"
      }
      args_str = " ".join(f"--{k}={v}" for k, v in vars(args).items() if v and k in safe_args)
      detected_arch = detectArch()
      buildErrMsg += dedent(f"""
      Build info:
      OS: {detected_arch}
      Using BITS from bits@{__version__ or "unknown"} recipes in bits@{os.environ["BITS_DIST_HASH"][:10]}
      Build arguments: {args_str}
      """)

      if detected_arch.startswith("osx"):
         buildErrMsg += f'XCode version: {getstatusoutput("xcodebuild -version")[1]}'

    except Exception as exc:
      warning("Failed to gather build info: %s", exc)

    if err and args.builders>1:
      return buildErrMsg.strip()
    else:
      dieOnError(err, buildErrMsg.strip())

  doFinalSync(spec, specs, args, syncHelper)


def _doCheckout(spec, workDir, referenceSources, docker, enforce_mode,
                syncHelper, parallel_sources, architecture):
  """Scheduler "download" task: fetch a package's sources.

  Used by the --builders path so that source clones/archive downloads run as
  scheduler tasks (capped by --parallel-downloads) overlapping compilation,
  instead of being executed serially in the preparation loop before any build
  starts.  Mirrors the work the Makeflow path does in its parallel .checkout
  rules (bits_helpers.checkout_runner).

  Returns an empty string on success or an error message on failure, matching
  the scheduler convention (a falsy result means the task succeeded).
  """
  try:
    checkout_sources(spec, workDir, referenceSources, docker,
                     enforce_mode=enforce_mode,
                     sync_helper=syncHelper,
                     parallel_sources=parallel_sources,
                     architecture=architecture)
  except OSError as e:
    return "Failed to fetch sources for %s@%s: %s" % (
      spec.get("package", "?"), spec.get("version", "?"), e)
  return ""


def doFinalSync(spec, specs, args, syncHelper):
  # When --pipeline --makeflow is active, the Makeflow .build rule runs
  # create_links.sh (dist symlinks) and the .upload rule handles the upload.
  # Nothing to do here in that mode.
  if getattr(args, "pipeline", False) and args.makeflow:
    return

  # We need to create 2 sets of links, once with the full requires,
  # once with only direct dependencies, since that's required to
  # register packages.
  createDistLinks(spec, specs, args, syncHelper, "dist", "full_requires")
  createDistLinks(spec, specs, args, syncHelper, "dist-direct", "requires")
  createDistLinks(spec, specs, args, syncHelper, "dist-runtime", "full_runtime_requires")

  # Make sure not to upload local-only packages! These might have been
  # produced in a previous run with a read-only remote store.
  if not spec["revision"].startswith("local"):
    syncHelper.upload_symlinks_and_tarball(spec)
    # Record the tarball's SHA-256 in the local integrity ledger so that
    # future recalls from the store can be verified against it.
    # Only active when --store-integrity is set (or store_integrity = true
    # in bits.rc); off by default for backward compatibility.
    if getattr(args, "storeIntegrity", False):
      from bits_helpers.store_integrity import record_tarball_checksum
      record_tarball_checksum(spec, args.workDir, args.architecture)

  # ── Manifest recording ─────────────────────────────────────────────────────
  # Record the completed package in the incremental build manifest so that a
  # partial build still yields a useful record.  The outcome is:
  #   • "from_store"         — spec["cachedTarball"] was non-empty (we unpacked
  #                            a tarball recalled from the remote store).
  #   • "built_from_source"  — the build script ran; the tarball was produced
  #                            locally and (for non-local revisions) uploaded.
  if getattr(args, "manifest", None) is not None:
    from bits_helpers.utilities import resolve_store_path, effective_arch, ver_rev
    _cached = spec.get("cachedTarball", "")
    _outcome = "from_store" if _cached else "built_from_source"
    # Locate the local tarball for checksum recording.
    _arch = effective_arch(spec, args.architecture)
    _tarball_name = "{}-{}.{}.tar.gz".format(
      spec["package"], ver_rev(spec), _arch)
    _tarball_path = os.path.join(
      args.workDir,
      resolve_store_path(_arch, spec["hash"]),
      _tarball_name,
    )
    args.manifest.add_package(spec, _outcome,
                               _tarball_path if os.path.isfile(_tarball_path) else None,
                               effective_architecture=_arch)

  # Touch the sentinel so the cleanup command counts this package as recently used.
  try:
    from bits_helpers.cleanup import touch_sentinel as _touch_sentinel
    from bits_helpers.utilities import ver_rev as _ver_rev
    _touch_sentinel(args.workDir, args.architecture, spec["package"], _ver_rev(spec))
  except Exception:
    pass


def _download_time_mode(mode: str) -> str:
  """Return the enforcement mode to apply *during* source download.

  ``warn`` and ``enforce`` are security gates — they must fire before the
  compiler ever sees a source file, so they remain active during download.

  ``print`` and ``off`` have no pre-build security purpose: ``print`` is
  deferred to :func:`_run_post_build_checksum_phase` so that it covers
  packages whose tarball was already cached (and whose sources were therefore
  not re-downloaded this run).
  """
  return mode if mode in ("warn", "enforce") else "off"


def _print_checksums_for_spec(spec, work_dir):
  """Print computed checksums for all sources and patches of *spec*.

  Reads from the download cache (``SOURCES/cache/``) so that this works even
  when the package tarball was cached and ``checkout_sources()`` was not called
  this run.  Missing cache entries are warned about but do not abort.
  """
  from bits_helpers.checksum import parse_entry as _pe, checksum_file as _cf
  from bits_helpers.download import getUrlChecksum as _guc
  from bits_helpers.utilities import short_commit_hash

  pkgname = spec.get("package", "")
  version = spec.get("version", "")
  src_dir = join(work_dir, "SOURCES", pkgname, version, short_commit_hash(spec))

  printed_header = [False]   # mutable cell so the nested helper can set it

  def _header():
    if not printed_header[0]:
      print("# %s" % pkgname)
      printed_header[0] = True

  if "sources" in spec:
    sources_printed = False
    for s in spec["sources"]:
      url, _ = _pe(s)
      fname = url.rsplit("/", 1)[-1]
      url_hash = _guc(url)
      # Primary cache location written by download(); fall back to src_dir.
      candidate = join(work_dir, "SOURCES", "cache", url_hash[:2], url_hash, fname)
      if not exists(candidate):
        candidate = join(work_dir, "TMP", url_hash, fname)   # legacy path
      if not exists(candidate):
        candidate = join(src_dir, fname)
      if exists(candidate):
        _header()
        if not sources_printed:
          print("sources:")
          sources_printed = True
        print("  %s: %s" % (url, _cf(candidate)))
      else:
        warning("--print-checksums: cannot find cached source for %s in %s",
                pkgname, url)

  if "patches" in spec:
    patches_printed = False
    for patch_entry in spec["patches"]:
      patch_name, _ = _pe(patch_entry)
      patch_path = join(spec.get("pkgdir", ""), "patches", patch_name)
      if exists(patch_path):
        _header()
        if not patches_printed:
          print("patches:")
          patches_printed = True
        print("  %s: %s" % (patch_name, _cf(patch_path)))

  if printed_header[0]:
    print()   # blank line between packages


def _run_post_build_checksum_phase(specs, work_dir, do_print, do_write):
  """Run print / write checksum operations for *all* packages in one pass.

  Called after the main build loop so that:

  * Output from ``--print-checksums`` appears as a single consolidated block
    rather than being scattered through the build log.
  * Both operations cover packages whose tarball was already cached (and whose
    sources were therefore not re-downloaded this run), as long as the source
    files are still present in ``SOURCES/cache/``.

  ``warn`` / ``enforce`` verification is intentionally **not** handled here —
  those modes are security gates that run during download via
  :func:`_download_time_mode`.
  """
  if do_print:
    banner("Checksums")
  for spec in specs:
    if do_print:
      _print_checksums_for_spec(spec, work_dir)
    if do_write:
      _write_checksums_for_spec(spec, work_dir)


def _write_checksums_for_spec(spec, work_dir):
  """Compute and write the checksums/<pkg>.checksum file for *spec*.

  Called when ``--write-checksums`` is active.  Computes the actual SHA-256 of
  every downloaded source tarball and patch file, reads back the current HEAD
  commit for ``source:`` + ``tag:`` packages, and writes the result to
  ``<pkgdir>/checksums/<pkgname>.checksum``.

  Silently skips entries whose files cannot be found (e.g. cached tarballs that
  were not re-downloaded).
  """
  from bits_helpers.checksum_store import write_checksum_file as _write_ck
  from bits_helpers.utilities import short_commit_hash

  pkgdir = spec.get("pkgdir", "")
  pkgname = spec.get("package", "")
  if not pkgdir or not pkgname:
    return

  store = {"tag": None, "sources": {}, "patches": {}}

  # --- sources (downloaded tarballs) ----------------------------------------
  source_parent = join(work_dir, "SOURCES", pkgname, spec.get("version", ""))
  src_dir = join(source_parent, short_commit_hash(spec))
  if "sources" in spec:
    from bits_helpers.checksum import parse_entry as _pe
    from bits_helpers.download import getUrlChecksum as _guc
    import hashlib
    for s in spec["sources"]:
      url, _ = _pe(s)
      # download() stores files under a subdirectory keyed by md5(url)
      url_hash = _guc(url)
      from os.path import basename as _bn
      fname = _bn(url)
      candidate = join(work_dir, "TMP", url_hash, fname)
      if not exists(candidate):
        candidate = join(src_dir, fname)
      if exists(candidate):
        store["sources"][url] = compute_checksum_file(candidate)
      else:
        warning("--write-checksums: could not find downloaded file for %s", url)

  # --- patches --------------------------------------------------------------
  if "patches" in spec:
    from bits_helpers.checksum import parse_entry as _pe
    for patch_entry in spec["patches"]:
      patch_name, _ = _pe(patch_entry)
      patch_path = join(src_dir, patch_name)
      if exists(patch_path):
        store["patches"][patch_name] = compute_checksum_file(patch_path)

  # --- git commit pin -------------------------------------------------------
  if "source" in spec and "tag" in spec:
    scm = spec.get("scm")
    if scm is not None:
      try:
        store["tag"] = scm.checkedOutCommitName(src_dir).strip()
      except Exception as exc:  # noqa: BLE001
        warning("--write-checksums: could not read HEAD for %s: %s", pkgname, exc)

  if store["tag"] or store["sources"] or store["patches"]:
    path = _write_ck(pkgdir, pkgname, store)
    info("Wrote checksum file: %s", path)
  else:
    debug("--write-checksums: nothing to record for %s", pkgname)


def doBuild(args, parser):
  packages = args.pkgname
  specs = {}
  buildOrder = []
  workDir = abspath(args.workDir)
  prunePaths(workDir)

  buildTargets = " ".join(args.pkgname)

  if not exists(args.configDir):
    from bits_helpers.repo_provider import bootstrap_default_config, cwd_is_recipe_dir
    _default_config_dir = os.environ.get("BITS_REPO_DIR", "alidist")

    # Step 1 — CWD detection: if the user is sitting inside a checked-out
    # recipe repository (e.g. they did "git clone …/lhcb.bits && cd lhcb.bits")
    # and did NOT explicitly override --config-dir, use "." so bits picks up the
    # local recipes without any further configuration.
    if args.configDir == _default_config_dir and cwd_is_recipe_dir():
      debug("Recipe files detected in current directory; using '.' as config dir")
      args.configDir = "."

    # Step 2 — Network bootstrap: when still no config dir, fetch bits-providers
    # and follow the community pointer (default.bits.sh or <org>.bits.sh) to
    # clone a recipe repo automatically.
    elif not exists(args.configDir):
      bootstrapped = bootstrap_default_config(args, workDir)
      if bootstrapped:
        args.configDir = bootstrapped

  dieOnError(not exists(args.configDir),
            'Cannot find recipes under directory "%s".\n'
            'Maybe you need to "cd" to the right directory or '
            'you forgot to run "bits init"?' % args.configDir)

  _, value = git(("symbolic-ref", "-q", "HEAD"), directory=args.configDir, check=False)
  branch_basename = re.sub("refs/heads/", "", value)
  branch_stream = re.sub("-patches$", "", branch_basename)
  # In case the basename and the stream are the same,
  # the stream becomes empty.
  if branch_stream == branch_basename:
    branch_stream = ""

  def defaultsReader():
    meta, body = readDefaults(args.configDir, args.defaults, parser.error, args.architecture)
    # --flavour variables feed both the (?NAME) conditional matchers (via the
    # `variables:` map) and the build environment + package hash (via the `env:`
    # map, which becomes the defaults-release env every package depends on). CLI
    # flavours override a defaults entry of the same name.
    flavours = getattr(args, "flavours", None)
    if flavours:
      from collections import OrderedDict as _OD
      meta.setdefault("variables", _OD())
      meta.setdefault("env", _OD())
      for _k, _v in flavours.items():
        meta["variables"][_k] = _v
        meta["env"][_k] = _v
    return meta, body
  (err, overrides, taps, defaultsMeta) = parseDefaults(args.disable,
                                        defaultsReader, debug, args.architecture, args.configDir)
  dieOnError(err, err)
  makedirs(join(workDir, "SPECS"), exist_ok=True)

  # When any loaded defaults file sets ``qualify_arch: true`` the install tree
  # is placed under a combined architecture string, e.g. "slc7_x86-64-dev-gcc13"
  # instead of "slc7_x86-64".  This lets multiple defaults combinations coexist
  # in the same work directory.  The original raw architecture is preserved so
  # that it can be passed as $ARCHITECTURE to the build script (where it is
  # used, for example, to detect macOS via ${ARCHITECTURE:0:3}).
  raw_architecture = args.architecture
  args.architecture = compute_combined_arch(defaultsMeta, args.defaults, raw_architecture)
  if args.architecture != raw_architecture:
    debug("qualify_arch active: using combined architecture %s (raw: %s)",
          args.architecture, raw_architecture)

  # ── CVMFS layout (templated dirs from defaults-release) ────────────────────
  # When defaults declare cvmfs_dir / install_dir / module_dir (templates that
  # may use %(architecture)s), resolve them and use them to default the build/
  # reuse flags so the whole CVMFS chain can be driven from one declaration:
  #   * docker build  -> --cvmfs-prefix = <install_path> (build in place)
  #   * reuse deployed -> --remote-store = cvmfs://<cvmfs_dir>  (with --reuse-cvmfs)
  from bits_helpers.cvmfs_layout import resolve_cvmfs_layout
  _cvmfs = resolve_cvmfs_layout(defaultsMeta, args.architecture)
  if _cvmfs:
    info("CVMFS layout: install=%s  modules=%s", _cvmfs["install_path"], _cvmfs["module_path"])
    if args.docker and not getattr(args, "cvmfsPrefix", None) and _cvmfs["cvmfs_dir"]:
      args.cvmfsPrefix = _cvmfs["install_path"]
      info("Defaulting --cvmfs-prefix to %s (from defaults CVMFS layout)", args.cvmfsPrefix)
    if getattr(args, "reuseCvmfs", False) and not args.remoteStore and _cvmfs["cvmfs_dir"]:
      args.remoteStore = "cvmfs://" + _cvmfs["cvmfs_dir"]
      info("Reusing deployed components: --remote-store %s", args.remoteStore)

  # Global build-time network policy for the recipe sandbox. Precedence:
  #   explicit --sandbox-network  >  defaults `sandbox_network:`  >  "on".
  # A recipe's own sandbox_network field still overrides this per package
  # (handled in sandbox.wrap_build_command). YAML parses bare on/off as bools,
  # so normalise to the "on"/"off" strings the sandbox layer expects.
  if getattr(args, "sandboxNetwork", None) is None:
    _dn = defaultsMeta.get("sandbox_network", "on")
    if isinstance(_dn, bool):
      _dn = "on" if _dn else "off"
    args.sandboxNetwork = str(_dn).strip().lower()
    if args.sandboxNetwork == "off":
      info("Build-time sandbox network allowed by default (defaults sandbox_network: off)")

  # CPU oversubscription factor for the per-builder -j share. Precedence:
  #   explicit --oversubscribe  >  defaults `build_oversubscribe:`  >  1.0.
  # Memory budgeting is unaffected (see effective_jobs).
  if getattr(args, "oversubscribe", None) is None:
    try:
      args.oversubscribe = float(defaultsMeta.get("build_oversubscribe", 1.0))
    except (TypeError, ValueError):
      args.oversubscribe = 1.0
    if args.oversubscribe > 1.0:
      info("CPU oversubscription factor %.2f (defaults build_oversubscribe)", args.oversubscribe)

  # syncHelper is constructed after defaults loading so that it receives the
  # (potentially combined) architecture string.
  syncHelper = remote_from_url(args.remoteStore, args.writeStore, args.architecture,
                               args.workDir, getattr(args, "insecure", False))

  # If the bits workdir contains a .sl directory (or .git/sl for git repos
  # with Sapling enabled), we use Sapling as SCM. Otherwise, we default to git
  # (without checking for the actual presence of .git). We mustn't check for a
  # .git directory, because some tests use a subdirectory of the bits source
  # tree as the "*.bits" checkout, and that won't have a .git directory.
  config_path = Path(args.configDir)
  has_sapling = (config_path / ".sl").exists() or (config_path / ".git" / "sl").exists()
  if has_sapling and shutil.which("sl"):
    scm = Sapling()
  else:
    scm = Git()
  try:
    checkedOutCommitName = scm.checkedOutCommitName(directory=args.configDir)
  except SCMError:
    dieOnError(True, "Cannot find SCM directory in %s." % args.configDir)
  os.environ["BITS_DIST_HASH"] = checkedOutCommitName

  debug("Building for architecture %s", args.architecture)
  debug("Number of parallel builds: %d", args.jobs)
  debug("Using bitsBuild from bits@%s recipes in dist@%s",
        __version__ or "unknown", os.environ["BITS_DIST_HASH"])

  install_wrapper_script("git", workDir)

  extra_env = {"BITS_CONFIG_DIR": "/pkgdist.bits" if args.docker else os.path.abspath(args.configDir)}
  extra_env.update(dict([e.partition('=')[::2] for e in args.environment]))

  # ── Repository-provider discovery ─────────────────────────────────────────
  # Phase 1 – Always-on providers: recipes with ``always_load: true`` (and
  # optionally the auto-synthesised ``bits-providers`` package built from
  # $BITS_PROVIDERS / bits.rc).  These are cloned *before* the iterative scan
  # so that the recipes they contain are visible to getPackageList right away.
  always_on_dirs = load_always_on_providers(
    config_dir        = args.configDir,
    work_dir          = workDir,
    reference_sources = args.referenceSources,
    fetch_repos       = args.fetchRepos,
    bits_providers    = getattr(args, "bits_providers", None),
    taps              = taps,
    provider_policy   = getattr(args, "provider_policy", {}),
  )

  # Phase 2 – Iterative scan: walk the top-level package list for any packages
  # that carry ``provides_repository: true`` and clone them into the local REPOS
  # cache, extending BITS_PATH.  A freshly-cloned provider may itself contain
  # further providers, which are discovered and cloned on the next pass.
  #
  # The scan is also seeded with any top-level ``requires`` / ``build_requires``
  # declared directly in the active defaults file(s).  This allows a defaults
  # file to trigger provider loading with the ordinary ``requires`` field:
  #
  #   requires:
  #     - my-org-recipes   # a recipe whose .sh declares provides_repository: true
  #
  # ``filterByArchitectureDefaults`` is intentionally skipped here: being
  # conservative (pre-loading a provider on every architecture) is safe and
  # avoids a chicken-and-egg where the provider's own recipes would be needed
  # to evaluate the architecture condition.
  defaults_provider_seed = (
    list(defaultsMeta.get("requires", []))
    + list(defaultsMeta.get("build_requires", []))
  )

  provider_dirs = fetch_repo_providers_iteratively(
    packages          = packages + defaults_provider_seed,
    config_dir        = args.configDir,
    work_dir          = workDir,
    reference_sources = args.referenceSources,
    fetch_repos       = args.fetchRepos,
    taps              = taps,
    provider_policy   = getattr(args, "provider_policy", {}),
  )
  provider_dirs.update(always_on_dirs)

  # ── Build manifest initialisation ─────────────────────────────────────────
  # The manifest is always written; it records every package, provider, and
  # checksum so the build can be reproduced later with --from-manifest.
  from bits_helpers.manifest import BuildManifest
  args.manifest = BuildManifest(
    work_dir          = workDir,
    requested_packages= packages,
    architecture      = args.architecture,
    defaults          = args.defaults,
    config_dir        = args.configDir,
    config_commit     = os.environ.get("BITS_DIST_HASH", ""),
    # Use the last (top-level) requested package as the filename identifier.
    # This mirrors how mainPackage = buildOrder[-1] is resolved later; using
    # packages[-1] here avoids having to delay manifest creation until after
    # the full dependency graph has been resolved.
    target            = packages[-1] if packages else "",
  )
  args.manifest.add_providers(provider_dirs)

  with DockerRunner(args.dockerImage, args.docker_extra_args, extra_env=extra_env,
                    extra_volumes=[f"{os.path.abspath(args.configDir)}:/pkgdist.bits:ro"] if args.docker else [],
                    platform=getattr(args, "dockerPlatform", None)) as getstatusoutput_docker:
    def performPreferCheckWithTempDir(pkg, cmd):
      with tempfile.TemporaryDirectory(prefix=f"bits_prefer_check_{pkg['package']}_") as temp_dir:
        return getstatusoutput_docker(cmd, cwd=temp_dir)

    systemPackages, ownPackages, failed, validDefaults = \
      getPackageList(packages                = packages,
                     specs                   = specs,
                     configDir               = args.configDir,
                     preferSystem            = args.preferSystem,
                     noSystem                = args.noSystem,
                     architecture            = raw_architecture,
                     disable                 = args.disable,
                     force_rebuild           = args.force_rebuild,
                     defaults                = args.defaults,
                     performPreferCheck      = performPreferCheckWithTempDir,
                     performRequirementCheck = performPreferCheckWithTempDir,
                     performValidateDefaults = lambda spec: validateDefaults(spec, args.defaults),
                     overrides               = overrides,
                     taps                    = taps,
                     log                     = debug,
                     provider_dirs          = provider_dirs,
                     defaults_meta           = defaultsMeta)

  dieOnError(validDefaults and any(d not in validDefaults for d in args.defaults),
             "Specified default `%s' is not compatible with the packages you want to build.\n"
             "Valid defaults:\n\n- %s" % ("::".join(args.defaults), "\n- ".join(sorted(validDefaults or []))))
  dieOnError(failed,
             "The following packages are system requirements and could not be found:\n\n- %s\n\n"
             "Please run:\n\n\tbitsDoctor --defaults %s %s\n\nto get a full diagnosis." %
             ("\n- ".join(sorted(failed)), "::".join(args.defaults), " ".join(args.pkgname)))
  
  banner("Configured directory:\n%s", os.path.abspath(args.configDir))
  banner("Package Recipe will be searched in the following order \n%s", os.environ.get("BITS_PATH"))
  # Resolve the effective auto-patch flag for every package. Default behaviour is
  # unchanged: patches are applied automatically. A recipe opts out individually
  # with `auto_patch: false` in its header (already in spec via YAML); the global
  # --no-auto-patch CLI flag or `auto_patch: false` in the active defaults force
  # it off for every package. When off, bits still stages the patch files in
  # $SOURCEDIR and exports $PATCH0..$PATCH_COUNT, but the recipe applies them.
  _global_auto_patch = (bool(getattr(args, "autoPatch", True))
                        and bool(defaultsMeta.get("auto_patch", True)))
  for x in specs.values():
    x["requires"] = [r for r in x["requires"] if r not in args.disable]
    x["build_requires"] = [r for r in x["build_requires"] if r not in args.disable]
    x["runtime_requires"] = [r for r in x["runtime_requires"] if r not in args.disable]
    x["auto_patch"] = _global_auto_patch and bool(x.get("auto_patch", True))

  if systemPackages:
    banner("bits can take the following packages from the system and will not build them:\n  %s",
           ", ".join(systemPackages))

  if ownPackages:
    banner("The following packages cannot be taken from the system and will be built:\n  %s",
           ", ".join(ownPackages))

  buildOrder = list(topological_sort(specs))

  # Check if any of the packages can be picked up from a local checkout
  if args.forceTracked:
    develPkgs = set()
  else:
    develCandidates = {basename(d) for d in glob("*") if os.path.isdir(d)} - frozenset(args.noDevel)
    develCandidatesUpper = {d.upper() for d in develCandidates}
    develPkgs = frozenset(buildOrder) & develCandidates
    develPkgsUpper = {p for p in buildOrder if p.upper() in develCandidatesUpper}
    dieOnError(develPkgs != develPkgsUpper,
               "The following development packages have the wrong spelling: %s.\n"
               "Please check your local checkout and adapt to the correct one indicated." %
               ", ".join(develPkgsUpper - develPkgs))
    del develCandidates, develCandidatesUpper, develPkgsUpper

  if buildOrder:
    if args.onlyDeps:
      builtPackages = buildOrder[:-1]
    else:
      builtPackages = buildOrder
    if len(builtPackages) > 1:
      banner("Packages will be built in the following order:\n - %s",
             "\n - ".join(x+" (development package)" if x in develPkgs else "{}@{}".format(x, specs[x]["tag"])
                          for x in builtPackages if x != "defaults-release"))
    else:
      banner("No dependencies of package %s to build.", buildOrder[-1])


  if develPkgs:
    banner("You have packages in development mode (%s).\n"
           "This means their source code can be freely modified under:\n\n"
           "  %s/<package_name>\n\n"
           "bits does not automatically update such packages to avoid work loss.\n"
           "In most cases this is achieved by doing in the package source directory:\n\n"
           "  git pull --rebase\n",
           ", ".join(develPkgs),
           os.getcwd())

  for pkg, spec in specs.items():
    spec["is_devel_pkg"] = pkg in develPkgs
    if spec["is_devel_pkg"]:
      spec["source"] = str(Path.cwd() / pkg)

    # Only initialize Sapling if it's in PATH and the repo uses it
    use_sapling = False
    if "source" in spec:
        source_path = Path(spec["source"])
        has_sapling = ( (source_path / ".sl").exists() or (source_path / ".git" / "sl").exists() )
        if has_sapling and shutil.which("sl"):
            use_sapling = True
    spec["scm"] = Sapling() if use_sapling else Git()

    reference_repo = join(os.path.abspath(args.referenceSources), pkg.lower())
    if exists(reference_repo):
      spec["reference"] = reference_repo
  del develPkgs

  # Clone/update repos
  update_git_repos(args, specs, buildOrder)
  # This is the list of packages which have untracked files in their
  # source directory, and which are rebuilt every time. We will warn
  # about them at the end of the build.
  untrackedFilesDirectories = []

  buildTargets = []
  
  # Resolve the tag to the actual commit ref
  for p in buildOrder:
    spec = specs[p]
    spec["commit_hash"] = "0"
    develPackageBranch = ""
    # This is a development package (i.e. a local directory named like
    # spec["package"]), but there is no "source" key in its bits recipe,
    # so there shouldn't be any code for it! Presumably, a user has
    # mistakenly named a local directory after one of our packages.
    dieOnError("source" not in spec and spec["is_devel_pkg"],
               "Found a directory called {package} here, but we're not "
               "expecting any code for the package {package}. If this is a "
               "mistake, please rename the {package} directory or use the "
               "'--no-local {package}' option. If bits should pick up "
               "source code from this directory, add a 'source:' key to "
               "{recipe}.sh instead."
               .format(package=p, recipe=p.lower()))

    if "tag" not in spec:
      spec["tag"] = spec["version"]
    if "source" in spec:
      # Tag may contain date params like %(year)s, %(month)s, %(day)s, %(hour).
      spec["tag"] = resolve_tag(spec)
      # First, we try to resolve the "tag" as a branch name, and use its tip as
      # the commit_hash. If it's not a branch, it must be a tag or a raw commit
      # hash, so we use it directly. Finally if the package is a development
      # one, we use the name of the branch as commit_hash.
      assert "scm_refs" in spec
      try:
        spec["commit_hash"] = spec["scm_refs"]["refs/heads/" + spec["tag"]]
      except KeyError:
        spec["commit_hash"] = spec["tag"]
      # We are in development mode, we need to rebuild if the commit hash is
      # different or if there are extra changes on top.
      if spec["is_devel_pkg"]:
        # Devel package: we get the commit hash from the checked source, not from remote.
        out = spec["scm"].checkedOutCommitName(directory=spec["source"])
        spec["commit_hash"] = out.strip()
        local_hash, untracked = hash_local_changes(spec)
        untrackedFilesDirectories.extend(untracked)
        spec["devel_hash"] = spec["commit_hash"] + local_hash
        out = spec["scm"].branchOrRef(directory=spec["source"])
        develPackageBranch = out.replace("/", "-")
        spec["tag"] = args.develPrefix if "develPrefix" in args else develPackageBranch
        spec["commit_hash"] = "0"

    if "sources" in spec:
      for i, s in enumerate(spec["sources"]):
        resolved = resolveLocalPath(args.configDir, s)
        spec["sources"][i] = resolved 
      spec["commit_hash"] = spec["tag"]
    # Version may contain date params like tag, plus %(commit_hash)s,
    # %(short_hash)s and %(tag)s.
    spec["version"] = resolve_version(spec, args.defaults, branch_basename, branch_stream)

    spec.setdefault("variables", OrderedDict(spec.get("variables", {})))
    variables = spec["variables"]
    if "Python" in spec.get("requires", []):
        # Find the Python package spec safely
        python_version_str = ""
        py_spec = specs.get("Python")
        if isinstance(py_spec, dict):
            python_version_str = (py_spec.get("version", "") or "").replace("v", "")
        python_version = python_version_str.split(".") if python_version_str else []

        # Safely extract major, minor, patch versions
        major = python_version[0] if len(python_version) > 0 else "0"
        minor = python_version[1] if len(python_version) > 1 else "0"
        patch = python_version[2] if len(python_version) > 2 else "0"

        # Populate variables dictionary
        variables.update({
            "python_major_version": major,
            "python_minor_version": minor,
            "python_patch_version": patch,
            "python_major_minor": f"{major}.{minor}",
            "python_major_minor_str": f"{major}{minor}",
        })
    for k, v in variables.items():
      variables[k] = resolve_spec_data(spec, v, args.defaults, branch_basename, branch_stream)
    if "source" in spec:
      spec["source"] = resolve_spec_data(spec, spec["source"], args.defaults, branch_basename, branch_stream)
    if "sources" in spec:
      spec["sources"] = [resolve_spec_data(spec, src, args.defaults, branch_basename, branch_stream) for src in spec["sources"]]
    if "patches" in spec:
      spec["patches"] = [resolve_spec_data(spec, p, args.defaults, branch_basename, branch_stream) for p in spec["patches"]]
    # Variables defined in the active --defaults profile's `variables:` block are
    # available to every recipe body.  When a recipe does not itself opt into
    # expansion (no `variables:` / `expand_recipe: true`) we expand it in SOFT
    # mode: only known variables are substituted and any other %(...)s / bare %
    # is left untouched, so profile-wide variables never clobber or break a
    # recipe that happens to contain a literal %(...)s or shell `%`.
    default_vars = defaultsMeta.get("variables") or None
    recipe_opts_in = bool(variables or spec.get("expand_recipe", False))
    if recipe_opts_in or default_vars:
      spec["recipe"] = resolve_spec_data(spec, spec["recipe"], args.defaults,
                                         branch_basename, branch_stream,
                                         default_vars=default_vars,
                                         strict=recipe_opts_in)

    if spec["is_devel_pkg"] and "develPrefix" in args and args.develPrefix != "ali-master":
      spec["version"] = args.develPrefix

  # Decide what is the main package we are building and at what commit.
  #
  # We emit an event for the main package, when encountered, so that we can use
  # it to index builds of the same hash on different architectures. We also
  # make sure add the main package and it's hash to the debug log, so that we
  # can always extract it from it.
  # If one of the special packages is in the list of packages to be built,
  # we use it as main package, rather than the last one.
  if not buildOrder:
    banner("Nothing to be done.")
    return
  mainPackage = buildOrder[-1]
  mainHash = specs[mainPackage]["commit_hash"]

  debug("Main package is %s@%s", mainPackage, mainHash)
  log_current_package(None, mainPackage, specs, getattr(args, "develPrefix", None))

  # Now that we have the main package set, we can print out Useful information
  # which we will be able to associate with this build. Also lets make sure each package
  # we need to build can be built with the current default.
  for p in buildOrder:
    spec = specs[p]
    if "source" in spec:
      debug("Commit hash for %s@%s is %s", spec["source"], spec["tag"], spec["commit_hash"])

  # We recursively calculate the full set of requires "full_requires"
  # including build_requires and the subset of them which are needed at
  # runtime "full_runtime_requires". Do this in build order, so that we can
  # rely on each spec's dependencies already having their full_*_requires
  # properties populated.
  for p in buildOrder:
    spec = specs[p]
    for key in ("requires", "runtime_requires", "build_requires"):
      full_key = "full_" + key
      spec[full_key] = set()
      for dep in spec.get(key, ()):
        spec[full_key].add(dep)
        # Runtime deps of build deps should count as build deps.
        spec[full_key] |= specs[dep]["full_requires" if key == "build_requires" else full_key]
    # Propagate build deps of runtime deps, so that they are not added into
    # the generated modulefile by bits-generate-module.
    for dep in spec["runtime_requires"]:
      spec["full_build_requires"] |= specs[dep]["full_build_requires"]
    # If something requires or runtime_requires a package, then it's not a
    # pure build_requires only anymore, so we drop it from the list.
    spec["full_build_requires"] -= spec["full_runtime_requires"]
   # Use the selected plugin to build, instead of the default behaviour, if a
  # plugin was selected.
  if args.plugin != "legacy":
    return importlib.import_module("bits_helpers.%s_plugin" % args.plugin) \
                    .build_plugin(specs, args, buildOrder)

  debug("We will build packages in the following order: %s", " ".join(buildOrder))
  if args.dryRun:
    info("--dry-run / -n specified. Not building.")
    return

  # Validate --pipeline: it requires --makeflow.
  if getattr(args, "pipeline", False) and not args.makeflow:
    warning("--pipeline requires --makeflow; disabling --pipeline for this run.")
    args.pipeline = False

  # We now iterate on all the packages, making sure we build correctly every
  # single one of them. This is done this way so that the second time we run we
  # can check if the build was consistent and if it is, we bail out.
  report_event("install", "{p} disabled={dis} devel={dev} system={sys} own={own} deps={deps}".format(
    p=args.pkgname,
    dis=",".join(sorted(args.disable)),
    dev=",".join(sorted(spec["package"] for spec in specs.values() if spec["is_devel_pkg"])),
    sys=",".join(sorted(systemPackages)),
    own=",".join(sorted(ownPackages)),
    deps=",".join(buildOrder[:-1]),
  ), args.architecture)

  buildList=[]
  # Specs collected during the build loop for the post-build checksum phase.
  # Every processed spec is appended here, including those whose tarball was
  # already cached, so that --print-checksums / --write-checksums (and the
  # equivalent defaults-profile fields) cover the full build closure.
  specs_for_checksum_phase = []
  # If we are building only the dependencies, the last package in
  # the build order can be considered done.
  if args.onlyDeps and len(buildOrder) > 1:
    mainPackage = buildOrder.pop()
    warning("Not rebuilding %s because --only-deps option provided.", mainPackage)

  # Records {package: scriptDir} for packages whose build we resource-monitor,
  # so we can distil per-package CPU/RAM stats at the end of the run (P3).
  monitoredDirs = {}

  scheduler = None
  if (args.builders > 1) and buildOrder:
    from bits_helpers.scheduler import Scheduler
    from bits_helpers.log import logger

    # --- Self-tuning resource stats (P3) --------------------------------------
    # When building many packages in parallel, hand the scheduler per-package
    # CPU/RAM estimates so its ResourceManager only admits a new build when the
    # machine still has budget — preventing N heavy builds from starting at once
    # and thrashing the node.  We auto-load the stats file a previous run left
    # behind (re-stamped for this machine), and auto-enable monitoring so the
    # current run refreshes it.
    #
    # This measurement-driven gating is OPT-IN (--auto-resources), off by
    # default: without it, concurrency is bounded purely by --builders, which is
    # more predictable.  Explicit --resources / --resource-monitoring still take
    # precedence and work regardless of the flag.
    if getattr(args, "autoResources", False):
      if not args.resources:
        from bits_helpers.build_stats import autoload_stats_path
        _auto_stats = autoload_stats_path(workDir)
        if _auto_stats:
          args.resources = _auto_stats
          info("Auto-loaded build resource stats from a previous run: %s", _auto_stats)
      if not args.resourceMonitoring:
        try:
          import psutil  # noqa: F401  (availability probe only)
          args.resourceMonitoring = True
          debug("Enabled resource monitoring (--builders > 1) to record build stats")
        except Exception:  # pylint: disable=broad-except
          debug("psutil unavailable; resource monitoring stays off")

    scheduler = Scheduler(args.builders, logDelegate=logger, buildStats=args.resources,
                          parallelDownloads=max(1, getattr(args, "parallelDownloads", 2)))

    # Collect concise per-package failures during the run so we can write a
    # readable summary at the end (write_failure_summary), instead of leaving the
    # user to scroll the full verbose error dump.
    import threading as _threading
    scheduler.buildFailures = []
    scheduler.buildFailuresLock = _threading.Lock()

    # Optionally stagger concurrent build jobs across OS 'nice' levels so CPU
    # contention degrades gracefully (lead build at nice 0, others niced down).
    # OPT-IN (--build-nice), off by default: the default --builders path does no
    # command wrapping and starts no renice-watchdog thread.
    scheduler.nice_ladder = None
    scheduler.renice_watchdog = None
    if getattr(args, "buildNice", False):
      from bits_helpers.nice_ladder import NiceLadder, ReniceWatchdog
      scheduler.nice_ladder = NiceLadder(args.builders, step=getattr(args, "buildNiceStep", 5))
      info("Build nice-ladder enabled (--build-nice): %d slots, step %d (lead build at nice 0).",
           args.builders, getattr(args, "buildNiceStep", 5))
      # A build backed off by the ladder can become the last straggler and
      # crawl; a watchdog boosts the longest such build back to full speed, one
      # at a time.  Native builds are reniced toward 0; docker builds (each in
      # its own named container) get their cgroup cpu-shares restored via
      # `docker update`.
      _boost_after = getattr(args, "buildNiceBoostAfter", 600)
      if _boost_after:
        scheduler.renice_watchdog = ReniceWatchdog(boost_after=_boost_after, log=info).start()

  # --- Stale sentinel cleanup -------------------------------------------------
  # Remove any leftover *.downloading sentinels from a previous run that was
  # killed before it could clean up.  This must happen BEFORE launching the
  # prefetch pool so that no live sentinels are confused with stale ones.
  # Use os.walk rather than glob(..., recursive=True) to avoid the mock in tests.
  if os.path.isdir(workDir):
    for _root, _dirs, _files in os.walk(workDir):
      for _fname in _files:
        if _fname.endswith(".downloading"):
          _s = os.path.join(_root, _fname)
          debug("Removing stale sentinel: %s", _s)
          try:
            os.unlink(_s)
          except OSError:
            pass

  # --- Optional prefetch pool -------------------------------------------------
  # Default (-1) means "auto": scale with the number of builders so that, on the
  # serial preparation loop, downloads overlap instead of blocking — capped at 4
  # to avoid hammering the store.  0 explicitly disables prefetch; N>0 forces N.
  _prefetch_workers = getattr(args, "prefetchWorkers", -1)
  if _prefetch_workers < 0:
    _prefetch_workers = min(max(int(getattr(args, "builders", 1)), 1), 4)
  _prefetch_executor = None
  if _prefetch_workers > 0 and buildOrder and not isinstance(syncHelper,
      __import__("bits_helpers.sync", fromlist=["NoRemoteSync"]).NoRemoteSync):
    debug("Starting %d prefetch worker(s)", _prefetch_workers)
    _prefetch_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=_prefetch_workers,
        thread_name_prefix="bits-prefetch",
    )
    for _pkg in buildOrder:
      _pspec = specs[_pkg]
      _prefetch_executor.submit(_prefetch_package, _pspec, syncHelper, workDir, args.architecture)
    # Do NOT call executor.shutdown() here — we let it run in the background
    # and join lazily via a daemon-thread finaliser registered below.
    import atexit
    atexit.register(lambda ex=_prefetch_executor: ex.shutdown(wait=False, cancel_futures=True))

  while buildOrder:
    p = buildOrder.pop(0)
    spec = specs[p]
    log_current_package(p, mainPackage, specs, getattr(args, "develPrefix", None))

    # Calculate the hashes. We do this in build order so that we can guarantee
    # that the hashes of the dependencies are calculated first. Do this inside
    # the main build loop to make sure that our dependencies have been assigned
    # a single, definitive hash.
    debug("Calculating hash.")
    debug("develPkgs = %r", sorted(spec["package"] for spec in specs.values() if spec["is_devel_pkg"]))
    storeHook(p, specs, args.defaults[0])
    storeHashes(p, specs, considerRelocation=(
      raw_architecture.startswith("osx") and spec.get("architecture") != SHARED_ARCH
    ))
    debug("Hashes for recipe %s are %s (remote); %s (local)", p,
          ", ".join(spec["remote_hashes"]), ", ".join(spec["local_hashes"]))

    # Warn if a package declares architecture: shared but has arch-specific
    # deps — the shared label would be misleading in that case because its
    # hash (and therefore install path) will differ across platforms.
    if spec.get("architecture") == SHARED_ARCH:
      arch_specific_deps = [
        dep for dep in spec.get("requires", [])
        if dep != "defaults-release" and specs[dep].get("architecture") != SHARED_ARCH
      ]
      if arch_specific_deps:
        warning(
          "Package %s declares 'architecture: shared' but depends on "
          "arch-specific package(s): %s. Its hash may differ across platforms.",
          spec["package"], ", ".join(arch_specific_deps),
        )

    if spec["is_devel_pkg"] and getattr(syncHelper, "writeStore", None):
      warning("Disabling remote write store from now since %s is a development package.", spec["package"])
      syncHelper.writeStore = ""

    # Since we can execute this multiple times for a given package, in order to
    # ensure consistency, we need to reset things and make them pristine.
    spec.pop("revision", None)

    debug("Updating from tarballs")
    # If we arrived here it really means we have a tarball which was created
    # using the same recipe. We will use it as a cache for the build. This means
    # that while we will still perform the build process, rather than
    # executing the build itself we will:
    #
    # - Unpack it in a temporary place.
    # - Invoke the relocation specifying the correct work_dir and the
    #   correct path which should have been used.
    # - Move the version directory to its final destination, including the
    #   correct revision.
    # - Repack it and put it in the store with the
    #
    # this will result in a new package which has the same binary contents of
    # the old one but where the relocation will work for the new dictory. Here
    # we simply store the fact that we can reuse the contents of cachedTarball.
    syncHelper.fetch_symlinks(spec)

    # Decide how it should be called, based on the hash and what is already
    # available.
    debug("Checking for packages already built.")

    # ---- force_revision bypass -----------------------------------------------
    # When force_revision is provided in defaults-*.sh (per-package overrides:
    # block or top-level global field), skip the symlink-scanning and revision
    # counter logic entirely.  The content-addressed store still uses the
    # package hash, so binary integrity is preserved regardless of the label.
    #
    # Risk: if force_revision is "" (empty), two incompatible builds of the
    # same version will share the same install path (<pkg>/<version>/) and the
    # convenience symlink will be silently overwritten by the later build.
    # The hash-addressed store path is NOT affected.
    if "force_revision" in spec:
      forced = spec["force_revision"]   # "" → revision-less; "X" → literal
      spec["revision"] = forced
      if not forced:
        warning(
          "Package %s: force_revision is empty — install path will omit "
          "the revision suffix (%s/%s). If two incompatible builds of "
          "this version coexist the convenience symlink will be silently "
          "overwritten.", spec["package"], spec["package"], spec["version"],
        )
      # Hash was already computed; align spec["hash"] to the remote store
      # (forced revisions are never prefixed with "local").
      spec["hash"] = spec["remote_revision_hash"]
    else:
      # Normal revision-counter logic: scan existing symlinks and find the
      # next free (or already-matching) revision number.
      #
      # Make sure this regex broadly matches the regex below that parses the
      # symlink's target. Overly-broadly matching the version, for example,
      # can lead to false positives that trigger a warning below.
      spec_arch = effective_arch(spec, args.architecture)
      # The revision group is made optional ((?:-(?:local)?[0-9]+)?) so that
      # symlinks created when force_revision="" (revision-less path) are also
      # picked up by subsequent normal builds of the same version.
      links_regex = re.compile(
        r"{package}-{version}(?:-(?:local)?[0-9]+)?\.{arch}\.tar\.gz".format(
          package=re.escape(spec["package"]),
          version=re.escape(spec["version"]),
          arch=re.escape(spec_arch),
        ))
      symlink_dir = join(workDir, "TARS", spec_arch, spec["package"])
      try:
        packages = [join(symlink_dir, symlink_path)
                    for symlink_path in os.listdir(symlink_dir)
                    if links_regex.fullmatch(symlink_path)]
      except OSError:
        # If symlink_dir does not exist or cannot be accessed, return an empty
        # list of packages.
        packages = []
      del links_regex, symlink_dir

    # Calculate the build_family for the package.
    #
    # If the package is a devel package, we need to associate it a devel
    # prefix, either via the -z option or using its checked out branch. This
    # affects its build hash.
    #
    # Moreover we need to define a global "buildFamily" which is used
    # to tag all the packages incurred in the build, this way we can have
    # a latest-<buildFamily> link for all of them an we will not incur in the
    # flip - flopping described in https://github.com/alisw/alibuild/issues/325.
    develPrefix = ""
    possibleDevelPrefix = getattr(args, "develPrefix", develPackageBranch)
    if spec["is_devel_pkg"]:
      develPrefix = possibleDevelPrefix

    if possibleDevelPrefix:
      spec["build_family"] = "{}-{}".format(possibleDevelPrefix, "_".join(args.defaults))
    else:
      spec["build_family"] = "_".join(args.defaults)
    if spec["package"] == mainPackage:
      mainBuildFamily = spec["build_family"]

    if "force_revision" not in spec:
      # Normal revision-counter path: scan existing symlinks to find a reusable
      # or the next free revision number.
      # In case there is no installed software, revision is 1
      # If there is already an installed package:
      # - Remove it if we do not know its hash
      # - Use the latest number in the version, to decide its revision
      debug("Packages already built using this version\n%s", "\n".join(packages))

      candidate = None
      busyRevisions = set()
      # We can tell that the remote store is read-only if it has an empty or
      # no writeStore property. See below for explanation of why we need this.
      revisionPrefix = "" if getattr(syncHelper, "writeStore", "") else "local"
      for symlink_path in packages:
        # Skip dangling symlinks: a missing target means the tarball was deleted
        # from the store (e.g. by a partial cleanup) and cannot be reused.
        # readlink() succeeds even for dangling symlinks, so we must check
        # existence explicitly.
        if not os.path.isfile(symlink_path):
          warning("Ignoring dangling symlink in tarball directory: %s", symlink_path)
          continue
        realPath = readlink(symlink_path)
        # The revision group is optional ((?:-((?:local)?[0-9]+))?) to handle
        # symlinks previously created with force_revision="" (revision-less).
        matcher = (
          r"../../{arch}/store/[0-9a-f]{{2}}/([0-9a-f]+)/"
          r"{package}-{version}(?:-((?:local)?[0-9]+))?\.{arch}\.tar\.gz$"
        ).format(arch=spec_arch, **spec)
        match = re.match(matcher, realPath)
        if not match:
          warning("Symlink %s -> %s couldn't be parsed", symlink_path, realPath)
          continue
        rev_hash, revision = match.groups()
        if revision is None:
          # Symlink points to a revision-less tarball (force_revision="").
          # Treat it as a busy slot so we do not overwrite it inadvertently.
          continue

        if not (("local" in revision and rev_hash in spec["local_hashes"]) or
                ("local" not in revision and rev_hash in spec["remote_hashes"])):
          # This tarball's hash doesn't match what we need. Remember that its
          # revision number is taken, in case we assign our own later.
          if revision.startswith(revisionPrefix) and revision[len(revisionPrefix):].isdigit():
            # Strip revisionPrefix; the rest is an integer. Convert it to an int
            # so we can get a sensible max() existing revision below.
            busyRevisions.add(int(revision[len(revisionPrefix):]))
          continue

        # Don't re-use local revisions when we have a read-write store, so that
        # packages we'll upload later don't depend on local revisions.
        if getattr(syncHelper, "writeStore", False) and "local" in revision:
          debug("Skipping revision %s because we want to upload later", revision)
          continue

        # If we have an hash match, we use the old revision for the package
        # and we do not need to build it. Because we prefer reusing remote
        # revisions, only store a local revision if there is no other candidate
        # for reuse yet.
        candidate = better_tarball(spec, candidate, (revision, rev_hash, symlink_path))

      try:
        revision, rev_hash, symlink_path = candidate
      except TypeError:  # raised if candidate is still None
        # If we can't reuse an existing revision, assign the next free revision
        # to this package. If we're not uploading it, name it localN to avoid
        # interference with the remote store -- in case this package is built
        # somewhere else, the next revision N might be assigned there, and would
        # conflict with our revision N.
        # The code finding busyRevisions above already ensures that revision
        # numbers start with revisionPrefix, and has left us plain ints.
        spec["revision"] = revisionPrefix + str(
          min(set(range(1, max(busyRevisions) + 2)) - busyRevisions)
          if busyRevisions else 1)
      else:
        spec["revision"] = revision
        # Remember what hash we're actually using.
        spec["local_revision_hash" if revision.startswith("local")
             else "remote_revision_hash"] = rev_hash
        if spec["is_devel_pkg"] and "incremental_recipe" in spec:
          spec["obsolete_tarball"] = symlink_path
        else:
          debug("Package %s with hash %s is already found in %s. Not building.",
                p, rev_hash, symlink_path)
          # Ignore errors here, because the path we're linking to might not
          # exist (if this is the first run through the loop). On the second run
          # through, the path should have been created by the build process.
          call_ignoring_oserrors(symlink, ver_rev(spec),
                                 join(dirname(_pkg_install_path(workDir, effective_arch(spec, args.architecture), spec)),
                                      "latest-{build_family}".format(**spec)))
          call_ignoring_oserrors(symlink, ver_rev(spec),
                                 join(dirname(_pkg_install_path(workDir, effective_arch(spec, args.architecture), spec)), "latest"))

      # Now we know whether we're using a local or remote package, so we can
      # set the proper hash and tarball directory.
      if spec["revision"].startswith("local"):
        spec["hash"] = spec["local_revision_hash"]
      else:
        spec["hash"] = spec["remote_revision_hash"]

    # We do not use the override for devel packages, because we
    # want to avoid having to rebuild things when the /tmp gets cleaned.
    if spec["is_devel_pkg"]:
        buildWorkDir = args.workDir
    else:
        buildWorkDir = os.environ.get("BITS_BUILD_WORK_DIR", args.workDir)

    buildRoot = join(buildWorkDir, "BUILD", spec["hash"])

    spec["old_devel_hash"] = readHashFile(join(
      buildRoot, spec["package"], ".build_succeeded"))

    # Recreate symlinks to this development package builds.
    if spec["is_devel_pkg"]:
      debug("Creating symlinks to builds of devel package %s", spec["package"])
      # Ignore errors here, because the path we're linking to might not exist
      # (if this is the first run through the loop). On the second run
      # through, the path should have been created by the build process.
      call_ignoring_oserrors(symlink, spec["hash"], join(buildWorkDir, "BUILD", spec["package"] + "-latest"))
      if develPrefix:
        call_ignoring_oserrors(symlink, spec["hash"], join(buildWorkDir, "BUILD", spec["package"] + "-latest-" + develPrefix))
      # Last package built gets a "latest" mark.
      call_ignoring_oserrors(symlink, ver_rev(spec),
                             join(dirname(_pkg_install_path(workDir, effective_arch(spec, args.architecture), spec)), "latest"))
      # Latest package built for a given devel prefix gets a "latest-<family>" mark.
      if spec["build_family"]:
        call_ignoring_oserrors(symlink, ver_rev(spec),
                               join(dirname(_pkg_install_path(workDir, effective_arch(spec, args.architecture), spec)),
                                    "latest-" + spec["build_family"]))

    # Check if this development package needs to be rebuilt.
    if spec["is_devel_pkg"]:
      debug("Checking if devel package %s needs rebuild", spec["package"])
      # The source is unchanged only if devel_hash+deps_hash still matches the
      # sentinel.  But the install directory is named after ver_rev(spec), and
      # the *revision* can change without a source change (e.g. the dependency
      # hash shifted, so a new localN was assigned in the revision scan above).
      # When that happens the new revision's directory was never populated, yet
      # every consumer's init.sh sources this dependency at the new ver_rev --
      # so skipping the rebuild would leave them pointing at a missing
      # .../<pkg>/<ver_rev>/etc/profile.d/init.sh.  Only skip when that
      # directory actually exists.
      devel_install_dir = _pkg_install_path(
        workDir, effective_arch(spec, args.architecture), spec)
      if spec["devel_hash"]+spec["deps_hash"] == spec["old_devel_hash"] \
         and os.path.isdir(devel_install_dir):
        info("Development package %s does not need rebuild", spec["package"])
        continue
      if spec["devel_hash"]+spec["deps_hash"] == spec["old_devel_hash"]:
        debug("Devel package %s source unchanged but install dir %s is missing "
              "(revision changed to %s); rebuilding to populate it.",
              spec["package"], devel_install_dir, ver_rev(spec))

    # Now that we have all the information about the package we want to build, let's
    # check if it wasn't built / unpacked already.
    hashPath = _pkg_install_path(workDir, effective_arch(spec, args.architecture), spec)
    hashFile = hashPath + "/.build-hash"
    # If the folder is a symlink that resolves to an existing directory,
    # we consider it to be on CVMFS and take the hash for good.
    # We must also check os.path.isdir() (which follows symlinks) so that
    # dangling symlinks — e.g. created by a previous --makeflow run that
    # wrote fetch_symlinks() entries before the actual tarball existed —
    # are NOT mistaken for a successfully installed package.
    if os.path.islink(hashPath) and os.path.isdir(hashPath):
      fileHash = spec["hash"]
    else:
      fileHash = readHashFile(hashFile)
    # Development packages have their own rebuild-detection logic above.
    # spec["hash"] is only useful here for regular packages.
    if fileHash == spec["hash"] and not spec["is_devel_pkg"]:
      # If we get here, we know we are in sync with whatever remote store.  We
      # can therefore create a directory which contains all the packages which
      # were used to compile this one.
      debug("Package %s was correctly compiled. Moving to next one.", spec["package"])
      # If using incremental builds, next time we execute the script we need to remove
      # the placeholders which avoid rebuilds.
      if spec["is_devel_pkg"] and "incremental_recipe" in spec:
        unlink(hashFile)
      if "obsolete_tarball" in spec:
        unlink(realpath(spec["obsolete_tarball"]))
        unlink(spec["obsolete_tarball"])
      # We can now delete the INSTALLROOT and BUILD directories,
      # assuming the package is not a development one. We also can
      # delete the SOURCES in case we have aggressive-cleanup enabled.
      if not spec["is_devel_pkg"] and args.autoCleanup:
        cleanupDirs = [buildRoot,
                       join(workDir, "INSTALLROOT", spec["hash"])]
        if args.aggressiveCleanup:
          cleanupDirs.append(join(workDir, "SOURCES", spec["package"]))
        debug("Cleaning up:\n%s", "\n".join(cleanupDirs))

        for d in cleanupDirs:
          shutil.rmtree(d.encode("utf8"), True)
        try:
          unlink(join(buildWorkDir, "BUILD", spec["package"] + "-latest"))
          if "develPrefix" in args:
            unlink(join(buildWorkDir, "BUILD", spec["package"] + "-latest-" + args.develPrefix))
        except Exception:
          pass
        try:
          rmdir(join(buildWorkDir, "BUILD"))
          rmdir(join(workDir, "INSTALLROOT"))
        except Exception:
          pass
      # Record in the build manifest that this package was already installed.
      if getattr(args, "manifest", None) is not None:
        args.manifest.add_package(spec, "already_installed",
                                  effective_architecture=effective_arch(spec, args.architecture))
      # Touch the sentinel so the cleanup command knows this package was used.
      try:
        from bits_helpers.cleanup import touch_sentinel as _touch_sentinel
        _touch_sentinel(workDir, args.architecture, spec["package"], ver_rev(spec))
      except Exception:
        pass
      continue

    if fileHash != "0":
      debug("Mismatch between local area (%s) and the one which I should build (%s). Redoing.",
            fileHash, spec["hash"])
    # shutil.rmtree under Python 2 fails when hashFile is unicode and the
    # directory contains files with non-ASCII names, e.g. Golang/Boost.
    shutil.rmtree(dirname(hashFile).encode("utf-8"), True)

    tar_hash_dir = os.path.join(workDir, resolve_store_path(effective_arch(spec, args.architecture), spec["hash"]))
    debug("Looking for cached tarball in %s", tar_hash_dir)
    spec["cachedTarball"] = ""
    if not spec["is_devel_pkg"]:
      # If a prefetch worker is downloading this tarball, wait for it to finish
      # before we try to use the result.  The sentinel (tar_hash_dir + ".downloading")
      # is only created when a prefetch pool is active, so skip the check otherwise.
      if _prefetch_workers > 0:
        from bits_helpers.download import _wait_for_sentinel as _wfs
        _wfs(tar_hash_dir)
      syncHelper.fetch_tarball(spec)
      tarballs = [t for t in glob(os.path.join(tar_hash_dir, "*gz"))
                  if os.path.isfile(t)]  # skip dangling symlinks
      spec["cachedTarball"] = tarballs[0] if len(tarballs) else ""
      debug("Found tarball in %s" % spec["cachedTarball"]
            if spec["cachedTarball"] else "No cache tarballs found")
      # Verify the recalled tarball against the local integrity ledger.
      # Only active when --store-integrity is set (or store_integrity = true
      # in bits.rc); off by default for backward compatibility.
      if spec["cachedTarball"] and getattr(args, "storeIntegrity", False):
        from bits_helpers.store_integrity import verify_tarball_checksum
        verify_tarball_checksum(spec, workDir, args.architecture, spec["cachedTarball"])

    # The actual build script.
    
    fp = open(dirname(realpath(__file__))+'/build_template.sh')
    cmd_raw = fp.read()
    fp.close()

    container_workDir = ""
    cachedTarball = spec["cachedTarball"]
    if args.docker:
      cvmfs_prefix = getattr(args, "cvmfsPrefix", None)
      if cvmfs_prefix:
        # When --cvmfs-prefix is set, mount workDir at the CVMFS path inside
        # the container.  The build system then compiles packages with their
        # final CVMFS install prefix, eliminating the relocation step on publish.
        container_workDir = cvmfs_prefix
        # Adjust any cached tarball path the same way.
        cachedTarball = re.sub("^" + re.escape(workDir), container_workDir, cachedTarball)
      elif not args.containerUseWorkDir:
        container_workDir = "/container/bits/sw"
        cachedTarball = re.sub("^" + re.escape(workDir), container_workDir, cachedTarball)
      else:
        container_workDir = workDir

    # Resolve the effective checksum mode for this package, taking into account
    # CLI flags, per-recipe enforce_checksums, and the defaults-profile
    # checksum_mode field (via defaultsMeta).
    effective_checksum_mode = checksum_enforcement_mode(spec, args, defaultsMeta)

    if not cachedTarball:
      # During download only apply warn/enforce — these are security gates that
      # must fire before compilation.  print/write are deferred to the
      # post-build phase so they work for already-cached packages too.
      #
      # In Makeflow mode we skip the sequential checkout here and instead
      # generate a .checkout Makeflow rule per package so that all clones and
      # archive downloads run in parallel as part of the DAG.
      #
      # In --builders mode (args.builders > 1) we likewise defer the checkout:
      # it is registered below as a scheduler "download" task (fetch:<pkg>) that
      # the build task depends on, so source downloads overlap compilation
      # instead of running serially here before any build starts.  Only the
      # single-builder path still checks out inline.
      if not args.makeflow and args.builders == 1:
        try:
          checkout_sources(spec, workDir, args.referenceSources, args.docker,
                           enforce_mode=_download_time_mode(effective_checksum_mode),
                           sync_helper=syncHelper,
                           parallel_sources=getattr(args, "parallelSources", 1),
                           architecture=raw_architecture)
        except OSError as e:
          dieOnError(True, "Failed to fetch sources for %s@%s: %s" % (
            spec.get("package", "?"), spec.get("version", "?"), e))

    # Collect every processed spec for the post-build checksum phase.
    # This includes specs whose tarball was cached (cachedTarball != "").
    specs_for_checksum_phase.append(spec)

    family = spec.get("pkg_family", "")
    # ver_rev(spec) is used so that the SPECS directory name matches the actual
    # install path when force_revision is set (e.g. "" drops the revision suffix).
    scriptDir = join(workDir, "SPECS", effective_arch(spec, args.architecture),
                     *([family] if family else []),
                     spec["package"],
                     ver_rev(spec))

    init_workDir = container_workDir if args.docker else args.workDir
    makedirs(scriptDir, exist_ok=True)
    # Remember where the resource monitor will write this package's trace so we
    # can aggregate build stats once the run finishes (P3).
    if args.resourceMonitoring:
      monitoredDirs[p] = scriptDir
    writeAll("{}/{}.sh".format(scriptDir, spec["package"]), spec["recipe"])
    hook_params_locals = "\n  ".join(
      'export %s="%s"' % (k, v) for k, v in spec.get("hook_params", {}).items()
    )
    writeAll("%s/build.sh" % scriptDir, cmd_raw % {
      "provenance": create_provenance_info(spec["package"], specs, args),
      "initdotsh_deps": generate_initdotsh(p, specs, args.architecture, workDir=init_workDir, post_build=False),
      "initdotsh_full": generate_initdotsh(p, specs, args.architecture, workDir=init_workDir, post_build=True),
      "develPrefix": develPrefix,
      "workDir": workDir,
      "configDir": abspath(args.configDir),
      "incremental_recipe": spec.get("incremental_recipe", ":"),
      "requires": " ".join(spec["requires"]),
      "build_requires": " ".join(spec["build_requires"]),
      "runtime_requires": " ".join(spec["runtime_requires"]),
      "BITS_HOOK_PARAMS": hook_params_locals,
    })

    # Define the environment so that it can be passed up to the
    # actual build script
    bits_dir = dirname(dirname(realpath(__file__)))
    buildEnvironment = [
      ("ARCHITECTURE", raw_architecture),
      ("EFFECTIVE_ARCHITECTURE", effective_arch(spec, args.architecture)),
      ("BUILD_REQUIRES", " ".join(spec["build_requires"])),
      ("CACHED_TARBALL", cachedTarball),
      ("CAN_DELETE", args.aggressiveCleanup and "1" or ""),
      ("COMMIT_HASH", short_commit_hash(spec)),
      ("DEPS_HASH", spec.get("deps_hash", "")),
      ("DEVEL_HASH", spec.get("devel_hash", "")),
      ("DEVEL_PREFIX", develPrefix),
      ("BUILD_FAMILY", spec["build_family"]),
      ("GIT_COMMITTER_NAME", "unknown"),
      ("GIT_COMMITTER_EMAIL", "unknown"),
      ("INCREMENTAL_BUILD_HASH", spec.get("incremental_hash", "0")),
      ("JOBS", str(effective_jobs(args.jobs, spec, builders=args.builders,
                                  oversubscribe=getattr(args, "oversubscribe", 1.0) or 1.0))),
      ("PKGFAMILY", spec.get("pkg_family", "")),
      ("PKGHASH", spec["hash"]),
      ("PKGNAME", spec["package"]),
      ("PKGDIR", spec["pkgdir"]),
      ("PKGREVISION", spec["revision"]),
      ("PKGVERSION", spec["version"]),
      ("RELOCATE_PATHS", " ".join(spec.get("relocate_paths", []))),
      ("REQUIRES", " ".join(spec["requires"])),
      ("RUNTIME_REQUIRES", " ".join(spec["runtime_requires"])),
      ("FULL_RUNTIME_REQUIRES", " ".join(spec["full_runtime_requires"])),
      ("FULL_BUILD_REQUIRES", " ".join(spec["full_build_requires"])),
      ("FULL_REQUIRES", " ".join(spec["full_requires"])),
      ("BITS_PREFER_SYSTEM_KEY", spec.get("key", "")),
      ("BITS_SCRIPT_DIR", "/bits" if args.docker else bits_dir),
    ]
    if "sources" in spec:
      for idx, src in enumerate(spec["sources"]):
        url, _ = parse_checksum_entry(src)   # strip any ,algo:digest suffix
        buildEnvironment.append(("SOURCE%s" % idx, basename(url)))
      buildEnvironment.append(("SOURCE_COUNT", str(len(spec["sources"]))))
    else:
      buildEnvironment.append(("SOURCE_COUNT", "0"))
    if "patches" in spec:
      for idx, src in enumerate(spec["patches"]):
        patch_name, _ = parse_checksum_entry(src)  # strip any ,algo:digest suffix
        buildEnvironment.append(("PATCH%s" % idx, basename(patch_name)))
      buildEnvironment.append(("PATCH_COUNT", str(len(spec["patches"]))))
    else:
      buildEnvironment.append(("PATCH_COUNT", "0"))
    # Add resolved hooks as environment variables (POST_INSTALL -> POST_INSTALL_HOOKS)
    for hook_name, hook_value in spec.get("hook", {}).items():
      buildEnvironment.append((hook_name + "_HOOKS", hook_value))

    # Add the extra environment as passed from the command line.
    buildEnvironment += [e.partition('=')[::2] for e in args.environment]

    # Add the computed track_env environment
    buildEnvironment += [(key, value) for key, value in spec.get("track_env", {}).items()]

    # -- Pipeline mode: prepare tar/upload commands and write helper scripts ----
    # Requires --makeflow. Compatible with --docker because tar.sh, create_links.sh,
    # and upload_command all run on the HOST after the container exits; they access
    # the build output via args.workDir, which the container already volume-mounts.
    _is_config_pkg = spec["package"].startswith("defaults-")
    _use_pipeline = getattr(args, "pipeline", False) and args.makeflow and not _is_config_pkg
    tar_command = None
    upload_command = None
    if _use_pipeline:
      import stat as _stat
      # Signal build_template.sh to skip tarball creation.
      buildEnvironment.append(("SKIP_TARBALL", "1"))

      # Write tar.sh from the installed template.
      _tar_tpl_path = join(dirname(realpath(__file__)), "tar_template.sh")
      with open(_tar_tpl_path) as _f:
        _tar_tpl = _f.read()
      writeAll(scriptDir + "/tar.sh", _tar_tpl)
      os.chmod(scriptDir + "/tar.sh",
               _stat.S_IRWXU | _stat.S_IRGRP | _stat.S_IXGRP | _stat.S_IROTH | _stat.S_IXOTH)

      # Write create_links.sh (bakes in dependency symlink commands so the
      # shell rule does not need Python's specs dict).
      writeAll(scriptDir + "/create_links.sh",
               _generate_create_links_sh(spec, specs, args))
      os.chmod(scriptDir + "/create_links.sh",
               _stat.S_IRWXU | _stat.S_IRGRP | _stat.S_IXGRP | _stat.S_IROTH | _stat.S_IXOTH)

      # Build the tar command (env vars for tar_template.sh).
      _tar_env = " ".join(
        "{}={}".format(k, quote(v)) for k, v in [
          ("WORK_DIR",               workDir),
          ("PKGNAME",                spec["package"]),
          ("PKGVERSION",             spec["version"]),
          ("PKGREVISION",            spec["revision"]),
          ("PKGHASH",                spec["hash"]),
          ("EFFECTIVE_ARCHITECTURE", effective_arch(spec, args.architecture)),
          ("CACHED_TARBALL",         cachedTarball),
        ]
      )
      tar_command = "env {} {} -e -x {}/tar.sh 2>&1".format(_tar_env, BASH, quote(scriptDir))

      # Build the upload command (wrapped with the env vars that upload_cmd.py
      # / the inline s3cmd script read from the environment).
      _raw_upload = syncHelper.upload_shell_command(spec)
      if _raw_upload:
        _upload_env = " ".join(
          "{}={}".format(k, quote(v)) for k, v in [
            ("PKGNAME",                spec["package"]),
            ("PKGVERSION",            spec["version"]),
            ("PKGREVISION",           spec["revision"]),
            ("PKGHASH",               spec["hash"]),
            ("EFFECTIVE_ARCHITECTURE", effective_arch(spec, args.architecture)),
            ("BUILD_ARCH",            args.architecture),
          ]
        )
        upload_command = "env {} {} 2>&1".format(_upload_env, _raw_upload)

    # In case the --docker options is passed, we setup a docker container which
    # will perform the actual build. Otherwise build as usual using bash.
    if args.docker:
      _docker_platform = getattr(args, "dockerPlatform", None)
      build_command = (
        "docker run --rm --entrypoint= --user $(id -u):$(id -g) "
        "{platformArg}"
        "-v {workdir}:{container_workDir} -v{configDir}:/pkgdist.bits:ro "
        "-v {scriptDir}/build.sh:/build.sh:ro "
        "-v {bits_dir}:/bits "
        "{mirrorVolume} {develVolumes} {additionalEnv} {additionalVolumes} "
        "-e WORK_DIR_OVERRIDE={container_workDir} -e BITS_CONFIG_DIR_OVERRIDE=/pkgdist.bits {extraArgs} {image} bash -ex /build.sh"
      ).format(
        platformArg="--platform %s " % quote(_docker_platform) if _docker_platform else "",
        image=quote(args.dockerImage),
        workdir=quote(abspath(args.workDir)),
        container_workDir=container_workDir,
        bits_dir=bits_dir,
        configDir=quote(abspath(args.configDir)),
        scriptDir=quote(scriptDir),
        extraArgs=" ".join(map(quote, args.docker_extra_args)),
        additionalEnv=" ".join(
          f"-e {var}={quote(value)}" for var, value in buildEnvironment),
        # Used e.g. by O2DPG-sim-tests to find the O2DPG repository.
        develVolumes=" ".join(
          '-v "$PWD/$(readlink {pkg} || echo {pkg})":/{pkg}:rw'.format(pkg=quote(spec["package"]))
          for spec in specs.values() if spec["is_devel_pkg"]),
        additionalVolumes=" ".join(
          "-v %s" % quote(volume) for volume in args.volumes),
        mirrorVolume=("-v %s:/mirror" % quote(dirname(spec["reference"]))
                      if "reference" in spec else ""),
      )
    else:
      buildEnvironment = ([key, (val if isinstance(val, str) else "_".join(val))] for key, val in buildEnvironment)
      env_vars = " ".join(["{}={}".format(key, quote(val)) for key, val in buildEnvironment])
      build_command =  "env {} {} -e -x {}/build.sh 2>&1".format(env_vars, BASH, quote(scriptDir))

    # Warn when cross-compiling (QEMU) with sandboxing enabled: nested podman
    # inside a QEMU-emulated container requires seccomp=unconfined on the outer
    # docker run and may still fail on kernels without unprivileged userns.
    # Recommend --sandbox=off for cross-compilation builds.
    if getattr(args, "dockerPlatform", None) and getattr(args, "sandbox", "off") != "off":
      from bits_helpers.log import warning as _warn
      _warn(
          "Cross-compilation (--docker-platform %s) with --sandbox=%s: "
          "nested QEMU + podman may fail unless the outer container is run with "
          "--security-opt seccomp=unconfined.  Pass --sandbox=off if builds fail.",
          args.dockerPlatform, args.sandbox,
      )

    # Apply recipe sandbox (podman / sandbox-exec) if configured.
    # sandbox=auto selects the best available mode; sandbox=off is a no-op.
    # Per-recipe: sandbox_network: on (default) blocks outgoing network;
    #             sandbox_network: off allows it.
    build_command = wrap_build_command(
        build_command,
        spec,
        args,
        workdir=abspath(args.workDir),
        docker_active=bool(getattr(args, "docker", False)),
        container_workdir=container_workDir if getattr(args, "docker", False) else None,
        docker_image=getattr(args, "dockerImage", None),
    )

    # defaults-* packages are pure build-time configuration with no source to
    # compile. In Makeflow mode, run them synchronously in the preparation phase
    # instead of emitting Makeflow rules. This removes them from the DAG critical
    # path and allows dependent packages to start without waiting for a Makeflow slot.
    if args.makeflow and _is_config_pkg:
      runBuildCommand(scheduler, p, specs, args, build_command,
                      cachedTarball, scriptDir, workDir, syncHelper)
      continue  # skip buildTargets.append and buildList.append

    buildTargets.append(p)
    if not args.makeflow:
      if args.builders == 1:
        runBuildCommand(scheduler, p, specs, args, build_command, cachedTarball, scriptDir, workDir, syncHelper)
      else:
        build_deps = ["build:%s" % d for d in specs[p]["full_requires"] if d in buildTargets]
        # When the package must be built from source, register its checkout as a
        # scheduler "download" task (capped by --parallel-downloads) and make the
        # build wait on it.  The scheduler then compiles ready packages while
        # other packages' sources are still downloading, removing the up-front
        # serial download loop.  Packages restored from a cached tarball need no
        # source download, so they get no fetch task.
        if not cachedTarball:
          fetch_id = "fetch:%s" % p
          scheduler.parallel(fetch_id, [], "download", _doCheckout, spec, workDir,
                             args.referenceSources, args.docker,
                             _download_time_mode(effective_checksum_mode), syncHelper,
                             getattr(args, "parallelSources", 1), raw_architecture)
          build_deps = build_deps + [fetch_id]
        scheduler.parallel("build:%s" % p, build_deps, "build", runBuildCommand, scheduler, p, specs, args, build_command,cachedTarball, scriptDir, workDir, syncHelper)
    else:
      breq = " ".join([str(element) + ".build" for element in spec["full_requires"] if element in buildTargets])
      # In pipeline mode, append create_links.sh to the .build command so that
      # dist symlinks are created inside the same rule (before .tar/.upload run).
      _build_cmd = build_command
      if _use_pipeline:
        _build_cmd = "{} && {} -e -x {}/create_links.sh".format(
            build_command, BASH, quote(scriptDir))

      # --- Makeflow checkout rule -----------------------------------------
      # When the package needs to be built from source (no cached tarball),
      # generate a spec_checkout.json + checkout.sh in scriptDir and record
      # the command so the Jinja template can emit a parallel .checkout rule.
      # This moves all git clones / archive downloads out of the sequential
      # Python preparation phase and into independent Makeflow tasks.
      checkout_cmd = ""
      if not cachedTarball:
        _scm_type = "sapling" if isinstance(spec.get("scm"), Sapling) else "git"
        _checkout_spec = {
          "scm_type":         _scm_type,
          "package":          spec["package"],
          "version":          spec["version"],
          "commit_hash":      spec.get("commit_hash", ""),
          "tag":              spec.get("tag", spec["version"]),
          "pkgdir":           spec.get("pkgdir", ""),
          "source":           spec.get("source", ""),
          "is_devel_pkg":     spec.get("is_devel_pkg", False),
          "reference":        spec.get("reference", ""),
          "write_repo":       spec.get("write_repo", ""),
          "patches":          spec.get("patches", []),
          "auto_patch":       spec.get("auto_patch", True),
          "sources":          spec.get("sources", []),
          "source_checksums": spec.get("source_checksums") or {},
          "patch_checksums":  spec.get("patch_checksums") or {},
        }
        _checkout_json = join(scriptDir, "spec_checkout.json")
        with open(_checkout_json, "w") as _fh:
          json.dump(_checkout_spec, _fh)
        _ref = quote(args.referenceSources) if args.referenceSources else "''"
        _enforce = quote(_download_time_mode(effective_checksum_mode))
        _psrc = str(getattr(args, "parallelSources", 1))
        checkout_cmd = (
          "PYTHONPATH={bits_dir} {py} -m bits_helpers.checkout_runner"
          " --spec-json {json}"
          " --work-dir {wd}"
          " --reference-sources {ref}"
          " --enforce-mode {enforce}"
          " --parallel-sources {psrc}"
        ).format(
          bits_dir=quote(bits_dir),
          py=quote(sys.executable),
          json=quote(_checkout_json),
          wd=quote(workDir),
          ref=_ref,
          enforce=_enforce,
          psrc=_psrc,
        )

      buildList.append((p, _build_cmd, tar_command, upload_command, cachedTarball, breq, checkout_cmd))

  if (not args.makeflow) and (args.builders > 1) and buildTargets:
    try:
      scheduler.run()
    finally:
      # Always stop the straggler-renice watchdog, even if run() raised.
      if getattr(scheduler, "renice_watchdog", None) is not None:
        scheduler.renice_watchdog.stop()
    # Refresh the self-tuning resource-stats file from this run's monitor traces
    # so the next --builders invocation can schedule with up-to-date estimates (P3).
    if args.resourceMonitoring and monitoredDirs:
      try:
        from bits_helpers.build_stats import aggregate_and_write
        aggregate_and_write(workDir, monitoredDirs)
      except Exception as exc:  # pylint: disable=broad-except
        warning("Could not update build resource stats: %s", exc)
    for (action, error) in scheduler.errors.items():
      info("* The action \"{}\" was not completed successfully because {}".format(action, error))
    # Write a concise failure summary plus a combined full error log, and tell
    # the user where to find them and the individual per-package logs.
    _summary_path, _full_path = write_failure_summary(workDir, scheduler)
    if _summary_path or _full_path:
      info("=" * 70)
      info("Build finished with errors. Where to look:")
      if _summary_path:
        info("  Summary (start here):   %s", _summary_path)
      if _full_path:
        info("  Full error log:         %s", _full_path)
      info("  Per-package build logs: %s/BUILD/<package>-latest/log", workDir)
      info("=" * 70)
    if scheduler.brokenJobs:
      dieOnError(True, "Please fix the above errors.")
  elif args.makeflow and buildTargets:
    mFlow = "makeflow"
    mfDir = join(workDir, "BUILD", spec["hash"], "makeflow")
    mfFile = mfDir + "/Makeflow"
    makedirs(mfDir, exist_ok=True)
    _mf_max_local = getattr(args, "makeflowJobs", 4)
    _mf_local_flag = "--max-local {}".format(_mf_max_local) if _mf_max_local > 0 else ""
    # FIX: quote(mfDir) prevents shell injection when workDir contains spaces,
    # semicolons, or other shell metacharacters (shell=True is still needed for
    # the cd+semicolon compound command pattern).
    mfCmd = "(cd {dir}; {mf} --clean; {mf} {local})".format(
        dir=quote(mfDir), mf=mFlow, local=_mf_local_flag)
    makedirs(mfDir, exist_ok=True)
    jnj = ""
    try:
      fp = open(dirname(realpath(__file__))+'/Makeflow.jnj')
      jnj = fp.read()
      fp.close()
    except:
      from pkg_resources import resource_string
      jnj = resource_string("bits_helpers", 'Makeflow.jnj')
    with open(mfFile, 'w') as mf:
      mf.write (SandboxedEnvironment(autoescape=False)
              .from_string(jnj)
              .render(specs=specs, args=args, ToDo=buildList)
              )
    for (p, build_command, tar_command, upload_command, cachedTarball, breq, checkout_cmd) in buildList:
      spec = specs[p]
      print (
        ("Unpacking %s@%s" if cachedTarball else
        "Compiling %s@%s (use --debug for full output)") %
        (spec["package"],
        args.develPrefix if "develPrefix" in args and spec["is_devel_pkg"] else spec["version"])
      )
    child = subprocess.run(mfCmd, shell=True, capture_output=True, text=True)
    err = child.returncode
    
    buildErrMsg = ""
    if(err):
      print(child.stdout)
      
      # Color codes for error message (if TTY)
      bold = "\033[1m" if sys.stderr.isatty() else ""
      red = "\033[31m" if sys.stderr.isatty() else ""
      reset = "\033[0m" if sys.stderr.isatty() else ""
      
      # Determine paths
      log_path = f"{mfDir}/log"
      
      # Use relative paths if we're inside the work directory
      try:
        from os.path import relpath
        log_path = relpath(log_path, os.getcwd())
        mfDir_rel = relpath(mfDir, os.getcwd())
      except (ValueError, OSError):
        mfDir_rel = mfDir  # Keep absolute paths if relpath fails
      
      # Build the error message
      buildErrMsg = f"{red}{bold}MAKEFLOW BUILD FAILED{reset}\n"
      buildErrMsg += "=" * 70 + "\n\n"
      
      buildErrMsg += f"{bold}Makeflow Command:{reset}\n"
      buildErrMsg += f"  {mfCmd}\n\n"
      
      buildErrMsg += f"{bold}Log File:{reset}\n"
      buildErrMsg += f"  {log_path}\n\n"
      
      buildErrMsg += f"{bold}Makeflow Directory:{reset}\n"
      buildErrMsg += f"  {mfDir_rel}\n"
      
      # Gather build info for the error message
      try:
        detected_arch = detectArch()

        # Only show safe arguments (no tokens/secrets) in CLI-usable format
        safe_args = {
          "pkgname", "defaults", "architecture", "forceUnknownArch",
          "develPrefix", "jobs", "noSystem", "noDevel", "forceTracked", "plugin",
          "disable", "annotate", "onlyDeps", "docker", "makeflow"
        }
        
        cli_args = []
        for k, v in vars(args).items():
          if not v or k not in safe_args:
            continue
          
          # Format based on type for CLI usage
          if isinstance(v, bool):
            if v:  # Only show if True
              cli_args.append(f"--{k}")
          elif isinstance(v, list):
            if v:  # Only show non-empty lists
              seen = set()
              for item in v:
                if item not in seen:
                  seen.add(item)
                  cli_args.append(f"--{k}={quote(str(item))}")
          else:
            # Quote if needed
            cli_args.append(f"--{k}={quote(str(v))}")
        
        args_str = " ".join(cli_args)

        buildErrMsg += f"\n{bold}Environment:{reset}\n"
        buildErrMsg += f"  OS: {detected_arch}\n"
        buildErrMsg += f"  bits: {__version__ or 'unknown'} (bits@{os.environ['BITS_DIST_HASH'][:10]})\n"

        if detected_arch.startswith("osx"):
          xcode_info = getstatusoutput("xcodebuild -version")[1]
          # Combine XCode version lines into one
          xcode_lines = xcode_info.strip().split('\n')
          if len(xcode_lines) >= 2:
            xcode_str = f"{xcode_lines[0]} ({xcode_lines[1]})"
          else:
            xcode_str = xcode_lines[0] if xcode_lines else "Unknown"
          buildErrMsg += f"  XCode: {xcode_str}\n"

        buildErrMsg += f"  Arguments: {args_str}\n"

      except Exception as exc:
        warning("Failed to gather build info", exc_info=exc)
      
      # Add Next Steps section
      buildErrMsg += f"\n{bold}Next Steps:{reset}\n"
      buildErrMsg += f"  • View makeflow log:       cat {log_path}\n"
      buildErrMsg += f"  • View makeflow file:      cat {mfDir_rel}/Makeflow\n"
      if not args.debug:
        buildErrMsg += f"  • Rebuild with debug:      bitsBuild build {' '.join(args.pkgname)} --debug --makeflow\n"
      buildErrMsg += f"  • Please upload the full log to CERNBox/Dropbox if you intend to request support.\n"
      
    else:
      debug(child.stdout)
    dieOnError(err, buildErrMsg.strip())
    for (p, _, _, _, _, _, _) in buildList:
      doFinalSync(specs[p], specs, args, syncHelper)

  # ── Post-build checksum phase ──────────────────────────────────────────────
  # Runs after all packages have been built (or confirmed up-to-date) so that
  # output is consolidated and so that already-cached packages are covered.
  # warn/enforce remain in checkout_sources (pre-build security gate);
  # only print/write are handled here.
  #
  # The mode is resolved from the global config (CLI flags + defaults profile),
  # not from the per-spec effective_checksum_mode of the last loop iteration.
  _global_mode = checksum_enforcement_mode({}, args, defaultsMeta)
  _do_print = (_global_mode == "print")
  _do_write = write_checksums_enabled(args, defaultsMeta)
  if (_do_print or _do_write) and specs_for_checksum_phase:
    _run_post_build_checksum_phase(specs_for_checksum_phase, workDir,
                                   do_print=_do_print, do_write=_do_write)

  if not args.onlyDeps:
      # Resolve the main package's install root (sw/<arch>/<pkg>/<ver-rev>) so
      # the success summary points at the package directory, not just the arch
      # root -- mirroring the Install Root shown on failure.
      _install_root_line = ""
      _main_spec = specs.get(mainPackage)
      if _main_spec is not None:
        try:
          _install_root_line = "\nThe %s install root is:\n\n  %s\n" % (
            mainPackage,
            abspath(_pkg_install_path(args.workDir,
                                      effective_arch(_main_spec, args.architecture),
                                      _main_spec)))
        except Exception:  # pylint: disable=broad-except
          _install_root_line = ""
      banner(f"Build of {mainPackage} successfully completed on `{socket.gethostname()}'.\n"
             "Your software installation is at:"
             f"\n\n  {abspath(join(args.workDir, args.architecture))}\n"
             f"{_install_root_line}\n"
             "You can use this package by loading the environment:"
             f"\n\n  bits enter {mainPackage}/latest-{mainBuildFamily}",
             )
  else:
      banner("Successfully built dependencies for package %s on `%s'.\n",
             mainPackage, socket.gethostname()
            )
  for spec in specs.values():
    if spec["is_devel_pkg"]:
      banner("Build directory for devel package %s:\n%s/BUILD/%s-latest%s/%s",
             spec["package"], abspath(buildWorkDir), spec["package"],
             ("-" + args.develPrefix) if "develPrefix" in args else "",
             spec["package"])
  if untrackedFilesDirectories:
    banner("Untracked files in the following directories resulted in a rebuild of "
           "the associated package and its dependencies:\n%s\n\nPlease commit or remove them to avoid useless rebuilds.", "\n".join(untrackedFilesDirectories))

  # Finalise the build manifest.
  if getattr(args, "manifest", None) is not None:
    args.manifest.complete()
    banner("Build manifest written to:\n  %s", args.manifest.path)

  debug("Everything done")

