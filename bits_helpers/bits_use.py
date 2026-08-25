# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later

"""`bits use` — save reusable command-line args in ``./.bitscmd`` so repeated
commands stay short. Distinct from ``.bitsrc`` (typed key=value settings):
``.bitscmd`` holds raw CLI tokens, structured by the command they apply to.

A ``[common]`` section is injected into *every* command (e.g. ``--architecture``,
which ``build`` AND ``q``/``enter`` all need); a per-command section
(``[build]``, ``[q]``, …) adds command-specific args. Injected BEFORE the user's
own args, so those override single-value options. Example ``.bitscmd``::

    [common]
    --architecture x86_64-el9-gcc14-opt --defaults lcg::release::gcc14::opt

    [build]
    --docker --docker-image reg/alma9 --sandbox off --reuse-from cvmfs::relaxed

Prototype — runs standalone::

    python3 -m bits_helpers.bits_use common --architecture x86_64-el9-gcc14-opt
    python3 -m bits_helpers.bits_use build  --docker --sandbox off
    python3 -m bits_helpers.bits_use            # show all sections
    python3 -m bits_helpers.bits_use --clear [SECTION]
"""

import os
import shlex
import sys

PROFILE = ".bitscmd"
COMMON = "common"          # section injected into every command ('global' alias)


def _join(tokens):
    try:
        return shlex.join(tokens)          # Python 3.8+
    except AttributeError:                 # pragma: no cover
        return " ".join(shlex.quote(t) for t in tokens)


def read_all(path=PROFILE):
    """Parse the profile into an ordered ``{section: [tokens]}`` dict.

    ``[name]`` opens a section; lines before any header belong to ``common``;
    ``#`` comments and blank lines are ignored. Each section's lines are joined
    and shlex-split into tokens.
    """
    sections, cur, buf = {}, COMMON, []
    if not os.path.exists(path):
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
        # ValueError: a malformed token (e.g. an unbalanced quote) in the
        # profile. Fail safe — ignore the profile rather than crash a build.
        return {}
    return sections


def write_section(section, tokens, path=PROFILE):
    """Replace *section* with *tokens*, preserving the other sections."""
    section = COMMON if section in ("global", COMMON) else section.lower()
    sections = read_all(path)
    sections[section] = list(tokens)
    _write_all(sections, path)
    return path


def clear_section(section=None, path=PROFILE):
    """Clear one section, or the whole profile when *section* is None."""
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


def _write_all(sections, path=PROFILE):
    order = [COMMON] + [s for s in sections if s != COMMON]
    with open(path, "w") as fh:
        for s in order:
            toks = sections.get(s)
            if not toks:
                continue
            fh.write("[%s]\n%s\n\n" % (s, _join(toks)))


def merged_argv(command, user_args, path=PROFILE):
    """Args to run for *command*: ``[common]`` then ``[command]`` then the
    user's own args (which come last and win on single-value options)."""
    sec = read_all(path)
    return sec.get(COMMON, []) + sec.get((command or "").lower(), []) + list(user_args)


# Top-level flags that may precede the action (from the root argparse parser);
# skipped when locating the action token. `use` never injects into itself.
TOP_FLAGS = {"-d", "--debug", "-n", "--dry-run"}
NO_INJECT = {"use"}


def _find_action(argv):
    """Index of the action token in *argv* (first non-top-flag word), or None."""
    for i, tok in enumerate(argv):
        if tok in TOP_FLAGS:
            continue
        if tok.startswith("-"):
            return None          # an option before any action → leave as-is
        return i
    return None


def rewrite_argv(argv, path=PROFILE):
    """Return *argv* with the ``.bitscmd`` profile injected right after the action
    token: ``[common]`` for every command, plus ``[<action>]`` for that action.
    A no-op when there is no profile, no action, or the action opts out
    (``use``). This is the single entry point the wrapper calls at startup.
    """
    argv = list(argv)
    sec = read_all(path)
    if not sec:
        return argv
    ai = _find_action(argv)
    if ai is None:
        return argv
    action = argv[ai].lower()
    if action in NO_INJECT:
        return argv
    inject = sec.get(COMMON, []) + sec.get(action, [])
    if not inject:
        return argv
    return argv[:ai + 1] + inject + argv[ai + 1:]


# ── CLI ──────────────────────────────────────────────────────────────────────

def _show(path=PROFILE):
    sec = read_all(path)
    if not sec:
        print("no .bitscmd in %s" % os.getcwd()); return 0
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
            sys.stderr.write("using .bitscmd (+%d arg%s)\n" % (n, "" if n == 1 else "s"))
        # NUL-terminate EVERY token (trailing NUL included) so the wrapper's
        # `while read -d ''` loop captures the final token — an unterminated last
        # field is assigned but the loop exits before appending it (dropping it).
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
    write_section(section, rest)
    print("saved to [%s] in %s: %s" % (
        COMMON if section in ("global", COMMON) else section.lower(),
        PROFILE, _join(rest)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
