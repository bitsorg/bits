import codecs
import errno
import os
import os.path
import shutil
import subprocess
import tempfile
import zipfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed


from bits_helpers.log import dieOnError, debug, error, warning
from bits_helpers.download import download
from bits_helpers.utilities import call_ignoring_oserrors, symlink, short_commit_hash, asList
from bits_helpers.checksum import parse_entry, check_file as check_file_checksum

FETCH_LOG_NAME = "fetch-log.txt"


def cleanup_git_log(referenceSources):
  """Remove a stale fetch-log.txt.

  You must call this function before running updateReferenceRepoSpec or
  updateReferenceRepo any number of times. This is not done automatically, so
  that running those functions in parallel works properly.
  """
  try:
    os.unlink(os.path.join(referenceSources, FETCH_LOG_NAME))
  except OSError as exc:
    # Ignore errors when deleting a nonexistent file.
    dieOnError(exc.errno != errno.ENOENT,
               "Could not delete stale git log: %s" % exc)


def logged_scm(scm, package, referenceSources,
               command, directory, prompt, logOutput=True):
  """Run an SCM command, but produce an output file if it fails.

  This is useful in CI, so that we can pick up SCM failures and show them in
  the final produced log. For this reason, the file we write in this function
  must not contain any secrets. We only output the SCM command we ran, its exit
  code, and the package name, so this should be safe.
  """
  debug("%s %s for repository for %s...", scm.name, command[0], package)
  err, output = scm.exec(command, directory=directory, check=False, prompt=prompt)
  if logOutput:
    debug(output)
  if err:
    try:
      with codecs.open(os.path.join(referenceSources, FETCH_LOG_NAME),
                       "a", encoding="utf-8", errors="replace") as logf:
        logf.write("%s command for package %r failed.\n"
                   "Command: %s %s\nIn directory: %s\nExit code: %d\n" %
                   (scm.name, package, scm.name.lower(), " ".join(command), directory, err))
    except OSError as exc:
      error("Could not write error log from SCM command:", exc_info=exc)
  dieOnError(err, "Error during %s %s for reference repo for %s." %
             (scm.name.lower(), command[0], package))
  debug("Done %s %s for repository for %s", scm.name.lower(), command[0], package)
  return output


def updateReferenceRepoSpec(referenceSources, p, spec,
                            fetch=True, usePartialClone=True, allowGitPrompt=True):
  """
  Update source reference area whenever possible, and set the spec's "reference"
  if available for reading.

  @referenceSources : a string containing the path to the sources to be updated
  @p                : the name of the package to be updated
  @spec             : the spec of the package to be updated (an OrderedDict)
  @fetch            : whether to fetch updates: if False, only clone if not found
  """
  spec["reference"] = updateReferenceRepo(referenceSources, p, spec, fetch,
                                          usePartialClone, allowGitPrompt)
  if not spec["reference"]:
    del spec["reference"]


def updateReferenceRepo(referenceSources, p, spec,
                        fetch=True, usePartialClone=True, allowGitPrompt=True):
  """
  Update source reference area, if possible.
  If the area is already there and cannot be written, assume it maintained
  by someone else.

  If the area can be created, clone a bare repository with the sources.

  Returns the reference repository's local path if available, otherwise None.
  Throws a fatal error in case repository cannot be updated even if it appears
  to be writeable.

  @referenceSources : a string containing the path to the sources to be updated
  @p                : the name of the package to be updated
  @spec             : the spec of the package to be updated (an OrderedDict)
  @fetch            : whether to fetch updates: if False, only clone if not found
  """
  assert isinstance(spec, OrderedDict)
  if spec["is_devel_pkg"] or "source" not in spec:
    return None

  scm = spec["scm"]

  debug("Updating references.")
  referenceRepo = os.path.join(os.path.abspath(referenceSources), p.lower())

  call_ignoring_oserrors(os.makedirs, os.path.abspath(referenceSources), exist_ok=True)

  if not is_writeable(referenceSources):
    if os.path.exists(referenceRepo):
      debug("Using %s as reference for %s", referenceRepo, p)
      return referenceRepo  # reference is read-only
    else:
      debug("Cannot create reference for %s in %s", p, referenceSources)
      return None  # no reference can be found and created (not fatal)

  if not os.path.exists(referenceRepo):
    cmd = scm.cloneReferenceCmd(spec["source"], referenceRepo, usePartialClone)
    logged_scm(scm, p, referenceSources, cmd, ".", allowGitPrompt)
  elif fetch:
    ref_match_rule = asList(spec.get("ref_match_rule", ["+refs/tags/*:refs/tags/*", "+refs/heads/*:refs/heads/*"]))
    cmd = scm.fetchCmd(spec["source"], *ref_match_rule)
    logged_scm(scm, p, referenceSources, cmd, referenceRepo, allowGitPrompt)

  return referenceRepo  # reference is read-write


def is_writeable(dirpath):
  try:
    with tempfile.NamedTemporaryFile(dir=dirpath):
      return True
  except Exception:
    return False


def _verify_commit_pin(scm, spec, source_dir: str, enforce_mode: str) -> None:
  """Check that the checked-out HEAD matches the pinned commit SHA, if any.

  The pin is stored in ``spec["pin_commit"]`` and comes from the recipe
  repository's ``checksums/<pkgname>.checksum`` file (``tag:`` field).

  Behaviour follows the standard enforcement modes:
  - ``"off"``     — no check performed (pin is stored but ignored).
  - ``"warn"``    — mismatch emits a warning; build continues.
  - ``"enforce"`` — mismatch aborts the build.
  - ``"print"``   — actual commit SHA is printed; no verification.
  """
  pin = spec.get("pin_commit")
  package = spec.get("package", "?")

  if enforce_mode == "print":
    try:
      actual = scm.checkedOutCommitName(source_dir).strip()
      print("  %s (git): commit:%s" % (package, actual))
    except Exception:  # noqa: BLE001
      pass
    return

  if not pin or enforce_mode == "off":
    return

  try:
    actual = scm.checkedOutCommitName(source_dir).strip().lower()
  except Exception as exc:  # noqa: BLE001
    warning("Could not read HEAD for %s: %s", package, exc)
    return

  if actual == pin.lower():
    debug("Commit pin OK for %s: %s", package, actual[:10])
    return

  msg = ("Commit pin mismatch for %s: expected %s, got %s"
         % (package, pin[:10], actual[:10]))
  if enforce_mode == "enforce":
    dieOnError(True, msg)
  else:
    warning("%s", msg)


_TAR_EXTENSIONS = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar.zst")
_ZIP_EXTENSIONS = (".zip",)


def _extract_zip_strip(archive_path, dest_dir):
  """Extract a zip archive into dest_dir, stripping the first path component.

  This mirrors the behaviour of ``tar --strip-components=1``: every member
  whose path starts with a top-level directory has that directory removed
  before extraction, so the archive contents land directly in dest_dir.
  Members that sit at the archive root (no ``/`` in their name) are skipped,
  since they are the top-level directory entries themselves.
  """
  with zipfile.ZipFile(archive_path) as zf:
    for member in zf.infolist():
      parts = member.filename.split("/", 1)
      if len(parts) < 2 or not parts[1]:
        continue
      member.filename = parts[1]
      zf.extract(member, dest_dir)


def _apply_patches(spec, source_dir):
  """Apply patches listed in spec['patches'] to source_dir using patch -p1.

  Patch files are already present in source_dir (placed there by
  checkout_sources before source extraction / git checkout).  This function
  runs ``patch -p1`` for each one in declaration order.

  A ``.bits_patched`` sentinel file is written after a successful run so that
  repeated invocations (e.g. a resumed incremental build) skip re-application
  and do not fail with "already applied" errors.
  """
  if not spec.get("patches"):
    return

  sentinel = os.path.join(source_dir, ".bits_patched")
  if os.path.exists(sentinel):
    return

  _patch_checksums = spec.get("patch_checksums") or {}
  for patch_entry in spec["patches"]:
    patch_name, _ = parse_entry(patch_entry)
    patch_path = os.path.join(source_dir, patch_name)
    debug("Applying patch %s in %s", patch_name, source_dir)
    subprocess.check_call(["patch", "-p1", "--input", patch_path], cwd=source_dir)

  open(sentinel, "w").close()


def _extract_source_archives(source_dir):
  """Extract any source archives found directly inside source_dir.

  After ``download()`` places a release tarball (e.g. ``gsl-2.8.tar.gz``) in
  ``source_dir``, the build recipe expects an *unpacked* source tree there —
  not a bare archive file.  This function scans source_dir for known archive
  types and extracts each one, stripping the top-level directory that release
  tarballs almost universally contain (``--strip-components=1`` for tar,
  equivalent logic for zip).

  A ``.bits_extracted`` sentinel file is written after a successful run so
  that repeated invocations (e.g. a resumed build) skip re-extraction and do
  not clobber a partially-built tree.
  """
  sentinel = os.path.join(source_dir, ".bits_extracted")
  if os.path.exists(sentinel):
    return

  extracted = False
  for entry in sorted(os.listdir(source_dir)):
    filepath = os.path.join(source_dir, entry)
    if not os.path.isfile(filepath):
      continue
    lower = entry.lower()
    if any(lower.endswith(ext) for ext in _TAR_EXTENSIONS):
      debug("Extracting %s into %s", entry, source_dir)
      subprocess.check_call(
        ["tar", "xf", filepath, "--strip-components=1", "-C", source_dir]
      )
      extracted = True
    elif any(lower.endswith(ext) for ext in _ZIP_EXTENSIONS):
      debug("Extracting %s into %s", entry, source_dir)
      _extract_zip_strip(filepath, source_dir)
      extracted = True

  if extracted:
    open(sentinel, "w").close()


def checkout_sources(spec, work_dir, reference_sources, containerised_build,
                     enforce_mode="off", sync_helper=None, parallel_sources=1):
  """Check out sources to be compiled, potentially from a given reference.

  ``sync_helper`` is an optional sync-backend instance (from
  ``bits_helpers.sync``).  When provided it is forwarded to every
  ``download()`` call so that source archives are fetched from / archived
  to the remote store as described in ``bits_helpers.download.download``.

  ``parallel_sources`` controls how many URLs in the ``sources:`` list are
  downloaded concurrently.  The default (1) preserves the original sequential
  behaviour.  Values >1 use a ``ThreadPoolExecutor`` and raise the first
  exception encountered, preserving the same failure semantics.
  """
  scm = spec["scm"]

  def scm_exec(command, directory=".", check=True):
    """Run the given SCM command, simulating a shell exit code."""
    try:
      logged_scm(scm, spec["package"], reference_sources, command, directory, prompt=False)
    except SystemExit as exc:
      if check:
        raise
      return exc.code
    return 0

  source_parent_dir = os.path.join(work_dir, "SOURCES", spec["package"], spec["version"])
  # The build script expects SOURCEDIR to be named after the shortened commit
  # hash, not the full one.
  source_dir = os.path.join(source_parent_dir, short_commit_hash(spec))
  os.makedirs(source_parent_dir, exist_ok=True)

  if spec["commit_hash"] != spec["tag"]:
    symlink(spec["commit_hash"], os.path.join(source_parent_dir, spec["tag"].replace("/", "_")))

  # External checksum store takes precedence over inline comma-suffix values.
  _source_checksums = spec.get("source_checksums") or {}
  _patch_checksums = spec.get("patch_checksums") or {}

  if spec.get("patches"):
    os.makedirs(source_dir, exist_ok=True)
    for patch_entry in spec["patches"]:
      patch_name, inline_checksum = parse_entry(patch_entry)
      patch_checksum = _patch_checksums.get(patch_name) or inline_checksum
      dst = os.path.join(source_dir, patch_name)
      shutil.copyfile(os.path.join(spec["pkgdir"], 'patches', patch_name), dst)
      check_file_checksum(dst, patch_name, patch_checksum, enforce_mode)
  if spec.get("sources"):
    def _download_one(s):
      url, inline_checksum = parse_entry(s)
      src_checksum = _source_checksums.get(url) or inline_checksum
      download(url, source_dir, work_dir, checksum=src_checksum,
               enforce_mode=enforce_mode, sync_helper=sync_helper)

    if parallel_sources <= 1 or len(spec["sources"]) <= 1:
      # Sequential path: preserves original behaviour for the common case.
      for s in spec["sources"]:
        _download_one(s)
    else:
      # Parallel path: submit all source downloads and re-raise the first error.
      with ThreadPoolExecutor(max_workers=parallel_sources) as pool:
        futures = {pool.submit(_download_one, s): s for s in spec["sources"]}
        first_exc = None
        for fut in as_completed(futures):
          exc = fut.exception()
          if exc is not None and first_exc is None:
            first_exc = exc
        if first_exc is not None:
          raise first_exc
    # Unpack any downloaded archives so the build script sees an unpacked
    # source tree at $SOURCEDIR rather than a bare archive file.
    _extract_source_archives(source_dir)
    _apply_patches(spec, source_dir)
  elif not spec.get("source"):
    # There are no sources (neither tarball URLs nor a git repo), so just
    # create an empty SOURCEDIR.  Also handles the Makeflow serialisation path
    # where source is always present in the JSON but may be an empty string.
    os.makedirs(source_dir, exist_ok=True)
  elif spec["is_devel_pkg"]:
    shutil.rmtree(source_dir, ignore_errors=True)
    # In a container, we mount development packages' source dirs in /.
    # Outside a container, we have access to the source dir directly.
    symlink("/" + os.path.basename(spec["source"])
            if containerised_build else spec["source"],
            source_dir)
  elif os.path.isdir(source_dir):
    # Sources are a relative path or URL and the local repo already exists, so
    # checkout the right commit there.
    err = scm_exec(scm.checkoutCmd(spec["tag"]), source_dir, check=False)
    if err:
      # If we can't find the tag, it might be new. Fetch tags and try again.
      tag_ref = "refs/tags/{0}:refs/tags/{0}".format(spec["tag"])
      scm_exec(scm.fetchCmd(spec["source"], tag_ref), source_dir)
      scm_exec(scm.checkoutCmd(spec["tag"]), source_dir)
    _verify_commit_pin(scm, spec, source_dir, enforce_mode)
    _apply_patches(spec, source_dir)
  else:
    # Sources are a relative path or URL and don't exist locally yet, so clone
    # and checkout the git repo from there.
    shutil.rmtree(source_dir, ignore_errors=True)
    scm_exec(scm.cloneSourceCmd(spec["source"], source_dir, spec.get("reference"),
                                usePartialClone=True))
    scm_exec(scm.setWriteUrlCmd(spec.get("write_repo", spec["source"])), source_dir)
    scm_exec(scm.checkoutCmd(spec["tag"]), source_dir)
    _verify_commit_pin(scm, spec, source_dir, enforce_mode)
    _apply_patches(spec, source_dir)
