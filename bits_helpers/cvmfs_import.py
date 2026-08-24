# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Importer for foreign CVMFS deployments (ADR-0001 Stage 2).

Turns a deployed release that lacks bits-native metadata (e.g. an LCG release)
into a bits-consumable overlay: harvest each deployed modulefile's *resolved*
operations, classify them, and (later) regenerate bits modulefiles + a build_id.

This module currently provides the harvest parser. We parse the resolved output
of ``modulecmd sh display <pkg>/<ver>`` (or ``module show``) rather than the raw
Tcl, so environment-modules does the interpretation and we only classify the
concrete operations it would perform:

* ``prepend-path`` / ``append-path`` and ``setenv``  → structured env ops
* ``module load|add`` / ``prereq`` / ``depends-on``  → dependency edges (remappable)
* module-whatis / conflict / module-version / set / comments / separators → ignored
* anything else                                       → kept verbatim (so a
  package that does something unusual is reproduced faithfully)

The dependency edges are captured structurally (not as opaque text) so their
names can be remapped through the lcg.bits alias table at generation time, and
so they form the release graph used for the closure/`build_id` check.
"""

# Module subcommands that declare a runtime dependency (the cascade that
# produces the "Loading requirement:" output).
_DEP_DIRECTIVES = ("prereq", "prereq-all", "depends-on")
# Tcl/module lines that carry no environment and are not dependencies.
_IGNORED = ("module-whatis", "conflict", "conflicts", "module-version",
            "module-alias", "set", "set-function", "puts", "if", "}")


def _strip_path_flags(tokens):
    """Drop leading option flags from a (prepend|append)-path argument list.

    Handles the common ``-d <delim>`` / ``--delim <delim>`` / ``--duplicates``
    forms so that the variable name and value are isolated. Returns the
    remaining tokens (``[VAR, VALUE...]``) or None if they cannot be isolated.
    """
    toks = list(tokens)
    while toks and toks[0].startswith("-"):
        flag = toks.pop(0)
        if flag in ("-d", "--delim") and toks:
            toks.pop(0)            # consume the delimiter argument
    return toks if len(toks) >= 2 else None


def parse_module_display(text):
    """Parse resolved `modulecmd display` / `module show` output.

    Returns ``{"ops": [...], "deps": [...], "verbatim": [...]}`` where each op is
    ``(directive, var, value)`` with directive in
    ``{prepend-path, append-path, setenv}``; *deps* is the ordered, de-duplicated
    list of dependency module names; *verbatim* is the list of unclassified
    non-trivial lines, preserved for faithful regeneration.
    """
    ops, deps, verbatim = [], [], []
    seen_dep = set()

    def _add_dep(name):
        if name and name not in seen_dep:
            seen_dep.add(name)
            deps.append(name)

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if set(line) <= set("-=_ "):        # separator rule lines
            continue
        toks = line.split()
        cmd = toks[0]

        if cmd in ("prepend-path", "append-path"):
            rest = _strip_path_flags(toks[1:])
            if rest:
                ops.append((cmd, rest[0], " ".join(rest[1:])))
            else:
                verbatim.append(line)
        elif cmd == "setenv" and len(toks) >= 3:
            ops.append((cmd, toks[1], " ".join(toks[2:])))
        elif cmd == "module" and len(toks) >= 3 and toks[1] in ("load", "add"):
            for d in toks[2:]:
                _add_dep(d)
        elif cmd in _DEP_DIRECTIVES:
            for d in toks[1:]:
                _add_dep(d)
        elif cmd in _IGNORED:
            continue
        else:
            verbatim.append(line)

    return {"ops": ops, "deps": deps, "verbatim": verbatim}


# ── Corpus builder: classify a package's ops into a generic BitsModule shape ──

# Path variables that the generic BitsModule template already knows how to emit,
# mapped to the recipe MODULE_OPTIONS category they correspond to. A package whose
# path ops are only these collapses to "BitsModule(options)"; anything else is
# kept verbatim.
_PATH_CATEGORY = {
    "PATH": "bin",
    "LD_LIBRARY_PATH": "lib",
    "DYLD_LIBRARY_PATH": "lib",
    "CMAKE_PREFIX_PATH": "lib",
    "PYTHONPATH": "python",
    "PKG_CONFIG_PATH": "pkgconfig",
}


def _factor(value, base_prefix):
    """Replace the package install prefix with the ``$PREFIX`` placeholder so the
    generated overlay can re-target it (prefix factoring)."""
    if base_prefix and value.startswith(base_prefix):
        return "$PREFIX" + value[len(base_prefix):]
    return value


def _factor_line(line, base_prefix):
    return line.replace(base_prefix, "$PREFIX") if base_prefix else line


def factor_ops(ops, base_prefix):
    """Return *ops* with the install prefix factored to ``$PREFIX`` (lossless).

    Keeping the full ops — not just a category summary — matters: the exact
    sub-paths (e.g. ``lib/python3.13/site-packages``, ``lib64`` vs ``lib``) must
    be reproduced when the overlay modulefile is regenerated.
    """
    return [(directive, var, _factor(value, base_prefix))
            for directive, var, value in ops]


def summarize_options(ops, base_prefix):
    """Derived summary: which generic BitsModule categories a package's path ops
    cover (bin/lib/python/pkgconfig). Informational — generation uses the full
    ops, not this — but useful for reporting how "standard" the imported set is.
    """
    options = []
    for directive, var, value in ops:
        if (directive in ("prepend-path", "append-path")
                and var in _PATH_CATEGORY and base_prefix
                and value.startswith(base_prefix)):
            cat = _PATH_CATEGORY[var]
            if cat not in options:
                options.append(cat)
    return options


def build_corpus_entry(display_text, base_prefix, version=None, revision=None):
    """Build one corpus entry from a package's ``module show`` text.

    Returns ``{version, revision, base_prefix, env, options, verbatim, deps}``:
    ``env`` is the prefix-factored list of ``(directive, var, value)`` ops (the
    source of truth for regeneration); ``options`` is the derived category
    summary; ``verbatim`` is the prefix-factored unparsed lines; ``deps`` is the
    dependency edge list.
    """
    parsed = parse_module_display(display_text)
    return {
        "version": version,
        "revision": revision,
        "base_prefix": base_prefix,
        "env": factor_ops(parsed["ops"], base_prefix),
        "options": summarize_options(parsed["ops"], base_prefix),
        "verbatim": [_factor_line(line, base_prefix) for line in parsed["verbatim"]],
        "deps": parsed["deps"],
    }


def generate_modulefile(module_id, entry, build_id, prefix=None):
    """Regenerate a **build-sufficient** bits-style modulefile for a corpus entry.

    The modulefile is the single source of truth for the package's environment:
    loading it (via environment-modules or ``bits printenv``) yields an env
    sufficient to *build* against the package, so imported packages are consumed
    exactly like bits-native ones — no separate ``init.sh`` sidecar is needed.

    Re-targets the factored ``$PREFIX`` to *prefix* (the deployed path), stamps
    the ``build_id`` as a queryable ``module-whatis`` (not a setenv, so it does
    not leak into the environment), emits each dependency as a ``prereq`` and the
    harvested env ops, then adds the few build-time hooks a runtime-only
    modulefile might omit (``CMAKE_PREFIX_PATH`` / ``PKG_CONFIG_PATH`` / ``CPATH``
    / ``<Pkg>_ROOT``) — each guarded on the deployed tree so it never introduces
    a dangling path.
    """
    target = prefix if prefix is not None else entry.get("base_prefix", "")
    pkg = module_id.split("/", 1)[0]

    def _sub(s):
        return s.replace("$PREFIX", target)

    lines = ["#%Module1.0"]
    if build_id:
        lines.append('module-whatis "build_id: %s"' % build_id)
    for dep in entry.get("deps", []):
        lines.append("prereq %s" % dep)

    saw = set()
    for directive, var, value in entry.get("env", []):
        saw.add(var)
        lines.append("%s %s %s" % (directive, var, _sub(value)))

    # Build-time hooks the harvested runtime ops may not have provided.
    if "CMAKE_PREFIX_PATH" not in saw:
        lines.append("prepend-path CMAKE_PREFIX_PATH %s" % target)
    if "PKG_CONFIG_PATH" not in saw:
        lines.append('if {[file isdirectory "%s/lib/pkgconfig"]} {' % target)
        lines.append("    prepend-path PKG_CONFIG_PATH %s/lib/pkgconfig" % target)
        lines.append("}")
    if "CPATH" not in saw:
        lines.append('if {[file isdirectory "%s/include"]} {' % target)
        lines.append("    prepend-path CPATH %s/include" % target)
        lines.append("}")
    root = _shell_id(pkg)
    lines.append("setenv %s_ROOT %s" % (root, target))
    if root.upper() != root:
        lines.append("setenv %s_ROOT %s" % (root.upper(), target))

    for line in entry.get("verbatim", []):
        lines.append(_sub(line))
    return "\n".join(lines) + "\n"


def _shell_id(name):
    """Sanitise a package name to a valid shell/env identifier fragment."""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name)


def rewrite_module_anchor(text, install_base):
    """Re-anchor a bits-built modulefile so it no longer needs the shared BASEDIR.

    A deployed bits modulefile derives its install root from ``$::env(BASEDIR)``
    (set by the BASE module), so the first BASE on MODULEPATH pins the location
    for every package. When such a modulefile is reused next to a local build
    that would misplace it. Replace the ``BASEDIR`` env reference with the
    absolute *install_base* so the copy is self-anchoring and order-independent;
    everything else (guards, deps, $version) is preserved verbatim.
    """
    base = (install_base or "").rstrip("/")
    return (text.replace("$::env(BASEDIR)", base)
                .replace("$env(BASEDIR)", base))


def _read_meta(path):
    """Load a JSON ``.meta.json`` defensively; None on any read/parse error."""
    import json
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _is_base_id(token):
    """True if *token* names the BASE module (BASE or BASE/<ver>), quotes/braces
    tolerated — matched as a whole id so BASECAMP/BASELINE are NOT BASE."""
    t = token.strip("'\"{}")
    return t == "BASE" or t.startswith("BASE/")


def _line_loads_base(line):
    """True if *line* has a ``load`` / ``prereq`` / ``is-loaded`` directive whose
    next token is the BASE module id — matched by token so ``BASECAMP`` etc. are
    not mis-matched."""
    _directives = ("load", "prereq", "prereq-all", "depends-on", "is-loaded")
    toks = line.split()
    return any(tok in _directives and i + 1 < len(toks) and _is_base_id(toks[i + 1])
               for i, tok in enumerate(toks))


def strip_base_dep(text):
    """Drop the ``BASE`` module dependency from a re-anchored modulefile.

    A deployed bits modulefile loads ``BASE`` only to obtain ``BASEDIR``; once
    ``rewrite_module_anchor`` has inlined that as an absolute path, ``BASE`` is
    unneeded (and may be absent from the reuse set). Real deps (``CMake``,
    ``Python``…) are kept. Handles both the one-line guard and the multi-line
    ``if ![ is-loaded 'BASE/1.0' ] {`` / ``module load BASE/1.0`` / ``}`` block:
    when the BASE line opens an unbalanced ``{``, consume through its matching
    ``}`` so no orphan brace is left behind.
    """
    out = []
    lines = text.splitlines()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _line_loads_base(line):
            depth = line.count("{") - line.count("}")
            i += 1
            while depth > 0 and i < n:
                depth += lines[i].count("{") - lines[i].count("}")
                i += 1
            continue
        out.append(line)
        i += 1
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _module_load_deps(text):
    """Ordered, de-duplicated ``module load <id>`` targets in *text*, excluding BASE."""
    deps = []
    for line in text.splitlines():
        toks = line.split()
        for i, tok in enumerate(toks):
            if tok == "load" and i and toks[i - 1] == "module" and i + 1 < len(toks):
                dep = toks[i + 1].strip("'\"{}")
                if dep and dep != "BASE" and not dep.startswith("BASE/") and dep not in deps:
                    deps.append(dep)
    return deps


def harvest_trusted(module_root, install_base):
    """Harvest a bits-built deployment into a re-anchored corpus.

    *module_root* is the deployed modulefiles tree (``<pkg>/<verrev>`` files, e.g.
    ``.../Modules/modulefiles``); *install_base* is the absolute ``Packages`` root
    the modulefiles' ``BASEDIR`` should resolve to. For each modulefile: re-anchor
    it to *install_base*, strip its ``BASE`` dep, and read the co-located package
    ``.meta.json`` (``<install_base>/<pkg>/<verrev>/.meta.json``) for the content
    hash / build_id. Returns ``(corpus, package_hashes, build_id)``; build_id is
    the deployment's recorded one (all packages share it), or "" if none carried it.
    """
    import json
    import os
    corpus, hashes, build_id = {}, {}, ""
    if not (module_root and os.path.isdir(module_root)):
        return corpus, hashes, build_id
    for pkg in sorted(os.listdir(module_root)):
        pkg_dir = os.path.join(module_root, pkg)
        if not os.path.isdir(pkg_dir):
            continue
        for verrev in sorted(os.listdir(pkg_dir)):
            mfile = os.path.join(pkg_dir, verrev)
            if not os.path.isfile(mfile):
                continue
            with open(mfile) as fh:
                rendered = strip_base_dep(rewrite_module_anchor(fh.read(), install_base))
            module_id = "%s/%s" % (pkg, verrev)
            meta = _read_meta(os.path.join(install_base, pkg, verrev, ".meta.json")) or {}
            pkg_info = meta.get("package") if isinstance(meta.get("package"), dict) else {}
            hashes[module_id] = pkg_info.get("hash", "")
            build_id = build_id or meta.get("build_id", "")
            corpus[module_id] = {
                "version": pkg_info.get("version"),
                "revision": pkg_info.get("revision"),
                "deps": _module_load_deps(rendered),
                "rendered": rendered,
                "base_prefix": install_base,
            }
    return corpus, hashes, build_id


def import_trusted_release(module_root, install_base, arch, out_root, label="reuse",
                           force=False):
    """Import a trusted bits deployment: harvest → build_id → write overlay.

    Uses the deployment's recorded build_id when present, else a corpus-derived
    one. Returns ``{"build_id", "written", "dangling"}`` (same shape as
    ``import_release``).
    """
    import os
    corpus, hashes, dep_build_id = harvest_trusted(module_root, install_base)
    dangling = closure_check(corpus)
    if dangling and not force:
        return {"build_id": None, "written": [], "dangling": dangling,
                "overlay_path": None}
    build_id = dep_build_id or compute_corpus_build_id(corpus, label)
    written = write_overlay(corpus, build_id, arch, out_root, package_hashes=hashes)
    # Return the overlay path so callers need not reconstruct <out_root>/<id>/<arch>.
    return {"build_id": build_id, "written": written, "dangling": dangling,
            "overlay_path": os.path.join(out_root, build_id, arch)}


def build_module_meta(module_id, entry, build_id, package_hash="", abi_tag=""):
    """Module-side ``.meta.json`` payload for a corpus *entry* (D6 overlay).

    Co-located with the generated modulefile (not the foreign package tree), this
    is what relaxed reuse reads: the ``build_id`` coherence token plus the
    identity (name/version/revision/hash) and ``abi_tag`` needed to graft the
    deployed tree without recompiling. Additive and self-contained.
    """
    pkg = module_id.split("/", 1)[0]
    return {
        "package": pkg,
        "module_id": module_id,
        "version": entry.get("version"),
        "revision": entry.get("revision"),
        "hash": package_hash,
        "build_id": build_id,
        "abi_tag": abi_tag,
        "base_prefix": entry.get("base_prefix", ""),
        "deps": list(entry.get("deps", [])),
        "imported": True,
    }


# ── Closure check + deterministic build_id ───────────────────────────────────
#
# A corpus is ``{module_id: entry}`` where *module_id* is the fully-qualified
# module name as it appears in dependency edges (e.g. ``"ROOT/6.38.00"``). The
# closure check ensures every edge points inside the corpus, so the whole set is
# a self-contained, internally-consistent release before it is stamped with one
# build_id — the token that lets relaxed reuse adopt any subset ABI-safely.

def closure_check(corpus):
    """Return the sorted dependency edges that point *outside* the corpus.

    An empty list means the corpus is closed (every ``deps`` target is a known
    module). A non-empty list is a refusal reason — do not assign a build_id.
    """
    keys = set(corpus)
    dangling = set()
    for entry in corpus.values():
        for dep in entry.get("deps", ()):
            if dep not in keys:
                dangling.add(dep)
    return sorted(dangling)


def compute_corpus_build_id(corpus, label):
    """Return a deterministic, content-derived build_id for *corpus*.

    ``<label>-<digest>`` where the digest is a hash over the sorted, normalised
    corpus, so an independent or repeated import of the same release yields the
    same id (matching the ``provenance.compute_build_id`` format used natively).
    """
    import hashlib
    import json
    members = sorted(
        [mid,
         entry.get("base_prefix", ""),
         [list(op) for op in entry.get("env", [])],
         list(entry.get("verbatim", [])),
         list(entry.get("deps", []))]
        for mid, entry in corpus.items()
    )
    digest = hashlib.sha256(
        json.dumps(members, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return "%s-%s" % (label, digest)


# ── Name-alias map (D8): lcg.bits name <-> foreign module name ────────────────
#
# The single fuzzy, human-maintained step. Foreign deployments name modules in
# their own scheme (e.g. LCG's "ROOT"); bits recipes use their own (e.g. "root").
# The alias map translates foreign → bits at generation time so the overlay's
# module ids and prereq edges resolve against bits-native names. Names absent
# from the map pass through unchanged; `unmapped()` reports the gaps to fill in.

class AliasMap(object):

    def __init__(self, foreign_to_bits=None):
        self._f2b = dict(foreign_to_bits or {})
        self._b2f = {}
        for foreign, bits in self._f2b.items():
            self._b2f.setdefault(bits, foreign)   # first wins on collision

    def to_bits(self, name):
        return self._f2b.get(name, name)

    def to_foreign(self, name):
        return self._b2f.get(name, name)

    def unmapped(self, foreign_names):
        """Foreign names with no explicit bits alias (the human to-do list)."""
        return sorted(n for n in set(foreign_names) if n not in self._f2b)

    @classmethod
    def load(cls, path):
        """Load a map from JSON. Accepts ``{foreign: bits}``, ``{"aliases": {..}}``
        or ``[[foreign, bits], ...]``. Returns an empty (identity) map on error."""
        import json
        try:
            with open(path) as fh:
                data = json.load(fh)
        except Exception:
            return cls()
        if isinstance(data, dict):
            data = data.get("aliases", data)
        if isinstance(data, dict):
            return cls({str(k): str(v) for k, v in data.items()})
        if isinstance(data, list):
            try:
                return cls({str(a): str(b) for a, b in data})
            except Exception:
                return cls()
        return cls()


def _remap_id(module_id, fn):
    """Apply name-mapping *fn* to the name component of ``name/version``."""
    if "/" in module_id:
        name, ver = module_id.split("/", 1)
        return fn(name) + "/" + ver
    return fn(module_id)


# ── Harvest driver + manifest fallback + overlay writer ───────────────────────

def _infer_base_prefix(ops, module_id=""):
    """Best-effort install prefix of a package from its own path ops.

    Prefer a ``PATH .../bin`` (strip ``/bin``), then a ``*LIBRARY_PATH .../lib``;
    else the common path of all absolute op values. Used when the harvest source
    does not state the prefix explicitly.
    """
    import os
    for _d, var, val in ops:
        if var == "PATH" and val.endswith("/bin"):
            return val[:-len("/bin")]
    for _d, var, val in ops:
        if var in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH") and val.endswith("/lib"):
            return val[:-len("/lib")]
    vals = [v for _d, _v, v in ops if v.startswith("/")]
    if not vals:
        return ""
    try:
        return os.path.commonpath(vals)
    except Exception:
        return os.path.dirname(vals[0])


def harvest_display(module_id, modulepath, modulecmd="modulecmd", base_prefix=None):
    """Harvest one deployed module into a corpus entry by running
    ``modulecmd sh display <module_id>`` under *modulepath*.

    environment-modules writes the resolved display to **stderr**. Returns the
    corpus entry, or None if the command could not be run. The shell-out is the
    only non-pure part of the importer; everything downstream is testable.
    """
    import os
    import subprocess
    env = dict(os.environ)
    env["MODULEPATH"] = modulepath
    try:
        proc = subprocess.run([modulecmd, "sh", "display", module_id],
                              env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, universal_newlines=True)
    except Exception:
        return None
    text = proc.stderr or proc.stdout or ""
    ver = module_id.split("/", 1)[1] if "/" in module_id else None
    parsed = parse_module_display(text)
    prefix = base_prefix or _infer_base_prefix(parsed["ops"], module_id)
    return build_corpus_entry(text, prefix, version=ver)


def corpus_from_manifest(manifest):
    """Fallback corpus builder for deployments without modulefiles.

    *manifest* is ``{"packages": [item, ...]}`` or a bare list, where each item
    is ``{module_id, base_prefix, env, deps, version, revision[, verbatim]}`` and
    ``env`` is the already-factored ``[[directive, var, value], ...]`` list.
    """
    items = manifest.get("packages", manifest) if isinstance(manifest, dict) else manifest
    corpus = {}
    for it in items:
        mid = it["module_id"]
        corpus[mid] = {
            "version": it.get("version"),
            "revision": it.get("revision"),
            "base_prefix": it.get("base_prefix", ""),
            "env": [tuple(op) for op in it.get("env", [])],
            "options": list(it.get("options", [])),
            "verbatim": list(it.get("verbatim", [])),
            "deps": list(it.get("deps", [])),
        }
    return corpus


def _unsafe_component(*comps):
    """True if any path component would escape its parent (absolute or contains a
    ``..`` segment) — a guard against path traversal via a hostile module id."""
    for c in comps:
        norm = (c or "").replace("\\", "/")
        if not norm or norm.startswith("/") or ".." in norm.split("/"):
            return True
    return False


def write_overlay(corpus, build_id, arch, out_root, alias=None,
                  package_hashes=None, abi_tag=""):
    """Write the per-build_id module+metadata overlay (ADR-0001 D6/D10).

    Layout (each build_id dir is one CVMFS nested catalog)::

        <out_root>/<build_id>/<arch>/
            .cvmfscatalog
            <bits_name>/<version>             # build-sufficient Tcl modulefile
            <bits_name>/.<version>.meta.json  # relaxed-resolver metadata (hidden)

    The modulefile is the single environment artifact (no init.sh): loading it
    yields a build-sufficient env, the same way bits-native packages are
    consumed. Foreign names (module ids and dep edges) are remapped to bits names
    through *alias*. Returns the sorted list of written bits module ids.
    """
    import json
    import os
    alias = alias or AliasMap()
    package_hashes = package_hashes or {}
    arch_root = os.path.join(out_root, build_id, arch)
    written = []
    for module_id, entry in corpus.items():
        bits_id = _remap_id(module_id, alias.to_bits)
        remapped = dict(entry)
        remapped["deps"] = [_remap_id(d, alias.to_bits) for d in entry.get("deps", [])]
        name, _, ver = bits_id.partition("/")
        vfile = ver or "default"
        # Refuse a module id whose name/version would write outside the overlay
        # (path traversal); the names come from foreign modulefiles / a manifest.
        if _unsafe_component(name, vfile):
            continue
        dest = os.path.join(arch_root, name)
        os.makedirs(dest, exist_ok=True)
        # A trusted-harvest entry carries the deployment's own modulefile,
        # already re-anchored (entry["rendered"]); a foreign one is regenerated
        # from its parsed ops. One writer, two sources.
        with open(os.path.join(dest, vfile), "w") as fh:
            fh.write(entry.get("rendered")
                     or generate_modulefile(bits_id, remapped, build_id))
        meta = build_module_meta(bits_id, entry, build_id,
                                 package_hash=package_hashes.get(module_id, ""),
                                 abi_tag=abi_tag)
        with open(os.path.join(dest, ".%s.meta.json" % vfile), "w") as fh:
            json.dump(meta, fh, indent=2, sort_keys=True)
        written.append(bits_id)
    os.makedirs(os.path.join(out_root, build_id), exist_ok=True)
    open(os.path.join(out_root, build_id, ".cvmfscatalog"), "w").close()
    return sorted(written)


def import_release(corpus, label, arch, out_root, alias=None, abi_tag="",
                   force=False):
    """Orchestrate: closure-check → build_id → write overlay.

    Returns ``{"build_id", "written", "dangling"}``. If the corpus is not closed
    and *force* is false, no overlay is written and ``build_id`` is None — a
    non-closed release cannot be coherently stamped.
    """
    dangling = closure_check(corpus)
    if dangling and not force:
        return {"build_id": None, "written": [], "dangling": dangling}
    build_id = compute_corpus_build_id(corpus, label)
    written = write_overlay(corpus, build_id, arch, out_root, alias=alias,
                            abi_tag=abi_tag)
    return {"build_id": build_id, "written": written, "dangling": dangling}
