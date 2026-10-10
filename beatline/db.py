"""SQLite storage. One file, no server - swap for Postgres/PostGIS when an
agency outgrows it; the queries here are deliberately plain SQL."""

from __future__ import annotations

import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS beats (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    polygon TEXT NOT NULL              -- JSON ring [[lon, lat], ...]
);

CREATE TABLE IF NOT EXISTS officers (
    id        TEXT PRIMARY KEY,        -- badge / employee number
    name      TEXT NOT NULL,
    call_sign TEXT NOT NULL,
    squad     TEXT
);

-- Who worked which shift, and which beat they were assigned (NULL = floater).
CREATE TABLE IF NOT EXISTS assignments (
    officer_id  TEXT NOT NULL REFERENCES officers(id),
    shift_start INTEGER NOT NULL,      -- unix seconds, UTC
    shift_end   INTEGER NOT NULL,
    shift_name  TEXT NOT NULL,
    beat_id     TEXT REFERENCES beats(id),
    PRIMARY KEY (officer_id, shift_start)
);
CREATE INDEX IF NOT EXISTS assignments_shift ON assignments(shift_start, shift_end);

-- AVL / GPS breadcrumbs.
CREATE TABLE IF NOT EXISTS pings (
    officer_id TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    speed_mph  REAL,
    PRIMARY KEY (officer_id, ts)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS pings_ts ON pings(ts);

-- CAD calls for service.
CREATE TABLE IF NOT EXISTS calls (
    id         TEXT PRIMARY KEY,
    ts         INTEGER NOT NULL,
    cleared_ts INTEGER,
    call_type  TEXT NOT NULL,
    priority   INTEGER NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    officer_id TEXT
);
CREATE INDEX IF NOT EXISTS calls_ts ON calls(ts);

-- Directed-patrol hot spots with a per-shift presence target.
CREATE TABLE IF NOT EXISTS patrol_zones (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    lat            REAL NOT NULL,
    lon            REAL NOT NULL,
    radius_m       REAL NOT NULL,
    target_minutes REAL NOT NULL,
    reason         TEXT
);

CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('sergeant', 'officer', 'auditor', 'admin')),
    officer_id    TEXT,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    username   TEXT NOT NULL REFERENCES users(username),
    expires    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Append-only record of who looked at what.
CREATE TABLE IF NOT EXISTS audit_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       INTEGER NOT NULL,
    username TEXT,
    action   TEXT NOT NULL,
    detail   TEXT,
    ip       TEXT
);
CREATE INDEX IF NOT EXISTS audit_ts ON audit_log(ts);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def load_beats(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT id, name, polygon FROM beats ORDER BY id").fetchall()
    return {r["id"]: {"id": r["id"], "name": r["name"], "ring": json.loads(r["polygon"])} for r in rows}


def load_zones(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM patrol_zones ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def load_officers(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM officers ORDER BY id").fetchall()
    return {r["id"]: dict(r) for r in rows}


def upsert_beat(conn: sqlite3.Connection, beat_id: str, name: str, ring: list[list[float]]) -> None:
    conn.execute(
        "INSERT INTO beats (id, name, polygon) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name, polygon = excluded.polygon",
        (beat_id, name, json.dumps(ring)),
    )


def insert_pings(conn: sqlite3.Connection, pings: list[tuple]) -> int:
    """pings: (officer_id, ts, lat, lon, speed_mph). Duplicates are ignored."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR IGNORE INTO pings (officer_id, ts, lat, lon, speed_mph) VALUES (?, ?, ?, ?, ?)",
        pings,
    )
    return conn.total_changes - before


def insert_calls(conn: sqlite3.Connection, calls: list[tuple]) -> int:
    """calls: (id, ts, cleared_ts, call_type, priority, lat, lon, officer_id)."""
    before = conn.total_changes
    conn.executemany(
        "INSERT OR REPLACE INTO calls "
        "(id, ts, cleared_ts, call_type, priority, lat, lon, officer_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        calls,
    )
    return conn.total_changes - before


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
