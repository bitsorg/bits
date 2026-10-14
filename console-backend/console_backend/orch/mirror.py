# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The mirror (M8b): every version of the set the worker owns, committed to
bits-manifests, so the repository stays what the CI would sign from. Going back
to M8a is then turning BITS_ORCH_OWNS_SET off and the CI's sign job on.

A version is one commit on the main branch (files under manifests/ added,
updated or deleted), pushed in version order with the gitlab-mirror
credential; its commit is recorded on the version. The set is the worker's: a
commit that fails is tried again on the next pass, and nothing waits on it.
The marker (MANIFESTS/orchestrator.json in the store) tells bits clients that
this store's manifests are the orchestrator's.
"""

import json
import logging

from . import certified, db, events

log = logging.getLogger("bits_orchestrator.mirror")
MARKER = "MANIFESTS/orchestrator.json"


def _kv(conn, key):
    r = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def _set_kv(conn, key, value):
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                 " DO UPDATE SET value = excluded.value", (key, str(value)))


def push(conn, settings, forge, limit=5):
    """Commit the versions not mirrored yet. Returns how many were pushed."""
    if forge is None:
        raise RuntimeError("the mirror needs the gitlab-mirror credential"
                           " (BITS_FORGE_MIRROR_TOKEN or the credentials proxy)")
    done = _kv(conn, "mirror.version")
    if done is None:
        # The first time: everything up to the last imported version is the
        # repository's own already.
        done = conn.execute("SELECT MAX(version) FROM set_versions WHERE source != ?",
                            (certified.SOURCE_ORCHESTRATOR,)).fetchone()[0] or 0
        with db.transaction(conn):
            _set_kv(conn, "mirror.version", done)
    done, branch, n = int(done), settings.orch_manifests_branch, 0
    for v in conn.execute("SELECT version, source FROM set_versions WHERE version > ?"
                          " ORDER BY version LIMIT ?", (done, int(limit))).fetchall():
        version = v["version"]
        commit, added, removed = "", 0, 0
        if v["source"] == certified.SOURCE_ORCHESTRATOR:
            adds = {r["path"]: bytes(r["body"]) for r in conn.execute(
                "SELECT c.path, b.body FROM certified c JOIN boms b ON b.sha256 = c.bom_sha256"
                " WHERE c.added_version = ?", (version,))}
            gone = [r["path"] for r in conn.execute(
                "SELECT path FROM certified WHERE removed_version = ?", (version,))
                if r["path"] not in adds]
            acts = []
            for path, body in sorted(adds.items()):
                fp = "manifests/" + path
                if not forge.file_exists(fp, branch):
                    acts.append({"action": "create", "file_path": fp, "content": body.decode()})
                elif forge.file_raw(fp, branch) != body:
                    acts.append({"action": "update", "file_path": fp, "content": body.decode()})
                # (already there as it is: pushed before a crash, nothing to commit)
            for path in sorted(gone):
                fp = "manifests/" + path
                if forge.file_exists(fp, branch):
                    acts.append({"action": "delete", "file_path": fp})
            if acts:
                commit = str((forge.create_commit(
                    branch, acts, "orchestrator: certified set version %d (+%d -%d)"
                    % (version, len(adds), len(gone))) or {}).get("id") or "")
            added, removed = len(adds), len(gone)
        with db.transaction(conn):
            if commit:
                conn.execute("UPDATE set_versions SET commit_sha = ? WHERE version = ?",
                             (commit, version))
            _set_kv(conn, "mirror.version", version)
            if commit:
                events.append(conn, "mirror.pushed", "certified", events.WORKER,
                              {"version": version, "commit": commit, "added": added,
                               "removed": removed})
        n += 1
    return n


def write_marker(conn, settings, store, writer, issuer, prefix):
    """Put MANIFESTS/orchestrator.json in the store (once, or when it changed):
    bits clients then leave signing to the orchestrator."""
    since = _kv(conn, "owns_set.since")
    if since is None:
        import datetime
        since = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with db.transaction(conn):
            _set_kv(conn, "owns_set.since", since)
    body = json.dumps({"issuer": issuer, "manifests": prefix, "since": since},
                      indent=1, sort_keys=True).encode()
    s3, bucket = store
    try:
        if s3.get_object(Bucket=bucket, Key=MARKER)["Body"].read() == body:
            return False
    except Exception:
        pass                                   # absent (or unreadable): write it
    writer.put_object(Bucket=bucket, Key=MARKER, Body=body, ContentType="application/json")
    with db.transaction(conn):
        events.append(conn, "mirror.marker", "certified", events.WORKER,
                      {"issuer": issuer, "since": since})
    return True
