"""NAIP API router — serves tiles directly from Planetary Computer COGs.

No local storage. Tiles are read on demand, rendered, and cached in memory.

Endpoints:
    GET /v1/naip/catalog          List NAIP AOIs + available years
    GET /v1/naip/years/{aoi}      Available years for an AOI
    GET /v1/naip/tiles/{aoi}/{year}/{chunk_id}  Rendered tile
    GET /v1/naip/query/{aoi}/{year}  Point query
"""

from __future__ import annotations

import io
import logging
import time

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from PIL import Image

from spacetime.api.geo import latlng_to_pixel
from spacetime.api.metering import meter
from spacetime.naip import (
    CHUNK_SIZE,
    NaipAoi,
    discover_naip_aoi,
    read_naip_tile_cached,
    render_naip_product,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/naip", tags=["naip"])

# Populated at startup by serve.py
NAIP_CATALOG: dict[str, NaipAoi] = {}

CACHE_HEADER = "public, max-age=86400, immutable"


def _get_naip_aoi(aoi: str) -> NaipAoi:
    """Look up and lazily discover a NAIP AOI."""
    if aoi not in NAIP_CATALOG:
        raise HTTPException(404, detail=f"Unknown NAIP AOI: {aoi}")
    naip_aoi = NAIP_CATALOG[aoi]
    if not naip_aoi._discovered:
        discover_naip_aoi(naip_aoi)
    return naip_aoi


@router.get("/catalog")
def naip_catalog():
    """List all configured NAIP AOIs and their available years."""
    results = []
    for name, aoi in NAIP_CATALOG.items():
        if not aoi._discovered:
            discover_naip_aoi(aoi)
        results.append(
            {
                "id": name,
                "name": aoi.label,
                "source": "NAIP",
                "bbox_wgs84": list(aoi.bbox),
                "epsg": aoi.epsg,
                "resolution_m": aoi.resolution,
                "years": aoi.years,
                "n_years": len(aoi.years),
                "grid_rows": aoi.grid_rows,
                "grid_cols": aoi.grid_cols,
                "n_chunks": len(aoi.chunk_ids),
                "height": aoi.height,
                "width": aoi.width,
                "products": ["true_color", "false_color", "ndvi", "ndwi"],
            }
        )
    return {"aois": results}


@router.get("/years/{aoi}")
def naip_years(aoi: str):
    """List available NAIP years for an AOI."""
    naip_aoi = _get_naip_aoi(aoi)
    return {
        "aoi": aoi,
        "years": naip_aoi.years,
        "n_years": len(naip_aoi.years),
        "items_per_year": {y: len(items) for y, items in naip_aoi.items_by_year.items()},
    }


@router.get(
    "/tiles/{aoi}/{year}/{chunk_id}",
    responses={200: {"content": {"image/jpeg": {}, "image/png": {}}}},
)
def naip_tile(
    aoi: str,
    year: str,
    chunk_id: str,
    request: Request,
    product: str = Query(
        default="true_color", description="Product: true_color, false_color, ndvi, ndwi"
    ),
    fmt: str = Query(default="jpeg", description="Image format: jpeg or png"),
):
    """Serve a rendered NAIP tile, read directly from Planetary Computer COGs."""
    naip_aoi = _get_naip_aoi(aoi)

    if year not in naip_aoi.years:
        raise HTTPException(400, detail=f"Year {year} not available. Available: {naip_aoi.years}")
    if chunk_id not in naip_aoi.chunk_ids:
        raise HTTPException(404, detail=f"Unknown chunk: {chunk_id}")

    t0 = time.perf_counter()

    # Read tile (cached)
    bands = read_naip_tile_cached(naip_aoi, year, chunk_id)
    t_read = time.perf_counter()

    # Render product
    rendered = render_naip_product(bands, product)
    t_render = time.perf_counter()

    # Encode to image
    img = Image.fromarray(rendered)
    buf = io.BytesIO()
    if fmt == "png":
        img.save(buf, format="PNG", optimize=False)
        media_type = "image/png"
    else:
        img.save(buf, format="JPEG", quality=85)
        media_type = "image/jpeg"
    img_bytes = buf.getvalue()
    t_encode = time.perf_counter()

    total_ms = (t_encode - t0) * 1000
    read_ms = (t_read - t0) * 1000
    render_ms = (t_render - t_read) * 1000
    encode_ms = (t_encode - t_render) * 1000

    logger.info(
        "naip tile %s/%s/%s product=%s: read=%.0fms render=%.0fms encode=%.0fms total=%.0fms",
        aoi,
        year,
        chunk_id,
        product,
        read_ms,
        render_ms,
        encode_ms,
        total_ms,
    )

    meter.record(request.state.api_key, "tile", bytes_served=len(img_bytes))

    return Response(
        content=img_bytes,
        media_type=media_type,
        headers={
            "Cache-Control": CACHE_HEADER,
            "X-Timing-Ms": f"{total_ms:.1f}",
            "X-Source": "naip-cog-proxy",
        },
    )


@router.get("/query/{aoi}/{year}")
def naip_query(
    aoi: str,
    year: str,
    request: Request,
    lat: float = Query(..., description="Latitude (WGS84)"),
    lng: float = Query(..., description="Longitude (WGS84)"),
):
    """Query NAIP band values at a geographic point.

    Returns raw R, G, B, NIR values plus NDVI and NDWI.
    """
    naip_aoi = _get_naip_aoi(aoi)

    if year not in naip_aoi.years:
        raise HTTPException(400, detail=f"Year {year} not available. Available: {naip_aoi.years}")

    # Convert lat/lng to pixel coordinates
    transform_list = [
        naip_aoi.transform.a,
        naip_aoi.transform.b,
        naip_aoi.transform.c,
        naip_aoi.transform.d,
        naip_aoi.transform.e,
        naip_aoi.transform.f,
    ]
    coords = latlng_to_pixel(
        lat,
        lng,
        naip_aoi.epsg,
        transform_list,
        CHUNK_SIZE,
        naip_aoi.height,
        naip_aoi.width,
    )
    if coords is None:
        raise HTTPException(400, detail=f"Point ({lat}, {lng}) falls outside AOI bounds")

    # Read tile and extract pixel
    bands = read_naip_tile_cached(naip_aoi, year, coords.chunk_id)
    lr, lc = coords.local_row, coords.local_col
    if lr >= bands.shape[1] or lc >= bands.shape[2]:
        raise HTTPException(400, detail="Pixel outside chunk bounds (edge chunk)")

    pixel = bands[:, lr, lc]
    blue, green, red, nir = int(pixel[0]), int(pixel[1]), int(pixel[2]), int(pixel[3])

    # Compute indices
    nir_f, red_f, green_f = float(nir), float(red), float(green)
    denom_ndvi = nir_f + red_f
    denom_ndwi = green_f + nir_f
    ndvi_val = (nir_f - red_f) / denom_ndvi if denom_ndvi > 0 else None
    ndwi_val = (green_f - nir_f) / denom_ndwi if denom_ndwi > 0 else None

    meter.record(request.state.api_key, "query")

    return {
        "aoi": aoi,
        "year": year,
        "lat": round(lat, 6),
        "lng": round(lng, 6),
        "pixel": {
            "row": coords.pixel_row,
            "col": coords.pixel_col,
            "chunk_id": coords.chunk_id,
        },
        "bands": {"red": red, "green": green, "blue": blue, "nir": nir},
        "values": {
            "ndvi": round(ndvi_val, 4) if ndvi_val is not None else None,
            "ndwi": round(ndwi_val, 4) if ndwi_val is not None else None,
        },
        "source": "NAIP",
        "resolution_m": naip_aoi.resolution,
    }
