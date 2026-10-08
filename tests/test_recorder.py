"""Recording loop tests: polling, clock choice, retention scheduling, raw rows."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from conftest import (
    T0,
    FakeClock,
    aircraft,
    decoder_opener,
    network_error,
    position,
    raw_opener,
    snapshot,
)
from dump1090_recorder.db import insert_positions, open_db, track
from dump1090_recorder.errors import RecorderError
from dump1090_recorder.fetch import fetch_aircraft, resolve_url
from dump1090_recorder.recorder import record, snapshot_ts


def no_sleep(_seconds: float) -> None:
    """A sleep that never sleeps: the suite runs in milliseconds."""


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("http://localhost:8080", "http://localhost:8080/data/aircraft.json"),
        ("http://readsb.lan:8080/", "http://readsb.lan:8080/data/aircraft.json"),
        ("http://readsb.lan:8080/data/aircraft.json", "http://readsb.lan:8080/data/aircraft.json"),
        ("http://decoder.local/custom.json", "http://decoder.local/custom.json"),
    ],
)
def test_resolve_url_appends_the_dump1090_document_path(given: str, expected: str) -> None:
    assert resolve_url(given) == expected


def _hexes(db: Path) -> set[str]:
    connection = open_db(db)
    try:
        return {str(row[0]) for row in connection.execute("SELECT DISTINCT hex FROM positions")}
    finally:
        connection.close()


def _track(db: Path, hex_code: str) -> list[Any]:
    connection = open_db(db)
    try:
        return track(connection, hex_code)
    finally:
        connection.close()


def test_record_polls_and_stores_positions(tmp_path: Path) -> None:
    db = tmp_path / "live.sqlite3"
    slept: list[float] = []
    stats = record(
        "http://decoder:8080",
        db=str(db),
        interval=1.0,
        max_polls=2,
        opener=decoder_opener(snapshot(T0)),
        sleep=slept.append,
    )
    assert stats.snapshots == 2
    assert stats.rows == 2
    assert stats.prunes == 0
    assert slept == [1.0]
    rows = _track(db, "4ca918")
    assert len(rows) == 2
    assert rows[0].ts == T0


def test_record_prefers_the_decoders_own_clock(tmp_path: Path) -> None:
    db = tmp_path / "decoder-clock.sqlite3"
    stats = record(
        "http://decoder:8080",
        db=str(db),
        max_polls=1,
        opener=decoder_opener({"now": T0 + 42, "aircraft": [aircraft()]}),
        sleep=no_sleep,
    )
    assert stats.rows == 1
    assert _track(db, "4ca918")[0].ts == T0 + 42


def test_record_falls_back_to_the_wall_clock(tmp_path: Path) -> None:
    db = tmp_path / "wall-clock.sqlite3"
    record(
        "http://decoder:8080",
        db=str(db),
        max_polls=1,
        opener=decoder_opener({"aircraft": [aircraft()]}),
        sleep=no_sleep,
        now=lambda: 1234.5,
    )
    assert _track(db, "4ca918")[0].ts == 1234.5


def test_record_survives_a_decoder_that_is_down(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "down.sqlite3"
    stats = record(
        "http://decoder:8080",
        db=str(db),
        max_polls=2,
        opener=decoder_opener(error=network_error()),
        sleep=no_sleep,
    )
    assert stats.snapshots == 0
    assert stats.rows == 0
    assert capsys.readouterr().err.count("warning:") == 2
    assert _hexes(db) == set()


def test_record_stores_raw_json_by_default(tmp_path: Path) -> None:
    db = tmp_path / "raw.sqlite3"
    record(
        "http://decoder:8080",
        db=str(db),
        max_polls=1,
        opener=decoder_opener(snapshot(T0)),
        sleep=no_sleep,
    )
    connection = open_db(db)
    try:
        raw = connection.execute("SELECT raw_json FROM positions").fetchone()[0]
    finally:
        connection.close()
    assert json.loads(raw)["hex"] == "4ca918"


def test_record_can_drop_raw_json(tmp_path: Path) -> None:
    db = tmp_path / "slim.sqlite3"
    record(
        "http://decoder:8080",
        db=str(db),
        max_polls=1,
        store_raw=False,
        opener=decoder_opener(snapshot(T0)),
        sleep=no_sleep,
    )
    connection = open_db(db)
    try:
        values = [row[0] for row in connection.execute("SELECT raw_json FROM positions")]
    finally:
        connection.close()
    assert values == [None]


def test_record_prunes_on_start(tmp_path: Path) -> None:
    db = tmp_path / "prune-start.sqlite3"
    wall = time.time()
    connection = open_db(db)
    insert_positions(
        connection,
        [
            position(ts=wall - 10 * 86400, hex="old111"),
            position(ts=wall - 6 * 86400, hex="oldish"),
            position(ts=wall, hex="new111"),
        ],
    )
    connection.close()
    stats = record(
        "http://decoder:8080",
        db=str(db),
        max_polls=1,
        retention_days=7,
        opener=decoder_opener(snapshot(T0)),
        sleep=no_sleep,
    )
    assert stats.prunes == 1
    assert "old111" not in _hexes(db)
    assert "oldish" in _hexes(db)
    assert "new111" in _hexes(db)


def test_record_prunes_hourly_while_running(tmp_path: Path) -> None:
    db = tmp_path / "prune-hourly.sqlite3"
    start = 1_800_000_000.0
    clock = FakeClock(start, step=3700.0)
    connection = open_db(db)
    insert_positions(
        connection,
        [
            position(ts=start - 86400 + 3600, hex="stale1"),  # survives the start prune
            position(ts=start - 60, hex="fresh1"),
        ],
    )
    connection.close()
    stats = record(
        "http://decoder:8080",
        db=str(db),
        max_polls=2,
        retention_days=1,
        opener=decoder_opener({"now": start, "aircraft": [aircraft()]}),
        sleep=no_sleep,
        now=clock,
    )
    assert stats.prunes == 2
    assert clock.calls >= 2
    hexes = _hexes(db)
    assert "stale1" not in hexes  # pruned by the hourly pass, not the start pass
    assert "fresh1" in hexes
    assert "4ca918" in hexes


def test_record_without_retention_never_prunes(tmp_path: Path) -> None:
    db = tmp_path / "no-prune.sqlite3"
    connection = open_db(db)
    insert_positions(connection, [position(ts=1.0, hex="ancient")])
    connection.close()
    stats = record(
        "http://decoder:8080",
        db=str(db),
        max_polls=2,
        opener=decoder_opener(snapshot(T0)),
        sleep=no_sleep,
        now=FakeClock(1_800_000_000.0, step=7200.0),
    )
    assert stats.prunes == 0
    assert "ancient" in _hexes(db)


def test_record_stops_cleanly_on_ctrl_c(tmp_path: Path, capsys: Any) -> None:
    db = tmp_path / "ctrlc.sqlite3"

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    stats = record(
        "http://decoder:8080",
        db=str(db),
        opener=decoder_opener(snapshot(T0)),
        sleep=interrupted,
    )
    assert stats.snapshots == 1
    assert "# stopped" in capsys.readouterr().err


def test_snapshot_ts_prefers_a_numeric_decoder_now() -> None:
    assert snapshot_ts({"now": T0}, wall=lambda: 0.0) == T0
    assert snapshot_ts({"now": 5}, wall=lambda: 0.0) == 5.0


def test_snapshot_ts_falls_back_to_the_wall_clock() -> None:
    assert snapshot_ts({}, wall=lambda: 99.0) == 99.0
    assert snapshot_ts({"now": "later"}, wall=lambda: 99.0) == 99.0
    assert snapshot_ts({"now": True}, wall=lambda: 99.0) == 99.0  # bool is not a clock


def test_fetch_aircraft_reads_a_valid_document() -> None:
    payload = fetch_aircraft("http://decoder:8080", opener=decoder_opener(snapshot(T0)))
    assert payload["now"] == T0
    assert isinstance(payload["aircraft"], list)


def test_fetch_aircraft_rejects_a_non_json_body() -> None:
    with pytest.raises(RecorderError, match="did not return JSON"):
        fetch_aircraft("http://decoder:8080", opener=raw_opener(b"<html>nope</html>"))


def test_fetch_aircraft_rejects_a_non_object_body() -> None:
    with pytest.raises(RecorderError, match="expected an object"):
        fetch_aircraft("http://decoder:8080", opener=raw_opener(b"[1, 2, 3]"))


def test_fetch_aircraft_rejects_a_missing_aircraft_array() -> None:
    with pytest.raises(RecorderError, match="no 'aircraft' array"):
        fetch_aircraft("http://decoder:8080", opener=raw_opener(b'{"now": 1}'))


def test_fetch_aircraft_reports_network_failures() -> None:
    with pytest.raises(RecorderError, match="cannot read"):
        fetch_aircraft("http://decoder:8080", opener=decoder_opener(error=network_error()))
