"""Coordinate transforms for point queries.

Converts between WGS84 lat/lng, native UTM coordinates, and mosaic pixel
coordinates. Used by the point query endpoint to locate the right chunk
and pixel for a given geographic position.
"""

from __future__ import annotations

from dataclasses import dataclass

from pyproj import Transformer
from rasterio.transform import Affine


@dataclass
class PixelCoords:
    """Result of a coordinate lookup: pixel position + chunk address."""

    pixel_row: int
    pixel_col: int
    chunk_row: int
    chunk_col: int
    local_row: int
    local_col: int
    chunk_id: str
    utm_x: float
    utm_y: float


def latlng_to_pixel(
    lat: float,
    lng: float,
    epsg: int,
    transform: list[float] | Affine,
    chunk_size: int,
    mosaic_height: int,
    mosaic_width: int,
) -> PixelCoords | None:
    """Convert WGS84 lat/lng to mosaic pixel and chunk coordinates.

    Returns None if the point falls outside the mosaic extent.
    """
    # WGS84 → UTM
    proj = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)
    utm_x, utm_y = proj.transform(lng, lat)

    return _utm_to_pixel(utm_x, utm_y, transform, chunk_size, mosaic_height, mosaic_width)


def pixel_rowcol_to_coords(
    pixel_row: int,
    pixel_col: int,
    epsg: int,
    transform: list[float] | Affine,
    chunk_size: int,
    mosaic_height: int,
    mosaic_width: int,
) -> PixelCoords | None:
    """Convert mosaic pixel row/col to chunk coordinates + UTM + lat/lng.

    Returns None if out of bounds.
    """
    if pixel_row < 0 or pixel_row >= mosaic_height:
        return None
    if pixel_col < 0 or pixel_col >= mosaic_width:
        return None

    if isinstance(transform, list):
        transform = Affine(
            transform[0], transform[1], transform[2], transform[3], transform[4], transform[5]
        )

    # Pixel → UTM
    utm_x = transform.c + pixel_col * transform.a + pixel_row * transform.b
    utm_y = transform.f + pixel_col * transform.d + pixel_row * transform.e

    chunk_row = pixel_row // chunk_size
    chunk_col = pixel_col // chunk_size
    local_row = pixel_row % chunk_size
    local_col = pixel_col % chunk_size
    chunk_id = f"r{chunk_row:03d}_c{chunk_col:03d}"

    return PixelCoords(
        pixel_row=pixel_row,
        pixel_col=pixel_col,
        chunk_row=chunk_row,
        chunk_col=chunk_col,
        local_row=local_row,
        local_col=local_col,
        chunk_id=chunk_id,
        utm_x=utm_x,
        utm_y=utm_y,
    )


def pixel_to_latlng(
    pixel_row: int, pixel_col: int, epsg: int, transform: list[float] | Affine
) -> tuple[float, float]:
    """Convert mosaic pixel to WGS84 lat/lng."""
    if isinstance(transform, list):
        transform = Affine(
            transform[0], transform[1], transform[2], transform[3], transform[4], transform[5]
        )

    utm_x = transform.c + pixel_col * transform.a + pixel_row * transform.b
    utm_y = transform.f + pixel_col * transform.d + pixel_row * transform.e

    proj = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    lng, lat = proj.transform(utm_x, utm_y)
    return lat, lng


def _utm_to_pixel(
    utm_x: float,
    utm_y: float,
    transform: list[float] | Affine,
    chunk_size: int,
    mosaic_height: int,
    mosaic_width: int,
) -> PixelCoords | None:
    """Convert UTM coordinates to pixel + chunk coordinates."""
    if isinstance(transform, list):
        transform = Affine(
            transform[0], transform[1], transform[2], transform[3], transform[4], transform[5]
        )

    # Invert affine: pixel = ~transform * (x, y)
    inv = ~transform
    pixel_col_f, pixel_row_f = inv * (utm_x, utm_y)
    pixel_row = int(pixel_row_f)
    pixel_col = int(pixel_col_f)

    if pixel_row < 0 or pixel_row >= mosaic_height:
        return None
    if pixel_col < 0 or pixel_col >= mosaic_width:
        return None

    chunk_row = pixel_row // chunk_size
    chunk_col = pixel_col // chunk_size
    local_row = pixel_row % chunk_size
    local_col = pixel_col % chunk_size
    chunk_id = f"r{chunk_row:03d}_c{chunk_col:03d}"

    return PixelCoords(
        pixel_row=pixel_row,
        pixel_col=pixel_col,
        chunk_row=chunk_row,
        chunk_col=chunk_col,
        local_row=local_row,
        local_col=local_col,
        chunk_id=chunk_id,
        utm_x=utm_x,
        utm_y=utm_y,
    )
