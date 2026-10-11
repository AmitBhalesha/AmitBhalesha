"""Houston connectors, tested against fixtures shaped like the public sources
(COHGIS GeoJSON, the HEC Active Incidents table, HPD's crime spreadsheet).
No network access: `fetch` is replaced by a fake."""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from beatline import analytics, db, houston

TZ = ZoneInfo("America/Chicago")
W = [[-95.40, 29.74], [-95.38, 29.74], [-95.38, 29.76], [-95.40, 29.76], [-95.40, 29.74]]
E = [[-95.38, 29.74], [-95.36, 29.74], [-95.36, 29.76], [-95.38, 29.76], [-95.38, 29.74]]
ISLAND = [[-95.30, 29.70], [-95.29, 29.70], [-95.29, 29.71], [-95.30, 29.71], [-95.30, 29.70]]

SERVICE = {"layers": [{"id": 4, "name": "Houston Police Districts"}, {"id": 5, "name": "Houston Police Beats"}]}
BEATS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"OBJECTID": 1, "Beats": "1a10", "District": "1"},
     "geometry": {"type": "Polygon", "coordinates": [W]}},
    {"type": "Feature", "properties": {"OBJECTID": 2, "Beats": "1A20", "District": "1"},
     "geometry": {"type": "MultiPolygon", "coordinates": [[E], [ISLAND]]}},
]}

PAGE = """<html><body><table><tr><td>
<table id="layout"><tr><td>City of Houston -- HFD / HPD Active Incidents</td></tr></table>
<table id="incidents">
 <tr><th>Agency</th><th>Address</th><th>Cross Street</th><th>Key Map</th><th>Call Time(Opened)</th>
     <th>Incident Type</th><th>Combined Response</th></tr>
 <tr><td>Police</td><td>1200 MAIN ST</td><td>DALLAS ST</td><td>493N</td><td>10/10/2026 14:05</td>
     <td>DISTURBANCE</td><td>N</td></tr>
 <tr><td>Fire</td><td>800 BAGBY ST</td><td></td><td>493P</td><td>10/10/2026 14:06</td><td>ALARM</td><td>N</td></tr>
 <tr><td>Police</td><td>W GRAY ST</td><td>BAGBY ST</td><td>493J</td><td>10/10/2026 14:10</td>
     <td>SUSPICIOUS PERSON</td><td>N</td></tr>
 <tr><td>Police</td><td>4400 BLOCK OF ALMEDA RD</td><td></td><td>533A</td><td>10/10/2026 14:20</td>
     <td>THEFT</td><td>N</td></tr>
</table></td></tr></table></body></html>"""


class FakeWeb:
    def __init__(self):
        self.page = PAGE
        self.calls = []

    def __call__(self, url, params=None, timeout=30):
        self.calls.append((url, params))
        if url == houston.GIS_SERVICE:
            return json.dumps(SERVICE).encode()
        if url.endswith("/5/query"):
            return json.dumps(BEATS).encode()
        if url == houston.ACTIVE_INCIDENTS_URL:
            return self.page.encode()
        if url == houston.CENSUS_GEOCODER:
            addr = params["address"]
            if addr.startswith("1200 MAIN ST"):
                return json.dumps({"result": {"addressMatches": [
                    {"matchedAddress": "1200 MAIN ST, HOUSTON, TX, 77002", "coordinates": {"x": -95.39, "y": 29.75}}]}}).encode()
            if addr.startswith("4400 ALMEDA RD"):
                return json.dumps({"result": {"addressMatches": [
                    {"matchedAddress": "4400 ALMEDA RD, HOUSTON, TX", "coordinates": {"x": -95.295, "y": 29.705}}]}}).encode()
            return json.dumps({"result": {"addressMatches": []}}).encode()
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture
def web():
    return FakeWeb()


@pytest.fixture
def hconn(tmp_path, web):
    c = db.connect(str(tmp_path / "houston.db"))
    houston.setup(c, fetch=web)
    yield c
    c.close()


def ts(text):
    return int(datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).timestamp())


def test_setup_loads_beats_including_multipart(hconn):
    beats = db.load_beats(hconn)
    assert set(beats) == {"1A10", "1A20"}
    assert len(beats["1A20"]["rings"]) == 2
    assert db.get_meta(hconn, "timezone") == "America/Chicago"
    assert db.get_meta(hconn, "demand_source") == "crime"


def test_parse_active_incidents_police_only():
    incidents, diag = houston.parse_active_incidents(PAGE)
    assert diag["header"][4] == "Call Time(Opened)"
    assert [i["incident_type"] for i in incidents] == ["DISTURBANCE", "SUSPICIOUS PERSON", "THEFT"]
    assert incidents[0]["ts"] == ts("2026-10-10 14:05")
    assert len({i["id"] for i in incidents}) == 3


def test_poll_records_new_and_ongoing_calls(hconn, web):
    first = houston.poll_once(hconn, fetch=web, now=ts("2026-10-10 14:25"))
    assert first == {"listed": 3, "new": 2, "ongoing": 0, "unmapped": 1, "geocode_errors": 0}
    second = houston.poll_once(hconn, fetch=web, now=ts("2026-10-10 14:30"))
    assert second["new"] == 0 and second["ongoing"] == 3
    rows = hconn.execute("SELECT cleared_ts, address, source FROM calls").fetchall()
    assert len(rows) == 2 and all(r["cleared_ts"] == ts("2026-10-10 14:30") for r in rows)
    assert {r["source"] for r in rows} == {"hec"}
    assert "@ DALLAS ST" in next(r["address"] for r in rows if r["address"].startswith("1200"))
    geocoder_hits = [c for c in web.calls if c[0] == houston.CENSUS_GEOCODER]
    assert len(geocoder_hits) == 3  # cached on the second poll


def test_clean_address():
    assert houston.clean_address("4400 block of  Almeda Rd") == "4400 ALMEDA RD"
    assert houston.clean_address("1200-1299 MAIN ST") == "1200 MAIN ST"


def write_xlsx(path, header, rows):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(header)
    for r in rows:
        ws.append(r)
    wb.save(path)


NIBRS_HEADER = ["Incident", "RMSOccurrenceDate", "RMSOccurrenceHour", "NIBRSClass", "NIBRSDescription",
                "OffenseCount", "Beat", "Premise", "StreetNo", "StreetName", "StreetType", "Suffix", "City",
                "ZIPCode", "MapLongitude", "MapLatitude"]


def test_import_crime_xlsx_and_calls_only_report(hconn, web, tmp_path):
    rows = []
    for i in range(30):  # beat 1A10 usually has 30 of 40 crimes
        rows.append([f"1{i:05d}", datetime(2026, 9, 1 + i % 28), 13, "13B", "Simple assault", 1, "1A10", "Road",
                     "1200", "MAIN", "ST", "", "HOUSTON", "77002", -95.39, 29.75])
    for i in range(10):  # beat 1A20: no coordinates, matched by the Beat column
        rows.append([f"2{i:05d}", datetime(2026, 9, 2 + i), 2, "23H", "All other larceny", 1, "1A20", "Road",
                     "4400", "ALMEDA", "RD", "", "HOUSTON", "77004", None, None])
    rows.append(["900000", datetime(2026, 9, 5), 1, "23H", "Theft", 1, "99Z99", "Road", "", "", "", "", "", "", None, None])
    path = tmp_path / "NIBRSPublicViewSep26.xlsx"
    write_xlsx(path, NIBRS_HEADER, rows)

    stats = houston.import_crime(hconn, str(path))
    assert stats["imported"] == 40 and stats["skipped"] == 1 and stats["with_coordinates"] == 30
    assert stats["first"] == "2026-09-01"
    again = houston.import_crime(hconn, str(path))  # re-import does not double count
    assert hconn.execute("SELECT SUM(offense_count) FROM crime_reports").fetchone()[0] == 40
    assert again["imported"] == 40

    # Live calls: 1A20 gets 2 of 2 calls although it usually has 25% of demand.
    houston.poll_once(hconn, fetch=web, now=ts("2026-10-10 14:25"))
    hconn.execute("UPDATE calls SET lat = 29.705, lon = -95.295")  # both calls on 1A20's second part
    report = analytics.shift_report(hconn, ts("2026-10-10 14:00"), ts("2026-10-10 22:00"))
    assert report["mode"] == "calls_only"
    assert report["demand"]["source"] == "crime" and report["demand"]["count"] == 40
    beats = {b["beat_id"]: b for b in report["beats"]}
    assert beats["1A10"]["demand_share"] == 0.75
    assert beats["1A20"]["calls"] == 2 and beats["1A20"]["expected_calls"] == 0.5
    assert report["summary"]["unmapped_calls"] == 1
    assert report["unmapped_calls"][0]["address"] == "W GRAY ST"
    heat = analytics.heat_grid(hconn, ts("2026-10-10 14:00"), ts("2026-10-10 22:00"), "calls")
    assert heat["demand"]["source"] == "crime" and sum(c["v"] for c in heat["cells"]) == 30


def test_import_older_csv_format(hconn, tmp_path):
    path = tmp_path / "jan.csv"
    path.write_text("Date,Hour,Offense Type,Beat,Premise,Block Range,Street Name,Type,Suffix,# Of Offenses\n"
                    "2026-01-03,14,Burglary,1A10,Residence,1200-1299,MAIN,ST,-,2\n"
                    "01/04/2026,9,Theft,1a20,Road,4400-4499,ALMEDA,RD,-,1\n")
    stats = houston.import_crime(hconn, str(path))
    assert stats["imported"] == 2
    row = hconn.execute("SELECT * FROM crime_reports WHERE beat_id = '1A10'").fetchone()
    assert row["offense_count"] == 2 and row["ts"] == ts("2026-01-03 14:00")


def test_call_windows_without_rosters(hconn, web):
    houston.poll_once(hconn, fetch=web, now=ts("2026-10-10 14:25"))
    windows = analytics.call_windows(hconn, "America/Chicago", ts("2026-10-10 15:00"))
    assert windows[0]["name"] == "Evenings" and windows[0]["in_progress"] and windows[0]["calls"] == 2
    assert windows[0]["start"] == ts("2026-10-10 14:00")
    assert not any(w["in_progress"] for w in windows[1:])


def test_call_status():
    assert analytics.call_status(12, 3.0)[1] == "busier"
    assert analytics.call_status(0, 9.0)[1] == "quieter"
    assert analytics.call_status(4, 3.0)[1] == "typical"
