# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""`bits use` — save reusable command-line args per directory so repeated
commands stay short. The profile holds raw CLI tokens, structured by the
command they apply to.

A ``[common]`` section is injected into arch-aware commands (put only broadly
accepted args here, i.e. ``--architecture``, which ``build`` AND ``q``/``enter``/
``clean`` all take); a per-command section (``[build]``, ``[q]``, …) adds args
that command accepts — e.g. ``--defaults`` belongs in ``[build]``, NOT
``[common]``. Injected BEFORE the user's own args, so those override
single-value options. Example::

    [common]
    --architecture x86_64-el9-gcc14-opt

    [build]
    --defaults lcg::release::gcc14::opt --docker --sandbox off --reuse-from cvmfs::relaxed

Storage (two-tier)
------------------
The profile lives in ``./.bitsuse`` when the current directory is writeable and
owned by you. When it is not (a shared or read-only checkout), it lives instead
under ``~/.bits/use/<key>`` keyed by the real path of the directory, so a choice
made with ``bits use`` still persists for that directory. A local ``.bitsuse``
is honoured only when it is owned by the invoking user — otherwise it is ignored
(it is injected into argv before parsing, so a world-writeable checkout must not
be able to plant one) and the home record is used. ``.bitscmd`` is the previous
name and is still read as a fallback.

Prototype — runs standalone::

    python3 -m bits_helpers.bits_use common --architecture x86_64-el9-gcc14-opt
    python3 -m bits_helpers.bits_use build  --docker --sandbox off
    python3 -m bits_helpers.bits_use            # show the active profile + source
    python3 -m bits_helpers.bits_use --clear [SECTION]
"""

import hashlib
import os
import shlex
import sys

PROFILE = ".bitsuse"            # per-directory saved-arg profile
LEGACY_PROFILE = ".bitscmd"     # previous name, still read as a fallback
HOME_STORE = os.path.join(os.path.expanduser("~"), ".bits", "use")
COMMON = "common"               # section injected into every command ('global' alias)


# ── storage resolution (two-tier: local ./.bitsuse or ~/.bits/use/<key>) ──────

def _owned_by_user(path):
    """True when *path* is owned by the invoking user (or ownership can't be
    determined, e.g. a platform without getuid — then don't gate)."""
    try:
        return os.stat(path).st_uid == os.getuid()
    except AttributeError:      # no os.getuid (non-POSIX) → no ownership gate
        return True
    except OSError:
        return False


def _home_paths(directory=None):
    """Return ``(home_profile_path, real_directory)`` for *directory* (default cwd)."""
    d = os.path.realpath(directory or os.getcwd())
    key = hashlib.sha256(d.encode("utf-8")).hexdigest()[:16]
    return os.path.join(HOME_STORE, key + ".use"), d


def _local_read_path():
    """The trusted local profile to read (``.bitsuse``, then legacy ``.bitscmd``),
    or None when none exists or the one that exists is not owned by the user."""
    for name in (PROFILE, LEGACY_PROFILE):
        if os.path.exists(name):
            if _owned_by_user(name):
                return name
            sys.stderr.write("bits use: ignoring %s (not owned by you)\n" % name)
    return None


def _read_path():
    """Where to READ the active profile: a trusted local file, else the
    ~/.bits/use record for this directory, else None."""
    local = _local_read_path()
    if local:
        return local
    home, _ = _home_paths()
    return home if os.path.exists(home) else None


def _write_path():
    """Where to WRITE. Prefer an existing owned+writeable local ``.bitsuse``
    (updating a file needs only file write permission, so this keeps working even
    in a dir that is not itself writeable, matching where reads look); else create
    ``./.bitsuse`` when the cwd is writeable and owned; else a ~/.bits/use record
    for this directory."""
    if os.path.exists(PROFILE) and _owned_by_user(PROFILE) and os.access(PROFILE, os.W_OK):
        return PROFILE
    cwd = os.getcwd()
    if os.access(cwd, os.W_OK) and _owned_by_user(cwd):
        return PROFILE
    os.makedirs(HOME_STORE, exist_ok=True)
    home, _ = _home_paths()
    return home


def _is_home_path(path):
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(HOME_STORE)


def _src_label(path):
    """Human label for a profile path: bare name for a local file, full path
    (with the directory it applies to) for a home record."""
    if _is_home_path(path):
        _, d = _home_paths()
        return "%s (for %s)" % (path, d)
    return path


# ── profile read/write ────────────────────────────────────────────────────────

def _join(tokens):
    try:
        return shlex.join(tokens)          # Python 3.8+
    except AttributeError:                 # pragma: no cover
        return " ".join(shlex.quote(t) for t in tokens)


def read_all(path=PROFILE):
    """Parse the profile at *path* into an ordered ``{section: [tokens]}`` dict.

    ``[name]`` opens a section; lines before any header belong to ``common``;
    ``#`` comments (including the ``# dir:`` header on home records) and blank
    lines are ignored. Each section's lines are joined and shlex-split.
    """
    sections, cur, buf = {}, COMMON, []
    if not path or not os.path.exists(path):
        return sections

    def _flush():
        if buf:
            sections.setdefault(cur, [])
            sections[cur] += shlex.split(" ".join(buf))
    try:
        with open(path) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("[") and line.endswith("]"):
                    _flush(); buf = []
                    cur = line[1:-1].strip().lower()
                    cur = COMMON if cur in ("global", COMMON) else cur
                    sections.setdefault(cur, [])
                    continue
                buf.append(line)
        _flush()
    except (OSError, ValueError):
        # ValueError: a malformed token (unbalanced quote). Fail safe — ignore
        # the profile rather than crash a build.
        return {}
    return sections


def _write_all(sections, path=PROFILE):
    order = [COMMON] + [s for s in sections if s != COMMON]
    with open(path, "w") as fh:
        if _is_home_path(path):
            # Record which directory this home profile belongs to (the parser
            # ignores '#' lines); lets --show name it and aids housekeeping.
            _, d = _home_paths()
            fh.write("# dir: %s\n" % d)
        for s in order:
            toks = sections.get(s)
            if not toks:
                continue
            fh.write("[%s]\n%s\n\n" % (s, _join(toks)))


def write_section(section, tokens, path=None):
    """Replace *section* with *tokens*, preserving the other sections. Writes to
    the resolved write path (local ``.bitsuse`` or a ~/.bits/use record)."""
    if path is None:
        target = _write_path()
        # Seed from the currently ACTIVE profile (which may be a legacy .bitscmd
        # or a home record) so existing sections carry over to the new file
        # instead of being silently dropped on first write under the new name.
        sections = read_all(_read_path())
    else:
        target = path
        sections = read_all(path)
    section = COMMON if section in ("global", COMMON) else section.lower()
    sections[section] = list(tokens)
    _write_all(sections, target)
    return target


def clear_section(section=None, path=None):
    """Clear one section, or the whole profile when *section* is None, operating
    on the currently active profile."""
    if path is None:
        path = _read_path()
    if not path:
        return False
    if section is None:
        try:
            os.unlink(path); return True
        except OSError:
            return False
    sections = read_all(path)
    section = COMMON if section in ("global", COMMON) else section.lower()
    if section in sections:
        del sections[section]
        _write_all(sections, path)
        return True
    return False


def merged_argv(command, user_args, path=None):
    """Args to run for *command*: ``[common]`` then ``[command]`` then the
    user's own args (which come last and win on single-value options)."""
    if path is None:
        path = _read_path()
    sec = read_all(path)
    return sec.get(COMMON, []) + sec.get((command or "").lower(), []) + list(user_args)


# Top-level flags that may precede the action (from the root argparse parser);
# skipped when locating the action token.
TOP_FLAGS = {"-d", "--debug", "-n", "--dry-run"}

# The profile is injected ONLY into arch-aware commands (they all accept
# `--architecture`, the intended `[common]` content). Meta commands (use, cvmfs,
# store, version, help, cvmfs-stage/publish) and `verify` (accepts neither
# --architecture nor --defaults) take a different option set and are excluded.
INJECT_ACTIONS = {
    "build", "deps", "doctor", "status", "clean", "cleanup", "gc",
    "import", "publish", "certify", "compliance",
    "q", "query", "enter", "setenv", "printenv", "load", "unload",
}


def _find_action(argv):
    """Index of the action token in *argv* (first non-top-flag word), or None."""
    for i, tok in enumerate(argv):
        if tok in TOP_FLAGS:
            continue
        if tok.startswith("-"):
            return None          # an option before any action → leave as-is
        return i
    return None


def rewrite_argv(argv, path=None):
    """Return *argv* with the active profile injected right after the action
    token: ``[common]`` plus ``[<action>]``, for arch-aware actions only
    (``INJECT_ACTIONS``). A no-op when there is no profile, no action, or the
    action is a meta command. This is the single entry point the wrapper calls.
    """
    argv = list(argv)
    if path is None:
        path = _read_path()
    sec = read_all(path)
    if not sec:
        return argv
    ai = _find_action(argv)
    if ai is None:
        return argv
    action = argv[ai].lower()
    if action not in INJECT_ACTIONS:
        return argv
    inject = sec.get(COMMON, []) + sec.get(action, [])
    if not inject:
        return argv
    return argv[:ai + 1] + inject + argv[ai + 1:]


# ── CLI ──────────────────────────────────────────────────────────────────────

def _show():
    path = _read_path()
    sec = read_all(path)
    if not sec:
        print("no bits use profile for %s" % os.getcwd()); return 0
    print("# %s" % _src_label(path))
    for s, toks in sec.items():
        if toks:
            print("[%s] %s" % (s, _join(toks)))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Wrapper hook: emit the profile-injected argv, NUL-separated, for the args
    # after '--'. `mapfile -t -d '' arr < <(bits_use --rewrite0 -- "$@")`.
    if argv and argv[0] == "--rewrite0":
        rest = argv[2:] if len(argv) > 1 and argv[1] == "--" else argv[1:]
        out = rewrite_argv(rest)
        if out != rest:                        # injection happened → tell the user
            n = len(out) - len(rest)
            path = _read_path() or PROFILE
            label = path if _is_home_path(path) else os.path.basename(path)
            sys.stderr.write("using %s (+%d arg%s)\n" % (label, n, "" if n == 1 else "s"))
        # NUL-terminate EVERY token (trailing NUL included) so the wrapper's
        # `while read -d ''` loop captures the final token.
        sys.stdout.write("".join(t + "\0" for t in out))
        return 0
    if not argv:
        return _show()
    if argv[0] in ("--clear", "clear"):
        sect = argv[1] if len(argv) > 1 else None
        ok = clear_section(sect)
        print(("cleared %s" % (sect or "all")) if ok else "nothing to clear")
        return 0
    # Optional leading section name (a bare word); otherwise default to common.
    if argv[0] and not argv[0].startswith("-"):
        section, rest = argv[0], argv[1:]
    else:
        section, rest = COMMON, argv
    if not rest:
        print("no args given for [%s]" % section); return 1
    path = write_section(section, rest)
    print("saved to [%s] in %s: %s" % (
        COMMON if section in ("global", COMMON) else section.lower(),
        _src_label(path), _join(rest)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
