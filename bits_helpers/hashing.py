# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""Content-addressable build hashing: turn a resolved spec (recipe body, deps,
defaults, provider hashes) into the hash(es) that name its tarball and install
dir. Split out of build.py; the alidist hash path is covered by the Phase 0
regression harness."""

import os
import re
import time
from collections import OrderedDict

from bits_helpers.log import debug
from bits_helpers.utilities import Hasher


_HEREDOC_START = re.compile(r"<<-?\s*([\"']?)([A-Za-z_][A-Za-z0-9_]*)\1")


# Front-matter keys that are metadata / publish-policy ONLY: they never affect
# what is built, so they are dropped from the HASH input (exactly like comments).
# Editing a license, description, project URL, attribution, source link, or the
# redistributable flag therefore does NOT change a package's hash — no rebuild and
# no re-publish. The executed recipe keeps every field; only hashing ignores these.
_HASH_EXCLUDED_META_KEYS = frozenset({
    "license", "description", "url", "homepage",
    "acknowledgment", "acknowledgement", "source_url", "redistributable",
    # preload: CVMFS filebundle test list, consumed post-publish by `bits preload`;
    # it never affects the build, so editing it must not force a rebuild.
    "preload",
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
