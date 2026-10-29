# SPDX-FileCopyrightText: 2015-2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""``bits overlay <format> …`` — pluggable overlay/export formats.

Turn a built bits closure into a consumable representation in a named FORMAT — an
LCG release view + manifest today (``bits overlay lcg``); Spack/Conda/modules
later. ``overlay`` is the generic verb (nothing CERN-specific in the core CLI);
``<format>`` selects a plugin.

Built-in formats are modules in this package (``bits_helpers/overlay/<format>.py``).
Extra formats drop in via ``$BITS_OVERLAY_PLUGINS`` (an os.pathsep-separated list
of ``.py`` files or directories that contain them), so ``bits overlay <another>``
works without modifying bits core. A plugin is any module exposing
``main(argv) -> int`` (and, optionally, a one-line ``DESCRIPTION``).
"""
import importlib
import importlib.util
import os
import pkgutil
import sys


def _builtin_formats():
    return {name for _, name, _ in pkgutil.iter_modules(__path__)
            if not name.startswith("_")}


def _external_paths():
    return [p for p in os.environ.get("BITS_OVERLAY_PLUGINS", "").split(os.pathsep) if p]


def _load_format(fmt):
    if fmt in _builtin_formats():
        return importlib.import_module("%s.%s" % (__name__, fmt))
    for base in _external_paths():
        cand = (base if base.endswith(".py") and os.path.basename(base)[:-3] == fmt
                else os.path.join(base, "%s.py" % fmt))
        if os.path.isfile(cand):
            spec = importlib.util.spec_from_file_location("bits_overlay_%s" % fmt, cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    return None


def _usage(out=sys.stderr):
    out.write("usage: bits overlay <format> <args>\n")
    for fmt in sorted(_builtin_formats()):
        try:
            desc = getattr(_load_format(fmt), "DESCRIPTION", "")
        except Exception:
            desc = ""
        out.write("  %-10s %s\n" % (fmt, desc))
    if _external_paths():
        out.write("  (plus formats on $BITS_OVERLAY_PLUGINS)\n")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        _usage(sys.stdout if argv else sys.stderr)   # asked for: stdout
        return 0 if argv else 2
    fmt, rest = argv[0], argv[1:]
    mod = _load_format(fmt)
    if mod is None or not hasattr(mod, "main"):
        sys.stderr.write("bits overlay: unknown format %r\n" % fmt)
        _usage()
        return 2
    return mod.main(rest)


if __name__ == "__main__":
    sys.exit(main())
