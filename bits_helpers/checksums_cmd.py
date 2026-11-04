# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""``bits checksums`` — compute and record the checksums of every recipe in a
recipe repository, without building anything.

For each recipe (``*.sh``, not ``defaults-*``) of the repository, and with
``--defaults`` for every recipe (also from ``--recipes`` repositories) as each
of the repository's ``defaults-*.sh`` profiles sets it (overrides, variables,
source mode), the source URLs, patch names and git tag are resolved as a build
would resolve them. Then:

* tarball sources are downloaded through the download cache (the remote store
  first when ``--remote-store`` is given, then upstream) and hashed (sha256).
  A cached or mirrored copy is trusted as a build trusts it; ``--fresh``
  downloads everything again from upstream into a private cache;
* patches are hashed from the recipe's ``patches/`` directory;
* a git ``tag:`` is resolved to its commit with ``git ls-remote``; a branch
  (``master``) moves, so it is reported and never pinned.

Every architecture variant of a ``(arch)url`` source is recorded; a
``$(bash)`` source is evaluated for ``--architecture`` only.

A profile is recorded only where it changes what the release profile (or, for
``defaults-release`` itself, the recipe as written) resolves to: those entries
belong to the repository holding the profile, which the build merges over the
recipe repository's own file.

Results are compared with the checksum files already there and with inline
``url,algo:hex`` suffixes. ``--write`` adds the new entries; an entry that
disagrees is reported and never overwritten.
"""

import os
import re
import socket
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

from bits_helpers.checksum import checksum_file, parse_entry
from bits_helpers.checksum_store import (find_checksum_file, parse_checksum_file,
                                         update_checksum_file, _same_checksum)
from bits_helpers.defaults import asDict, merge_dicts
from bits_helpers.log import error, info, warning
from bits_helpers.matchers import _parse_patch_entry, resolve_variables
from bits_helpers.recipe import getRecipeReader, parseRecipe
from bits_helpers.utilities import apply_version_from, resolve_spec_data
from bits_helpers.workarea import _resolve_source_entry, _split_arch_prefix

_SHA = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")
_UNRESOLVED = re.compile(r"%\([a-zA-Z][a-zA-Z0-9_]*\)s")
_DATED = re.compile(r"%\((year|month|day|hour)\)s")
_HASHED_PROTOCOLS = ("http://", "https://", "ftp://", "ftps://")


# ── Recipes and profiles ──────────────────────────────────────────────────────

def _read(path):
  """Parsed YAML header of recipe *path*, or ``None`` (warned) if unparsable."""
  err, meta, _ = parseRecipe(getRecipeReader(path))
  if err or not isinstance(meta, dict):
    warning("Skipping %s: %s", path, err or "not a recipe")
    return None
  return meta


def recipes_in(repo):
  """``{lowercase package name: recipe path}`` for the recipes of *repo*."""
  out = {}
  for f in sorted(os.listdir(repo)):
    path = os.path.join(repo, f)
    if f.endswith(".sh") and not f.startswith("defaults-") and os.path.isfile(path):
      out[f[:-3].lower()] = path
  return out


def profiles_in(repo, want):
  """``[(name, meta)]`` for the ``defaults-*.sh`` of *repo* named by *want*
  ("all" or a comma list). Raises ``ValueError`` for an unknown name."""
  have = sorted(f[len("defaults-"):-3] for f in os.listdir(repo)
                if f.startswith("defaults-") and f.endswith(".sh"))
  names = have if want == "all" else [x.strip() for x in want.split(",") if x.strip()]
  missing = [n for n in names if n not in have]
  if missing:
    raise ValueError("no defaults-%s.sh in %s" % (", defaults-".join(missing), repo))
  out = []
  for n in names:
    meta = _read(os.path.join(repo, "defaults-%s.sh" % n))
    if meta is not None:
      out.append((n, meta))
  return out


def override_sets(overrides, pkg):
  """The override dicts a profile applies to *pkg*: its unconditional keys
  merged, plus one set per matcher-gated key (``ROOT:osx``) on top of them.
  Keys match as the build matches them: lowercased regex, ``@dist`` dropped."""
  plain, gated = {}, []
  for key, ovr in (overrides or {}).items():
    head, sep, _ = str(key).partition(":")
    try:
      if not re.fullmatch(head.split("@", 1)[0].lower(), pkg.lower()):
        continue
    except re.error:
      continue
    if sep:
      gated.append(dict(ovr or {}))
    else:
      plain.update(ovr or {})
  sets = [plain] if plain else []
  return sets + [dict(plain, **g) for g in gated]


# ── Resolution ────────────────────────────────────────────────────────────────

def _expand(spec, text, defaults, variables):
  """*text* with %(...)s expanded; ``ValueError`` if anything is left over."""
  out = resolve_spec_data(spec, str(text), defaults, default_vars=variables, strict=False)
  if _UNRESOLVED.search(out):
    raise ValueError("cannot resolve %r" % text)
  return out


def source_mode(*metas):
  """'git' or 'tar', as the build picks it for a recipe declaring both source
  forms: $BITS_SOURCE_MODE, else the last profile of *metas* setting
  ``system.source_mode`` (or a ``source_mode`` variable), else 'tar'."""
  val = os.environ.get("BITS_SOURCE_MODE", "").strip().lower()
  if not val:
    for meta in reversed(metas):
      m = (meta or {})
      val = str((m.get("system") or {}).get("source_mode")
                or (m.get("variables") or {}).get("source_mode") or "").strip().lower()
      if val:
        break
  return "git" if val == "git" else "tar"


def wants(spec, defaults, variables, arch, mode="tar"):
  """What a build of *spec* would fetch, as ``{"sources": {url: inline},
  "patches": {name: inline}, "git": (source, tag, dated) | None,
  "skipped": [(entry, reason)]}``. Mirrors the build's order: tag, then
  version, then the recipe's variables, sources and patches; a recipe with
  both a git and a tarball source keeps only the *mode* one."""
  out = {"sources": {}, "patches": {}, "git": None, "skipped": []}
  if not (spec.get("source") or spec.get("sources") or spec.get("patches")):
    return out                             # nothing a build would fetch
  spec = dict(spec)
  if spec.get("source") and spec.get("sources"):
    if mode == "git":
      spec.pop("sources")
    else:
      spec.pop("source")
      spec.pop("tag", None)
  variables = dict(variables or {})
  if spec.get("version_from") in variables:
    apply_version_from(spec, variables)
  raw_tag = spec["tag"] = str(spec.get("tag", spec.get("version", "")))
  spec["tag"] = _expand(spec, raw_tag, defaults, variables)
  if spec.get("sources"):
    spec["commit_hash"] = spec["tag"]
  spec["version"] = _expand(spec, spec.get("version", ""), defaults, variables)
  spec["variables"] = {k: _expand(spec, v, defaults, variables)
                       for k, v in (spec.get("variables") or {}).items()}
  for raw in spec.get("sources") or []:
    try:
      entry = _expand(spec, raw, defaults, variables)
      split = _split_arch_prefix(entry)
      if split is not None:
        entry = split[1]                   # record every architecture's variant
      elif entry.startswith("$("):
        entry, _ = _resolve_source_entry(entry, arch)
    except (ValueError, OSError) as exc:
      out["skipped"].append((raw, str(exc)))
      continue
    url, inline = parse_entry(entry)
    if url.startswith(_HASHED_PROTOCOLS):
      out["sources"][url] = inline
    else:                                  # file://, git:, pip: — not a fixed file
      out["skipped"].append((url, "not a downloadable file"))
  for raw in spec.get("patches") or []:
    try:
      name, _, suffix, _ = _parse_patch_entry(_expand(spec, raw, defaults, variables))
    except ValueError as exc:
      out["skipped"].append((raw, str(exc)))
      continue
    out["patches"][name] = suffix[1:] if suffix else None
  source = spec.get("source")
  if source and spec["tag"]:
    try:
      out["git"] = (_expand(spec, source, defaults, variables), spec["tag"],
                    bool(_DATED.search(raw_tag)))
    except ValueError as exc:
      out["skipped"].append((source, str(exc)))
  return out


def delta(variant, ref):
  """The parts of *variant* that *ref* (a :func:`_union`) lacks."""
  git = variant["git"]
  return {"sources": {u: c for u, c in variant["sources"].items() if u not in ref["sources"]},
          "patches": {p: c for p, c in variant["patches"].items() if p not in ref["patches"]},
          "git": git if git and git[:2] not in ref["gits"] else None,
          "skipped": [s for s in variant["skipped"] if s not in ref["skipped"]]}


# ── Fetching ──────────────────────────────────────────────────────────────────

def _algo(checksum):
  """The algorithm of an ``algo:hex`` checksum; ``None`` for a commit SHA."""
  return checksum.partition(":")[0].lower() if ":" in checksum else None


def git_pin(source, tag):
  """``("pinned", sha)`` for a tag, ``("moving", sha)`` for a branch (the build
  checks a branch out first), ``("commit", sha)`` when *tag* is already a
  commit. Raises ``RuntimeError`` when the ref is missing or the remote fails."""
  if _SHA.match(tag):
    return "commit", tag.lower()
  if source.startswith("-") or tag.startswith("-"):
    raise RuntimeError("refusing option-like source or tag %r %r" % (source, tag))
  env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
  refs = ["refs/heads/" + tag, "refs/tags/" + tag, "refs/tags/%s^{}" % tag]
  try:
    res = subprocess.run(["git", "ls-remote", source] + refs, capture_output=True,
                         text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)
  except (OSError, subprocess.TimeoutExpired) as exc:
    raise RuntimeError(str(exc)) from exc
  if res.returncode:
    raise RuntimeError((res.stderr.strip().splitlines() or ["git ls-remote failed"])[-1])
  found = dict(reversed(l.split("\t", 1)) for l in res.stdout.splitlines() if "\t" in l)
  if refs[0] in found:
    return "moving", found[refs[0]]
  for ref in (refs[2], refs[1]):           # an annotated tag's commit, else the tag
    if ref in found:
      return "pinned", found[ref].lower()
  raise RuntimeError("no tag or branch %r in %s" % (tag, source))


def hash_source(url, work_dir, sync_helper):
  """``algo:hex`` of *url*, fetched through the download cache (a file:// one, read in place)."""
  from bits_helpers.download import download
  return checksum_file(download(url, None, work_dir, sync_helper=sync_helper))


# ── The command ───────────────────────────────────────────────────────────────

def plan(repo, recipe_dirs, profile_names, packages, arch):
  """``{package: {"sources", "patches", "git", "skipped", "origin"}}``: what to
  record in each ``<repo>/checksums/<package>.checksum``. ``sources`` maps a
  URL to its inline checksum, ``patches`` a name to ``(inline, patches_dir)``,
  ``git`` lists ``(source, tag, dated)``; ``origin`` names the profile behind
  an entry (none for the repository's own recipes).

  A profile's entries are those its overrides and variables add over the
  release profile (for the release profile itself, over the recipe as
  written). An unresolvable recipe is an error only where it matters: one of
  the repository's own, or one a profile overrides."""
  search = [repo] + [d for d in recipe_dirs if os.path.abspath(d) != repo]
  found = {}
  for d in reversed(search):               # earlier repositories win, as in a build
    found.update(recipes_in(d))
  own = recipes_in(repo)
  want = {p.lower() for p in packages}
  profiles = profiles_in(repo, profile_names) if profile_names else []
  release = next((m for m in map(_release, search) if m is not None), {})
  rel_vars = resolve_variables(release.get("variables"), {}, arch, ["release"])
  rel_chain = [asDict(release.get("overrides") or {})]
  todo = {}

  def add(pkg, patches_dir, got, origin):
    ent = todo.setdefault(pkg, {"sources": {}, "patches": {}, "git": [],
                                "skipped": [], "errors": [], "origin": {}})
    for url, inline in got["sources"].items():
      ent["sources"].setdefault(url, inline)
      ent["origin"].setdefault(url, origin)
    for name, inline in got["patches"].items():
      ent["patches"].setdefault(name, (inline, patches_dir))
      ent["origin"].setdefault(name, origin)
    if got["git"] and got["git"] not in ent["git"]:
      ent["git"].append(got["git"])
      ent["origin"].setdefault(got["git"][1], origin)
    ent["skipped"] += [s for s in got["skipped"] if s not in ent["skipped"]]

  # Per profile: its defaults, variables, source mode and override chain.
  views = []
  for pname, pmeta in profiles:
    rel = pname == "release"
    defaults = ["release"] if rel else ["release", pname]
    variables = resolve_variables(merge_dicts(release.get("variables") or {},
                                              {} if rel else pmeta.get("variables") or {}),
                                  {}, arch, defaults)
    chain = rel_chain + ([] if rel else [asDict(pmeta.get("overrides") or {})])
    views.append((pname, defaults, variables, source_mode(release, pmeta), chain, rel))

  for name in sorted(set(own) | set(found if profiles else ())):
    if want and name not in want:
      continue
    meta = _read(found[name])
    if meta is None:
      continue
    pkg = str(meta.get("package", name))
    if pkg.lower() != name:                # the build refuses such a recipe too
      warning("Skipping %s: its package field is %r", found[name], pkg)
      continue
    patches_dir = os.path.join(os.path.dirname(found[name]), "patches")
    try:
      base = wants(meta, ["release"], rel_vars, arch, source_mode(release))
    except ValueError as exc:
      if name in own:
        add(pkg, patches_dir, _union([]), "")
        todo[pkg]["errors"].append(("recipe", str(exc), ""))
      continue
    if name in own:
      add(pkg, patches_dir, base, "")
    # A profile's entries: what its overrides and variables add over the
    # release profile (for the release profile itself, over the recipe).
    for pname, defaults, variables, mode, chain, rel in views:
      sets = _chain_sets(chain, name)
      overridden = bool(sets)
      sets = sets or ([] if rel else [{}])
      try:
        ref = _union([base] + ([] if rel else
                               [wants(dict(meta, **r), ["release"], rel_vars, arch,
                                      source_mode(release)) for r in _chain_sets(rel_chain, name)]))
        for ovr in sets:
          got = delta(wants(dict(meta, **ovr), defaults, variables, arch, mode), ref)
          if any(got.values()):
            add(pkg, patches_dir, got, pname)
      except ValueError as exc:
        if overridden or name in own:
          add(pkg, patches_dir, _union([]), pname)
          todo[pkg]["errors"].append(("recipe", str(exc), pname))
  return todo


def _union(results):
  """Merge several :func:`wants` results; ``gits`` lists every git ref."""
  out = {"sources": {}, "patches": {}, "git": None, "gits": [], "skipped": []}
  for r in results:
    out["sources"].update(r["sources"])
    out["patches"].update(r["patches"])
    if r["git"]:
      out["gits"].append(r["git"][:2])
    out["skipped"] += [x for x in r["skipped"] if x not in out["skipped"]]
  return out


def _release(d):
  path = os.path.join(d, "defaults-release.sh")
  return _read(path) if os.path.isfile(path) else None


def _chain_sets(chain, pkg):
  """Override sets for *pkg* across a profile chain (release first); empty
  when no profile of the chain overrides it."""
  out = [{}]
  for overrides in chain:
    sets = override_sets(overrides, pkg)
    if sets:
      out = [dict(o, **s) for o in out for s in sets]
  return [s for s in out if s]


def doChecksums(args, parser):  # noqa: N802
  """`bits checksums [PACKAGE ...]`: report, and with --write record, checksums."""
  from bits_helpers.sync import remote_from_url
  repo = os.path.abspath(args.configDir)
  if not os.path.isdir(repo):
    parser.error("no recipe repository %s" % args.configDir)
  socket.setdefaulttimeout(120)            # a stalled server must not hang a worker
  try:
    todo = plan(repo, args.recipeDirs or [], args.defaultsProfiles, args.pkgname or [],
                args.architecture)
  except (OSError, ValueError) as exc:
    parser.error(str(exc))
  work_dir = args.workDir
  fresh = None
  if args.fresh:                           # a private cache, upstream only
    os.makedirs(args.workDir, exist_ok=True)
    fresh = tempfile.TemporaryDirectory(prefix="bits-checksums-", dir=args.workDir)
    work_dir = fresh.name
  store = "" if args.fresh else re.sub(r"::rw$", "", args.remoteStore or "")
  sync = remote_from_url(store, "", args.architecture, work_dir)
  try:
    return _check(args, repo, todo, work_dir, sync)
  finally:
    if fresh is not None:
      fresh.cleanup()


def _check(args, repo, todo, work_dir, sync):
  """Fetch everything *todo* needs, report it, and with --write record it."""

  jobs = {}
  for pkg, ent in todo.items():
    for url in ent["sources"]:
      jobs.setdefault(("source", url), lambda u=url: hash_source(u, work_dir, sync))
    for name, (_, pdir) in ent["patches"].items():
      path = os.path.join(pdir, name)
      jobs.setdefault(("patch", path), lambda p=path: checksum_file(p))
    for source, tag, _ in ent["git"]:
      jobs.setdefault(("git", source, tag), lambda s=source, t=tag: git_pin(s, t))
  results = {}
  with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
    futures = {key: pool.submit(fn) for key, fn in jobs.items()}
    for key, fut in futures.items():
      try:
        results[key] = (True, fut.result())
      except Exception as exc:  # noqa: BLE001 — reported per entry
        results[key] = (False, str(exc) or exc.__class__.__name__)

  counts = {"new": 0, "ok": 0, "unverified": 0, "MISMATCH": 0, "failed": 0, "moving": 0,
            "skipped": 0}
  wrote = []
  for pkg in sorted(todo, key=str.lower):
    ent = todo[pkg]
    path = find_checksum_file(repo, pkg)
    try:
      have = parse_checksum_file(path) if path else {"commits": {}, "sources": {}, "patches": {}}
    except (OSError, ValueError) as exc:
      error("%s: %s", pkg, exc)
      counts["failed"] += 1
      continue
    new = {"commits": {}, "sources": {}, "patches": {}}

    def report(status, what, value, key):
      counts[status] += 1
      where = ent["origin"].get(key)
      print("%-8s %s  %s  %s%s" % (status, pkg, what, value,
                                  "  [defaults-%s]" % where if where else ""))

    def check(section, key, value, inline, what):
      old = have[section].get(key)
      recorded = [c for c in (old, inline) if c]
      bad = [c for c in recorded if not _same_checksum(c, value)]
      if bad:
        report("MISMATCH", what, "%s (recorded %s)" % (value, bad[0]), key)
      elif old and _algo(old) != _algo(value) and _algo(inline or "") != _algo(value):
        report("unverified", what, "%s (recorded %s: another algorithm)" % (value, old), key)
      elif old:
        report("ok", what, value, key)
      else:
        new[section][key] = value
        report("new", what, value, key)

    for url, inline in sorted(ent["sources"].items()):
      ok, val = results[("source", url)]
      if ok:
        check("sources", url, val, inline, url)
      else:
        report("failed", url, val, url)
    for name, (inline, pdir) in sorted(ent["patches"].items()):
      ok, val = results[("patch", os.path.join(pdir, name))]
      if ok:
        check("patches", name, val, inline, "patch " + name)
      else:
        report("failed", "patch " + name, val, name)
    for source, tag, dated in ent["git"]:
      ok, val = results[("git", source, tag)]
      if not ok:
        report("failed", "tag " + tag, val, tag)
      elif val[0] == "moving" or dated:
        report("moving", "tag " + tag, "%s (a %s, not pinned)" % (val[1][:12],
               "date-based tag" if dated else "branch"), tag)
      elif val[0] == "commit":
        report("ok", "tag " + tag, "is a commit", tag)
      else:
        # A legacy `tag: <sha>` pins the recipe's own tag: check it too.
        legacy = have.get("tag") if ent["origin"].get(tag) == "" else None
        check("commits", tag, val[1], legacy, "tag " + tag)
    for entry, why in ent["skipped"]:
      report("skipped", entry, why, entry)
    for what, why, where in ent["errors"]:   # the build would stop here too
      counts["failed"] += 1
      print("%-8s %s  %s  %s%s" % ("failed", pkg, what, why,
                                  "  [defaults-%s]" % where if where else ""))

    if args.write and any(new.values()):
      try:
        written, _ = update_checksum_file(repo, pkg, new)
      except (OSError, ValueError) as exc:
        error("%s: %s", pkg, exc)
        counts["failed"] += 1
        continue
      if written:
        wrote.append(written)

  print()
  print(", ".join("%d %s" % (n, s) for s, n in counts.items() if n) or "nothing to check")
  sys.stdout.flush()                       # before the log lines below
  if wrote:
    info("Wrote %d checksum file(s) under %s", len(wrote), os.path.join(repo, "checksums"))
  elif counts["new"] and not args.write:
    info("Run again with --write to record the %d new entr%s.", counts["new"],
         "y" if counts["new"] == 1 else "ies")
  return 1 if counts["MISMATCH"] or counts["failed"] else 0
