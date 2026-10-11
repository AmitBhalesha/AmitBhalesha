"""HTTP API + static dashboard."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import analytics, audit, auth, db
from .config import Settings

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "beatline_session"
MAX_WINDOW_S = 31 * 86400
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_S = 15 * 60


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    # Scripts/integrations set this to get a bearer token; the browser relies on the HttpOnly cookie.
    want_token: bool = False


class PingIn(BaseModel):
    officer_id: str = Field(min_length=1, max_length=32)
    ts: int = Field(gt=0)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    speed_mph: float | None = Field(default=None, ge=0, le=200)


class CallIn(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    ts: int = Field(gt=0)
    cleared_ts: int | None = None
    call_type: str = Field(min_length=1, max_length=64)
    priority: int = Field(ge=1, le=9)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    officer_id: str | None = Field(default=None, max_length=32)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    conn = db.connect(settings.db_path)
    db.init_db(conn)
    conn.close()

    app = FastAPI(title="Beatline", docs_url="/api/docs", openapi_url="/api/openapi.json")
    failures: dict[str, deque] = defaultdict(deque)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if not request.url.path.startswith("/api/docs"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
            )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def get_conn():
        conn = db.connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def client_ip(request: Request) -> str | None:
        return request.client.host if request.client else None

    def current_user(request: Request, conn=Depends(get_conn)) -> dict:
        token = request.cookies.get(COOKIE)
        header = request.headers.get("authorization", "")
        if not token and header.lower().startswith("bearer "):
            token = header[7:].strip()
        user = auth.user_for_token(conn, token) if token else None
        if user is None:
            raise HTTPException(401, "Not signed in")
        request.state.token = token
        return user

    def require(*roles: str):
        def dependency(user: dict = Depends(current_user)) -> dict:
            if user["role"] not in roles:
                raise HTTPException(403, f"The {user['role']} role cannot access this")
            return user
        return dependency

    def window(start: int = Query(...), end: int = Query(...)) -> tuple[int, int]:
        if end <= start:
            raise HTTPException(400, "end must be after start")
        if end - start > MAX_WINDOW_S:
            raise HTTPException(400, "window may not exceed 31 days")
        return start, end

    def officer_scope(user: dict) -> list[str] | None:
        """Officers only ever see their own location history."""
        return [user["officer_id"]] if user["role"] == "officer" else None

    ops_roles = require("sergeant", "officer", "admin")

    @app.post("/api/login")
    def login(body: LoginIn, request: Request, response: Response, conn=Depends(get_conn)):
        key = body.username.lower()
        now = time.time()
        recent = failures[key]
        while recent and recent[0] < now - LOGIN_LOCKOUT_S:
            recent.popleft()
        if len(recent) >= LOGIN_MAX_FAILURES:
            audit.record(conn, body.username, "login_locked", {}, client_ip(request))
            raise HTTPException(429, "Too many failed sign-ins. Try again in 15 minutes.")
        user = auth.authenticate(conn, body.username, body.password)
        if user is None:
            recent.append(now)
            audit.record(conn, body.username, "login_failed", {}, client_ip(request))
            raise HTTPException(401, "Wrong username or password")
        recent.clear()
        token = auth.create_session(conn, user["username"])
        response.set_cookie(
            COOKIE, token, httponly=True, samesite="strict", secure=settings.secure_cookies,
            max_age=auth.SESSION_TTL_S,
        )
        audit.record(conn, user["username"], "login", {"role": user["role"]}, client_ip(request))
        return {"user": user, "token": token} if body.want_token else {"user": user}

    @app.post("/api/logout")
    def logout(request: Request, response: Response, user=Depends(current_user), conn=Depends(get_conn)):
        auth.end_session(conn, request.state.token)
        response.delete_cookie(COOKIE)
        audit.record(conn, user["username"], "logout", {}, client_ip(request))
        return {"ok": True}

    @app.get("/api/me")
    def me(user=Depends(current_user)):
        return user

    @app.get("/api/config")
    def config(conn=Depends(get_conn)):
        return {
            "agency_name": db.get_meta(conn, "agency_name", settings.agency_name),
            "timezone": db.get_meta(conn, "timezone", settings.timezone),
            "demo": db.get_meta(conn, "demo") == "1",
            "data_note": db.get_meta(conn, "data_note"),
            "bounds": db.beats_bounds(db.load_beats(conn)),
            "tile_url": settings.tile_url,
            "tile_url_dark": settings.tile_url_dark,
            "tile_attribution": settings.tile_attribution,
            "thresholds": {
                "under_covered": analytics.UNDER_COVERED,
                "over_covered": analytics.OVER_COVERED,
                "long_gap_minutes": analytics.LONG_GAP_S // 60,
            },
        }

    @app.get("/api/beats")
    def beats(user=Depends(current_user), conn=Depends(get_conn)):
        return list(db.load_beats(conn).values())

    @app.get("/api/zones")
    def zones(user=Depends(current_user), conn=Depends(get_conn)):
        return db.load_zones(conn)

    @app.get("/api/shifts")
    def shifts(limit: int = Query(60, ge=1, le=500), user=Depends(ops_roles), conn=Depends(get_conn)):
        sql = "SELECT shift_name, shift_start, shift_end, COUNT(*) AS units FROM assignments"
        params: list = []
        if user["role"] == "officer":
            sql += " WHERE officer_id = ?"
            params.append(user["officer_id"])
        sql += " GROUP BY shift_start, shift_end, shift_name ORDER BY shift_start DESC LIMIT ?"
        rows = conn.execute(sql, params + [limit]).fetchall()
        if not rows and not conn.execute("SELECT 1 FROM assignments LIMIT 1").fetchone():
            # No rosters at all (public data): offer shift-shaped windows over the recorded calls.
            tz = db.get_meta(conn, "timezone", settings.timezone)
            return analytics.call_windows(conn, tz, int(time.time()), limit)
        return [
            {"name": r["shift_name"], "start": r["shift_start"], "end": r["shift_end"], "units": r["units"]}
            for r in rows
        ]

    @app.get("/api/report")
    def report(request: Request, w=Depends(window), user=Depends(ops_roles), conn=Depends(get_conn)):
        scope = officer_scope(user)
        result = analytics.shift_report(conn, w[0], w[1], officer_filter=scope)
        audit.record(
            conn, user["username"], "view_shift_report",
            {"start": w[0], "end": w[1], "officers": [o["officer_id"] for o in result["officers"]]},
            client_ip(request),
        )
        return result

    @app.get("/api/trails")
    def trails(
        request: Request,
        officer_id: list[str] | None = Query(None),
        w=Depends(window),
        user=Depends(ops_roles),
        conn=Depends(get_conn),
    ):
        scope = officer_scope(user)
        if scope is not None:
            if officer_id and set(officer_id) - set(scope):
                audit.record(conn, user["username"], "denied_trails", {"requested": officer_id}, client_ip(request))
                raise HTTPException(403, "Officers can only view their own location history")
            officer_id = scope
        result = analytics.trails(conn, w[0], w[1], officer_id)
        audit.record(
            conn, user["username"], "view_trails",
            {"start": w[0], "end": w[1], "officers": sorted(result)},
            client_ip(request),
        )
        return result

    @app.get("/api/heat")
    def heat(
        request: Request,
        layer: str = Query("patrol", pattern="^(patrol|calls)$"),
        cell_m: float = Query(200, ge=50, le=2000),
        w=Depends(window),
        user=Depends(ops_roles),
        conn=Depends(get_conn),
    ):
        result = analytics.heat_grid(conn, w[0], w[1], layer, cell_m, officer_filter=officer_scope(user))
        audit.record(conn, user["username"], "view_heat", {"layer": layer, "start": w[0], "end": w[1]},
                     client_ip(request))
        return result

    @app.get("/api/audit")
    def audit_log(
        request: Request,
        limit: int = Query(200, ge=1, le=2000),
        username: str | None = None,
        user=Depends(require("auditor", "admin")),
        conn=Depends(get_conn),
    ):
        rows = audit.recent(conn, limit, username)
        audit.record(conn, user["username"], "view_audit_log", {"filter_username": username}, client_ip(request))
        return rows

    @app.post("/api/ingest/pings")
    def ingest_pings(
        body: list[PingIn], request: Request, user=Depends(require("admin")), conn=Depends(get_conn)
    ):
        inserted = db.insert_pings(conn, [(p.officer_id, p.ts, p.lat, p.lon, p.speed_mph) for p in body])
        conn.commit()
        audit.record(conn, user["username"], "ingest_pings", {"received": len(body), "inserted": inserted},
                     client_ip(request))
        return {"received": len(body), "inserted": inserted}

    @app.post("/api/ingest/calls")
    def ingest_calls(
        body: list[CallIn], request: Request, user=Depends(require("admin")), conn=Depends(get_conn)
    ):
        rows = [(c.id, c.ts, c.cleared_ts, c.call_type, c.priority, c.lat, c.lon, c.officer_id) for c in body]
        written = db.insert_calls(conn, rows)
        conn.commit()
        audit.record(conn, user["username"], "ingest_calls", {"received": len(body), "written": written},
                     client_ip(request))
        return {"received": len(body), "written": written}

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
