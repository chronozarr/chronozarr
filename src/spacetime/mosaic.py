"""Monthly median composite mosaics from Sentinel-2 L2A scenes.

Reads COGs from Planetary Computer via HTTPS, applies SCL cloud masking,
and produces monthly median composites in native UTM CRS.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.warp import Resampling, reproject

from spacetime.catalog import REQUIRED_BANDS, SceneRef

logger = logging.getLogger(__name__)

# SCL values to KEEP (everything else is masked)
# 4=vegetation, 5=bare_soil, 6=water, 7=unclassified (low prob cloud)
# 11=snow/ice (keep for Iowa winter)
SCL_VALID = {4, 5, 6, 7, 11}

# GDAL environment for efficient COG reads over HTTPS
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "5000000",
}


def compute_target_grid(
    bbox_wgs84: tuple[float, float, float, float],
    target_epsg: int,
    resolution: float = 10.0,
) -> tuple[rasterio.transform.Affine, int, int]:
    """Compute a pixel-aligned target grid for the AOI.

    Args:
        bbox_wgs84: (lon_min, lat_min, lon_max, lat_max)
        target_epsg: EPSG code for target CRS (UTM)
        resolution: Pixel size in meters

    Returns:
        (transform, height, width) for the target grid
    """
    from rasterio.crs import CRS
    from rasterio.warp import transform_bounds

    src_crs = CRS.from_epsg(4326)
    dst_crs = CRS.from_epsg(target_epsg)

    # Transform bbox to target CRS
    left, bottom, right, top = transform_bounds(src_crs, dst_crs, *bbox_wgs84)

    # Snap to resolution grid
    left = np.floor(left / resolution) * resolution
    bottom = np.floor(bottom / resolution) * resolution
    right = np.ceil(right / resolution) * resolution
    top = np.ceil(top / resolution) * resolution

    width = int((right - left) / resolution)
    height = int((top - bottom) / resolution)
    transform = from_bounds(left, bottom, right, top, width, height)

    logger.info(
        "Target grid: %d x %d pixels @ %.0fm, EPSG:%d", width, height, resolution, target_epsg
    )
    return transform, height, width


def read_band_window(
    href: str,
    dst_transform: rasterio.transform.Affine,
    dst_crs: rasterio.crs.CRS,
    dst_height: int,
    dst_width: int,
    resampling: Resampling = Resampling.bilinear,
) -> np.ndarray:
    """Read a single band COG and reproject/window into the target grid.

    Returns:
        2D uint16 array of shape (dst_height, dst_width). Nodata = 0.
    """
    dst = np.zeros((dst_height, dst_width), dtype=np.uint16)

    with rasterio.Env(**GDAL_ENV), rasterio.open(href) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=resampling,
        )
    return dst


def read_scl_mask(
    scl_href: str,
    dst_transform: rasterio.transform.Affine,
    dst_crs: rasterio.crs.CRS,
    dst_height: int,
    dst_width: int,
) -> np.ndarray:
    """Read SCL band and produce a boolean valid-pixel mask at target resolution.

    SCL is 20m; reprojected to 10m via nearest neighbor.

    Returns:
        2D bool array. True = valid pixel, False = masked.
    """
    scl = np.zeros((dst_height, dst_width), dtype=np.uint8)

    with rasterio.Env(**GDAL_ENV), rasterio.open(scl_href) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=scl,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.nearest,
        )

    valid = np.isin(scl, list(SCL_VALID))
    return valid


def load_scene(
    scene: SceneRef,
    dst_transform: rasterio.transform.Affine,
    dst_crs: rasterio.crs.CRS,
    dst_height: int,
    dst_width: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Load all bands + SCL mask for one scene, reprojected to target grid.

    Returns:
        bands: uint16 array of shape (n_bands, height, width)
        valid: bool array of shape (height, width)
    """
    bands = np.zeros((len(REQUIRED_BANDS), dst_height, dst_width), dtype=np.uint16)
    for i, band_name in enumerate(REQUIRED_BANDS):
        href = scene.asset_hrefs[band_name]
        bands[i] = read_band_window(href, dst_transform, dst_crs, dst_height, dst_width)

    valid = read_scl_mask(scene.scl_href, dst_transform, dst_crs, dst_height, dst_width)

    # Also mask nodata (band value 0)
    band_valid = np.all(bands > 0, axis=0)
    valid = valid & band_valid

    logger.info(
        "Loaded %s: %.1f%% valid pixels",
        scene.item_id[:40],
        100.0 * valid.mean(),
    )
    return bands, valid


def monthly_composite(
    scenes: list[SceneRef],
    dst_transform: rasterio.transform.Affine,
    dst_crs: rasterio.crs.CRS,
    dst_height: int,
    dst_width: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute median composite for a set of scenes (typically one month).

    Args:
        scenes: List of SceneRef for this month
        dst_transform: Target affine transform
        dst_crs: Target CRS
        dst_height: Target height in pixels
        dst_width: Target width in pixels

    Returns:
        composite: uint16 array of shape (n_bands, height, width)
        coverage: float32 array of shape (height, width), fraction of valid scenes per pixel
    """
    n_bands = len(REQUIRED_BANDS)
    n_scenes = len(scenes)

    if n_scenes == 0:
        return (
            np.zeros((n_bands, dst_height, dst_width), dtype=np.uint16),
            np.zeros((dst_height, dst_width), dtype=np.float32),
        )

    # Stack all scenes: (n_scenes, n_bands, height, width) as float32 for masked median
    stack = np.full((n_scenes, n_bands, dst_height, dst_width), np.nan, dtype=np.float32)

    # Load scenes in parallel (network-bound, benefits from concurrency)
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _load(idx_scene):
        idx, scene = idx_scene
        return idx, load_scene(scene, dst_transform, dst_crs, dst_height, dst_width)

    max_workers = min(4, n_scenes)
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_load, (i, s)): i for i, s in enumerate(scenes)}
        for future in as_completed(futures):
            i, (bands, valid) = future.result()
            mask_3d = np.broadcast_to(valid[np.newaxis, :, :], bands.shape)
            scene_float = bands.astype(np.float32)
            scene_float[~mask_3d] = np.nan
            stack[i] = scene_float

    # Median ignoring NaN
    with np.errstate(all="ignore"):
        composite_f = np.nanmedian(stack, axis=0)

    # Coverage: fraction of valid scenes per pixel
    valid_count = np.sum(~np.isnan(stack[:, 0, :, :]), axis=0)
    coverage = valid_count.astype(np.float32) / n_scenes

    # Convert back to uint16, NaN → 0
    composite = np.nan_to_num(composite_f, nan=0.0).astype(np.uint16)

    logger.info(
        "Monthly composite: %d scenes, mean coverage %.1f%%",
        n_scenes,
        100.0 * coverage.mean(),
    )
    return composite, coverage


def build_monthly_mosaics(
    scenes_by_month: dict[str, list[SceneRef]],
    bbox_wgs84: tuple[float, float, float, float],
    target_epsg: int,
    output_dir: Path,
    carry_forward: bool = True,
) -> dict[str, Path]:
    """Build monthly composites and save as numpy files.

    Args:
        scenes_by_month: Dict mapping "YYYY-MM" to scene lists
        bbox_wgs84: AOI bounding box in WGS84
        target_epsg: Target EPSG for output grid
        output_dir: Directory to write monthly .npz files
        carry_forward: If True, fill gaps with previous month's data

    Returns:
        Dict mapping "YYYY-MM" to output file path.

    Each .npz file contains:
        - bands: uint16 (n_bands, height, width)
        - coverage: float32 (height, width)
        - transform: 6 affine coefficients
        - epsg: int
        - band_names: string array
    """
    from rasterio.crs import CRS

    output_dir.mkdir(parents=True, exist_ok=True)
    dst_crs = CRS.from_epsg(target_epsg)
    dst_transform, dst_height, dst_width = compute_target_grid(bbox_wgs84, target_epsg)

    months = sorted(scenes_by_month.keys())
    outputs: dict[str, Path] = {}
    prev_composite: np.ndarray | None = None

    for month_key in months:
        scenes = scenes_by_month[month_key]
        logger.info("=== Processing %s (%d scenes) ===", month_key, len(scenes))

        composite, coverage = monthly_composite(
            scenes, dst_transform, dst_crs, dst_height, dst_width
        )

        # Carry-forward: fill zero pixels from previous month
        if carry_forward and prev_composite is not None:
            gap_mask = np.all(composite == 0, axis=0)
            if gap_mask.any():
                n_filled = gap_mask.sum()
                logger.info("Carry-forward: filling %d pixels from previous month", n_filled)
                composite[:, gap_mask] = prev_composite[:, gap_mask]

        prev_composite = composite.copy()

        # Save
        out_path = output_dir / f"{month_key}.npz"
        np.savez_compressed(
            out_path,
            bands=composite,
            coverage=coverage,
            transform=np.array(list(dst_transform)[:6]),
            epsg=np.array(target_epsg),
            band_names=np.array(list(REQUIRED_BANDS)),
        )
        file_size_mb = out_path.stat().st_size / (1024 * 1024)
        logger.info("Saved %s (%.2f MB)", out_path, file_size_mb)
        outputs[month_key] = out_path

    return outputs


def load_mosaic(path: Path) -> dict:
    """Load a saved monthly mosaic .npz file.

    Returns:
        Dict with keys: bands, coverage, transform, epsg, band_names
    """
    data = np.load(path, allow_pickle=False)
    from rasterio.transform import Affine

    coeffs = data["transform"]
    transform = Affine(*coeffs)
    return {
        "bands": data["bands"],
        "coverage": data["coverage"],
        "transform": transform,
        "epsg": int(data["epsg"]),
        "band_names": list(data["band_names"]),
    }
