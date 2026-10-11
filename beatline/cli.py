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

from . import audit, auth, db, importers
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
    property and an optional `name`."""
    with open(path) as f:
        return importers.import_beats_geojson(conn, json.load(f))


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
    hou = sub.add_parser("houston", help="Houston, TX public data: beats, live calls, crime baseline")
    hsub = hou.add_subparsers(dest="houston_cmd", required=True)
    p = hsub.add_parser("setup", help="load real HPD beats from the city GIS and configure for Houston")
    p.add_argument("--layer", type=int, help="GIS layer id (default: found by name, else 5)")
    p = hsub.add_parser("poll", help="record live police calls from HEC Active Incidents")
    p.add_argument("--every", type=int, default=300, help="seconds between polls (min 60, default 300)")
    p.add_argument("--once", action="store_true", help="poll once and exit (for cron)")
    p = hsub.add_parser("import-crime", help="load an HPD monthly crime spreadsheet (.xlsx/.csv) as the baseline")
    p.add_argument("path")
    hsub.add_parser("check", help="test every live source once and print what was found")
    p = sub.add_parser("export-demo", help="write a static demo page + precomputed data to a folder")
    p.add_argument("out_dir")
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
    elif args.cmd == "houston":
        from . import houston
        if args.houston_cmd == "setup":
            result = houston.setup(conn, layer=args.layer)
            print(f"Loaded {result['beats']} HPD beats. Beat layer fields: {', '.join(result['fields'])}")
            print("Next: `python -m beatline houston poll` (leave running), then import a monthly crime file.")
        elif args.houston_cmd == "poll":
            if args.once:
                print(json.dumps(houston.poll_once(conn)))
            else:
                print(f"Polling Active Incidents every {max(args.every, 60)}s. Ctrl+C to stop.", flush=True)
                houston.poll_forever(conn, args.every)
        elif args.houston_cmd == "import-crime":
            stats = houston.import_crime(conn, args.path)
            print(f"Imported {stats['imported']:,} of {stats['rows']:,} rows ({stats['first']} to {stats['last']}); "
                  f"{stats['beats_matched']:,} matched a beat, {stats['with_coordinates']:,} had coordinates, "
                  f"{stats['skipped']:,} skipped")
        elif args.houston_cmd == "check":
            houston.check(conn)
    elif args.cmd == "export-demo":
        from .export_demo import export_demo
        result = export_demo(conn, args.out_dir, settings.agency_name, settings.timezone)
        print(f"Wrote {result['page']} and {len(result['files'])} data files ({result['bytes'] / 1e6:.1f} MB)")
    elif args.cmd == "serve":
        import os

        import uvicorn

        os.environ["BEATLINE_DB"] = args.db
        conn.close()
        from .api import create_app
        uvicorn.run(create_app(Settings.from_env()), host=args.host, port=args.port)
    return 0
