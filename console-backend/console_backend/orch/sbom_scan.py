# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""Vulnerability scans of the published builds' SBOMs.

The worker step sends each build's sbom.cdx.json (MANIFESTS/<build_id>/) to the
sbom-scanner service (BITS_ORCH_SCANNER_URL, bits-services), which matches it with
grype and returns it with a CycloneDX `vulnerabilities` section. A build is
scanned once, again whenever the scanner's vulnerability database is newer than
the one of its last good scan, so old builds learn of new CVEs, and again when a
newer pipeline publishes the build (a re-run writes its SBOM anew).

sbom_scans keeps each build's counts by severity (findings: a vulnerability in a
package) and the last error; the `vulnerabilities` section itself is a gzip file
beside the database (sbom-scans/, rebuilt by scanning again, so not in its
backups). A failed scan keeps the last good counts. A change of a build's counts
is an event (sbom.scanned).

A good scan also indexes the SBOM: its components (sbom_packages: which builds
contain a package), what matched (sbom_findings), and how many components a
scanner can identify at all (a CPE, or a purl of an ecosystem it knows): a build
with none is not scannable, and its zero findings say nothing. The findings the
admins' triage covers (vuln_triage) are left out of the effective counts."""

import gzip
import hashlib
import json
import logging
import os
import re
import time

from . import db, events, vuln_triage

log = logging.getLogger("bits_orchestrator.sbom_scan")

PER_PASS = 3                       # builds scanned in one worker pass, at most
BUDGET_S = 20                      # no new scan once a pass has spent this long
RETRY_MS = 3600 * 1000             # a failed scan is tried again after this
TIMEOUT_S = (5, 60)                # connect, read: the scanner gives up at 45 s
SEVERITIES = ("critical", "high", "medium", "low", "other")
SBOM = "sbom.cdx.json"
INDEX_VERSION = 2                  # a good scan older than this indexes its build again
# purl types grype matches (pkg:generic and pkg:github it cannot)
MATCHED_PURLS = {"apk", "cargo", "cocoapods", "composer", "conan", "deb", "gem", "golang",
                 "hackage", "hex", "maven", "npm", "nuget", "pub", "pypi", "rpm", "swift"}


class Failed(Exception):
    pass


class Busy(Exception):
    """The scanner cannot take a scan now (updating its database): stop the pass."""


def _get(url):
    import requests
    r = requests.get(url, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def _post(url, body):
    import requests
    r = requests.post(url, data=body, timeout=TIMEOUT_S,
                      headers={"Content-Type": "application/json"})
    if r.status_code == 503:
        raise Busy(r.text[:200])
    if r.status_code >= 400:
        raise Failed("scanner answered %d: %s" % (r.status_code, r.text[:200]))
    return r.json()


def _missing(e):
    # Without ListBucket a missing key answers 403: there is nothing to read either way.
    code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
    return isinstance(e, KeyError) or code in ("NoSuchKey", "404", "NotFound", "AccessDenied", "403")


def _path(directory, bid):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", bid)
    if safe != bid or safe.startswith("."):
        safe = hashlib.sha256(bid.encode()).hexdigest()
    return os.path.join(directory, safe + ".vulns.json.gz")


def _write(directory, bid, vulns):
    os.makedirs(directory, exist_ok=True)
    path = _path(directory, bid)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(gzip.compress(json.dumps(vulns, sort_keys=True).encode(), mtime=0))
    os.replace(tmp, path)


def vulnerabilities(directory, bid):
    """Build *bid*'s `vulnerabilities` from its last good scan, or None."""
    if not directory:
        return None
    try:
        with open(_path(directory, bid), "rb") as f:
            return json.loads(gzip.decompress(f.read()))
    except FileNotFoundError:
        return None


def _props(c):
    """A CycloneDX component's properties as {name: value} (strings)."""
    props = c.get("properties")
    return {str(p.get("name")): str(p.get("value")) for p in props if isinstance(p, dict)
            } if isinstance(props, list) else {}


def _severity(v):
    r = (v.get("ratings") or [{}])[0]
    s = str(r.get("severity") if isinstance(r, dict) else "").lower()
    return s if s in SEVERITIES else "other"


def _list(x):
    return x if isinstance(x, list) else []


def _float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def index(doc, vulns):
    """(components, identified, sbom_packages rows, sbom_findings rows) of the
    CycloneDX document *doc* and its scan's *vulns*, without the build id."""
    packages, identified = [], 0
    comps = [c for c in _list(doc.get("components"))
             if isinstance(c, dict) and c.get("bom-ref")]
    by_name = {}                       # bits name -> bom-ref, for build_requires
    for c in comps:
        props = _props(c)
        by_name.setdefault(str(props.get("bits:package") or c.get("name") or ""), str(c["bom-ref"]))
    deps = {}
    for d in _list(doc.get("dependencies")):
        if isinstance(d, dict) and isinstance(d.get("dependsOn"), list):
            deps[str(d.get("ref"))] = [str(r) for r in d["dependsOn"]]
    for c in comps:
        props, ref = _props(c), str(c["bom-ref"])
        purl = str(c.get("purl") or "")
        cpe = str(c.get("cpe") or props.get("syft:cpe23") or "")
        if cpe or purl.partition(":")[2].partition("/")[0] in MATCHED_PURLS:
            identified += 1
        build = [by_name[str(p.get("value"))] for p in _list(c.get("properties"))
                 if isinstance(p, dict) and p.get("name") == "bits:build_requires"
                 and str(p.get("value")) in by_name]
        on = sorted(set(deps.get(ref, []) + build) - {ref})
        version, rev = str(c.get("version") or ""), props.get("bits:revision")
        if rev and version.endswith("-" + rev):         # older SBOMs: version-revision
            version = version[:-len(rev) - 1]
        packages.append((ref, str(c.get("name") or ""), str(props.get("bits:package") or ""),
                         version, purl, cpe,
                         int(props.get("bits:provided_by") == "system"),
                         str(props.get("bits:pinned_by") or ""), json.dumps(on)))
    refs = {p[0] for p in packages}
    findings = {}
    for v in _list(vulns):
        if not isinstance(v, dict) or not v.get("id"):
            continue
        threat = _props(v)
        for a in _list(v.get("affects")):
            if not isinstance(a, dict) or not isinstance(a.get("ref"), str) or a["ref"] not in refs:
                continue
            fixed = sorted({str(x.get("version")) for x in _list(a.get("versions"))
                            if isinstance(x, dict) and x.get("status") == "unaffected" and x.get("version")})
            findings[(str(v["id"]), a["ref"])] = (
                _severity(v), _float(threat.get("grype:risk")), _float(threat.get("grype:epss")),
                str(threat.get("grype:kev") or ""), json.dumps(fixed))
    return len(packages), identified, packages, sorted(k + f for k, f in findings.items())


def due(conn, db_built, now, limit=None):
    """Build ids to scan, newest first: never scanned, last good scan with an
    older database, older than the build's newest publication or not indexed
    (INDEX_VERSION), or failed more than RETRY_MS ago."""
    rows = conn.execute(
        "SELECT b.build_id, MAX(b.recorded_ms) AS seen, s.db_built, s.error, s.scanned_ms,"
        " s.index_version"
        " FROM builds b LEFT JOIN sbom_scans s ON s.build_id = b.build_id"
        " GROUP BY b.build_id ORDER BY seen DESC").fetchall()
    out, limit = [], limit or PER_PASS
    for r in rows:
        if r["scanned_ms"] is None:
            out.append(r["build_id"])
        elif r["error"]:
            if now - r["scanned_ms"] > RETRY_MS:
                out.append(r["build_id"])
        elif (r["db_built"] != db_built or r["seen"] > r["scanned_ms"]
              or r["index_version"] < INDEX_VERSION):
            out.append(r["build_id"])
        if len(out) >= limit:
            break
    return out


def _failed(conn, bid, error):
    """Record a failed scan, keeping the last good one's counts."""
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO sbom_scans (build_id, scanned_ms, error) VALUES (?, ?, ?)"
            " ON CONFLICT(build_id) DO UPDATE SET scanned_ms = excluded.scanned_ms,"
            " error = excluded.error", (bid, db.now_ms(), error[:300]))


def _scanned(conn, directory, bid, sha, db_built, summary, vulns, doc):
    s = summary or {}
    n_components, n_identified, packages, findings = index(doc, vulns)
    counts = [int(s.get(k) or 0) for k in SEVERITIES]
    vsha = hashlib.sha256(json.dumps(vulns, sort_keys=True).encode()).hexdigest()
    old = conn.execute("SELECT * FROM sbom_scans WHERE build_id = ?", (bid,)).fetchone()
    if old is None or old["vulns_sha256"] != vsha or vulnerabilities(directory, bid) is None:
        _write(directory, bid, vulns)
    good = old is not None and old["db_built"]           # a good scan before this one
    before = [old[k] for k in SEVERITIES] if good else None
    with db.transaction(conn):
        conn.execute(
            "INSERT INTO sbom_scans (build_id, sbom_sha256, db_built, scanned_ms, critical, high,"
            " medium, low, other, packages, vulnerabilities, vulns_sha256, error, components,"
            " identified, index_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'',?,?,?)"
            " ON CONFLICT(build_id) DO UPDATE SET sbom_sha256 = excluded.sbom_sha256,"
            " db_built = excluded.db_built, scanned_ms = excluded.scanned_ms,"
            " critical = excluded.critical, high = excluded.high, medium = excluded.medium,"
            " low = excluded.low, other = excluded.other, packages = excluded.packages,"
            " vulnerabilities = excluded.vulnerabilities, vulns_sha256 = excluded.vulns_sha256,"
            " error = '', components = excluded.components, identified = excluded.identified,"
            " index_version = excluded.index_version",
            (bid, sha, db_built, db.now_ms(), *counts, int(s.get("packages") or 0),
             int(s.get("vulnerabilities") or 0), vsha, n_components, n_identified, INDEX_VERSION))
        indexed = old is not None and old["index_version"] >= INDEX_VERSION
        if not (indexed and old["sbom_sha256"] == sha):      # the same SBOM: the same rows
            conn.execute("DELETE FROM sbom_packages WHERE build_id = ?", (bid,))
            conn.executemany("INSERT OR IGNORE INTO sbom_packages (build_id, ref, name, alias,"
                             " version, purl, cpe, system, pinned_by, depends_on)"
                             " VALUES (?,?,?,?,?,?,?,?,?,?)",
                             [(bid, *p) for p in packages])
        if not (indexed and old["sbom_sha256"] == sha and old["vulns_sha256"] == vsha):
            conn.execute("DELETE FROM sbom_findings WHERE build_id = ?", (bid,))
            conn.executemany("INSERT INTO sbom_findings (build_id, vuln_id, ref, severity, risk,"
                             " epss, kev, fixed_in) VALUES (?,?,?,?,?,?,?,?)",
                             [(bid, *f) for f in findings])
        if counts != (before or [0] * len(SEVERITIES)):    # a first clean scan: no event
            events.append(conn, "sbom.scanned", "build:%s" % bid, events.WORKER,
                          {"build_id": bid, "db_built": db_built,
                           **dict(zip(SEVERITIES, counts)), "was": before})


def step(conn, store, url, directory, get=_get, post=_post, clock=time.monotonic):
    """Scan the builds that are due; returns how many were scanned."""
    if not url:
        return 0
    if store is None or not directory:
        raise RuntimeError("SBOM scans need the store (BITS_ORCH_STORE) and BITS_ORCH_DB")
    base = url.rstrip("/")
    status = get(base + "/v1/status")
    if status.get("updating"):
        return 0                                       # back once its database is in
    db_built = str((status.get("db") or {}).get("built") or "")
    if not db_built:
        raise RuntimeError("the sbom-scanner has no valid vulnerability database")
    s3, bucket = store
    start, n = clock(), 0
    for bid in due(conn, db_built, db.now_ms()):
        if clock() - start > BUDGET_S:
            break
        try:
            body = s3.get_object(Bucket=bucket, Key="MANIFESTS/%s/%s" % (bid, SBOM))["Body"].read()
        except Exception as e:            # pylint: disable=broad-except
            if not _missing(e):
                raise
            _failed(conn, bid, "no %s in the store" % SBOM)
            continue
        try:
            doc = json.loads(body)
            if not isinstance(doc, dict):
                raise ValueError("not an object")
        except (ValueError, RecursionError) as e:
            _failed(conn, bid, "%s is not JSON: %s" % (SBOM, e))
            continue
        try:
            res = post(base + "/v1/scan", body)
        except Busy:
            break                                      # nothing recorded: next pass
        except Exception as e:            # pylint: disable=broad-except
            log.warning("scan of %s: %s", bid, e)
            _failed(conn, bid, str(e))
            continue
        try:
            _scanned(conn, directory, bid, hashlib.sha256(body).hexdigest(),
                     str((res.get("db") or {}).get("built") or db_built), res.get("summary"),
                     (res.get("sbom") or {}).get("vulnerabilities") or [], doc)
        except (TypeError, ValueError, AttributeError, KeyError) as e:   # an odd SBOM
            log.warning("indexing the scan of %s: %r", bid, e)
            _failed(conn, bid, "could not index the scan: %r" % e)
            continue
        n += 1
    return n


def _in(column, values):
    return "%s IN (%s)" % (column, ",".join("?" * len(values))), sorted(values)


def _findings(conn, community, rules):
    """Findings of the builds of *community* ({build id: its community}) as dicts,
    each with the triage rule covering it (None if none), its package and version."""
    if not community:
        return []
    cond, vals = _in("f.build_id", community)
    rows = conn.execute(
        "SELECT f.build_id, f.vuln_id, f.ref, f.severity, p.name, p.alias, p.version"
        " FROM sbom_findings f JOIN sbom_packages p ON p.build_id = f.build_id AND p.ref = f.ref"
        " WHERE " + cond, vals).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["rule"] = rules.rule(community[d["build_id"]], d["vuln_id"], d["name"], d["version"],
                               d["alias"])
        out.append(d)
    return out


def listing(conn, communities=None, flagged=False, limit=200, now=None):
    """The scanned builds, most severe first (*flagged*: those with open findings
    or not scannable): counts, when, against which database, the pipelines that
    published them and their package; how many components a scanner could
    identify, and the counts without the findings the triage covers (`effective`,
    once indexed), by which they are ordered."""
    q = ("SELECT s.build_id, s.db_built, s.scanned_ms, s.critical, s.high, s.medium, s.low,"
         " s.other, s.packages, s.vulnerabilities, s.error, s.components, s.identified,"
         " s.index_version, MAX(b.pipeline_id) AS pipeline_id,"
         " b.community, p.package, p.version, p.platform, b.published_at"
         " FROM sbom_scans s JOIN builds b ON b.build_id = s.build_id"
         " LEFT JOIN pipelines p ON p.id = b.pipeline_id")
    where, args = [], []
    if communities is not None:
        if not communities:
            return []
        cond, vals = _in("b.community", communities)
        where.append(cond)
        args += vals
    if flagged:
        where.append("((s.critical + s.high + s.medium + s.low + s.other) > 0"
                     " OR (s.components > 0 AND s.identified = 0))")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += (" GROUP BY s.build_id ORDER BY s.critical DESC, s.high DESC, s.medium DESC,"
          " s.low DESC, s.scanned_ms DESC LIMIT ?")
    rows = [dict(r) for r in conn.execute(q, args + [int(limit)])]
    indexed = {r["build_id"] for r in rows if r["index_version"] >= INDEX_VERSION}
    rules = vuln_triage.Rules.load(conn, {r["community"] for r in rows}, now=now)
    effective = {b: dict.fromkeys(SEVERITIES, 0) for b in indexed}
    suppressed = dict.fromkeys(indexed, 0)
    for f in _findings(conn, {r["build_id"]: r["community"] for r in rows
                              if r["build_id"] in indexed}, rules):
        if f["rule"]:
            suppressed[f["build_id"]] += 1
        else:
            effective[f["build_id"]][f["severity"]] += 1
    for r in rows:
        indexed_now = r.pop("index_version") >= INDEX_VERSION
        r["effective"] = effective.get(r["build_id"]) if indexed_now else None
        r["suppressed"] = suppressed.get(r["build_id"], 0)
    if flagged:
        rows = [r for r in rows if any(_open(r)) or _blind(r)]
    return sorted(rows, key=lambda r: [-n for n in _open(r)[:4]] + [-r["scanned_ms"]])


def _blind(row):
    """Is a listed scan of an SBOM none of whose components a scanner can identify?"""
    return row["effective"] is not None and row["components"] > 0 and not row["identified"]


def _open(row):
    """A listed scan's counts by severity, without what the triage covers."""
    return [(row["effective"] or row)[k] for k in SEVERITIES]


def _version_match(version):
    """SQL for a version and every version it is a prefix of up to a separator
    ("2.9" or "2.9.x": 2.9, 2.9.13, 2.9-1; not 2.90); None for no version."""
    v = re.sub(r"[.]?[x*]$", "", version.strip())
    if not v:
        return None
    esc = v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return ("(p.version = ? OR p.version LIKE ? ESCAPE '\\' OR p.version LIKE ? ESCAPE '\\'"
            " OR p.version LIKE ? ESCAPE '\\')", [v, esc + ".%", esc + "-%", esc + "+%"])


def packages(conn, name, version="", communities=None, limit=500, now=None):
    """The indexed builds that contain the package *name* (its name in the SBOM
    or its bits name, any case), at *version* (see _version_match) if given,
    newest first: the build, its release, the component and its findings."""
    if communities is not None and not communities:
        return []
    where = ["(p.name = ? COLLATE NOCASE OR p.alias = ? COLLATE NOCASE)"]
    args = [name.strip(), name.strip()]
    match = _version_match(version)
    if match:
        where.append(match[0])
        args += match[1]
    if communities is not None:
        cond, vals = _in("b.community", communities)
        where.append(cond)
        args += vals
    rows = [dict(r) for r in conn.execute(
        "SELECT p.build_id, p.ref, p.name, p.alias, p.version AS component_version, p.purl, p.cpe,"
        " p.system, b.community, MAX(b.pipeline_id) AS pipeline_id, pl.package, pl.version,"
        " pl.platform, MAX(b.published_at) AS published_at, s.scanned_ms"
        " FROM sbom_packages p JOIN builds b ON b.build_id = p.build_id"
        " JOIN sbom_scans s ON s.build_id = p.build_id"
        " LEFT JOIN pipelines pl ON pl.id = b.pipeline_id WHERE " + " AND ".join(where) +
        " GROUP BY p.build_id, p.ref ORDER BY published_at DESC, p.build_id DESC LIMIT ?",
        args + [int(limit)])]
    rules = vuln_triage.Rules.load(conn, {r["community"] for r in rows}, now=now)
    by = {}
    for f in _findings(conn, {r["build_id"]: r["community"] for r in rows}, rules):
        by.setdefault((f["build_id"], f["ref"]), []).append(
            {"id": f["vuln_id"], "severity": f["severity"],
             "suppressed": f["rule"]["id"] if f["rule"] else None})
    for r in rows:
        r["system"] = bool(r["system"])
        r["findings"] = sorted(by.get((r["build_id"], r["ref"]), []),
                               key=lambda f: (SEVERITIES.index(f["severity"]), f["id"]))
    return rows


def names(conn, communities=None):
    """The package names (and bits names) in the indexed builds, for completion."""
    if communities is not None and not communities:
        return []
    q = "SELECT DISTINCT p.name, p.alias FROM sbom_packages p"
    args = []
    if communities is not None:
        cond, args = _in("b.community", communities)
        q += " JOIN builds b ON b.build_id = p.build_id WHERE " + cond
    out = set()
    for r in conn.execute(q, args):
        out.update(n for n in (r["name"], r["alias"]) if n)
    return sorted(out, key=str.lower)


def affected(conn, vuln_id, communities=None, limit=500, now=None):
    """The indexed builds a vulnerability was found in, newest first: the build,
    its release, the package and version, and the triage rule covering it."""
    if communities is not None and not communities:
        return []
    where, args = ["f.vuln_id = ? COLLATE NOCASE"], [vuln_id.strip()]
    if communities is not None:
        cond, vals = _in("b.community", communities)
        where.append(cond)
        args += vals
    rows = [dict(r) for r in conn.execute(
        "SELECT f.build_id, f.vuln_id, f.ref, f.severity, p.name, p.alias,"
        " p.version AS component_version,"
        " p.system, b.community, MAX(b.pipeline_id) AS pipeline_id, pl.package, pl.version,"
        " pl.platform, MAX(b.published_at) AS published_at"
        " FROM sbom_findings f JOIN sbom_packages p ON p.build_id = f.build_id AND p.ref = f.ref"
        " JOIN builds b ON b.build_id = f.build_id LEFT JOIN pipelines pl ON pl.id = b.pipeline_id"
        " WHERE " + " AND ".join(where) +
        " GROUP BY f.build_id, f.ref ORDER BY published_at DESC, f.build_id DESC LIMIT ?",
        args + [int(limit)])]
    rules = vuln_triage.Rules.load(conn, {r["community"] for r in rows}, now=now)
    for r in rows:
        rule = rules.rule(r["community"], r["vuln_id"], r["name"], r["component_version"],
                          r["alias"])
        r["system"] = bool(r["system"])
        r["suppressed"] = rule["id"] if rule else None
    return rows
