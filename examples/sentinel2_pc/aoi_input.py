"""AOIs for the Sentinel-2 ingest that are not listed in aois.yaml.

An AOI is a name, a WGS84 bounding box (west, south, east, north in degrees) and the EPSG code
of the UTM zone its 10 m grid is built in. aois.yaml lists those by hand; `bbox_aoi` and
`geojson_aoi` make one from a bounding box or from the bounding box of a GeoJSON geometry.

The UTM zone is the standard zone of the bounding box's centre, which is how every AOI in
aois.yaml was chosen. Scenes in a neighbouring zone are warped onto the grid, as they are for
those AOIs. The grid is the bounding box snapped outwards to whole 10 m pixels
(`mosaic.compute_target_grid`); the pixels of a polygon's bounding box that lie outside the
polygon are not masked.

Standard library only, so the checks run without the ingest's dependencies.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Sequence
from pathlib import Path

BBox = tuple[float, float, float, float]

NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
# UTM is defined from 80 S to 84 N.
UTM_LAT_MIN, UTM_LAT_MAX = -80.0, 84.0


def validate_name(name: str) -> str:
    """`name` names the output directories and the STAC id, so it must be a plain file name."""
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError(
            f"AOI name {name!r} must start with a letter or digit and contain only letters, "
            "digits, '_', '-' and '.'. Pass another one with --name"
        )
    return name


def validate_bbox(bbox: BBox, source: str) -> BBox:
    west, south, east, north = bbox
    if not all(math.isfinite(v) for v in bbox):
        raise ValueError(f"{source}: {bbox} has a value that is not a finite number")
    if not -180 <= west < east <= 180:
        raise ValueError(
            f"{source}: west ({west}) must be less than east ({east}), both between -180 and "
            "180 degrees. The order is west south east north. A box that crosses the "
            "antimeridian must be split into one AOI on each side"
        )
    if east - west > 180:
        raise ValueError(
            f"{source}: spans {east - west:g} degrees of longitude. A geometry that crosses the "
            "antimeridian is stored as parts near -180 and 180; split it into one AOI on each "
            "side"
        )
    if not UTM_LAT_MIN <= south < north <= UTM_LAT_MAX:
        raise ValueError(
            f"{source}: south ({south}) must be less than north ({north}), both between "
            f"{UTM_LAT_MIN:g} and {UTM_LAT_MAX:g} degrees, the range of the UTM zones"
        )
    return bbox


def parse_bbox(values: Sequence[str]) -> BBox:
    """A --bbox value: four numbers, as separate arguments or as one comma-separated one."""
    parts = [p for v in values for p in v.split(",")]
    source = f"--bbox {' '.join(values)}"
    if len(parts) != 4:
        raise ValueError(
            f"{source}: expected 4 numbers (west south east north in degrees), got {len(parts)}"
        )
    try:
        west, south, east, north = (float(p) for p in parts)
    except ValueError as e:
        raise ValueError(f"{source}: {e}") from e
    return validate_bbox((west, south, east, north), source)


def utm_epsg(bbox: BBox) -> int:
    """EPSG code of the standard UTM zone of the centre of `bbox` (326xx north, 327xx south)."""
    west, south, east, north = bbox
    zone = int(((west + east) / 2 + 180) // 6) + 1
    return (32600 if (south + north) / 2 >= 0 else 32700) + zone


def _positions(coordinates: object) -> Iterator[Sequence[float]]:
    if (
        isinstance(coordinates, list)
        and len(coordinates) >= 2
        and all(isinstance(c, int | float) and not isinstance(c, bool) for c in coordinates)
    ):
        yield coordinates
    elif isinstance(coordinates, list):
        for part in coordinates:
            yield from _positions(part)


def _geometry_positions(node: object) -> Iterator[Sequence[float]]:
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    if kind == "FeatureCollection":
        for feature in node.get("features") or []:
            yield from _geometry_positions(feature)
    elif kind == "Feature":
        yield from _geometry_positions(node.get("geometry"))
    elif kind == "GeometryCollection":
        for geometry in node.get("geometries") or []:
            yield from _geometry_positions(geometry)
    else:
        yield from _positions(node.get("coordinates"))


def bbox_of_geojson(path: Path) -> BBox:
    """Bounding box of every coordinate in a GeoJSON file (Feature, FeatureCollection or
    geometry; longitude and latitude as RFC 7946 requires). Heights are ignored."""
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise ValueError(f"cannot read GeoJSON {path}: {e}") from e
    positions = list(_geometry_positions(document))
    if not positions:
        raise ValueError(f"{path} has no coordinates; expected GeoJSON with a Polygon or similar")
    lons = [p[0] for p in positions]
    lats = [p[1] for p in positions]
    bbox = (min(lons), min(lats), max(lons), max(lats))
    return validate_bbox(
        bbox, f"{path} (bounding box of its geometry; GeoJSON must be in longitude, latitude)"
    )


def bbox_aoi(name: str, bbox: BBox) -> dict:
    """An AOI in the shape of an aois.yaml entry (name, bbox, epsg)."""
    return {"name": validate_name(name), "bbox": list(bbox), "epsg": utm_epsg(bbox)}


def geojson_aoi(path: Path, name: str | None = None) -> dict:
    """The AOI of a GeoJSON file's bounding box, named `name` or after the file."""
    return bbox_aoi(name or path.stem, bbox_of_geojson(path))
