"""Static STAC Collection and Item for a chronozarr store.

The Item carries the Zarr asset, spatial and temporal extent, band metadata, the datacube
extension (`cube:dimensions`, `cube:variables`) and whatever provenance the store records, so a
STAC client can find and describe the store without opening it. Output is plain JSON files that
can sit next to the store in a bucket. Needs rasterio (`chronozarr[geo]`) to express the extent in
WGS84.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

from chronozarr import schema
from chronozarr.crs import Crs
from chronozarr.decode import ChronoStore, open_store
from chronozarr.doctor import is_url

STAC_VERSION = "1.1.0"
EXT_DATACUBE = "https://stac-extensions.github.io/datacube/v2.3.0/schema.json"
EXT_PROJECTION = "https://stac-extensions.github.io/projection/v2.0.0/schema.json"
EXT_EO = "https://stac-extensions.github.io/eo/v2.0.0/schema.json"
EXT_RASTER = "https://stac-extensions.github.io/raster/v2.0.0/schema.json"
ZARR_MEDIA_TYPE = "application/vnd+zarr"
_EDGE_POINTS = 21


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", text).strip("-") or "chronozarr-store"


def default_id(source: str) -> str:
    """`<parent>-<name>` of a store path or URL, e.g. `ucayali_santa_maria-chronozarr-2`."""
    if is_url(source):
        parts = [p for p in urllib.parse.urlsplit(source).path.split("/") if p]
    else:
        parts = list(Path(source).resolve().parts)
    return _slug("-".join(parts[-2:]))


def _epsg_number(crs: str) -> int | str:
    match = re.fullmatch(r"EPSG:(\d+)", crs)
    return int(match.group(1)) if match else crs


def _proj_properties(crs: Crs) -> dict[str, Any]:
    """STAC projection 2.0: `proj:code`, or a null `proj:code` with the CRS definition."""
    if crs.code is not None:
        return {"proj:code": crs.code}
    properties: dict[str, Any] = {"proj:code": None}
    if crs.wkt2 is not None:
        properties["proj:wkt2"] = crs.wkt2
    if crs.projjson is not None:
        properties["proj:projjson"] = crs.projjson
    return properties


def _proj_summaries(crs: Crs) -> dict[str, Any]:
    if crs.code is not None:
        return {"proj:code": [crs.code]}
    return {key: [value] for key, value in _proj_properties(crs).items() if value is not None}


def _longitude_piece(ring: list[list[float]], west: float, east: float) -> list[list[float]]:
    """Clip an unwrapped ring to one longitude window and move it into -180..180."""
    vertices = ring[:-1]
    for edge, keep_east in ((west, True), (east, False)):
        clipped = []
        if not vertices:
            return []
        previous = vertices[-1]
        previous_inside = previous[0] >= edge if keep_east else previous[0] <= edge
        for point in vertices:
            inside = point[0] >= edge if keep_east else point[0] <= edge
            if inside != previous_inside:
                fraction = (edge - previous[0]) / (point[0] - previous[0])
                clipped.append([edge, previous[1] + fraction * (point[1] - previous[1])])
            if inside:
                clipped.append(point)
            previous, previous_inside = point, inside
        vertices = clipped
    rounded: list[list[float]] = []
    for x, y in vertices:
        point = [round(x - west - 180, 6), round(y, 6)]
        if not rounded or point != rounded[-1]:
            rounded.append(point)
    if rounded and rounded[-1] == rounded[0]:
        rounded.pop()
    if len(rounded) < 3:
        return []
    area = sum(
        x0 * y1 - x1 * y0
        for (x0, y0), (x1, y1) in zip(rounded, rounded[1:] + rounded[:1], strict=True)
    )
    if area == 0:
        return []
    if area < 0:  # GeoJSON exterior rings follow the right-hand rule.
        rounded.reverse()
    return [*rounded, rounded[0]]


def _footprint(
    crs: str, bounds: tuple[float, float, float, float]
) -> tuple[dict[str, Any], list[float]]:
    """WGS84 geometry and bbox, cutting densified edges at the antimeridian."""
    from rasterio._err import CPLE_BaseError  # ty: ignore[unresolved-import]  # compiled
    from rasterio.crs import CRS  # ty: ignore[unresolved-import]  # compiled
    from rasterio.errors import RasterioError
    from rasterio.warp import transform

    left, bottom, right, top = bounds
    projected = CRS.from_user_input(crs).is_projected
    poles = []
    for candidate in (90.0, -90.0):
        try:
            pole_x, pole_y = transform("EPSG:4326", crs, [0.0], [candidate])
        except (RasterioError, CPLE_BaseError):
            continue
        if left <= pole_x[0] <= right and bottom <= pole_y[0] <= top:
            if projected and (pole_x[0] in (left, right) or pole_y[0] in (bottom, top)):
                raise ValueError("a STAC footprint with a pole on its boundary is unsupported")
            poles.append(candidate)
    t = np.linspace(0.0, 1.0, _EDGE_POINTS)
    xs = np.concatenate([left + t * (right - left), np.full_like(t, right)])
    ys = np.concatenate([np.full_like(t, top), top + t * (bottom - top)])
    xs = np.concatenate([xs, right + t * (left - right), np.full_like(t, left)])
    ys = np.concatenate([ys, np.full_like(t, bottom), bottom + t * (top - bottom)])
    lon, lat = transform(crs, "EPSG:4326", xs.tolist(), ys.tolist())
    if not (np.isfinite(lon).all() and np.isfinite(lat).all() and max(map(abs, lat)) <= 90):
        raise ValueError("the store's bounds do not have a finite WGS84 footprint")
    unwrapped = np.rad2deg(np.unwrap(np.deg2rad(lon)))
    ring = [[float(x), float(y)] for x, y in zip(unwrapped, lat, strict=True)]
    winding = ring[-1][0] - ring[0][0]
    if abs(winding) > 180:
        if not math.isclose(abs(winding), 360, rel_tol=0, abs_tol=1e-7):
            raise ValueError("the store's footprint winds around the world more than once")
        # Close a polar cap at the antimeridian, rather than at the first sample's
        # arbitrary meridian: the latter would leave two pieces sharing an interior edge.
        cut = 180 + 360 * math.ceil((min(unwrapped) - 180) / 360)
        for i, (previous, point) in enumerate(pairwise(ring)):
            if previous[0] != point[0] and min(previous[0], point[0]) <= cut <= max(
                previous[0], point[0]
            ):
                fraction = (cut - previous[0]) / (point[0] - previous[0])
                latitude = previous[1] + fraction * (point[1] - previous[1])
                ring = [
                    [cut, latitude],
                    *ring[i + 1 :],
                    *[[x + winding, y] for x, y in ring[1 : i + 1]],
                    [cut + winding, latitude],
                ]
                shift = (-180 if winding > 0 else 180) - cut
                ring = [[x + shift, y] for x, y in ring]
                break
        if len(poles) != 1:
            raise ValueError("the store's footprint does not identify a unique enclosed pole")
        pole = poles[0]
        ring.extend([[ring[-1][0], pole], [ring[0][0], pole], ring[0]])
    else:
        ring[-1] = ring[0]
    west, east = min(p[0] for p in ring), max(p[0] for p in ring)
    south, north = min(p[1] for p in ring), max(p[1] for p in ring)
    span = east - west
    if span > 360 + 1e-6:
        raise ValueError("the store's footprint spans more than one longitude turn")
    full_turn = math.isclose(span, 360, rel_tol=0, abs_tol=1e-7)
    if full_turn and abs(winding) < 180:
        # A full-width geographic/Mercator grid is one band, with no seam at its origin.
        if not all(
            any(
                math.isclose(value, edge, rel_tol=0, abs_tol=1e-8)
                for value, edge in ((x, west), (x, east), (y, south), (y, north))
            )
            for x, y in ring
        ):
            raise ValueError("a full-longitude footprint with curved edges is unsupported")
        ring = [[-180, south], [180, south], [180, north], [-180, north], [-180, south]]
        pieces = [_longitude_piece(ring, -180, 180)]
    else:
        pieces = [
            piece
            for window in range(math.floor((west + 180) / 360), math.floor((east + 180) / 360) + 1)
            if (piece := _longitude_piece(ring, -180 + window * 360, 180 + window * 360))
        ]
    if not pieces:
        raise ValueError("the store's footprint is empty at six-decimal coordinate precision")
    geometry = (
        {"type": "Polygon", "coordinates": [pieces[0]]}
        if len(pieces) == 1
        else {"type": "MultiPolygon", "coordinates": [[piece] for piece in pieces]}
    )
    bbox = [
        -180 if full_turn else round((west + 180) % 360 - 180, 6),
        round(south, 6),
        180 if full_turn else round((east + 180) % 360 - 180, 6),
        round(north, 6),
    ]
    if not full_turn and bbox[2] == -180:
        bbox[2] = 180
    return geometry, bbox


def _band_objects(store: ChronoStore) -> list[dict[str, Any]]:
    data_type = str(store.levels[0].data.dtype)
    nodata = store.attrs.nodata
    out = []
    for band in store.attrs.bands:
        entry: dict[str, Any] = {"name": band.name}
        if band.common_name:
            entry["eo:common_name"] = band.common_name
        if band.scale is not None:
            entry["raster:scale"] = band.scale
        if band.offset is not None:
            entry["raster:offset"] = band.offset
        if band.units:
            entry["unit"] = band.units
        entry["data_type"] = data_type
        if nodata is not None:
            entry["nodata"] = nodata
        out.append(entry)
    return out


def _cube(store: ChronoStore, bounds: tuple[float, float, float, float]) -> tuple[dict, dict]:
    level = store.levels[0]
    left, bottom, right, top = bounds
    crs = _epsg_number(store.crs.to_string())
    times = list(store.attrs.times)
    dimensions = {
        "x": {
            "type": "spatial",
            "axis": "x",
            "extent": [left, right],
            "step": level.transform[0],
            "reference_system": crs,
        },
        "y": {
            "type": "spatial",
            "axis": "y",
            "extent": [bottom, top],
            "step": level.transform[4],
            "reference_system": crs,
        },
        "time": {
            "type": "temporal",
            "extent": [times[0], times[-1]],
            "values": times,
            "step": None,
        },
        "band": {"type": "bands", "values": list(store.bands)},
    }
    data_variable: dict[str, Any] = {
        "type": "data",
        "dimensions": ["time", "band", "y", "x"],
        "data_type": str(level.data.dtype),
        "description": "Pixel values of the store at pyramid level 0 (stored at the native "
        "resolution, 2x coarser per level).",
    }
    if store.attrs.nodata is not None:
        data_variable["nodata"] = store.attrs.nodata
    variables = {"data": data_variable}
    return dimensions, variables


def _source_links(provenance: Any) -> list[dict[str, str]]:
    sources = provenance.get("sources", []) if isinstance(provenance, dict) else []
    return [
        {"rel": "derived_from", "href": s, "type": "application/json"}
        for s in sources
        if isinstance(s, str) and s.startswith(("http://", "https://"))
    ]


def build_stac(
    store: ChronoStore,
    *,
    href: str,
    id: str,
    title: str | None = None,
    description: str | None = None,
    license: str = "proprietary",
    collection_href: str = "../collection.json",
    item_href: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The Collection and Item documents for `store`, whose Zarr root is at `href`."""
    if store.attrs.spec_version is None:
        raise ValueError(
            "this store has no chronozarr block, and the STAC documents describe chronozarr "
            "stores: write one with chronozarr convert"
        )
    level = store.levels[0]
    a, _, c, _, e, f = level.transform
    height, width = level.shape[2:]
    bounds = (c, f + e * height, c + a * width, f)  # left, bottom, right, top
    stored_crs = store.crs.to_string()
    geometry, bbox = _footprint(stored_crs, bounds)
    times = list(store.attrs.times)
    dimensions, variables = _cube(store, bounds)
    bands = _band_objects(store)
    provenance = store.attrs.provenance
    provenance = provenance if isinstance(provenance, dict) else None
    text = description or (
        f"chronozarr time series: {len(times)} timesteps, {len(bands)} bands, "
        f"{abs(a):g} {schema.axis_unit(stored_crs)} pixels in {store.crs.describe()}, "
        f"{len(store.levels)} pyramid levels."
    )
    asset = {
        "href": href,
        "type": ZARR_MEDIA_TYPE,
        "title": "chronozarr Zarr v3 store",
        "description": "Root of the store. Levels are the groups 0, 1, 2, ...; open it with "
        "chronozarr.open_store or the chronozarr viewer.",
        "roles": ["data"],
    }
    properties: dict[str, Any] = {
        "datetime": None,
        "start_datetime": times[0],
        "end_datetime": times[-1],
        "title": title or id,
        **_proj_properties(store.crs),
        "proj:shape": [height, width],
        "proj:transform": [a, 0.0, c, 0.0, e, f, 0.0, 0.0, 1.0],
        "proj:bbox": list(bounds),
        "bands": bands,
        "cube:dimensions": dimensions,
        "cube:variables": variables,
        "chronozarr:spec_version": store.attrs.spec_version,
        "chronozarr:zarr_conventions": [
            schema.registration(n) for n in ("multiscales", "proj", "spatial")
        ],
        "chronozarr:levels": [
            {
                "path": str(lv.index),
                "resolution": lv.resolution,
                "shape": list(lv.shape),
                "grid": list(lv.grid),
            }
            for lv in store.levels
        ],
    }
    if provenance is not None:
        properties["chronozarr:provenance"] = provenance

    extensions = [EXT_DATACUBE, EXT_PROJECTION]
    if any("eo:common_name" in b for b in bands):
        extensions.append(EXT_EO)
    if any("raster:scale" in b for b in bands):
        extensions.append(EXT_RASTER)

    item_name = item_href or f"./{id}.json"
    item: dict[str, Any] = {
        "type": "Feature",
        "stac_version": STAC_VERSION,
        "stac_extensions": extensions,
        "id": id,
        "geometry": geometry,
        "bbox": bbox,
        "properties": properties,
        "links": [
            {"rel": "self", "href": item_name, "type": "application/geo+json"},
            {"rel": "root", "href": collection_href, "type": "application/json"},
            {"rel": "parent", "href": collection_href, "type": "application/json"},
            {"rel": "collection", "href": collection_href, "type": "application/json"},
            *_source_links(provenance),
        ],
        "assets": {"zarr": asset},
        "collection": id,
    }
    collection: dict[str, Any] = {
        "type": "Collection",
        "stac_version": STAC_VERSION,
        "stac_extensions": [EXT_DATACUBE],
        "id": id,
        "title": title or id,
        "description": text,
        "license": license,
        "extent": {
            "spatial": {"bbox": [bbox]},
            "temporal": {"interval": [[times[0], times[-1]]]},
        },
        "summaries": _proj_summaries(store.crs),
        "cube:dimensions": dimensions,
        "cube:variables": variables,
        "links": [
            {"rel": "self", "href": "./collection.json", "type": "application/json"},
            {"rel": "root", "href": "./collection.json", "type": "application/json"},
            {"rel": "item", "href": f"./{id}/{id}.json", "type": "application/geo+json"},
            *_source_links(provenance),
        ],
    }
    return collection, item


def write_stac(
    store: ChronoStore | str | Path,
    out_dir: str | Path,
    *,
    href: str | None = None,
    id: str | None = None,
    title: str | None = None,
    description: str | None = None,
    license: str = "proprietary",
) -> tuple[Path, Path]:
    """Write `collection.json` and `<id>/<id>.json` under `out_dir`; returns both paths.

    `store` is an opened store, a local path or an https URL. `href` is where the Zarr asset
    lives for consumers: default the URL itself, or for a local store the relative path from the
    Item file to the store directory. Existing STAC files are overwritten.
    """
    source = store.source if isinstance(store, ChronoStore) else str(store)
    opened = store if isinstance(store, ChronoStore) else open_store(store)
    stac_id = id or default_id(source)
    out = Path(out_dir)
    item_dir = out / stac_id
    item_dir.mkdir(parents=True, exist_ok=True)
    if href is None:
        href = (
            source
            if is_url(source)
            else Path(os.path.relpath(Path(source).resolve(), item_dir.resolve())).as_posix()
        )
    collection, item = build_stac(
        opened,
        href=href,
        id=stac_id,
        title=title,
        description=description,
        license=license,
        item_href=f"./{stac_id}.json",
    )
    collection_path = out / "collection.json"
    item_path = item_dir / f"{stac_id}.json"
    for path, document in ((collection_path, collection), (item_path, item)):
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return collection_path, item_path
