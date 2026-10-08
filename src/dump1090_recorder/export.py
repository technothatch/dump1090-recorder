"""Export recorded positions as CSV, GeoJSON or NDJSON.

NDJSON uses the ecosystem-wide convention shared with dump1090-replay and
dump1090-influx — one JSON object per line::

    {"t": <epoch>, "aircraft": [...]}

``t`` is the snapshot timestamp (the decoder's ``now`` when it was recorded)
and ``aircraft`` holds the stored fields for every row captured in that poll,
so a recording exported here loads in dump1090-replay / dump1090-influx and
vice versa.
"""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from itertools import groupby
from typing import Any, TextIO

from dump1090_recorder.db import Position
from dump1090_recorder.errors import RecorderError

__all__ = ["EXPORT_FORMATS", "export_csv", "export_geojson", "export_ndjson", "export_rows"]

EXPORT_FORMATS: tuple[str, ...] = ("csv", "geojson", "ndjson")

CSV_COLUMNS: tuple[str, ...] = (
    "ts",
    "hex",
    "flight",
    "lat",
    "lon",
    "alt_baro",
    "gs",
    "track",
    "squawk",
    "rssi",
    "seen_pos",
)


def _cell(value: Any) -> Any:
    return "" if value is None else value


def export_csv(rows: Iterable[Position], out: TextIO) -> int:
    """Write a header row plus one line per position. Returns rows written."""
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    count = 0
    for row in rows:
        writer.writerow(
            [
                row.ts,
                row.hex,
                _cell(row.flight),
                _cell(row.lat),
                _cell(row.lon),
                _cell(row.alt_baro),
                _cell(row.gs),
                _cell(row.track),
                _cell(row.squawk),
                _cell(row.rssi),
                _cell(row.seen_pos),
            ]
        )
        count += 1
    return count


def export_geojson(rows: Iterable[Position], out: TextIO) -> int:
    """Write a FeatureCollection of Point features. Returns features written.

    Rows without a stored position have no geometry and are skipped.
    """
    features: list[dict[str, Any]] = []
    for row in rows:
        if row.lat is None or row.lon is None:
            continue
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [row.lon, row.lat]},
                "properties": {
                    "ts": row.ts,
                    "hex": row.hex,
                    "flight": row.flight,
                    "alt_baro": row.alt_baro,
                    "gs": row.gs,
                    "track": row.track,
                    "squawk": row.squawk,
                    "rssi": row.rssi,
                    "seen_pos": row.seen_pos,
                },
            }
        )
    document = {"type": "FeatureCollection", "features": features}
    out.write(json.dumps(document, separators=(",", ":")) + "\n")
    return len(features)


def _aircraft_dict(row: Position) -> dict[str, Any]:
    entry: dict[str, Any] = {"hex": row.hex}
    optional = {
        "flight": row.flight,
        "lat": row.lat,
        "lon": row.lon,
        "alt_baro": row.alt_baro,
        "gs": row.gs,
        "track": row.track,
        "squawk": row.squawk,
        "rssi": row.rssi,
        "seen_pos": row.seen_pos,
    }
    entry.update({key: value for key, value in optional.items() if value is not None})
    return entry


def export_ndjson(rows: Iterable[Position], out: TextIO) -> int:
    """Write ``{"t": <epoch>, "aircraft": [...]}`` records. Returns records written.

    Rows are grouped by snapshot timestamp (input must be time-ordered, which
    :func:`dump1090_recorder.db.iter_positions` guarantees).
    """
    count = 0
    for ts, group in groupby(rows, key=lambda row: row.ts):
        record = {"t": ts, "aircraft": [_aircraft_dict(row) for row in group]}
        out.write(json.dumps(record, separators=(",", ":")) + "\n")
        count += 1
    return count


def export_rows(rows: Iterable[Position], fmt: str, out: TextIO) -> int:
    """Dispatch to one of :data:`EXPORT_FORMATS`.

    Raises:
        RecorderError: The format name is unknown.
    """
    if fmt == "csv":
        return export_csv(rows, out)
    if fmt == "geojson":
        return export_geojson(rows, out)
    if fmt == "ndjson":
        return export_ndjson(rows, out)
    raise RecorderError(f"unknown export format {fmt!r} (expected {', '.join(EXPORT_FORMATS)})")
