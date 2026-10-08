# dump1090-recorder

[![CI](https://github.com/technothatch/dump1090-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/technothatch/dump1090-recorder/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/technothatch/dump1090-recorder/blob/main/LICENSE)
[![Latest Release](https://img.shields.io/github/v/release/technothatch/dump1090-recorder)](https://github.com/technothatch/dump1090-recorder/releases)
[![Issues](https://img.shields.io/github/issues/technothatch/dump1090-recorder)](https://github.com/technothatch/dump1090-recorder/issues)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/technothatch/dump1090-recorder/pulls)
[![Code size](https://img.shields.io/github/languages/code-size/technothatch/dump1090-recorder)](https://github.com/technothatch/dump1090-recorder)
[![Last commit](https://img.shields.io/github/last-commit/technothatch/dump1090-recorder)](https://github.com/technothatch/dump1090-recorder/commits/main)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen.svg)](https://github.com/technothatch/dump1090-recorder)

A long-term flight recorder for the ADS-B traffic published by
[dump1090](https://github.com/flightaware/dump1090) and
[readsb](https://github.com/wiedehopf/readsb): it polls `aircraft.json`,
appends every observation to a SQLite database in WAL mode, and gives you a
query CLI for the two questions that matter months later — *where was this
ICAO hex* and *what was near my receiver*. `summary`, `track HEX`,
`near LAT LON RADIUS_NM`, retention that prunes itself, and export to CSV,
GeoJSON or the shared `{"t", "aircraft"}` NDJSON format — all from one
stdlib-only Python process (`sqlite3`, `argparse`, `urllib`, nothing else).

## Features

- **`record`** polls a decoder (default `http://localhost:8080`, override
  with `--url` or `DUMP1090_URL`) and appends each snapshot's `aircraft[]`
  array to `positions(ts, hex, flight, lat, lon, alt_baro, gs, track, squawk,
  rssi, seen_pos, raw_json)` with indexes on `(hex, ts)` and `ts` — track
  reconstruction and time-window pruning both stay index-backed.
- **WAL mode**, so `record` keeps writing while you run `summary` / `track` /
  `near` / `export` from another terminal (or while
  [dump1090-coverage](https://github.com/technothatch/dump1090-coverage)
  reads an export).
- **Derived `flights` table** per ICAO hex: `first_seen`, `last_seen`,
  `max_alt`, `min_rssi` and every callsign observed. Refreshed incrementally
  for the hexes in each poll, fully rebuilt after a prune.
- **Retention that manages itself**: `--retention-days N` prunes on start and
  hourly during `record`; `prune --retention-days N` does the same on demand
  (cron-friendly).
- **`--no-store-raw`** drops the per-row copy of the original `aircraft[]`
  JSON — the biggest column by far — when you only care about the typed
  fields.
- **`track HEX`** reconstructs a path oldest-first; **`near LAT LON RADIUS_NM`**
  filters with an exact haversine test (correct at the poles and across the
  antimeridian), sorted nearest first.
- **`summary`** prints aircraft/day totals, message counts per day and the
  top aircraft by sightings, with their callsigns and time ranges.
- **`export --format csv|geojson|ndjson`** with `--hex`, `--since`,
  `--until` and `--limit` filters. NDJSON uses the ecosystem convention
  `{"t": <epoch>, "aircraft": [...]}` shared with
  [dump1090-replay](https://github.com/technothatch/dump1090-replay) and
  [dump1090-influx](https://github.com/technothatch/dump1090-influx) —
  exports load in those tools and vice versa.
- **Faithful clocks**: `ts` comes from the decoder's own `now`, `seen_pos`
  keeps the position age in seconds, and `alt_baro: "ground"` is stored as
  0 ft with the original JSON preserved in `raw_json`.
- **Zero runtime dependencies** — `sqlite3`, `urllib.request`, `argparse`,
  `json` only. Fully typed (`py.typed`), 123 tests at 99% coverage.

## Install

```bash
pip install dump1090-recorder
```

Or from source:

```bash
git clone https://github.com/technothatch/dump1090-recorder.git
cd dump1090-recorder
pip install -e ".[dev]"
```

Requires Python 3.11+.

## Quickstart

**Record from a decoder** (the only step that needs one running):

```console
$ dump1090-recorder record --url http://localhost:8080 --db flights.sqlite3 --interval 1 --max-polls 3 --retention-days 30
# recording http://localhost:8080/data/aircraft.json every 1s -> flights.sqlite3
# recorded 3 snapshot(s), 6 position(s), 1 prune(s)
```

Drop `--max-polls` to run until Ctrl+C — `# stopped` is printed and the
database is left consistent. With `--retention-days 30`, rows older than
30 days are pruned on start and then hourly while recording.

**See what is in the database:**

```console
$ dump1090-recorder summary --db flights.sqlite3
database: flights.sqlite3
positions: 18 row(s) across 2 aircraft over 2 day(s)
range: 2026-10-03 12:00:00 UTC .. 2026-10-04 12:01:00 UTC

per-day totals
  date        positions  aircraft
  ----------  ---------  --------
  2026-10-03  12         2
  2026-10-04  6          2

top 2 aircraft by sightings
  hex     callsigns  positions  first seen               last seen
  ------  ---------  ---------  -----------------------  -----------------------
  40621d  BAW117     9          2026-10-03 12:00:00 UTC  2026-10-04 12:01:00 UTC
  4ca918  RYR22GK    9          2026-10-03 12:00:00 UTC  2026-10-04 12:01:00 UTC
```

**Reconstruct one aircraft's track:**

```console
$ dump1090-recorder track 4ca918 --db flights.sqlite3
track 4ca918: 8 position(s)  callsigns: RYR22GK
  time (UTC)               lat       lon       alt    gs   track  squawk  rssi
  -----------------------  --------  --------  -----  ---  -----  ------  ----
  2026-10-03 12:00:00 UTC  51.47000  -0.45000  18000  380  45.0   1200    -4.2
  2026-10-03 12:00:30 UTC  51.49000  -0.44000  20000  385  46.0   1200    -4.7
  2026-10-03 12:01:00 UTC  51.51000  -0.43000  22000  390  47.0   1200    -5.2
  2026-10-03 12:01:30 UTC  51.53000  -0.42000  24000  395  48.0   1200    -5.7
  2026-10-03 12:02:00 UTC  51.55000  -0.41000  26000  400  49.0   1200    -6.2
  2026-10-04 12:00:00 UTC  51.47000  -0.45000  18000  380  45.0   1200    -4.2
  2026-10-04 12:00:30 UTC  51.49000  -0.44000  20000  385  46.0   1200    -4.7
  2026-10-04 12:01:00 UTC  51.51000  -0.43000  22000  390  47.0   1200    -5.2
```

**Ask what was near a point** (haversine, nautical miles, nearest first):

```console
$ dump1090-recorder near 51.47 -0.45 10 --limit 5 --db flights.sqlite3
near 51.4700,-0.4500 within 10 nm: 5 position(s)
  dist_nm  time (UTC)               hex     flight   alt    rssi
  -------  -----------------------  ------  -------  -----  ----
  0.00     2026-10-03 12:00:00 UTC  4ca918  RYR22GK  18000  -4.2
  0.00     2026-10-04 12:00:00 UTC  4ca918  RYR22GK  18000  -4.2
  1.26     2026-10-03 12:00:00 UTC  40621d  BAW117   34000  -3.1
  1.26     2026-10-03 12:00:30 UTC  4ca918  RYR22GK  20000  -4.7
  1.26     2026-10-03 12:00:30 UTC  40621d  BAW117   34000  -3.1
```

**Export a filtered slice** (a `# wrote N row(s) to …` note follows on stderr):

```console
$ dump1090-recorder export --format csv --hex 4ca918 --until 1791028980 --db flights.sqlite3
ts,hex,flight,lat,lon,alt_baro,gs,track,squawk,rssi,seen_pos
1791028800.0,4ca918,RYR22GK,51.47,-0.45,18000.0,380.0,45.0,1200,-4.2,0.7
1791028830.0,4ca918,RYR22GK,51.49,-0.44,20000.0,385.0,46.0,1200,-4.7,0.7
1791028860.0,4ca918,RYR22GK,51.51,-0.43,22000.0,390.0,47.0,1200,-5.2,0.7
1791028890.0,4ca918,RYR22GK,51.53,-0.42,24000.0,395.0,48.0,1200,-5.7,0.7
1791028920.0,4ca918,RYR22GK,51.55,-0.41,26000.0,400.0,49.0,1200,-6.2,0.7
1791028950.0,4ca918,RYR22GK,,,30000.0,430.0,50.0,1200,-7.1,
```

The same rows as GeoJSON (every row with a position becomes a `Point` with
`[lon, lat]` coordinates; rows without one are skipped):

```console
$ dump1090-recorder export --format geojson --limit 2 --db flights.sqlite3 | python -m json.tool | head -20
{
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                "coordinates": [
                    -0.45,
                    51.47
                ]
            },
            "properties": {
                "ts": 1791028800.0,
                "hex": "4ca918",
                "flight": "RYR22GK",
                "alt_baro": 18000.0,
                "gs": 380.0,
                "track": 45.0,
                "squawk": "1200",
```

Or as NDJSON — one `{"t": <epoch>, "aircraft": [...]}` record per poll, the
format [dump1090-replay](https://github.com/technothatch/dump1090-replay) and
[dump1090-influx](https://github.com/technothatch/dump1090-influx) speak:

```console
$ dump1090-recorder export --format ndjson --hex 4ca918 --until 1791028980 --db flights.sqlite3
{"t":1791028800.0,"aircraft":[{"hex":"4ca918","flight":"RYR22GK","lat":51.47,"lon":-0.45,"alt_baro":18000.0,"gs":380.0,"track":45.0,"squawk":"1200","rssi":-4.2,"seen_pos":0.7}]}
{"t":1791028830.0,"aircraft":[{"hex":"4ca918","flight":"RYR22GK","lat":51.49,"lon":-0.44,"alt_baro":20000.0,"gs":385.0,"track":46.0,"squawk":"1200","rssi":-4.7,"seen_pos":0.7}]}
{"t":1791028860.0,"aircraft":[{"hex":"4ca918","flight":"RYR22GK","lat":51.51,"lon":-0.43,"alt_baro":22000.0,"gs":390.0,"track":47.0,"squawk":"1200","rssi":-5.2,"seen_pos":0.7}]}
{"t":1791028890.0,"aircraft":[{"hex":"4ca918","flight":"RYR22GK","lat":51.53,"lon":-0.42,"alt_baro":24000.0,"gs":395.0,"track":48.0,"squawk":"1200","rssi":-5.7,"seen_pos":0.7}]}
{"t":1791028920.0,"aircraft":[{"hex":"4ca918","flight":"RYR22GK","lat":51.55,"lon":-0.41,"alt_baro":26000.0,"gs":400.0,"track":49.0,"squawk":"1200","rssi":-6.2,"seen_pos":0.7}]}
{"t":1791028950.0,"aircraft":[{"hex":"4ca918","flight":"RYR22GK","alt_baro":30000.0,"gs":430.0,"track":50.0,"squawk":"1200","rssi":-7.1}]}
```

Note the last record: that poll had no fresh position for `4ca918`, so the
aircraft is stored without `lat`/`lon` — counted in `summary`, excluded from
`track`, `near` and GeoJSON.

**Prune on demand** (e.g. from cron when you are not recording):

```console
$ dump1090-recorder prune --retention-days 30 --db flights.sqlite3
pruned 0 position(s) older than 30 day(s)
```

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Success (Ctrl+C stops `record` cleanly) |
| `1` | Runtime error: unreadable/unwritable database, decoder unreachable with nothing recorded, `track` for a hex with no positions |
| `2` | Argument error (bad `--format`, out-of-range latitude, missing subcommand, missing `HEX`) |

## Configuration reference

### Command line / environment

| Flag | Environment | Meaning | Default |
| --- | --- | --- | --- |
| `--db PATH` | `DUMP1090_RECORDER_DB` | SQLite database path (accepted before or after the subcommand) | `dump1090.sqlite3` |
| `--version` | — | Print version and exit | — |

### `dump1090-recorder record`

| Flag | Environment | Meaning | Default |
| --- | --- | --- | --- |
| `--url URL` | `DUMP1090_URL` | Decoder base URL or full `aircraft.json` URL | `http://localhost:8080` |
| `--interval SECS` | — | Seconds between polls | `1.0` |
| `--max-polls N` | — | Stop after N polls (`0` = until interrupted) | `0` |
| `--timeout SECS` | — | HTTP timeout per poll | `5.0` |
| `--retention-days N` | — | Prune rows older than N days on start and hourly (`0` = keep forever) | `0` |
| `--no-store-raw` | — | Do not keep the raw `aircraft[]` JSON per row | off |

### `dump1090-recorder prune`

| Flag | Meaning | Default |
| --- | --- | --- |
| `--retention-days N` | Delete positions older than N days, then rebuild `flights` | required |

### `dump1090-recorder summary`

| Flag | Meaning | Default |
| --- | --- | --- |
| `--top N` | Rows in the top-aircraft-by-sightings table | `10` |

### `dump1090-recorder track HEX`

| Flag | Meaning | Default |
| --- | --- | --- |
| `HEX` | ICAO address (positional, case-insensitive), e.g. `4ca918` | required |
| `--limit N` | Keep only the oldest N positioned rows (`0` = all) | `0` |

### `dump1090-recorder near LAT LON RADIUS_NM`

| Flag | Meaning | Default |
| --- | --- | --- |
| `LAT` / `LON` | Query point in degrees (positional, range-checked) | required |
| `RADIUS_NM` | Search radius in nautical miles (positional, `> 0`) | required |
| `--since EPOCH` | Only positions at or after this unix time | all history |
| `--limit N` | Keep only the N nearest matches (`0` = all) | `0` |

### `dump1090-recorder export`

| Flag | Meaning | Default |
| --- | --- | --- |
| `--format FMT` | `csv`, `geojson` or `ndjson` | `csv` |
| `--out PATH` | Output file (`-` = stdout) | `-` |
| `--hex HEX` | Only this ICAO address (case-insensitive) | all aircraft |
| `--since EPOCH` | Positions at or after this unix time | all history |
| `--until EPOCH` | Positions at or before this unix time | no upper bound |
| `--limit N` | Maximum rows (`0` = all) | `0` |

### The database

Two tables, created idempotently on first use, opened in WAL mode:

| Table | Columns |
| --- | --- |
| `positions` | `ts`, `hex`, `flight`, `lat`, `lon`, `alt_baro`, `gs`, `track`, `squawk`, `rssi`, `seen_pos`, `raw_json` — indexes on `(hex, ts)` and `ts` |
| `flights` (derived) | `hex` (PK), `first_seen`, `last_seen`, `max_alt`, `min_rssi`, `callsigns` (comma-separated) |

Semantics: `ts` is the decoder's `now` at capture time (unix seconds);
`seen_pos` is the age of the position in seconds relative to `ts`, exactly as
dump1090 / readsb report it; `alt_baro` is feet with `"ground"` stored as
`0.0` (the untouched original stays in `raw_json`); `flight` is trimmed and
`hex` lower-cased on insert; `raw_json` holds the original `aircraft[]`
entry verbatim unless `--no-store-raw`.

### The NDJSON format

One JSON object per line, UTF-8, LF-terminated — identical to the format
written by `dump1090-replay record` and consumed by
[dump1090-influx](https://github.com/technothatch/dump1090-influx) and
[dump1090-coverage](https://github.com/technothatch/dump1090-coverage):

```json
{"t": 1791028800.0, "aircraft": [{"hex": "4ca918", "flight": "RYR22GK", "lat": 51.47, "lon": -0.45, "alt_baro": 18000.0, "seen_pos": 0.7, "...": "..."}]}
```

- `t` — unix epoch seconds: the snapshot timestamp (the decoder's `now`).
- `aircraft` — one object per stored row of that snapshot. Fields that were
  never recorded are omitted; `alt_baro` may be `0.0` for a ground target.

## How it works

```text
                         +--------------------+
  dump1090 / readsb ---> | GET aircraft.json  |  urllib.request, stdlib only
  (localhost:8080)       +---------+----------+
                                  |
                          record  |  poll loop, decoder clock as ts
                                  v
   +---------------------------------------------------+
   | SQLite (WAL)  dump1090.sqlite3 / --db PATH        |
   |  positions(ts,hex,flight,lat,lon,alt_baro,...)     |
   |  flights(hex, first_seen, last_seen, ...)          |
   |  index (hex, ts)   index (ts)                      |
   |  --retention-days prune: on start + hourly         |
   +----------------------+----------------------------+
                          |
        +-----------------+------------------+
        |                 |                  |
   summary / track     near (haversine)   export csv | geojson | ndjson
   (other terminal,    (poles &           -> dump1090-replay / influx /
    never blocks         antimeridian        coverage / your scripts
    the writer)          safe)
```

- `record` is a dumb append-only loop: fetch snapshot → parse `aircraft[]` →
  `executemany` insert → refresh `flights` for those hexes → maybe prune.
  Clock, retries and HTTP details live in `fetch.py` / `recorder.py`.
- Queries (`summary`, `track`, `near`, `export`) are read-only and run in a
  separate process/terminal — WAL means they never block the writer.
- `near` pre-filters candidates with an indexed latitude/longitude box, then
  applies the exact haversine test, so it stays correct at the poles and
  across the antimeridian without a spatial extension.
- The CLI (`cli.py`) only parses and formats; all logic lives in the
  library modules, which is why the test suite drives both layers.

## Development

```bash
git clone https://github.com/technothatch/dump1090-recorder.git
cd dump1090-recorder
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make check
```

`make` is a convenience wrapper; the underlying commands are:

```bash
ruff check .
ruff format --check .
mypy src
python -m pytest -q --cov --cov-report=term-missing --cov-fail-under=80
```

Tests stub the decoder with `urllib` opener doubles — no network, no
antenna — and cover the schema, pruning, exports, geometry edge cases
(poles, antimeridian, `alt_baro: "ground"`) and every CLI subcommand
(`main()` takes injectable `opener`/`sleep`/`now` hooks). CI
(`.github/workflows/ci.yml`) runs the same four gates on every push.

## Related projects

- [dump1090](https://github.com/flightaware/dump1090) /
  [readsb](https://github.com/wiedehopf/readsb) — the decoders this polls.
- [dump1090-replay](https://github.com/technothatch/dump1090-replay) —
  replays NDJSON through the ecosystem.
- [dump1090-influx](https://github.com/technothatch/dump1090-influx) — ships
  the same NDJSON into InfluxDB.
- [dump1090-coverage](https://github.com/technothatch/dump1090-coverage) —
  receiver coverage analysis from NDJSON/exports.
- [dump1090-alerts](https://github.com/technothatch/dump1090-alerts) —
  proximity alerts for watched hexes.

## License

MIT — see [LICENSE](LICENSE).
