"""Schema, insertion, retention, track, near and summary tests."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import T0, position, snapshot
from dump1090_recorder.db import (
    get_flight,
    insert_positions,
    iter_positions,
    near,
    open_db,
    position_from_aircraft,
    positions_from_snapshot,
    prune,
    refresh_flights,
    summary,
    track,
)
from dump1090_recorder.errors import RecorderError


def _count(conn: Any) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0])


def _hexes(conn: Any) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT DISTINCT hex FROM positions")}


# ---------------------------------------------------------------- schema ---


def test_open_db_creates_both_tables(conn: Any) -> None:
    tables = {
        str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert {"positions", "flights"} <= tables


def test_open_db_enables_wal_mode(db_path: Path) -> None:
    connection = open_db(db_path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_positions_has_hex_ts_and_ts_indexes(conn: Any) -> None:
    indexes = {str(row[1]) for row in conn.execute("PRAGMA index_list('positions')")}
    assert {"idx_positions_hex_ts", "idx_positions_ts"} <= indexes
    hex_key = [str(row[2]) for row in conn.execute("PRAGMA index_info('idx_positions_hex_ts')")]
    ts_key = [str(row[2]) for row in conn.execute("PRAGMA index_info('idx_positions_ts')")]
    assert hex_key == ["hex", "ts"]
    assert ts_key == ["ts"]


def test_open_db_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(RecorderError, match="cannot open database"):
        open_db(tmp_path / "missing" / "nested" / "recorder.sqlite3")


def test_open_db_rejects_a_file_that_is_not_a_database(tmp_path: Path) -> None:
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"definitely not a sqlite database, just some text")
    with pytest.raises(RecorderError, match="cannot initialise database"):
        open_db(broken)


def test_schema_is_idempotent(db_path: Path) -> None:
    first = open_db(db_path)
    first.close()
    second = open_db(db_path)  # must not blow up on an existing schema
    try:
        assert _count(second) == 0
    finally:
        second.close()


# ------------------------------------------------------- aircraft -> rows ---


def test_insert_and_query_roundtrip(conn: Any) -> None:
    row = position(
        ts=T0,
        hex="ABC123",
        flight=" BAW117 ",
        lat=51.5,
        lon=-0.45,
        alt_baro=12000,
        gs=300,
        track=90.0,
        squawk="7700",
        rssi=-3.5,
        seen_pos=1.5,
    )
    assert insert_positions(conn, [row]) == 1
    stored = track(conn, "abc123")
    assert len(stored) == 1
    assert stored[0].hex == "abc123"
    assert stored[0].flight == "BAW117"
    assert stored[0].squawk == "7700"
    assert stored[0].alt_baro == 12000


def test_alt_baro_ground_is_stored_as_zero_feet() -> None:
    found = position_from_aircraft({"hex": "4ca918", "alt_baro": "ground"}, ts=T0, store_raw=False)
    assert found is not None
    assert found.alt_baro == 0.0


def test_raw_json_preserves_the_original_entry() -> None:
    entry = {"hex": "4ca918", "alt_baro": "ground", "nav_modes": ["NAV"], "mlat": ["a"]}
    found = position_from_aircraft(entry, ts=T0)
    assert found is not None and found.raw_json is not None
    assert json.loads(found.raw_json) == entry


def test_raw_json_can_be_disabled() -> None:
    found = position_from_aircraft({"hex": "4ca918"}, ts=T0, store_raw=False)
    assert found is not None
    assert found.raw_json is None


def test_entries_without_an_hex_are_skipped() -> None:
    doc = {"aircraft": [{"flight": "GHOST"}, {"hex": "abc123"}, "not-even-a-dict"]}
    rows = positions_from_snapshot(doc, ts=T0, store_raw=False)
    assert [row.hex for row in rows] == ["abc123"]


def test_snapshots_without_an_aircraft_array_yield_nothing() -> None:
    assert positions_from_snapshot({"now": T0}, ts=T0) == []
    assert positions_from_snapshot({"aircraft": "oops"}, ts=T0) == []


def test_hex_is_lowercased_and_flight_stripped() -> None:
    found = position_from_aircraft({"hex": " 4CA918 ", "flight": "  RYR22GK "}, ts=T0)
    assert found is not None
    assert found.hex == "4ca918"
    assert found.flight == "RYR22GK"


def test_bad_field_types_are_dropped_not_fatal() -> None:
    found = position_from_aircraft(
        {
            "hex": "abcdef",
            "lat": "north",
            "lon": [1],
            "gs": True,  # a bool is not a speed
            "rssi": None,
            "squawk": 1200,
        },
        ts=T0,
    )
    assert found is not None
    assert found.lat is None
    assert found.lon is None
    assert found.gs is None
    assert found.rssi is None
    assert found.squawk is None  # a numeric squawk is not a 4-digit string


def test_alt_baro_that_is_neither_number_nor_ground_is_dropped() -> None:
    found = position_from_aircraft({"hex": "abcdef", "alt_baro": "high"}, ts=T0, store_raw=False)
    assert found is not None
    assert found.alt_baro is None


# ------------------------------------------------------------- flights ---


def test_insert_positions_derives_the_flight_row(conn: Any) -> None:
    insert_positions(
        conn,
        [
            position(ts=T0, hex="aaaaaa", flight="AAA111", alt_baro=1000, rssi=-10.0),
            position(ts=T0 + 60, hex="aaaaaa", flight="AAB222", alt_baro=5000, rssi=-6.0),
            position(ts=T0 + 30, hex="aaaaaa", flight="AAA111", alt_baro=3000, rssi=-12.0),
        ],
    )
    info = get_flight(conn, "aaaaaa")
    assert info is not None
    assert info.first_seen == T0
    assert info.last_seen == T0 + 60
    assert info.max_alt == 5000
    assert info.min_rssi == -12.0
    assert info.callsigns == ("AAA111", "AAB222")


def test_flight_without_callsigns_has_an_empty_tuple(conn: Any) -> None:
    insert_positions(conn, [position(hex="bbbbbb", flight=None)])
    info = get_flight(conn, "bbbbbb")
    assert info is not None
    assert info.callsigns == ()


def test_get_flight_for_unknown_hex_is_none(conn: Any) -> None:
    assert get_flight(conn, "ffffff") is None


def test_full_refresh_drops_flights_whose_rows_are_gone(conn: Any) -> None:
    insert_positions(conn, [position(hex="cccccc")])
    assert get_flight(conn, "cccccc") is not None
    conn.execute("DELETE FROM positions")
    refresh_flights(conn)
    assert get_flight(conn, "cccccc") is None


def test_partial_refresh_only_touches_named_hexes(conn: Any) -> None:
    insert_positions(
        conn,
        [position(ts=T0, hex="aaaaaa", flight="AAA111"), position(ts=T0, hex="bbbbbb")],
    )
    conn.execute("UPDATE positions SET flight = 'ZZZ999' WHERE hex = 'aaaaaa'")
    refresh_flights(conn, hexes=["aaaaaa"])
    alpha = get_flight(conn, "aaaaaa")
    bravo = get_flight(conn, "bbbbbb")
    assert alpha is not None and alpha.callsigns == ("ZZZ999",)
    assert bravo is not None and bravo.callsigns == ("RYR22GK",)


def test_partial_refresh_drops_a_hex_that_has_no_rows_left(conn: Any) -> None:
    insert_positions(conn, [position(hex="cccccc")])
    assert get_flight(conn, "cccccc") is not None
    conn.execute("DELETE FROM positions WHERE hex = 'cccccc'")
    refresh_flights(conn, hexes=["cccccc"])
    assert get_flight(conn, "cccccc") is None
    # and a hex that never existed is simply a no-op
    refresh_flights(conn, hexes=["000000"])


# ----------------------------------------------------------- retention ---


def test_prune_removes_old_rows_and_keeps_recent_ones(conn: Any) -> None:
    now = time.time()
    insert_positions(
        conn,
        [
            position(ts=now - 10 * 86400, hex="old111"),
            position(ts=now - 60, hex="new111"),
        ],
    )
    assert prune(conn, 7, now=now) == 1
    assert _hexes(conn) == {"new111"}


def test_prune_is_idempotent(conn: Any) -> None:
    now = time.time()
    insert_positions(conn, [position(ts=now - 10 * 86400, hex="old111")])
    assert prune(conn, 7, now=now) == 1
    assert prune(conn, 7, now=now) == 0


def test_prune_with_zero_days_is_a_noop(conn: Any) -> None:
    insert_positions(conn, [position(ts=0.0)])
    assert prune(conn, 0, now=time.time()) == 0
    assert prune(conn, -3, now=time.time()) == 0
    assert _count(conn) == 1


def test_prune_rebuilds_derived_aggregates(conn: Any) -> None:
    now = time.time()
    insert_positions(
        conn,
        [
            position(ts=now - 10 * 86400, hex="aaaaaa", alt_baro=41000),
            position(ts=now - 60, hex="aaaaaa", alt_baro=1000),
        ],
    )
    before = get_flight(conn, "aaaaaa")
    assert before is not None and before.max_alt == 41000
    assert prune(conn, 7, now=now) == 1
    after = get_flight(conn, "aaaaaa")
    assert after is not None
    assert after.max_alt == 1000
    assert after.first_seen == now - 60


# -------------------------------------------------------------- track ---


def test_track_reconstructs_the_path_in_time_order(conn: Any) -> None:
    insert_positions(
        conn,
        [
            position(ts=T0 + 10, hex="aaaaaa", lat=52.0, lon=-1.0),
            position(ts=T0, hex="aaaaaa", lat=51.0, lon=-0.5),
            position(ts=T0 + 5, hex="bbbbbb", lat=51.5, lon=-0.6),
        ],
    )
    rows = track(conn, "aaaaaa")
    assert [row.ts for row in rows] == [T0, T0 + 10]
    assert [row.lat for row in rows] == [51.0, 52.0]


def test_track_skips_rows_without_a_position(conn: Any) -> None:
    insert_positions(
        conn,
        [position(lat=None, lon=None), position(ts=T0 + 1, lat=51.0, lon=0.0)],
    )
    rows = track(conn, "4ca918")
    assert len(rows) == 1
    assert rows[0].lat == 51.0


def test_track_honours_the_row_limit(conn: Any) -> None:
    insert_positions(
        conn,
        [position(ts=T0 + i, lat=51.0 + i * 0.01, lon=0.0) for i in range(5)],
    )
    rows = track(conn, "4ca918", limit=2)
    assert [row.ts for row in rows] == [T0, T0 + 1]


def test_track_of_an_unknown_hex_is_empty(conn: Any) -> None:
    assert track(conn, "000000") == []


# --------------------------------------------------------------- near ---


def test_near_includes_points_inside_the_radius_sorted_by_distance(conn: Any) -> None:
    insert_positions(
        conn,
        [
            position(ts=T0, hex="far001", lat=51.5, lon=0.0),  # ~30 nm
            position(ts=T0 + 1, hex="near01", lat=51.05, lon=0.0),  # ~3 nm
            position(ts=T0 + 2, hex="off001", lat=51.0, lon=3.0),  # far east
        ],
    )
    matches = near(conn, 51.0, 0.0, 10.0)
    assert [point.position.hex for point in matches] == ["near01"]
    assert matches[0].dist_nm == pytest.approx(3.0, abs=0.05)


def test_near_radius_boundary_is_a_haversine_test(conn: Any) -> None:
    insert_positions(conn, [position(lat=51.1, lon=0.0)])  # exactly ~6 nm north
    assert near(conn, 51.0, 0.0, 5.0) == []
    inside = near(conn, 51.0, 0.0, 7.0)
    assert len(inside) == 1
    assert inside[0].dist_nm == pytest.approx(6.0, abs=0.05)


def test_near_ignores_rows_without_a_position(conn: Any) -> None:
    insert_positions(conn, [position(lat=None, lon=None)])
    assert near(conn, 51.0, 0.0, 500.0) == []


def test_near_since_window_and_limit(conn: Any) -> None:
    insert_positions(
        conn,
        [position(ts=T0, lat=51.01, lon=0.0), position(ts=T0 + 100, lat=51.02, lon=0.0)],
    )
    windowed = near(conn, 51.0, 0.0, 10.0, since=T0 + 50)
    assert [point.position.ts for point in windowed] == [T0 + 100]
    assert len(near(conn, 51.0, 0.0, 10.0, limit=1)) == 1


def test_near_is_independent_of_query_direction(conn: Any) -> None:
    insert_positions(conn, [position(lat=51.0, lon=1.0)])
    southbound = near(conn, 51.5, 1.0, 60.0)
    northbound = near(conn, 50.5, 1.0, 60.0)
    assert southbound and northbound
    assert southbound[0].dist_nm == pytest.approx(30.0, abs=0.5)
    assert northbound[0].dist_nm == pytest.approx(30.0, abs=0.5)


# ------------------------------------------------------------ summary ---


def test_summary_counts_rows_days_and_aircraft(conn: Any) -> None:
    insert_positions(
        conn,
        [
            position(ts=T0, hex="aaaaaa"),
            position(ts=T0 + 60, hex="aaaaaa"),
            position(ts=T0 + 86400, hex="bbbbbb"),
        ],
    )
    data = summary(conn)
    assert data.total_rows == 3
    assert data.aircraft == 2
    assert data.first_ts == T0
    assert data.last_ts == T0 + 86400
    assert [day.date for day in data.days] == ["2026-10-03", "2026-10-04"]
    assert [day.rows for day in data.days] == [2, 1]
    assert [day.aircraft for day in data.days] == [1, 1]


def test_summary_ranks_top_aircraft_by_sightings(conn: Any) -> None:
    insert_positions(
        conn,
        [
            position(ts=T0, hex="aaaaaa", flight="AAA111"),
            position(ts=T0 + 1, hex="aaaaaa", flight="AAA111"),
            position(ts=T0 + 2, hex="aaaaaa", flight="AAB222"),
            position(ts=T0 + 3, hex="bbbbbb", flight="BBB333"),
        ],
    )
    data = summary(conn, top=1)
    assert len(data.top) == 1
    leader = data.top[0]
    assert leader.hex == "aaaaaa"
    assert leader.rows == 3
    assert leader.callsigns == "AAA111,AAB222"
    assert leader.first_seen == T0
    assert leader.last_seen == T0 + 2


def test_summary_of_an_empty_database(conn: Any) -> None:
    data = summary(conn)
    assert data.total_rows == 0
    assert data.aircraft == 0
    assert data.first_ts is None
    assert data.last_ts is None
    assert data.days == ()
    assert data.top == ()


# --------------------------------------------------------- export query ---


def test_iter_positions_filters_hex_time_and_limit(conn: Any) -> None:
    insert_positions(
        conn,
        [
            position(ts=T0, hex="aaaaaa"),
            position(ts=T0 + 10, hex="aaaaaa"),
            position(ts=T0 + 20, hex="bbbbbb"),
        ],
    )
    assert [row.ts for row in iter_positions(conn)] == [T0, T0 + 10, T0 + 20]
    assert [row.hex for row in iter_positions(conn, hex_code="AAAAAA")] == ["aaaaaa", "aaaaaa"]
    assert [row.ts for row in iter_positions(conn, since=T0 + 5, until=T0 + 15)] == [T0 + 10]
    assert len(list(iter_positions(conn, limit=1))) == 1
    assert list(iter_positions(conn, since=T0 + 1000)) == []


def test_insert_positions_of_an_empty_batch_is_a_noop(conn: Any) -> None:
    assert insert_positions(conn, []) == 0
    assert _count(conn) == 0


def test_snapshot_document_can_be_inserted_whole(conn: Any) -> None:
    rows = positions_from_snapshot(snapshot(T0), ts=T0, store_raw=True)
    assert insert_positions(conn, rows) == 1
    raw = conn.execute("SELECT raw_json FROM positions").fetchone()[0]
    assert json.loads(raw)["hex"] == "4ca918"
