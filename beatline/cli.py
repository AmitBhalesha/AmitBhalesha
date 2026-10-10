"""Command line: python -m beatline <command>"""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from . import audit, auth, db
from .config import Settings


def parse_ts(value: str, tz: ZoneInfo) -> int:
    value = value.strip()
    if value.isdigit():
        return int(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return int(dt.timestamp())


def _float_or_none(value: str | None) -> float | None:
    return float(value) if value not in (None, "") else None


def import_pings(conn, path: str, tz: ZoneInfo) -> int:
    """CSV columns: officer_id, timestamp, lat, lon[, speed_mph]."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            rows.append((r["officer_id"].strip(), parse_ts(r["timestamp"], tz), float(r["lat"]),
                         float(r["lon"]), _float_or_none(r.get("speed_mph"))))
    for officer_id in {r[0] for r in rows}:
        conn.execute(
            "INSERT OR IGNORE INTO officers (id, name, call_sign) VALUES (?, ?, ?)", (officer_id, officer_id, officer_id)
        )
    n = db.insert_pings(conn, rows)
    conn.commit()
    return n


def import_calls(conn, path: str, tz: ZoneInfo) -> int:
    """CSV columns: id, timestamp, call_type, priority, lat, lon[, officer_id, cleared_timestamp]."""
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            cleared = r.get("cleared_timestamp")
            rows.append((r["id"].strip(), parse_ts(r["timestamp"], tz), parse_ts(cleared, tz) if cleared else None,
                         r["call_type"].strip(), int(r["priority"]), float(r["lat"]), float(r["lon"]),
                         (r.get("officer_id") or "").strip() or None))
    n = db.insert_calls(conn, rows)
    conn.commit()
    return n


def import_beats(conn, path: str) -> int:
    """GeoJSON FeatureCollection of Polygon/MultiPolygon features with an `id` or `beat`
    property and an optional `name`. MultiPolygons keep their largest ring."""
    with open(path) as f:
        fc = json.load(f)
    count = 0
    for feat in fc["features"]:
        props = feat.get("properties") or {}
        beat_id = str(props.get("id") or props.get("beat") or feat.get("id"))
        geom = feat["geometry"]
        if geom["type"] == "Polygon":
            ring = geom["coordinates"][0]
        elif geom["type"] == "MultiPolygon":
            ring = max((poly[0] for poly in geom["coordinates"]), key=len)
        else:
            continue
        db.upsert_beat(conn, beat_id, str(props.get("name") or f"Beat {beat_id}"), [[p[0], p[1]] for p in ring])
        count += 1
    conn.commit()
    return count


def purge(conn, days: int) -> int:
    cutoff = int(time.time()) - days * 86400
    n = conn.execute("DELETE FROM pings WHERE ts < ?", (cutoff,)).rowcount
    conn.execute("DELETE FROM sessions WHERE expires < ?", (int(time.time()),))
    conn.commit()
    audit.record(conn, "system", "purge_pings", {"older_than_days": days, "deleted": n})
    return n


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(prog="beatline", description="Patrol coverage for sergeants")
    parser.add_argument("--db", default=settings.db_path, help="SQLite file (env BEATLINE_DB)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db", help="create tables")
    p = sub.add_parser("seed-demo", help="replace data with a synthetic demo city")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("create-user", help="add or update a user (prompts for password)")
    p.add_argument("username")
    p.add_argument("--role", required=True, choices=auth.ROLES)
    p.add_argument("--name")
    p.add_argument("--officer-id")
    for name, help_text in (("import-pings", "AVL/GPS CSV"), ("import-calls", "CAD calls CSV"),
                            ("import-beats", "beat boundaries GeoJSON")):
        p = sub.add_parser(name, help=f"load {help_text}")
        p.add_argument("path")
    p = sub.add_parser("purge", help="delete location history older than the retention period")
    p.add_argument("--days", type=int, default=settings.retention_days)
    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    conn = db.connect(args.db)
    db.init_db(conn)
    tz = ZoneInfo(db.get_meta(conn, "timezone", settings.timezone))

    if args.cmd == "init-db":
        print(f"Initialized {args.db}")
    elif args.cmd == "seed-demo":
        from .simulate import seed_demo
        stats = seed_demo(conn, days=args.days, seed=args.seed, tz_name=settings.timezone)
        print(f"Seeded {args.db}: {stats['shifts']} shifts, {stats['officers']} officers, "
              f"{stats['pings']:,} GPS pings, {stats['calls']:,} calls")
        print(f"Demo logins ({', '.join(stats['users'])}), password: {stats['password']}")
    elif args.cmd == "create-user":
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat: "):
            print("Passwords do not match", file=sys.stderr)
            return 1
        auth.create_user(conn, args.username, password, args.role, args.name, args.officer_id)
        audit.record(conn, "system", "create_user", {"username": args.username, "role": args.role})
        print(f"Saved user {args.username} ({args.role})")
    elif args.cmd == "import-pings":
        print(f"Imported {import_pings(conn, args.path, tz):,} pings")
    elif args.cmd == "import-calls":
        print(f"Imported {import_calls(conn, args.path, tz):,} calls")
    elif args.cmd == "import-beats":
        print(f"Imported {import_beats(conn, args.path)} beats")
    elif args.cmd == "purge":
        print(f"Deleted {purge(conn, args.days):,} pings older than {args.days} days")
    elif args.cmd == "serve":
        import os

        import uvicorn

        os.environ["BEATLINE_DB"] = args.db
        conn.close()
        from .api import create_app
        uvicorn.run(create_app(Settings.from_env()), host=args.host, port=args.port)
    return 0
