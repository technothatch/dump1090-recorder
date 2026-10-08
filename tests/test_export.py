"""CSV, GeoJSON and NDJSON export tests."""

from __future__ import annotations

import csv
import io
import json

import pytest

from conftest import T0, position
from dump1090_recorder.db import Position
from dump1090_recorder.errors import RecorderError
from dump1090_recorder.export import (
    CSV_COLUMNS,
    export_csv,
    export_geojson,
    export_ndjson,
    export_rows,
)


def _rows() -> list[Position]:
    return [
        position(ts=T0, hex="aaaaaa", flight="AAA111", lat=51.0, lon=-0.5),
        position(
            ts=T0,
            hex="bbbbbb",
            flight=None,
            lat=None,
            lon=None,
            alt_baro=None,
            gs=None,
            track=None,
            squawk=None,
            rssi=None,
            seen_pos=None,
        ),
        position(ts=T0 + 5, hex="aaaaaa", flight="AAA111", lat=51.1, lon=-0.4),
    ]


def test_csv_writes_header_then_one_line_per_row() -> None:
    out = io.StringIO()
    assert export_csv(_rows(), out) == 3
    lines = out.getvalue().splitlines()
    assert lines[0] == ",".join(CSV_COLUMNS)
    assert len(lines) == 4
    assert lines[1].startswith(f"{T0},aaaaaa,AAA111,51.0,-0.5")


def test_csv_renders_missing_fields_as_empty_cells() -> None:
    out = io.StringIO()
    export_csv(
        [
            position(
                ts=T0,
                hex="abcdef",
                flight=None,
                lat=None,
                lon=None,
                alt_baro=None,
                gs=None,
                track=None,
                squawk=None,
                rssi=None,
                seen_pos=None,
            )
        ],
        out,
    )
    body = out.getvalue().splitlines()[1]
    fields = next(csv.reader(io.StringIO(body)))
    assert len(fields) == len(CSV_COLUMNS)
    assert fields[1] == "abcdef"
    assert fields[2:] == [""] * (len(CSV_COLUMNS) - 2)


def test_csv_every_line_has_the_same_field_count() -> None:
    out = io.StringIO()
    export_csv(_rows(), out)
    table = list(csv.reader(io.StringIO(out.getvalue())))
    assert all(len(line) == len(CSV_COLUMNS) for line in table)
    assert len(table) == 4


def test_geojson_builds_a_feature_collection_of_points() -> None:
    out = io.StringIO()
    assert export_geojson(_rows(), out) == 2  # the position-less row is skipped
    document = json.loads(out.getvalue())
    assert document["type"] == "FeatureCollection"
    assert len(document["features"]) == 2
    feature = document["features"][0]
    assert feature["type"] == "Feature"
    assert feature["geometry"] == {"type": "Point", "coordinates": [-0.5, 51.0]}
    assert feature["properties"]["hex"] == "aaaaaa"
    assert feature["properties"]["ts"] == T0
    assert feature["properties"]["squawk"] == "1200"


def test_geojson_of_only_positionless_rows_is_an_empty_collection() -> None:
    out = io.StringIO()
    assert export_geojson([position(lat=None, lon=None)], out) == 0
    document = json.loads(out.getvalue())
    assert document == {"type": "FeatureCollection", "features": []}


def test_ndjson_groups_rows_into_snapshot_records() -> None:
    out = io.StringIO()
    assert export_ndjson(_rows(), out) == 2
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [record["t"] for record in records] == [T0, T0 + 5]
    assert set(records[0]) == {"t", "aircraft"}
    assert len(records[0]["aircraft"]) == 2
    assert records[1]["aircraft"] == [
        {
            "hex": "aaaaaa",
            "flight": "AAA111",
            "lat": 51.1,
            "lon": -0.4,
            "alt_baro": 34000.0,
            "gs": 458.0,
            "track": 210.5,
            "squawk": "1200",
            "rssi": -4.2,
            "seen_pos": 0.7,
        }
    ]


def test_ndjson_omits_fields_that_were_never_recorded() -> None:
    out = io.StringIO()
    export_ndjson(
        [
            position(
                ts=T0,
                hex="abcdef",
                flight=None,
                lat=None,
                lon=None,
                alt_baro=None,
                gs=None,
                track=None,
                squawk=None,
                rssi=None,
                seen_pos=None,
            )
        ],
        out,
    )
    assert json.loads(out.getvalue()) == {"t": T0, "aircraft": [{"hex": "abcdef"}]}


def test_ndjson_of_an_empty_selection_writes_nothing() -> None:
    out = io.StringIO()
    assert export_ndjson([], out) == 0
    assert out.getvalue() == ""


@pytest.mark.parametrize("fmt", ["csv", "geojson", "ndjson"])
def test_export_rows_dispatches_every_documented_format(fmt: str) -> None:
    out = io.StringIO()
    assert export_rows(_rows(), fmt, out) > 0
    assert out.getvalue()


def test_export_rows_rejects_an_unknown_format() -> None:
    with pytest.raises(RecorderError, match="unknown export format"):
        export_rows([], "xml", io.StringIO())
