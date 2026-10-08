"""Fetching ``aircraft.json`` from a dump1090 / readsb decoder (stdlib only)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

from dump1090_recorder.errors import RecorderError

__all__ = ["AIRCRAFT_PATH", "DEFAULT_HOST", "fetch_aircraft", "resolve_url"]

#: Where both dump1090-fa and readsb publish their aircraft state.
DEFAULT_HOST = "http://localhost:8080"
AIRCRAFT_PATH = "/data/aircraft.json"


def resolve_url(url: str) -> str:
    """Accept a bare host (``http://host:8080``) or a full document URL.

    Anything that does not already point at a ``*.json`` document is treated
    as the decoder root and gets ``/data/aircraft.json`` appended.
    """
    cleaned = url.rstrip("/")
    if cleaned.endswith(".json"):
        return cleaned
    return cleaned + AIRCRAFT_PATH


def fetch_aircraft(
    url: str,
    *,
    timeout: float = 5.0,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Mapping[str, Any]:
    """Fetch one ``aircraft.json`` document.

    Raises:
        RecorderError: Network failure, non-JSON body, or an unexpected shape.
    """
    endpoint = resolve_url(url)
    try:
        with opener(endpoint, timeout=timeout) as response:
            body = response.read()
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise RecorderError(f"cannot read {endpoint}: {exc}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RecorderError(f"{endpoint} did not return JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RecorderError(
            f"{endpoint} returned a JSON {type(payload).__name__}, expected an object"
        )
    if not isinstance(payload.get("aircraft"), list):
        raise RecorderError(f"{endpoint} returned no 'aircraft' array")
    return payload
