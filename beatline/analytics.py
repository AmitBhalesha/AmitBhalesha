"""Turns raw GPS breadcrumbs + CAD calls into answers a sergeant cares about:

* Where did each unit spend the shift, and how much of it was in their beat?
* Which beats went the longest with nobody in them?
* Does patrol time line up with where calls actually come from?
* Did directed-patrol hot spots get the minutes they were supposed to?
* Without GPS (public data only): which beats are busier than usual?

Presence is measured from consecutive pings: the time between two pings is
credited to the location of the first one. Gaps longer than MAX_GAP_S (radio
dead zone, device off) are not credited to anywhere.
"""

from __future__ import annotations

import bisect
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import db
from .geo import BeatIndex, Grid, haversine_m

MAX_GAP_S = 300
STATIONARY_MPS = 0.9            # ~2 mph; slower than this counts as stationary
ARRIVAL_RADIUS_M = 100          # unit within this distance of a call = on scene
ARRIVAL_SEARCH_S = 3600
DEMAND_LOOKBACK_S = 28 * 86400     # baseline from calls: the 28 days before the window
CRIME_LOOKBACK_S = 90 * 86400      # baseline from crime reports: the latest 90 days on file
BUSY_Z = 2.5                       # calls vs. baseline: how unusual before a beat is flagged
DEFAULT_SHIFTS = [["Days", 6], ["Evenings", 14], ["Nights", 22]]
UNDER_COVERED = 0.5             # coverage index thresholds
OVER_COVERED = 2.0
LONG_GAP_S = 2 * 3600


@dataclass
class Segment:
    officer_id: str
    start: int
    end: int
    lat: float
    lon: float
    meters: float
    contiguous: bool  # True if it directly follows the previous segment

    @property
    def seconds(self) -> int:
        return self.end - self.start


def load_pings(
    conn: sqlite3.Connection, start: int, end: int, officer_ids: list[str] | None = None
) -> dict[str, list[sqlite3.Row]]:
    sql = "SELECT officer_id, ts, lat, lon, speed_mph FROM pings WHERE ts >= ? AND ts <= ?"
    params: list = [start - MAX_GAP_S, end + MAX_GAP_S]
    if officer_ids is not None:
        if not officer_ids:
            return {}
        sql += f" AND officer_id IN ({','.join('?' * len(officer_ids))})"
        params += officer_ids
    sql += " ORDER BY officer_id, ts"
    out: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in conn.execute(sql, params):
        out[row["officer_id"]].append(row)
    return out


def iter_segments(officer_id: str, pings: list, start: int, end: int):
    prev_end = None
    for a, b in zip(pings, pings[1:]):
        dt = b["ts"] - a["ts"]
        if dt <= 0 or dt > MAX_GAP_S:
            prev_end = None
            continue
        s, e = max(a["ts"], start), min(b["ts"], end)
        if e <= s:
            continue
        meters = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) * (e - s) / dt
        yield Segment(officer_id, s, e, a["lat"], a["lon"], meters, contiguous=prev_end == s)
        prev_end = e


def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for s, e in sorted(intervals):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def longest_gap(intervals: list[tuple[int, int]], start: int, end: int) -> tuple[int, int, int]:
    """Longest stretch inside [start, end] not covered by any interval.
    Returns (seconds, gap_start, gap_end)."""
    best = (0, start, start)
    cursor = start
    for s, e in merge_intervals(intervals):
        if s > cursor and s - cursor > best[0]:
            best = (s - cursor, cursor, s)
        cursor = max(cursor, e)
    if end - cursor > best[0]:
        best = (end - cursor, cursor, end)
    return best


def assignments_in_window(conn: sqlite3.Connection, start: int, end: int) -> dict[str, dict]:
    """officer_id -> assignment row with the largest overlap with the window."""
    rows = conn.execute(
        "SELECT * FROM assignments WHERE shift_start < ? AND shift_end > ?", (end, start)
    ).fetchall()
    best: dict[str, tuple[int, dict]] = {}
    for r in rows:
        overlap = min(r["shift_end"], end) - max(r["shift_start"], start)
        if r["officer_id"] not in best or overlap > best[r["officer_id"]][0]:
            best[r["officer_id"]] = (overlap, dict(r))
    return {k: v[1] for k, v in best.items()}


def calls_in_window(
    conn: sqlite3.Connection, start: int, end: int, index: BeatIndex | None = None
) -> list[dict]:
    if index is None:
        index = BeatIndex({b["id"]: b["rings"] for b in db.load_beats(conn).values()})
    rows = conn.execute(
        "SELECT * FROM calls WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)
    ).fetchall()
    out = []
    for r in rows:
        call = dict(r)
        call["beat_id"] = index.locate(r["lat"], r["lon"])
        out.append(call)
    return out


def demand_points(conn: sqlite3.Connection, end: int) -> tuple[list[tuple], dict]:
    """Where demand usually comes from, as (beat_id, lat, lon, weight) rows plus a
    description of the source. Uses reported crimes when the database is set up
    for them (meta demand_source=crime) and has any; otherwise the 28 days of
    calls before `end`."""
    if db.get_meta(conn, "demand_source") == "crime":
        latest = conn.execute("SELECT MAX(ts) FROM crime_reports").fetchone()[0]
        if latest is not None:
            hi = min(end, latest + 1)
            lo = hi - CRIME_LOOKBACK_S
            rows = conn.execute(
                "SELECT beat_id, lat, lon, offense_count FROM crime_reports WHERE ts >= ? AND ts < ?", (lo, hi)
            ).fetchall()
            points = [(r["beat_id"], r["lat"], r["lon"], r["offense_count"]) for r in rows]
            return points, {"source": "crime", "start": lo, "end": hi, "count": sum(p[3] for p in points)}
    lo = end - DEMAND_LOOKBACK_S
    rows = conn.execute("SELECT lat, lon FROM calls WHERE ts >= ? AND ts < ?", (lo, end)).fetchall()
    return [(None, r["lat"], r["lon"], 1) for r in rows], {"source": "calls", "start": lo, "end": end, "count": len(rows)}


def call_status(calls: int, expected: float) -> tuple[float | None, str]:
    """Compare a beat's calls with what its baseline share predicts. Call counts
    are roughly Poisson, so (calls - expected) / sqrt(expected) is a z-score."""
    if expected <= 0:
        return None, "busier" if calls >= 3 else "typical"
    z = (calls - expected) / math.sqrt(expected)
    if z >= BUSY_Z:
        return round(z, 1), "busier"
    if z <= -BUSY_Z:
        return round(z, 1), "quieter"
    return round(z, 1), "typical"


def call_windows(conn: sqlite3.Connection, tz_name: str, now: int, limit: int = 60) -> list[dict]:
    """Shift-shaped windows when there are no rosters (public data only): one
    per shift from the first recorded call until now, newest first."""
    first = conn.execute("SELECT MIN(ts) FROM calls").fetchone()[0]
    if first is None:
        return []
    tz = ZoneInfo(tz_name)
    shifts = json.loads(db.get_meta(conn, "shift_starts") or "null") or DEFAULT_SHIFTS
    day = datetime.fromtimestamp(first, tz).date() - timedelta(days=1)
    last_day = datetime.fromtimestamp(now, tz).date() + timedelta(days=1)
    bounds = []
    while day <= last_day:
        bounds += [(name, int(datetime(day.year, day.month, day.day, hour, tzinfo=tz).timestamp()))
                   for name, hour in shifts]
        day += timedelta(days=1)
    windows = []
    for (name, start), (_, end) in zip(bounds, bounds[1:]):
        if start > now or end <= first:
            continue
        n = conn.execute("SELECT COUNT(*) FROM calls WHERE ts >= ? AND ts < ?", (start, end)).fetchone()[0]
        windows.append({"name": name, "start": start, "end": end, "units": 0, "calls": n,
                        "in_progress": start <= now < end})
    return windows[::-1][:limit]


def first_arrival(pings: list, call: dict) -> int | None:
    """Seconds from call creation until the unit's GPS first puts it on scene."""
    if not pings:
        return None
    times = [p["ts"] for p in pings]
    i = bisect.bisect_left(times, call["ts"])
    limit = call["ts"] + ARRIVAL_SEARCH_S
    for p in pings[i:]:
        if p["ts"] > limit:
            break
        if haversine_m(p["lat"], p["lon"], call["lat"], call["lon"]) <= ARRIVAL_RADIUS_M:
            return p["ts"] - call["ts"]
    return None


@dataclass
class _OfficerAcc:
    tracked_s: int = 0
    assigned_s: int = 0
    stationary_s: int = 0
    meters: float = 0.0
    beat_s: dict = field(default_factory=lambda: defaultdict(int))
    first_ts: int | None = None
    last_ts: int | None = None


def _coverage_status(patrol_s: int, index: float | None) -> str:
    if patrol_s == 0:
        return "uncovered"
    if index is None:
        return "low_demand"
    if index < UNDER_COVERED:
        return "under"
    if index > OVER_COVERED:
        return "over"
    return "balanced"


def shift_report(
    conn: sqlite3.Connection, start: int, end: int, officer_filter: list[str] | None = None
) -> dict:
    """Everything the dashboard needs for one time window.

    `officer_filter` limits per-officer rows (an officer viewing their own
    data). Beat and zone totals are always computed from all units so the
    coverage picture stays truthful, but they contain no per-officer detail.
    """
    beats = db.load_beats(conn)
    index = BeatIndex({b["id"]: b["rings"] for b in beats.values()})
    zones = db.load_zones(conn)
    officers = db.load_officers(conn)
    assignments = assignments_in_window(conn, start, end)
    pings = load_pings(conn, start, end)

    acc: dict[str, _OfficerAcc] = defaultdict(_OfficerAcc)
    beat_intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    beat_officers: dict[str, set] = defaultdict(set)
    zone_s: dict[str, int] = defaultdict(int)
    zone_visits: dict[str, int] = defaultdict(int)
    zone_officers: dict[str, set] = defaultdict(set)

    for officer_id, rows in pings.items():
        a = acc[officer_id]
        assigned_beat = assignments.get(officer_id, {}).get("beat_id")
        inside_zone: set[str] = set()
        for seg in iter_segments(officer_id, rows, start, end):
            if not seg.contiguous:
                inside_zone = set()
            a.tracked_s += seg.seconds
            a.meters += seg.meters
            a.first_ts = seg.start if a.first_ts is None else a.first_ts
            a.last_ts = seg.end
            if seg.meters / seg.seconds < STATIONARY_MPS:
                a.stationary_s += seg.seconds
            beat_id = index.locate(seg.lat, seg.lon)
            if beat_id:
                a.beat_s[beat_id] += seg.seconds
                beat_intervals[beat_id].append((seg.start, seg.end))
                beat_officers[beat_id].add(officer_id)
                if beat_id == assigned_beat:
                    a.assigned_s += seg.seconds
            now_inside = set()
            for z in zones:
                if haversine_m(seg.lat, seg.lon, z["lat"], z["lon"]) <= z["radius_m"]:
                    now_inside.add(z["id"])
                    zone_s[z["id"]] += seg.seconds
                    zone_officers[z["id"]].add(officer_id)
                    if z["id"] not in inside_zone:
                        zone_visits[z["id"]] += 1
            inside_zone = now_inside

    calls = calls_in_window(conn, start, end, index)
    calls_by_beat: dict[str, int] = defaultdict(int)
    calls_by_officer: dict[str, int] = defaultdict(int)
    response_by_priority: dict[int, list[int]] = defaultdict(list)
    for c in calls:
        if c["beat_id"]:
            calls_by_beat[c["beat_id"]] += 1
        if c["officer_id"]:
            calls_by_officer[c["officer_id"]] += 1
            arrival = first_arrival(pings.get(c["officer_id"], []), c)
            c["gps_response_s"] = arrival
            if arrival is not None:
                response_by_priority[c["priority"]].append(arrival)

    points, demand_info = demand_points(conn, end)
    demand_by_beat: dict[str, int] = defaultdict(int)
    for beat_id, lat, lon, weight in points:
        if beat_id not in beats:
            beat_id = index.locate(lat, lon) if lat is not None else None
        if beat_id:
            demand_by_beat[beat_id] += weight
    total_demand = sum(demand_by_beat.values())
    calls_in_beats = sum(calls_by_beat.values())
    total_patrol = sum(sum(e - s for s, e in merge_intervals(iv)) for iv in beat_intervals.values())
    total_patrol_unit_s = sum(sum(a.beat_s.values()) for a in acc.values())

    beat_rows = []
    for beat_id, beat in beats.items():
        unit_s = sum(a.beat_s.get(beat_id, 0) for a in acc.values())
        covered_s = sum(e - s for s, e in merge_intervals(beat_intervals[beat_id]))
        patrol_share = unit_s / total_patrol_unit_s if total_patrol_unit_s else 0.0
        demand_share = demand_by_beat[beat_id] / total_demand if total_demand else 0.0
        cov_index = round(patrol_share / demand_share, 2) if demand_share else None
        gap_s, gap_start, gap_end = longest_gap(beat_intervals[beat_id], start, end)
        expected = demand_share * calls_in_beats
        z, busy = call_status(calls_by_beat[beat_id], expected)
        beat_rows.append({
            "beat_id": beat_id,
            "name": beat["name"],
            "unit_minutes": round(unit_s / 60, 1),
            "covered_minutes": round(covered_s / 60, 1),
            "covered_pct": round(100 * covered_s / (end - start), 1),
            "units_present": len(beat_officers[beat_id]),
            "assigned_units": sorted(o for o, a in assignments.items() if a["beat_id"] == beat_id),
            "calls": calls_by_beat[beat_id],
            "expected_calls": round(expected, 1),
            "call_z": z,
            "call_status": busy,
            "demand_count": demand_by_beat[beat_id],
            "patrol_share": round(patrol_share, 3),
            "demand_share": round(demand_share, 3),
            "coverage_index": cov_index,
            "status": _coverage_status(unit_s, cov_index),
            "longest_gap_minutes": round(gap_s / 60, 1),
            "longest_gap_start": gap_start,
            "longest_gap_end": gap_end,
            "long_gap": gap_s >= LONG_GAP_S,
        })

    officer_ids = set(assignments) | {o for o, a in acc.items() if a.tracked_s > 0}
    if officer_filter is not None:
        officer_ids &= set(officer_filter)
    officer_rows = []
    for officer_id in sorted(officer_ids, key=lambda o: officers.get(o, {}).get("call_sign", o)):
        a = acc.get(officer_id, _OfficerAcc())
        info = officers.get(officer_id, {})
        asg = assignments.get(officer_id, {})
        assigned_beat = asg.get("beat_id")
        top = sorted(a.beat_s.items(), key=lambda kv: -kv[1])[:4]
        officer_rows.append({
            "officer_id": officer_id,
            "name": info.get("name", officer_id),
            "call_sign": info.get("call_sign", officer_id),
            "squad": info.get("squad"),
            "assigned_beat": assigned_beat,
            "tracked_minutes": round(a.tracked_s / 60, 1),
            "in_assigned_minutes": round(a.assigned_s / 60, 1),
            "pct_in_assigned": round(100 * a.assigned_s / a.tracked_s, 1) if assigned_beat and a.tracked_s else None,
            "miles": round(a.meters / 1609.344, 1),
            "stationary_minutes": round(a.stationary_s / 60, 1),
            "calls_handled": calls_by_officer[officer_id],
            "beat_minutes": [{"beat_id": b, "minutes": round(s / 60, 1)} for b, s in top],
            "first_ping": a.first_ts,
            "last_ping": a.last_ts,
        })

    zone_rows = []
    for z in zones:
        minutes = zone_s[z["id"]] / 60
        zone_rows.append({
            "zone_id": z["id"],
            "name": z["name"],
            "reason": z["reason"],
            "target_minutes": z["target_minutes"],
            "minutes": round(minutes, 1),
            "pct_of_target": round(100 * minutes / z["target_minutes"], 1) if z["target_minutes"] else None,
            "visits": zone_visits[z["id"]],
            "units": len(zone_officers[z["id"]]),
        })

    if officer_filter is not None:
        visible = set(officer_filter)
        calls = [c for c in calls if c["officer_id"] in visible]

    unmapped_rows = [dict(r) for r in conn.execute(
        "SELECT id, ts, call_type, address, last_seen FROM unmapped_calls WHERE ts >= ? AND ts < ? ORDER BY ts",
        (start, end),
    )]
    unmapped = len(unmapped_rows)
    return {
        "window": {"start": start, "end": end},
        # "calls_only" when there is no GPS for this window (public data): the
        # dashboard then compares calls with the baseline instead of patrol time.
        "mode": "avl" if pings or assignments else "calls_only",
        "demand": demand_info,
        "summary": {
            "units": len(officer_ids),
            "unit_hours": round(sum(a.tracked_s for o, a in acc.items() if o in officer_ids) / 3600, 1),
            "calls": len(calls),
            "unmapped_calls": unmapped,
            "beats_busier": sum(1 for b in beat_rows if b["call_status"] == "busier"),
            "beats_with_long_gap": sum(1 for b in beat_rows if b["long_gap"]),
            "beats_under_covered": sum(1 for b in beat_rows if b["status"] in ("under", "uncovered")),
            "median_gps_response_s": {
                str(p): int(statistics.median(v)) for p, v in sorted(response_by_priority.items())
            },
            "city_covered_pct": round(100 * total_patrol / ((end - start) * len(beats)), 1) if beats else 0,
        },
        "officers": officer_rows,
        "unmapped_calls": unmapped_rows if officer_filter is None else [],
        "beats": beat_rows,
        "zones": zone_rows,
        "calls": [
            {k: c.get(k) for k in ("id", "ts", "cleared_ts", "call_type", "priority", "lat", "lon", "officer_id",
                                   "beat_id", "address", "source")}
            | {"gps_response_s": c.get("gps_response_s")}
            for c in calls
        ],
    }


def trails(
    conn: sqlite3.Connection, start: int, end: int, officer_ids: list[str] | None = None
) -> dict[str, list[list]]:
    sql = "SELECT officer_id, ts, lat, lon, speed_mph FROM pings WHERE ts >= ? AND ts <= ?"
    params: list = [start, end]
    if officer_ids is not None:
        if not officer_ids:
            return {}
        sql += f" AND officer_id IN ({','.join('?' * len(officer_ids))})"
        params += officer_ids
    out: dict[str, list[list]] = defaultdict(list)
    for r in conn.execute(sql + " ORDER BY officer_id, ts", params):
        out[r["officer_id"]].append([r["ts"], round(r["lat"], 6), round(r["lon"], 6), r["speed_mph"]])
    return dict(out)


def heat_grid(
    conn: sqlite3.Connection,
    start: int,
    end: int,
    layer: str,
    cell_m: float = 200,
    officer_filter: list[str] | None = None,
) -> dict:
    """layer='patrol': unit-minutes per cell in [start, end].
    layer='calls': the demand baseline per cell (see demand_points)."""
    beats = db.load_beats(conn)
    ref_lat = next(iter(beats.values()))["ring"][0][1] if beats else 0.0
    grid = Grid(cell_m, ref_lat)
    values: dict[tuple[int, int], float] = defaultdict(float)
    demand_info = None
    if layer == "patrol":
        for officer_id, rows in load_pings(conn, start, end, officer_filter).items():
            for seg in iter_segments(officer_id, rows, start, end):
                values[grid.cell(seg.lat, seg.lon)] += seg.seconds / 60
    elif layer == "calls":
        points, demand_info = demand_points(conn, end)
        for _, lat, lon, weight in points:
            if lat is not None:
                values[grid.cell(lat, lon)] += weight
    else:
        raise ValueError(f"unknown layer {layer!r}")
    cells = [{"b": grid.bounds(c), "v": round(v, 1)} for c, v in values.items() if v > 0]
    return {"layer": layer, "cell_m": cell_m, "max": max((c["v"] for c in cells), default=0), "cells": cells,
            "demand": demand_info}
