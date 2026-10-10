"""Append-only access log. Every view of officer location data is recorded so
the department can answer "who looked at whose location, and when"."""

from __future__ import annotations

import json
import sqlite3
import time


def record(
    conn: sqlite3.Connection,
    username: str | None,
    action: str,
    detail: dict | None = None,
    ip: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log (ts, username, action, detail, ip) VALUES (?, ?, ?, ?, ?)",
        (int(time.time()), username, action, json.dumps(detail or {}, sort_keys=True), ip),
    )
    conn.commit()


def recent(conn: sqlite3.Connection, limit: int = 200, username: str | None = None) -> list[dict]:
    sql = "SELECT * FROM audit_log"
    params: list = []
    if username:
        sql += " WHERE username = ?"
        params.append(username)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) | {"detail": json.loads(r["detail"] or "{}")} for r in rows]
