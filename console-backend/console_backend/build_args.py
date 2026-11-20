# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The `bits build` options a console build may carry (BITS_BUILD_ARGS): what the
console, the dispatcher and the nightlies send, and choices about what is built.
Nothing that reaches outside the build's container or changes the store, its trust
or the work dir. The same file is bits-console's .gitlab/check-build-args.py and
bits' console_backend/build_args.py: change both."""

import re
import sys

_WORD = re.compile(r"[A-Za-z0-9._+-]{1,200}")
_LIST = re.compile(r"[A-Za-z0-9._+-]{1,200}(,[A-Za-z0-9._+-]{1,200}){0,50}")
_SEGMENT = r"([A-Za-z0-9][A-Za-z0-9._+-]{0,299})?"       # a CVMFS path segment ({day}, {release}, ...)
# A defaults variable, exported to the build and its init.sh: none a program reads
# in lower case (curl's, pip's, git's proxies; zsh's tied path variables).
_SET = re.compile(r"(?!(path|cdpath|fpath|manpath|module_path|mailpath)=|[a-z0-9_]*_proxy=)"
                  r"[a-z][a-z0-9_]{0,99}=" + _SEGMENT)
_ANNOTATE = re.compile(r"[A-Za-z0-9._+-]{1,200}=[A-Za-z0-9._+:@%^-]{0,300}")
_DEFAULTS = re.compile(r"[A-Za-z0-9._+-]{1,100}(::[A-Za-z0-9._+-]{1,100}){0,10}")
_IMAGE = re.compile(r"[A-Za-z0-9._:/@+-]{1,300}")
_INT = re.compile(r"[0-9]|[1-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-6]")    # 0 to 256
_NUMBER = re.compile(r"[0-9]{1,4}(\.[0-9]{1,3})?")
_DAY = re.compile(_SEGMENT)


def _one_of(*words):
    return re.compile("|".join(map(re.escape, words)))


# Each option, as bits names it in full (argparse would take any abbreviation of
# any option: none is taken), with the shape of its value; FLAG: none; OPTIONAL:
# a value only when the next word is not an option.
FLAG, OPTIONAL = None, "optional"
OPTIONS = {
    "--force-rebuild": _LIST, "-a": _WORD, "--architecture": _WORD,
    "--docker": FLAG, "--docker-image": _IMAGE,
    "--docker-extra-args": re.compile(r"--cpus=[0-9]{1,4}(\.[0-9]{1,2})?"),
    "-j": _INT, "--jobs": _INT, "--builders": OPTIONAL, "--parallel": OPTIONAL,
    "--sandbox": _one_of("off", "auto", "podman", "sandbox-exec"),
    "-d": FLAG, "--debug": FLAG,
    "--defaults": _DEFAULTS, "--day": _DAY,
    "--set": _SET, "--flavour": _SET, "--flavor": _SET,
    "--disable": _LIST, "--only-deps": FLAG, "--run-tests": FLAG, "--annotate": _ANNOTATE,
    "--no-system": OPTIONAL, "--prefer-system": FLAG, "--always-prefer-system": FLAG,
    "--pip-build": _one_of("wheel", "source", "all"), "--no-auto-patch": FLAG,
    "--check-checksums": FLAG, "--enforce-checksums": FLAG,
    "--oversubscribe": re.compile(r"[0-9](\.[0-9]{1,2})?"), "--unleash-final": FLAG, "--no-unleash-final": FLAG,
    "--critical-path-schedule": FLAG, "--no-critical-path-schedule": FLAG,
    "--build-nice": FLAG, "--no-build-nice": FLAG, "--build-nice-step": _NUMBER,
    "--build-nice-boost-after": _NUMBER, "--auto-resources": FLAG,
    "--resource-monitoring": FLAG, "--no-resource-monitoring": FLAG,
    "-u": FLAG, "--fetch-repos": FLAG, "--no-fetch-repos": FLAG, "--fetch-build-requires": FLAG,
    "--prefetch-workers": _INT, "--parallel-downloads": _INT, "--parallel-sources": _INT,
    "--aggressive-cleanup": FLAG, "--no-auto-cleanup": FLAG,
}
_OPTIONAL_VALUE = {"--builders": _INT, "--parallel": _INT, "--no-system": _LIST}


def problem(words, image=None):
    """Why the `bits build` arguments *words* may not be a console build's, or None.
    With *image* (the build's platform's, "" for none): in its container, --docker
    --docker-image *image*, or, without one, on the host."""
    i, named = 0, set()
    while i < len(words):
        w = words[i]
        name, eq, value = w.partition("=") if w.startswith("--") else (w, "", "")
        if name not in OPTIONS:
            return ("%r is not an option a console build may give" % w[:100]) if w.startswith("-") \
                else ("%r is not the value of an option" % w[:100])
        named.add(name)
        shape = OPTIONS[name]
        if shape is FLAG:
            if eq:
                return "%s takes no value" % name
        elif shape == OPTIONAL:
            if not eq and i + 1 < len(words) and not words[i + 1].startswith("-"):
                i, value, eq = i + 1, words[i + 1], "="
            if eq and not _OPTIONAL_VALUE[name].fullmatch(value):
                return "%s: %r is not a value it takes" % (name, value[:100])
        else:
            if not eq:
                if i + 1 >= len(words) or words[i + 1].startswith("-"):
                    return "%s needs a value" % name
                i, value = i + 1, words[i + 1]
            if not shape.fullmatch(value):
                return "%s: %r is not a value it takes" % (name, value[:100])
            if name == "--docker-image" and image is not None and value != image:
                return "--docker-image: only the platform's image (%s)" % (image or "none")
        i += 1
    if image and not {"--docker", "--docker-image"} <= named:
        return "the build runs in its platform's image: --docker --docker-image %s" % image
    if image == "" and "--docker" in named:
        return "--docker: the platform has no image"
    return None


if __name__ == "__main__":
    # check-build-args.py [--image IMAGE] -- WORD...: the reason on stderr, status 1
    argv, image = sys.argv[1:], None
    if argv[:1] == ["--image"]:
        image, argv = argv[1], argv[2:]
    why = problem(argv[1:] if argv[:1] == ["--"] else argv, image)
    if why:
        print("BITS_BUILD_ARGS: %s" % why, file=sys.stderr)
    sys.exit(1 if why else 0)
