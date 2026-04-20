"""NAIP (National Agriculture Imagery Program) COG proxy.

Reads high-resolution (0.6-1m) aerial imagery directly from Planetary Computer
COGs via HTTP range requests. No local storage — full AOI mosaic is read once
per year and cached in RAM, then tiles are sliced from the cache.

NAIP band order in source COGs: R(1), G(2), B(3), NIR(4)
Internal band order (matching S2 convention): B(0), G(1), R(2), NIR(3)
"""

from __future__ import annotations

import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np
import planetary_computer as pc
import rasterio
from pystac_client import Client
from rasterio.crs import CRS
from rasterio.transform import Affine, from_bounds
from rasterio.warp import Resampling, reproject, transform_bounds

logger = logging.getLogger(__name__)

PC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
NAIP_COLLECTION = "naip"
CHUNK_SIZE = 512

# GDAL env for efficient COG reads
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "10000000",
}


@dataclass(frozen=True)
class NaipItem:
    """Reference to a single NAIP quadrangle."""

    item_id: str
    year: int
    state: str
    gsd: float  # ground sample distance in meters
    href: str  # signed COG URL
    bbox: tuple[float, float, float, float]  # WGS84
    epsg: int


@dataclass
class NaipAoi:
    """A configured NAIP AOI with cached item discovery."""

    name: str
    label: str
    bbox: tuple[float, float, float, float]  # WGS84 (lon_min, lat_min, lon_max, lat_max)
    epsg: int
    resolution: float  # meters per pixel
    # Computed at discovery time
    years: list[str] = field(default_factory=list)
    items_by_year: dict[str, list[NaipItem]] = field(default_factory=dict)
    transform: Affine | None = None
    height: int = 0
    width: int = 0
    grid_rows: int = 0
    grid_cols: int = 0
    chunk_ids: list[str] = field(default_factory=list)
    _discovered: bool = False


def discover_naip_aoi(aoi: NaipAoi) -> NaipAoi:
    """Query STAC for all NAIP items covering an AOI, grouped by year.

    Mutates and returns the AOI with discovery results populated.
    """
    if aoi._discovered:
        return aoi

    t0 = time.perf_counter()
    client = Client.open(PC_STAC_URL, modifier=pc.sign_inplace)

    search = client.search(
        collections=[NAIP_COLLECTION],
        bbox=aoi.bbox,
    )

    items_by_year: dict[str, list[NaipItem]] = {}
    for item in search.items():
        dt = item.datetime
        if dt is None:
            continue
        year = str(dt.year)

        # Get the image asset href
        if "image" not in item.assets:
            continue
        href = item.assets["image"].href

        # Extract metadata
        gsd = item.properties.get("gsd", 0.6)
        state = item.properties.get("naip:state", "")

        # Get CRS
        proj_code = item.properties.get("proj:epsg", 0)
        if proj_code == 0:
            proj_str = item.properties.get("proj:code", "")
            if proj_str.startswith("EPSG:"):
                proj_code = int(proj_str.split(":")[1])

        item_bbox = item.bbox if item.bbox else aoi.bbox

        naip_item = NaipItem(
            item_id=item.id,
            year=dt.year,
            state=state,
            gsd=gsd,
            href=href,
            bbox=tuple(item_bbox),
            epsg=proj_code or aoi.epsg,
        )
        items_by_year.setdefault(year, []).append(naip_item)

    # Compute target grid
    dst_crs = CRS.from_epsg(aoi.epsg)
    left, bottom, right, top = transform_bounds(CRS.from_epsg(4326), dst_crs, *aoi.bbox)

    # Snap to resolution grid
    res = aoi.resolution
    left = math.floor(left / res) * res
    bottom = math.floor(bottom / res) * res
    right = math.ceil(right / res) * res
    top = math.ceil(top / res) * res

    width = int((right - left) / res)
    height = int((top - bottom) / res)
    transform = from_bounds(left, bottom, right, top, width, height)

    grid_rows = math.ceil(height / CHUNK_SIZE)
    grid_cols = math.ceil(width / CHUNK_SIZE)
    chunk_ids = [f"r{r:03d}_c{c:03d}" for r in range(grid_rows) for c in range(grid_cols)]

    # Populate AOI
    aoi.years = sorted(items_by_year.keys())
    aoi.items_by_year = items_by_year
    aoi.transform = transform
    aoi.height = height
    aoi.width = width
    aoi.grid_rows = grid_rows
    aoi.grid_cols = grid_cols
    aoi.chunk_ids = chunk_ids
    aoi._discovered = True

    elapsed = time.perf_counter() - t0
    total_items = sum(len(v) for v in items_by_year.values())
    logger.info(
        "NAIP discover %s: %d years, %d items, %dx%d grid (%d chunks), %dx%d px in %.1fs",
        aoi.name,
        len(aoi.years),
        total_items,
        grid_cols,
        grid_rows,
        len(chunk_ids),
        width,
        height,
        elapsed,
    )
    return aoi


# ---- Full-mosaic cache ----
# Read the entire AOI extent once per year, then slice tiles from RAM.
# Key: (aoi_name, year) -> (4, H, W) uint8 array
_mosaic_cache: dict[tuple[str, str], np.ndarray] = {}


def _read_naip_item(
    item: NaipItem,
    dst_transform: Affine,
    dst_crs: CRS,
    dst_height: int,
    dst_width: int,
) -> np.ndarray:
    """Read one NAIP COG, reprojected into the target grid.

    Returns (4, H, W) uint8 in [B, G, R, NIR] order.
    """
    result = np.zeros((4, dst_height, dst_width), dtype=np.uint8)
    band_map = [(3, 0), (2, 1), (1, 2), (4, 3)]  # src_band -> dst_idx

    with rasterio.Env(**GDAL_ENV), rasterio.open(item.href) as src:
        for src_band, dst_idx in band_map:
            band_data = np.zeros((dst_height, dst_width), dtype=np.uint8)
            reproject(
                source=rasterio.band(src, src_band),
                destination=band_data,
                src_transform=src.transform,
                src_crs=src.crs,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
            )
            result[dst_idx] = band_data

    return result


def _read_naip_mosaic(aoi: NaipAoi, year: str) -> np.ndarray:
    """Read full AOI mosaic for a year from NAIP COGs. Cached in RAM.

    Reads all items for the year in parallel threads, composites into
    a single (4, H, W) uint8 array.
    """
    key = (aoi.name, year)
    if key in _mosaic_cache:
        return _mosaic_cache[key]

    items = aoi.items_by_year.get(year, [])
    if not items:
        raise ValueError(f"No NAIP data for {aoi.name} year {year}")

    dst_crs = CRS.from_epsg(aoi.epsg)
    t0 = time.perf_counter()

    mosaic = np.zeros((4, aoi.height, aoi.width), dtype=np.uint8)

    # Read items in parallel — each thread reads one COG
    def _read(item):
        return _read_naip_item(item, aoi.transform, dst_crs, aoi.height, aoi.width)

    with ThreadPoolExecutor(max_workers=min(4, len(items))) as pool:
        futures = {pool.submit(_read, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                result = future.result()
                # Composite: overwrite zeros with data
                has_data = result.sum(axis=0) > 0
                for b in range(4):
                    mosaic[b][has_data] = result[b][has_data]
            except Exception as e:
                logger.warning("Failed to read NAIP item %s: %s", item.item_id, e)

    elapsed = time.perf_counter() - t0
    size_mb = mosaic.nbytes / (1024 * 1024)
    logger.info(
        "NAIP mosaic %s/%s: %d items, %dx%d, %.1f MB in %.1fs",
        aoi.name,
        year,
        len(items),
        aoi.width,
        aoi.height,
        size_mb,
        elapsed,
    )

    _mosaic_cache[key] = mosaic
    return mosaic


def read_naip_tile(aoi: NaipAoi, year: str, chunk_id: str) -> np.ndarray:
    """Read a tile by slicing from the cached full mosaic.

    Returns:
        uint8 array of shape (4, tile_h, tile_w) in [Blue, Green, Red, NIR] order.
    """
    mosaic = _read_naip_mosaic(aoi, year)

    parts = chunk_id.split("_")
    row = int(parts[0][1:])
    col = int(parts[1][1:])

    row_off = row * CHUNK_SIZE
    col_off = col * CHUNK_SIZE
    tile_h = min(CHUNK_SIZE, aoi.height - row_off)
    tile_w = min(CHUNK_SIZE, aoi.width - col_off)

    if tile_h <= 0 or tile_w <= 0:
        raise ValueError(f"Chunk {chunk_id} out of bounds")

    return mosaic[:, row_off : row_off + tile_h, col_off : col_off + tile_w].copy()


def render_naip_true_color(bands: np.ndarray) -> np.ndarray:
    """Render NAIP true color. Bands are already uint8 RGB — just reorder.

    Args:
        bands: uint8 (4, H, W) in [Blue, Green, Red, NIR] order

    Returns:
        uint8 (H, W, 3) RGB
    """
    # Stack as R, G, B
    rgb = np.stack([bands[2], bands[1], bands[0]], axis=-1)

    # Mild contrast stretch for visual pop
    nodata = (bands[0] == 0) & (bands[1] == 0) & (bands[2] == 0)
    valid = rgb[~nodata]
    if len(valid) > 0:
        lo = float(np.percentile(valid, 1))
        hi = float(np.percentile(valid, 99))
        if hi > lo:
            rgb_f = (rgb.astype(np.float32) - lo) / (hi - lo)
            rgb = np.clip(rgb_f * 255, 0, 255).astype(np.uint8)

    rgb[nodata] = 0
    return rgb


def render_naip_false_color(bands: np.ndarray) -> np.ndarray:
    """Render NAIP false color (NIR, Red, Green).

    Args:
        bands: uint8 (4, H, W) in [Blue, Green, Red, NIR] order

    Returns:
        uint8 (H, W, 3) RGB
    """
    # NIR=3, Red=2, Green=1
    rgb = np.stack([bands[3], bands[2], bands[1]], axis=0).astype(np.float32)

    # Per-band percentile stretch
    out = np.zeros((bands.shape[1], bands.shape[2], 3), dtype=np.uint8)
    for i in range(3):
        band = rgb[i]
        valid = band[band > 0]
        if len(valid) == 0:
            continue
        lo = np.percentile(valid, 2)
        hi = np.percentile(valid, 98)
        if hi <= lo:
            hi = lo + 1
        stretched = np.clip((band - lo) / (hi - lo) * 255, 0, 255)
        out[:, :, i] = stretched.astype(np.uint8)
    return out


def render_naip_ndvi(bands: np.ndarray) -> np.ndarray:
    """Compute NDVI from NAIP bands.

    Args:
        bands: uint8 (4, H, W) in [Blue, Green, Red, NIR] order

    Returns:
        float32 (H, W), range [-1, 1]. NaN where both bands are 0.
    """
    nir = bands[3].astype(np.float32)
    red = bands[2].astype(np.float32)
    denom = nir + red
    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.where(denom > 0, (nir - red) / denom, np.nan)
    return result


def render_naip_ndwi(bands: np.ndarray) -> np.ndarray:
    """Compute NDWI from NAIP bands (Green - NIR) / (Green + NIR).

    Args:
        bands: uint8 (4, H, W) in [Blue, Green, Red, NIR] order

    Returns:
        float32 (H, W), range [-1, 1].
    """
    green = bands[1].astype(np.float32)
    nir = bands[3].astype(np.float32)
    denom = green + nir
    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.where(denom > 0, (green - nir) / denom, np.nan)
    return result


def render_naip_product(bands: np.ndarray, product: str) -> np.ndarray:
    """Render a NAIP product.

    Args:
        bands: uint8 (4, H, W) in [Blue, Green, Red, NIR]
        product: One of true_color, false_color, ndvi, ndwi

    Returns:
        uint8 (H, W, 3) for visual products, or float32 (H, W) for indices.
    """
    from spacetime.render import ndvi_colormap, ndwi_colormap

    dispatch = {
        "true_color": render_naip_true_color,
        "false_color": render_naip_false_color,
        "ndvi": render_naip_ndvi,
        "ndwi": render_naip_ndwi,
    }
    if product not in dispatch:
        raise ValueError(f"Unknown NAIP product '{product}'. Available: {list(dispatch.keys())}")

    rendered = dispatch[product](bands)

    # Apply colormaps for index products
    if rendered.dtype == np.float32:
        cmap = ndwi_colormap if product == "ndwi" else ndvi_colormap
        rendered = cmap(rendered)

    return rendered
