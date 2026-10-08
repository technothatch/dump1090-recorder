"""CLI tests: argument handling, every subcommand end to end, exit codes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import T0, decoder_opener, network_error, position, snapshot
from dump1090_recorder.cli import main
from dump1090_recorder.db import insert_positions, open_db

CSV_HEADER = "ts,hex,flight,lat,lon,alt_baro,gs,track,squawk,rssi,seen_pos"


def _seed(db: Path) -> None:
    """A small, realistic history: two hexes, two days, one position-less row."""
    connection = open_db(db)
    insert_positions(
        connection,
        [
            position(ts=T0, hex="4ca918", flight="RYR22GK", lat=51.47, lon=-0.45),
            position(ts=T0 + 60, hex="4ca918", flight="RYR22GK", lat=51.50, lon=-0.40),
            position(ts=T0 + 120, hex="4ca918", flight="RYR22GK", lat=51.55, lon=-0.35),
            position(ts=T0 + 30, hex="40621d", flight="BAW117", lat=52.5, lon=-1.0),
            position(ts=T0 + 86400, hex="3c673a", flight="DLH441", lat=48.0, lon=11.0),
            position(ts=T0 + 200, hex="4ca918", flight="RYR22GK", lat=None, lon=None),
        ],
    )
    connection.close()


def _counts(db: Path) -> dict[str, int]:
    connection = open_db(db)
    try:
        return {
            str(row[0]): int(row[1])
            for row in connection.execute("SELECT hex, COUNT(*) FROM positions GROUP BY hex")
        }
    finally:
        connection.close()


# ------------------------------------------------------------ arguments ---


def test_version_prints_and_exits_zero(capsys: Any) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "dump1090-recorder 0.1.0" in capsys.readouterr().out


def test_missing_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([])
    assert excinfo.value.code == 2


def test_unknown_subcommand_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["fly"])
    assert excinfo.value.code == 2


def test_unknown_flag_is_a_usage_error(db_file: Path) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["summary", "--verbose", "--db", str(db_file)])
    assert excinfo.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        ["near", "91", "0", "10"],  # latitude out of range
        ["near", "51", "181", "10"],  # longitude out of range
        ["near", "51", "0", "0"],  # radius must be positive
        ["near", "51", "0", "far"],  # radius must be a number
        ["export", "--format", "xml"],  # not a documented format
        ["prune"],  # --retention-days is required
        ["summary", "--top", "0"],  # --top must be positive
        ["summary", "--top", "many"],  # --top must be an integer
        ["record", "--interval", "-1"],  # interval cannot be negative
        ["record", "--max-polls", "-1"],  # poll count cannot be negative
        ["record", "--max-polls", "1.5"],  # poll count must be an integer
        ["near", "51", "0", "10", "--since", "-5"],  # timestamps are epoch seconds
        ["track"],  # HEX is required
    ],
)
def test_bad_arguments_exit_two(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 2


# ------------------------------------------------------------- summary ---


def test_summary_on_a_fresh_database(db_file: Path, capsys: Any) -> None:
    assert main(["--db", str(db_file), "summary"]) == 0
    out = capsys.readouterr().out
    assert "positions: 0 row(s) across 0 aircraft" in out
    assert "per-day totals" not in out
    assert db_file.is_file()


def test_summary_reports_days_messages_and_top_aircraft(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    assert main(["summary", "--db", str(db_file), "--top", "1"]) == 0
    out = capsys.readouterr().out
    assert "positions: 6 row(s) across 3 aircraft over 2 day(s)" in out
    assert "2026-10-03" in out
    assert "2026-10-04" in out
    assert "top 1 aircraft by sightings" in out
    assert "4ca918" in out
    assert "RYR22GK" in out


def test_db_path_can_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    db = tmp_path / "from-env.sqlite3"
    open_db(db).close()
    monkeypatch.setenv("DUMP1090_RECORDER_DB", str(db))
    assert main(["summary"]) == 0
    assert "positions: 0 row(s)" in capsys.readouterr().out


def test_default_database_path_is_relative_to_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DUMP1090_RECORDER_DB", raising=False)
    assert main(["summary"]) == 0
    assert "database: dump1090.sqlite3" in capsys.readouterr().out
    assert (tmp_path / "dump1090.sqlite3").is_file()


# --------------------------------------------------------------- track ---


def test_track_prints_the_reconstructed_path(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    assert main(["track", "4ca918", "--db", str(db_file)]) == 0
    out = capsys.readouterr().out
    assert "track 4ca918: 3 position(s)" in out
    assert "callsigns: RYR22GK" in out
    assert "time (UTC)" in out
    assert out.count("2026-10-03") == 3  # one line per positioned row


def test_track_accepts_an_uppercase_hex(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    assert main(["track", "4CA918", "--db", str(db_file)]) == 0
    assert "track 4ca918:" in capsys.readouterr().out


def test_track_limit_keeps_the_oldest_rows(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    assert main(["track", "4ca918", "--limit", "2", "--db", str(db_file)]) == 0
    out = capsys.readouterr().out
    assert "track 4ca918: 2 position(s)" in out


def test_track_of_an_unknown_hex_exits_one(db_file: Path, capsys: Any) -> None:
    assert main(["track", "ffffff", "--db", str(db_file)]) == 1
    assert "no positions recorded for ffffff" in capsys.readouterr().err


# ---------------------------------------------------------------- near ---


def test_near_with_no_matches_still_succeeds(db_file: Path, capsys: Any) -> None:
    assert main(["near", "51.0", "0.0", "1", "--db", str(db_file)]) == 0
    out = capsys.readouterr().out
    assert "near 51.0000,0.0000 within 1 nm: 0 position(s)" in out
    assert "dist_nm" not in out


def test_near_filters_by_haversine_distance(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    # 51.47,-0.45 is ~3 nm from the query point; the 52.5 and 48.0 rows are not.
    assert main(["near", "51.47", "-0.45", "10", "--db", str(db_file)]) == 0
    out = capsys.readouterr().out
    assert "3 position(s)" in out
    assert "4ca918" in out
    assert "40621d" not in out
    assert "dist_nm" in out


def test_near_can_be_limited(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    assert main(["near", "51.47", "-0.45", "50", "--limit", "1", "--db", str(db_file)]) == 0
    assert "1 position(s)" in capsys.readouterr().out


def test_near_since_window_excludes_older_rows(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    assert (
        main(["near", "51.47", "-0.45", "10", "--since", str(T0 + 90), "--db", str(db_file)]) == 0
    )
    assert "1 position(s)" in capsys.readouterr().out


# -------------------------------------------------------------- export ---


def test_export_csv_to_stdout(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    assert main(["export", "--format", "csv", "--db", str(db_file)]) == 0
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert lines[0] == CSV_HEADER
    assert len(lines) == 7  # header + 6 rows
    assert "wrote 6 row(s) to stdout" in captured.err


def test_export_geojson_to_a_file(db_file: Path, tmp_path: Path, capsys: Any) -> None:
    _seed(db_file)
    target = tmp_path / "track.geojson"
    assert main(["export", "--format", "geojson", "--out", str(target), "--db", str(db_file)]) == 0
    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["type"] == "FeatureCollection"
    assert len(document["features"]) == 5  # the position-less row has no geometry
    assert document["features"][0]["geometry"]["type"] == "Point"
    assert "wrote 5 feature(s)" in capsys.readouterr().err


def test_export_ndjson_uses_the_shared_convention(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    assert main(["export", "--format", "ndjson", "--db", str(db_file)]) == 0
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines()]
    assert len(records) == 6  # six distinct snapshot timestamps
    assert set(records[0]) == {"t", "aircraft"}
    assert records[0]["t"] == T0
    assert records[0]["aircraft"][0]["hex"] == "4ca918"
    assert "wrote 6 record(s) to stdout" in captured.err


def test_export_can_filter_by_hex_and_time(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    assert (
        main(
            [
                "export",
                "--hex",
                "4CA918",
                "--since",
                str(T0 + 50),
                "--until",
                str(T0 + 150),
                "--db",
                str(db_file),
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    body = captured.out.splitlines()[1:]
    assert len(body) == 2
    assert all("4ca918" in line for line in body)
    assert "wrote 2 row(s) to stdout" in captured.err


def test_export_with_no_matches_exits_zero(db_file: Path, capsys: Any) -> None:
    assert main(["export", "--db", str(db_file)]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [CSV_HEADER]
    assert "wrote 0 row(s) to stdout" in captured.err


def test_export_to_an_unwritable_path_exits_one(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    missing = db_file.parent / "no-such-dir" / "out.csv"
    assert main(["export", "--out", str(missing), "--db", str(db_file)]) == 1
    assert "cannot write" in capsys.readouterr().err


# --------------------------------------------------------------- prune ---


def test_prune_command_deletes_old_rows(db_file: Path, capsys: Any) -> None:
    connection = open_db(db_file)
    insert_positions(
        connection,
        [
            position(ts=1.0, hex="ancient"),
            position(ts=1.0 + 86400, hex="also-ancient"),
        ],
    )
    connection.close()
    assert main(["prune", "--retention-days", "1", "--db", str(db_file)]) == 0
    assert "pruned 2 position(s)" in capsys.readouterr().out
    assert _counts(db_file) == {}


def test_prune_command_with_nothing_to_do(db_file: Path, capsys: Any) -> None:
    _seed(db_file)
    assert main(["prune", "--retention-days", "3650", "--db", str(db_file)]) == 0
    assert "pruned 0 position(s)" in capsys.readouterr().out
    assert sum(_counts(db_file).values()) == 6


# -------------------------------------------------------------- record ---


def test_record_command_polls_the_decoder(db_file: Path, capsys: Any) -> None:
    code = main(
        [
            "record",
            "--url",
            "http://decoder:8080",
            "--db",
            str(db_file),
            "--max-polls",
            "2",
            "--interval",
            "0",
        ],
        opener=decoder_opener(snapshot(T0)),
        sleep=lambda _seconds: None,
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "recording http://decoder:8080/data/aircraft.json every 0s" in err
    assert "recorded 2 snapshot(s), 2 position(s), 0 prune(s)" in err
    assert _counts(db_file) == {"4ca918": 2}


def test_record_command_exits_one_when_the_decoder_is_down(db_file: Path, capsys: Any) -> None:
    code = main(
        ["record", "--db", str(db_file), "--max-polls", "1"],
        opener=decoder_opener(error=network_error()),
        sleep=lambda _seconds: None,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "warning: " in err
    assert "recorded 0 snapshot(s)" in err


def test_record_command_url_comes_from_the_environment(
    db_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    monkeypatch.setenv("DUMP1090_URL", "http://env-decoder:8080/data/aircraft.json")
    seen: list[str] = []

    def opener(url: str, timeout: float = 0.0) -> Any:
        seen.append(url)
        return decoder_opener(snapshot(T0))(url)

    code = main(
        ["record", "--db", str(db_file), "--max-polls", "1"],
        opener=opener,
        sleep=lambda _seconds: None,
    )
    assert code == 0
    assert seen == ["http://env-decoder:8080/data/aircraft.json"]
    capsys.readouterr()


def test_record_command_no_store_raw(db_file: Path) -> None:
    assert (
        main(
            ["record", "--db", str(db_file), "--max-polls", "1", "--no-store-raw"],
            opener=decoder_opener(snapshot(T0)),
            sleep=lambda _seconds: None,
        )
        == 0
    )
    connection = open_db(db_file)
    try:
        values = [row[0] for row in connection.execute("SELECT raw_json FROM positions")]
    finally:
        connection.close()
    assert values == [None]


def test_record_command_applies_retention_on_start(db_file: Path, capsys: Any) -> None:
    connection = open_db(db_file)
    insert_positions(connection, [position(ts=1.0, hex="ancient")])
    connection.close()
    assert (
        main(
            [
                "record",
                "--db",
                str(db_file),
                "--max-polls",
                "1",
                "--retention-days",
                "7",
            ],
            opener=decoder_opener(snapshot(T0)),
            sleep=lambda _seconds: None,
        )
        == 0
    )
    assert "1 prune(s)" in capsys.readouterr().err
    assert "ancient" not in _counts(db_file)


def test_record_command_creates_a_missing_database(db_file: Path) -> None:
    assert not db_file.exists()
    assert (
        main(
            ["record", "--db", str(db_file), "--max-polls", "1"],
            opener=decoder_opener(snapshot(T0)),
            sleep=lambda _seconds: None,
        )
        == 0
    )
    assert db_file.is_file()
    assert _counts(db_file) == {"4ca918": 1}


def test_record_command_reports_an_unopenable_database(tmp_path: Path, capsys: Any) -> None:
    blocked = tmp_path / "no-such-dir" / "recorder.sqlite3"
    code = main(
        ["record", "--db", str(blocked), "--max-polls", "1"],
        opener=decoder_opener(snapshot(T0)),
        sleep=lambda _seconds: None,
    )
    assert code == 1
    assert "error: cannot open database" in capsys.readouterr().err
