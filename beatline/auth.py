"""Users, roles and sessions.

Roles:
  sergeant - sees every unit's location history and the coverage picture
  officer  - sees only their own location history (plus city-wide aggregates)
  auditor  - sees the audit log, nothing operational
  admin    - everything, plus data ingestion
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time

ROLES = ("sergeant", "officer", "auditor", "admin")
SESSION_TTL_S = 12 * 3600
PBKDF2_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations))
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_user(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    role: str,
    display_name: str | None = None,
    officer_id: str | None = None,
) -> None:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    if role == "officer" and not officer_id:
        raise ValueError("officer accounts must be linked to an officer_id")
    if len(password) < 8:
        raise ValueError("password must be at least 8 characters")
    conn.execute(
        "INSERT INTO users (username, display_name, role, officer_id, password_hash) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(username) DO UPDATE SET display_name = excluded.display_name, role = excluded.role, "
        "officer_id = excluded.officer_id, password_hash = excluded.password_hash",
        (username, display_name or username, role, officer_id, hash_password(password)),
    )
    conn.commit()


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> dict | None:
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        # Burn comparable time so response timing doesn't reveal valid usernames.
        hashlib.pbkdf2_hmac("sha256", password.encode(), b"0" * 16, PBKDF2_ITERATIONS)
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return public_user(row)


def public_user(row: sqlite3.Row) -> dict:
    return {
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
        "officer_id": row["officer_id"],
    }


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, username: str) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    conn.execute("DELETE FROM sessions WHERE expires < ?", (now,))
    conn.execute(
        "INSERT INTO sessions (token_hash, username, expires) VALUES (?, ?, ?)",
        (_token_hash(token), username, now + SESSION_TTL_S),
    )
    conn.commit()
    return token


def user_for_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.username = s.username "
        "WHERE s.token_hash = ? AND s.expires > ?",
        (_token_hash(token), int(time.time())),
    ).fetchone()
    return public_user(row) if row else None


def end_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
    conn.commit()
