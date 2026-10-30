"""SQLite persistence. One file on disk, no external database server.

Every thread gets its own connection; WAL mode lets the web server read while a job runner writes.
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    kind         TEXT NOT NULL,
    environment  TEXT NOT NULL DEFAULT 'sandbox',
    config       TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    action         TEXT NOT NULL,
    connection_id  TEXT NOT NULL REFERENCES connections(id),
    status         TEXT NOT NULL,
    options        TEXT NOT NULL DEFAULT '{}',
    source_file    TEXT,
    parent_job_id  TEXT,
    approved_hash  TEXT,
    approved_at    TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    started_at     TEXT,
    finished_at    TEXT
);

CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    row_no      INTEGER NOT NULL,
    original    TEXT NOT NULL,
    input       TEXT NOT NULL,
    revision    INTEGER NOT NULL DEFAULT 0,
    included    INTEGER NOT NULL DEFAULT 1,
    op          TEXT,
    target      TEXT,
    payload     TEXT,
    messages    TEXT NOT NULL DEFAULT '[]',
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    remote_id   TEXT,
    result      TEXT,
    error       TEXT,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS items_job_status ON items(job_id, status, row_no);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id    TEXT,
    item_id   INTEGER,
    ts        TEXT NOT NULL,
    level     TEXT NOT NULL,
    kind      TEXT NOT NULL,
    message   TEXT NOT NULL,
    data      TEXT
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, id);

CREATE TABLE IF NOT EXISTS reports (
    id             TEXT PRIMARY KEY,
    connection_id  TEXT NOT NULL,
    report         TEXT NOT NULL,
    params         TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL,
    scanned        INTEGER NOT NULL DEFAULT 0,
    rows           TEXT,
    error          TEXT,
    created_at     TEXT NOT NULL,
    finished_at    TEXT
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        conn = self.conn()
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"{self.path} was created by a newer version of the toolkit")
        conn.executescript(SCHEMA)  # idempotent; runs in its own transaction
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 10000")
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self):
        """A write transaction. BEGIN IMMEDIATE takes the write lock up front, avoiding upgrade deadlocks."""
        conn = self.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def query(self, sql, params=()):
        return self.conn().execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        return self.conn().execute(sql, params).fetchone()
