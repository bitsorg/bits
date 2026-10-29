# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The console's CVMFS status document: cvmfs-status.json in the build project's
generic package registry, one record per CVMFS publish, replaced by its pkg_id
(<package>-<version>-<revision>-<build arch>, as the CI job wrote it).

The worker is its only writer, so a read-modify-write needs no lock. It merges
into the document, since the database holds only the lookback window. A record
is written for each build of a successful pipeline that published to CVMFS,
once: a cursor on when builds were recorded or their pipeline indexed again,
advanced after the upload."""

import json
import logging
import time

from . import db

log = logging.getLogger("bits_orchestrator.cvmfs_status")

PACKAGE, VERSION, FILE = "cvmfs-status", "latest", "cvmfs-status.json"
CURSOR = "cvmfs_status.since"


def _missing(e):
    code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
    return code in ("NoSuchKey", "404", "NotFound")


def _built(store, keys, package):
    """(the build arch, version-revision of *package*) from a build's BOMs. A BOM
    gone from the store is passed over; any other read error is raised, so the
    build is looked at again on the next pass."""
    from bits_helpers.cvmfs_release import build_architecture, get_json, is_bom
    s3, bucket = store
    boms = []
    for key in keys:
        try:
            bom = get_json(s3, bucket, key)
        except Exception as exc:          # pylint: disable=broad-except
            if not _missing(exc):
                raise
            log.warning("BOM %s is not in the store", key)
            continue
        if is_bom(bom):
            boms.append(bom)
    bom, entry = next(((b, p) for b in boms for p in b["packages"]
                       if p.get("package") == package), (None, None))
    if entry is None:
        return "", ""
    rev = str(entry.get("revision") or "")
    vdir = "%s-%s" % (entry.get("version"), rev) if rev else str(entry.get("version"))
    # The build's arch as the CI job named it; older BOMs record the host's.
    arch = build_architecture(boms) or str(entry.get("effective_architecture")
                                           or bom.get("effective_architecture") or "")
    return arch, vdir


def records(conn, store, since):
    """(the records of the builds of successful pipelines indexed after *since*,
    the newest such time). A pipeline retried to success is indexed again."""
    rows = conn.execute(
        "SELECT b.pipeline_id, b.build_id, b.bom_keys, b.published_at, b.recorded_ms,"
        " max(b.recorded_ms, p.indexed_ms) AS seen, p.variables FROM builds b"
        " JOIN pipelines p ON p.id = b.pipeline_id"
        " WHERE p.outcome = 'success' AND max(b.recorded_ms, p.indexed_ms) > ? ORDER BY seen",
        (since,)).fetchall()
    out, newest = [], since
    for r in rows:
        newest = max(newest, r["seen"])
        v = json.loads(r["variables"] or "{}")
        package = v.get("PACKAGE", "")
        if v.get("PUBLISH_CVMFS") != "true" or not package:
            log.debug("pipeline %s: no CVMFS publish (PUBLISH_CVMFS %r)",
                      r["pipeline_id"], v.get("PUBLISH_CVMFS"))
            continue
        arch, vdir = _built(store, json.loads(r["bom_keys"] or "[]"), package)
        if not (arch and vdir):
            log.warning("build %s of pipeline %s: no %s in its BOMs, no CVMFS status",
                        r["build_id"], r["pipeline_id"], package)
            continue
        out.append({"pkg_id": "%s-%s-%s" % (package, vdir, arch.replace("/", "_")),
                    "package": package, "version": v.get("VERSION", ""),
                    "platform": v.get("PLATFORM", ""), "community": v.get("COMMUNITY", ""),
                    "published_at": r["published_at"] or time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["recorded_ms"] / 1000)),
                    "run_id": str(r["pipeline_id"]), "pipeline": "prepub",
                    "status": "published"})
    return out, newest


def merge(doc, new):
    """*doc* with each record of *new* in place of the one with its pkg_id."""
    doc = doc if isinstance(doc, dict) else {}
    pkgs = [p for p in doc.get("packages") or [] if isinstance(p, dict)]
    at = {p.get("pkg_id"): i for i, p in enumerate(pkgs)}
    for rec in new:
        if rec["pkg_id"] in at:
            pkgs[at[rec["pkg_id"]]] = rec
        else:
            at[rec["pkg_id"]] = len(pkgs)
            pkgs.append(rec)
    return dict(doc, packages=pkgs, updated_at=max([doc.get("updated_at") or ""]
                                                   + [r["published_at"] for r in new]))


def step(conn, forge, store):
    """Write the records of the newly recorded builds. Returns how many."""
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (CURSOR,)).fetchone()
    since = int(row["value"]) if row else 0
    new, newest = records(conn, store, since)
    if new:
        raw = forge.generic_file(PACKAGE, VERSION, FILE)
        try:
            doc = json.loads(raw) if raw else {}
        except ValueError:
            # Never overwrite a document that cannot be read: it holds the history.
            raise RuntimeError("%s in the package registry is not JSON" % FILE)
        forge.put_generic_file(PACKAGE, VERSION, FILE,
                               (json.dumps(merge(doc, new), indent=2) + "\n").encode())
    if newest != since:
        with db.transaction(conn):
            conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                         " DO UPDATE SET value = excluded.value", (CURSOR, str(newest)))
    return len(new)
