# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The admins' triage of vulnerability findings (VEX).

A rule of a community says a vulnerability does not matter there, for one
version of a package, for every version of it, or for every package (package
and version empty): not_affected (with a CycloneDX justification),
false_positive, or will_not_fix (an accepted risk). It has a reason, an author
and, optionally, an expiry; it is revoked rather than deleted. A finding a rule
covers is suppressed: left out of the effective counts, and served in the SBOM
with the rule as its CycloneDX `analysis`. Adding and revoking are events.
"""

import re

from . import db, events

STATES = ("not_affected", "false_positive", "will_not_fix")
JUSTIFICATIONS = ("code_not_present", "code_not_reachable", "requires_configuration",
                  "requires_dependency", "requires_environment", "protected_by_compiler",
                  "protected_at_runtime", "protected_at_perimeter",
                  "protected_by_mitigating_control")
MAX_DAYS = 730
_VULN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{1,63}$")
_DAY_MS = 86400 * 1000


class Invalid(ValueError):
    pass


def _text(value, name, limit, required=False):
    v = str(value or "").strip()
    if len(v) > limit or ("\n" in v and name != "reason"):
        raise Invalid("%s: at most %d characters, on one line" % (name, limit))
    if required and not v:
        raise Invalid("%s is required" % name)
    return v


def add(conn, community, vuln_id, state, reason, by, package="", version="",
        justification="", days=None, now=None):
    """Add a rule; returns its id. Raises Invalid."""
    community = _text(community, "community", 128, True).lower()
    vuln_id = str(vuln_id or "").strip()
    if not _VULN.match(vuln_id):
        raise Invalid("vuln_id: a vulnerability id such as CVE-2024-1234")
    if state not in STATES:
        raise Invalid("state is one of: %s" % ", ".join(STATES))
    justification = str(justification or "").strip()
    if state == "not_affected" and justification not in JUSTIFICATIONS:
        raise Invalid("not_affected needs a justification, one of: %s" % ", ".join(JUSTIFICATIONS))
    if state != "not_affected":
        justification = ""
    package = _text(package, "package", 128)
    version = _text(version, "version", 128)
    if version and not package:
        raise Invalid("a version needs its package")
    reason = _text(reason, "reason", 2000, True)
    now = db.now_ms() if now is None else now
    expires = None
    if days not in (None, "", 0):
        try:
            days = int(days)
        except (TypeError, ValueError):
            raise Invalid("days: a number of days")
        if not 1 <= days <= MAX_DAYS:
            raise Invalid("days: from 1 to %d" % MAX_DAYS)
        expires = now + days * _DAY_MS
    with db.transaction(conn):
        rid = conn.execute(
            "INSERT INTO vuln_triage (community, vuln_id, package, version, state, justification,"
            " reason, author, created_ms, expires_ms) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (community, vuln_id, package, version, state, justification, reason, by, now,
             expires)).lastrowid
        events.append(conn, "sbom.triaged", "vuln:%s" % vuln_id, events.Actor("person", by),
                      {"rule": rid, "community": community, "package": package,
                       "version": version, "state": state, "justification": justification,
                       "reason": reason, "expires_ms": expires})
    return rid


def get(conn, rule_id):
    r = conn.execute("SELECT * FROM vuln_triage WHERE id = ?", (int(rule_id),)).fetchone()
    return dict(r) if r else None


def revoke(conn, rule_id, by, now=None):
    """Revoke a rule; False when it already was."""
    now = db.now_ms() if now is None else now
    with db.transaction(conn):
        cur = conn.execute("UPDATE vuln_triage SET revoked_ms = ?, revoked_by = ? WHERE id = ?"
                           " AND revoked_ms IS NULL", (now, by, int(rule_id)))
        if cur.rowcount != 1:
            return False
        r = get(conn, rule_id)
        events.append(conn, "sbom.triage_revoked", "vuln:%s" % r["vuln_id"],
                      events.Actor("person", by), {"rule": r["id"], "community": r["community"]})
    return True


def listing(conn, communities=None, revoked=False, now=None, limit=500):
    """The rules of *communities* (None: all), newest first; the active ones
    only unless *revoked* (then expired and revoked ones too)."""
    now = db.now_ms() if now is None else now
    q, where, args = "SELECT * FROM vuln_triage", [], []
    if communities is not None:
        if not communities:
            return []
        where.append("community IN (%s)" % ",".join("?" * len(communities)))
        args += sorted(communities)
    if not revoked:
        where.append("revoked_ms IS NULL AND (expires_ms IS NULL OR expires_ms > ?)")
        args.append(now)
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY id DESC LIMIT ?"
    return [dict(r) for r in conn.execute(q, args + [int(limit)])]


def _covers(rule, vuln_id, names, version):
    return (rule["vuln_id"].lower() == vuln_id.lower()
            and (not rule["package"] or rule["package"].lower() in names)
            and (not rule["version"] or rule["version"] == version))


class Rules:
    """The active rules by community, to look findings up in."""

    def __init__(self, rules):
        self._by = {}
        for r in rules:
            self._by.setdefault((r["community"], r["vuln_id"].lower()), []).append(r)
        for rs in self._by.values():      # the most specific rule first, then the newest
            rs.sort(key=lambda r: (not r["version"], not r["package"], -r["id"]))

    @classmethod
    def load(cls, conn, communities=None, now=None):
        return cls(listing(conn, communities, now=now, limit=1000000))

    def rule(self, community, vuln_id, name, version, alias=""):
        """The rule covering the finding, or None."""
        names = {n.lower() for n in (name, alias) if n}
        return next((r for r in self._by.get((community, str(vuln_id).lower()), ())
                     if _covers(r, vuln_id, names, version)), None)


def analysis(rule):
    """A rule as a CycloneDX vulnerability `analysis`."""
    out = {"state": "exploitable" if rule["state"] == "will_not_fix" else rule["state"],
           "detail": "%s (%s)" % (rule["reason"], rule["author"])}
    if rule["justification"]:
        out["justification"] = rule["justification"]
    if rule["state"] == "will_not_fix":
        out["response"] = ["will_not_fix"]
    return out


NOT_SHIPPED = {"tool": "a host tool, run but not shipped with the stack (sbom: tool)",
               "build-only": "a system package only the build needs, not shipped with the stack"}


def annotate(vulns, components, rules, community, not_shipped=None):
    """CycloneDX *vulns* with the rules of *community*: a vulnerability's affected
    components a rule covers move to a copy of it with that rule's analysis (and
    bits:triage:* properties); those *not_shipped* ({ref: why}) to one marked
    not_affected (bits:not_shipped). *components* maps a bom-ref to its component."""
    out, not_shipped = [], not_shipped or {}
    for v in vulns or []:
        if not isinstance(v, dict):
            continue
        groups = {}
        for a in v.get("affects") or []:
            ref = a.get("ref") if isinstance(a, dict) else None
            if isinstance(ref, str) and not_shipped.get(ref):
                groups.setdefault(("not_shipped", not_shipped[ref]), (None, []))[1].append(a)
                continue
            c = components.get(ref) if isinstance(ref, str) else None
            c = c if isinstance(c, dict) else {}
            props = c.get("properties")
            alias = next((p.get("value") for p in props if isinstance(p, dict)
                          and p.get("name") == "bits:package"), "") if isinstance(props, list) else ""
            r = rules.rule(community, v.get("id"), str(c.get("name") or ""),
                           str(c.get("version") or ""), str(alias or ""))
            groups.setdefault(r["id"] if r else None, (r, []))[1].append(a)
        if list(groups) == [None] or not groups:
            out.append(v)
            continue
        for key, (r, affects) in sorted(groups.items(), key=lambda g: (
                isinstance(g[0], tuple), g[0] if isinstance(g[0], tuple) else g[0] or 0)):
            entry = dict(v, affects=affects)
            if isinstance(key, tuple):
                entry["bom-ref"] = "%s-%s" % (v.get("bom-ref") or v.get("id"), key[1])
                entry["analysis"] = {"state": "not_affected",
                                     "detail": NOT_SHIPPED.get(key[1], "not shipped with the stack")}
                entry["properties"] = [{"name": "bits:not_shipped", "value": key[1]}]
            elif r is not None:
                entry["bom-ref"] = "%s-triage-%d" % (v.get("bom-ref") or v.get("id"), key)
                entry["analysis"] = analysis(r)
                entry["properties"] = [{"name": "bits:triage:" + k, "value": str(r[k])} for k in (
                    "id", "package", "version", "author", "created_ms", "expires_ms") if r[k]]
            out.append(entry)
    return out
