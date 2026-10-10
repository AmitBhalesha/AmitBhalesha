"""Synthetic demo data: a 3x3-beat city, three squads, CAD calls clustered
around hot spots, and units that patrol, answer calls, take breaks and drift.

Nothing here describes a real agency or real people. Night shift is
deliberately short-staffed so coverage gaps show up in the dashboard.
"""

from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import auth, db
from .geo import M_PER_DEG_LAT, point_in_polygon

CENTER = (40.7608, -111.8910)
HALF_M = 1800
STATION = (-300.0, 200.0)
STEP_S = 10
PING_S = 30
GPS_NOISE_M = 4.0
DEMO_PASSWORD = "beatline-demo"

SHIFTS = [("Days", 6), ("Swings", 14), ("Nights", 22)]
SQUADS = {"Days": ("1", 10), "Swings": ("2", 11), "Nights": ("3", 7)}
STAFFING = {
    "Days": ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9"],
    "Swings": ["B1", "B2", "B3", "B4", "B5", "B6", "B7", "B8", "B9", None],
    "Nights": ["B1", "B2", "B3", "B5", "B6", "B8"],
}
CALLS_PER_HOUR = {"Days": 3.0, "Swings": 4.2, "Nights": 2.6}

# (id, name, x, y, sigma_m, weight by shift, directed-patrol reason, target minutes per shift)
HOTSPOTS = [
    ("Z1", "Main St bar district", 150, 100, 150, {"Days": 0.05, "Swings": 0.25, "Nights": 0.35},
     "Late-night assaults and disturbances", 120),
    ("Z2", "Transit center", -1100, -700, 120, {"Days": 0.15, "Swings": 0.15, "Nights": 0.10},
     "Robberies and narcotics complaints", 90),
    ("Z3", "Retail plaza lot", 1250, -1250, 140, {"Days": 0.25, "Swings": 0.15, "Nights": 0.05},
     "Vehicle break-ins and shoplifting", 90),
]
ZONE_RADIUS_M = 150
# Seconds a call waits in the CAD queue before a unit is sent, by priority.
DISPATCH_DELAY_S = {1: (30, 150), 2: (90, 480), 3: (180, 1200)}

# (type, priority, weight, on-scene minutes range)
CALL_TYPES = [
    ("Disturbance", 2, 14, (15, 40)),
    ("Suspicious Person", 3, 12, (10, 25)),
    ("Traffic Hazard", 3, 8, (10, 20)),
    ("Theft", 3, 12, (20, 45)),
    ("Alarm", 3, 10, (8, 20)),
    ("Welfare Check", 2, 9, (15, 35)),
    ("Domestic", 1, 7, (30, 60)),
    ("Assault", 1, 5, (30, 70)),
    ("Burglary", 2, 6, (30, 60)),
    ("Vehicle Collision", 2, 8, (20, 50)),
    ("Noise Complaint", 3, 9, (8, 15)),
]

FIRST = ["Alex", "Jordan", "Casey", "Riley", "Morgan", "Taylor", "Jamie", "Avery", "Quinn", "Drew",
         "Reese", "Rowan", "Skyler", "Parker", "Emerson", "Hayden", "Kendall", "Logan", "Peyton",
         "Sawyer", "Blake", "Cameron", "Dakota", "Elliot", "Finley", "Harper", "Jesse", "Kai"]
LAST = ["Alvarez", "Brennan", "Castillo", "Delgado", "Ellison", "Fairbanks", "Garza", "Holloway",
        "Ibarra", "Jennings", "Kowalski", "Lindqvist", "Mercado", "Nakamura", "Okafor", "Pruitt",
        "Quintero", "Rasmussen", "Sorensen", "Thibodeaux", "Underwood", "Valdez", "Whitfield",
        "Yazzie", "Zielinski", "Abernathy", "Bustamante", "Calloway"]


def to_latlon(x: float, y: float) -> tuple[float, float]:
    lat0, lon0 = CENTER
    return lat0 + y / M_PER_DEG_LAT, lon0 + x / (M_PER_DEG_LAT * math.cos(math.radians(lat0)))


@dataclass
class Beat:
    id: str
    ring: list[tuple[float, float]]  # local meters

    def contains(self, x: float, y: float) -> bool:
        return point_in_polygon(x, y, [list(p) for p in self.ring])

    def random_point(self, rng: random.Random) -> tuple[float, float]:
        xs, ys = [p[0] for p in self.ring], [p[1] for p in self.ring]
        while True:
            x, y = rng.uniform(min(xs), max(xs)), rng.uniform(min(ys), max(ys))
            if self.contains(x, y):
                return x, y

    @property
    def centroid(self) -> tuple[float, float]:
        return sum(p[0] for p in self.ring) / 4, sum(p[1] for p in self.ring) / 4


def make_beats(rng: random.Random) -> list[Beat]:
    step = 2 * HALF_M / 3
    v = [[[-HALF_M + c * step, -HALF_M + r * step] for c in range(4)] for r in range(4)]
    for r in (1, 2):
        for c in (1, 2):
            v[r][c][0] += rng.uniform(-0.15, 0.15) * step
            v[r][c][1] += rng.uniform(-0.15, 0.15) * step
    beats = []
    for r in range(3):          # south -> north
        for c in range(3):      # west -> east
            number = (2 - r) * 3 + c + 1   # Beat 1 is the north-west corner
            ring = [tuple(v[r][c]), tuple(v[r][c + 1]), tuple(v[r + 1][c + 1]), tuple(v[r + 1][c])]
            beats.append(Beat(f"B{number}", ring))
    return sorted(beats, key=lambda b: int(b.id[1:]))


def shift_windows(days: int, tz: ZoneInfo, now: datetime) -> list[tuple[str, int, int]]:
    """The last `days`*3 completed shifts as (name, start_ts, end_ts)."""
    local_now = now.astimezone(tz)
    boundaries = []
    for d in range(days + 2, -2, -1):
        date = (local_now - timedelta(days=d)).date()
        for name, hour in SHIFTS:
            boundaries.append((name, datetime(date.year, date.month, date.day, hour, tzinfo=tz)))
    boundaries.sort(key=lambda b: b[1])
    windows = [
        (name, int(start.timestamp()), int(nxt.timestamp()))
        for (name, start), (_, nxt) in zip(boundaries, boundaries[1:])
        if nxt <= local_now
    ]
    return windows[-days * 3:]


@dataclass
class Call:
    id: str
    ts: int
    call_type: str
    priority: int
    x: float
    y: float
    onscene_s: int
    dispatch_ts: int = 0
    officer_id: str | None = None
    cleared_ts: int | None = None


def generate_calls(rng, beats, shift_name, start, end, seq_start) -> list[Call]:
    rate = CALLS_PER_HOUR[shift_name]
    n = int(rng.gauss(rate * (end - start) / 3600, 2))
    types_w = [t[2] for t in CALL_TYPES]
    beat_w = {"B5": 2.2, "B4": 1.3, "B6": 1.3, "B8": 1.2}
    calls = []
    for i in range(max(n, 0)):
        roll = rng.random()
        acc = 0.0
        for _, _, hx, hy, sigma, weights, _, _ in HOTSPOTS:
            acc += weights[shift_name]
            if roll < acc:
                x, y = rng.gauss(hx, sigma), rng.gauss(hy, sigma)
                break
        else:
            beat = rng.choices(beats, weights=[beat_w.get(b.id, 1.0) for b in beats])[0]
            x, y = beat.random_point(rng)
        x, y = max(-HALF_M + 20, min(HALF_M - 20, x)), max(-HALF_M + 20, min(HALF_M - 20, y))
        ctype, prio, _, (lo, hi) = rng.choices(CALL_TYPES, weights=types_w)[0]
        ts = rng.randint(start, end - 1)
        delay = rng.randint(*DISPATCH_DELAY_S[prio])
        calls.append(Call("", ts, ctype, prio, x, y, rng.randint(lo, hi) * 60, ts + delay))
    calls.sort(key=lambda c: c.dispatch_ts)
    for i, c in enumerate(sorted(calls, key=lambda c: c.ts)):
        c.id = f"C{seq_start + i:06d}"
    return calls


@dataclass
class Unit:
    officer_id: str
    beat: Beat | None
    x: float
    y: float
    mode: str = "patrol"
    legs: list = field(default_factory=list)
    speed: float = 0.0
    timer: int = 0
    call: Call | None = None
    break_at: int = 0
    took_break: bool = False
    dark_from: int = -1
    dark_until: int = -1
    linger_on_arrival: int = 0

    @property
    def available(self) -> bool:
        return self.mode in ("patrol", "linger")

    def route_to(self, rng, tx, ty, speed):
        corner = (tx, self.y) if rng.random() < 0.5 else (self.x, ty)
        self.legs = [corner, (tx, ty)]
        self.speed = speed

    def advance(self, dt):
        dist = self.speed * dt
        while dist > 0 and self.legs:
            tx, ty = self.legs[0]
            d = math.hypot(tx - self.x, ty - self.y)
            if d <= dist:
                self.x, self.y = tx, ty
                dist -= d
                self.legs.pop(0)
            else:
                self.x += (tx - self.x) / d * dist
                self.y += (ty - self.y) / d * dist
                dist = 0
        return not self.legs


def _neighbors(beat: Beat, beats: list[Beat]) -> list[Beat]:
    n = int(beat.id[1:]) - 1
    r, c = divmod(n, 3)
    out = []
    for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        if 0 <= r + dr < 3 and 0 <= c + dc < 3:
            out.append(beats[(r + dr) * 3 + (c + dc)])
    return out


def _zones_for(beat: Beat | None, zones: list[tuple]) -> list[tuple]:
    if beat is None:
        return zones
    return [z for z in zones if beat.contains(z[2], z[3])]


def _new_patrol_target(rng, unit: Unit, beats, zones):
    """Returns (x, y, linger_s). Mostly in-beat; sometimes a hot spot or a neighbor beat."""
    own_zones = _zones_for(unit.beat, zones)
    if own_zones and rng.random() < (0.35 if unit.beat is None else 0.15):
        z = rng.choice(own_zones)
        ang, r = rng.uniform(0, 2 * math.pi), rng.uniform(0, ZONE_RADIUS_M * 0.6)
        return z[2] + r * math.cos(ang), z[3] + r * math.sin(ang), rng.randint(4, 12) * 60
    if unit.beat is None:
        x, y = rng.uniform(-HALF_M, HALF_M), rng.uniform(-HALF_M, HALF_M)
    elif rng.random() < 0.12:
        x, y = rng.choice(_neighbors(unit.beat, beats)).random_point(rng)
    else:
        x, y = unit.beat.random_point(rng)
    linger = rng.randint(1, 6) * 60 if rng.random() < 0.3 else 0
    return x, y, linger


def _patrol_leg(rng, unit: Unit, beats, zones):
    x, y, linger = _new_patrol_target(rng, unit, beats, zones)
    unit.mode = "patrol"
    unit.route_to(rng, x, y, rng.uniform(8, 13))
    unit.linger_on_arrival = linger


def simulate_shift(rng, beats, zones, units: list[Unit], calls: list[Call], start: int, end: int):
    pings = []
    pending: list[Call] = []
    queue = list(calls)
    beat_by_id = {b.id: b for b in beats}
    for u in units:
        u.break_at = start + rng.randint(3 * 3600, 5 * 3600)
        if rng.random() < 0.15:
            u.dark_from = start + rng.randint(3600, 6 * 3600)
            u.dark_until = u.dark_from + rng.randint(6, 10) * 60
        _patrol_leg(rng, u, beats, zones)

    for t in range(start, end, STEP_S):
        while queue and queue[0].dispatch_ts <= t:
            pending.append(queue.pop(0))
        for call in list(pending):
            free = [u for u in units if u.available]
            if not free:
                break
            call_beat = next((b for b in beats if b.contains(call.x, call.y)), None)
            own = [u for u in free if call_beat and u.beat and u.beat.id == call_beat.id]
            unit = own[0] if own else min(free, key=lambda u: math.hypot(u.x - call.x, u.y - call.y))
            unit.mode, unit.call = "respond", call
            unit.route_to(rng, call.x, call.y, rng.uniform(12, 15) if call.priority == 1 else rng.uniform(8, 12))
            call.officer_id = unit.officer_id
            pending.remove(call)

        for u in units:
            if u.mode == "patrol":
                if t >= end - 12 * 60:
                    u.mode = "rtb"
                    u.route_to(rng, *STATION, rng.uniform(9, 12))
                elif not u.took_break and t >= u.break_at:
                    u.mode, u.timer, u.took_break, u.legs, u.speed = "break", t + 30 * 60, True, [], 0
                elif u.advance(STEP_S):
                    if u.linger_on_arrival:
                        u.mode, u.timer, u.speed = "linger", t + u.linger_on_arrival, 0
                        u.linger_on_arrival = 0
                    else:
                        _patrol_leg(rng, u, beats, zones)
            elif u.mode == "linger" and t >= u.timer:
                _patrol_leg(rng, u, beats, zones)
            elif u.mode == "respond" and u.advance(STEP_S):
                u.mode, u.timer, u.speed = "onscene", t + u.call.onscene_s, 0
            elif u.mode == "onscene" and t >= u.timer:
                u.call.cleared_ts = t
                u.call = None
                _patrol_leg(rng, u, beats, zones)
            elif u.mode == "break" and t >= u.timer:
                _patrol_leg(rng, u, beats, zones)
            elif u.mode == "rtb":
                if u.advance(STEP_S):
                    u.speed = 0

            if (t - start) % PING_S == 0 and not (u.dark_from <= t < u.dark_until) and rng.random() > 0.01:
                lat, lon = to_latlon(u.x + rng.gauss(0, GPS_NOISE_M), u.y + rng.gauss(0, GPS_NOISE_M))
                mph = max(0.0, u.speed * 2.23694 + rng.gauss(0, 1.5)) if u.speed else 0.0
                pings.append((u.officer_id, t, round(lat, 6), round(lon, 6), round(mph, 1)))

    for call in pending + queue:  # never dispatched before shift end: next shift's problem
        call.officer_id = None
    for call in calls:
        if call.officer_id and call.cleared_ts is None:
            call.cleared_ts = min(call.ts + call.onscene_s + 600, end + 1800)
    return pings


def seed_demo(conn: sqlite3.Connection, days: int = 7, seed: int = 7, now: datetime | None = None,
              tz_name: str = "America/Denver", history_days: int = 28) -> dict:
    """Wipe operational tables and fill them with synthetic data.
    The audit log is append-only and is kept."""
    rng = random.Random(seed)
    tz = ZoneInfo(tz_name)
    now = now or datetime.now(tz)
    db.init_db(conn)
    for table in ("sessions", "users", "pings", "calls", "assignments", "officers", "patrol_zones", "beats"):
        conn.execute(f"DELETE FROM {table}")

    beats = make_beats(rng)
    for b in beats:
        ring = [list(reversed(to_latlon(x, y))) for x, y in b.ring]
        db.upsert_beat(conn, b.id, f"Beat {b.id[1:]}", [[round(lon, 6), round(lat, 6)] for lon, lat in ring])

    zones = [(zid, name, x, y, reason) for zid, name, x, y, _, _, reason, _ in HOTSPOTS]
    for zid, name, x, y, _, _, reason, target in HOTSPOTS:
        lat, lon = to_latlon(x, y)
        conn.execute(
            "INSERT INTO patrol_zones (id, name, lat, lon, radius_m, target_minutes, reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (zid, name, round(lat, 6), round(lon, 6), ZONE_RADIUS_M, target, reason),
        )

    names = set()
    rosters: dict[str, list[str]] = {}
    for shift_name, (digit, size) in SQUADS.items():
        rosters[shift_name] = []
        for i in range(1, size + 1):
            while True:
                name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
                if name not in names:
                    names.add(name)
                    break
            officer_id = f"{digit}{i:03d}"
            call_sign = f"{digit}A{i:02d}"
            conn.execute(
                "INSERT INTO officers (id, name, call_sign, squad) VALUES (?, ?, ?, ?)",
                (officer_id, name, call_sign, shift_name),
            )
            rosters[shift_name].append(officer_id)

    windows = shift_windows(days, tz, now)
    first_tracked = windows[0][1]
    history = shift_windows(history_days, tz, now)
    history = [w for w in history if w[2] <= first_tracked]

    seq = 1
    all_calls: list[Call] = []
    for shift_name, start, end in history:
        calls = generate_calls(rng, beats, shift_name, start, end, seq)
        seq += len(calls)
        for c in calls:
            c.officer_id = rng.choice(rosters[shift_name])
            c.cleared_ts = c.ts + c.onscene_s + rng.randint(5, 20) * 60
        all_calls += calls

    total_pings = 0
    for shift_name, start, end in windows:
        posts = list(STAFFING[shift_name])
        if rng.random() < 0.3:  # someone called out sick; their beat goes unfilled
            posts.remove(rng.choice([p for p in posts if p]))
        roster = rosters[shift_name][:]
        rng.shuffle(roster)
        units = []
        for officer_id, beat_id in zip(roster, posts):
            conn.execute(
                "INSERT INTO assignments (officer_id, shift_start, shift_end, shift_name, beat_id) VALUES (?, ?, ?, ?, ?)",
                (officer_id, start, end, shift_name, beat_id),
            )
            sx, sy = STATION
            units.append(Unit(officer_id, next((b for b in beats if b.id == beat_id), None),
                              sx + rng.uniform(-30, 30), sy + rng.uniform(-30, 30)))
        calls = generate_calls(rng, beats, shift_name, start, end, seq)
        seq += len(calls)
        pings = simulate_shift(rng, beats, zones, units, calls, start, end)
        total_pings += db.insert_pings(conn, pings)
        all_calls += calls

    rows = []
    for c in all_calls:
        lat, lon = to_latlon(c.x, c.y)
        rows.append((c.id, c.ts, c.cleared_ts, c.call_type, c.priority, round(lat, 6), round(lon, 6), c.officer_id))
    db.insert_calls(conn, rows)

    demo_officer = rosters["Swings"][0]
    officer_name = conn.execute("SELECT name FROM officers WHERE id = ?", (demo_officer,)).fetchone()["name"]
    db.set_meta(conn, "demo", "1")
    db.set_meta(conn, "timezone", tz_name)
    conn.commit()
    users = [
        ("sergeant", "sergeant", "Sgt. Demo (Swings)", None),
        ("officer", "officer", f"Ofc. {officer_name}", demo_officer),
        ("auditor", "auditor", "Internal Affairs Auditor", None),
        ("admin", "admin", "System Admin", None),
    ]
    for username, role, display, officer_id in users:
        auth.create_user(conn, username, DEMO_PASSWORD, role, display, officer_id)

    return {
        "beats": len(beats),
        "officers": sum(len(r) for r in rosters.values()),
        "shifts": len(windows),
        "pings": total_pings,
        "calls": len(all_calls),
        "users": [u[0] for u in users],
        "password": DEMO_PASSWORD,
    }
