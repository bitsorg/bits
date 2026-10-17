# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""The environment a release's bits modulefiles set, read without Tcl.

A release's merged view carries a setup.sh (cvmfs_publish.write_view_setup).
With what this module reads from the release's modulefiles in it as well, that
setup.sh gives what loading the release's modules does (the path entries the
view collapses being the view's), and `bits setenv` can source it instead of
running modulecmd over every modulefile.

Only the forms bits writes are understood: bits-recipe-tools' ModuleRecipe and
the plain setenv / prepend-path / append-path lines recipes add. Anything else
is reported as a problem: the result is then incomplete, and the release keeps
the modulecmd path.
"""

import posixpath
import re

from bits_helpers.view_cmd import COLLAPSE_VARS

BASE = "@B@"     # $::env(BASEDIR): the packages directory of the tree
VIEW = "@V@"     # the merged view
INHERITED = None  # where a path variable's value from before goes

# Set by modulecmd or bits for themselves, not by the software.
_IGNORED_SETENV = ("BITS_VIEW",)
# What setup.sh / setup.csh can write without quoting.
_SAFE_VALUE = re.compile(r"^[\w@./:+,=%-]*$")

_WORD = r'(?:"[^"]*"|\{[^{}]*\}|[^\s{}"\[\]]+)'
_SKIP = re.compile(r"^(?:#|module-whatis\b|conflict\b|module-version\b|module-alias\b)")
_SET = re.compile(r"^set\s+(\w+)\s+(%s)$" % _WORD)
_SETENV = re.compile(r"^setenv\s+(\w+)\s+(%s)$" % _WORD)
_PATH = re.compile(r"^(prepend-path|append-path)\s+(\w+)\s+(%s)$" % _WORD)
_LOAD = re.compile(r"^(module\s+(?:load|add)|prereq|depends-on)\s+(.+)$")
_IF_LOADED = re.compile(
    r"^if\s+\{?\s*!\s*\[\s*is-loaded\s+['\"]?([^'\"\]\s]+)['\"]?\s*\]\s*\}?\s*"
    r"\{\s*module\s+load\s+(\S+)\s*\}$")
_IF_DIR = re.compile(
    r"^if\s+\{\s*\[\s*file\s+isdirectory\s+(%s)\s*\]\s*\}\s*"
    r"\{\s*(prepend-path|append-path)\s+(\w+)\s+(%s)\s*\}$" % (_WORD, _WORD))
# ModuleRecipe's man pages: setup.sh adds the view's itself, on the same condition.
_IF_MANPATH = re.compile(r"^if\s+\{\[info exists ::env\(MANPATH\)\].*prepend-path MANPATH \S+\s*\}$")
_REF = re.compile(r"\$::env\((\w+)\)|\$env\((\w+)\)|\$\{(\w+)\}|\$(\w+)")


class Unsupported(Exception):
    """A modulefile form this module does not read."""


def commands(text):
    """The modulefile's commands, one string each (a braced block joined to
    one line), without the ModulesHelp proc."""
    out, cur, depth = [], [], 0
    for line in text.splitlines():
        s = line.strip()
        if not cur and (not s or s.startswith("#")):
            continue
        cur.append(s)
        depth += s.count("{") - s.count("}")
        if depth <= 0:
            cmd = " ".join(p for p in cur if p)
            cur, depth = [], 0
            if not cmd.startswith("proc "):
                out.append(cmd)
    if cur:
        raise Unsupported("unbalanced braces: %s" % " ".join(cur)[:80])
    return out


def subst(word, tcl_vars, env):
    """*word* with its variables substituted, as Tcl would: braces quote
    literally; $::env(BASEDIR) is BASE; other env variables must have been set
    by a modulefile loaded before."""
    if word.startswith("{") and word.endswith("}"):
        return word[1:-1]
    if word.startswith('"') and word.endswith('"'):
        word = word[1:-1]
    if "[" in word or "\\" in word:
        raise Unsupported("command or escape in %s" % word)

    def one(m):
        name = m.group(1) or m.group(2)
        if name:
            if name == "BASEDIR":
                return BASE
            if name in env:
                return env[name]
            raise Unsupported("$env(%s) is not set by the release" % name)
        name = m.group(3) or m.group(4)
        if name not in tcl_vars:
            raise Unsupported("$%s is not set" % name)
        return tcl_vars[name]
    return _REF.sub(one, word)


def read_modulefile(text):
    """The statements of one modulefile, in order: ("set", name, word),
    ("load", module) and ("op", command, var, word, guard), guard a directory
    that must exist (a word), or None. Words are substituted when the module
    is loaded (see release_ops)."""
    out = []
    for cmd in commands(text):
        if _SKIP.match(cmd) or _IF_MANPATH.match(cmd):
            continue
        m = _SET.match(cmd)
        if m:
            out.append(("set", m.group(1), m.group(2)))
            continue
        m = _IF_LOADED.match(cmd)
        if m:
            out.append(("load", m.group(2)))
            continue
        m = _LOAD.match(cmd)
        if m:
            names = m.group(2).split()
            if m.group(1) == "prereq" and len(names) > 1:   # any one of them
                raise Unsupported("prereq of several modules: %s" % cmd[:80])
            out.extend(("load", n) for n in names)
            continue
        m = _SETENV.match(cmd)
        if m:
            out.append(("op", "setenv", m.group(1), m.group(2), None))
            continue
        m = _PATH.match(cmd)
        if m:
            out.append(("op", m.group(1), m.group(2), m.group(3), None))
            continue
        m = _IF_DIR.match(cmd)
        if m:
            out.append(("op", m.group(2), m.group(3), m.group(4), m.group(1)))
            continue
        raise Unsupported("unknown command: %s" % cmd[:80])
    return out


def release_ops(top, members, read, is_dir):
    """The environment operations of loading the release's *top* modules, in
    the order modulecmd applies them (a module it loads, where it loads it),
    and the problems that make the result incomplete. *members* maps each
    package of the release to its version directory (<version>-<revision>);
    read(pkg) gives its modulefile text, or None; is_dir(pkg, rel) whether its
    package-relative *rel* is a directory. Both may raise Unsupported.
    Returns ([(pkg, command, var, value)], [problem, ...])."""
    out, problems, env, done = [], [], {}, set()

    def load(name, via):
        pkg, _, ver = name.partition("/")
        if name == "BASE/1.0":
            return
        if not ver:   # modulecmd takes the tree's default, maybe of another release
            problems.append("%s loads %s without a version" % (via, name))
            return
        if members.get(pkg) != ver:
            problems.append("%s loads %s, not a module of this release" % (via, name))
            return
        if pkg in done:
            return
        done.add(pkg)
        try:
            text = read(pkg)
            if text is None:
                raise Unsupported("no modulefile")
            statements = read_modulefile(text)
        except Unsupported as exc:
            problems.append("%s: %s" % (pkg, exc))
            return
        root = "%s/%s/%s" % (BASE, pkg, members[pkg])
        tcl_vars = {}
        for st in statements:
            try:
                if st[0] == "set":
                    tcl_vars[st[1]] = subst(st[2], tcl_vars, env)
                    continue
                if st[0] == "load":
                    load(st[1], pkg)
                    continue
                _, cmd, var, word, guard = st
                value = subst(word, tcl_vars, env)
                if guard is not None:
                    g = posixpath.normpath(subst(guard, tcl_vars, env))
                    if g != root and not g.startswith(root + "/"):
                        raise Unsupported("tests a directory outside the package: %s" % g)
                    if not is_dir(pkg, g[len(root) + 1:]):
                        continue
            except Unsupported as exc:
                problems.append("%s: %s" % (pkg, exc))
                continue
            if cmd == "setenv":
                env[var] = value
            out.append((pkg, cmd, var, value))

    for name in top:
        load("%s/%s" % (name, members.get(name, "")), "the release")
    return out, problems


def setup_env(ops, members, in_view):
    """What setup.sh sets for *ops* (from release_ops): ({var: [entries]} for
    the path variables, in order, INHERITED where the value from before goes;
    {var: value} for the others; problems). The path variables are built as
    modulecmd builds them; then, as the view helper collapses a loaded
    environment, a collapsed variable's entry for a package below what the view
    links of it (in_view(pkg, rel), rel "" for the package root) becomes the
    view's, keeping the first of equal entries."""
    raw, sets, problems = {}, {}, []
    for pkg, cmd, var, value in ops:
        if not _SAFE_VALUE.match(value):
            problems.append("%s: %s's value needs quoting: %s" % (pkg, var, value))
        elif any(v.startswith("/") for v in value.split(":")):   # e.g. the build's own path
            problems.append("%s: %s is an absolute path: %s" % (pkg, var, value))
        elif cmd == "setenv":
            if var not in _IGNORED_SETENV:
                sets[var] = value
        elif ":" in value:
            problems.append("%s: %s gets several entries at once: %s" % (pkg, var, value))
        else:
            entries = raw.setdefault(var, [INHERITED])
            if any(e and e[1] == value for e in entries):
                continue   # modulecmd does not add an entry twice
            if cmd == "prepend-path":
                entries.insert(0, (pkg, value))
            else:
                entries.append((pkg, value))
    paths = {}
    for var, entries in raw.items():
        out = []
        for e in entries:
            if e is not INHERITED and var in COLLAPSE_VARS:
                pkg, value = e
                root = "%s/%s/%s" % (BASE, pkg, members[pkg])
                rel = value[len(root) + 1:] if value.startswith(root + "/") else (
                    "" if value == root else None)
                if rel is not None and in_view(pkg, rel):
                    value = VIEW + ("/" + rel if rel else "")
                e = value
            elif e is not INHERITED:
                e = e[1]
            if e not in out:
                out.append(e)
        paths[var] = out
    for var in sorted(set(paths) & set(sets)):
        problems.append("%s is both set and extended" % var)
    return paths, sets, problems
