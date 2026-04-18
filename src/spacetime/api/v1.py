"""TileRipper v1 API router.

All public endpoints live here. The router is mounted at /v1/ by serve.py.

Endpoints:
    GET /v1/                        API info
    GET /v1/catalog                 List AOIs + products
    GET /v1/catalog/{aoi}           AOI detail
    GET /v1/products                Product catalog
    GET /v1/months/{aoi}            Available months
    GET /v1/tiles/{aoi}/{month}/{chunk_id}  Rendered tile image
    GET /v1/query/{aoi}/{month}     Point query (maps + values)
    GET /v1/stats/{aoi}/{month}     AOI summary statistics
    GET /v1/usage                   Current usage for this key
    GET /v1/raw/{aoi}/{month}/{chunk_id}  Raw uint16 band data (gzipped)
    POST /v1/jobs                   Submit a new processing job
    GET /v1/jobs                    List jobs for current API key
    GET /v1/jobs/{job_id}           Get job detail
    GET /v1/jobs/next               Worker endpoint to claim next pending job
    POST /v1/jobs/{job_id}/complete Worker marks job done
    POST /v1/jobs/{job_id}/fail     Worker marks job failed
"""

from __future__ import annotations

import gzip
import io
import logging
import os
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
import zarr
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from PIL import Image
from pydantic import BaseModel, Field

try:
    import pyvips

    HAS_PYVIPS = True
except (ImportError, OSError):
    pyvips = None
    HAS_PYVIPS = False
from spacetime.api.geo import latlng_to_pixel, pixel_rowcol_to_coords, pixel_to_latlng
from spacetime.api.jobs import JobDB, JobDetail, JobStatus, JobSubmission
from spacetime.api.metering import meter
from spacetime.api.models import (
    AOISummary,
    CatalogResponse,
    ErrorResponse,
    GridInfo,
    MonthsResponse,
    PixelLocation,
    PointQueryResponse,
    ProductInfo,
    StatsResponse,
    UsageResponse,
)
from spacetime.api.products import PRODUCTS, products_for_tier, tier_can_access
from spacetime.render import render_product, water_mask

logger = logging.getLogger(__name__)


def _encode_pyvips(rendered: np.ndarray, fmt: str, quality: int = 85) -> tuple[bytes, str]:
    """Encode rendered image to bytes using pyvips.

    Args:
        rendered: RGB array (H, W, 3) as uint8
        fmt: 'jpeg' or 'png'
        quality: JPEG quality (1-100), ignored for PNG

    Returns:
        (image_bytes, mime_type)
    """
    assert HAS_PYVIPS and pyvips is not None
    h, w = rendered.shape[:2]
    vimg = pyvips.Image.new_from_memory(rendered.tobytes(), w, h, 3, "uchar")
    if fmt == "png":
        return (vimg.pngsave_buffer(), "image/png")
    return (vimg.jpegsave_buffer(Q=quality), "image/jpeg")


router = APIRouter(prefix="/v1", tags=["v1"])
AOI_CATALOG: dict[str, dict] = {}
JOB_DB: JobDB | None = None
WORKER_KEY = os.environ.get("TILERIPPER_WORKER_KEY")
_BAND_CACHE_SIZE = 512


@lru_cache(maxsize=_BAND_CACHE_SIZE)
def _load_bands(store_dir: str, chunk_id: str, month_index: int) -> np.ndarray:
    """Load raw multiband data for a chunk/month. Cached for product switching."""
    zarr_path = Path(store_dir) / chunk_id / "stack.zarr"
    z = zarr.open(str(zarr_path), mode="r")
    return np.array(z[month_index])


def _render_tile(bands: np.ndarray, product: str, fmt: str = "jpeg") -> tuple[bytes, str]:
    """Render a product from bands and encode as image bytes."""
    from spacetime.render import ndvi_colormap, ndwi_colormap

    rendered = render_product(bands, product)
    if rendered.dtype == np.float32:
        cmap = ndwi_colormap if product == "ndwi" else ndvi_colormap
        rendered = cmap(rendered)
    if HAS_PYVIPS:
        return _encode_pyvips(rendered, fmt, quality=85)
    img = Image.fromarray(rendered)
    buf = io.BytesIO()
    if fmt == "png":
        img.save(buf, format="PNG", optimize=False)
        return (buf.getvalue(), "image/png")
    img.save(buf, format="JPEG", quality=85)
    return (buf.getvalue(), "image/jpeg")


CACHE_HEADER = "public, max-age=86400, immutable"


def _get_aoi(aoi: str) -> dict:
    """Look up AOI metadata, raising 404 if not found."""
    if aoi in AOI_CATALOG:
        return AOI_CATALOG[aoi]
    for suffix in ["/cs512", "/cs256", "/cs1024"]:
        key = aoi + suffix
        if key in AOI_CATALOG:
            return AOI_CATALOG[key]
    raise HTTPException(404, detail=f"Unknown AOI: {aoi}")


def _get_aoi_key(aoi: str) -> str:
    """Resolve the full AOI key including chunk size."""
    if aoi in AOI_CATALOG:
        return aoi
    for suffix in ["/cs512", "/cs256", "/cs1024"]:
        key = aoi + suffix
        if key in AOI_CATALOG:
            return key
    raise HTTPException(404, detail=f"Unknown AOI: {aoi}")


def _check_product_access(request: Request, product: str) -> None:
    """Raise 403 if the request's tier cannot access this product."""
    tier = request.state.tier
    if not tier_can_access(tier, product):
        required = PRODUCTS[product]["tier"]
        raise HTTPException(
            403,
            detail=(
                f"Product '{product}' requires {required} tier or above. "
                f"Current tier: {tier}. Upgrade at https://tileripper.dev/pricing"
            ),
        )


@router.get("/")
def api_info():
    """API root — version and links."""
    return {
        "name": "TileRipper",
        "version": "0.1.0",
        "description": "Temporally coherent earth observation basemaps with queryable values",
        "docs": "/docs",
        "endpoints": {
            "catalog": "/v1/catalog",
            "products": "/v1/products",
            "tiles": "/v1/tiles/{aoi}/{month}/{chunk_id}",
            "query": "/v1/query/{aoi}/{month}?lat=...&lng=...",
            "stats": "/v1/stats/{aoi}/{month}?product=ndvi",
        },
    }


@router.get("/catalog", response_model=CatalogResponse)
def get_catalog(request: Request):
    """List all available AOIs and products."""
    tier = request.state.tier
    accessible = products_for_tier(tier)
    aois = []
    seen = set()
    for _key, meta in AOI_CATALOG.items():
        aoi_id = meta["aoi"]
        if aoi_id in seen:
            continue
        seen.add(aoi_id)
        aois.append(
            AOISummary(
                id=aoi_id,
                name=meta.get("label", aoi_id),
                source=meta.get("source", "Sentinel-2 L2A"),
                epsg=meta["epsg"],
                months=meta["months"],
                n_months=meta["n_months"],
                grid=GridInfo(
                    n_rows=meta["n_rows"],
                    n_cols=meta["n_cols"],
                    chunk_size=meta["chunk_size"],
                    mosaic_height=meta["mosaic_height"],
                    mosaic_width=meta["mosaic_width"],
                ),
                products=accessible,
                bbox_wgs84=meta.get("bbox_wgs84"),
            )
        )
    products = [
        ProductInfo(**{k: v for k, v in p.items() if k != "range"}, **{"range": p["range"]})
        if p["range"] is not None
        else ProductInfo(**p)
        for p in PRODUCTS.values()
    ]
    return CatalogResponse(aois=aois, products=products)


@router.get("/catalog/{aoi}", response_model=AOISummary)
def get_aoi_detail(aoi: str, request: Request):
    """Get detailed info for a specific AOI."""
    meta = _get_aoi(aoi)
    tier = request.state.tier
    return AOISummary(
        id=meta["aoi"],
        name=meta.get("label", meta["aoi"]),
        source=meta.get("source", "Sentinel-2 L2A"),
        epsg=meta["epsg"],
        months=meta["months"],
        n_months=meta["n_months"],
        grid=GridInfo(
            n_rows=meta["n_rows"],
            n_cols=meta["n_cols"],
            chunk_size=meta["chunk_size"],
            mosaic_height=meta["mosaic_height"],
            mosaic_width=meta["mosaic_width"],
        ),
        products=products_for_tier(tier),
        bbox_wgs84=meta.get("bbox_wgs84"),
    )


@router.get("/products", response_model=list[ProductInfo])
def list_products(request: Request):
    """List all available products and their tier requirements."""
    return [
        ProductInfo(**{k: v for k, v in p.items() if k != "range"}, **{"range": p["range"]})
        if p["range"] is not None
        else ProductInfo(**p)
        for p in PRODUCTS.values()
    ]


@router.get("/months/{aoi}", response_model=MonthsResponse)
def get_months(aoi: str):
    """List available months for an AOI."""
    meta = _get_aoi(aoi)
    months = meta["months"]
    return MonthsResponse(
        aoi=meta["aoi"], months=months, n_months=len(months), first=months[0], last=months[-1]
    )


@router.get(
    "/tiles/{aoi}/{month}/{chunk_id}",
    responses={200: {"content": {"image/jpeg": {}, "image/png": {}}}},
)
def get_tile(
    aoi: str,
    month: str,
    chunk_id: str,
    request: Request,
    product: str = Query(default="true_color", description="Product to render"),
    fmt: str = Query(default="jpeg", description="Image format: jpeg or png"),
):
    """Serve a rendered tile image.

    The core endpoint: raw bands are loaded from Zarr (cached), and the
    requested product is derived on demand. Month can be an index (0-23)
    or a YYYY-MM string.
    """
    t0 = time.perf_counter()
    _check_product_access(request, product)
    aoi_key = _get_aoi_key(aoi)
    meta = AOI_CATALOG[aoi_key]
    month_index = _resolve_month(month, meta)
    if chunk_id not in meta["chunk_ids"]:
        raise HTTPException(404, detail=f"Unknown chunk: {chunk_id}")
    render_product_id = product
    if product in ("ndvi", "ndwi"):
        render_product_id = product
    cache_info_before = _load_bands.cache_info()
    t_zarr_start = time.perf_counter()
    bands = _load_bands(meta["store_dir"], chunk_id, month_index)
    t_zarr_end = time.perf_counter()
    cache_info_after = _load_bands.cache_info()
    cache_miss = (
        cache_info_after.misses > cache_info_before.misses
        or cache_info_after.currsize > cache_info_before.currsize
    )
    zarr_ms = (t_zarr_end - t_zarr_start) * 1000 if cache_miss else 0.0
    t_render_start = time.perf_counter()
    from spacetime.render import ndvi_colormap, ndwi_colormap, render_product

    rendered = render_product(bands, render_product_id)
    if rendered.dtype == np.float32:
        cmap = ndwi_colormap if product == "ndwi" else ndvi_colormap
        rendered = cmap(rendered)
    t_render_end = time.perf_counter()
    render_ms = (t_render_end - t_render_start) * 1000
    t_encode_start = time.perf_counter()
    if HAS_PYVIPS:
        img_bytes, media_type = _encode_pyvips(rendered, fmt, quality=85)
    else:
        img = Image.fromarray(rendered)
        buf = io.BytesIO()
        if fmt == "png":
            img.save(buf, format="PNG", optimize=False)
            media_type = "image/png"
        else:
            img.save(buf, format="JPEG", quality=85)
            media_type = "image/jpeg"
        img_bytes = buf.getvalue()
    t_encode_end = time.perf_counter()
    encode_ms = (t_encode_end - t_encode_start) * 1000
    total_ms = (time.perf_counter() - t0) * 1000
    logger.info(
        "tile %s/%s/%s product=%s: zarr=%.1fms render=%.1fms encode=%.1fms total=%.1fms",
        aoi,
        month,
        chunk_id,
        product,
        zarr_ms,
        render_ms,
        encode_ms,
        total_ms,
    )
    meter.record(request.state.api_key, "tile", bytes_served=len(img_bytes))
    return Response(
        content=img_bytes,
        media_type=media_type,
        headers={"Cache-Control": CACHE_HEADER, "X-Timing-Ms": f"{total_ms:.1f}"},
    )


@router.get(
    "/query/{aoi}/{month}",
    response_model=PointQueryResponse,
    responses={400: {"model": ErrorResponse}},
)
def point_query(
    aoi: str,
    month: str,
    request: Request,
    lat: float | None = Query(default=None, description="Latitude (WGS84)"),
    lng: float | None = Query(default=None, description="Longitude (WGS84)"),
    pixel_row: int | None = Query(default=None, description="Mosaic pixel row"),
    pixel_col: int | None = Query(default=None, description="Mosaic pixel column"),
):
    """Query band values and derived products at a geographic point.

    This is the 'maps plus values' endpoint. Provide either lat/lng (WGS84)
    or pixel_row/pixel_col (mosaic coordinates).

    Returns raw band reflectance, NDVI, NDWI, water classification, and
    source metadata for the queried point.
    """
    meta = _get_aoi(aoi)
    month_index = _resolve_month(month, meta)
    month_str = meta["months"][month_index]
    if lat is not None and lng is not None:
        coords = latlng_to_pixel(
            lat,
            lng,
            meta["epsg"],
            meta["transform"],
            meta["chunk_size"],
            meta["mosaic_height"],
            meta["mosaic_width"],
        )
        if coords is None:
            raise HTTPException(400, detail=f"Point ({lat}, {lng}) falls outside AOI bounds")
    elif pixel_row is not None and pixel_col is not None:
        coords = pixel_rowcol_to_coords(
            pixel_row,
            pixel_col,
            meta["epsg"],
            meta["transform"],
            meta["chunk_size"],
            meta["mosaic_height"],
            meta["mosaic_width"],
        )
        if coords is None:
            raise HTTPException(
                400, detail=f"Pixel ({pixel_row}, {pixel_col}) outside mosaic bounds"
            )
        lat, lng = pixel_to_latlng(pixel_row, pixel_col, meta["epsg"], meta["transform"])
    else:
        raise HTTPException(400, detail="Provide either lat+lng or pixel_row+pixel_col")
    bands = _load_bands(meta["store_dir"], coords.chunk_id, month_index)
    lr, lc = (coords.local_row, coords.local_col)
    h, w = (bands.shape[1], bands.shape[2])
    if lr >= h or lc >= w:
        raise HTTPException(400, detail="Pixel outside chunk bounds (edge chunk)")
    pixel_bands = bands[:, lr, lc]
    b02, b03, b04, b08 = [int(pixel_bands[i]) for i in range(4)]
    nir_f, red_f, green_f = (float(b08), float(b04), float(b03))
    ndvi_val = _safe_ratio(nir_f - red_f, nir_f + red_f)
    ndwi_val = _safe_ratio(green_f - nir_f, green_f + nir_f)
    is_water = ndwi_val is not None and ndwi_val > 0
    tier = request.state.tier
    values: dict[str, float | None] = {}
    if tier_can_access(tier, "ndvi"):
        values["ndvi"] = _round(ndvi_val)
    if tier_can_access(tier, "ndwi"):
        values["ndwi"] = _round(ndwi_val)
    if tier_can_access(tier, "water"):
        values["water"] = 1.0 if is_water else 0.0
    meter.record(request.state.api_key, "query")
    return PointQueryResponse(
        aoi=meta["aoi"],
        month=month_str,
        lat=round(lat, 6),
        lng=round(lng, 6),
        pixel=PixelLocation(
            row=coords.pixel_row,
            col=coords.pixel_col,
            chunk_id=coords.chunk_id,
            utm_x=round(coords.utm_x, 2),
            utm_y=round(coords.utm_y, 2),
        ),
        values=values,
        bands={"B02": b02, "B03": b03, "B04": b04, "B08": b08},
        source=meta.get("source", "Sentinel-2 L2A"),
        composite_method=meta.get("composite_method", "monthly median, SCL cloud mask"),
    )


@router.get(
    "/stats/{aoi}/{month}", response_model=StatsResponse, responses={400: {"model": ErrorResponse}}
)
def get_stats(
    aoi: str,
    month: str,
    request: Request,
    product: str = Query(default="ndvi", description="Product to compute stats for"),
):
    """Compute summary statistics for a product over the full AOI.

    Returns mean, median, std, min, max, p10, p90, and valid pixel counts.
    """
    _check_product_access(request, product)
    meta = _get_aoi(aoi)
    month_index = _resolve_month(month, meta)
    month_str = meta["months"][month_index]
    all_values = []
    total_pixels = 0
    for chunk_id in meta["chunk_ids"]:
        bands = _load_bands(meta["store_dir"], chunk_id, month_index)
        rendered = render_product(bands, product)
        if rendered.dtype == np.float32:
            valid = rendered[~np.isnan(rendered)]
            all_values.append(valid.ravel())
            total_pixels += rendered.size
        elif product == "water":
            mask = water_mask(bands)
            nodata = bands[0].astype(np.float32) + bands[3].astype(np.float32) == 0
            valid_mask = ~nodata
            all_values.append(mask[valid_mask].astype(np.float32).ravel())
            total_pixels += mask.size
        else:
            brightness = rendered.mean(axis=-1).astype(np.float32)
            valid = brightness[brightness > 0]
            all_values.append(valid.ravel())
            total_pixels += brightness.size
    combined = np.concatenate(all_values) if all_values else np.array([])
    valid_pixels = len(combined)
    if valid_pixels == 0:
        raise HTTPException(400, detail="No valid pixels for this AOI/month/product")
    stats = {
        "mean": _round(float(np.mean(combined))),
        "median": _round(float(np.median(combined))),
        "std": _round(float(np.std(combined))),
        "min": _round(float(np.min(combined))),
        "max": _round(float(np.max(combined))),
        "p10": _round(float(np.percentile(combined, 10))),
        "p90": _round(float(np.percentile(combined, 90))),
    }
    meter.record(request.state.api_key, "stats")
    return StatsResponse(
        aoi=meta["aoi"],
        month=month_str,
        product=product,
        stats=stats,
        valid_pixels=valid_pixels,
        total_pixels=total_pixels,
    )


@router.get("/usage", response_model=UsageResponse)
def get_usage(request: Request):
    """Get current usage for this API key."""
    from spacetime.api.auth import TIERS

    key = request.state.api_key
    tier = request.state.tier
    usage = meter.get(key)
    quota = TIERS[tier]["monthly_quota"]
    return UsageResponse(
        tier=tier,
        period="current",
        requests=usage.requests,
        tile_requests=usage.tile_requests,
        query_requests=usage.query_requests,
        quota=quota,
    )


@router.get(
    "/raw/{aoi}/{month}/{chunk_id}", responses={200: {"content": {"application/octet-stream": {}}}}
)
def get_raw_bands(aoi: str, month: str, chunk_id: str, request: Request):
    """Serve raw uint16 band data as gzipped binary.

    Returns B02, B03, B04, B08 band data in a compact binary format:
    - 8-byte header: uint16 n_bands, uint16 height, uint16 width, uint16 reserved(0)
    - Followed by n_bands x height x width x 2 bytes of uint16 little-endian data

    No tier gating — raw bands are available to all tiers.
    """
    aoi_key = _get_aoi_key(aoi)
    meta = AOI_CATALOG[aoi_key]
    month_index = _resolve_month(month, meta)
    if chunk_id not in meta["chunk_ids"]:
        raise HTTPException(404, detail=f"Unknown chunk: {chunk_id}")
    bands = _load_bands(meta["store_dir"], chunk_id, month_index)
    n_bands, height, width = bands.shape
    header = np.array([n_bands, height, width, 0], dtype=np.uint16).tobytes()
    data_bytes = bands.astype(np.uint16, copy=False).tobytes()
    raw_bytes = header + data_bytes
    compressed = gzip.compress(raw_bytes, compresslevel=1)
    meter.record(request.state.api_key, "raw", bytes_served=len(compressed))
    return Response(
        content=compressed,
        media_type="application/octet-stream",
        headers={"Cache-Control": CACHE_HEADER, "Content-Encoding": "gzip"},
    )


def _check_worker_key(request: Request) -> None:
    """Validate worker key from X-Worker-Key header.

    Raises:
        HTTPException: 503 if worker key not configured, 401 if invalid key provided
    """
    if WORKER_KEY is None:
        raise HTTPException(
            status_code=503,
            detail="Worker endpoints are not configured (TILERIPPER_WORKER_KEY not set)",
        )
    provided_key = request.headers.get("X-Worker-Key")
    if provided_key != WORKER_KEY:
        raise HTTPException(status_code=401, detail="Invalid worker key")


@router.post(
    "/jobs",
    response_model=JobStatus,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 422: {"model": ErrorResponse}},
)
def submit_job(submission: JobSubmission, request: Request):
    """Submit a new AOI processing job.

    Requires authentication (any tier). Auto-detects UTM zone from bbox
    centroid. Returns the job ID and initial status of 'pending'.

    - bbox_area_km2 must be < 10,000 km²
    - Date range must not exceed 36 months
    """
    if JOB_DB is None:
        raise HTTPException(503, detail="Job database not initialized")
    api_key = request.state.api_key
    try:
        job_id = JOB_DB.submit(submission, api_key)
    except ValueError as e:
        raise HTTPException(400, detail=str(e)) from e
    return JobStatus(id=job_id, status="pending", aoi_name=submission.aoi_name, created_at=None)


@router.get("/jobs", response_model=list[JobStatus])
def list_jobs(
    request: Request,
    status: str | None = Query(
        default=None, description="Filter by status (pending, claimed, done, failed)"
    ),
):
    """List all jobs submitted by the current API key.

    Optionally filter by status. Returns job summaries with id, status,
    aoi_name, and timestamps.
    """
    if JOB_DB is None:
        raise HTTPException(503, detail="Job database not initialized")
    api_key = request.state.api_key
    jobs = JOB_DB.list_for_key(api_key)
    if status:
        jobs = [j for j in jobs if j.status == status]
    return jobs


@router.get("/jobs/next", response_model=JobDetail | None)
def claim_next_job(
    request: Request, worker_id: str = Query(default="default", description="Worker identifier")
):
    """Worker endpoint: atomically claim the oldest pending job.

    Requires X-Worker-Key header matching TILERIPPER_WORKER_KEY.
    Returns the claimed job details or 204 No Content if no pending jobs.
    """
    _check_worker_key(request)
    if JOB_DB is None:
        raise HTTPException(503, detail="Job database not initialized")
    job = JOB_DB.claim(worker_id)
    if job is None:
        raise HTTPException(204)
    return job


@router.get("/jobs/{job_id}", response_model=JobDetail)
def get_job_detail(job_id: str, request: Request):
    """Get full details of a specific job.

    Accessible only by the API key that submitted the job, or by dev tier.
    """
    if JOB_DB is None:
        raise HTTPException(503, detail="Job database not initialized")
    job = JOB_DB.get(job_id)
    if job is None:
        raise HTTPException(404, detail=f"Job not found: {job_id}")
    api_key = request.state.api_key
    tier = request.state.tier
    if job.api_key != api_key and tier != "dev":
        raise HTTPException(403, detail="Access denied: job owned by different API key")
    return job


class CompleteJobRequest(BaseModel):
    """Request body for marking a job as complete."""

    store_path: str = Field(..., description="Path to the generated AOI store")


@router.post("/jobs/{job_id}/complete")
def complete_job(job_id: str, body: CompleteJobRequest, request: Request):
    """Worker endpoint: mark a job as successfully completed.

    Requires X-Worker-Key header. Updates status to 'done' and triggers
    hot-reload of the AOI catalog to include the new data.
    """
    _check_worker_key(request)
    if JOB_DB is None:
        raise HTTPException(503, detail="Job database not initialized")
    updated = JOB_DB.update_status(job_id, "done", store_path=body.store_path)
    if not updated:
        raise HTTPException(404, detail=f"Job not found: {job_id}")
    from spacetime.serve import _discover_aois

    new_catalog = _discover_aois()
    AOI_CATALOG.clear()
    AOI_CATALOG.update(new_catalog)
    logger.info("Job %s completed, reloaded AOI catalog (%d stores)", job_id, len(AOI_CATALOG))
    return {"status": "done", "job_id": job_id}


class FailJobRequest(BaseModel):
    """Request body for marking a job as failed."""

    error: str = Field(..., description="Error message describing the failure")


@router.post("/jobs/{job_id}/fail")
def fail_job(job_id: str, body: FailJobRequest, request: Request):
    """Worker endpoint: mark a job as failed.

    Requires X-Worker-Key header. Updates status to 'failed' and stores
    the error message.
    """
    _check_worker_key(request)
    if JOB_DB is None:
        raise HTTPException(503, detail="Job database not initialized")
    updated = JOB_DB.update_status(job_id, "failed", error=body.error)
    if not updated:
        raise HTTPException(404, detail=f"Job not found: {job_id}")
    logger.warning("Job %s failed: %s", job_id, body.error)
    return {"status": "failed", "job_id": job_id, "error": body.error}


def _resolve_month(month: str, meta: dict) -> int:
    """Resolve a month string (index or YYYY-MM) to a valid month index."""
    try:
        idx = int(month)
        if 0 <= idx < meta["n_months"]:
            return idx
        raise HTTPException(400, detail=f"Month index {idx} out of range [0, {meta['n_months']})")
    except ValueError:
        pass
    if month in meta["months"]:
        return meta["months"].index(month)
    raise HTTPException(
        400,
        detail=(
            f"Unknown month '{month}'. Use an index (0-{meta['n_months'] - 1}) "
            f"or YYYY-MM string (e.g. '{meta['months'][0]}')"
        ),
    )


def _safe_ratio(num: float, denom: float) -> float | None:
    """Compute a ratio, returning None if denominator is zero."""
    if denom == 0:
        return None
    return num / denom


def _round(val: float | None, digits: int = 4) -> float | None:
    """Round a float value, passing through None."""
    if val is None:
        return None
    return round(val, digits)
