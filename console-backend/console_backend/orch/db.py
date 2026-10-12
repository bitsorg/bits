# SPDX-FileCopyrightText: 2026 CERN
# SPDX-License-Identifier: GPL-3.0-or-later
"""The orchestrator's database: one SQLite file in WAL mode, shared by the worker
(which owns it and applies migrations) and the API (which writes only requests,
approvals and corrections).

Schema changes are numbered migrations (each a list of statements), applied in order by the worker at start
and recorded in PRAGMA user_version. The API refuses a schema newer than the one
it knows, so an old API never writes rows a newer schema reads differently.
Each later milestone adds its tables as a new migration.
"""

import contextlib
import fcntl
import os
import sqlite3
import time
from urllib.parse import quote

MIGRATIONS = [
    # 1 — the event log (also the audit chain) and the actions every change of
    # state goes through.
    [
        """CREATE TABLE events (
            seq         INTEGER PRIMARY KEY,
            ts_ms       INTEGER NOT NULL,
            kind        TEXT NOT NULL,
            subject     TEXT NOT NULL,
            actor_kind  TEXT NOT NULL,
            actor_id    TEXT NOT NULL,
            cause       TEXT,
            payload     TEXT NOT NULL,
            prev_hash   TEXT NOT NULL,
            hash        TEXT NOT NULL
        )""",
        "CREATE INDEX events_subject ON events(subject, seq)",
        """CREATE TABLE actions (
            id                INTEGER PRIMARY KEY,
            kind              TEXT NOT NULL,
            params            TEXT NOT NULL,
            idempotency_key   TEXT NOT NULL,
            subject           TEXT NOT NULL,
            community         TEXT NOT NULL,
            state             TEXT NOT NULL CHECK (state IN
                                ('proposed','approved','running','done','failed','rejected')),
            requested_by_kind TEXT NOT NULL,
            requested_by_id   TEXT NOT NULL,
            approved_by_kind  TEXT,
            approved_by_id    TEXT,
            approval_id       INTEGER,
            cause             TEXT,
            attempts          INTEGER NOT NULL DEFAULT 0,
            not_before_ms     INTEGER NOT NULL DEFAULT 0,
            result            TEXT,
            error             TEXT,
            created_ms        INTEGER NOT NULL,
            updated_ms        INTEGER NOT NULL,
            UNIQUE (kind, requested_by_kind, requested_by_id, idempotency_key)
        )""",
        "CREATE INDEX actions_state ON actions(state, id)",
        "CREATE INDEX actions_subject ON actions(subject, state, id)",
    ],
    # 2 — what the worker knows of GitLab pipelines and the builds they published
    # (M1). Both can be rebuilt from GitLab and S3.
    [
        """CREATE TABLE kv (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )""",
        """CREATE TABLE pipelines (
            id            INTEGER PRIMARY KEY,
            is_build      INTEGER NOT NULL,
            community     TEXT NOT NULL DEFAULT '',
            name          TEXT NOT NULL DEFAULT '',
            source        TEXT NOT NULL DEFAULT '',
            ref           TEXT NOT NULL DEFAULT '',
            sha           TEXT NOT NULL DEFAULT '',
            web_url       TEXT NOT NULL DEFAULT '',
            package       TEXT NOT NULL DEFAULT '',
            version       TEXT NOT NULL DEFAULT '',
            platform      TEXT NOT NULL DEFAULT '',
            variables     TEXT NOT NULL DEFAULT '{}',
            gl_status     TEXT NOT NULL DEFAULT '',
            outcome       TEXT NOT NULL DEFAULT 'running' CHECK (outcome IN
                            ('running','success','failed','canceled')),
            jobs          TEXT NOT NULL DEFAULT '[]',
            created_at    TEXT NOT NULL DEFAULT '',
            updated_at    TEXT NOT NULL DEFAULT '',
            finished_at   TEXT NOT NULL DEFAULT '',
            first_seen_ms INTEGER NOT NULL,
            checked_ms    INTEGER NOT NULL DEFAULT 0,
            indexed_ms    INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX pipelines_outcome ON pipelines(is_build, outcome, id)",
        "CREATE INDEX pipelines_community ON pipelines(community, id)",
        """CREATE TABLE builds (
            pipeline_id   INTEGER NOT NULL REFERENCES pipelines(id),
            build_id      TEXT NOT NULL,
            community     TEXT NOT NULL DEFAULT '',
            state         TEXT NOT NULL DEFAULT 'published',
            arches        TEXT NOT NULL DEFAULT '[]',
            bom_keys      TEXT NOT NULL DEFAULT '[]',
            top_level     TEXT NOT NULL DEFAULT '[]',
            packages      INTEGER NOT NULL DEFAULT 0,
            published_at  TEXT NOT NULL DEFAULT '',
            recorded_ms   INTEGER NOT NULL,
            PRIMARY KEY (pipeline_id, build_id)
        )""",
        """CREATE TABLE hints (
            pipeline_id   INTEGER PRIMARY KEY,
            kind          TEXT NOT NULL,
            ts_ms         INTEGER NOT NULL
        )""",
    ],
    # 3 — failures (M2): every failed job's record, the deduplicated issues they
    # are occurrences of, and the reviewed signatures that name a cause. These
    # cannot be rebuilt once GitLab expires the logs: they are backed up.
    [
        "ALTER TABLE pipelines ADD COLUMN collected_ms INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE signatures (
            id            INTEGER PRIMARY KEY,
            pattern       TEXT NOT NULL,
            cause         TEXT NOT NULL,
            action        TEXT NOT NULL,
            safe_to_retry INTEGER NOT NULL DEFAULT 0,
            state         TEXT NOT NULL DEFAULT 'active' CHECK (state IN
                            ('proposed','active','retired')),
            proposed_by   TEXT NOT NULL,
            accepted_by   TEXT,
            version       INTEGER NOT NULL DEFAULT 1,
            created_ms    INTEGER NOT NULL
        )""",
        """CREATE TABLE issues (
            id            INTEGER PRIMARY KEY,
            fingerprint   TEXT NOT NULL UNIQUE,
            package       TEXT NOT NULL,
            title         TEXT NOT NULL,
            excerpt       TEXT NOT NULL,
            first_seen    TEXT NOT NULL,
            last_seen     TEXT NOT NULL,
            count         INTEGER NOT NULL DEFAULT 0,
            communities   TEXT NOT NULL DEFAULT '[]',
            platforms     TEXT NOT NULL DEFAULT '[]',
            status        TEXT NOT NULL DEFAULT 'open',
            signature_id  INTEGER REFERENCES signatures(id),
            created_ms    INTEGER NOT NULL
        )""",
        "CREATE INDEX issues_last ON issues(last_seen)",
        """CREATE TABLE failures (
            id             INTEGER PRIMARY KEY,
            job_id         INTEGER NOT NULL,
            pipeline_id    INTEGER NOT NULL REFERENCES pipelines(id),
            package        TEXT NOT NULL,
            version        TEXT NOT NULL DEFAULT '',
            community      TEXT NOT NULL DEFAULT '',
            platform       TEXT NOT NULL DEFAULT '',
            job_name       TEXT NOT NULL DEFAULT '',
            failure_reason TEXT NOT NULL DEFAULT '',
            runner         TEXT NOT NULL DEFAULT '',
            bits_version   TEXT NOT NULL DEFAULT '',
            excerpt        TEXT NOT NULL,
            source         TEXT NOT NULL,
            log_url        TEXT NOT NULL DEFAULT '',
            failed_at      TEXT NOT NULL DEFAULT '',
            fingerprint    TEXT NOT NULL,
            issue_id       INTEGER NOT NULL REFERENCES issues(id),
            recorded_ms    INTEGER NOT NULL,
            UNIQUE (job_id, package)
        )""",
        "CREATE INDEX failures_issue ON failures(issue_id, id)",
        "CREATE INDEX failures_pipeline ON failures(pipeline_id)",
        "CREATE INDEX failures_community ON failures(community, id)",
    ],
    # 4 — approvals (M3): passkey-approved records that survive a restart. A
    # pre-approval lets CI sign one build's manifests; a standing approval lets a
    # schedule's builds be certified for a number of runs or days. They cannot
    # be rebuilt: they are backed up, and every grant, use and revocation is an
    # event.
    [
        """CREATE TABLE approvals (
            id            INTEGER PRIMARY KEY,
            kind          TEXT NOT NULL CHECK (kind IN ('preapproval', 'standing')),
            subject       TEXT NOT NULL,
            groups        TEXT NOT NULL,
            approver      TEXT NOT NULL,
            via           TEXT NOT NULL DEFAULT 'console',
            packages      TEXT NOT NULL DEFAULT '[]',
            binding       TEXT NOT NULL DEFAULT '{}',
            assertion     TEXT NOT NULL DEFAULT '{}',
            challenge     TEXT NOT NULL DEFAULT '',
            created_ms    INTEGER NOT NULL,
            expires_ms    INTEGER NOT NULL,
            signs         INTEGER NOT NULL DEFAULT 0,
            sign_cap      INTEGER NOT NULL DEFAULT 0,
            runs_left     INTEGER,
            revoked_ms    INTEGER,
            revoked_by    TEXT
        )""",
        "CREATE INDEX approvals_subject ON approvals(kind, subject, id)",
    ],
    # 5 (M4): certifications in shadow. A certification is one bits-manifests
    # pipeline whose certify job signed; a sign run is one architecture of it,
    # here the worker's own computation compared with what was signed (mode
    # 'shadow', and 'set' for the same computed from the certified set; the
    # worker's own certification of the build, unsigned, 'replay' (M8a); its
    # signing 'canary' and 'live' from M8). store_digests
    # remembers the sha256 of store objects, which never change, so a
    # certification only hashes the objects that are new.
    [
        """CREATE TABLE certifications (
            pipeline_id  INTEGER PRIMARY KEY,
            sha          TEXT NOT NULL,
            before_sha   TEXT NOT NULL DEFAULT '',
            updated_at   TEXT NOT NULL DEFAULT '',
            job_id       INTEGER,
            job_status   TEXT NOT NULL DEFAULT '',
            state        TEXT NOT NULL DEFAULT 'pending'
                         CHECK (state IN ('pending', 'compared', 'skipped', 'failed')),
            reason       TEXT NOT NULL DEFAULT '',
            archs        TEXT NOT NULL DEFAULT '[]',
            attempts     INTEGER NOT NULL DEFAULT 0,
            next_ms      INTEGER NOT NULL DEFAULT 0,
            created_ms   INTEGER NOT NULL,
            updated_ms   INTEGER NOT NULL
        )""",
        "CREATE INDEX certifications_due ON certifications(state, next_ms, pipeline_id)",
        """CREATE TABLE sign_runs (
            id               INTEGER PRIMARY KEY,
            certification    INTEGER NOT NULL REFERENCES certifications(pipeline_id),
            mode             TEXT NOT NULL,
            arch             TEXT NOT NULL,
            state            TEXT NOT NULL,
            source           TEXT NOT NULL DEFAULT '',
            source_commit    TEXT NOT NULL DEFAULT '',
            packages         INTEGER NOT NULL DEFAULT 0,
            manifest_sha256  TEXT NOT NULL DEFAULT '',
            signed_sha256    TEXT NOT NULL DEFAULT '',
            diff             TEXT NOT NULL DEFAULT '{}',
            created_ms       INTEGER NOT NULL,
            UNIQUE (certification, mode, arch)
        )""",
        """CREATE TABLE store_digests (
            key         TEXT PRIMARY KEY,
            etag        TEXT NOT NULL,
            size        INTEGER NOT NULL,
            sha256      TEXT NOT NULL,
            checked_ms  INTEGER NOT NULL
        )""",
    ],
    # 6 (M5): the certified set. boms holds each certified BOM's exact bytes,
    # named by their sha256 and never changed; certified places them in the set
    # (added and removed by versions of the set); a version is one step of it,
    # for now one bits-manifests main pipeline's commit. They cannot be rebuilt
    # from GitLab once bits-manifests is retired: backed up, and every change is
    # an event.
    [
        """CREATE TABLE boms (
            sha256    TEXT PRIMARY KEY,
            body      BLOB NOT NULL,
            size      INTEGER NOT NULL,
            first_ms  INTEGER NOT NULL
        )""",
        """CREATE TABLE set_versions (
            version      INTEGER PRIMARY KEY,
            pipeline_id  INTEGER UNIQUE,
            commit_sha   TEXT NOT NULL DEFAULT '',
            source       TEXT NOT NULL,
            added        INTEGER NOT NULL DEFAULT 0,
            removed      INTEGER NOT NULL DEFAULT 0,
            digest       TEXT NOT NULL DEFAULT '',
            job_status   TEXT NOT NULL DEFAULT '',
            created_ms   INTEGER NOT NULL
        )""",
        """CREATE TABLE certified (
            id               INTEGER PRIMARY KEY,
            path             TEXT NOT NULL,
            grp              TEXT NOT NULL,
            arch             TEXT NOT NULL DEFAULT '',
            build_id         TEXT NOT NULL DEFAULT '',
            bom_sha256       TEXT NOT NULL REFERENCES boms(sha256),
            problem          TEXT NOT NULL DEFAULT '',
            store_match      TEXT NOT NULL DEFAULT '',
            store_key        TEXT,
            approval_id      INTEGER,
            added_version    INTEGER NOT NULL,
            added_event      INTEGER,
            added_ms         INTEGER NOT NULL,
            removed_version  INTEGER,
            removed_event    INTEGER
        )""",
        "CREATE INDEX certified_live ON certified(removed_version, grp, arch)",
        "CREATE UNIQUE INDEX certified_live_path ON certified(path) WHERE removed_version IS NULL",
    ],
]

SCHEMA_VERSION = len(MIGRATIONS)


class SchemaError(Exception):
    pass


class LockedError(Exception):
    """Another worker holds the database lock."""


def now_ms():
    return int(time.time() * 1000)


def connect(path, create=True, busy_ms=10000):
    """A connection with the settings both processes rely on. Autocommit mode:
    writes happen only inside `transaction()`. With create=False a missing file
    is an error instead of a new empty database."""
    if create:
        conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    else:
        conn = sqlite3.connect("file:%s?mode=rw" % quote(path), uri=True,
                               timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=%d" % int(busy_ms))
    return conn


@contextlib.contextmanager
def transaction(conn):
    """BEGIN IMMEDIATE: take the write lock up front, so two writers serialise
    instead of one failing mid-transaction, and the event chain reads a stable
    last hash."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        # SQLite may already have rolled back (disk full, I/O error); never let
        # a second error hide the first, nor leave the write lock held.
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn):
    """Apply every migration newer than the database. Worker only."""
    have = version(conn)
    if have > SCHEMA_VERSION:
        raise SchemaError("database schema %d is newer than this code (%d)"
                          % (have, SCHEMA_VERSION))
    for n in range(have, SCHEMA_VERSION):
        with transaction(conn):
            for stmt in MIGRATIONS[n]:
                conn.execute(stmt)
            conn.execute("PRAGMA user_version=%d" % (n + 1))
    return SCHEMA_VERSION


def check(conn):
    """For the API: the schema must be one this code knows, and present."""
    have = version(conn)
    if have > SCHEMA_VERSION:
        raise SchemaError("database schema %d is newer than this code (%d)"
                          % (have, SCHEMA_VERSION))
    if have < SCHEMA_VERSION:
        raise SchemaError("database schema %d is older than this code (%d): the "
                          "worker has not migrated it yet" % (have, SCHEMA_VERSION))


def open_checked(path, busy_ms=10000):
    """For the API: an existing database the worker has migrated."""
    try:
        conn = connect(path, create=False, busy_ms=busy_ms)
    except sqlite3.OperationalError as e:
        raise SchemaError("cannot open %s (%s): has the worker started?" % (path, e))
    try:
        check(conn)
    except Exception:
        conn.close()
        raise
    return conn


def backup(conn, directory, keep=7, now=None):
    """Copy the database to *directory*/orch-<YYYYMMDD>.db once a day (SQLite's
    online backup: consistent while in use), keeping the newest *keep*. Returns
    the file written, or None when today's copy exists."""
    day = time.strftime("%Y%m%d", time.gmtime(now or time.time()))
    target = os.path.join(directory, "orch-%s.db" % day)
    if os.path.exists(target):
        return None
    os.makedirs(directory, exist_ok=True)
    tmp = target + ".part"
    dst = sqlite3.connect(tmp)
    try:
        conn.backup(dst)
    finally:
        dst.close()
    os.replace(tmp, target)
    old = sorted(f for f in os.listdir(directory) if f.startswith("orch-") and f.endswith(".db"))
    for f in old[:-keep] if keep > 0 else []:
        os.remove(os.path.join(directory, f))
    return target


@contextlib.contextmanager
def worker_lock(path):
    """Exclusive, non-blocking lock beside the database: a second worker exits
    instead of running the same steps twice. Released when the process dies."""
    fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LockedError("another worker holds %s.lock" % path)
        yield
    finally:
        os.close(fd)
