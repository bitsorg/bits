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
]

SCHEMA_VERSION = len(MIGRATIONS)


class SchemaError(Exception):
    pass


class LockedError(Exception):
    """Another worker holds the database lock."""


def now_ms():
    return int(time.time() * 1000)


def connect(path, create=True):
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
    conn.execute("PRAGMA busy_timeout=10000")
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


def open_checked(path):
    """For the API: an existing database the worker has migrated."""
    try:
        conn = connect(path, create=False)
    except sqlite3.OperationalError as e:
        raise SchemaError("cannot open %s (%s): has the worker started?" % (path, e))
    try:
        check(conn)
    except Exception:
        conn.close()
        raise
    return conn


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
