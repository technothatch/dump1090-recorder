"""The recording loop: poll a decoder, append to SQLite, honour retention.

Timestamps come from the decoder's own ``now`` field whenever it is numeric,
so a remote feeder records the receiver's clock rather than the recorder
host's. Retention runs once on start and then hourly while recording, and
``--no-store-raw`` drops the per-row ``raw_json`` copy of the original
``aircraft[]`` entry (the biggest column by far).
"""

from __future__ import annotations

import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from dump1090_recorder.db import insert_positions, open_db, positions_from_snapshot, prune
from dump1090_recorder.errors import RecorderError
from dump1090_recorder.fetch import fetch_aircraft, resolve_url

__all__ = ["PRUNE_INTERVAL", "RecordStats", "record", "snapshot_ts"]

#: How often retention prunes while ``record`` is running.
PRUNE_INTERVAL = 3600.0


@dataclass(frozen=True, slots=True)
class RecordStats:
    """What one ``record`` run achieved."""

    snapshots: int
    rows: int
    prunes: int


def snapshot_ts(doc: Mapping[str, Any], *, wall: Callable[[], float]) -> float:
    """Pick the timestamp for one snapshot: decoder ``now``, else wall clock.

    ``wall`` is only read when the document lacks a numeric ``now``, so a
    healthy decoder never touches the recorder host's clock.
    """
    decoder_now = doc.get("now")
    if isinstance(decoder_now, int | float) and not isinstance(decoder_now, bool):
        return float(decoder_now)
    return wall()


def record(
    url: str,
    *,
    db: str,
    interval: float = 1.0,
    max_polls: int = 0,
    timeout: float = 5.0,
    retention_days: int = 0,
    store_raw: bool = True,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> RecordStats:
    """Poll a decoder and append every aircraft to the database.

    Args:
        url: Decoder base URL or full ``aircraft.json`` URL.
        db: Database path (created if missing).
        interval: Seconds between polls.
        max_polls: Stop after this many poll attempts (0 = run until interrupted).
        timeout: HTTP timeout per poll.
        retention_days: Prune rows older than N days on start, then hourly (0 = keep forever).
        store_raw: Keep the raw ``aircraft[]`` JSON per row.
        opener: Injectable HTTP opener for tests.
        sleep: Injectable sleep for tests.
        now: Injectable wall clock (fallback timestamps and prune scheduling).

    Returns:
        Snapshots stored, positions written and prune passes run. A decoder
        that is down produces warnings, not a crash: failed polls count
        towards ``max_polls`` but write nothing.

    Raises:
        RecorderError: The database cannot be opened.
    """
    endpoint = resolve_url(url)
    conn = open_db(db)
    try:
        prunes = 0
        last_prune = now()
        if retention_days > 0:
            prune(conn, retention_days, now=last_prune)
            prunes = 1
        snapshots = 0
        rows = 0
        polls = 0
        try:
            while True:
                polls += 1
                try:
                    doc = fetch_aircraft(endpoint, timeout=timeout, opener=opener)
                except RecorderError as exc:
                    print(f"warning: {exc}", file=sys.stderr)
                else:
                    ts = snapshot_ts(doc, wall=now)
                    rows += insert_positions(
                        conn,
                        positions_from_snapshot(doc, ts=ts, store_raw=store_raw),
                    )
                    snapshots += 1
                if max_polls and polls >= max_polls:
                    break
                sleep(interval)
                if retention_days > 0:
                    moment = now()
                    if moment - last_prune >= PRUNE_INTERVAL:
                        prune(conn, retention_days, now=moment)
                        prunes += 1
                        last_prune = moment
        except KeyboardInterrupt:  # Ctrl+C ends the recording, not a crash
            print("\n# stopped", file=sys.stderr)
    finally:
        conn.close()
    return RecordStats(snapshots=snapshots, rows=rows, prunes=prunes)
