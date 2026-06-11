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


def classify_ops(ops, base_prefix):
    """Split env ops into generic BitsModule *options* and *verbatim* extras.

    A (prepend|append)-path op on a known path variable whose value lives under
    the package's install prefix contributes its category (bin/lib/python/
    pkgconfig) to ``options`` (de-duplicated, order-preserving). Everything else —
    other path variables, paths pointing outside the prefix, every ``setenv`` —
    is kept verbatim with the prefix factored to ``$PREFIX``.
    """
    options, verbatim = [], []
    for directive, var, value in ops:
        if (directive in ("prepend-path", "append-path")
                and var in _PATH_CATEGORY and base_prefix
                and value.startswith(base_prefix)):
            cat = _PATH_CATEGORY[var]
            if cat not in options:
                options.append(cat)
        else:
            verbatim.append("%s %s %s" % (directive, var, _factor(value, base_prefix)))
    return {"options": options, "verbatim": verbatim}


def build_corpus_entry(display_text, base_prefix, version=None, revision=None):
    """Build one corpus entry from a package's ``module show`` text.

    Returns ``{version, revision, base_prefix, options, verbatim, deps}`` — the
    prefix-factored, classified representation used to regenerate a bits modulefile
    and to form the release dependency graph.
    """
    parsed = parse_module_display(display_text)
    classified = classify_ops(parsed["ops"], base_prefix)
    verbatim = classified["verbatim"] + [
        _factor_line(line, base_prefix) for line in parsed["verbatim"]
    ]
    return {
        "version": version,
        "revision": revision,
        "base_prefix": base_prefix,
        "options": classified["options"],
        "verbatim": verbatim,
        "deps": parsed["deps"],
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
         list(entry.get("options", [])),
         list(entry.get("verbatim", [])),
         list(entry.get("deps", []))]
        for mid, entry in corpus.items()
    )
    digest = hashlib.sha256(
        json.dumps(members, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return "%s-%s" % (label, digest)
