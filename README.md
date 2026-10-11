# Beatline

Patrol coverage analytics for police sergeants. Beatline turns the GPS/AVL breadcrumbs an agency
already collects, plus CAD calls for service, into the answers a supervisor needs at the end of a shift:

- **Where did each unit spend the shift?** Trails, time inside the assigned beat, miles, calls handled
  and stationary time for every unit, plus a playback slider to replay the shift.
- **Which beats went uncovered?** The longest stretch with no unit in each beat, and the share of the
  shift with at least one unit present.
- **Does patrol match demand?** A per-beat *coverage index* (share of patrol time ÷ share of the last
  28 days of calls) flags beats that get far less, or far more, patrol than their call volume suggests.
- **Did hot spots get their directed-patrol minutes?** Minutes and visits per hot spot against a
  per-shift target.
- **How fast did units actually arrive?** Response times measured from GPS: call created until the
  assigned unit was within 100 m.

Accountability is built in. Each role sees a different amount of data, and the audit log is
append-only.

| Role | Sees |
|---|---|
| `sergeant` | Every unit's location history and the coverage picture |
| `officer` | Only their own location history, plus city-wide aggregates |
| `auditor` | The audit log only, with no operational data |
| `admin` | Everything, plus the data-ingestion API |

Every sign-in and every view of location data (who, when, which units, which time window) is written
to an append-only audit log. The database rejects updates or deletes to it.

## Quick start (demo data)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m beatline seed-demo          # 7 days of a synthetic 9-beat city
python -m beatline serve              # http://127.0.0.1:8000
```

Sign in as `sergeant`, `officer`, `auditor` or `admin`. The password for all four is `beatline-demo`.

The demo city, officers and calls are all synthetic. Night shift is deliberately short-staffed, so
the coverage gaps and missed hot-spot targets show up in the dashboard.

## Houston live (public data)

Beatline can run on Houston's public data. It doesn't need an agency partnership:

| Data | Source | Command |
|---|---|---|
| HPD beat boundaries | City of Houston GIS, `HoustonMap/Public_Safety` map service, "Houston Police Beats" layer (public domain) | `houston setup` |
| Live police calls | HEC Active Incidents page, which the city regenerates every 5 minutes from its dispatch system | `houston poll` |
| Usual demand per beat | HPD's monthly crime spreadsheet (NIBRS, by street and beat) | `houston import-crime FILE` |

```bash
pip install -e ".[houston]"
export BEATLINE_DB=houston.db
python -m beatline houston check        # tests each source once and prints what it found
python -m beatline houston setup        # loads the real HPD beats
python -m beatline houston import-crime NIBRSPublicView.xlsx   # from HPD's Monthly Crime Data page
python -m beatline create-user sgt --role sergeant
python -m beatline houston poll &       # leave running; records calls every 5 minutes
python -m beatline serve
```

Download the crime spreadsheet from HPD's
[Monthly Crime Data by Street and Police Beat](https://www.houstontx.gov/police/cs/Monthly_Crime_Data_by_Street_and_Police_Beat.htm)
page. Columns are matched by name, so both the current NIBRS layout and the older layout
(`Date, Hour, Offense Type, Beat, ...`) import.

Officer locations are not public, so the dashboard runs in **calls-only mode**:
- Each beat's calls this shift are compared with its usual share of the crime baseline (the latest 90
  days on file). A beat is flagged *busier* or *quieter* than usual only when the difference is too
  large to be chance.
- The current shift is marked live and refreshes every minute.
- Calls whose address can't be geocoded are listed separately, not dropped.

Limits of the public sources:
- **No coordinates or priority.** Active Incidents publishes neither. Addresses are geocoded with the
  free US Census geocoder and cached, and priority is shown as unknown.
- **Partial history.** Only calls listed while the poller is running are captured. History starts the
  first time you run `houston poll`.
- **Possibly incomplete.** The page may not list every call.
- **Layout changes.** If the city changes the page layout, `houston check` shows what the parser sees.
- **Baseline lag.** The crime file lags 1–2 weeks after each month ends, and HPD notes that its
  figures can change.

With an agency's AVL feed connected (`/api/ingest/pings` plus shift assignments), the same Houston
beats switch to the full patrol-coverage view.

## Shareable static demo

```bash
python -m beatline export-demo demo-site/
```

This writes a single page plus precomputed JSON for every shift. You can host it on any static host or
publish it as a Claude artifact. Sign-in is replaced by a "View as" switcher (sergeant, officer,
auditor), and the audit log records that viewer's clicks in memory. There is no street map, because
static hosts can't load map tiles.

## Using real agency data

```bash
python -m beatline import-beats beats.geojson   # beat boundaries (Polygon/MultiPolygon, `id` + `name` properties)
python -m beatline import-pings avl.csv         # officer_id,timestamp,lat,lon[,speed_mph]
python -m beatline import-calls cad.csv         # id,timestamp,call_type,priority,lat,lon[,officer_id,cleared_timestamp]
python -m beatline create-user jsmith --role sergeant --name "Sgt. J. Smith"
python -m beatline purge --days 90              # delete location history past the retention period
```

Timestamps can be Unix seconds or ISO 8601. Times without a zone are read in the agency time zone
(`BEATLINE_TZ`). Shift rosters (who worked which shift, assigned to which beat) go in the
`assignments` table. Most CAD/RMS systems can export them.

Live feeds can push to the ingestion API as an `admin` user (`POST /api/ingest/pings`,
`POST /api/ingest/calls`). For a bearer token, sign in with `{"want_token": true}`. Interactive API
docs are at `/api/docs`.

### Configuration

| Variable | Default |
|---|---|
| `BEATLINE_DB` | `beatline.db` |
| `BEATLINE_AGENCY` | `Demo City Police Department` |
| `BEATLINE_TZ` | `America/Denver` |
| `BEATLINE_RETENTION_DAYS` | `90` |
| `BEATLINE_TILE_URL` / `BEATLINE_TILE_URL_DARK` | CARTO light/dark basemaps |
| `BEATLINE_SECURE_COOKIES` | `0` (set `1` behind HTTPS) |

## How the numbers are computed

Presence is built from consecutive GPS pings. The time between two pings is credited to the location
of the first one. Gaps longer than 5 minutes (dead zones, device off) are credited to nowhere, so
they never inflate coverage. All thresholds live at the top of `beatline/analytics.py`.

## Project layout

```
beatline/
  analytics.py   time-in-beat, coverage index, gaps, hot-spot dosage, GPS response, heat grids
  api.py         FastAPI app: auth, role scoping, audit logging, ingestion, static dashboard
  auth.py        users, PBKDF2 password hashing, sessions
  audit.py       append-only access log
  cli.py         seed / import / purge / serve
  db.py          SQLite schema
  geo.py         point-in-polygon, distances, grid cells
  simulate.py    synthetic demo city
  static/        dashboard (vanilla JS + vendored Leaflet 1.9.4)
tests/           pytest suite
```

Run the tests with `pytest`.

## Before a real deployment

This is an MVP. Before it holds live agency data it needs:

- **CJIS Security Policy compliance.** That means MFA, hosting in a CJIS-ready environment (for
  example AWS GovCloud or Azure Government), encryption at rest, and personnel screening for anyone
  with data access.
- **Postgres/PostGIS** in place of SQLite for multi-user scale.
- **Vendor CAD/AVL connectors** instead of CSV exports.
- **Policy review.** A written use policy covering what location data supervisors may review and
  why, agreed with the officers' union where applicable, and checked against any local
  surveillance-technology ordinance.
