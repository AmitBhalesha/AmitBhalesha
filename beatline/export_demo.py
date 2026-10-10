"""Exports a self-contained static demo: one HTML page plus precomputed JSON.

Every number on the demo page comes from the same analytics code the server
runs; demo-shim.js answers the dashboard's API calls from the exported files.
The page has no sign-in (a "View as" switcher picks the role) and no street
map, since static hosts like Claude artifacts cannot load map tiles.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from . import analytics, db

STATIC = Path(__file__).parent / "static"
LEAFLET_CDN = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.js"


def _compact(obj) -> str:
    return json.dumps(obj, separators=(",", ":"))


def _shift_list(rows) -> list[dict]:
    return [{"name": r["shift_name"], "start": r["shift_start"], "end": r["shift_end"], "units": r["units"]} for r in rows]


def build_page() -> str:
    """index.html with stylesheets and scripts inlined, as a page fragment
    (no <html>/<head>/<body>), so it works both standalone and as an artifact."""
    html = (STATIC / "index.html").read_text()
    body = html[html.index("<body>") + len("<body>"): html.index("</body>")]
    body = re.sub(r"\s*<script src=\"/static/[^\"]+\"></script>", "", body)
    styles = (STATIC / "vendor/leaflet/leaflet.css").read_text() + "\n" + (STATIC / "styles.css").read_text()
    shim = (STATIC / "demo-shim.js").read_text()
    app = (STATIC / "app.js").read_text()
    return (
        '<meta charset="utf-8">\n'
        "<title>Beatline</title>\n"
        f"<style>\n{styles}\n</style>\n"
        f"{body.strip()}\n"
        f'<script src="{LEAFLET_CDN}"></script>\n'
        f"<script>\n{shim}\n</script>\n"
        f"<script>\n{app}\n</script>\n"
    )


def export_demo(conn: sqlite3.Connection, out_dir: str, agency_name: str, default_tz: str) -> dict:
    out = Path(out_dir)
    (out / "data").mkdir(parents=True, exist_ok=True)

    users = {
        r["username"]: {"username": r["username"], "display_name": r["display_name"], "role": r["role"],
                        "officer_id": r["officer_id"]}
        for r in conn.execute("SELECT * FROM users")
    }
    officer = next((u for u in users.values() if u["role"] == "officer"), None)
    officer_id = officer["officer_id"] if officer else None

    beats = list(db.load_beats(conn).values())
    lats = [p[1] for b in beats for p in b["ring"]]
    lons = [p[0] for b in beats for p in b["ring"]]
    shifts = _shift_list(conn.execute(
        "SELECT shift_name, shift_start, shift_end, COUNT(*) AS units FROM assignments "
        "GROUP BY shift_start, shift_end, shift_name ORDER BY shift_start DESC"
    ))
    officer_shifts = _shift_list(conn.execute(
        "SELECT shift_name, shift_start, shift_end, COUNT(*) AS units FROM assignments WHERE officer_id = ? "
        "GROUP BY shift_start, shift_end, shift_name ORDER BY shift_start DESC", (officer_id,)
    ))
    meta = {
        "config": {
            "agency_name": agency_name,
            "timezone": db.get_meta(conn, "timezone", default_tz),
            "demo": True,
            "bounds": [[min(lats), min(lons)], [max(lats), max(lons)]] if beats else None,
            "tile_url": None,
            "tile_url_dark": None,
            "tile_attribution": "",
            "thresholds": {
                "under_covered": analytics.UNDER_COVERED,
                "over_covered": analytics.OVER_COVERED,
                "long_gap_minutes": analytics.LONG_GAP_S // 60,
            },
        },
        "users": users,
        "beats": beats,
        "zones": db.load_zones(conn),
        "shifts": shifts,
        "officer_shifts": officer_shifts,
    }
    (out / "data" / "meta.json").write_text(_compact(meta))

    officer_starts = {s["start"] for s in officer_shifts}
    files = ["data/meta.json"]
    for s in shifts:
        start, end = s["start"], s["end"]
        trails = {
            oid: [[p[0], round(p[1], 5), round(p[2], 5)] for p in pts]
            for oid, pts in analytics.trails(conn, start, end).items()
        }
        doc = {
            "report": analytics.shift_report(conn, start, end),
            "trails": trails,
            "heat_patrol": analytics.heat_grid(conn, start, end, "patrol"),
            "heat_calls": analytics.heat_grid(conn, start, end, "calls"),
        }
        if officer_id and start in officer_starts:
            doc["officer_report"] = analytics.shift_report(conn, start, end, officer_filter=[officer_id])
            doc["officer_heat_patrol"] = analytics.heat_grid(conn, start, end, "patrol", officer_filter=[officer_id])
        name = f"data/shift-{start}.json"
        (out / name).write_text(_compact(doc))
        files.append(name)

    (out / "index.html").write_text(build_page())
    size = sum((out / f).stat().st_size for f in files + ["index.html"])
    return {"page": str(out / "index.html"), "files": files, "bytes": size}
