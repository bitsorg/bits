# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Licences and dependencies of the published builds, from their SBOMs (the
index sbom_scan keeps: each package's licence, redistribution, runtime and build
links, and the packages the release asks for, its top).

Licences, per release line (its newest indexed build, as sbom_plan): every
package's licence (an SPDX expression) in coarse classes, and the findings:

* conflict: a package whose runtime closure holds licences it cannot be
  combined with (GPL-2.0-only with GPL-3.0, AGPL or Apache-2.0; GPL-3.0/AGPL
  with GPL-2.0-only), each with its dependency path;
* unknown: a package with no licence, NOASSERTION, a LicenseRef or a name that
  is no SPDX id (unreviewed).

Build-only links form no combined work, so they are not followed, nor are the
standalone build tools a recipe may list among its runtime requires; packages
taken from the host system are not distributed by us. Copyleft reach lists, per
top package, the GPL and AGPL code in its runtime closure (its own apart), and
restricted the packages whose binaries may not be redistributed (left out of
publishing): both inform, they are not findings. The admins accept a finding with a reason, as a
vulnerability triage rule (vuln_triage) on the id licence:<kind>.

Dependencies, per build: the packages most others depend on (how many rebuild
when one changes), those needed only to build, those nothing needs, and what
changed since the line's previous build; across lines, the packages built in
several versions.
"""

import functools
import json
import re

from . import vuln_triage

KINDS = ("conflict", "unknown")
COPYLEFT = ("gpl2only", "gpl2plus", "gpl3", "agpl")
CAN_CONFLICT = ("gpl2only", "gpl3", "agpl")      # the classes conflict() knows a rule for
STRICTNESS = {"permissive": 0, "weak": 1, "lgpl": 2, "gpl2plus": 3, "gpl2only": 4, "gpl3": 4,
              "agpl": 5, "custom": 6, "unknown": 7}
# Exceptions that exist to permit combining: a licence carrying one grants it.
_EXCEPTION_OK = re.compile(r"^(GCC-exception|GCC-Runtime|Classpath-exception|LLVM-exception|"
                           r"Universal-FOSS-exception|Autoconf-exception|Bison-exception|"
                           r"Font-exception)", re.I)
# File-level (weak) copyleft: shown apart, no conflict rule.
_WEAK = re.compile(r"^(MPL|EPL|CDDL|EUPL|OSL|CPL|MS-RL|CECILL)-", re.I)
MAX_WITH = 20                       # dependencies listed per conflict finding
MAX_ALTERNATIVES = 64               # ways to satisfy one licence expression, at most
# Standalone build tools, at times in a recipe's requires: no combined work.
BUILD_TOOLS = {n.lower() for n in (
    "CMake", "ninja", "gmake", "make", "m4", "autotools", "automake", "autoconf", "libtool",
    "pkg_config", "pkgconfig", "gettext", "gperf", "bison", "flex", "swig", "ccache",
    "patchelf", "GCC-Toolchain", "Clang", "bits-recipe-tools", "yacc-like", "doxygen")}
TOP_N = 50
LICENCE_INDEX = 3                   # sbom_scan's index this analysis needs (licences, links, top)


def finding_id(kind):
    """The vuln_triage id a finding of *kind* is accepted under."""
    return "licence:%s" % kind


@functools.lru_cache(maxsize=1)
def _spdx_ids():
    from bits_helpers.spdx_ids import LICENSES
    return frozenset(x.upper() for x in LICENSES)


def _id_class(lid):
    """The class of one licence id: a GPL family member by its SPDX id (the
    deprecated GPL-2.0 is -only, GPL-2.0+ -or-later), a LicenseRef or a name
    that is no SPDX id (ids match whatever their case) is unreviewed, any
    other SPDX id permissive or weak."""
    u = lid.upper()
    if u in ("NOASSERTION", "NONE"):
        return "unknown"
    m = re.match(r"^(A|L)?GPL-(\d)\.\d(-ONLY|-OR-LATER|\+)?$", u)
    if m:
        kind, major, scope = m.group(1) or "", int(m.group(2)), m.group(3) or ""
        if kind == "A":
            return "agpl"
        if kind == "L":
            return "lgpl"
        if major >= 3:
            return "gpl3"
        return "gpl2only" if major == 2 and scope in ("", "-ONLY") else "gpl2plus"
    if u not in _spdx_ids() and u.rstrip("+") not in _spdx_ids():
        return "custom"
    return "weak" if _WEAK.match(lid) else "permissive"


def _parse(lic):
    """The alternatives of an SPDX expression: [(class, ids)], one per way to
    satisfy it (an OR adds alternatives, an AND combines them: the strictest
    class, the ids that apply). A licence WITH an exception that permits
    combining is permissive, with no ids. Text that is not an expression is
    one unreviewed alternative."""
    toks = re.findall(r"\(|\)|[^\s()]+", lic)
    pos = [0]

    def peek():
        return toks[pos[0]].upper() if pos[0] < len(toks) else None

    def take():
        pos[0] += 1
        return toks[pos[0] - 1]

    def atom():
        if peek() == "(":
            take()
            out = either()
            if peek() != ")":
                raise ValueError("no )")
            take()
            return out
        if peek() in (None, ")", "AND", "OR", "WITH"):
            raise ValueError("a licence id expected")
        lid = take()
        if peek() == "WITH":
            take()
            if peek() in (None, "(", ")", "AND", "OR", "WITH"):
                raise ValueError("an exception id expected")
            if _EXCEPTION_OK.match(take()):
                return [("permissive", frozenset())]
        return [(_id_class(lid), frozenset([lid.upper()]))]

    def both():
        alts = atom()
        while peek() == "AND":
            take()
            right = atom()
            alts = [(max(a, b, key=STRICTNESS.get), x | y) for a, x in alts for b, y in right]
            alts = alts[:MAX_ALTERNATIVES]
        return alts

    def either():
        alts = both()
        while peek() == "OR":
            take()
            alts = (alts + both())[:MAX_ALTERNATIVES]
        return alts

    try:
        out = either()
        if pos[0] != len(toks):
            raise ValueError("text after the expression")
        return out
    except (ValueError, IndexError):
        return [("custom", frozenset())]


@functools.lru_cache(maxsize=4096)
def licence(lic):
    """The alternatives of a licence as an SBOM gives it (see _parse)."""
    lic = str(lic or "").strip()
    return tuple(_parse(lic)) if lic else (("unknown", frozenset()),)


def licence_class(lic):
    """The coarse class of a licence: its most permissive alternative's."""
    return min((c for c, _ in licence(lic)), key=STRICTNESS.get)


def _pair(a, b, ids, own, dep):
    if a == "gpl2only" and b in ("gpl3", "agpl"):
        return "GPL-2.0-only cannot be combined with %s" % dep
    if a in ("gpl3", "agpl") and b == "gpl2only":
        return "%s cannot be combined with GPL-2.0-only" % own
    if a == "gpl2only" and "APACHE-2.0" in ids:
        return "GPL-2.0-only cannot be combined with Apache-2.0"
    return None


def conflict(own, dep):
    """Why a package licensed *own* cannot use one licensed *dep*, or None: only
    when no alternative of either licence can be combined with one of the other."""
    reasons = [_pair(a, b, ids, own, dep) for a, _ in licence(own) for b, ids in licence(dep)]
    return reasons[0] if reasons and all(reasons) else None


def restricted(value):
    """Do the binaries of a package marked *value* stay unpublished?"""
    return str(value or "").strip().lower() in ("none", "sources", "false")


# ── a build's graph ──────────────────────────────────────────────────────────

def _loads(text):
    try:
        out = json.loads(text or "[]")
    except ValueError:
        return []
    return [x for x in out if isinstance(x, str)] if isinstance(out, list) else []


def graph(conn, build_id):
    """{ref: package} of a build: its row, with `runtime` and `links` (all) lists."""
    out = {}
    for r in conn.execute("SELECT * FROM sbom_packages WHERE build_id = ?", (build_id,)):
        p = dict(r)
        p["runtime"], p["links"] = _loads(p["runtime_on"]), _loads(p["depends_on"])
        p["label"] = p["alias"] or p["name"]
        out[p["ref"]] = p
    return out


def _follows(p):
    return p is not None and not p["system"] and p["label"].lower() not in BUILD_TOOLS


def _walk(packages, starts, key="runtime", follow=_follows):
    """{ref: parent ref (None at a start)} of what *starts* reach over *key*
    links, breadth first, so each parent chain is a shortest path."""
    seen, todo = {s: None for s in starts if s in packages}, [s for s in starts if s in packages]
    while todo:
        nxt = []
        for ref in todo:
            if not follow(packages[ref]) and seen[ref] is not None:
                continue
            for d in packages[ref][key]:
                if d in packages and d not in seen:
                    seen[d] = ref
                    nxt.append(d)
        todo = nxt
    return seen


def _path(packages, parents, ref):
    out = []
    while ref is not None:
        out.append(packages[ref]["label"])
        ref = parents[ref]
    return out[::-1]


def licences(packages, community, rules):
    """The licence analysis of a build's *packages*: class counts, findings (each
    with the triage rule accepting it, or None), copyleft reach, restricted."""
    classes, findings, reach = {}, [], []
    tops = [r for r, p in packages.items() if p["top"]] or list(packages)

    def add(kind, p, **kw):
        rule = rules.rule(community, finding_id(kind), p["name"], p["version"], p["alias"])
        findings.append(dict(kind=kind, package=p["label"], version=p["version"],
                             licence=p["license"], rule=rule, **kw))

    for ref, p in sorted(packages.items(), key=lambda kv: kv[1]["label"].lower()):
        if p["system"]:
            continue
        cls = licence_class(p["license"])
        classes[cls] = classes.get(cls, 0) + 1
        if cls in ("custom", "unknown"):
            add("unknown", p)
        if cls not in CAN_CONFLICT or not _follows(p):
            continue
        parents, hits = _walk(packages, [ref]), []
        for d in parents:
            q = packages[d]
            why = d != ref and _follows(q) and conflict(p["license"], q["license"])
            if why:
                hits.append({"package": q["label"], "licence": q["license"], "reason": why,
                             "path": _path(packages, parents, d)})
        if hits:
            hits.sort(key=lambda h: (len(h["path"]), h["package"].lower()))
            add("conflict", p, using=hits[:MAX_WITH], more=max(0, len(hits) - MAX_WITH))
    for ref in tops:
        parents = _walk(packages, [ref])
        hits = [d for d in parents if d != ref and _follows(packages[d])
                and licence_class(packages[d]["license"]) in COPYLEFT]
        if hits:
            reach.append({"top": packages[ref]["label"], "licence": packages[ref]["license"],
                          "copyleft": [{"package": packages[d]["label"],
                                        "licence": packages[d]["license"],
                                        "path": _path(packages, parents, d)} for d in sorted(
                                            hits, key=lambda d: packages[d]["label"].lower())]})
    kept = sorted(p["label"] for p in packages.values() if not p["system"] and restricted(
        p["redistributable"]))
    return {"classes": classes, "findings": findings, "copyleft": reach, "restricted": kept}


def dependencies(packages):
    """The dependency analysis of a build's *packages*: the most depended-on
    (rebuilds: how many depend on it, build links too; runtime: at run time), the
    packages only builds need, and those nothing the release asks for needs."""
    users, runtime_users = {r: set() for r in packages}, {r: set() for r in packages}
    for r, p in packages.items():
        for d in p["links"]:
            if d in users:
                users[d].add(r)
        for d in p["runtime"]:
            if d in runtime_users:
                runtime_users[d].add(r)

    def closure(edges, ref):
        seen, todo = set(), [ref]
        while todo:
            for u in edges[todo.pop()]:
                if u not in seen:
                    seen.add(u)
                    todo.append(u)
        return seen

    tops = [r for r, p in packages.items() if p["top"]]
    shipped = set(_walk(packages, tops, follow=lambda p: True))
    needed = set(_walk(packages, tops, key="links", follow=lambda p: True))
    rows = []
    for r, p in packages.items():
        if p["system"]:
            continue
        rows.append({"package": p["label"], "version": p["version"],
                     "rebuilds": len(closure(users, r)), "runtime": len(closure(runtime_users, r)),
                     "direct": len(users[r])})
    rows.sort(key=lambda x: (-x["rebuilds"], x["package"].lower()))
    names = lambda refs: sorted(packages[r]["label"] for r in refs if not packages[r]["system"])
    return {"packages": sum(not p["system"] for p in packages.values()),
            "top": names(tops), "most_depended": rows[:TOP_N],
            "build_only": names(needed - shipped) if tops else [],
            "unneeded": names(set(packages) - needed) if tops else []}


def diff(old, new):
    """What changed from build *old* to *new* (two graphs): packages added,
    removed, in another version, under another licence."""
    key = lambda pk: {p["label"].lower(): p for p in pk.values() if not p["system"]}
    a, b = key(old), key(new)
    out = {"added": [], "removed": [], "version": [], "licence": []}
    for k in sorted(set(a) | set(b)):
        if k not in a:
            out["added"].append({"package": b[k]["label"], "version": b[k]["version"]})
        elif k not in b:
            out["removed"].append({"package": a[k]["label"], "version": a[k]["version"]})
        else:
            if a[k]["version"] != b[k]["version"]:
                out["version"].append({"package": b[k]["label"], "from": a[k]["version"],
                                       "to": b[k]["version"]})
            if a[k]["license"] != b[k]["license"] and a[k]["license"] and b[k]["license"]:
                out["licence"].append({"package": b[k]["label"], "from": a[k]["license"],
                                       "to": b[k]["license"]})
    return out


# ── the release lines ────────────────────────────────────────────────────────

def _lines(conn, communities):
    """The newest build of each release line (sbom_plan), with whether it is
    indexed for this analysis."""
    from .sbom_plan import release_lines
    lines = release_lines(conn, communities)
    for r in lines:
        v = conn.execute("SELECT index_version FROM sbom_scans WHERE build_id = ?",
                         (r["build_id"],)).fetchone()
        r["indexed"] = bool(v and v["index_version"] >= LICENCE_INDEX)
    return lines


def _head(r):
    return {k: r.get(k) for k in ("build_id", "community", "line", "platform", "package",
                                  "version", "pipeline_id", "published_at", "indexed")}


def licence_lines(conn, communities=None, now=None):
    """{"releases": each release line of *communities*, with its open and
    accepted findings by kind (once indexed)}."""
    lines = _lines(conn, communities)
    rules = vuln_triage.Rules.load(conn, {r["community"] for r in lines}, now=now)
    out = []
    for r in lines:
        row = _head(r)
        if r["indexed"]:
            fs = licences(graph(conn, r["build_id"]), r["community"], rules)["findings"]
            row["open"] = {k: sum(f["kind"] == k and not f["rule"] for f in fs) for k in KINDS}
            row["accepted"] = sum(bool(f["rule"]) for f in fs)
        out.append(row)
    return {"releases": out}


def licence_report(conn, build_id, communities=None, now=None):
    """The licence analysis of a release line's newest build *build_id*; None
    when it is not one of them."""
    r = next((x for x in _lines(conn, communities) if x["build_id"] == build_id), None)
    if r is None:
        return None
    out = _head(r)
    if r["indexed"]:
        rules = vuln_triage.Rules.load(conn, {r["community"]}, now=now)
        out.update(licences(graph(conn, build_id), r["community"], rules))
    return out


def previous(conn, release):
    """The build the line of *release* published before it, or None."""
    from .sbom_scan import FINDINGS_INDEX
    rows = conn.execute(
        "SELECT b.build_id, MAX(b.published_at) AS published_at FROM builds b"
        " JOIN sbom_scans s ON s.build_id = b.build_id"
        " LEFT JOIN pipelines p ON p.id = b.pipeline_id"
        " WHERE b.community = ? AND COALESCE(p.platform, '') = ? AND b.build_id != ?"
        " AND s.index_version >= ? GROUP BY b.build_id",
        (release["community"], release["platform"] or "", release["build_id"],
         FINDINGS_INDEX)).fetchall()
    from .sbom_plan import _LINE
    older = [x for x in rows if _LINE.sub("", x["build_id"]) == release["line"]
             and x["published_at"] <= (release["published_at"] or "")]
    return max(older, key=lambda x: x["published_at"])["build_id"] if older else None


def dependency_lines(conn, communities=None):
    """{"releases": the release lines, "versions": the packages the newest builds
    of the lines have in several versions}."""
    lines = _lines(conn, communities)
    seen = {}
    for r in lines:
        for p in graph(conn, r["build_id"]).values():
            if not p["system"]:
                seen.setdefault(p["label"].lower(), {"package": p["label"], "versions": {}}
                                )["versions"].setdefault(p["version"], []).append(
                    "%s/%s (%s)" % (r["community"], r["line"], r["platform"] or "?"))
    versions = sorted((v for v in seen.values() if len(v["versions"]) > 1),
                      key=lambda v: (-len(v["versions"]), v["package"].lower()))
    return {"releases": [_head(r) for r in lines], "versions": versions}


def dependency_report(conn, build_id, communities=None):
    """The dependency analysis of a release line's newest build *build_id*, with
    the diff from the line's previous build; None when it is not one of them."""
    release = next((r for r in _lines(conn, communities) if r["build_id"] == build_id), None)
    if release is None:
        return None
    new = graph(conn, build_id)
    out = dict(_head(release), **dependencies(new))
    before = previous(conn, release)
    out["previous"] = before
    out["diff"] = diff(graph(conn, before), new) if before else None
    return out
