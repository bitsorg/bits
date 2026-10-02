# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read-only reuse plan for ``bits build --dry-run``.

For each package in build order: compute its hash as the build would, then say
where it would come from (installed, local tarball, remote store) or that it
would be built. Nothing is downloaded, installed or linked; the remote store is
only listed (one request per candidate hash).
"""
import os
from glob import glob

from bits_helpers.arch import effective_arch
from bits_helpers.log import banner, debug
from bits_helpers.sync import binary_redistributable
from bits_helpers.rev_index import revision_from_tarball
from bits_helpers.utilities import resolve_store_path

INSTALLED = "installed"
LOCAL_TARBALL = "local tarball"
REMOTE = "from remote store"
OVERLAY = "from reuse overlay"
REMOTE_UNSIGNED = "build (in store, not signed)"
BUILD = "build"
DEVEL = "build (development package)"


def store_can_list(sync_helper):
  """Whether *sync_helper* can list store objects (http(s):// and b3:// stores).

  The base RemoteSync.list_store_tarballs is a no-op; s3:// (s3cmd) and rsync
  stores keep it, so their contents cannot be checked without downloading."""
  from bits_helpers.sync import RemoteSync
  return getattr(type(sync_helper), "list_store_tarballs", None) not in (
      None, RemoteSync.list_store_tarballs)


def pick_revision(names, spec, arch, local=False, forced=None):
  """Lowest revision among tarball *names* for *spec* (as the build picks), else None.

  Only ``localN`` revisions when *local*, only plain ones otherwise; with a
  *forced* revision only that one or the revision-less name counts."""
  revs = []
  for n in names:
    r = revision_from_tarball(os.path.basename(n), spec["package"], spec["version"], arch)
    if r is None:
      continue
    if forced is not None:
      if r in (forced, ""):
        revs.append(r)
    elif r and r.startswith("local") == local:
      revs.append(r)
  if not revs:
    return None
  def _key(r):   # numeric revisions first, by number; others (hash labels) after
    n = r[len("local"):] if r.startswith("local") else r
    return (not n.isdigit(), int(n) if n.isdigit() else 0, r)
  return min(revs, key=_key)


def _installed(spec, work_dir, eff, rev, h):
  from bits_helpers.build import _pkg_install_path
  path = _pkg_install_path(work_dir, eff, dict(spec, revision=rev))
  if os.path.islink(path) and os.path.isdir(path):   # e.g. a CVMFS-linked install
    return True
  try:
    with open(os.path.join(path, ".build-hash")) as f:
      return f.read().strip() == h
  except OSError:
    return False


def classify(spec, arch, work_dir, sync_helper, can_list, write_store, trusted,
             overlay=None):
  """Return (state, hash, revision) for *spec*, mirroring build_one_package.

  *trusted* is None (signed reuse off) or the verified hash map; *overlay* is a
  predicate for packages the reuse overlay satisfies. Not modelled: revision
  candidates known only from the signed manifest / rev-index markers, and the
  sha256 check of a downloaded tarball (so a REMOTE line can still rebuild)."""
  eff = effective_arch(spec, arch)
  if spec.get("is_devel_pkg"):
    return DEVEL, spec["local_revision_hash"], ""
  if overlay is not None and overlay(spec):
    return OVERLAY, spec["remote_revision_hash"], ""
  forced = spec.get("force_revision") if "force_revision" in spec else None
  if forced is not None:
    remote = [spec["remote_revision_hash"]]
    local = []
  else:
    remote = list(spec["remote_hashes"])
    # With a write store the build never reuses a localN revision.
    local = [] if write_store else list(spec["local_hashes"])
  for hashes, is_local in ((remote, False), (local, True)):
    for h in hashes:
      rev = pick_revision(glob(os.path.join(work_dir, resolve_store_path(eff, h), "*.tar.gz")),
                          spec, eff, local=is_local, forced=forced)
      if rev is not None:
        state = INSTALLED if _installed(spec, work_dir, eff, rev, h) else LOCAL_TARBALL
        return state, h, rev
  if can_list:
    for h in remote:
      rev = pick_revision(sync_helper.list_store_tarballs(eff, h), spec, eff, forced=forced)
      if rev is not None:
        if trusted is not None and h not in trusted:
          return REMOTE_UNSIGNED, h, rev
        return REMOTE, h, rev
  if forced is not None or write_store:
    return BUILD, spec["remote_revision_hash"], forced or ""
  return BUILD, spec["local_revision_hash"], ""


def _overlay_predicate(cfg):
  """The build's reuse-overlay check (build_one_package), or None when unused."""
  if not getattr(cfg, "reuse_overlay", None):
    return None
  from bits_helpers.cvmfs_import import overlay_reuse_module
  build_local = cfg.build_local or []
  if isinstance(build_local, str):
    build_local = build_local.split(",")
  build_local = set(x for x in build_local if x)
  relaxed = cfg.reuse_policy == "relaxed"

  def satisfied(spec):
    if spec["package"].startswith("defaults-") or spec["package"] in build_local:
      return False
    want = None if relaxed else spec.get("remote_revision_hash")
    if not (relaxed or want):
      return False
    return bool(overlay_reuse_module(cfg.reuse_overlay, spec["package"],
                                     want_hash=want, want_version=spec.get("version")))
  return satisfied


def print_plan(rows, store_checked):
  """Print the per-package plan and a one-line summary."""
  w0 = max([len("Package")] + [len(r[0]) for r in rows])
  w1 = max([len("Version")] + [len(r[1]) for r in rows])
  lines = ["  %-*s  %-*s  %s" % (w0, "Package", w1, "Version", "Plan")]
  for pkg, ver, state, note in rows:
    lines.append("  %-*s  %-*s  %s%s" % (w0, pkg, w1, ver, state, note))
  count = lambda s: sum(1 for r in rows if r[2] == s)
  reused = count(INSTALLED) + count(LOCAL_TARBALL) + count(REMOTE) + count(OVERLAY)
  summary = "%d reused (%d installed, %d local tarball, %d from remote store%s), %d to build" % (
      reused, count(INSTALLED), count(LOCAL_TARBALL), count(REMOTE),
      (", %d from overlay" % count(OVERLAY)) if count(OVERLAY) else "", len(rows) - reused)
  if not store_checked:
    summary += "; remote store not checked (only http(s):// and b3:// stores can be listed)"
  banner("Dry run: build plan (nothing built)\n%s\n\n%s", "\n".join(lines), summary)


def plan_build(build_order, specs, args, work_dir, sync_helper, raw_architecture,
               cfg, trusted_index_fn):
  """Compute hashes in build order and print what the build would do."""
  from bits_helpers.build import storeHook
  from bits_helpers.hashing import storeHashes
  from bits_helpers.arch import SHARED_ARCH
  write_store = bool(getattr(sync_helper, "writeStore", ""))
  can_list = store_can_list(sync_helper)
  trusted = trusted_index_fn() if (cfg.require_signed_reuse and can_list) else None
  overlay = _overlay_predicate(cfg)
  rows = []
  for p in build_order:
    spec = specs[p]
    storeHook(p, specs, args.defaults[0])
    storeHashes(p, specs, considerRelocation=(
      raw_architecture.startswith("osx") and spec.get("architecture") != SHARED_ARCH))
    state, spec["hash"], spec["revision"] = classify(
        spec, args.architecture, work_dir, sync_helper, can_list, write_store, trusted,
        overlay)
    debug("Plan %s: %s (hash %s, candidates %s)", p, state, spec["hash"],
          ", ".join(spec["remote_hashes"]))
    if state == DEVEL:
      write_store = False   # the build disables uploads from here on (build.py)
    if p.startswith("defaults-"):
      continue
    note = ""
    if state in (BUILD, REMOTE_UNSIGNED) and write_store and not binary_redistributable(spec):
      note = "  (not uploadable: redistributable)"
    rows.append((p, spec["version"], state, note))
  print_plan(rows, can_list)
  return rows
