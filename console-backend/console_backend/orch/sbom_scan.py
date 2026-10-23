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
is an event (sbom.scanned)."""

import gzip
import hashlib
import json
import logging
import os
import re
import time

from . import db, events

log = logging.getLogger("bits_orchestrator.sbom_scan")

PER_PASS = 3                       # builds scanned in one worker pass, at most
BUDGET_S = 20                      # no new scan once a pass has spent this long
RETRY_MS = 3600 * 1000             # a failed scan is tried again after this
TIMEOUT_S = (5, 60)                # connect, read: the scanner gives up at 45 s
SEVERITIES = ("critical", "high", "medium", "low", "other")
SBOM = "sbom.cdx.json"


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


def due(conn, db_built, now, limit=None):
    """Build ids to scan, newest first: never scanned, last good scan with an
    older database or older than the build's newest publication, or failed more
    than RETRY_MS ago."""
    rows = conn.execute(
        "SELECT b.build_id, MAX(b.recorded_ms) AS seen, s.db_built, s.error, s.scanned_ms"
        " FROM builds b LEFT JOIN sbom_scans s ON s.build_id = b.build_id"
        " GROUP BY b.build_id ORDER BY seen DESC").fetchall()
    out, limit = [], limit or PER_PASS
    for r in rows:
        if r["scanned_ms"] is None:
            out.append(r["build_id"])
        elif r["error"]:
            if now - r["scanned_ms"] > RETRY_MS:
                out.append(r["build_id"])
        elif r["db_built"] != db_built or r["seen"] > r["scanned_ms"]:
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


def _scanned(conn, directory, bid, sha, db_built, summary, vulns):
    s = summary or {}
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
            " medium, low, other, packages, vulnerabilities, vulns_sha256, error)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'')"
            " ON CONFLICT(build_id) DO UPDATE SET sbom_sha256 = excluded.sbom_sha256,"
            " db_built = excluded.db_built, scanned_ms = excluded.scanned_ms,"
            " critical = excluded.critical, high = excluded.high, medium = excluded.medium,"
            " low = excluded.low, other = excluded.other, packages = excluded.packages,"
            " vulnerabilities = excluded.vulnerabilities, vulns_sha256 = excluded.vulns_sha256,"
            " error = ''",
            (bid, sha, db_built, db.now_ms(), *counts, int(s.get("packages") or 0),
             int(s.get("vulnerabilities") or 0), vsha))
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
            res = post(base + "/v1/scan", body)
        except Busy:
            break                                      # nothing recorded: next pass
        except Exception as e:            # pylint: disable=broad-except
            log.warning("scan of %s: %s", bid, e)
            _failed(conn, bid, str(e))
            continue
        _scanned(conn, directory, bid, hashlib.sha256(body).hexdigest(),
                 str((res.get("db") or {}).get("built") or db_built), res.get("summary"),
                 (res.get("sbom") or {}).get("vulnerabilities") or [])
        n += 1
    return n


def with_vulnerabilities(sbom, vulns):
    """The CycloneDX document *sbom* (bytes) with *vulns* as its section (bytes)."""
    doc = json.loads(sbom)
    doc["vulnerabilities"] = vulns
    return json.dumps(doc).encode()


def listing(conn, communities=None, flagged=False, limit=200):
    """The scanned builds, most severe first: counts, when, against which
    database, the pipelines that published them and their package."""
    q = ("SELECT s.build_id, s.db_built, s.scanned_ms, s.critical, s.high, s.medium, s.low,"
         " s.other, s.packages, s.vulnerabilities, s.error, MAX(b.pipeline_id) AS pipeline_id,"
         " b.community, p.package, p.version, p.platform, b.published_at"
         " FROM sbom_scans s JOIN builds b ON b.build_id = s.build_id"
         " LEFT JOIN pipelines p ON p.id = b.pipeline_id")
    where, args = [], []
    if communities is not None:
        if not communities:
            return []
        where.append("b.community IN (%s)" % ",".join("?" * len(communities)))
        args += sorted(communities)
    if flagged:
        where.append("(s.critical + s.high + s.medium + s.low + s.other) > 0")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += (" GROUP BY s.build_id ORDER BY s.critical DESC, s.high DESC, s.medium DESC,"
          " s.low DESC, s.scanned_ms DESC LIMIT ?")
    return [dict(r) for r in conn.execute(q, args + [int(limit)])]
