"""Spherical geometry helpers for dump1090-recorder.

Everything is stdlib ``math`` — no pyproj, no numpy. Coordinates are decimal
degrees on a spherical Earth, which is accurate to well under 1 % for the
distances ADS-B receivers care about (tens to thousands of nautical miles).
"""

from __future__ import annotations

import math

__all__ = ["EARTH_RADIUS_NM", "haversine_nm"]

#: Mean Earth radius in nautical miles (6371.0088 km / 1.852 km per nm).
EARTH_RADIUS_NM = 3440.065


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in nautical miles.

    Uses the haversine formula, which stays numerically stable for the small
    separations typical of ADS-B tracks (Vincenty-style formulae are overkill
    here and need iteration).
    """
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2.0) ** 2
    return 2.0 * EARTH_RADIUS_NM * math.asin(math.sqrt(min(1.0, a)))
