"""Haversine / great-circle distance tests."""

from __future__ import annotations

import pytest

from dump1090_recorder.geo import EARTH_RADIUS_NM, haversine_nm

JFK = (40.6397, -73.7789)
LHR = (51.4775, -0.4614)


def test_identical_points_are_zero_nm_apart() -> None:
    assert haversine_nm(51.5, -0.45, 51.5, -0.45) == 0.0


def test_one_degree_of_latitude_is_about_sixty_nm() -> None:
    # 1 degree of latitude = pi/180 * R, independent of longitude.
    assert haversine_nm(50.0, 10.0, 51.0, 10.0) == pytest.approx(60.04, abs=0.05)


def test_distance_is_symmetric() -> None:
    forward = haversine_nm(*JFK, *LHR)
    backward = haversine_nm(*LHR, *JFK)
    assert forward == pytest.approx(backward)
    assert forward == pytest.approx(3000, rel=0.01)


def test_half_the_globe_is_about_half_the_circumference() -> None:
    antipodal = haversine_nm(0.0, 0.0, 0.0, 180.0)
    assert antipodal == pytest.approx(3.141592653589793 * EARTH_RADIUS_NM, rel=1e-6)


def test_longitude_convergence_shrinks_distances_at_high_latitude() -> None:
    # 1 degree of longitude at 60N is half its equator length.
    at_equator = haversine_nm(0.0, 0.0, 0.0, 1.0)
    at_sixty = haversine_nm(60.0, 0.0, 60.0, 1.0)
    assert at_sixty == pytest.approx(at_equator / 2, rel=0.01)


def test_small_adsb_separations_are_accurate() -> None:
    # A 3 nm offset due north is 0.05 degrees of latitude.
    assert haversine_nm(51.0, 0.0, 51.05, 0.0) == pytest.approx(3.0, abs=0.02)
