"""Errors shared across dump1090-recorder."""

from __future__ import annotations

__all__ = ["RecorderError"]


class RecorderError(Exception):
    """Bad input, invalid options, or an unreadable data source."""

    # Intentionally plain: every caller turns this into a user-facing message.
