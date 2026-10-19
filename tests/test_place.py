"""A longitude and latitude in a store's own coordinates, checked against its extent."""

from __future__ import annotations

import sys

import pytest

from chronozarr.place import check_xy, lonlat_to_xy

pytestmark = pytest.mark.unit

# UTM zone 31N has its central meridian at 3 E and a false northing of 0 north of the equator, so
# (3 E, 0 N) is exactly (500000, 0): a reference that does not use a projection library.
UTM_EXTENT = (499800.0, -300.0, 500300.0, 100.0)


def test_a_point_on_the_central_meridian_and_equator_is_the_false_easting_and_zero():
    assert lonlat_to_xy("EPSG:32631", UTM_EXTENT, 3.0, 0.0) == (500000.0, 0.0)


def test_the_x_of_a_projection_with_northing_first_axes_is_the_easting():
    # EPSG:3035 (ETRS89-LAEA) is defined with northing first; its origin is (10 E, 52 N) at the
    # false origin (4321000, 3210000). A store holds easting as x whatever the axis order.
    extent = (4320000.0, 3209000.0, 4322000.0, 3211000.0)
    assert lonlat_to_xy("EPSG:3035", extent, 10.0, 52.0) == (4321000.0, 3210000.0)


def test_a_geographic_store_needs_no_projection_library(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyproj", None)  # an import of pyproj now fails
    assert lonlat_to_xy("EPSG:4326", (-10.0, 30.0, 10.0, 50.0), -3.25, 40.5) == (-3.25, 40.5)


def test_a_projected_store_without_pyproj_names_the_missing_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyproj", None)
    with pytest.raises(ImportError) as raised:
        lonlat_to_xy("EPSG:32631", UTM_EXTENT, 3.0, 0.0)
    assert raised.value.name == "pyproj"


def test_a_longitude_is_moved_into_the_range_a_zero_to_360_store_uses():
    # NOAA OISST and similar grids store longitudes 0..360
    extent = (280.0, 0.0, 360.0, 45.0)
    assert lonlat_to_xy("EPSG:4326", extent, -64.6, 10.0) == (295.4, 10.0)


def test_a_longitude_beyond_180_is_moved_into_a_minus_180_to_180_store():
    extent = (-180.0, -90.0, 180.0, 90.0)
    assert lonlat_to_xy("EPSG:4326", extent, 190.0, 0.0) == (-170.0, 0.0)


def test_a_store_across_the_antimeridian_keeps_longitudes_past_180():
    extent = (165.0, -30.0, 195.0, -10.0)
    assert lonlat_to_xy("EPSG:4326", extent, -170.0, -20.0) == (190.0, -20.0)
    assert lonlat_to_xy("EPSG:4326", extent, 170.0, -20.0) == (170.0, -20.0)


def test_the_edge_of_the_extent_is_inside():
    assert lonlat_to_xy("EPSG:4326", (0.0, 0.0, 10.0, 10.0), 10.0, 0.0) == (10.0, 0.0)


def test_a_point_outside_the_store_is_refused_with_the_extent_in_degrees_and_store_units():
    with pytest.raises(ValueError) as raised:
        lonlat_to_xy("EPSG:32631", UTM_EXTENT, 3.0, 5.0)
    message = str(raised.value)
    assert "longitude 3, latitude 5 is outside the store" in message
    assert "longitude 2.9982 to 3.0027 and latitude -0.00271 to 0.0009" in message
    assert "EPSG:32631 x 499800 to 500300, y -300 to 100" in message
    assert "longitude first" in message


def test_a_geographic_store_names_its_extent_in_degrees_once():
    with pytest.raises(ValueError) as raised:
        lonlat_to_xy("EPSG:4326", (0.0, 0.0, 10.0, 10.0), 20.0, 5.0)
    assert "longitude 0 to 10 and latitude 0 to 10" in str(raised.value)


@pytest.mark.parametrize(
    ("lon", "lat"), [(0.0, 91.0), (0.0, -90.5), (float("nan"), 0.0), (0.0, float("inf"))]
)
def test_an_impossible_longitude_or_latitude_is_refused(lon, lat):
    with pytest.raises(ValueError, match="latitude from -90 to 90"):
        lonlat_to_xy("EPSG:4326", (-180.0, -90.0, 180.0, 90.0), lon, lat)


def test_a_point_with_no_position_in_the_projection_is_refused():
    # the antipode of the centre of a Lambert azimuthal projection cannot be drawn
    extent = (4320000.0, 3209000.0, 4322000.0, 3211000.0)
    with pytest.raises(ValueError, match="no position in EPSG:3035"):
        lonlat_to_xy("EPSG:3035", extent, -170.0, -52.0)


def test_store_coordinates_are_checked_against_the_extent():
    check_xy("EPSG:32631", UTM_EXTENT, 500000.0, 0.0)
    with pytest.raises(ValueError) as raised:
        check_xy("EPSG:32631", UTM_EXTENT, 500000.0, 200.0)
    assert "x 500000, y 200 is outside the store" in str(raised.value)
    assert "EPSG:32631 x 499800 to 500300, y -300 to 100" in str(raised.value)
