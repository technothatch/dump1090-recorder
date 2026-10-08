"""Shared test fixtures — inline ADS-B data, never a live decoder.

Every snapshot here is written inline, so the suite runs on a machine with no
dump1090 / readsb instance, no network and no clock assumptions beyond epoch
timestamps that are explicitly passed around.
"""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from dump1090_recorder.db import Position, open_db

#: 2026-10-03 12:00:00 UTC — same epoch family as the sibling repositories.
T0 = 1791028800.0


def aircraft(**fields: Any) -> dict[str, Any]:
    """One realistic ``aircraft[]`` entry (SPEC.md field list subset)."""
    base: dict[str, Any] = {
        "hex": "4ca918",
        "flight": "RYR22GK ",
        "type": "B738",
        "alt_baro": 34000,
        "gs": 458,
        "track": 210.5,
        "squawk": "1200",
        "emergency": "none",
        "lat": 51.4712,
        "lon": -0.4543,
        "seen": 0.5,
        "seen_pos": 0.7,
        "messages": 100,
        "rssi": -4.2,
    }
    base.update(fields)
    return base


def snapshot(ts: float, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """One full ``aircraft.json`` document as the decoder would publish it."""
    return {
        "now": ts,
        "messages": 100,
        "aircraft": [aircraft()] if entries is None else entries,
    }


def position(**fields: Any) -> Position:
    """Build a :class:`Position` with realistic defaults, overridable per test."""
    base: dict[str, Any] = {
        "ts": T0,
        "hex": "4ca918",
        "flight": "RYR22GK",
        "lat": 51.4712,
        "lon": -0.4543,
        "alt_baro": 34000.0,
        "gs": 458.0,
        "track": 210.5,
        "squawk": "1200",
        "rssi": -4.2,
        "seen_pos": 0.7,
    }
    base.update(fields)
    return Position(**base)


class FakeResponse:
    """Minimal stand-in for the object ``urllib.request.urlopen`` returns."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def decoder_opener(
    payload: dict[str, Any] | None = None, *, error: Exception | None = None
) -> Callable[..., FakeResponse]:
    """An opener serving ``payload`` (default: one aircraft) or raising ``error``."""
    document = snapshot(T0) if payload is None else payload

    def opener(url: str, timeout: float = 0.0) -> FakeResponse:
        if error is not None:
            raise error
        return FakeResponse(json.dumps(document).encode())

    return opener


def raw_opener(body: bytes) -> Callable[..., FakeResponse]:
    """An opener returning a raw body, whatever it is."""

    def opener(url: str, timeout: float = 0.0) -> FakeResponse:
        return FakeResponse(body)

    return opener


def network_error(message: str = "connection refused") -> Exception:
    """The failure mode urllib actually raises when a host is unreachable."""
    return urllib.error.URLError(message)


class FakeClock:
    """A wall clock that jumps ``step`` seconds on every call (deterministic)."""

    def __init__(self, start: float, step: float = 0.0) -> None:
        self.value = start
        self.step = step
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        current = self.value
        self.value += self.step
        return current


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "recorder.sqlite3"


@pytest.fixture
def conn(db_path: Path) -> Any:
    connection = open_db(db_path)
    yield connection
    connection.close()


@pytest.fixture
def db_file(tmp_path: Path) -> Path:
    """A path that does not exist yet — the CLI will create the database."""
    return tmp_path / "cli.sqlite3"
