# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Read-only reuse plan, shown before every build and as ``bits build --dry-run``.

For each package in build order: compute its hash as the build would, then say
where it would come from (installed, local tarball, remote store) or that it
would be built. Nothing is downloaded, installed or linked; the remote store is
only listed (one request per candidate hash).

A package only another, reused, package needs to build (its build_requires) is
"not needed": the build neither fetches nor builds it (issue #141).
"""
import concurrent.futures
import copy
import os
from glob import glob

from bits_helpers.arch import effective_arch
from bits_helpers.log import ProgressPrint, banner, debug
from bits_helpers.sync import binary_redistributable
from bits_helpers.rev_index import revision_from_tarball
from bits_helpers.utilities import is_virtual_package, resolve_store_path

INSTALLED = "installed"
LOCAL_TARBALL = "local tarball"
REMOTE = "from store"
OVERLAY = "from overlay"
SYMLINK = "symlink from deployment"
REMOTE_UNSIGNED = "build (unsigned)"     # in the store, but no signed manifest vouches for it
BUILD = "build"
DEVEL = "build (development)"
NOT_NEEDED = "not needed"
REUSED = (INSTALLED, LOCAL_TARBALL, REMOTE, OVERLAY, SYMLINK)
LIST_WORKERS = 16     # store listings at once: one request each, mostly latency


def store_can_list(sync_helper):
  """Whether *sync_helper* can list store objects (http(s)://, s3:// and b3:// stores).

  The base RemoteSync.list_store_tarballs is a no-op; rsync stores keep it, so
  their contents cannot be checked without downloading."""
  from bits_helpers.sync import RemoteSync, MultiRemoteSync
  if isinstance(sync_helper, MultiRemoteSync):
    return any(store_can_list(r) for r in sync_helper.readers)
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


def _local(spec, eff, work_dir, write_store):
  """(INSTALLED or LOCAL_TARBALL, hash, revision) of a tarball of *spec* in the
  local store, or None."""
  forced = spec.get("force_revision") if "force_revision" in spec else None
  if forced is not None:
    remote, local = [spec["remote_revision_hash"]], []
  else:
    # With a write store the build never reuses a localN revision.
    remote, local = list(spec["remote_hashes"]), [] if write_store else list(spec["local_hashes"])
  for hashes, is_local in ((remote, False), (local, True)):
    for h in hashes:
      rev = pick_revision(glob(os.path.join(work_dir, resolve_store_path(eff, h), "*.tar.gz")),
                          spec, eff, local=is_local, forced=forced)
      if rev is not None:
        state = INSTALLED if _installed(spec, work_dir, eff, rev, h) else LOCAL_TARBALL
        return state, h, rev
  return None


def classify(spec, arch, work_dir, sync_helper, can_list, write_store, trusted,
             overlay=None, symlink=None):
  """Return (state, hash, revision) for *spec*, mirroring build_one_package.

  *trusted* is None (signed reuse off) or the verified {(arch, hash): sha256} map; *overlay* is a
  predicate for packages the reuse overlay satisfies. Not modelled: revision
  candidates known only from the signed manifest / rev-index markers, and the
  sha256 check of a downloaded tarball (so a REMOTE line can still rebuild)."""
  eff = effective_arch(spec, arch)
  if spec.get("is_devel_pkg"):
    return DEVEL, spec["local_revision_hash"], ""
  if symlink is not None:
    match = symlink(spec)
    if match:
      from bits_helpers.reuse_symlinks import matching_local_install
      if matching_local_install(spec, match, work_dir, arch):
        return INSTALLED, match["hash"], match["revision"]
      return SYMLINK, match["hash"], match["revision"]
  if overlay is not None and overlay(spec):
    return OVERLAY, spec["remote_revision_hash"], ""
  forced = spec.get("force_revision") if "force_revision" in spec else None
  remote = [spec["remote_revision_hash"]] if forced is not None else list(spec["remote_hashes"])
  here = _local(spec, eff, work_dir, write_store)
  if here:
    return here
  # A remote tarball the signed manifest does not vouch for is discarded by
  # the build, which then builds as if the store had none (the hash-policy
  # fallback below included).
  unsigned = None
  if can_list:
    for h in remote:
      rev = pick_revision(sync_helper.list_store_tarballs(eff, h), spec, eff, forced=forced)
      if rev is not None:
        if trusted is not None and (eff, h) not in trusted:
          unsigned = (h, rev)
          break
        return REMOTE, h, rev
  if (spec.get("_revision_policy_hash_injected") and not write_store
      and not is_virtual_package(spec)):
    from bits_helpers.hashing import apply_local_hash_fallback
    local_hash = spec["local_revision_hash"]
    local_files = glob(os.path.join(
        work_dir, resolve_store_path(eff, local_hash), "*.tar.gz"))
    local_rev = pick_revision(local_files, spec, eff, forced=local_hash)
    if local_rev is not None:
      state = (INSTALLED if _installed(spec, work_dir, eff, local_rev, local_hash)
               else LOCAL_TARBALL)
      apply_local_hash_fallback(spec, write_store, reusable=False)
      return state, local_hash, local_rev
    apply_local_hash_fallback(spec, write_store, reusable=False)
    return (REMOTE_UNSIGNED if unsigned else BUILD), spec["hash"], spec["revision"]
  if unsigned:
    return (REMOTE_UNSIGNED,) + unsigned
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


def needed(build_order, specs, states, targets, build_targets=()):
  """The packages the build needs: *targets* and what they require, where a
  reused package needs only its runtime and untracked requirements, not its
  build_requires. *build_targets* are followed as if built (--only-deps)."""
  # defaults-* and development packages are always needed, and so is what they require.
  out = set()
  todo = list(targets) + [p for p in build_order
                          if p.startswith("defaults-") or specs[p].get("is_devel_pkg")]
  while todo:
    p = todo.pop()
    if p in out or p not in specs:
      continue
    out.add(p)
    spec = specs[p]
    if states.get(p) in REUSED and p not in build_targets:
      todo += list(spec.get("runtime_requires") or ()) + list(spec.get("untracked_requires") or ())
    else:
      todo += list(spec.get("requires") or ())
  return out


def adopt(specs, planned, packages):
  """Give the not-needed *packages* the hash and revision the plan found for them
  in *planned*: the reused packages that build with them fold those in. Marked
  not_needed, they are left out of those packages' init.sh."""
  found = {p: (planned[p]["hash"], planned[p]["revision"]) for p in packages}
  for p, (h, rev) in found.items():
    specs[p].update(hash=h, revision=rev, not_needed=True)


def plan_rows(build_order, specs, states, need, unuploadable=(), source=None):
  """(package, version, plan, note, recipe source) per package; defaults-* left out."""
  rows = []
  for p in build_order:
    if p.startswith("defaults-"):
      continue
    state, note = states[p], ""
    if p not in need:
      state = NOT_NEEDED
      users = [q for q in build_order if q in need and states.get(q) in REUSED
               and p in (specs[q].get("full_requires") or specs[q].get("requires") or ())]
      if users:
        note = "  (build-only, for reused %s%s)" % (", ".join(users[:3]),
                                                    ", ..." if len(users) > 3 else "")
    elif p in unuploadable:
      note = "  (not uploadable: redistributable)"
    rows.append((p, specs[p]["version"], state, note, source(p) if source else ""))
  return rows


def _unsigned_reason(n):
  from bits_helpers.build import _try_import_trust
  if _try_import_trust() is None:
    return "%s cannot be checked without the Python package cryptography" % (
        "its signature" if n == 1 else "their signatures")
  return "no verified signed manifest vouches for %s" % ("it" if n == 1 else "them")


NAME_WIDTH, VERSION_WIDTH = 28, 16     # longer ones overflow their own row only


def print_plan(rows, store_checked, title="Dry run: build plan (nothing built)"):
  """Print the per-package plan and a summary, narrow enough for a terminal: the
  recipe source shared by most packages is named once, in the title."""
  sources = [r[4] for r in rows if len(r) > 4 and r[4]]
  common = max(dict.fromkeys(sources), key=sources.count) if sources else ""
  if common and sources.count(common) * 2 <= len(rows):
    common = ""
  if common:
    title += "\nRecipes from %s, unless shown" % common
  shown = lambda r: r[4] if len(r) > 4 and r[4] and r[4] != common else ""   # noqa: E731
  w0 = min(max([len("Package")] + [len(r[0]) for r in rows]), NAME_WIDTH)
  w1 = min(max([len("Version")] + [len(r[1]) for r in rows]), VERSION_WIDTH)
  w2 = max([len("Plan")] + [len(r[2] + r[3]) for r in rows if shown(r)])
  lines = ["%-*s  %-*s  Plan" % (w0, "Package", w1, "Version")]
  for r in rows:
    pkg, ver, state, note = r[:4]
    line = "%-*s  %-*s  %s%s" % (w0, pkg, w1, ver, state, note)
    if shown(r):
      line += "%*s  [%s]" % (max(w2 - len(state + note), 0), "", shown(r))
    lines.append(line.rstrip())
  count = lambda s: sum(1 for r in rows if r[2] == s)   # noqa: E731
  reused = sum(count(s) for s in REUSED)
  summary = ["%d reused (%d installed, %d local tarball, %d from store%s), %d to build%s" % (
      reused, count(INSTALLED), count(LOCAL_TARBALL), count(REMOTE),
      ((", %d from overlay" % count(OVERLAY)) if count(OVERLAY) else "") +
      ((", %d symlinked" % count(SYMLINK)) if count(SYMLINK) else ""),
      len(rows) - reused - count(NOT_NEEDED),
      (", %d not needed" % count(NOT_NEEDED)) if count(NOT_NEEDED) else "")]
  if count(REMOTE_UNSIGNED):
    n = count(REMOTE_UNSIGNED)
    summary.append("%d to build %s in the remote store but not reused: %s" % (
        n, "is" if n == 1 else "are", _unsigned_reason(n)))
  if store_checked is False:
    summary.append("remote store not checked (rsync stores cannot be listed)")
  elif store_checked is None:
    summary.append("no remote store used")
  banner("%s\n%s\n\n%s", title, "\n".join(lines), "\n".join(summary))


class _Listings:
  """The store listings of a plan, made in parallel up front: in front of
  *helper*'s list_store_tarballs, one HTTP request per (architecture, hash).
  With a *kept* StoreCache and the *trusted* (signed) index, a certified
  tarball found in an earlier run is not asked for again: the store's garbage
  collection keeps what a signed manifest lists."""

  def __init__(self, helper, kept=None, trusted=None):
    self.helper, self.cache, self.kept, self.trusted = helper, {}, kept, trusted
    self.failed = set()                # not asked for again ahead, only when needed

  def _from_kept(self, key):
    if self.kept is None or self.trusted is None or key not in self.trusted:
      return None
    return self.kept.get(*key)

  def _found(self, key, names):
    self.cache[key] = names
    if self.kept is not None and self.trusted is not None and key in self.trusted:
      self.kept.put(key[0], key[1], names)

  def list_store_tarballs(self, arch, pkg_hash):
    key = (arch, pkg_hash)
    if key not in self.cache:          # a hash the speculation did not foresee
      names = self._from_kept(key)
      if names is None:
        names = self.helper.list_store_tarballs(arch, pkg_hash)
        self._found(key, names)
      self.cache[key] = names
    return self.cache[key]

  def prefetch(self, keys, what="Checking the remote store"):
    """List *keys* not known yet, in parallel; returns how many were asked for."""
    keys = [k for k in dict.fromkeys(keys) if k not in self.cache and k not in self.failed]
    for k in keys:
      names = self._from_kept(k)
      if names is not None:
        self.cache[k] = names
    known = len(keys)
    keys = [k for k in keys if k not in self.cache]
    known -= len(keys)
    if not keys:
      return 0
    progress = ProgressPrint(what)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=LIST_WORKERS)
    futures = {pool.submit(self.helper.list_store_tarballs, *k): k for k in keys}
    try:
      for i, f in enumerate(concurrent.futures.as_completed(futures)):
        progress("[%d/%d] packages", i + 1, len(keys))
        try:
          self._found(futures[f], f.result())
        except Exception as exc:   # listed again, one by one, when needed
          debug("store listing failed for %s: %s", futures[f], exc)
          self.failed.add(futures[f])
    except BaseException:          # Ctrl-C: drop what has not started
      for f in futures:
        f.cancel()
      pool.shutdown(wait=False)
      raise
    pool.shutdown()
    progress.end("done%s" % (", %d known from earlier runs" % known if known else ""))
    return len(keys)


class _Assumed:
  """The store as the speculation sees it: the *known* listings, and a tarball
  (signed) under every hash not listed yet: the usual case."""

  def __init__(self, known, trusted):
    self.known, self.trusted, self.spec = known, trusted, None

  def list_store_tarballs(self, arch, pkg_hash):
    if (arch, pkg_hash) in self.known:
      return self.known[(arch, pkg_hash)]
    rev = self.spec.get("force_revision", "1")      # "" forced: a revision-less name
    return ["%s-%s%s.%s.tar.gz" % (self.spec["package"], self.spec["version"],
                                   "-" + rev if rev else "", arch)]

  def __contains__(self, key):      # as the trusted index
    return key not in self.known or (self.trusted is not None and key in self.trusted)


def _foreseen(build_order, specs, args, raw_architecture, work_dir, write_store, known=None,
              trusted=None, overlay=None):
  """The (architecture, hash) pairs the plan will list, but not those of
  packages already here: computed on a copy of *specs*, each package classified
  as the plan will, from the *known* listings ({(arch, hash): names}) and, for
  hashes not listed yet, assuming the store has them, signed."""
  from bits_helpers.arch import SHARED_ARCH
  from bits_helpers.build import storeHook
  from bits_helpers.hashing import storeHashes
  store = _Assumed(known or {}, trusted)
  signed = store if trusted is not None else None
  trial, keys = copy.deepcopy(specs), []
  for p in build_order:
    spec = trial[p]
    storeHook(p, trial, args.defaults[0])
    storeHashes(p, trial, considerRelocation=(
      raw_architecture.startswith("osx") and spec.get("architecture") != SHARED_ARCH))
    store.spec = spec
    state, spec["hash"], spec["revision"] = classify(
      spec, args.architecture, work_dir, store, True, write_store, signed, overlay)
    if state == DEVEL:
      write_store = False   # as the plan (and the build) do from here on
    if state in (DEVEL, OVERLAY, INSTALLED, LOCAL_TARBALL) or is_virtual_package(spec):
      continue
    eff = effective_arch(spec, args.architecture)
    hashes = [spec["remote_revision_hash"]] if "force_revision" in spec else spec["remote_hashes"]
    keys += [(eff, h) for h in hashes]
  return keys


MAX_ROUNDS = 4          # listings made again for hashes the earlier ones changed


def plan(build_order, specs, args, work_dir, sync_helper, raw_architecture,
         cfg, trusted_index_fn):
  """({package: state}, packages not uploadable, whether the store was listed),
  as the build would decide. Sets each spec's hash and revision: a real build
  passes a copy of its specs, so that its own loop decides afresh."""
  from bits_helpers.build import storeHook
  from bits_helpers.hashing import storeHashes
  from bits_helpers.arch import SHARED_ARCH
  write_store = bool(getattr(sync_helper, "writeStore", ""))
  from bits_helpers.sync import NoRemoteSync
  # None: there is no store, so nothing to check.
  can_list = None if isinstance(sync_helper, NoRemoteSync) else store_can_list(sync_helper)
  trusted = trusted_index_fn() if (cfg.require_signed_reuse and can_list) else None
  overlay = _overlay_predicate(cfg)
  lister, kept = sync_helper, None
  if can_list:
    from bits_helpers.store_cache import StoreCache, store_id
    kept = StoreCache(work_dir, store_id(sync_helper))
    lister = _Listings(sync_helper, kept, trusted)
    try:
      # A package not in the store is built with its local hash, which changes
      # the hashes of all that need it: listed in the next round, not one by one.
      what = "Checking the remote store"
      for _ in range(MAX_ROUNDS):
        if not lister.prefetch(_foreseen(build_order, specs, args, raw_architecture, work_dir,
                                         write_store, lister.cache, trusted, overlay), what):
          break
        what = "Checking the remote store for the hashes that changed"
    except Exception as exc:     # the plan then lists one package at a time
      debug("cannot list the store ahead: %s", exc)
  from bits_helpers.reuse_symlinks import select_symlink_package
  symlink = lambda spec: select_symlink_package(spec, cfg, args.architecture)   # noqa: E731
  states, unuploadable = {}, set()
  for p in build_order:
    spec = specs[p]
    storeHook(p, specs, args.defaults[0])
    storeHashes(p, specs, considerRelocation=(
      raw_architecture.startswith("osx") and spec.get("architecture") != SHARED_ARCH))
    state, spec["hash"], spec["revision"] = classify(
        spec, args.architecture, work_dir, lister, can_list, write_store, trusted,
        overlay, symlink=symlink)
    debug("Plan %s: %s (hash %s, candidates %s)", p, state, spec["hash"],
          ", ".join(spec["remote_hashes"]))
    states[p] = state
    if state == DEVEL:
      write_store = False   # the build disables uploads from here on (build.py)
    if state in (BUILD, REMOTE_UNSIGNED) and write_store and not binary_redistributable(spec):
      unuploadable.add(p)
  if kept is not None:
    kept.save()
  return states, unuploadable, can_list


def decide(build_order, specs, args, work_dir, sync_helper, raw_architecture, cfg,
           trusted_index_fn, targets=None, build_targets=(), title=None, source=None):
  """Plan, find what is needed and print it: (rows, states, needed packages).
  With cfg.fetch_build_requires every package is needed (the old behaviour)."""
  states, unuploadable, can_list = plan(build_order, specs, args, work_dir, sync_helper,
                                        raw_architecture, cfg, trusted_index_fn)
  if getattr(cfg, "fetch_build_requires", False):
    need = set(build_order)
  else:
    need = needed(build_order, specs, states, targets or build_order[-1:], build_targets)
  rows = plan_rows(build_order, specs, states, need, unuploadable, source)
  print_plan(rows, can_list, *([title] if title else []))
  return rows, states, need


def plan_build(build_order, specs, args, work_dir, sync_helper, raw_architecture,
               cfg, trusted_index_fn, **kw):
  """`bits build --dry-run`: print what the build would do; returns the rows."""
  return decide(build_order, specs, args, work_dir, sync_helper, raw_architecture, cfg,
                trusted_index_fn, **kw)[0]
