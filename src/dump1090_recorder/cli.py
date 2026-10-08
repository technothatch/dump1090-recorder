"""Command-line interface for dump1090-recorder."""

from __future__ import annotations

import argparse
import datetime
import os
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TextIO

from dump1090_recorder import __version__
from dump1090_recorder.db import (
    DEFAULT_DB,
    get_flight,
    iter_positions,
    near,
    open_db,
    prune,
    summary,
    track,
)
from dump1090_recorder.errors import RecorderError
from dump1090_recorder.export import EXPORT_FORMATS, export_rows
from dump1090_recorder.fetch import DEFAULT_HOST, resolve_url
from dump1090_recorder.recorder import record

EXIT_OK = 0
EXIT_ERROR = 1

__all__ = ["EXIT_ERROR", "EXIT_OK", "main"]

_EXPORT_NOUNS = {"csv": "row(s)", "geojson": "feature(s)", "ndjson": "record(s)"}


def _float_arg(raw: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"'{raw}' is not a number") from exc


def _int_arg(raw: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"'{raw}' is not an integer") from exc


def _positive_float(raw: str) -> float:
    value = _float_arg(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {raw}")
    return value


def _non_negative_float(raw: str) -> float:
    value = _float_arg(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {raw}")
    return value


def _positive_int(raw: str) -> int:
    value = _int_arg(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {raw}")
    return value


def _non_negative_int(raw: str) -> int:
    value = _int_arg(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {raw}")
    return value


def _lat(raw: str) -> float:
    value = _float_arg(raw)
    if not -90.0 <= value <= 90.0:
        raise argparse.ArgumentTypeError(f"latitude must be between -90 and 90, got {raw}")
    return value


def _lon(raw: str) -> float:
    value = _float_arg(raw)
    if not -180.0 <= value <= 180.0:
        raise argparse.ArgumentTypeError(f"longitude must be between -180 and 180, got {raw}")
    return value


def _epoch(raw: str) -> float:
    value = _float_arg(raw)
    if value < 0:
        raise argparse.ArgumentTypeError(f"timestamp must be >= 0, got {raw}")
    return value


def _fmt_ts(epoch: float) -> str:
    moment = datetime.datetime.fromtimestamp(epoch, tz=datetime.UTC)
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def _cell(value: float | None, spec: str = ".1f") -> str:
    return "" if value is None else format(value, spec)


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    print("  " + "  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _db_path(args: argparse.Namespace) -> str:
    value = getattr(args, "db", None)
    if isinstance(value, str) and value:
        return value
    env = os.environ.get("DUMP1090_RECORDER_DB")
    if env:
        return env
    return DEFAULT_DB


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"SQLite database path (env DUMP1090_RECORDER_DB, default {DEFAULT_DB})",
    )
    parser = argparse.ArgumentParser(
        prog="dump1090-recorder",
        parents=[common],
        description="Long-term flight recorder for dump1090 / readsb aircraft.json (SQLite).",
        epilog="ADS-B data is public by design; nothing here contacts the network unless asked to.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", parents=[common], help="poll a decoder and append positions")
    rec.add_argument(
        "--url",
        metavar="URL",
        default=None,
        help=f"decoder base URL or aircraft.json URL (env DUMP1090_URL, default {DEFAULT_HOST})",
    )
    rec.add_argument(
        "--interval",
        type=_non_negative_float,
        default=1.0,
        metavar="SECS",
        help="seconds between polls (default: 1)",
    )
    rec.add_argument(
        "--max-polls",
        type=_non_negative_int,
        default=0,
        metavar="N",
        help="stop after N polls (0 = run until interrupted)",
    )
    rec.add_argument(
        "--timeout", type=_positive_float, default=5.0, metavar="SECS", help="HTTP timeout"
    )
    rec.add_argument(
        "--retention-days",
        type=_non_negative_int,
        default=0,
        metavar="N",
        help="prune rows older than N days on start and hourly (0 = keep forever)",
    )
    rec.add_argument(
        "--no-store-raw", action="store_true", help="do not store the raw aircraft JSON per row"
    )

    prunep = sub.add_parser(
        "prune", parents=[common], help="delete rows older than the retention window"
    )
    prunep.add_argument(
        "--retention-days",
        type=_positive_int,
        required=True,
        metavar="N",
        help="delete positions older than N days",
    )

    summ = sub.add_parser(
        "summary", parents=[common], help="aircraft/day totals, message counts, top aircraft"
    )
    summ.add_argument(
        "--top",
        type=_positive_int,
        default=10,
        metavar="N",
        help="rows in the top-aircraft table (default: 10)",
    )

    trk = sub.add_parser("track", parents=[common], help="reconstruct one aircraft's track")
    trk.add_argument("hex", metavar="HEX", help="ICAO address, e.g. 4ca918")
    trk.add_argument(
        "--limit", type=_positive_int, default=0, metavar="N", help="maximum rows (0 = all)"
    )

    nr = sub.add_parser("near", parents=[common], help="positions within a radius of a point")
    nr.add_argument("lat", type=_lat, metavar="LAT", help="latitude in degrees")
    nr.add_argument("lon", type=_lon, metavar="LON", help="longitude in degrees")
    nr.add_argument(
        "radius_nm", type=_positive_float, metavar="RADIUS_NM", help="search radius, nautical miles"
    )
    nr.add_argument(
        "--since",
        type=_epoch,
        default=None,
        metavar="EPOCH",
        help="only positions at or after this unix time",
    )
    nr.add_argument(
        "--limit", type=_positive_int, default=0, metavar="N", help="maximum rows (0 = all)"
    )

    exp = sub.add_parser("export", parents=[common], help="export positions as csv/geojson/ndjson")
    exp.add_argument(
        "--format",
        dest="fmt",
        choices=EXPORT_FORMATS,
        default="csv",
        help="output format (default: csv)",
    )
    exp.add_argument("--out", metavar="PATH", default="-", help="output file, - for stdout")
    exp.add_argument("--hex", dest="hex_code", metavar="HEX", default=None, help="only this ICAO")
    exp.add_argument(
        "--since",
        type=_epoch,
        default=None,
        metavar="EPOCH",
        help="positions at or after unix time",
    )
    exp.add_argument(
        "--until",
        type=_epoch,
        default=None,
        metavar="EPOCH",
        help="positions at or before unix time",
    )
    exp.add_argument(
        "--limit", type=_positive_int, default=0, metavar="N", help="maximum rows (0 = all)"
    )
    return parser


def _cmd_record(
    args: argparse.Namespace,
    *,
    opener: Callable[..., Any],
    sleep: Callable[[float], None],
    now: Callable[[], float],
) -> int:
    db_path = _db_path(args)
    url = args.url or os.environ.get("DUMP1090_URL", DEFAULT_HOST)
    endpoint = resolve_url(url)
    print(f"# recording {endpoint} every {args.interval:g}s -> {db_path}", file=sys.stderr)
    try:
        stats = record(
            url,
            db=db_path,
            interval=args.interval,
            max_polls=args.max_polls,
            timeout=args.timeout,
            retention_days=args.retention_days,
            store_raw=not args.no_store_raw,
            opener=opener,
            sleep=sleep,
            now=now,
        )
    except RecorderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    print(
        f"# recorded {stats.snapshots} snapshot(s), {stats.rows} position(s), "
        f"{stats.prunes} prune(s)",
        file=sys.stderr,
    )
    return EXIT_OK if stats.snapshots else EXIT_ERROR


def _cmd_prune(args: argparse.Namespace) -> int:
    conn = open_db(_db_path(args))
    try:
        deleted = prune(conn, args.retention_days)
    finally:
        conn.close()
    print(f"pruned {deleted} position(s) older than {args.retention_days} day(s)")
    return EXIT_OK


def _cmd_summary(args: argparse.Namespace) -> int:
    conn = open_db(_db_path(args))
    try:
        data = summary(conn, top=args.top)
    finally:
        conn.close()
    print(f"database: {_db_path(args)}")
    print(
        f"positions: {data.total_rows} row(s) across {data.aircraft} aircraft "
        f"over {len(data.days)} day(s)"
    )
    if data.first_ts is not None and data.last_ts is not None:
        print(f"range: {_fmt_ts(data.first_ts)} .. {_fmt_ts(data.last_ts)}")
    if data.days:
        print()
        print("per-day totals")
        _print_table(
            ("date", "positions", "aircraft"),
            [(day.date, str(day.rows), str(day.aircraft)) for day in data.days],
        )
    if data.top:
        print()
        print(f"top {len(data.top)} aircraft by sightings")
        _print_table(
            ("hex", "callsigns", "positions", "first seen", "last seen"),
            [
                (
                    item.hex,
                    item.callsigns or "-",
                    str(item.rows),
                    _fmt_ts(item.first_seen),
                    _fmt_ts(item.last_seen),
                )
                for item in data.top
            ],
        )
    return EXIT_OK


def _cmd_track(args: argparse.Namespace) -> int:
    hex_code = args.hex.lower()
    conn = open_db(_db_path(args))
    try:
        rows = track(conn, hex_code, limit=args.limit or None)
        info = get_flight(conn, hex_code)
    finally:
        conn.close()
    if not rows:
        print(f"error: no positions recorded for {hex_code}", file=sys.stderr)
        return EXIT_ERROR
    callsigns = ", ".join(info.callsigns) if info is not None and info.callsigns else "-"
    print(f"track {hex_code}: {len(rows)} position(s)  callsigns: {callsigns}")
    _print_table(
        ("time (UTC)", "lat", "lon", "alt", "gs", "track", "squawk", "rssi"),
        [
            (
                _fmt_ts(row.ts),
                _cell(row.lat, ".5f"),
                _cell(row.lon, ".5f"),
                _cell(row.alt_baro, ".0f"),
                _cell(row.gs, ".0f"),
                _cell(row.track, ".1f"),
                row.squawk or "",
                _cell(row.rssi, ".1f"),
            )
            for row in rows
        ],
    )
    return EXIT_OK


def _cmd_near(args: argparse.Namespace) -> int:
    conn = open_db(_db_path(args))
    try:
        matches = near(
            conn,
            args.lat,
            args.lon,
            args.radius_nm,
            since=args.since,
            limit=args.limit or None,
        )
    finally:
        conn.close()
    print(
        f"near {args.lat:.4f},{args.lon:.4f} within {args.radius_nm:g} nm: "
        f"{len(matches)} position(s)"
    )
    if matches:
        _print_table(
            ("dist_nm", "time (UTC)", "hex", "flight", "alt", "rssi"),
            [
                (
                    f"{point.dist_nm:.2f}",
                    _fmt_ts(point.position.ts),
                    point.position.hex,
                    point.position.flight or "-",
                    _cell(point.position.alt_baro, ".0f"),
                    _cell(point.position.rssi, ".1f"),
                )
                for point in matches
            ],
        )
    return EXIT_OK


def _open_out(path: str) -> TextIO:
    target = Path(path)
    try:
        return target.open("w", encoding="utf-8", newline="")
    except OSError as exc:
        raise RecorderError(f"cannot write {target}: {exc}") from exc


def _cmd_export(args: argparse.Namespace) -> int:
    noun = _EXPORT_NOUNS[args.fmt]
    conn = open_db(_db_path(args))
    try:
        rows = iter_positions(
            conn,
            hex_code=args.hex_code,
            since=args.since,
            until=args.until,
            limit=args.limit or None,
        )
        if args.out == "-":
            count = export_rows(rows, args.fmt, sys.stdout)
            print(f"# wrote {count} {noun} to stdout", file=sys.stderr)
        else:
            handle = _open_out(args.out)
            try:
                with handle:
                    count = export_rows(rows, args.fmt, handle)
            except OSError as exc:
                raise RecorderError(f"cannot write {args.out}: {exc}") from exc
            print(f"# wrote {count} {noun} to {args.out}", file=sys.stderr)
    finally:
        conn.close()
    return EXIT_OK


def main(
    argv: Sequence[str] | None = None,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> int:
    """Entry point. Returns a process exit code.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).
        opener: Injectable HTTP opener for ``record`` (tests).
        sleep: Injectable sleep for ``record`` (tests).
        now: Injectable wall clock for ``record`` (tests).
    """
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "record":
            return _cmd_record(args, opener=opener, sleep=sleep, now=now)
        if args.command == "prune":
            return _cmd_prune(args)
        if args.command == "summary":
            return _cmd_summary(args)
        if args.command == "track":
            return _cmd_track(args)
        if args.command == "near":
            return _cmd_near(args)
        return _cmd_export(args)
    except RecorderError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
