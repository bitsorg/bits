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
    """Regenerate a bits-style modulefile for a corpus *entry*.

    Re-targets the factored ``$PREFIX`` to *prefix* (defaults to the deployed
    ``base_prefix``, i.e. the overlay points straight at the package on CVMFS),
    stamps the ``build_id`` as a queryable ``module-whatis`` (not a setenv, so it
    does not leak into the environment), emits each dependency as a ``prereq``,
    then the env ops and any verbatim extras.
    """
    target = prefix if prefix is not None else entry.get("base_prefix", "")

    def _sub(s):
        return s.replace("$PREFIX", target)

    lines = ["#%Module1.0"]
    if build_id:
        lines.append('module-whatis "build_id: %s"' % build_id)
    for dep in entry.get("deps", []):
        lines.append("prereq %s" % dep)
    for directive, var, value in entry.get("env", []):
        lines.append("%s %s %s" % (directive, var, _sub(value)))
    for line in entry.get("verbatim", []):
        lines.append(_sub(line))
    return "\n".join(lines) + "\n"


def _shell_id(name):
    """Sanitise a package name to a valid shell/env identifier fragment."""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in name)


def generate_init_sh(module_id, entry, prefix=None):
    """Synthesise a **build-sufficient** ``init.sh`` for a corpus *entry*.

    This is the ADR's highest-risk surface: a relaxed build breaks first at
    compile/link if the grafted dependency exposes only a *runtime* environment.
    So beyond replaying the modulefile's path ops as shell exports, we also
    guarantee the three things a downstream **build** needs to find a dependency:

    * ``CMAKE_PREFIX_PATH`` contains the prefix (CMake ``find_package`` config mode);
    * ``PKG_CONFIG_PATH`` contains ``lib/pkgconfig`` (autotools/pkg-config);
    * ``<Pkg>_ROOT`` / ``<PKG>_ROOT`` point at the prefix (find_package ROOT hint);
    * headers are surfaced via ``CPATH`` when an ``include/`` dir exists.

    Path-existence is guarded at runtime (``[ -d ... ]``) since the deployed tree
    is not inspectable at generation time.
    """
    target = prefix if prefix is not None else entry.get("base_prefix", "")
    pkg = module_id.split("/", 1)[0]

    def _sub(s):
        return s.replace("$PREFIX", target)

    lines = ["# build-sufficient environment for %s (generated)" % module_id,
             'P="%s"' % target]

    saw = set()
    for directive, var, value in entry.get("env", []):
        val = _sub(value)
        saw.add(var)
        if directive == "setenv":
            lines.append('export %s="%s"' % (var, val))
        elif directive == "append-path":
            lines.append('export %s="${%s:+$%s:}%s"' % (var, var, var, val))
        else:  # prepend-path
            lines.append('export %s="%s${%s:+:$%s}"' % (var, val, var, var))

    # Build-time guarantees the runtime modulefile may not have provided.
    if "CMAKE_PREFIX_PATH" not in saw:
        lines.append('export CMAKE_PREFIX_PATH="$P${CMAKE_PREFIX_PATH:+:$CMAKE_PREFIX_PATH}"')
    if "PKG_CONFIG_PATH" not in saw:
        lines.append('[ -d "$P/lib/pkgconfig" ] && '
                     'export PKG_CONFIG_PATH="$P/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"')
    lines.append('[ -d "$P/include" ] && '
                 'export CPATH="$P/include${CPATH:+:$CPATH}"')
    root = _shell_id(pkg)
    lines.append('export %s_ROOT="$P"' % root)
    if root.upper() != root:
        lines.append('export %s_ROOT="$P"' % root.upper())

    for line in entry.get("verbatim", []):
        lines.append("# verbatim: " + _sub(line))
    return "\n".join(lines) + "\n"


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
