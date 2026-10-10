from beatline import analytics

from .conftest import SHIFT, T0


def by(rows, key):
    return {r[key]: r for r in rows}


def test_time_in_assigned_beat(conn):
    report = analytics.shift_report(conn, *SHIFT)
    officers = by(report["officers"], "officer_id")
    a = officers["101"]
    # 30 one-minute intervals tracked. Each interval is credited to its first ping, so
    # minutes 0-21 are in W (assigned) and 21-30 in E. Signal lost after minute 30.
    assert a["tracked_minutes"] == 30
    assert a["in_assigned_minutes"] == 21
    assert a["pct_in_assigned"] == 70.0
    assert a["calls_handled"] == 1
    assert officers["102"]["pct_in_assigned"] == 100.0


def test_beat_gaps_and_coverage(conn):
    report = analytics.shift_report(conn, *SHIFT)
    beats = by(report["beats"], "beat_id")
    # Nobody in W after minute 21.
    assert beats["W"]["longest_gap_minutes"] == 39
    assert beats["W"]["longest_gap_start"] == T0 + 21 * 60
    assert beats["E"]["longest_gap_minutes"] == 0
    assert beats["E"]["covered_pct"] == 100.0
    # Demand: one call each in the trailing 28 days -> 50/50. Patrol: W 21 of 90 unit-minutes.
    assert beats["W"]["demand_share"] == 0.5
    assert beats["W"]["coverage_index"] == round((21 / 90) / 0.5, 2)
    assert beats["W"]["status"] == "under"


def test_gap_longer_than_max_is_not_credited(conn):
    # Officer 101's last ping is minute 30; a ping 20 minutes later must not credit 20 minutes.
    conn.execute("INSERT INTO pings VALUES ('101', ?, 40.0, -111.0, 0)", (T0 + 50 * 60,))
    report = analytics.shift_report(conn, *SHIFT)
    assert by(report["officers"], "officer_id")["101"]["tracked_minutes"] == 30


def test_window_clips_segments(conn):
    report = analytics.shift_report(conn, T0 + 10 * 60, T0 + 25 * 60)
    a = by(report["officers"], "officer_id")["101"]
    assert a["tracked_minutes"] == 15
    assert a["in_assigned_minutes"] == 11  # minutes 10-21 (the 20->21 interval is credited to W)


def test_zone_dosage(conn):
    report = analytics.shift_report(conn, *SHIFT)
    z = report["zones"][0]
    assert z["minutes"] == 21  # minutes 0-21 credited at the zone point
    assert z["visits"] == 1
    assert z["pct_of_target"] == 105.0


def test_gps_response_time(conn):
    report = analytics.shift_report(conn, *SHIFT)
    call = report["calls"][0]
    assert call["id"] == "C1" and call["gps_response_s"] == 0  # unit was already on scene


def test_officer_filter_hides_other_officers(conn):
    report = analytics.shift_report(conn, *SHIFT, officer_filter=["102"])
    assert [o["officer_id"] for o in report["officers"]] == ["102"]
    assert report["calls"] == []  # C1 belongs to 101
    # Beat totals are still city-wide.
    assert by(report["beats"], "beat_id")["W"]["unit_minutes"] == 21


def test_heat_grid_layers(conn):
    patrol = analytics.heat_grid(conn, *SHIFT, "patrol")
    assert round(sum(c["v"] for c in patrol["cells"])) == 30 + 60
    calls = analytics.heat_grid(conn, *SHIFT, "calls")
    assert sum(c["v"] for c in calls["cells"]) == 2


def test_longest_gap_helper():
    assert analytics.longest_gap([], 0, 100) == (100, 0, 100)
    assert analytics.longest_gap([(0, 10), (5, 20), (70, 100)], 0, 100) == (50, 20, 70)
