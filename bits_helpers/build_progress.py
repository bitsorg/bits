# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Human-readable package and stage progress, independent of CI reporting.

Packages are numbered along the selected plan (reused ones included,
defaults-release not), after selections such as --only-deps; parallel builds
report out of that order. A serial build on a terminal shows a spinner with the
compiler's percentages; parallel builds print labeled lines, no spinners.

The build script delimits its phases with `bits:stage:<name>` lines (STAGES).
Within a recipe, stages come from Bash's trace of the commands it runs
(_command_stage), never from text that mentions make or install, nor from the
traced echo of a marker. A custom tool can print bits:stage:configure, compile or
install itself. Post-install hooks, packaging and relocation share "Installing".
"""

import logging
import os.path
import re
import shlex
import sys

from bits_helpers.log import ProgressPrint, debug, info, logger


# Explicit markers delimit recipe execution. Within a recipe, Bash's existing
# command trace identifies common build tools without changing recipe execution.
STAGE_PREFIX = "bits:stage:"
STAGES = {
    "prepare": "Preparing build environment",
    "recipe": "Running build recipe",
    "incremental": "Running incremental build recipe",
    "configure": "Configuring",
    "compile": "Compiling",
    "install": "Installing",
    "unpack": "Unpacking cached archive",
    "post-install": "Installing",
    "relocate-config": "Installing",
    "package": "Installing",
    "relocate": "Installing",
}


def _command_stage(line):
    """Identify an executed build command, never text echoed by a recipe."""
    if not re.match(r"^\++ ", line):
        return None
    try:
        words = shlex.split(line.lstrip("+"))
    except ValueError:
        return None  # A multiline traced argument is not a complete command.
    while words and (re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", words[0])
                     or words[0] in ("command", "exec", "env", "ccache", "sccache")):
        words.pop(0)
    if not words:
        return None
    command, args = os.path.basename(words[0]), words[1:]
    if command in ("configure", "Configure"):
        return "configure"
    if command == "Make":
        return "compile"
    if command in ("MakeInstall", "install"):
        return "install"
    if command == "cmake":
        if "--build" in args:
            if "--target" in args and "install" in args[args.index("--target") + 1:]:
                return "install"
            return "compile"
        if "--install" in args:
            return "install"
        if any(a in ("-E", "-P", "--version", "--help", "--find-package") for a in args):
            return None
        return "configure"
    if command == "meson" and args:
        return {"setup": "configure", "configure": "configure",
                "compile": "compile", "install": "install"}.get(args[0])
    if command in ("make", "gmake", "ninja", "ninja-build"):
        if any(a in ("--version", "--help", "-t", "-n", "--just-print", "--dry-run") for a in args):
            return None
        targets = []
        skip = False
        for index, arg in enumerate(args):
            if skip:
                skip = False
            elif arg in ("-C", "-f", "--directory", "--file", "--makefile"):
                skip = True
            elif arg in ("-j", "-l"):
                skip = index + 1 < len(args) and bool(re.fullmatch(r"\d+(?:\.\d+)?", args[index + 1]))
            elif not arg.startswith("-") and "=" not in arg:
                targets.append(arg)
        if any(t == "install" or t.startswith(("install-", "install/", "install_")) for t in targets):
            return "install"
        if targets and all(t in ("clean", "distclean", "maintainer-clean", "help") for t in targets):
            return None
        return "compile"
    if command in ("sh", "bash", "perl") and args and os.path.basename(args[0]) in ("configure", "Configure"):
        return "configure"
    if re.fullmatch(r"python(?:[23](?:\.\d+)*)?", command):
        if args[:2] == ["-m", "pip"]:
            args = args[2:]
            command = "pip"
        elif args[:2] == ["-m", "build"]:
            return "compile"
        elif args and os.path.basename(args[0]) == "setup.py":
            if "install" in args:
                return "install"
            if any(a in ("build", "build_ext", "bdist_wheel") for a in args):
                return "compile"
    if re.fullmatch(r"pip(?:[23](?:\.\d+)*)?", command) and args:
        return {"install": "install", "wheel": "compile"}.get(args[0])
    return None


def set_package_progress(specs, packages):
    """Number the selected plan, including reused packages, excluding defaults."""
    planned = [p for p in packages if p != "defaults-release"]
    for index, package in enumerate(planned, 1):
        specs[package]["_build_progress"] = (index, len(planned))


def package_label(spec, version=None):
    label = "{}@{}".format(spec["package"], spec["version"] if version is None else version)
    if "_build_progress" in spec:
        label = "[{}/{}] {}".format(*spec["_build_progress"], label)
    return label


class BuildProgress:
    """Show stage transitions in logs and retain compiler percentages on a TTY."""

    def __init__(self, label, stage, emit=None, interactive=True):
        self.label = label
        self.emit = emit or info
        self.interactive = interactive
        self.display = None
        self.stage = None
        self.in_recipe = False
        self.update(stage)

    def update(self, stage, interactive=True):
        if stage == self.stage:
            return
        if self.display is not None:
            self.display.end("done")
            self.display = None
        self.stage = stage
        message = "{}: {}".format(self.label, stage)
        if interactive and self.interactive and sys.stdout.isatty() and not logger.isEnabledFor(logging.DEBUG):
            self.display = ProgressPrint(message)
            self.display("%s", "")
        else:
            self.emit(message)

    def __call__(self, text, *args):
        line = text % args if args else text
        if line.startswith(STAGE_PREFIX) and line[len(STAGE_PREFIX):] in STAGES:
            marker = line[len(STAGE_PREFIX):]
            self.in_recipe = marker in ("recipe", "incremental", "configure", "compile", "install")
            self.update(STAGES[marker])
            return
        stage = _command_stage(line) if self.in_recipe else None
        if stage:
            self.update(STAGES[stage])
        if self.display is not None:
            self.display("%s", line)
        else:
            debug("%s", line)

    def end(self, error=False):
        if self.display is not None:
            self.display.end("failed" if error else "done", error)
            self.display = None
        else:
            self.emit("{}: {}".format(self.label, "failed" if error else "done"))
