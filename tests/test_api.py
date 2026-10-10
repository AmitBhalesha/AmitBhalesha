import pytest
from fastapi.testclient import TestClient

from beatline import auth, db
from beatline.api import create_app
from beatline.config import Settings

from .conftest import SHIFT

Q = f"start={SHIFT[0]}&end={SHIFT[1]}"


@pytest.fixture
def client(conn, tmp_path):
    for username, role, officer_id in [
        ("sgt", "sergeant", None), ("ofc", "officer", "101"), ("ia", "auditor", None), ("root", "admin", None),
    ]:
        auth.create_user(conn, username, "password123", role, officer_id=officer_id)
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    return TestClient(create_app(Settings(db_path=path)))


def login(client, username):
    r = client.post("/api/login", json={"username": username, "password": "password123"})
    assert r.status_code == 200, r.text
    return r.json()["user"]


def audit_actions(conn):
    return [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id")]


def test_requires_login(client):
    assert client.get(f"/api/report?{Q}").status_code == 401
    assert client.get("/api/config").status_code == 200


def test_bad_password_is_audited_and_locks_out(client, conn):
    for _ in range(5):
        assert client.post("/api/login", json={"username": "sgt", "password": "nope"}).status_code == 401
    assert client.post("/api/login", json={"username": "sgt", "password": "password123"}).status_code == 429
    assert audit_actions(conn).count("login_failed") == 5


def test_sergeant_sees_all_units(client, conn):
    login(client, "sgt")
    report = client.get(f"/api/report?{Q}").json()
    assert {o["officer_id"] for o in report["officers"]} == {"101", "102"}
    trails = client.get(f"/api/trails?{Q}").json()
    assert set(trails) == {"101", "102"}
    assert "view_trails" in audit_actions(conn)
    row = conn.execute("SELECT detail FROM audit_log WHERE action = 'view_trails'").fetchone()
    assert '"101"' in row["detail"] and '"102"' in row["detail"]


def test_officer_sees_only_self(client, conn):
    login(client, "ofc")
    report = client.get(f"/api/report?{Q}").json()
    assert [o["officer_id"] for o in report["officers"]] == ["101"]
    assert set(client.get(f"/api/trails?{Q}").json()) == {"101"}
    r = client.get(f"/api/trails?{Q}&officer_id=102")
    assert r.status_code == 403
    assert "denied_trails" in audit_actions(conn)


def test_auditor_cannot_see_locations_but_can_read_log(client):
    login(client, "ia")
    assert client.get(f"/api/report?{Q}").status_code == 403
    assert client.get(f"/api/trails?{Q}").status_code == 403
    log = client.get("/api/audit").json()
    assert any(r["action"] == "login" and r["username"] == "ia" for r in log)


def test_sergeant_cannot_read_audit_log_or_ingest(client):
    login(client, "sgt")
    assert client.get("/api/audit").status_code == 403
    assert client.post("/api/ingest/pings", json=[]).status_code == 403


def test_admin_ingest(client, conn):
    login(client, "root")
    r = client.post("/api/ingest/pings", json=[
        {"officer_id": "101", "ts": SHIFT[0] + 45 * 60, "lat": 40.0, "lon": -111.0},
        {"officer_id": "101", "ts": SHIFT[0], "lat": 40.0, "lon": -111.0},  # duplicate key, ignored
    ])
    assert r.json() == {"received": 2, "inserted": 1}
    assert client.post("/api/ingest/pings", json=[{"officer_id": "1", "ts": 1, "lat": 91, "lon": 0}]).status_code == 422


def test_window_validation(client):
    login(client, "sgt")
    assert client.get(f"/api/report?start={SHIFT[1]}&end={SHIFT[0]}").status_code == 400
    assert client.get(f"/api/report?start=0&end={40 * 86400}").status_code == 400


def test_audit_log_is_append_only(conn):
    from beatline import audit

    audit.record(conn, "x", "test")
    with pytest.raises(Exception):
        conn.execute("DELETE FROM audit_log")
    with pytest.raises(Exception):
        conn.execute("UPDATE audit_log SET action = 'edited'")


def test_logout_ends_session(client):
    login(client, "sgt")
    assert client.get("/api/me").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401


def test_shifts_and_static(client):
    login(client, "sgt")
    shifts = client.get("/api/shifts").json()
    assert shifts == [{"name": "Days", "start": SHIFT[0], "end": SHIFT[1], "units": 2}]
    r = client.get("/")
    assert r.status_code == 200 and "Beatline" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_bearer_token_for_integrations(client):
    r = client.post("/api/login", json={"username": "root", "password": "password123", "want_token": True})
    token = r.json()["token"]
    fresh = TestClient(client.app)
    assert fresh.get("/api/me", headers={"Authorization": f"Bearer {token}"}).json()["username"] == "root"


def test_demo_seed_end_to_end(tmp_path):
    from datetime import datetime, timezone

    from beatline import analytics
    from beatline.simulate import seed_demo

    c = db.connect(str(tmp_path / "demo.db"))
    stats = seed_demo(c, days=1, now=datetime(2026, 10, 10, 12, tzinfo=timezone.utc))
    assert stats["shifts"] == 3 and stats["pings"] > 10_000
    s = c.execute("SELECT shift_start, shift_end FROM assignments ORDER BY shift_start DESC LIMIT 1").fetchone()
    report = analytics.shift_report(c, s["shift_start"], s["shift_end"])
    assert len(report["beats"]) == 9
    for o in report["officers"]:
        assert 0 < o["tracked_minutes"] <= 480
        assert o["pct_in_assigned"] is None or 0 <= o["pct_in_assigned"] <= 100


def test_export_demo_writes_page_and_data(conn, tmp_path):
    import json

    from beatline.export_demo import export_demo

    auth.create_user(conn, "officer", "password123", "officer", officer_id="101")
    result = export_demo(conn, str(tmp_path / "demo"), "Test PD", "America/Denver")
    page = (tmp_path / "demo" / "index.html").read_text()
    assert "<html" not in page and "<body" not in page and "BEATLINE_DEMO" in page
    meta = json.loads((tmp_path / "demo" / "data" / "meta.json").read_text())
    assert meta["config"]["tile_url"] is None and meta["officer_shifts"]
    shift = json.loads((tmp_path / "demo" / result["files"][1]).read_text())
    assert [o["officer_id"] for o in shift["officer_report"]["officers"]] == ["101"]
    assert set(shift["trails"]) == {"101", "102"}
