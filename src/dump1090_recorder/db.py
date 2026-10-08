"""SQLite storage layer for dump1090-recorder.

The database is deliberately boring: one append-only ``positions`` table
indexed for per-aircraft track reconstruction (``hex, ts``) and time-window
pruning (``ts``), plus a derived per-hex ``flights`` aggregate that is
recomputed whenever rows are inserted or pruned.

Timestamps are unix epoch seconds taken from the decoder's own ``now`` field
in ``aircraft.json``, so a recorder fed by a remote decoder still records the
receiver's clock. ``seen_pos`` keeps the position age in seconds relative to
that timestamp, exactly as dump1090 / readsb report it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from dump1090_recorder.errors import RecorderError
from dump1090_recorder.geo import haversine_nm

__all__ = [
    "DEFAULT_DB",
    "DayCount",
    "FlightInfo",
    "NearPoint",
    "Position",
    "Summary",
    "TopAircraft",
    "get_flight",
    "insert_positions",
    "iter_positions",
    "near",
    "open_db",
    "position_from_aircraft",
    "positions_from_snapshot",
    "prune",
    "refresh_flights",
    "summary",
    "track",
]

#: Default database location (override with ``--db`` or DUMP1090_RECORDER_DB).
DEFAULT_DB = "dump1090.sqlite3"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS positions (
    ts REAL NOT NULL,
    hex TEXT NOT NULL,
    flight TEXT,
    lat REAL,
    lon REAL,
    alt_baro REAL,
    gs REAL,
    track REAL,
    squawk TEXT,
    rssi REAL,
    seen_pos REAL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_hex_ts ON positions(hex, ts);
CREATE INDEX IF NOT EXISTS idx_positions_ts ON positions(ts);
CREATE TABLE IF NOT EXISTS flights (
    hex TEXT PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    max_alt REAL,
    min_rssi REAL,
    callsigns TEXT NOT NULL DEFAULT ''
);
"""

_POSITION_COLUMNS = (
    "ts, hex, flight, lat, lon, alt_baro, gs, track, squawk, rssi, seen_pos, raw_json"
)


@dataclass(frozen=True, slots=True)
class Position:
    """One recorded observation of one aircraft."""

    ts: float
    hex: str
    flight: str | None = None
    lat: float | None = None
    lon: float | None = None
    alt_baro: float | None = None
    gs: float | None = None
    track: float | None = None
    squawk: str | None = None
    rssi: float | None = None
    seen_pos: float | None = None
    raw_json: str | None = None


@dataclass(frozen=True, slots=True)
class FlightInfo:
    """Derived per-hex aggregate (the ``flights`` table)."""

    hex: str
    first_seen: float
    last_seen: float
    max_alt: float | None
    min_rssi: float | None
    callsigns: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NearPoint:
    """A stored position plus its distance from a query point."""

    position: Position
    dist_nm: float


@dataclass(frozen=True, slots=True)
class DayCount:
    """Positions recorded on one UTC day."""

    date: str
    rows: int
    aircraft: int


@dataclass(frozen=True, slots=True)
class TopAircraft:
    """One row of the "top aircraft by sightings" ranking."""

    hex: str
    callsigns: str
    rows: int
    first_seen: float
    last_seen: float


@dataclass(frozen=True, slots=True)
class Summary:
    """Everything ``dump1090-recorder summary`` prints."""

    total_rows: int
    aircraft: int
    first_ts: float | None
    last_ts: float | None
    days: tuple[DayCount, ...]
    top: tuple[TopAircraft, ...]


def open_db(path: str | Path) -> sqlite3.Connection:
    """Open (creating if needed) a recorder database in WAL mode.

    WAL matters here: the ``record`` loop writes continuously while you run
    ``summary`` / ``track`` / ``near`` / ``export`` in another terminal, and
    WAL lets readers work without blocking the writer.

    Raises:
        RecorderError: The path cannot be opened or the schema cannot be built.
    """
    target = Path(path)
    try:
        conn = sqlite3.connect(target)
    except sqlite3.Error as exc:
        raise RecorderError(f"cannot open database {target}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
    except sqlite3.Error as exc:
        conn.close()
        raise RecorderError(f"cannot initialise database {target}: {exc}") from exc
    conn.commit()
    return conn


def _num(value: Any) -> float | None:
    """Coerce an aircraft.json value to float, or None when unusable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        result = float(value)
        return None if result != result else result  # NaN guard
    return None


def _text(value: Any) -> str | None:
    """Coerce an aircraft.json value to a stripped non-empty string."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _alt(value: Any) -> float | None:
    """Barometric altitude in feet; dump1090's ``"ground"`` becomes 0."""
    if isinstance(value, str):
        return 0.0 if value.strip().lower() == "ground" else None
    return _num(value)


def position_from_aircraft(
    entry: Mapping[str, Any], *, ts: float, store_raw: bool = True
) -> Position | None:
    """Convert one ``aircraft[]`` entry into a :class:`Position`.

    Entries without an ICAO ``hex`` cannot be recorded and are skipped.
    ``alt_baro: "ground"`` is stored as 0 feet (the original JSON is preserved
    verbatim in ``raw_json`` unless ``store_raw`` is off).
    """
    hex_value = _text(entry.get("hex"))
    if hex_value is None:
        return None
    raw_json = None
    if store_raw:
        raw_json = json.dumps(dict(entry), separators=(",", ":"), sort_keys=True)
    return Position(
        ts=ts,
        hex=hex_value.lower(),
        flight=_text(entry.get("flight")),
        lat=_num(entry.get("lat")),
        lon=_num(entry.get("lon")),
        alt_baro=_alt(entry.get("alt_baro")),
        gs=_num(entry.get("gs")),
        track=_num(entry.get("track")),
        squawk=_text(entry.get("squawk")),
        rssi=_num(entry.get("rssi")),
        seen_pos=_num(entry.get("seen_pos")),
        raw_json=raw_json,
    )


def positions_from_snapshot(
    doc: Mapping[str, Any], *, ts: float, store_raw: bool = True
) -> list[Position]:
    """Turn a full ``aircraft.json`` document into recordable positions."""
    entries = doc.get("aircraft")
    result: list[Position] = []
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, Mapping):
                position = position_from_aircraft(entry, ts=ts, store_raw=store_raw)
                if position is not None:
                    result.append(position)
    return result


def _rebuild_flight(conn: sqlite3.Connection, hex_code: str) -> None:
    """Recompute the ``flights`` row for one hex from retained positions."""
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts), MAX(alt_baro), MIN(rssi) FROM positions WHERE hex = ?",
        (hex_code,),
    ).fetchone()
    if row is None or row[0] is None:
        conn.execute("DELETE FROM flights WHERE hex = ?", (hex_code,))
        return
    callsigns = [
        str(other[0])
        for other in conn.execute(
            "SELECT DISTINCT flight FROM positions "
            "WHERE hex = ? AND flight IS NOT NULL AND flight != '' ORDER BY flight",
            (hex_code,),
        )
    ]
    conn.execute(
        "INSERT INTO flights (hex, first_seen, last_seen, max_alt, min_rssi, callsigns) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(hex) DO UPDATE SET "
        "first_seen = excluded.first_seen, last_seen = excluded.last_seen, "
        "max_alt = excluded.max_alt, min_rssi = excluded.min_rssi, "
        "callsigns = excluded.callsigns",
        (hex_code, float(row[0]), float(row[1]), row[2], row[3], ",".join(callsigns)),
    )


def refresh_flights(conn: sqlite3.Connection, hexes: Iterable[str] | None = None) -> None:
    """Rebuild the derived ``flights`` aggregate.

    With ``hexes=None`` the whole table is rebuilt (used after pruning, where
    aggregates such as ``max_alt`` may drop); otherwise only the given hexes
    are touched, which keeps the per-poll cost during recording tiny.
    """
    if hexes is None:
        targets = [str(row[0]) for row in conn.execute("SELECT DISTINCT hex FROM positions")]
        conn.execute("DELETE FROM flights")
    else:
        targets = sorted({str(hex_value).lower() for hex_value in hexes})
    for hex_code in targets:
        _rebuild_flight(conn, hex_code)
    conn.commit()


def _normalise(row: Position) -> Position:
    """Enforce storage invariants regardless of caller: lowercase hex, trimmed flight."""
    hex_code = row.hex.lower()
    flight = row.flight
    if flight is not None:
        stripped = flight.strip()
        flight = stripped or None
    if hex_code == row.hex and flight == row.flight:
        return row
    return replace(row, hex=hex_code, flight=flight)


def insert_positions(conn: sqlite3.Connection, rows: Sequence[Position]) -> int:
    """Append positions in one transaction and refresh their flights.

    Returns:
        The number of rows written (0 for an empty batch, no commit done).
    """
    if not rows:
        return 0
    stored = [_normalise(row) for row in rows]
    conn.executemany(
        f"INSERT INTO positions ({_POSITION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row.ts,
                row.hex,
                row.flight,
                row.lat,
                row.lon,
                row.alt_baro,
                row.gs,
                row.track,
                row.squawk,
                row.rssi,
                row.seen_pos,
                row.raw_json,
            )
            for row in stored
        ],
    )
    refresh_flights(conn, hexes={row.hex for row in stored})
    return len(stored)


def prune(conn: sqlite3.Connection, retention_days: int, *, now: float | None = None) -> int:
    """Delete positions older than ``retention_days`` and refresh aggregates.

    Args:
        conn: Database connection.
        retention_days: Keep this many days of history (0 or less = no-op).
        now: Clock to measure the window from (defaults to wall time).

    Returns:
        Number of positions deleted. ``flights`` is fully rebuilt when
        anything was removed, since max/min aggregates may change.
    """
    if retention_days <= 0:
        return 0
    cutoff = (time.time() if now is None else now) - retention_days * 86400
    deleted = conn.execute("DELETE FROM positions WHERE ts < ?", (cutoff,)).rowcount
    if deleted > 0:
        refresh_flights(conn)
    conn.commit()
    return deleted


def _row_to_position(row: sqlite3.Row) -> Position:
    return Position(
        ts=float(row["ts"]),
        hex=str(row["hex"]),
        flight=row["flight"],
        lat=row["lat"],
        lon=row["lon"],
        alt_baro=row["alt_baro"],
        gs=row["gs"],
        track=row["track"],
        squawk=row["squawk"],
        rssi=row["rssi"],
        seen_pos=row["seen_pos"],
        raw_json=row["raw_json"],
    )


def track(conn: sqlite3.Connection, hex_code: str, *, limit: int | None = None) -> list[Position]:
    """Reconstruct one aircraft's path, oldest first (positioned rows only)."""
    sql = (
        "SELECT * FROM positions WHERE hex = ? AND lat IS NOT NULL AND lon IS NOT NULL ORDER BY ts"
    )
    params: list[Any] = [hex_code.lower()]
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [_row_to_position(row) for row in conn.execute(sql, params)]


def near(
    conn: sqlite3.Connection,
    lat: float,
    lon: float,
    radius_nm: float,
    *,
    since: float | None = None,
    limit: int | None = None,
) -> list[NearPoint]:
    """All stored positions within ``radius_nm`` of a point, nearest first.

    A full scan with an exact haversine test: the dataset is filtered by the
    optional ``since`` window first, and correctness near the poles and the
    antimeridian matters more than a bounding-box shortcut.
    """
    sql = "SELECT * FROM positions WHERE lat IS NOT NULL AND lon IS NOT NULL"
    params: list[Any] = []
    if since is not None:
        sql += " AND ts >= ?"
        params.append(since)
    matches: list[NearPoint] = []
    for row in conn.execute(sql, params):
        position = _row_to_position(row)
        assert position.lat is not None and position.lon is not None
        dist = haversine_nm(lat, lon, position.lat, position.lon)
        if dist <= radius_nm:
            matches.append(NearPoint(position=position, dist_nm=dist))
    matches.sort(key=lambda point: (point.dist_nm, point.position.ts))
    if limit is not None and limit > 0:
        return matches[:limit]
    return matches


def iter_positions(
    conn: sqlite3.Connection,
    *,
    hex_code: str | None = None,
    since: float | None = None,
    until: float | None = None,
    limit: int | None = None,
) -> Iterator[Position]:
    """Stream positions in time order, optionally filtered for export."""
    clauses: list[str] = []
    params: list[Any] = []
    if hex_code is not None:
        clauses.append("hex = ?")
        params.append(hex_code.lower())
    if since is not None:
        clauses.append("ts >= ?")
        params.append(since)
    if until is not None:
        clauses.append("ts <= ?")
        params.append(until)
    sql = f"SELECT * FROM positions{_where(clauses)} ORDER BY ts"
    if limit is not None and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    for row in conn.execute(sql, params):
        yield _row_to_position(row)


def _where(clauses: Sequence[str]) -> str:
    return f" WHERE {' AND '.join(clauses)}" if clauses else ""


def get_flight(conn: sqlite3.Connection, hex_code: str) -> FlightInfo | None:
    """Look up the derived flight aggregate for one hex."""
    row = conn.execute(
        "SELECT hex, first_seen, last_seen, max_alt, min_rssi, callsigns FROM flights "
        "WHERE hex = ?",
        (hex_code.lower(),),
    ).fetchone()
    if row is None:
        return None
    callsigns = tuple(item for item in str(row["callsigns"]).split(",") if item)
    max_alt = row["max_alt"]
    min_rssi = row["min_rssi"]
    return FlightInfo(
        hex=str(row["hex"]),
        first_seen=float(row["first_seen"]),
        last_seen=float(row["last_seen"]),
        max_alt=None if max_alt is None else float(max_alt),
        min_rssi=None if min_rssi is None else float(min_rssi),
        callsigns=callsigns,
    )


def summary(conn: sqlite3.Connection, *, top: int = 10) -> Summary:
    """Aggregate per-day totals, message counts and top aircraft by sightings."""
    totals = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT hex), MIN(ts), MAX(ts) FROM positions"
    ).fetchone()
    assert totals is not None
    days = tuple(
        DayCount(date=str(row[0]), rows=int(row[1]), aircraft=int(row[2]))
        for row in conn.execute(
            "SELECT date(ts, 'unixepoch') AS day, COUNT(*), COUNT(DISTINCT hex) "
            "FROM positions GROUP BY day ORDER BY day"
        )
    )
    limit = top if top > 0 else 1
    top_rows = tuple(
        TopAircraft(
            hex=str(row[0]),
            callsigns=str(row[4]),
            rows=int(row[1]),
            first_seen=float(row[2]),
            last_seen=float(row[3]),
        )
        for row in conn.execute(
            "SELECT p.hex, COUNT(*), MIN(p.ts), MAX(p.ts), COALESCE(f.callsigns, '') "
            "FROM positions p LEFT JOIN flights f ON f.hex = p.hex "
            "GROUP BY p.hex ORDER BY COUNT(*) DESC, p.hex ASC LIMIT ?",
            (limit,),
        )
    )
    first_ts = totals[2]
    last_ts = totals[3]
    return Summary(
        total_rows=int(totals[0]),
        aircraft=int(totals[1]),
        first_ts=None if first_ts is None else float(first_ts),
        last_ts=None if last_ts is None else float(last_ts),
        days=days,
        top=top_rows,
    )
