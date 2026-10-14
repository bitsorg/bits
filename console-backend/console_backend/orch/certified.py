# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The certified set (M5): which BOMs the signed manifests are made from.

Until now the record is the bits-manifests git tree. The worker keeps its own:
each certified BOM's exact bytes (in `boms`, named by their sha256, never
changed) and its place in the set (`certified`: its path under manifests/, its
group, architecture and build, the version of the set that added it and the one
that removed it). A version is one step of the set; for now each version is
one bits-manifests main pipeline's commit, followed in pipeline order, the first
one importing the whole tree. Every addition and removal is an event (and so in
the outside copy), carrying the path, group, architecture, build and sha256.

`materialize` writes the set as of a version back out as a manifests/ tree, so
the shadow can compute a certification from the set as well as from git and
show the two agree. From M8 the worker adds versions itself, and bits-manifests
is no longer read.

Each certified BOM is also matched to the copy the build published in the store
(MANIFESTS/<build_id>/...), by content, ignoring the `certified_by` the
certification added: `same`, `differs` (a copy of that build, not the same
content) or `absent`. That is reported; the set does not depend on it, since
it keeps its own bytes.
"""

import hashlib
import json
import logging
import os
import shutil

from . import db, events, shadow

log = logging.getLogger("bits_orchestrator.certified")

SOURCE_GIT = "bits-manifests"
SOURCE_ORCHESTRATOR = "orchestrator"            # M8b: a version the worker made
MAX_VERSIONS = 10                 # per pass
MAX_ATTEMPTS = 5                  # a pipeline whose tree cannot be read is then left out
MATCH_PER_PASS = 50


def _subject(group, arch):
    return "certified:%s/%s" % (group or "-", arch or "-")


def _describe(body):
    """(group-independent) facts about one BOM: (build_id, arch, problem)."""
    from bits_helpers import certify
    try:
        man = json.loads(body)
    except ValueError:
        return "", "", "not JSON"
    if not isinstance(man, dict):
        return "", "", "not a JSON object"
    try:
        arch = certify.bom_architecture(man) or ""
    except certify.CertifyError as e:
        return str(man.get("build_id") or ""), "", str(e)[:200]
    return str(man.get("build_id") or ""), arch, ""


def _group(path):
    parts = path.split("/")
    return parts[0] if len(parts) >= 2 else ""


def _layout_problem(path):
    """bits takes a BOM's group from its directory only at <group>/<file>.json."""
    parts = path.split("/")
    return "" if len(parts) == 2 and parts[1].endswith(".json") else \
        "not at <group>/<file>.json: bits gives it no group of its own"


def digest_of(members):
    """The digest of a version's membership: sha256 over its sorted
    "path sha256" lines. It identifies the set the way a git commit did."""
    h = hashlib.sha256()
    for path, sha in sorted(members.items()):
        h.update(("%s %s\n" % (path, sha)).encode())
    return h.hexdigest()


def members(conn, version):
    """{path: sha256} of the set as of *version*."""
    return {r["path"]: r["bom_sha256"] for r in conn.execute(
        "SELECT path, bom_sha256 FROM certified WHERE added_version <= ?"
        " AND (removed_version IS NULL OR removed_version > ?)", (int(version), int(version)))}


def current(conn):
    """(version, pipeline_id, commit) of the newest version, or (0, None, None)."""
    r = conn.execute("SELECT version, pipeline_id, commit_sha FROM set_versions"
                     " ORDER BY version DESC LIMIT 1").fetchone()
    return (r["version"], r["pipeline_id"], r["commit_sha"]) if r else (0, None, None)


def followed(conn):
    """The newest bits-manifests pipeline the set has dealt with (followed or
    given up on), or None before the first import."""
    r = conn.execute("SELECT value FROM kv WHERE key = 'certified.pipeline'").fetchone()
    return int(r["value"]) if r else None


def _set_followed(conn, pid):
    conn.execute("INSERT INTO kv (key, value) VALUES ('certified.pipeline', ?) ON CONFLICT(key)"
                 " DO UPDATE SET value = excluded.value", (str(int(pid)),))


def version_of(conn, pipeline_id):
    r = conn.execute("SELECT version FROM set_versions WHERE pipeline_id = ?",
                     (int(pipeline_id),)).fetchone()
    return r["version"] if r else None


def apply_tree(conn, root, pipeline_id, commit, source=SOURCE_GIT):
    """Make the set equal the manifests/ tree at *root* as a new version.
    Returns (version, added, removed)."""
    # Read and hash first: the write lock is held only for the database work.
    files = {}
    for path, p in shadow._files(root).items():
        body = shadow._read(p)
        files[path] = (body, hashlib.sha256(body).hexdigest())
    with db.transaction(conn):
        return _apply(conn, files, pipeline_id, commit, source)


def change(conn, add=None, remove=(), cause=None):
    """A version the worker makes (M8b): the set as it is, with *add*
    ({path: BOM bytes}) put in and the paths in *remove* taken out. Call inside
    a transaction. Returns (version, added, removed); (None, 0, 0) when nothing
    changes."""
    live = {r["path"]: r["bom_sha256"] for r in conn.execute(
        "SELECT path, bom_sha256 FROM certified WHERE removed_version IS NULL")}
    files = {}
    for path, sha in live.items():
        if path not in remove:
            files[path] = (conn.execute("SELECT body FROM boms WHERE sha256 = ?",
                                        (sha,)).fetchone()["body"], sha)
    for path, body in (add or {}).items():
        body = bytes(body)
        files[path] = (body, hashlib.sha256(body).hexdigest())
    if {p: h for p, (_b, h) in files.items()} == live:
        return None, 0, 0
    return _apply(conn, files, None, "", SOURCE_ORCHESTRATOR, cause)


def _apply(conn, files, pipeline_id, commit, source, cause=None):
    """Make the set equal *files* ({path: (body, sha256)}) as a new version, in
    the caller's transaction."""
    now = db.now_ms()
    version = (conn.execute("SELECT MAX(version) AS v FROM set_versions").fetchone()["v"] or 0) + 1
    live = {r["path"]: r for r in conn.execute(
        "SELECT * FROM certified WHERE removed_version IS NULL")}
    first = conn.execute("SELECT 1 FROM certified LIMIT 1").fetchone() is None
    added = removed = 0
    for path in sorted(set(files) | set(live)):
        body, sha = files.get(path, (None, None))
        old = live.get(path)
        if old is not None and sha != old["bom_sha256"]:
            ev = events.append(conn, "certified.removed", _subject(old["grp"], old["arch"]),
                               events.WORKER,
                               {"path": path, "group": old["grp"], "arch": old["arch"],
                                "build_id": old["build_id"], "sha256": old["bom_sha256"],
                                "version": version, "commit": commit})
            conn.execute("UPDATE certified SET removed_version = ?, removed_event = ?"
                         " WHERE id = ?", (version, ev, old["id"]))
            removed += 1
        if body is not None and (old is None or sha != old["bom_sha256"]):
            build_id, arch, problem = _describe(body)
            problem = problem or _layout_problem(path)
            group = _group(path)
            conn.execute("INSERT OR IGNORE INTO boms (sha256, body, size, first_ms)"
                         " VALUES (?, ?, ?, ?)", (sha, body, len(body), now))
            payload = {"path": path, "group": group, "arch": arch, "build_id": build_id,
                       "sha256": sha, "version": version, "commit": commit}
            if first:
                payload["imported"] = True
            if problem:
                payload["problem"] = problem
            ev = events.append(conn, "certified.added", _subject(group, arch),
                               events.WORKER, payload)
            conn.execute(
                "INSERT INTO certified (path, grp, arch, build_id, bom_sha256, problem,"
                " added_version, added_event, added_ms) VALUES (?,?,?,?,?,?,?,?,?)",
                (path, group, arch, build_id, sha, problem, version, ev, now))
            added += 1
    digest = digest_of({p: h for p, (_b, h) in files.items()})
    conn.execute("INSERT INTO set_versions (version, pipeline_id, commit_sha, source,"
                 " added, removed, digest, created_ms) VALUES (?,?,?,?,?,?,?,?)",
                 (version, pipeline_id, commit, source, added, removed, digest, now))
    if pipeline_id is not None:
        _set_followed(conn, pipeline_id)
    events.append(conn, "certified.imported" if first else "certified.version", "certified",
                  events.WORKER, {"version": version, "pipeline": pipeline_id,
                                  "commit": commit, "added": added, "removed": removed,
                                  "digest": digest, "source": source}, cause=cause)
    return version, added, removed


def _attempt(conn, pid):
    key = "certified.attempts.%d" % int(pid)
    r = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    n = int(r["value"]) + 1 if r else 1
    with db.transaction(conn):
        conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key)"
                     " DO UPDATE SET value = excluded.value", (key, str(n)))
    return n


def _give_up(conn, pid, sha, reason):
    """Leave a pipeline out of the set's history, loudly: its comparisons say
    'unsynced', and the next version follows the next pipeline's tree."""
    with db.transaction(conn):
        _set_followed(conn, pid)
        conn.execute("DELETE FROM kv WHERE key = ?", ("certified.attempts.%d" % int(pid),))
        events.append(conn, "certified.gap", "certified", events.WORKER,
                      {"pipeline": pid, "commit": sha, "reason": reason[:500]})
    log.error("certified set: left out pipeline %s (%s)", pid, reason)


def sync(conn, reader, cache_dir, limit=MAX_VERSIONS, branch=None, attempts=MAX_ATTEMPTS):
    """Follow bits-manifests: one version per main pipeline, in pipeline order,
    never past one that is still running (its commit may change the tree before
    a later one's does). The first import starts at the oldest certification
    still to compare (else the newest), not at the start of the history.
    Returns the versions added."""
    last = followed(conn)
    if last is None:
        r = conn.execute("SELECT MIN(pipeline_id) AS p FROM certifications WHERE state = 'pending'"
                         ).fetchone()["p"]
        if r is None:
            r = conn.execute("SELECT MAX(pipeline_id) AS p FROM certifications").fetchone()["p"]
        if r is None:
            return 0
        last = r - 1
    ceiling = None
    if branch:
        busy = [int(p["id"]) for scope in ("running", "pending")
                for p in reader.finished_pipelines(branch, scope=scope) if isinstance(p, dict)]
        ceiling = min(busy) if busy else None
    rows = conn.execute("SELECT pipeline_id, sha FROM certifications WHERE pipeline_id > ?"
                        " AND pipeline_id < ? ORDER BY pipeline_id LIMIT ?",
                        (last, ceiling if ceiling is not None else 1 << 62, int(limit))).fetchall()
    n = 0
    for r in rows:
        pid, sha = r["pipeline_id"], r["sha"]
        try:
            if not sha:
                sha = str((reader.get_pipeline(pid) or {}).get("sha") or "")
            root = shadow.tree(reader, sha, cache_dir)
            apply_tree(conn, root, pid, sha)
            n += 1
        except Exception as e:
            if shadow.transient(e) or shadow._transient_cause(e):
                raise                     # GitLab is away: try again next pass, from here
            reason = "%s: %s" % (type(e).__name__, str(e)[:400])
            if _attempt(conn, pid) >= attempts:
                _give_up(conn, pid, sha, reason)
                continue
            log.warning("certified set: pipeline %s: %s", pid, reason)
            return n                      # keep the order: try this one again next pass
        finally:
            shadow.prune(cache_dir, protect=(sha,))
    return n


def materialize(conn, version, root):
    """Write the set as of *version* as a manifests/ tree under *root*; returns
    the manifests directory."""
    out = os.path.join(root, "manifests")
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(out)
    real = os.path.realpath(out) + os.sep
    for r in conn.execute(
            "SELECT c.path, b.body FROM certified c JOIN boms b ON b.sha256 = c.bom_sha256"
            " WHERE c.added_version <= ? AND (c.removed_version IS NULL OR c.removed_version > ?)",
            (int(version), int(version))):
        p = os.path.join(out, r["path"])
        if ".." in r["path"].split("/") or not os.path.realpath(p).startswith(real):
            raise ValueError("unsafe path in the certified set: %r" % r["path"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(r["body"])
    return out


def _strip(doc):
    return {k: v for k, v in doc.items() if k not in ("certified_by", "_source_group")}


def _store_docs(s3, bucket, build_id):
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": "MANIFESTS/%s/" % build_id}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for o in page.get("Contents", []):
            if o["Key"].endswith(".json"):
                try:
                    doc = json.loads(s3.get_object(Bucket=bucket, Key=o["Key"])["Body"].read())
                except ValueError:
                    continue
                if isinstance(doc, dict):
                    yield o["Key"], doc
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            return


def match_store(conn, store, limit=MATCH_PER_PASS):
    """Match live certified BOMs not yet matched to their build's copy of the
    same architecture in the store: 'same' (ignoring certified_by), 'differs',
    or 'absent'. A store that fails to answer ends the pass; the rest wait."""
    from bits_helpers import certify
    if store is None:
        return 0
    s3, bucket = store
    rows = conn.execute("SELECT c.id, c.build_id, c.arch, b.body FROM certified c JOIN boms b"
                        " ON b.sha256 = c.bom_sha256 WHERE c.store_match = ''"
                        " AND c.removed_version IS NULL LIMIT ?", (int(limit),)).fetchall()
    n = 0
    for r in rows:
        match, key = "absent", None
        try:
            mine = _strip(json.loads(r["body"]))
        except (ValueError, AttributeError):
            mine = None
        try:
            if r["build_id"] and mine is not None:
                for k, doc in _store_docs(s3, bucket, r["build_id"]):
                    try:
                        arch = certify.bom_architecture(doc) or ""
                    except certify.CertifyError:
                        continue
                    if arch != r["arch"]:
                        continue
                    if _strip(doc) == mine:
                        match, key = "same", k
                        break
                    match, key = "differs", k
        except Exception as e:
            if shadow.transient(e):
                log.warning("certified set: the store did not answer: %s", e)
                return n
            match, key = "error", None
            log.warning("certified set: cannot match %s: %s", r["build_id"], e)
        with db.transaction(conn):
            conn.execute("UPDATE certified SET store_match = ?, store_key = ? WHERE id = ?",
                         (match, key, r["id"]))
        n += 1
    return n


def listing(conn, groups=None, group="", arch="", include_removed=False, limit=1000,
            build_id=""):
    """The set (live entries, unless *include_removed*), and a summary. *groups*:
    the groups the caller may see (lowercase; None = all). One read snapshot."""
    where, args = ["1 = 1"], []
    if build_id:
        where.append("c.build_id = ?")
        args.append(build_id)
    if not include_removed:
        where.append("c.removed_version IS NULL")
    if group:
        where.append("c.grp = ?")
        args.append(group)
    if arch:
        where.append("c.arch = ?")
        args.append(arch)
    if groups is not None:
        where.append("lower(c.grp) IN (%s)" % ",".join("?" * len(groups)) if groups else "0")
        args.extend(sorted(groups))
    keep = ("path", "arch", "build_id", "bom_sha256", "problem", "store_match", "store_key",
            "added_version", "added_commit", "removed_version", "approval_id")
    conn.execute("BEGIN")
    try:
        entries = [dict({k: r[k] for k in keep}, group=r["grp"]) for r in conn.execute(
            "SELECT c.*, v.commit_sha AS added_commit FROM certified c"
            " JOIN set_versions v ON v.version = c.added_version WHERE " + " AND ".join(where)
            + " ORDER BY c.grp, c.arch, c.path LIMIT ?", args + [max(1, min(int(limit), 5000))])]
        version, pid, commit = current(conn)
        digest = (conn.execute("SELECT digest FROM set_versions WHERE version = ?",
                               (version,)).fetchone() or {"digest": ""})["digest"]
        counts = {}
        for r in conn.execute("SELECT grp, store_match, COUNT(*) AS n FROM certified"
                              " WHERE removed_version IS NULL GROUP BY grp, store_match"):
            if groups is None or r["grp"].lower() in groups:
                counts.setdefault(r["grp"], {})[r["store_match"] or "unchecked"] = r["n"]
    finally:
        conn.execute("COMMIT")
    return {"version": version, "pipeline": pid, "commit": commit, "digest": digest,
            "groups": counts, "entries": entries}


def bom(conn, sha256, groups=None):
    """One certified BOM's bytes, if the caller may see a group it is in."""
    rows = conn.execute("SELECT DISTINCT grp FROM certified WHERE bom_sha256 = ?",
                        (sha256,)).fetchall()
    if not rows or (groups is not None and not any(r["grp"].lower() in groups for r in rows)):
        return None
    r = conn.execute("SELECT body FROM boms WHERE sha256 = ?", (sha256,)).fetchone()
    return r["body"] if r else None
