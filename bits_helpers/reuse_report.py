# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""What a build reused and what it built, and why: the end-of-build summary.

From the build manifest's package entries (``outcome`` and, for a package
built here, ``rebuilt_because``). A package already installed is reused
silently during the build; this is where it shows.
"""

from collections import Counter

OUTCOMES = (("already_installed", "already installed"),
            ("from_store", "from the store"),
            ("built_from_source", "built"))


def summarize(packages):
    """``{outcome: [entry, ...]}`` in manifest order, config packages left out."""
    out = {key: [] for key, _label in OUTCOMES}
    for e in packages or ():
        if not isinstance(e, dict) or str(e.get("package", "")).startswith("defaults"):
            continue
        out.setdefault(e.get("outcome") or "?", []).append(e)
    return out


def _names(entries, limit, reasons=False):
    shown = []
    for e in entries[:limit]:
        name = str(e.get("package", "?"))
        if reasons and e.get("rebuilt_because"):
            name += " (%s)" % e["rebuilt_because"]
        shown.append(name)
    more = len(entries) - len(shown)
    return ", ".join(shown) + (", … %d more" % more if more > 0 else "")


def format_summary(packages, limit=8):
    """The summary text, or "" for a manifest with no packages."""
    groups = summarize(packages)
    total = sum(len(v) for v in groups.values())
    if not total:
        return ""
    counts = ", ".join("%d %s" % (len(groups.get(k, ())), label) for k, label in OUTCOMES)
    lines = ["Reuse: %d package(s) — %s" % (total, counts)]
    if groups.get("from_store"):
        lines.append("  from the store: " + _names(groups["from_store"], limit))
    built = groups.get("built_from_source") or []
    if built:
        lines.append("  built: " + _names(built, limit, reasons=True))
        why = Counter(e.get("rebuilt_because") or "reason not recorded" for e in built)
        if len(why) > 1 or len(built) > limit:
            lines.append("  built, by reason: " + "; ".join(
                "%d %s" % (n, r) for r, n in why.most_common()))
    return "\n".join(lines)


def metric_lines(packages, instance, architecture, package):
    """Prometheus lines: the build's package count per outcome."""
    def q(v):
        return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")
    groups = summarize(packages)
    base = 'instance="%s",job="bits",arch="%s",package="%s"' % (
        q(instance), q(architecture), q(package))
    out = ['bits_build_packages{%s,outcome="%s"} %d' % (base, k, len(groups.get(k, ())))
           for k, _label in OUTCOMES]
    for reason, n in Counter(e.get("rebuilt_because") or "unrecorded"
                             for e in groups.get("built_from_source") or ()).items():
        out.append('bits_build_rebuilt{%s,reason="%s"} %d' % (base, q(reason), n))
    return out
