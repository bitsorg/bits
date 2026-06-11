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
