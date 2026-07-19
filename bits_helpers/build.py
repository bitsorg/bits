# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

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
from bits_helpers.utilities import resolve_store_path, resolve_links_path, effective_arch, SHARED_ARCH, compute_combined_arch, pkg_to_shell_id, ver_rev
from bits_helpers.utilities import parseDefaults, readDefaults, resolve_variables
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

  This function is designed to be run in a thread pool; its result is never
  collected, so any exception it raises is silently swallowed by the executor.
  It must therefore not depend on state the main loop has not yet produced.

  In particular ``spec["hash"]`` is assigned per package inside the build loop
  (``storeHashes`` / the revision counter), in topological order because a hash
  folds in its dependencies' hashes. The prefetch pool, however, is submitted
  BEFORE that loop starts, so for any package not yet processed ``spec["hash"]``
  does not exist yet. Return quietly in that case instead of raising a swallowed
  KeyError: prefetch is a best-effort speedup, the main loop fetches the tarball
  synchronously anyway, and a security gate (``--require-signed-reuse``) keys off
  ``spec["prefetched_tarballs"]`` which we simply leave unset here.
  """
  from bits_helpers.download import _acquire_download, _wait_for_sentinel, download
  from bits_helpers.checksum import parse_entry as _pe

  if not spec.get("hash"):
    debug("Prefetch skipped for %s: hash not computed yet", spec.get("package"))
    return

  arch = effective_arch(spec, build_arch)
  tar_hash_dir = os.path.join(work_dir, resolve_store_path(arch, spec["hash"]))

  # --- Tarball prefetch -------------------------------------------------------
  if not spec.get("is_devel_pkg"):
    # Try to atomically claim the tarball download slot.
    # sentinel path: tar_hash_dir + ".downloading"
    if _acquire_download(tar_hash_dir):
      try:
        os.makedirs(tar_hash_dir, exist_ok=True)
        _before = set(glob(os.path.join(tar_hash_dir, "*gz")))
        sync_helper.fetch_tarball(spec)
        # Record what came from the REMOTE store. doBuild treats tarballs that
        # predate its own fetch_tarball call as trusted local build-node
        # artifacts and exempts them from --require-signed-reuse; a prefetched
        # tarball also predates that call, but is remote and must stay gated.
        spec["prefetched_tarballs"] = sorted(
          set(glob(os.path.join(tar_hash_dir, "*gz"))) - _before)
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


def _localize_manifest(src, work_dir):
  """Return a local filesystem path for a manifest *src*.

  A local path is returned unchanged; a URL is fetched (with its detached
  ``.sig``) into ``MANIFESTS/trust`` and the local path returned. Note
  ``downloadUrllib2`` keeps a pre-existing file of the same name, so repeated
  calls within one build (e.g. from both trust-index and trust-records loaders)
  resolve to the same local manifest.
  """
  if "://" not in src:
    return src
  from bits_helpers.download import downloadUrllib2
  dest = os.path.join(work_dir, "MANIFESTS", "trust")
  os.makedirs(dest, exist_ok=True)
  name = os.path.basename(src.split("?")[0]) or "trust-manifest.json"
  downloadUrllib2(src, dest, work_dir, dest_filename=name)
  downloadUrllib2(src + ".sig", dest, work_dir, dest_filename=name + ".sig")
  return os.path.join(dest, name)


_TRUST_IMPORT_WARNED = False


def _try_import_trust():
  """Return the ``bits_helpers.trust`` module, or ``None`` if it can't be imported.

  ``trust`` hard-depends on the optional ``cryptography`` package. When it (or any
  other import dependency) is absent, signed-reuse verification is simply
  unavailable: every caller here fails CLOSED (no reuse -> rebuild) rather than
  letting an ``ImportError`` abort the build. Warns once so the operator knows to
  install ``cryptography`` to re-enable signed reuse.
  """
  global _TRUST_IMPORT_WARNED
  try:
    from bits_helpers import trust
    return trust
  except Exception as exc:  # ImportError (cryptography missing) or anything else
    if not _TRUST_IMPORT_WARNED:
      warning("Signed-reuse support is unavailable (%s). Install the "
              "'cryptography' package in the bits environment to enable "
              "--require-signed-reuse; until then remote tarballs are not "
              "verified and will be rebuilt instead of reused.", exc)
      _TRUST_IMPORT_WARNED = True
    return None


def _load_trusted_index(src, work_dir, accept_groups):
  """Verify one signed manifest (local path or URL) -> (key_id, {hash: sha256}).

  Returns ``(None, {})`` on any failure (missing/unreachable/untrusted, or the
  signing library not installed), so a caller can degrade to a rebuild rather
  than crash — fail-closed.
  """
  trust = _try_import_trust()
  if trust is None:
    return None, {}
  try:
    return trust.trusted_index(_localize_manifest(src, work_dir),
                               accept_groups=accept_groups)
  except Exception as exc:
    warning("--require-signed-reuse: could not fetch/verify %s (%s); those "
            "entries will not be reused.", src, exc)
    return None, {}


def trusted_reuse_index(args, work_dir):
  """Return the verified {hash: tarball_sha256} index for --require-signed-reuse.

  --trust-manifest may name several signed manifests (comma-separated): a build
  node fetches its own ``common-manifest-<arch>.json`` plus the always-relevant
  ``common-manifest-shared.json`` and this merges their indexes. Each is
  signature-verified independently; content hashes are globally unique so the
  merge is collision-free. Loaded once, memoised on *args*. An empty map means
  nothing is attested, so every remote reuse fails closed.
  """
  cached = getattr(args, "_trustedReuseIndex", None)
  if cached is not None:
    return cached
  raw = getattr(args, "trustManifest", None)
  sources = [s.strip() for s in str(raw or "").split(",") if s.strip()]
  raw_groups = getattr(args, "trustGroups", None)
  accept_groups = ([g.strip() for g in raw_groups.split(",") if g.strip()]
                   if raw_groups else None)
  index = {}
  if not sources:
    warning("--require-signed-reuse set without --trust-manifest; "
            "no remote tarball will be reused.")
  for src in sources:
    kid, part = _load_trusted_index(src, work_dir, accept_groups)
    if not kid:
      warning("--require-signed-reuse: could not verify signed manifest %s; "
              "its tarballs will not be reused.", src)
      continue
    index.update(part)
    debug("Trusted reuse index from %s (signed by %s): %d entries%s",
          src, kid, len(part),
          "" if accept_groups is None else " (groups: %s + common)" % ",".join(accept_groups))
  args._trustedReuseIndex = index
  return index


def trusted_reuse_records(args, work_dir):
  """Verified common-manifest package entries — the primary rev-index source.

  ADR-0005: once the S3 store keeps only hash-keyed tarballs (no version links),
  the revision counter derives its ``(version, revision, hash)`` history from the
  signed common manifest instead of scanning ``TARS/<arch>/<pkg>/``. This returns
  the accepted package entries of every configured --trust-manifest (same sources
  and group policy as :func:`trusted_reuse_index`), each carrying
  ``package/version/revision/effective_architecture/hash``. Memoised on *args*;
  empty when signed reuse is off, in which case the counter falls back to the
  rev-index markers (and, until Phase 2d, the local version links).
  """
  cached = getattr(args, "_trustedReuseRecords", None)
  if cached is not None:
    return cached
  trust = _try_import_trust()
  if trust is None:
    args._trustedReuseRecords = []      # cache the miss: don't retry per package
    return []
  raw = getattr(args, "trustManifest", None)
  sources = [s.strip() for s in str(raw or "").split(",") if s.strip()]
  raw_groups = getattr(args, "trustGroups", None)
  accept_groups = ([g.strip() for g in raw_groups.split(",") if g.strip()]
                   if raw_groups else None)
  records = []
  for src in sources:
    try:
      kid, entries = trust.trusted_records(
        _localize_manifest(src, work_dir), accept_groups=accept_groups)
      if kid:
        records.extend(entries)
    except Exception as exc:
      debug("rev-index: could not load records from %s (%s)", src, exc)
  args._trustedReuseRecords = records
  return records


def _store_revision_records(spec, spec_arch, work_dir, sync_helper):
  """``[(revision, hash), …]`` read from the NAMES of our own content objects.

  This is the authoritative ``hash -> revision`` direction, and the only source
  that can answer "what revision does the store already use for *my* hash?".

  Both other sources answer the opposite question. The rev-index markers are
  keyed ``revision -> hash`` and are write-once, so once revision 1 is claimed by
  hash A, a later build of hash B at revision 1 cannot record itself; the manifest
  likewise only lists revisions that were certified. When a hash is missing from
  both, the counter sees its revision as "busy", assigns N+1, and then
  ``fetch_tarball`` — which matches purely by hash — happily unpacks the tarball
  named ``-N`` into the ``-(N+1)`` install root. Every reused package then fails.

  Reading the name back makes the counter self-correcting, and keeps a wiped or
  diverged rev-index from ever desynchronising the install directory from the
  tarball it contains.

  A hash dir *should* hold exactly one object, but the upload HEAD-skip is keyed
  on the full key (hash AND file name, see ``upload_symlinks_and_tarball``), so a
  hash that was once uploaded under two revision labels keeps both. We therefore
  pick the LOWEST numeric revision — the label that landed first — which is stable
  across builders and matches what an earlier build would already have installed.

  Hashes are probed in ``remote_hashes`` preference order and we stop at the first
  that yields a record: that hash is the one ``better_tarball`` would pick anyway.
  Local files (already prefetched) are consulted before the network.

  Returns at most one ``(revision, hash)`` pair, so the fold cannot tie against
  itself.
  """
  from bits_helpers import rev_index
  lister = getattr(sync_helper, "list_store_tarballs", None)

  def _revs(names):
    # Skip revision-less objects (force_revision="" -> ""), and localN objects:
    # _fold_revision_records only ever matches against remote_hashes, and local
    # revisions are never published, so a localN record here could only mislabel
    # a remote tarball. Mirrors the version-link scan's own local/remote split.
    return sorted(int(rev) for rev in
                  (rev_index.revision_from_tarball(n, spec["package"],
                                                   spec["version"], spec_arch)
                   for n in names or ())
                  if rev and not rev.startswith("local"))

  for pkg_hash in spec["remote_hashes"]:
    try:
      local = os.listdir(os.path.join(work_dir,
                                      resolve_store_path(spec_arch, pkg_hash)))
    except OSError:
      local = []
    # The local hash dir may exist but be EMPTY: _prefetch_package makes it before
    # downloading. Never let that suppress the remote lookup — an empty local
    # listing is "unknown", not "absent".
    revs = _revs(local)
    if not revs and lister:
      revs = _revs(lister(spec_arch, pkg_hash))
    if revs:
      if len(revs) > 1:
        warning("Store holds %s revisions %s for %s %s under one hash (%s); "
                "using the lowest.", len(revs), ", ".join(map(str, revs)),
                spec["package"], spec["version"], pkg_hash)
      # preference order: the first hash that exists is the one better_tarball
      # would pick anyway, so stop here.
      return [(str(revs[0]), pkg_hash)]
  return []


def _select_cached_tarball(tarballs, spec, spec_arch):
  """Pick the tarball to unpack, or "" to force a rebuild.

  ``fetch_tarball`` matches purely by HASH, and its name regex makes the revision
  group optional, so it happily hands back an object named ``-1`` when the counter
  assigned revision 2. build_template.sh then extracts the archive and moves
  ``TMP/<hash>/<pkg>/<version>-<revision>`` into INSTALLROOT — a path that does not
  exist in the ``-1`` tree — and the reused package fails with no useful error.

  So require the name to agree with ``spec["revision"]``. A revision-less object
  (``force_revision=""``) is still accepted, since it has no revision to disagree
  with. Anything else is discarded: rebuilding is always safe, unpacking the wrong
  revision never is.
  """
  if not tarballs:
    return ""
  want = "{pkg}-{ver}-{rev}.{arch}.tar.gz".format(
    pkg=spec["package"], ver=spec["version"], rev=spec["revision"], arch=spec_arch)
  revless = "{pkg}-{ver}.{arch}.tar.gz".format(
    pkg=spec["package"], ver=spec["version"], arch=spec_arch)
  for name in (want, revless):
    for tarball in tarballs:
      if os.path.basename(tarball) == name:
        return tarball
  warning("Ignoring cached tarball(s) %s for %s: none matches the assigned "
          "revision %s. Rebuilding instead of unpacking a mismatched tree.",
          ", ".join(sorted(os.path.basename(t) for t in tarballs)),
          spec["package"], spec["revision"])
  return ""


def _revision_index_records(spec, spec_arch, args, work_dir, sync_helper):
  """``[(revision, hash), …]`` candidates for (pkg, version, arch), ADR-0005 P2c.

  Union of the store's own content-object names (authoritative for OUR hash), the
  certified common-manifest records, and the S3 rev-index markers (supplement, for
  uncertified rebuilds). A LIST of pairs, because one revision can legitimately
  carry several hashes (rebuilt after a recipe change). Best-effort: an empty list
  just means the counter relies on whatever local version links are present.

  For any hash the store has an object for, the object's NAME is definitive, so we
  drop every manifest/marker pair that mentions that same hash. Ordering alone
  would NOT achieve this: ``better_tarball`` tie-breaks on the position of the hash
  in ``remote_hashes``, and for two records with the SAME hash the tie resolves in
  favour of whichever is folded LAST. Filtering removes the tie entirely, so a
  stale ``(revision, our-hash)`` pairing can never out-rank the real object name.
  Pairs for other hashes are kept: they still reserve their revision numbers.
  """
  from bits_helpers import rev_index
  manifest_recs = rev_index.manifest_records(
    trusted_reuse_records(args, work_dir),
    spec["package"], spec["version"], spec_arch)
  markers = {}
  reader = getattr(sync_helper, "read_rev_markers", None)
  if reader:
    markers = reader(spec["package"], spec["version"], spec_arch)

  store_recs = _store_revision_records(spec, spec_arch, work_dir, sync_helper)
  covered = {h for _, h in store_recs}
  if covered:
    manifest_recs = [(r, h) for r, h in manifest_recs if h not in covered]
    markers = {r: h for r, h in markers.items() if h not in covered}
  return rev_index.merge_records(store_recs + manifest_recs, markers)


def _fold_revision_records(records, spec, candidate, busy_revisions, revision_prefix):
  """Fold rev-index ``{revision: hash}`` records into the revision counter state.

  Mirrors the version-link scan (ADR-0005 P2c): a record whose hash matches this
  build becomes a reuse candidate (via :func:`better_tarball`); any other reserves
  its revision number in *busy_revisions*. Records are remote-only — local
  revisions are never published, so they never appear here. Returns the updated
  ``(candidate, busy_revisions)``.

  Invoked by the counter only as a gap-fill, i.e. with *candidate* ``None`` (the
  local scan found nothing to reuse), so it never overrides a link-derived reuse
  choice.

  *records* is a list of ``(revision, hash)`` PAIRS, not a map: one revision may
  carry several hashes (rebuilt after a recipe change), and collapsing them would
  hide the very hash we are about to reuse — the counter would then mark that
  revision busy, assign a fresh one, and still unpack the old revision's tarball.
  A revision that matches on ANY of its hashes is reused; `busy` only collects
  revisions none of whose hashes we can use (and is ignored anyway once a
  candidate exists, since we then reuse instead of assigning).
  """
  for rev, rev_hash in (records or ()):
    if rev_hash in spec["remote_hashes"]:
      candidate = better_tarball(spec, candidate, (rev, rev_hash, None))
    elif rev.startswith(revision_prefix) and rev[len(revision_prefix):].isdigit():
      busy_revisions.add(int(rev[len(revision_prefix):]))
  return candidate, busy_revisions


def derive_trust_manifest_srcs(store, prefix, arch, endpoint=None):
  """Derive the signed common-manifest source URLs from a remote store.

  Returns a list of http(s) sources (own-arch first, then the always-shared one)
  so a bare ``bits build`` gets signed reuse without an explicit --trust-manifest;
  [] if the store form is unsupported.

  * ``http(s)://…`` read stores host the manifest directly beneath them.
  * ``b3://<bucket>[::rw]`` / ``s3://<bucket>`` map to the bucket's ANONYMOUS S3
    read URL — ``<endpoint>/swift/v1/<bucket>`` — because the manifest is fetched
    over http (urllib): it is the same public object an http store would serve.
    ``endpoint`` defaults to CERN S3; non-swift/non-CERN S3 should pass an
    explicit --trust-manifest.
  """
  store = str(store or "")
  prefix = str(prefix or "MANIFESTS/common-manifest").lstrip("/")
  base = None
  if store.startswith(("http://", "https://")):
    base = store.rstrip("/") + "/" + prefix
  elif store.startswith(("b3://", "s3://")):
    bucket = store.split("://", 1)[1].split("/", 1)[0].split("::", 1)[0]
    ep = str(endpoint or "https://s3.cern.ch").rstrip("/")
    if bucket:
      base = "%s/swift/v1/%s/%s" % (ep, bucket, prefix)
  if not base:
    return []
  srcs = ["%s-%s.json" % (base, arch)] if arch else []
  srcs.append("%s-shared.json" % base)
  return srcs


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
# The dependency-closure field that drives each dist* tree (graph -> symlinks).
#   dist         -> full build+runtime closure
#   dist-direct  -> direct requires only
#   dist-runtime -> full runtime closure
DIST_LINK_TYPES = (
    ("dist",         "full_requires"),
    ("dist-direct",  "requires"),
    ("dist-runtime", "full_runtime_requires"),
)


def _dist_links(spec, specs, arch, work_dir, repo_type, requires_key):
  """Rebuild one dist* tree for *spec* purely from the resolved *specs* graph.

  Writes ``TARS/<arch>/<repo_type>/<pkg>/<pkg>-<verrev>/`` with one symlink per
  (the package itself + its dependencies named in *requires_key*), each pointing
  at that dependency's content-addressed store tarball. Reads only the graph — no
  S3, no filenames — so it is the pure core of ``createDistLinks``.

  dist links are per-build-platform even when a package is ``shared``, so the
  tree arch is *arch* while each symlink target uses the dependency's own
  ``effective_arch``.  ``ver_rev()`` honours per-package ``force_revision``.
  """
  target_dir = "{work_dir}/TARS/{arch}/{repo}/{package}/{package}-{ver_rev}" \
    .format(work_dir=work_dir, arch=arch, repo=repo_type,
            ver_rev=ver_rev(spec), **spec)
  shutil.rmtree(target_dir.encode("utf-8"), ignore_errors=True)
  makedirs(target_dir, exist_ok=True)
  for pkg in [spec["package"]] + list(spec[requires_key]):
    dep_spec = specs[pkg]
    dep_arch = effective_arch(dep_spec, arch)
    dep_tarball = "../../../../../TARS/{arch}/store/{short_hash}/{hash}/{package}-{ver_rev}.{arch}.tar.gz" \
      .format(arch=dep_arch, short_hash=dep_spec["hash"][:2],
              ver_rev=ver_rev(dep_spec), **dep_spec)
    symlink(dep_tarball, target_dir)


def createDistLinks(spec, specs, args, syncHelper, repoType, requiresType):
  # At the point we call this function, spec has a single, definitive hash.
  # Thin wrapper over the pure graph->symlink core (kept for the existing call
  # sites' signature; syncHelper is unused — dist links come from the graph).
  _dist_links(spec, specs, args.architecture, args.workDir, repoType, requiresType)


def create_version_link(spec, arch, work_dir):
  """Rebuild the version link from the graph (no S3 fetch).

  ``TARS/<eff>/<pkg>/<pkg>-<verrev>.<eff>.tar.gz`` -> the content-addressed store
  tarball, where ``eff = effective_arch(spec, arch)`` (``shared`` for noarch).
  Produces the exact link string the build/reuse paths write today
  (``../../<eff>/store/<h2>/<hash>/<file>``), so it is a drop-in for what
  ``fetch_symlinks`` currently downloads from S3.
  """
  eff = effective_arch(spec, arch)
  links_dir = os.path.join(work_dir, resolve_links_path(eff, spec["package"]))
  makedirs(links_dir, exist_ok=True)
  tarball = "{package}-{ver_rev}.{arch}.tar.gz".format(
      package=spec["package"], ver_rev=ver_rev(spec), arch=eff)
  target = "../../{arch}/store/{short_hash}/{hash}/{tarball}".format(
      arch=eff, short_hash=spec["hash"][:2], hash=spec["hash"], tarball=tarball)
  symlink(target, os.path.join(links_dir, tarball))


def reconstruct_local_layout(spec, specs, arch, work_dir):
  """Rebuild a package's local version + dist*/closure symlinks from the graph.

  Foundation for ADR-0005: the S3 store keeps only the content-addressed
  tarballs, and this rebuilds the version/dist symlink layout on the node from
  the resolved dependency graph instead of fetching it from S3. Pure graph ->
  symlinks — no S3 access, and it does NOT change the upload/fetch paths yet
  (Phase 1). It reproduces what ``create_version_link`` + ``createDistLinks``
  (hence the build/reuse paths) produce today.
  """
  create_version_link(spec, arch, work_dir)
  for repo_type, requires_key in DIST_LINK_TYPES:
    _dist_links(spec, specs, arch, work_dir, repo_type, requires_key)

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

_HEREDOC_START = re.compile(r"<<-?\s*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")


# Front-matter keys that are metadata / publish-policy ONLY: they never affect
# what is built, so they are dropped from the HASH input (exactly like comments).
# Editing a license, description, project URL, attribution, source link, or the
# redistributable flag therefore does NOT change a package's hash — no rebuild and
# no re-publish. The executed recipe keeps every field; only hashing ignores these.
_HASH_EXCLUDED_META_KEYS = frozenset({
    "license", "description", "url", "homepage",
    "acknowledgment", "acknowledgement", "source_url", "redistributable",
})

# Source-selection keys are ALSO dropped from the recipe TEXT hash — not because
# they are cosmetic, but because storeHashes already folds the RESOLVED source
# identity into the hash from the spec (every sources: URL, the git source + tag,
# and commit_hash), AFTER _apply_source_mode has pruned to the selected form.
# Hashing the raw text on top would double-count AND make merely DECLARING a git
# alternative on a tarball recipe rebuild it, even though the default (tar) build
# is byte-identical. Excluding them keeps dual-source declarations hash-neutral
# while the spec-field hashing still makes every distinct source a distinct build.
_HASH_REDUNDANT_SOURCE_KEYS = frozenset({"source", "sources", "tag"})


def normalize_recipe_for_hash(recipe):
  """Return a copy of a recipe for HASHING ONLY, with elements that do not affect
  the build removed so that editing them does not change the build hash (and thus
  does not force a rebuild / re-publish). The executed recipe is untouched.

  Two classes are dropped:
    * full-line comments and blank lines, everywhere except inside a here-doc
      (where a leading '#' is data). The here-doc scan is conservative: it only
      ever protects MORE text, never merges two distinct recipes.
    * metadata / publish-policy keys (``_HASH_EXCLUDED_META_KEYS``) in the YAML
      front-matter — the key line and any indented block value beneath it — so
      license/description/url/acknowledgment/source_url/redistributable are free
      to edit. These are stripped ONLY in the header (before the first column-0
      ``---`` separator); the shell body is never scanned for them.
  """
  if not isinstance(recipe, str):
    return recipe
  lines = recipe.split("\n")
  # Header ends at the first column-0 "---" (an indented "---" is block-scalar
  # data, not the separator). With NO separator the string has no front-matter
  # (it is a bare shell body, as some callers/tests pass), so treat it all as body
  # -- never as header -- to preserve here-doc/comment handling.
  boundary = next((i for i, ln in enumerate(lines) if ln.rstrip() == "---"), None)
  header = lines[:boundary] if boundary is not None else []
  body = lines[boundary:] if boundary is not None else lines

  out = []
  # --- YAML front-matter: drop comments/blanks + metadata-only keys and their
  #     indented continuation lines.
  skipping_meta_block = False
  for line in header:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):   # comment / blank: never hashed
      continue
    if line[:1].isspace():                         # indented continuation line
      if skipping_meta_block:
        continue                                   # part of a dropped key's value
      out.append(line)
      continue
    key = stripped.split(":", 1)[0].strip()        # a top-level key
    if key in _HASH_EXCLUDED_META_KEYS or key in _HASH_REDUNDANT_SOURCE_KEYS:
      skipping_meta_block = True
      continue
    skipping_meta_block = False
    out.append(line)

  # --- shell body (from the "---" separator onward): unchanged behaviour, with
  #     here-doc protection.
  pending, active = [], None
  for line in body:
    if active is not None:          # inside a here-doc body: keep verbatim
      out.append(line)
      if line.strip() == active:    # terminator (tabs allowed for <<-)
        active = pending.pop(0) if pending else None
      continue
    delims = [m.group(2) for m in _HEREDOC_START.finditer(line)]
    if delims:                      # this line opens one or more here-docs
      out.append(line)
      active, pending = delims[0], delims[1:]
      continue
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):  # blank or whole-line comment
      continue
    out.append(line)
  return "\n".join(out)


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

  # Relaxed CVMFS graft (ADR-0001): a grafted package adopts the *deployed*
  # artifact's hash. The existing reuse path (CVMFSRemoteSync.fetch_symlinks +
  # the reuse decision) then materialises and symlinks the deployed tree under
  # that hash instead of building, and consumers hash against the real deployed
  # dependency — so no separate build-skip branch is needed. Only triggers when
  # the resolver tagged the spec from_cvmfs (relaxed mode); never in strict.
  if spec.get("from_cvmfs") and spec.get("cvmfs_hash"):
    _h = spec["cvmfs_hash"]
    spec["remote_revision_hash"] = _h
    spec["local_revision_hash"] = _h
    spec["remote_hashes"] = [_h]
    spec["local_hashes"] = [_h]
    spec["hash"] = _h
    # The grafted package has no followed dependencies; set deps_hash too (the
    # normal path always sets it, and DEPS_HASH is read via spec.get downstream).
    spec.setdefault("deps_hash", "")
    return

  # For now, all the hashers share data -- they'll be split below.
  h_all = Hasher()

  if spec.get("force_rebuild", False):
    h_all(str(time.time()))

  for key in ("recipe", "version", "package"):
    val = spec.get(key, "none")
    # Hash the recipe with full-line comments / blank lines removed so that
    # documentation-only edits do not change the hash and force a rebuild.
    if key == "recipe":
      val = normalize_recipe_for_hash(val)
    h_all(val)

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

  # A package's build hash is defined by its OWN inputs only — recipe text
  # (comment-stripped), sources, patches, and the hashes of its declared
  # dependencies — never the commit hash of the repository provider the recipe
  # came from. By convention recipes are self-contained; anything they need from
  # elsewhere is pulled in as an explicit package dependency (requires/
  # build_requires) or via bits-include, both of which resolve to separately and
  # granularly hashed packages — so cross-recipe coupling is already captured.
  # Folding the provider's whole-repo commit hash here instead rebuilt EVERY
  # package from that provider on ANY commit to it (even a docs/comment change);
  # invalidation must be driven by the individual packages, not the repository.
  # recipe_provider_hash is still set on the spec and recorded in the manifest
  # (manifest.add_providers) for provenance — it just no longer enters the hash.

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

  # untracked_requires: dependencies the user controls and links at runtime but
  # has chosen NOT to fold into this package's identity hash, so that editing one
  # does not invalidate (rebuild) this package or anything above it. (Empty for
  # ordinary recipes, so their hashes are byte-identical to before.)
  untracked = set(spec.get("untracked_requires", ()))
  dh = Hasher()
  for dep in spec.get("requires", []):
    # At this point, our dependencies have a single hash, local or remote, in
    # specs[dep]["hash"].
    hash_and_devel_hash = specs[dep]["hash"] + specs[dep].get("devel_hash", "")
    if dep in untracked:
      # Excluded from the identity hash entirely (not even the base hash), so a
      # change to this dependency leaves the consumer's hash — and therefore the
      # hashes of everything above it — unchanged. It is still fed into deps_hash
      # below, so a *development* build of this package picks the new dependency
      # up via an incremental rebuild.
      dh(hash_and_devel_hash)
      continue
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


def generate_initdotsh(package, specs, architecture, workDir="sw", post_build=False,
                       from_modules=False):
  """Return the contents of the given package's etc/profile/init.sh as a string.

  If post_build is true, also generate variables pointing to the package
  itself; else, only generate variables pointing at it dependencies.

  If from_modules is true (the --initdotsh-from-modules build mode), the
  post_build self-environment additionally exposes the development/build
  variables the runtime modulefile carries but the legacy init.sh omits
  (<PKG>_INCLUDE_DIR, Python site-packages on PYTHONPATH), generated from the
  package root and guarded on existence. Off by default, so the generated text
  is byte-identical to before when the mode is not active.
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


# Copyleft licenses carry a corresponding-source obligation when their binaries
# are redistributed, so a package under one of these gets a NOTICE that points at
# the exact source it was built from. Base id matched for "<id> WITH <exception>".
_COPYLEFT_LICENSES = frozenset({
    "GPL-2.0-only", "GPL-2.0-or-later", "GPL-3.0-only", "GPL-3.0-or-later",
    "LGPL-2.1-only", "LGPL-2.1-or-later", "LGPL-3.0-only", "LGPL-3.0-or-later",
    "AGPL-3.0-only", "AGPL-3.0-or-later", "MPL-2.0", "EPL-2.0", "CDDL-1.0",
})


def _is_copyleft(license_id):
    lic = (license_id or "").strip()
    return lic in _COPYLEFT_LICENSES or lic.split(" WITH ")[0].strip() in _COPYLEFT_LICENSES


def _notice_text(spec):
    """The per-package NOTICE body, or "" when none is warranted.

    Written "when necessary": a package gets a NOTICE only if it declares an
    ``acknowledgment`` (a permissive license that requires attribution) or its
    license is copyleft (source-provision obligation). Otherwise upstream's own
    LICENSE/COPYING files in the tree already suffice and no NOTICE is added.
    """
    lic = (spec.get("license") or "").strip()
    ack = (spec.get("acknowledgment") or spec.get("acknowledgement") or "").strip()
    copyleft = _is_copyleft(lic)
    if not ack and not copyleft:
        return ""
    lines = [("%s %s" % (spec.get("package", ""), spec.get("version", ""))).strip()]
    if lic:
        lines.append("License: %s" % lic)
    if ack:
        lines += ["", ack]
    if copyleft:
        lines += ["", "This component is copyleft-licensed. The corresponding source "
                      "for the exact version built is available at:"]
        listed = False
        try:
            from bits_helpers.checksum import parse_entry
        except ImportError:
            parse_entry = None
        for s in spec.get("sources") or []:
            if isinstance(s, str):
                url = parse_entry(s)[0] if parse_entry else s
                if url:
                    lines.append("  %s" % url)
                    listed = True
        if spec.get("source") and spec.get("tag"):
            lines.append("  git: %s @ %s" % (spec["source"], spec["tag"]))
            listed = True
        if not listed:
            lines.append("  (see the source: field of this package's recipe)")
    return "\n".join(lines).rstrip() + "\n"


def _notice_block(spec):
    """Shell that writes ``$INSTALLROOT/NOTICE``, or "" when no NOTICE is needed.

    A quoted here-doc (``<<\\TERM``) so the body is emitted verbatim with no shell
    expansion (safe for arbitrary URLs/text); the fixed terminator is defused if it
    ever appears in the body.
    """
    text = _notice_text(spec)
    if not text:
        return ""
    term = "BITS_NOTICE_EOF"
    body = text.replace(term, term + "_")
    return 'cat > "$INSTALLROOT/NOTICE" <<\\%s\n%s%s\n' % (term, body, term)


def _source_mode(defaults_meta):
    """'git' or 'tar' — which source a recipe that declares BOTH should build from.

    A group chooses in its defaults (``system.source_mode`` or a ``source_mode``
    variable in defaults-release.sh); the default is ``tar`` (build from the cached
    tarball ``sources:``). ``BITS_SOURCE_MODE`` overrides for a one-off build.
    Recipes that declare only one source form are unaffected either way.
    """
    val = os.environ.get("BITS_SOURCE_MODE", "").strip().lower()
    if not val:
        system = (defaults_meta or {}).get("system", {}) or {}
        variables = (defaults_meta or {}).get("variables", {}) or {}
        val = str(system.get("source_mode")
                  or variables.get("source_mode") or "tar").strip().lower()
    return "git" if val == "git" else "tar"


def _apply_source_mode(spec, mode):
    """Disambiguate a recipe that declares BOTH a git source (``source:``/``tag:``)
    AND tarball ``sources:`` — keep only the selected form so the two build paths
    never both run. ``version:`` is untouched; in ``tar`` mode the git ``tag:`` is
    dropped so the tarball identity stays version-based (``tag`` defaults to
    ``version`` downstream). A recipe with only one form is left alone.
    """
    if "source" in spec and spec.get("sources"):
        if mode == "git":
            spec.pop("sources", None)          # build from git; ignore the tarballs
        else:                                  # tar (default)
            spec.pop("source", None)           # build from tarball; ignore git...
            spec.pop("tag", None)              # ...and its git ref (-> version)


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

  # ADR-0001 additive provenance: build_id / abi_tag / reuse_policy + a repro
  # block. Never enters the package hash and never alters behaviour (the simple
  # aliBuild case is unaffected); all reads are defensive so a minimal build
  # still produces a record. Stage 0: reuse_policy is always "strict" and
  # provenance "pure" (relaxed reuse, which sets "loose", lands in a later stage).
  from bits_helpers.provenance import (
    compute_build_id, compute_abi_tag, recipe_tools_ref,
  )
  # Contagious provenance (ADR-0001): a locally-built package is "loose" when its
  # dependency closure contains a package grafted from CVMFS (adopted by
  # name/build_id, not verified hash). Grafted packages are not built, so this
  # function only ever runs for local builds.
  def _closure_grafted():
    for _key in ("full_build_requires", "full_runtime_requires"):
      for _dep in specs[package].get(_key, ()):
        _ds = specs.get(_dep)
        if isinstance(_ds, dict) and _ds.get("from_cvmfs"):
          return True
    return False
  # A build is also loose if its closure decoupled a dependency via
  # untracked_requires: this package, or one below it, was hashed as if that
  # dependency never changed, so its identity no longer certifies its full input
  # closure. Contagious upward like grafted provenance.
  def _closure_untracked():
    if specs[package].get("untracked_requires"):
      return True
    for _key in ("full_build_requires", "full_runtime_requires"):
      for _dep in specs[package].get(_key, ()):
        _ds = specs.get(_dep)
        if isinstance(_ds, dict) and _ds.get("untracked_requires"):
          return True
    return False
  _untracked = list(specs[package].get("untracked_requires", ()))
  _provenance = "loose" if (_closure_grafted() or _closure_untracked()) else "pure"
  return json.dumps({
    "comment": args.annotate.get(package),
    "bits_version": __version__,
    "dist": {
      "commit": os.environ["BITS_DIST_HASH"],
    },
    "architecture": args.architecture,
    "defaults": args.defaults,
    "build_id": compute_build_id(specs, args),
    "abi_tag": compute_abi_tag(args),
    # The resolved CVMFS layout (install/module/views dirs), so publish and the
    # view client read the three tree paths from here, not by reloading defaults.
    # None when the profile declares no layout (additive; never hashed).
    "cvmfs_layout": getattr(args, "cvmfsLayout", None),
    # The group's CVMFS path templates (system.cvmfs_path_template / _modules_ /
    # _shared_), so the publish pipeline resolves the final path from the group's
    # own declaration instead of re-defining it. None when undeclared (additive;
    # never hashed — declared under system:, which is not part of any pkg hash).
    "cvmfs_templates": getattr(args, "cvmfsTemplates", None),
    "reuse_policy": getattr(args, "reusePolicy", "strict") or "strict",
    "provenance": _provenance,
    # Dependencies this package linked but excluded from its identity hash.
    "untracked_requires": _untracked,
    "repro": {
      "dist_commit": os.environ.get("BITS_DIST_HASH"),
      "recipe_tools": recipe_tools_ref(specs),
      "defaults": args.defaults,
    },
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


def write_failure_summary(work_dir, scheduler, arch):
  """Write a concise per-run failure summary for a --builders build.

  Logs are written under ``<work_dir>/LOGS/<arch>/`` so that concurrent builds
  of *different* platforms sharing one work area do not clobber each other.

  The full per-package error messages collected by the scheduler are verbose
  (log paths, environment, next-steps, ...), so a whole-stack failure produces
  an unreadable wall of text.  This distils, into
  ``<work_dir>/LOGS/<arch>/build-summary.log``:
    * each package that *directly* failed to build, with its log path and the
      proximate error excerpt (the matched error lines);
    * the count of packages skipped only because a dependency failed.
  Also writes the full, verbose per-action errors to
  ``<work_dir>/LOGS/<arch>/build-errors-full.log`` so there is a single combined log to
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
  # Per-architecture log directory: one shared work area may be used to build
  # different effective platforms, and these run-level logs are not otherwise
  # arch-scoped, so write them under LOGS/<arch>/ to avoid cross-platform
  # clobbering. Fall back to the work-dir root if the directory can't be made.
  log_dir = os.path.join(work_dir, "LOGS", arch or "")
  try:
    os.makedirs(log_dir, exist_ok=True)
  except OSError as exc:
    warning("Could not create log dir %s (%s); writing logs to %s instead",
            log_dir, exc, work_dir)
    log_dir = work_dir
  full_path = os.path.join(log_dir, "build-errors-full.log")
  try:
    with open(full_path, "w") as fh:
      for action, msg in errors.items():
        fh.write("* %s\n%s\n\n" % (action, _ansi.sub("", str(msg))))
  except OSError as exc:
    warning("Could not write full error log %s: %s", full_path, exc)
    full_path = None
  path = os.path.join(log_dir, "build-summary.log")
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
  # Reuse marker (shown even without --debug): "[from store]" when a remote store
  # is configured (tarball from the shared cache), "[cached]" for a local-only
  # tarball. Blank when the package is compiled.
  _reuse_tag = ((" [from store]" if getattr(syncHelper, "remoteStore", "")
                 else " [cached]") if cachedTarball else "")
  if args.builders==1:
    progress_msg = ("Unpacking %s@%s" + _reuse_tag) if cachedTarball else "Compiling %s@%s"
    if not cachedTarball and not args.debug:
      progress_msg += " (use --debug for full output)"
    progress = ProgressPrint(
      progress_msg %
      (spec["package"],
      args.develPrefix if "develPrefix" in args and spec["is_devel_pkg"] else spec["version"])
    )
  else:
    scheduler.log (
      (("Unpacking %s@%s" + _reuse_tag) if cachedTarball else
      "Compiling %s@%s (use --debug for full output)") %
      (spec["package"],
      args.develPrefix if "develPrefix" in args and spec["is_devel_pkg"] else spec["version"])
    )
  # Report progress (no-op unless running under gitlab-runner): this package is
  # now starting. defaults-release is excluded to match the planned total.
  if spec["package"] != "defaults-release":
    try:
      from bits_helpers import progress as _progress
      _progress.tick(spec["package"])
    except Exception:
      pass

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
  from bits_helpers import monitor as _bits_monitor
  _bits_monitor.note_build(p, getattr(args, "architecture", ""), True)
  try:
    if args.resourceMonitoring:
      err = run_monitor_on_command(build_command, "{}/{}.json".format(scriptDir, p), printer=progress)
    else:
      err = execute(build_command, printer=progress)
  finally:
    _bits_monitor.note_build(p, getattr(args, "architecture", ""), False)
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
  #
  # Repository packages (provides_repository: true, e.g. lcg.bits) exist only to
  # trigger recipe-repo loading during resolution; they carry no publishable
  # artifacts and must NOT be pushed to the store (nor published to CVMFS). Skip
  # their upload the same way local-only builds are skipped.
  if not spec["revision"].startswith("local") and not spec.get("provides_repository"):
    syncHelper.upload_symlinks_and_tarball(spec)
    # Log (info level) that a freshly built tarball was pushed to the write store.
    # Reused packages (cachedTarball) are already there and were marked
    # "[from store]" at build time, so they are skipped here.
    if getattr(syncHelper, "writeStore", "") and not spec.get("cachedTarball"):
      info("%s@%s [uploaded]", spec["package"], spec["version"])
    # Record the tarball's SHA-256 in the local integrity ledger so that
    # future recalls from the store can be verified against it.
    # Only active when --store-integrity is set (or store_integrity = true
    # in bits.rc); off by default for backward compatibility.
    if getattr(args, "storeIntegrity", False):
      from bits_helpers.store_integrity import record_tarball_checksum
      record_tarball_checksum(spec, args.workDir, args.architecture)

  # --aggressive-cleanup + a write store: the build script kept the tarball (it
  # would otherwise have skipped it) only so it could be uploaded above. Now that
  # the upload is done, reclaim the space — mirroring the in-build CAN_DELETE
  # behaviour for the no-write-store case. Safe if it was never created.
  if getattr(args, "aggressiveCleanup", False) and getattr(syncHelper, "writeStore", ""):
    from bits_helpers.utilities import resolve_store_path, effective_arch, ver_rev
    _arch = effective_arch(spec, args.architecture)
    _tar = os.path.join(args.workDir, resolve_store_path(_arch, spec["hash"]),
                        "{}-{}.{}.tar.gz".format(spec["package"], ver_rev(spec), _arch))
    try:
      os.remove(_tar)
    except OSError as err:
      # Best-effort cleanup: inability to remove this tarball must not fail the build.
      debug("Skipping aggressive cleanup for %s: %s", _tar, err)

  # ── Manifest recording ─────────────────────────────────────────────────────
  # Record the completed package in the incremental build manifest so that a
  # partial build still yields a useful record.  The outcome is:
  #   • "from_store"         — spec["cachedTarball"] was non-empty (we unpacked
  #                            a tarball recalled from the remote store).
  #   • "built_from_source"  — the build script ran; the tarball was produced
  #                            locally and (for non-local revisions) uploaded.
  # Accumulate reused-from-store hashes for the best-effort reuse beacon, fired
  # once at the end of the build (never per package, never blocking).
  if spec.get("cachedTarball") and getattr(syncHelper, "remoteStore", ""):
    # args._reusedHashes is pre-created single-threaded in doBuild; set.add is
    # safe under the GIL for concurrent builder threads.
    _rh = getattr(args, "_reusedHashes", None)
    if _rh is not None:
      _rh.add(spec["hash"])

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

  # Touch the sentinel so the cleanup command counts this package as recently
  # used, and record the package's disk usage in it (computed once, here) so
  # cleanup never has to walk the install tree to size this package.
  try:
    from bits_helpers.cleanup import touch_sentinel as _touch_sentinel
    from bits_helpers.utilities import ver_rev as _ver_rev
    _touch_sentinel(args.workDir, args.architecture, spec["package"], _ver_rev(spec),
                    record_size=True)
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

  # Pre-create the reuse-beacon hash set here (single-threaded) so the parallel
  # per-package finalisers only ever .add() to it — no lazy-init race.
  args._reusedHashes = set()

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
    # Resolve the `variables:` block into a flat map. Entries may be gated
    # (`name: {value: V, when: MATCHER}`) on CLI flavours, the predefined
    # architecture variables (osx/linux/arm64/...), or earlier entries; the CLI
    # --flavour values are folded in as inputs. The resulting map feeds the
    # (?NAME) conditional matchers that gate package requires and %(NAME)s recipe
    # templating. CLI flavours override a defaults entry of the same name.
    flavours = getattr(args, "flavours", None) or {}
    meta["variables"] = resolve_variables(
        meta.get("variables"), flavours, args.architecture, args.defaults)
    # Resolve the release LABEL and write it back into `release`, so that
    # %(release)s (which the defaults feed to the lcg.bits branch override)
    # follows the same rule as the CVMFS {release} slot: an explicit non-trunk
    # release: wins, else the working-dir branch, else "main". This is what makes
    # a build on a recipe branch fetch the matching lcg.bits branch, while the
    # default/main line keeps building lcg.bits `main` exactly as before.
    # Only groups that OPT IN by declaring a `release` are touched — a group using
    # a different convention (e.g. %(lcgversion)s) is left untouched, so no extra
    # variable is introduced and its package hashes are unchanged.
    from bits_helpers.cvmfs_layout import (
        resolve_release as _resolve_release, _declared_release as _declared)
    if isinstance(meta.get("variables"), dict) and _declared(meta):
      meta["variables"]["release"] = _resolve_release(meta, branch_basename)
    # Flavours are ALSO exported into the build environment + package hash (via
    # the `env:` map, which becomes the defaults-release env every package
    # depends on), exactly as before.
    if flavours:
      from collections import OrderedDict as _OD
      # An empty `env:` block parses to None, so setdefault would keep it None.
      if not isinstance(meta.get("env"), dict):
        meta["env"] = _OD()
      for _k, _v in flavours.items():
        meta["env"][_k] = _v
    # init.sh-from-modules (the default) publishes a build-mode marker through the
    # defaults-release env. Routing it here is deliberate: the defaults env is
    # (a) folded into every package's hash, so the mode yields a distinct,
    # reproducible identity rather than silently colliding with legacy artifacts,
    # and (b) exported into the build environment before each recipe is sourced, so
    # bits_pythonpath_from_deps / CMakeRecipe can gate their now-redundant
    # reconstruction on it. In legacy mode (--legacy-initdotsh) nothing is added,
    # so its hashes are byte-identical to the pre-modules default (alidist tarballs
    # stay reusable).
    if getattr(args, "initdotshFromModules", False):
      from collections import OrderedDict as _OD
      # An empty `env:` block parses to None, so setdefault would keep it None.
      if not isinstance(meta.get("env"), dict):
        meta["env"] = _OD()
      meta["env"]["BITS_INITDOTSH_FROM_MODULES"] = "1"
    return meta, body
  # Deriving the dependency env from the dependencies' modulefiles is the default.
  # --legacy-initdotsh (CLI) or BITS_LEGACY_INITDOTSH=1 (the environment — the
  # aliBuild wrapper sets it) selects the legacy build-time init.sh, which injects
  # nothing above and so hashes byte-identically to the pre-modules default (bits
  # can still reuse alidist tarballs). Resolved here, before parseDefaults runs the
  # reader closure above that reads args.initdotshFromModules.
  if getattr(args, "initdotshFromModules", None) is None:
    _legacy_env = os.environ.get("BITS_LEGACY_INITDOTSH", "").strip().lower() in (
      "1", "true", "yes", "on")
    args.initdotshFromModules = not _legacy_env
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
  #   * reuse deployed -> --remote-store = cvmfs://<cvmfs_dir>  (with --reuse-cvmfs)
  # Docker builds are relocatable by default (built in WORK_DIR with padded
  # placeholders + relocate-me.sh), so the tarballs can be reused anywhere and are
  # relocated into CVMFS on publish. Pass --cvmfs-prefix explicitly only for an
  # in-place, non-relocatable build (skips relocation on publish, but the result
  # is NOT reusable outside that exact CVMFS path).
  from bits_helpers.cvmfs_layout import resolve_cvmfs_layout
  _cvmfs = resolve_cvmfs_layout(defaultsMeta, args.architecture)
  # Stash the resolved layout so create_provenance_info can record it in each
  # package's .meta.json — that way publish (targets) and the view client
  # (views_dir) read the three tree paths from the package metadata without
  # re-loading the defaults profile.
  args.cvmfsLayout = _cvmfs
  if _cvmfs:
    info("CVMFS layout: install=%s  modules=%s  views=%s",
         _cvmfs["install_path"], _cvmfs["module_path"], _cvmfs["views_path"])
    if getattr(args, "reuseCvmfs", False) and not args.remoteStore and _cvmfs["cvmfs_dir"]:
      args.remoteStore = "cvmfs://" + _cvmfs["cvmfs_dir"]
      info("Reusing deployed components: --remote-store %s", args.remoteStore)

  # Build-host policy knobs live under a single `system:` entry in defaults.
  # These control *how* the build runs (network, CPU) — not *what* it produces —
  # so, unlike `env:`, they are NOT folded into any package hash and changing
  # them never triggers a rebuild. For backward compatibility a bare top-level
  # key is still honoured (system: wins).
  _system = defaultsMeta.get("system", {}) or {}
  def _system_opt(key, top_default):
    if key in _system:
      return _system[key]
    return defaultsMeta.get(key, top_default)

  # CVMFS publish-path templates declared by the group under system: (never
  # affect a package hash), recorded in .meta.json.cvmfs_templates so the publish
  # pipeline and the pre-build reserve (`bits cvmfs-path`) resolve the same path.
  # BITS_CVMFS_PREFIX (the community's authoritative root, supplied by bits-console)
  # is the ROOT and OVERRIDES any recipe system.prefix — the prefix is an auth
  # boundary, so a recipe cannot redirect the publish into another group's tree; a
  # recipe prefix is only a local-dev fallback. The recipe still owns the LAYOUT.
  from bits_helpers.cvmfs_layout import (
      resolve_cvmfs_templates, resolve_release, path_release, bake_release)
  args.cvmfsTemplates = resolve_cvmfs_templates(
      defaultsMeta, os.environ.get("BITS_CVMFS_PREFIX") or None)
  # {release} is a build-level constant — the release LABEL resolved from the same
  # inputs that pick the lcg.bits branch (explicit non-trunk release: → recipe
  # branch → "main"). Bake its PATH form into the recorded templates so the publish
  # pipeline (and each .meta.json) carry a concrete slot; a trunk/main release
  # collapses the {release}/ segment out entirely (pre-release layout preserved).
  # `bits cvmfs-path` bakes the identical value for the reserve.
  # {family}/{pkg}/{tag}/{platform} stay as tokens (resolved per package).
  if args.cvmfsTemplates:
    _release_path = path_release(resolve_release(defaultsMeta, branch_basename))
    for _k in ("path", "modules", "shared", "prefix", "user_prefix"):
      if args.cvmfsTemplates.get(_k):
        args.cvmfsTemplates[_k] = bake_release(args.cvmfsTemplates[_k], _release_path)

  # Global build-time network policy for the recipe sandbox. Precedence:
  #   explicit --sandbox-network  >  defaults system.sandbox_network  >  "on".
  # A recipe's own sandbox_network field still overrides this per package
  # (handled in sandbox.wrap_build_command). YAML parses bare on/off as bools,
  # so normalise to the "on"/"off" strings the sandbox layer expects.
  if getattr(args, "sandboxNetwork", None) is None:
    _dn = _system_opt("sandbox_network", "on")
    if isinstance(_dn, bool):
      _dn = "on" if _dn else "off"
    args.sandboxNetwork = str(_dn).strip().lower()

  # CPU oversubscription factor for the per-builder -j share. Precedence:
  #   explicit --oversubscribe  >  defaults system.build_oversubscribe  >  1.0.
  # Memory budgeting is unaffected (see effective_jobs).
  if getattr(args, "oversubscribe", None) is None:
    try:
      args.oversubscribe = float(_system_opt("build_oversubscribe", 1.0))
    except (TypeError, ValueError):
      args.oversubscribe = 1.0

  # Binary store URL from the active defaults (system.remote_store). Under
  # system: it is NOT hashed, so a stack-wide store never invalidates package
  # hashes (unlike env:). Precedence: CLI/bits.rc/env > system.remote_store >
  # built-in arch default. '::rw' shorthand is honoured as on the CLI.
  if (not getattr(args, "remoteStoreExplicit", False)
      and not getattr(args, "no_remote_store", False)):
    _rs = _system_opt("remote_store", None)
    if _rs:
      _rs = str(_rs).strip()
      if _rs.endswith("::rw"):
        _rs = _rs[:-4]
        if not getattr(args, "writeStore", ""):
          args.writeStore = _rs
      args.remoteStore = _rs

  # Trusted-reuse policy from the active defaults (system:), non-hashed. Lets a
  # community turn on signed reuse + point at its common manifest once, so a bare
  # `bits build` gets it. Precedence: CLI > system:.
  def _truthy(v):
    return str(v).strip().lower() in ("1", "true", "yes", "on") if v is not None else False
  if not getattr(args, "trustManifest", None):
    _tm = _system_opt("trust_manifest", None)
    if _tm:
      args.trustManifest = str(_tm).strip()
  # Signed reuse is ON by default: reusing an untrusted remote store is unsafe.
  # Precedence: explicit CLI flag (--require-signed-reuse / --no-require-signed-reuse,
  # True/False) > system: require_signed_reuse > built-in default (True). When on
  # but the store has no signed manifest, the reuse gate degrades to a rebuild
  # (see trusted_reuse_index), so this never breaks an uncertified store.
  if getattr(args, "requireSignedReuse", None) is None:
    _rsr = _system_opt("require_signed_reuse", None)
    args.requireSignedReuse = _truthy(_rsr) if _rsr is not None else True
  if not getattr(args, "trustGroups", None):
    _tg = _system_opt("trust_groups", None)
    if _tg:
      args.trustGroups = str(_tg).strip()
  # If signed reuse is on but no manifest was given, derive it from the (http[s])
  # read store. The signed manifest is partitioned by architecture, so a node
  # fetches its own arch file plus the always-shared one:
  #   <store>/<prefix>-<arch>.json , <store>/<prefix>-shared.json
  # (`bits certify` publishes exactly these). So `require_signed_reuse: true`
  # alone is enough.
  if getattr(args, "requireSignedReuse", False) and not getattr(args, "trustManifest", None):
    # Endpoint precedence matches the S3 client: --s3-endpoint > env > CERN S3.
    _ep = (getattr(args, "s3Endpoint", None)
           or os.environ.get("BITS_S3_ENDPOINT_URL") or os.environ.get("S3_ENDPOINT_URL")
           or os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL"))
    _srcs = derive_trust_manifest_srcs(
        getattr(args, "remoteStore", ""),
        _system_opt("trust_manifest_prefix", "MANIFESTS/common-manifest"),
        str(getattr(args, "architecture", "") or ""), _ep)
    if _srcs:
      args.trustManifest = ",".join(_srcs)
      info("--require-signed-reuse: trust manifests derived from store -> %s",
           args.trustManifest)

  # The final target builds alone (every other package is one of its
  # already-finished dependencies), so the per-builder -j split needlessly
  # starves the largest compile of the run. Let it use the full -j; the memory
  # cap (mem_per_job) still applies. Non-hashed build-host policy like the knobs
  # above — JOBS never feeds a package hash, so this changes wall time only.
  # Precedence: --unleash-final/--no-unleash-final > system.build_unleash_final > on.
  if getattr(args, "unleashFinal", None) is None:
    _uf = _system_opt("build_unleash_final", True)
    args.unleashFinal = _uf if isinstance(_uf, bool) \
        else str(_uf).strip().lower() in ("1", "true", "yes", "on")

  # Critical-path scheduling order for --builders (non-hashed; affects dispatch
  # order only, never build output). Precedence: explicit flag >
  # system.build_critical_path_schedule > on.
  if getattr(args, "criticalPathSchedule", None) is None:
    _cp = _system_opt("build_critical_path_schedule", True)
    args.criticalPathSchedule = _cp if isinstance(_cp, bool) \
        else str(_cp).strip().lower() in ("1", "true", "yes", "on")

  # Relaxed CVMFS reuse policy (ADR-0001). Non-hashed build-host policy, like
  # the two above. Precedence: explicit --reuse-policy/--reuse-base  >  defaults
  # system.reuse_policy / reuse_base  >  strict / none. Default strict keeps the
  # simple aliBuild case bit-for-bit unchanged.
  if getattr(args, "reusePolicy", None) is None:
    args.reusePolicy = str(_system_opt("reuse_policy", "strict")).strip().lower()
  if args.reusePolicy not in ("strict", "relaxed"):
    args.reusePolicy = "strict"
  if getattr(args, "reuseBase", None) is None:
    args.reuseBase = _system_opt("reuse_base", "") or ""
  # Publish guard: relaxed builds are loose-provenance (their closure includes
  # unverified deployed binaries) and must never reach a write store / publish
  # pipeline. Refuse early and clearly.
  if args.reusePolicy == "relaxed" and (getattr(args, "writeStore", "") or getattr(args, "pipeline", False)):
    dieOnError(True,
               "--reuse-policy relaxed produces loose-provenance artifacts that cannot be "
               "published. Drop --write-store/--pipeline, or rebuild with --reuse-policy strict.")

  # syncHelper is constructed after defaults loading so that it receives the
  # (potentially combined) architecture string.
  syncHelper = remote_from_url(args.remoteStore, args.writeStore, args.architecture,
                               args.workDir, getattr(args, "insecure", False),
                               s3_endpoint=getattr(args, "s3Endpoint", None),
                               s3_access_key=getattr(args, "s3AccessKey", None),
                               s3_secret_key=getattr(args, "s3SecretKey", None),
                               s3_region=getattr(args, "s3Region", None),
                               s3_addressing_style=getattr(args, "s3AddressingStyle", None))

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
  # --brew lets prefer_system_check scripts run `brew install <formula>` when a
  # Homebrew-sourced system package is missing (macOS dev platform). The checks
  # run unsandboxed during resolution and read this from the environment; the
  # sandboxed build phase only symlinks the (now-present) Homebrew prefix.
  if getattr(args, "brew", False):
    extra_env["BITS_BREW"] = "1"

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
  # Also seed with the bootstrap org-pointer recipe's own requires (e.g.
  # alice.bits.sh ``requires: [alidist.bits]``): the recipe repo we just
  # bootstrapped depends on those sibling provider repos for its base recipes,
  # but they are not build-graph dependencies of the requested target, so the
  # walk would otherwise never reach them.
  defaults_provider_seed = (
    list(defaultsMeta.get("requires", []))
    + list(defaultsMeta.get("build_requires", []))
    + list(getattr(args, "_bootstrap_provider_requires", []) or [])
  )

  provider_dirs = fetch_repo_providers_iteratively(
    packages          = packages + defaults_provider_seed,
    config_dir        = args.configDir,
    work_dir          = workDir,
    reference_sources = args.referenceSources,
    fetch_repos       = args.fetchRepos,
    taps              = taps,
    provider_policy   = getattr(args, "provider_policy", {}),
    overrides         = overrides,
    defaults          = args.defaults,
    default_vars      = defaultsMeta.get("variables"),
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

    # Relaxed CVMFS graft callback (ADR-0001). Active only under --reuse-policy
    # relaxed with a cvmfs:// remote store and a --reuse-base build_id; None in
    # every other case → strict behaviour, no graft (simple aliBuild path
    # unaffected). Uses the combined architecture (args.architecture) — the arch
    # recorded in the deployed packages' .meta.json — not raw_architecture.
    _cvmfs_match = None
    if getattr(args, "reusePolicy", "strict") == "relaxed":
      _base = getattr(args, "reuseBase", "") or ""
      _store = args.remoteStore or ""
      if not _base:
        warning("--reuse-policy relaxed needs --reuse-base <build_id> (or defaults "
                "reuse_base:); no packages will be grafted.")
      elif not _store.startswith("cvmfs://"):
        warning("--reuse-policy relaxed needs a cvmfs:// --remote-store "
                "(or --reuse-cvmfs); no packages will be grafted.")
      else:
        from bits_helpers.cvmfs_reuse import graftable_match
        _store_root = re.sub("^cvmfs://", "", _store)
        _build_local = set(getattr(args, "buildLocal", []) or [])
        def _cvmfs_match(spec, _root=_store_root, _bid=_base,
                         _arch=args.architecture, _bl=_build_local):
          if spec["package"] in _bl:
            return None
          return graftable_match(spec["package"], _arch, _bid, _root)

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
                     defaults_meta           = defaultsMeta,
                     performCvmfsMatch       = _cvmfs_match)

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
    # Expand %(version)s etc. in the tag for this display only. Per-spec tag
    # resolution (resolve_tag, further below) hasn't run yet here, so a templated
    # tag like "v%(version)s" would otherwise print raw. strict=False makes it
    # best-effort: unknown placeholders are left as-is and it never aborts.
    def _display_ref(pkg):
      spec = specs[pkg]
      return resolve_spec_data(spec, str(spec.get("tag", spec.get("version", "?"))),
                               args.defaults, strict=False)
    if len(builtPackages) > 1:
      # One row per package as (name/ref, source); source is the recipe origin
      # "repo@commit" (already includes its own '@'), or the devel marker. Align
      # the source column so a long build order is easy to scan, e.g.:
      #   - CMake/3.30.6   [lcg.bits@c4087be6c3]
      #   - Boost/1.90.0   [lcg.bits@c4087be6c3]
      #   - MyPkg/feature  [development package]
      def _build_row(pkg):
        label = "{}/{}".format(pkg, _display_ref(pkg))
        source = ("development package" if pkg in develPkgs
                  else specs[pkg].get("recipe_source", "?"))
        return label, source
      _rows = [_build_row(x) for x in builtPackages if x != "defaults-release"]
      _w = max((len(label) for label, _ in _rows), default=0)
      banner("Packages will be built in the following order:\n - %s",
             "\n - ".join("{}  [{}]".format(label.ljust(_w), source)
                          for label, source in _rows))
    else:
      banner("No dependencies of package %s to build.", buildOrder[-1])

    # Tell the progress reporter how many packages will be built, so it can post
    # a percentage to the GitLab commit status as each one starts (no-op unless
    # running under gitlab-runner). defaults-release is excluded to match the
    # plan shown above.
    try:
      from bits_helpers import progress as _progress
      _progress.set_total(len([x for x in builtPackages if x != "defaults-release"]))
    except Exception:
      pass


  if develPkgs:
    banner("You have packages in development mode (%s).\n"
           "This means their source code can be freely modified under:\n\n"
           "  %s/<package_name>\n\n"
           "bits does not automatically update such packages to avoid work loss.\n"
           "In most cases this is achieved by doing in the package source directory:\n\n"
           "  git pull --rebase\n",
           ", ".join(develPkgs),
           os.getcwd())

  # Packages pulled in by some recipe via `untracked_requires`: linked at runtime
  # but excluded from their consumers' identity hash, so editing one does not
  # rebuild the stack above it. List them like development packages, and warn if a
  # target has no stable install label — a reused consumer references it by
  # <pkg>/<version-revision>, so that path must not move when the package changes.
  untrackedTargets = sorted({d for s in specs.values()
                             for d in s.get("untracked_requires", ()) if d in specs})
  if untrackedTargets:
    banner("Untracked dependencies (%s).\n"
           "These are linked at runtime but excluded from the identity hash of the\n"
           "packages that require them, so editing one does NOT rebuild the packages\n"
           "above it. Builds whose closure includes one are marked loose-provenance\n"
           "in .meta.json. You are responsible for keeping them ABI-compatible.",
           ", ".join(untrackedTargets))
    for t in untrackedTargets:
      if "force_revision" not in specs[t]:
        warning("Untracked dependency %s has no stable install label "
                "(force_revision): its install path moves when it changes, so "
                "already-built consumers keep linking the previous build. Set "
                "`force_revision:` on %s to keep <%s>/<version-revision> stable.",
                t, t, t)

  # A recipe may declare BOTH a git source (source:/tag:) and cached tarball
  # sources (sources:); the group's source_mode (defaults-release.sh) picks which
  # one every such recipe builds from. Applied before scm setup so the unused form
  # is gone. Devel packages always build from their local checkout, so they are
  # exempt (their source is forced just below).
  _smode = _source_mode(defaultsMeta)
  for pkg, spec in specs.items():
    spec["is_devel_pkg"] = pkg in develPkgs
    if spec["is_devel_pkg"]:
      spec["source"] = str(Path.cwd() / pkg)
    else:
      _apply_source_mode(spec, _smode)

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
        _auto_stats = autoload_stats_path(workDir, args.architecture)
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
                          parallelDownloads=max(1, getattr(args, "parallelDownloads", 2)),
                          criticalPath=getattr(args, "criticalPathSchedule", True))

    # Opt-in build-host monitor (--monitor / system 'monitor'): a best-effort
    # background sampler of this runner (load / memory / build filesystem / sw
    # size) and the building packages, pushed to --monitor-url. It never blocks
    # or fails the build and is stopped at process exit. Runtime only — no hash
    # impact.
    _mon_url = (getattr(args, "monitorUrl", None) or os.environ.get("METRICS_URL")
                or _system_opt("monitor_url", None))
    _mon_on = getattr(args, "monitor", None)
    if _mon_on is None:
      _sys_mon = _system_opt("monitor", None)
      # Default ON when a metrics endpoint is configured (e.g. $METRICS_URL under
      # bits-console) so no CLI flag is needed — a plain `bits build` with
      # METRICS_URL set just works, and an older bits without --monitor is
      # unaffected. Explicit --monitor/--no-monitor or system 'monitor' still win.
      _mon_on = _truthy(_sys_mon) if _sys_mon is not None else bool(_mon_url)
    if _mon_on and _mon_url:
      try:
        from bits_helpers import monitor as _bits_monitor
        _bits_monitor.start_monitor(
            url=_mon_url,
            instance=getattr(args, "monitorInstance", None) or _system_opt("monitor_instance", None),
            interval=float(getattr(args, "monitorInterval", None) or _system_opt("monitor_interval", 15) or 15),
            disk_interval=float(getattr(args, "monitorDiskInterval", None) or _system_opt("monitor_disk_interval", 60) or 60),
            sw_dir=abspath(args.workDir))
        import atexit as _atexit
        _atexit.register(_bits_monitor.stop_monitor)
        info("build-host monitor: pushing per-runner metrics to %s", _mon_url)
      except Exception as _mon_err:  # pylint: disable=broad-except
        debug("build-host monitor not started: %s", _mon_err)

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
  #
  # Sentinels are only ever created at two fixed, known depths (see
  # _prefetch_package / bits_helpers.download):
  #   - source archives:  SOURCES/<package>/<version>/<file>.downloading
  #   - prebuilt tarballs: TARS/<arch>/store/<hash[:2]>/<hash>.downloading
  #     (resolve_store_path)
  # Walking the WHOLE workDir would also descend INSTALLROOT and BUILD -- the
  # entire installed stack, tens of thousands of files -- adding a long,
  # pointless stat() storm before the first build (very noticeable on macOS).
  # Because the depths are fixed, we don't even need a recursive walk: two
  # depth-bounded (non-recursive) glob patterns match exactly the directories
  # where sentinels can appear and nothing else.
  _sentinel_globs = [
    os.path.join(workDir, "SOURCES", "*", "*", "*.downloading"),
    os.path.join(workDir, "TARS", "*", "store", "*", "*.downloading"),
  ]
  for _pattern in _sentinel_globs:
    for _s in glob(_pattern):
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

      # ADR-0005 P2c: if the local version-link scan found NO reuse candidate,
      # fall back to the revision history recorded by the certified common
      # manifest and the S3 rev-index markers. This is what lets the reuse/assign
      # decision survive once the version links are dropped (Phase 2d): the fold
      # can then supply the reuse candidate (fetched by hash later) and reserve
      # the revision numbers already taken remotely.
      #
      # We deliberately fold ONLY when the scan is empty-handed:
      # - when the scan already found a candidate we reuse it and never consult
      #   busyRevisions, so folding could not change the outcome — skipping keeps
      #   the decision (and the per-package S3 read) identical to before whenever
      #   the local links are present;
      # - devel packages are always built locally and never appear in the remote
      #   manifest/markers.
      if candidate is None and not spec["is_devel_pkg"]:
        try:
          candidate, busyRevisions = _fold_revision_records(
            _revision_index_records(spec, spec_arch, args, workDir, syncHelper),
            spec, candidate, busyRevisions, revisionPrefix)
        except Exception as exc:
          # The rev-index is a best-effort supplement; never let a manifest/marker
          # read (network, S3, parse) abort or misdirect a build. Fall back to the
          # local scan's result.
          debug("rev-index fold failed for %s: %s", spec["package"], exc)

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

    # ADR-0005: rebuild this package's local version link from the graph now that
    # its revision and hash are final. The version link
    # (TARS/<eff>/<pkg>/<pkg>-<verrev>.<eff>.tar.gz -> the content-addressed
    # store) used to come from the S3 version-link object — written by the upload
    # for freshly-built packages, fetched by fetch_symlinks for reused ones. With
    # the store keeping only hash-keyed tarballs (Phase 2d) it is reconstructed
    # locally instead, so the single local artefact the CVMFS publish step reads
    # is present for BOTH built and reused packages.
    #
    # Done for every package regardless of makeflow: makeflow's tar_template.sh
    # only writes the link for FRESHLY-BUILT packages, so a makeflow *reused*
    # package would otherwise get no local link now that the S3 version link is
    # gone (upload is hash-only and fetch_symlinks finds nothing). Recreating it
    # here is idempotent for the built case (same symlink, same target).
    # Best-effort: never abort the build over a link (a genuine miss surfaces as a
    # publish skip, exactly as a system-provided package does).
    try:
      create_version_link(spec, args.architecture, workDir)
    except Exception as exc:
      debug("Could not reconstruct version link for %s: %s", spec["package"], exc)

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
      # Tarballs already present before the remote fetch are local build-node
      # artifacts (ultimately trusted); ones that appear only after fetch came
      # from the remote store and are subject to --require-signed-reuse.
      # A prefetch worker (above) may already have pulled the REMOTE tarball into
      # this directory, and _wait_for_sentinel just blocked until it finished --
      # so subtract whatever it downloaded, or the gate would exempt it.
      _preFetchTars = (set(glob(os.path.join(tar_hash_dir, "*gz")))
                       - set(spec.get("prefetched_tarballs", ())))
      syncHelper.fetch_tarball(spec)
      tarballs = [t for t in glob(os.path.join(tar_hash_dir, "*gz"))
                  if os.path.isfile(t)]  # skip dangling symlinks
      spec["cachedTarball"] = _select_cached_tarball(
        tarballs, spec, effective_arch(spec, args.architecture))
      debug("Found tarball in %s" % spec["cachedTarball"]
            if spec["cachedTarball"] else "No cache tarballs found")
      # Verify the recalled tarball against the local integrity ledger.
      # Only active when --store-integrity is set (or store_integrity = true
      # in bits.rc); off by default for backward compatibility.
      if spec["cachedTarball"] and getattr(args, "storeIntegrity", False):
        from bits_helpers.store_integrity import verify_tarball_checksum
        verify_tarball_checksum(spec, workDir, args.architecture, spec["cachedTarball"])
      # Trusted-reuse gate (--require-signed-reuse): a tarball recalled from the
      # remote store is reused only if a verified signed manifest vouches for it
      # (hash present AND sha256 matches). Otherwise fall through to a rebuild;
      # a sha256 mismatch is fatal (tampering).
      if (spec["cachedTarball"] and getattr(args, "requireSignedReuse", False)
          and spec["cachedTarball"] not in _preFetchTars):
        _idx = trusted_reuse_index(args, workDir)
        _sha = _idx.get(spec["hash"])
        if _sha is None:
          warning("Trusted reuse: %s@%s not vouched for by the signed manifest; "
                  "discarding remote tarball and rebuilding.",
                  spec["package"], spec["hash"])
          spec["cachedTarball"] = ""
        else:
          _actual = compute_checksum_file(spec["cachedTarball"])
          dieOnError(_actual != _sha,
                     "INTEGRITY FAILURE: remote tarball %s does not match the "
                     "signed manifest.\n  Expected: %s\n  Actual:   %s\n  "
                     "Do NOT use it." % (os.path.basename(spec["cachedTarball"]),
                                         _sha, _actual))
          debug("Trusted reuse: %s@%s verified against signed manifest",
                spec["package"], spec["hash"])

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
      "initdotsh_deps": generate_initdotsh(p, specs, args.architecture, workDir=init_workDir, post_build=False,
                                           from_modules=getattr(args, "initdotshFromModules", False)),
      "initdotsh_full": generate_initdotsh(p, specs, args.architecture, workDir=init_workDir, post_build=True,
                                           from_modules=getattr(args, "initdotshFromModules", False)),
      "develPrefix": develPrefix,
      "workDir": workDir,
      "configDir": abspath(args.configDir),
      "incremental_recipe": spec.get("incremental_recipe", ":"),
      "requires": " ".join(spec["requires"]),
      "build_requires": " ".join(spec["build_requires"]),
      "runtime_requires": " ".join(spec["runtime_requires"]),
      "BITS_HOOK_PARAMS": hook_params_locals,
      "notice_block": _notice_block(spec),
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
      # Whether a write store will need this package's tarball for upload. Under
      # --aggressive-cleanup the build script otherwise skips creating the tarball
      # (to save space), but doFinalSync still needs it to upload — so keep it when
      # a write store is configured. The space is reclaimed after upload below.
      ("BITS_HAS_WRITE_STORE", "1" if getattr(syncHelper, "writeStore", "") else ""),
      ("COMMIT_HASH", short_commit_hash(spec)),
      ("DEPS_HASH", spec.get("deps_hash", "")),
      ("DEVEL_HASH", spec.get("devel_hash", "")),
      ("DEVEL_PREFIX", develPrefix),
      ("BUILD_FAMILY", spec["build_family"]),
      ("GIT_COMMITTER_NAME", "unknown"),
      ("GIT_COMMITTER_EMAIL", "unknown"),
      ("INCREMENTAL_BUILD_HASH", spec.get("incremental_hash", "0")),
      # The final (top-level) package builds alone once its dependencies finish,
      # so give it the full -j instead of the per-builder share (builders=1).
      # mainPackage is buildOrder[-1] (in --only-deps it is popped off and never
      # built, so nothing matches and nothing is unleashed). No-op for
      # --builders == 1, keeping the common path byte-identical.
      ("JOBS", str(effective_jobs(
        args.jobs, spec,
        builders=(1 if (getattr(args, "unleashFinal", True)
                        and args.builders > 1
                        and spec["package"] == mainPackage)
                  else args.builders),
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
      # Tripwire: mount the shared SOURCES tree read-only so a recipe that mutates
      # its source in place (in-tree patching, codegen, in-tree downloads) fails
      # loudly with EROFS instead of silently poisoning the reused tree for the
      # next build/arch. On by default (every bits recipe-tools recipe builds from
      # its private rsync'd copy, so a correct recipe is unaffected); disable with
      # BITS_READONLY_SOURCES=0 if a not-yet-migrated recipe needs to write back.
      # It overlays the read-write workdir mount and the more-specific :ro mount
      # wins. No chmod of the host tree.
      _src_dir = os.path.join(abspath(args.workDir), "SOURCES")
      _ro_enabled = os.environ.get("BITS_READONLY_SOURCES", "1").strip().lower() \
                    not in ("0", "false", "no", "off", "")
      _ro_sources = ("-v %s:%s/SOURCES:ro " % (quote(_src_dir), container_workDir)
                     if _ro_enabled and os.path.isdir(_src_dir)
                     else "")
      # --user $(id -u):$(id -g) runs as the host uid, which usually has no
      # passwd entry inside the image, so $HOME is unset and expands to "" — any
      # recipe that writes under ~/ then targets the filesystem root and fails
      # (e.g. gflags' CMake package registry -> //.cmake, IJulia's kernelspec ->
      # /.local). Point HOME at the container-local /tmp (world-writable, and per
      # container so concurrent builds never collide). HOME is not a hash input,
      # so this changes no package hash.
      build_command = (
        "docker run --rm --entrypoint= --user $(id -u):$(id -g) "
        "{platformArg}"
        "-v {workdir}:{container_workDir} {roSources}-v{configDir}:/pkgdist.bits:ro "
        "-v {scriptDir}/build.sh:/build.sh:ro "
        "-v {bits_dir}:/bits "
        "{mirrorVolume} {develVolumes} {additionalEnv} {additionalVolumes} "
        "-e HOME=/tmp -e WORK_DIR_OVERRIDE={container_workDir} -e BITS_CONFIG_DIR_OVERRIDE=/pkgdist.bits {extraArgs} {image} bash -ex /build.sh"
      ).format(
        platformArg="--platform %s " % quote(_docker_platform) if _docker_platform else "",
        roSources=_ro_sources,
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
    _run_t0 = time.monotonic()
    try:
      scheduler.run()
    finally:
      # Always stop the straggler-renice watchdog, even if run() raised.
      if getattr(scheduler, "renice_watchdog", None) is not None:
        scheduler.renice_watchdog.stop()
    _run_wall = time.monotonic() - _run_t0
    # Refresh the self-tuning resource-stats file from this run's monitor traces
    # so the next --builders invocation can schedule with up-to-date estimates (P3).
    # Also estimate CPU utilisation and, when there is headroom, record/print a
    # suggestion for --builders / --oversubscribe.
    _tuning = None
    if args.resourceMonitoring and monitoredDirs:
      try:
        from bits_helpers.build_stats import aggregate_and_write, tuning_report, default_stats_path
        _tuning = tuning_report(monitoredDirs, _run_wall, args.builders, args.jobs,
                                getattr(args, "oversubscribe", 1.0) or 1.0)
        aggregate_and_write(workDir, monitoredDirs, tuning=_tuning, arch=args.architecture)
      except Exception as exc:  # pylint: disable=broad-except
        warning("Could not update build resource stats: %s", exc)
    for (action, error) in scheduler.errors.items():
      info("* The action \"{}\" was not completed successfully because {}".format(action, error))
    # Write a concise failure summary plus a combined full error log, and tell
    # the user where to find them and the individual per-package logs.
    _summary_path, _full_path = write_failure_summary(workDir, scheduler, args.architecture)
    if _summary_path or _full_path:
      info("=" * 70)
      info("Build finished with errors. Where to look:")
      if _summary_path:
        info("  Summary (start here):   %s", _summary_path)
      if _full_path:
        info("  Full error log:         %s", _full_path)
      info("  Per-package build logs: %s/BUILD/<package>-latest/log", workDir)
      info("=" * 70)
    # End-of-run resource-tuning hint. Only on a clean build — the utilisation
    # numbers are meaningless for a partial/failed run. The full report is in
    # bits_build_stats.json under "tuning".
    if _tuning and _tuning.get("headroom") and not scheduler.brokenJobs:
      banner("Resource tuning (recorded in %s):\n  %s",
             default_stats_path(workDir, args.architecture), _tuning["recommendation"])
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
      # When --defaults qualified the architecture (qualify_arch), the install
      # tree lives under the combined arch string, but `bits enter` auto-detects
      # only the raw base arch -- so the suggested command must pass -a
      # explicitly, otherwise it would look in the wrong sw/<arch>.
      if args.architecture != raw_architecture:
        _arch_flag = "-a %s " % args.architecture
        _arch_note = (
            "\n\n(This build used the defaults-qualified architecture "
            f"`{args.architecture}'; pass it with -a as above, or persist it "
            f"with `export BITS_ARCHITECTURE={args.architecture}'.)")
      else:
        _arch_flag, _arch_note = "", ""
      banner(f"Build of {mainPackage} successfully completed on `{socket.gethostname()}'.\n"
             "Your software installation is at:"
             f"\n\n  {abspath(join(args.workDir, args.architecture))}\n"
             f"{_install_root_line}\n"
             "You can use this package by loading the environment:"
             f"\n\n  bits {_arch_flag}enter {mainPackage}/latest-{mainBuildFamily}"
             f"{_arch_note}",
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
    if getattr(args, "signManifest", None):
      from bits_helpers import trust
      sig = trust.sign_manifest(args.manifest.path, args.signManifest)
      banner("Signed build manifest for trusted reuse:\n  %s", sig)

  # Best-effort reuse beacon: report which shared hashes this build consumed.
  # Fire-and-forget in a daemon thread — never blocks or fails the build.
  _beaconUrl = getattr(args, "reuseBeacon", None) or os.environ.get("BITS_REUSE_BEACON")
  _reused = getattr(args, "_reusedHashes", None)
  if _beaconUrl and _reused:
    from bits_helpers.beacon import send_reuse_beacon
    from bits_helpers.provenance import compute_build_id
    send_reuse_beacon(_beaconUrl, compute_build_id(specs, args), sorted(_reused))

  debug("Everything done")

