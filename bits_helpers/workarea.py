# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

import codecs
import errno
import os
import os.path
import re
import shutil
import subprocess
import tarfile as _tarfile_mod
import tempfile
import zipfile
from glob import glob
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed


from bits_helpers.log import dieOnError, debug, error, warning, ProgressPrint
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
                   "Command: %s %s\nIn directory: %s\nExit code: %d\n"
                   "Output:\n%s\n" %
                   (scm.name, package, scm.name.lower(), " ".join(command),
                    directory, err, (output or "").strip() or "(no output)"))
    except OSError as exc:
      error("Could not write error log from SCM command:", exc_info=exc)
  # Surface git's own message inline so the failure is diagnosable without
  # re-running at --debug (the full text is in fetch-log.txt). The captured
  # output already includes stderr (getstatusoutput merges it).
  _excerpt = " ".join((output or "").split())
  if len(_excerpt) > 300:
    _excerpt = "…" + _excerpt[-300:]
  dieOnError(err, "Error during %s %s for reference repo for %s.%s" %
             (scm.name.lower(), command[0], package,
              ("\n  git: " + _excerpt) if _excerpt else ""))
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

def _split_arch_prefix(entry):
  """Split ``(arch_pattern)url`` using balanced-parenthesis counting.

  Returns ``(arch_pattern, url)`` when *entry* starts with ``(`` and the
  matching closing ``)`` is found, otherwise returns ``None``.  This handles
  patterns that themselves contain parentheses, such as regex lookaheads
  like ``(?!osx)``, which the simpler ``[^)]*`` regex approach cannot.
  """
  if not entry.startswith("("):
    return None
  depth = 0
  for i, c in enumerate(entry):
    if c == "(":
      depth += 1
    elif c == ")":
      depth -= 1
      if depth == 0:
        return entry[1:i], entry[i + 1:].lstrip()
  return None


def _resolve_source_entry(entry, architecture):
  """Resolve a source entry, returning ``(resolved_entry, include)`` pair.

  Two optional syntaxes are supported, mirroring the ``name:arch_regex``
  convention already used in ``requires`` / ``build_requires``:

  ``(arch_pattern)url[,checksum]``
      Include this source only when *architecture* matches *arch_pattern*.
      The pattern is tried as a regex first (anchored at the start, so
      ``(?!osx)`` excludes macOS and ``osx.*`` includes only macOS); if it
      is not valid regex it is treated as a fnmatch glob.  Patterns may
      contain nested parentheses (e.g. regex lookaheads like ``(?!osx)``).
      The resolved entry is the ``url[,checksum]`` part with the prefix
      stripped.

  ``$(bash_expression)``
      Evaluate *bash_expression* in a bash subshell with ``ARCHITECTURE``
      set.  The expression's stdout (stripped) is used as the URL.  Useful
      when the URL must be constructed from the architecture string itself
      (e.g. for arch-specific pre-built binary tarballs).
      The entry always matches; skip the arch-filter prefix to suppress the
      source on unsupported architectures.

  Any other entry is returned unchanged with ``include=True``.
  """
  # --- Architecture-conditional prefix: (arch_glob_or_regex)url ---
  split = _split_arch_prefix(entry)
  if split is not None:
    import fnmatch
    arch_pat, rest = split
    # Patterns may be either POSIX-extended regexes (e.g. "(?!osx)",
    # "slc[0-9].*") or simple glob wildcards (e.g. "*x86-64*linux*").
    # Try regex first; if the pattern is not valid regex, treat it as a
    # glob pattern via fnmatch so recipe authors can use either style.
    try:
      include = bool(re.match(arch_pat, architecture))
    except re.PatternError:
      include = fnmatch.fnmatch(architecture, arch_pat)
    return rest, include

  # --- Inline bash evaluation: $(bash_expr) ---
  if entry.startswith("$(") and entry.endswith(")"):
    expr = entry[2:-1]
    env = dict(os.environ)
    env["ARCHITECTURE"] = architecture
    try:
      result = subprocess.check_output(
        ["bash", "-c", "echo " + expr], env=env, text=True
      ).strip()
    except subprocess.CalledProcessError as exc:
      raise OSError(
        "Failed to evaluate source expression %r: %s" % (entry, exc)
      )
    return result, True

  return entry, True


def _archive_prefix_depth(archive_path):
  """Return the number of leading path components shared by all entries.

  Standard tarballs have one top-level directory (depth 1).  Occasionally a
  tarball embeds a two-level prefix such as ``package/version/file`` (depth 2)
  — the PHOTOS 215.4 tarball (``photos/215.4/…``) is one example.  Extracting
  such an archive with ``--strip-components=1`` leaves the inner directory
  (``215.4/``) in place, so subsequent ``patch -p1`` cannot find files it
  expects at the source root.

  This function inspects the archive member list and returns the count of
  leading path components that are *identical across every member* so that
  callers can pass the exact ``--strip-components`` value needed to land the
  source files directly in the destination directory.

  Returns 1 on any error or when the archive appears to be flat (no common
  prefix), so existing behaviour is preserved for the common case.
  """
  lower = archive_path.lower()
  try:
    if any(lower.endswith(ext) for ext in _TAR_EXTENSIONS):
      # Use tarfile module so we can filter by member type (files only,
      # not directories) — tar -tf output does not reliably include
      # trailing slashes on directory entries.
      with _tarfile_mod.open(archive_path) as tf:
        file_paths = [m.name for m in tf.getmembers() if m.isfile()]
    elif lower.endswith(".zip"):
      with zipfile.ZipFile(archive_path) as zf:
        file_paths = [m.filename for m in zf.infolist()
                      if not m.filename.endswith("/")]
    else:
      return 1

    if not file_paths:
      return 1

    # Do NOT normalise away leading "./" — tar treats "." as a real path
    # component when counting --strip-components, so "./pkg-1.0/file" has
    # depth 2 (strips "." then "pkg-1.0"), while "pkg-1.0/file" has depth 1.

    split_paths = [p.split("/") for p in file_paths]
    depth = 0
    # Stop one component before the end: the last component is the filename
    # and must never be counted as a common prefix level.  Without this guard,
    # a single-file archive (or any archive where all files share a full path)
    # would yield depth == full path length instead of the directory depth.
    min_len = min(len(p) for p in split_paths)
    for i in range(min_len - 1):
      first = split_paths[0][i]
      if all(p[i] == first for p in split_paths):
        depth += 1
      else:
        break
    return max(depth, 1)
  except Exception:
    return 1


def _assert_safe_archive_members(filepath):
  """Refuse a tar archive whose member names would escape the extraction dir.

  Source archives can come from the store MIRROR (keyed by URL hash, content
  not verified when the recipe declares no checksum), so member names are
  untrusted: a name like ``../../.bashrc`` or ``/etc/...`` is the classic
  tar-slip. GNU/bsd tar versions differ in their own protections, so enforce
  explicitly, using tarfile for metadata-only iteration (no extraction).

  ``.tar.zst`` cannot be opened by tarfile on all platforms; it falls through
  the ImportError/ReadError branch and relies on tar's built-in refusal of
  absolute/``..`` names (GNU tar >= 1.29 skips them without ``-P``).

  Raises ``ValueError`` on an unsafe member; returns None when safe/unscanned.
  """
  import tarfile
  try:
    with tarfile.open(filepath) as tf:
      for m in tf:
        name = m.name
        if name.startswith(("/", "\\")) or os.path.splitdrive(name)[0]:
          raise ValueError("absolute member path %r" % name)
        if ".." in name.split("/"):
          raise ValueError("traversing member path %r" % name)
        # A hardlink/symlink MEMBER NAME that traverses is as bad as a file.
        if (m.islnk() or m.issym()) and m.linkname.startswith("/"):
          # Absolute link targets pointing outside are suspicious in source
          # archives; relative '..' targets are common (lib64 -> ../lib) and
          # are allowed — tar cannot write THROUGH them mid-extraction for
          # names we have already vetted above.
          raise ValueError("absolute link target %r -> %r" % (name, m.linkname))
  except tarfile.ReadError:
    debug("cannot pre-scan %s with tarfile (unsupported compression) — "
          "relying on tar's own traversal protection", filepath)


def _extract_zip_strip(archive_path, dest_dir, strip=1):
  """Extract a zip archive into dest_dir, stripping *strip* path components.

  This mirrors the behaviour of ``tar --strip-components=N``: every member
  whose path starts with at least *strip* directory components has those
  components removed before extraction.  Members with fewer than *strip*
  components (including the top-level directory entries themselves) are
  skipped.
  """
  with zipfile.ZipFile(archive_path) as zf:
    for member in zf.infolist():
      parts = member.filename.split("/", strip)
      if len(parts) <= strip or not parts[strip]:
        continue
      member.filename = parts[strip]
      zf.extract(member, dest_dir)


def _patchset_fingerprint(spec, patches_dir):
  """Return a stable hex digest of the recipe's patch set (each patch's name and
  full content, in declaration order), or None when the package has no patches.
  Patch files are read from *patches_dir*.

  Used to detect when a previously-patched source tree must be re-extracted
  because the patch *content* changed — bits keys the source directory by
  package version/commit, not by patch content, so without this an edited patch
  would silently have no effect on rebuild (see _wipe_source_if_patchset_changed).
  """
  patches = spec.get("patches")
  if not patches:
    return None
  import hashlib
  h = hashlib.sha256()
  for patch_entry in patches:
    patch_name, _ = parse_entry(patch_entry)
    h.update(patch_name.encode("utf-8", "replace"))
    h.update(b"\0")
    with open(os.path.join(patches_dir, patch_name), "rb") as fh:
      h.update(fh.read())
    h.update(b"\0")
  return h.hexdigest()


def _wipe_source_if_patchset_changed(spec, source_dir):
  """Remove source_dir unless it can be guaranteed pristine-then-correctly-patched.

  Patches must always be applied to a freshly-extracted tree.  The source dir is
  shared across builds of the same version, _apply_patches skips re-patching when
  the ``.bits_patched`` sentinel is present, and _extract_source_archives skips
  re-extraction when ``.bits_extracted`` is present.  Wipe (forcing a clean
  re-extract + re-apply) when either:

    * ``.bits_patched`` records a DIFFERENT patch-set fingerprint than the current
      patches — i.e. a patch file was edited (a legacy/empty sentinel has no
      fingerprint and counts as different, so old trees self-heal); or
    * there is NO ``.bits_patched`` sentinel but the tree was already extracted
      (``.bits_extracted`` present).  That means a previous patch run did not
      complete (``.bits_patched`` is only written on full success), leaving a
      partially/already-patched tree.  Re-running ``patch`` on it yields
      "Reversed (or previously applied) patch detected" and corruption, so the
      tree must be re-extracted clean first.
  """
  import json
  patched = os.path.join(source_dir, ".bits_patched")
  extracted = os.path.join(source_dir, ".bits_extracted")
  current = _patchset_fingerprint(spec, os.path.join(spec["pkgdir"], "patches"))
  if os.path.exists(patched):
    try:
      recorded = json.loads(open(patched).read()).get("patchset")
    except Exception:
      recorded = None   # legacy empty sentinel, or unreadable → treat as changed
    if recorded == current:
      return            # already patched with the same patch set: reuse as-is
    reason = "patch set changed (recorded %r, now %r)" % (recorded, current)
  elif os.path.exists(extracted):
    reason = ("source was extracted but not successfully patched "
              "(no .bits_patched); a previous patch run likely failed partway")
  else:
    return              # pristine / not yet extracted: nothing to wipe
  debug("Wiping %s for %s to re-extract and re-patch from pristine sources: %s",
        source_dir, spec.get("package", "?"), reason)
  shutil.rmtree(source_dir, ignore_errors=True)


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

  # Opt-out: when auto-patching is disabled (recipe header `auto_patch: false`,
  # the global --no-auto-patch flag, or `auto_patch: false` in the active
  # defaults), bits stages the patch files in $SOURCEDIR and exports
  # $PATCH0..$PATCH_COUNT but does NOT apply them — the recipe body applies them
  # itself (e.g. via the bits_apply_patches helper). No sentinel is written, so
  # the recipe owns idempotency. Default (key absent) is True: unchanged.
  if not spec.get("auto_patch", True):
    debug("Auto-patching disabled for %s; %d patch file(s) staged in %s for the "
          "recipe to apply", spec.get("package", "?"),
          len(spec.get("patches") or []), source_dir)
    return

  sentinel = os.path.join(source_dir, ".bits_patched")
  if os.path.exists(sentinel):
    return

  import logging
  _patch_checksums = spec.get("patch_checksums") or {}
  pkg_label = "%s@%s" % (spec.get("package", "?"), spec.get("version", "?"))
  progress = ProgressPrint("Patching %s" % pkg_label)
  # Emit the header immediately so the package name is visible before any
  # output from patch(1) — important when a failure dumps verbose text.
  progress("Patching %s", pkg_label)
  for patch_entry in spec["patches"]:
    patch_name, _ = parse_entry(patch_entry)
    patch_path = os.path.join(source_dir, patch_name)
    debug("Applying patch %s in %s", patch_name, source_dir)
    # In non-debug mode suppress patch(1) stdout/stderr so it doesn't leak
    # into the progress display; capture it so we can include it in the error
    # message on failure.
    capture = not logging.getLogger().isEnabledFor(logging.DEBUG)
    pipe = subprocess.PIPE if capture else None
    try:
      result = subprocess.run(
        ["patch", "-p1", "--batch", "--input", patch_path],
        cwd=source_dir,
        stdout=pipe, stderr=subprocess.STDOUT if capture else None,
        check=True,
      )
      if capture:
        debug("patch output for %s:\n%s", patch_name,
              result.stdout.decode(errors="replace"))
    except subprocess.CalledProcessError as exc:
      patch_out = exc.output.decode(errors="replace") if exc.output else ""
      # Collect any .rej files left behind by patch so the developer can see
      # exactly which hunks failed without having to dig into the build tree.
      rej_files = sorted(glob(os.path.join(source_dir, "**", "*.rej"), recursive=True))
      rejects = ""
      for rej in rej_files:
        try:
          with open(rej) as fh:
            rejects += "\n--- %s ---\n%s" % (os.path.relpath(rej, source_dir), fh.read())
        except OSError:
          rejects += "\n--- %s --- (could not read)\n" % os.path.relpath(rej, source_dir)
      progress.end("failed", error=True)
      dieOnError(True, "Patch %s failed to apply for %s.%s%s" % (
        patch_name, spec.get("package", "?"),
        ("\n" + patch_out.strip()) if patch_out.strip() else "",
        rejects if rejects else "\n(no .rej files found — check patch format/strip level)",
      ))
      return  # sentinel must not be written on failure (also guards mocked dieOnError in tests)

  progress.end("done")
  # Record the patch-set fingerprint so a later build can detect a changed patch
  # set and re-extract (see _wipe_source_if_patchset_changed).
  import json
  with open(sentinel, "w") as _sf:
    json.dump({"patchset": _patchset_fingerprint(spec, source_dir)}, _sf)


def _extract_source_archives(source_dir, expected_names=None):
  """Extract any source archives found directly inside source_dir.

  After ``download()`` places a release tarball (e.g. ``gsl-2.8.tar.gz``) in
  ``source_dir``, the build recipe expects an *unpacked* source tree there —
  not a bare archive file.  This function scans source_dir for known archive
  types and extracts each one, stripping the top-level directory that release
  tarballs almost universally contain (``--strip-components=1`` for tar,
  equivalent logic for zip).

  When *expected_names* is given (the set of basenames bits downloaded for the
  current spec) extraction is restricted to those files.  This keeps a stale
  archive left over from a previous recipe revision — e.g. ``foo-1.1.tar.gz``
  lingering in a version directory after the ``sources:`` URL was bumped to
  ``foo-1.1.atlas1.tar.gz`` — from being picked up and aborting the build.  The
  source directory is keyed by version and shared across rebuilds, so such
  leftovers are common.  ``expected_names=None`` preserves the legacy "extract
  everything" behaviour for callers that do not know the download set.

  A ``.bits_extracted`` sentinel file is written after a successful run so
  that repeated invocations (e.g. a resumed build) skip re-extraction and do
  not clobber a partially-built tree.  The sentinel records the strip depth
  used for each archive so that a stale extraction (e.g. produced by older
  bits code that hardcoded --strip-components=1) is detected and replaced
  when the required depth has changed.
  """
  import json
  sentinel = os.path.join(source_dir, ".bits_extracted")

  # Guard against a caller that never created source_dir (e.g. empty
  # active_sources that bypassed makedirs, or a future code path change).
  if not os.path.isdir(source_dir):
    return

  # Compute the strip depths we would use for every archive present now.
  # We do this before consulting the sentinel so we can compare.
  expected = set(expected_names) if expected_names is not None else None
  archives = []
  for entry in sorted(os.listdir(source_dir)):
    filepath = os.path.join(source_dir, entry)
    if not os.path.isfile(filepath):
      continue
    if expected is not None and entry not in expected:
      debug("Skipping %s: not among the sources downloaded for this spec (%s)",
            entry, ", ".join(sorted(expected)) or "<none>")
      continue
    lower = entry.lower()
    if any(lower.endswith(ext) for ext in _TAR_EXTENSIONS) or \
       any(lower.endswith(ext) for ext in _ZIP_EXTENSIONS):
      archives.append((entry, filepath, _archive_prefix_depth(filepath)))

  if not archives:
    return  # nothing to extract

  # Check whether a sentinel already exists and whether its recorded strips
  # match what we would use today.  A mismatch means the previous extraction
  # used a different (wrong) strip depth — invalidate the sentinel so we
  # re-extract with the correct depth.
  if os.path.exists(sentinel):
    try:
      recorded = json.loads(open(sentinel).read())
      recorded_strips = recorded.get("strips", {})
    except (OSError, ValueError, KeyError):
      recorded_strips = {}
    current_strips = {entry: strip for entry, _, strip in archives}
    if recorded_strips == current_strips:
      return  # sentinel is valid; skip re-extraction
    debug("Stale extraction sentinel for %s (recorded strips %s, now %s): re-extracting",
          source_dir, recorded_strips, current_strips)
    os.unlink(sentinel)

  for entry, filepath, strip in archives:
    lower = entry.lower()
    debug("Extracting %s into %s (--strip-components=%d)", entry, source_dir, strip)
    try:
      if any(lower.endswith(ext) for ext in _TAR_EXTENSIONS):
        _assert_safe_archive_members(filepath)   # tar-slip guard (untrusted mirror)
        subprocess.check_call(
          ["tar", "xf", filepath, "--strip-components=%d" % strip, "-C", source_dir]
        )
      else:
        # zipfile.extract sanitises member names itself (drives, leading
        # separators and '..' components are stripped by CPython).
        _extract_zip_strip(filepath, source_dir, strip=strip)
    except (subprocess.CalledProcessError, zipfile.BadZipFile, ValueError,
            OSError) as exc:
      # A corrupt or wrong-format archive must fail this package cleanly rather
      # than crash the whole run with a traceback. The most common cause is a
      # download that returned an error/HTML page instead of the tarball (a
      # dead or mistyped source URL), or a truncated download.
      dieOnError(True,
                 "Failed to unpack source archive '%s' (%s). It is not a valid "
                 "archive -- the download may be corrupt or the source URL may "
                 "have returned an error page. Remove %s and re-run, and verify "
                 "the package's sources: URL." % (entry, exc, filepath))

  strips_map = {entry: strip for entry, _, strip in archives}
  with open(sentinel, "w") as fh:
    json.dump({"strips": strips_map}, fh)


def checkout_sources(spec, work_dir, reference_sources, containerised_build,
                     enforce_mode="off", sync_helper=None, parallel_sources=1,
                     architecture=None):
  """Check out sources to be compiled, potentially from a given reference.

  ``architecture`` is the raw (pre-combined) architecture string used to
  filter arch-conditional ``sources:`` entries.  When ``None`` the value is
  read from the ``ARCHITECTURE`` environment variable (which is only set
  inside the build shell script, *not* in the Python process).  Callers
  should always pass the value explicitly.

  ``sync_helper`` is an optional sync-backend instance (from
  ``bits_helpers.sync``).  When provided it is forwarded to every
  ``download()`` call so that source archives are fetched from / archived
  to the remote store as described in ``bits_helpers.download.download``.

  ``parallel_sources`` controls how many URLs in the ``sources:`` list are
  downloaded concurrently.  The default (1) preserves the original sequential
  behaviour.  Values >1 use a ``ThreadPoolExecutor`` and raise the first
  exception encountered, preserving the same failure semantics.
  """
  # Recipes forbidding source redistribution (redistributable_sources /
  # redistributable: false) may still READ the store's source mirror, but a
  # fresh upstream download must never be archived to it — the store may be
  # world-readable, and "no redistribution" covers the source form too.
  from bits_helpers.sync import source_sync_for
  sync_helper = source_sync_for(spec, sync_helper)
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

  # For tarball sources, if this shared source tree was already patched with a
  # different patch set, wipe it so it is re-extracted and re-patched cleanly.
  # (Scoped to tarball sources; git checkouts handle their own working tree.)
  if spec.get("sources") and spec.get("patches") and os.path.isdir(source_dir):
    _wipe_source_if_patchset_changed(spec, source_dir)

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
    # Resolve arch-conditional / bash-evaluated source entries before
    # downloading.  ``architecture`` is passed in by the caller (build.py
    # knows raw_architecture); fall back to the env var for backwards
    # compatibility with tests and external callers, then to "".
    _arch = architecture or os.environ.get("ARCHITECTURE", "")
    _fmt = {"name": spec["package"], "version": spec["version"]}
    active_sources = []
    for s in spec["sources"]:
      resolved, include = _resolve_source_entry(s, _arch)
      if not include:
        debug("Skipping source %r: architecture %r does not match", s, _arch)
        continue
      # Substitute %(name)s and %(version)s in the resolved URL so recipes
      # can write concise entries like:
      #   https://example.com/%(name)s-%(version)s.tar.gz
      try:
        resolved = resolved % _fmt
      except (KeyError, ValueError):
        pass  # leave the string as-is if substitution fails
      active_sources.append(resolved)

    # Fail early with a clear message when no source entry matched the current
    # architecture.  Without this check the build would silently continue with
    # an empty source directory and fail deep inside Configure/Make with a
    # confusing error unrelated to the real cause (pattern mismatch).
    if not active_sources:
      dieOnError(True,
        "No source URL matched architecture %r for %s@%s.\n"
        "  Defined source entries:\n%s" % (
          _arch, spec["package"], spec["version"],
          "".join("    %s\n" % s for s in spec["sources"])))

    # Ensure source_dir exists before any download attempt.  download() only
    # creates the destination directory on a successful download (mkdir -p is
    # part of the cp command on line 452 of download.py); an empty
    # active_sources list or a failed download would leave source_dir absent,
    # causing _extract_source_archives to ENOENT on os.listdir.
    os.makedirs(source_dir, exist_ok=True)

    def _download_one(s):
      url, inline_checksum = parse_entry(s)
      src_checksum = _source_checksums.get(url) or inline_checksum
      download(url, source_dir, work_dir, checksum=src_checksum,
               enforce_mode=enforce_mode, sync_helper=sync_helper)

    if parallel_sources <= 1 or len(active_sources) <= 1:
      # Sequential path: preserves original behaviour for the common case.
      for s in active_sources:
        _download_one(s)
    else:
      # Parallel path: submit all source downloads and re-raise the first error.
      with ThreadPoolExecutor(max_workers=parallel_sources) as pool:
        futures = {pool.submit(_download_one, s): s for s in active_sources}
        first_exc = None
        for fut in as_completed(futures):
          exc = fut.exception()
          if exc is not None and first_exc is None:
            first_exc = exc
        if first_exc is not None:
          raise first_exc
    # Unpack any downloaded archives so the build script sees an unpacked
    # source tree at $SOURCEDIR rather than a bare archive file.
    # _extract_source_archives() detects stale sentinels (wrong strip depth
    # from older bits) by comparing recorded vs. current strip depths, so no
    # manual sentinel removal is needed here for any package type.  We restrict
    # it to the basenames we actually downloaded for this spec so a stale
    # archive from a previous recipe revision sharing the version directory is
    # ignored rather than aborting the build.
    _expected_archives = {parse_entry(s)[0].rsplit("/", 1)[-1] for s in active_sources}
    _extract_source_archives(source_dir, expected_names=_expected_archives)
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
    # Safety: verify source_dir is inside work_dir before any destructive
    # operation — an empty or malformed commit hash could otherwise cause
    # source_dir to collapse to a parent directory.
    _safe_work_dir = os.path.abspath(work_dir) + os.sep
    _safe_source_dir = os.path.abspath(source_dir)
    dieOnError(not _safe_source_dir.startswith(_safe_work_dir),
               "source_dir '%s' is outside work_dir '%s' — refusing to remove "
               "it to prevent accidental data loss." % (source_dir, work_dir))
    # Safety: refuse to clone over an existing git repository.
    dieOnError(os.path.exists(os.path.join(source_dir, ".git")),
               "source_dir '%s' already contains a .git repository; refusing to "
               "clone '%s' over it to prevent clobbering an existing checkout."
               % (source_dir, spec.get("source", "?")))
    shutil.rmtree(source_dir, ignore_errors=True)
    scm_exec(scm.cloneSourceCmd(spec["source"], source_dir, spec.get("reference"),
                                usePartialClone=True))
    scm_exec(scm.setWriteUrlCmd(spec.get("write_repo", spec["source"])), source_dir)
    scm_exec(scm.checkoutCmd(spec["tag"]), source_dir)
    _verify_commit_pin(scm, spec, source_dir, enforce_mode)
    _apply_patches(spec, source_dir)
