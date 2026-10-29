# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Upgrade plans from the scanned builds (sbom_scan's index): what to upgrade, to
which version and in which order, to remove the most security risk.

A release line is a community's build of one release name on one platform; its
plan is made from its newest indexed build. For each package with open findings
(not covered by the triage; a component the stack does not ship has none), the
target is the smallest stable version newer than the current one that the
scanner names as fixing each of them. The packages fall
into waves, each meant as one set of version bumps and one rebuild, so the
release stays consistent after each: 0 known exploited (CISA KEV), 1 upgrades
within the major version, 2 major upgrades; packages taken from the host system
are listed apart (an image or host update), and findings no version fixes are
left to the triage. A wave is ordered by the risk it removes (grype's score, which
weighs severity, EPSS and KEV) per package rebuilt (the package and all that
depend on it).

The converged plan then picks one version per package for all the release lines
(of all the communities in scope), the highest any of them needs, preferring a
version already built and scanned clean; a release line whose version is pinned
(a defaults override, another package's requires) is reported as an exception.
"""

import functools
import json
import re

from . import vuln_triage

# A finding's risk when the scan has none of grype's (0-100): by severity alone.
SEVERITY_RISK = {"critical": 50.0, "high": 25.0, "medium": 10.0, "low": 2.0, "other": 0.5}
WAVES = ((0, "Known exploited"), (1, "Upgrades within the major version"), (2, "Major upgrades"))
_LINE = re.compile(r"-[0-9a-f]{12}$")


_PRE_WORDS = {"alpha", "beta", "rc", "pre", "preview", "dev", "snapshot"}
_PRE_LETTERS = {"a", "b", "c"}      # 1.0a1, 2.0b3; not 1.1.1c (OpenSSL) nor hashes


_EPOCH, _TOKEN, _V = re.compile(r"^\d+:"), re.compile(r"\d+|[A-Za-z]+"), re.compile(r"^[vV](?=\d)")
_LETTER = re.compile(r"\d([A-Za-z])\d+$")


@functools.lru_cache(maxsize=65536)
def _vkey(v):
    """A sort key for versions: an epoch (1:...) first, numbers numerically, then
    words; a pre-release (rc, beta, ...) before the end, other words after it."""
    epoch, _, rest = str(v).partition(":") if _EPOCH.match(str(v)) else ("0", "", str(v))
    tokens = _TOKEN.findall(_V.sub("", rest))
    letter = _LETTER.search(rest)          # 1.0a1: a letter between the last digits only
    parts = [int(epoch or 0)]
    for i, t in enumerate(tokens):
        w = t.lower()
        pre = w in _PRE_WORDS or (letter is not None and i + 2 == len(tokens) and w in _PRE_LETTERS)
        parts.append((0, int(t)) if t.isdigit() else (-2, w) if pre else (-1, w))
    return tuple(parts) + ((-1.5, ""),)


def _prerelease(v):
    return any(p[0] == -2 for p in _vkey(v)[1:])


def _major(v):
    """The first number of a version (its epoch aside)."""
    k = _vkey(v)
    return k[1] if len(k) > 2 else None


def release_lines(conn, communities=None):
    """The newest indexed build of each release line of *communities* (None: all)."""
    from .sbom_scan import FINDINGS_INDEX, _in
    q = ("SELECT b.build_id, b.community, MAX(b.pipeline_id) AS pipeline_id, p.package,"
         " p.version, p.platform, MAX(b.published_at) AS published_at, s.scanned_ms,"
         " s.components, s.identified"
         " FROM builds b JOIN sbom_scans s ON s.build_id = b.build_id"
         " LEFT JOIN pipelines p ON p.id = b.pipeline_id WHERE s.index_version >= ?")
    args = [FINDINGS_INDEX]
    if communities is not None:
        if not communities:
            return []
        cond, vals = _in("b.community", communities)
        q += " AND " + cond
        args += vals
    newest = {}
    for r in conn.execute(q + " GROUP BY b.build_id", args):
        r = dict(r)
        r["line"] = _LINE.sub("", r["build_id"])
        key = (r["community"], r["line"], r["platform"] or "")
        if key not in newest or (r["published_at"], r["scanned_ms"]) > (
                newest[key]["published_at"], newest[key]["scanned_ms"]):
            newest[key] = r
    return sorted(newest.values(), key=lambda r: (r["community"], r["line"], r["platform"] or ""))


def _need(fixed_in, current):
    """The smallest stable fixing version newer than *current*, or None."""
    newer = [f for f in fixed_in if not _prerelease(f) and _vkey(f) > _vkey(current)]
    return min(newer, key=_vkey) if newer else None


def _dependents(packages):
    """A function: ref -> the refs that depend on it, directly or not (memoised)."""
    users, memo = {r: set() for r in packages}, {}
    for r, p in packages.items():
        for d in p["depends_on"]:
            if d in users:
                users[d].add(r)

    def of(ref):
        if ref not in memo:
            seen, todo = set(), [ref]
            while todo:
                for u in users[todo.pop()]:
                    if u not in seen:
                        seen.add(u)
                        todo.append(u)
            memo[ref] = seen - {ref}
        return memo[ref]
    return of


def plan_release(conn, rel, rules):
    """The plan of the release line *rel* (from release_lines), with *rules*."""
    bid, community = rel["build_id"], rel["community"]
    packages = {}
    for r in conn.execute("SELECT * FROM sbom_packages WHERE build_id = ?", (bid,)):
        try:
            on = [d for d in json.loads(r["depends_on"]) if isinstance(d, str)]
        except (TypeError, ValueError):
            on = []
        packages[r["ref"]] = dict(r, depends_on=on, findings=[], triaged=0, unshipped=0)
    for f in conn.execute("SELECT * FROM sbom_findings WHERE build_id = ?", (bid,)):
        p = packages.get(f["ref"])
        if p is None:
            continue
        if p["not_shipped"]:                       # a host tool, what only tools use, build-only
            p["unshipped"] += 1
            continue
        if rules.rule(community, f["vuln_id"], p["name"], p["version"], p["alias"]):
            p["triaged"] += 1
            continue
        try:
            fixed = [x for x in json.loads(f["fixed_in"]) if isinstance(x, str)]
        except (TypeError, ValueError):
            fixed = []
        p["findings"].append({"id": f["vuln_id"], "severity": f["severity"], "kev": f["kev"],
                              "epss": f["epss"], "need": _need(fixed, p["version"]),
                              "risk": f["risk"] or SEVERITY_RISK.get(f["severity"], 0.1)})
    users = _dependents(packages)
    items, host, unfixed = [], [], []
    for ref, p in packages.items():
        if not p["findings"]:
            continue
        fixed = [f for f in p["findings"] if f["need"]]
        left = [f for f in p["findings"] if not f["need"]]
        entry = {"ref": ref, "name": p["alias"] or p["name"], "sbom_name": p["name"],
                 "version": p["version"], "pinned_by": p["pinned_by"], "system": bool(p["system"]),
                 "fixes": sorted(f["id"] for f in fixed), "unfixed": sorted(f["id"] for f in left),
                 "kev": sorted(f["id"] for f in fixed if f["kev"]),
                 "risk_removed": round(sum(f["risk"] for f in fixed), 2),
                 "risk_left": round(sum(f["risk"] for f in left), 2), "triaged": p["triaged"],
                 "rebuild": len(users(ref)) + 1 if fixed else 0}
        if left:
            unfixed.append(entry)
        if not fixed:
            continue
        entry["target"] = max((f["need"] for f in fixed), key=_vkey)
        entry["major"] = _major(entry["target"]) != _major(p["version"])
        if p["system"]:
            host.append(entry)
            continue
        entry["wave"] = 0 if entry["kev"] else 2 if entry["major"] else 1
        items.append(entry)
    items.sort(key=lambda e: (e["wave"], -e["risk_removed"] / e["rebuild"], e["name"].lower()))
    total = sum(f["risk"] for p in packages.values() for f in p["findings"])
    waves, left_risk, rebuilt = [], total, set()
    for w, title in WAVES:
        ws = [e for e in items if e["wave"] == w]
        if not ws:
            continue
        removed = sum(e["risk_removed"] for e in ws)
        left_risk -= removed
        refs = set()
        for e in ws:
            refs |= users(e["ref"]) | {e["ref"]}
        rebuilt |= refs
        waves.append({"wave": w, "title": title, "packages": ws, "rebuild": len(refs),
                      "risk_removed": round(removed, 2), "risk_left": round(max(left_risk, 0), 2)})
    return dict(rel, components=len(packages), risk=round(total, 2), waves=waves,
                host=sorted(host, key=lambda e: -e["risk_removed"]),
                unfixed=sorted(unfixed, key=lambda e: -e["risk_left"]),
                findings=sum(len(p["findings"]) for p in packages.values()),
                triaged=sum(p["triaged"] for p in packages.values()),
                not_shipped=sum(p["unshipped"] for p in packages.values()))


def converge(conn, plans):
    """One version per package for all the release lines of *plans*: for each
    package one of them needs to upgrade, the highest version any needs, or a
    higher one already in use and scanned clean; per release line, the upgrade,
    or why it cannot follow (a pin)."""
    seen = {}       # package (lowercase bits name) -> [(plan, its package row, its plan entry)]
    for pl in plans:
        entries = {e["ref"]: e for w in pl["waves"] for e in w["packages"]}
        pl["_open"] = {e["ref"] for e in pl["unfixed"]} | set(entries)
        for r in conn.execute("SELECT ref, name, alias, version, system, pinned_by FROM sbom_packages"
                              " WHERE build_id = ? AND system = 0 AND not_shipped = ''",
                              (pl["build_id"],)):
            seen.setdefault((r["alias"] or r["name"]).lower(), []).append((pl, dict(r), entries.get(r["ref"])))
    out = []
    for key, uses in seen.items():
        if not any(e for _, _, e in uses):
            continue
        # what each line needs: its target, or its own version where it has no open finding
        needs = [e["target"] if e else r["version"] for pl, r, e in uses
                 if e or r["ref"] not in pl["_open"]]
        version = max(needs, key=_vkey)
        while True:     # a version in use that is itself vulnerable: what it needs
            higher = [e["target"] for _, r, e in uses if e and r["version"] == version]
            if not higher or max(higher, key=_vkey) == version:
                break
            version = max(higher, key=_vkey)
        clean = [pl["build_id"] for pl, r, e in uses
                 if r["version"] == version and r["ref"] not in pl["_open"]]
        lines = []
        for pl, r, e in uses:
            line = {"build_id": pl["build_id"], "community": pl["community"], "line": pl["line"],
                    "platform": pl["platform"], "version": r["version"], "pinned_by": r["pinned_by"],
                    "risk_removed": e["risk_removed"] if e else 0, "kev": e["kev"] if e else [],
                    "target": e["target"] if e else ""}
            if r["version"] == version:
                line["action"] = "keep"
            elif r["pinned_by"]:
                line["action"] = "pinned"           # its own target, or change the pin
            else:
                line["action"] = "upgrade"
                line["major"] = _major(version) != _major(r["version"])
            lines.append(line)
        name = uses[0][1]["alias"] or uses[0][1]["name"]
        out.append({"name": name, "version": version, "in_use_clean": clean,
                    "kev": sorted({k for ln in lines for k in ln["kev"]}),
                    "risk_removed": round(sum(ln["risk_removed"] for ln in lines), 2),
                    "pinned": sum(ln["action"] == "pinned" for ln in lines),
                    "releases": sorted(lines, key=lambda ln: (ln["community"], ln["line"]))})
    out.sort(key=lambda c: (not c["kev"], -c["risk_removed"], c["name"].lower()))
    return out


def plan(conn, communities=None, now=None):
    """{"releases": the plan of each release line, "converged": converge(...)}."""
    lines = release_lines(conn, communities)
    rules = vuln_triage.Rules.load(conn, {r["community"] for r in lines}, now=now)
    plans = [plan_release(conn, r, rules) for r in lines]
    converged = converge(conn, plans)
    for pl in plans:
        pl.pop("_open", None)
    return {"releases": plans, "converged": converged}
