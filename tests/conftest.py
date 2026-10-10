import json

import pytest

from beatline import auth, db

# Two 1 km x 1 km beats side by side: W (west) and E (east).
LAT0, LON0 = 40.0, -111.0
DLAT = 1000 / 111_320
DLON = 1000 / (111_320 * 0.766)  # cos(40 deg)
WEST = [[LON0 - DLON, LAT0], [LON0, LAT0], [LON0, LAT0 + DLAT], [LON0 - DLON, LAT0 + DLAT]]
EAST = [[LON0, LAT0], [LON0 + DLON, LAT0], [LON0 + DLON, LAT0 + DLAT], [LON0, LAT0 + DLAT]]
W_PT = (LAT0 + DLAT / 2, LON0 - DLON / 2)
E_PT = (LAT0 + DLAT / 2, LON0 + DLON / 2)
T0 = 1_700_000_000
SHIFT = (T0, T0 + 3600)


@pytest.fixture(autouse=True)
def fast_hashing(monkeypatch):
    monkeypatch.setattr(auth, "PBKDF2_ITERATIONS", 1000)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(str(tmp_path / "test.db"))
    db.init_db(c)
    db.upsert_beat(c, "W", "West", WEST)
    db.upsert_beat(c, "E", "East", EAST)
    c.execute("INSERT INTO officers (id, name, call_sign, squad) VALUES ('101', 'Ofc. A', '1A01', 'Days')")
    c.execute("INSERT INTO officers (id, name, call_sign, squad) VALUES ('102', 'Ofc. B', '1A02', 'Days')")
    c.execute("INSERT INTO assignments VALUES ('101', ?, ?, 'Days', 'W')", SHIFT)
    c.execute("INSERT INTO assignments VALUES ('102', ?, ?, 'Days', 'E')", SHIFT)
    # A zone in the middle of the west beat.
    c.execute(
        "INSERT INTO patrol_zones VALUES ('Z1', 'West hot spot', ?, ?, 150, 20, 'test')", (W_PT[0], W_PT[1])
    )
    # Officer 101: 0-20 min in W (on the zone), 20-30 min in E, then signal lost.
    pings = []
    for m in range(0, 21):
        pings.append(("101", T0 + m * 60, W_PT[0], W_PT[1], 0.0))
    for m in range(21, 31):
        pings.append(("101", T0 + m * 60, E_PT[0], E_PT[1], 25.0))
    # Officer 102: whole hour in E, pinging every minute.
    for m in range(0, 61):
        pings.append(("102", T0 + m * 60, E_PT[0], E_PT[1], 0.0))
    db.insert_pings(c, pings)
    # One call in W, answered by 101 who is already there; one call in E 28 days ago (demand).
    db.insert_calls(c, [
        ("C1", T0 + 5 * 60, T0 + 15 * 60, "Alarm", 3, W_PT[0], W_PT[1], "101"),
        ("C0", T0 - 10 * 86400, T0 - 10 * 86400 + 600, "Theft", 3, E_PT[0], E_PT[1], None),
    ])
    c.commit()
    yield c
    c.close()


def dumps(obj):
    return json.dumps(obj, sort_keys=True)
