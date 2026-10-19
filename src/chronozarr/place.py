"""Where a longitude and latitude fall in a store's own coordinate system.

The viewer's centre (`c` in its address) is in the store's coordinates, so a place that a person
knows as a longitude and latitude has to be converted, and checked against the store: the viewer
moves a centre outside the store to its nearest edge without saying so.
"""

from __future__ import annotations

import math

WGS84 = "EPSG:4326"

Extent = tuple[float, float, float, float]  # xmin, ymin, xmax, ymax in the store's CRS


def _number(value: float) -> str:
    """`value` to 5 decimals (about a metre in degrees), without trailing zeros or a minus zero."""
    return f"{round(value, 5) + 0.0:.5f}".rstrip("0").rstrip(".")


def _range(low: float, high: float) -> str:
    return f"{_number(low)} to {_number(high)}"


def _in_store_units(crs: str, extent: Extent) -> str:
    xmin, ymin, xmax, ymax = extent
    return f"{crs} x {_range(xmin, xmax)}, y {_range(ymin, ymax)}"


def _inside(extent: Extent, x: float, y: float) -> bool:
    xmin, ymin, xmax, ymax = extent
    return xmin <= x <= xmax and ymin <= y <= ymax


def _wrap_longitude(x: float, west: float, east: float) -> float:
    """`x` moved by a whole turn into west..east when that puts it there, else unchanged."""
    for turn in (0.0, 360.0, -360.0):
        if west <= x + turn <= east:
            return x + turn
    return x


def _degrees_extent(crs: str, extent: Extent) -> Extent:
    if crs == WGS84:
        return extent
    import pyproj

    transformer = pyproj.Transformer.from_crs(crs, WGS84, always_xy=True)
    west, south, east, north = transformer.transform_bounds(*extent, densify_pts=21)
    return west, south, east, north


def check_xy(crs: str, extent: Extent, x: float, y: float) -> None:
    """Raise ValueError, naming the store's extent, when store coordinates x, y are outside it."""
    if not _inside(extent, x, y):
        raise ValueError(
            f"x {_number(x)}, y {_number(y)} is outside the store, which covers "
            f"{_in_store_units(crs, extent)}"
        )


def lonlat_to_xy(crs: str, extent: Extent, lon: float, lat: float) -> tuple[float, float]:
    """Store coordinates (x, y) of a longitude and latitude in degrees (WGS 84).

    `crs` is the store's CRS, such as "EPSG:32631", and `extent` the store's bounds in it. A store
    in EPSG:4326 needs no projection library; any other needs pyproj (ImportError otherwise). In a
    geographic store a longitude is moved by 360 degrees when that puts it in the store's range, so
    -64.6 finds 295.4 in a grid that stores 0..360. Raises ValueError for an impossible latitude,
    a position the projection cannot draw, or a point outside the store, and says what the store
    covers.
    """
    if not (math.isfinite(lon) and math.isfinite(lat) and -90.0 <= lat <= 90.0):
        raise ValueError(
            f"longitude and latitude must be numbers, latitude from -90 to 90 degrees; got "
            f"longitude {lon:g}, latitude {lat:g}"
        )
    if crs == WGS84:
        x, y, geographic = lon, lat, True
    else:
        import pyproj

        x, y = pyproj.Transformer.from_crs(WGS84, crs, always_xy=True).transform(lon, lat)
        geographic = pyproj.CRS.from_user_input(crs).is_geographic
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"longitude {lon:g}, latitude {lat:g} has no position in {crs}")
    if geographic:
        x = _wrap_longitude(x, extent[0], extent[2])
    x, y = round(x, 6) + 0.0, round(y, 6) + 0.0  # clear projection noise such as 4e-10
    if not _inside(extent, x, y):
        west, south, east, north = _degrees_extent(crs, extent)
        covers = f"longitude {_range(west, east)} and latitude {_range(south, north)}"
        if crs != WGS84:
            covers += f" ({_in_store_units(crs, extent)})"
        raise ValueError(
            f"longitude {_number(lon)}, latitude {_number(lat)} is outside the store, which "
            f"covers {covers}. Give the longitude first, then the latitude."
        )
    return x, y
