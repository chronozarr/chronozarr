"""V1 encoder: star-delta temporal encoding with multiscale pyramid.

Key features:
- Zstd compression (no Blosc wrapper)
- 7 LOD levels (powers of 2 downsample from native 10m)
- Star-delta temporal encoding: anchors stored as uint16, deltas as int16
- Each non-anchor month references the NEAREST anchor (not previous month)
- Volatility score per cell at LOD 0

Storage structure:
    store_dir/
        manifest.json
        lod/
            0/chunks/r000_c000/stack.zarr
            0/chunks/r000_c001/stack.zarr
            ...
            6/chunks/r000_c000/stack.zarr
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np
import zarr
from numcodecs import Zstd
from rasterio.transform import Affine

from spacetime.chunk import ChunkGrid, extract_chunk, make_chunk_grid

logger = logging.getLogger(__name__)

COMPRESSOR = Zstd(level=5)
REFLECTANCE_SCALE = 10000  # Sentinel-2 reflectance scaling factor


def compute_anchor_schedule(
    n_months: int, anchor_interval: int
) -> tuple[list[int], dict[int, int]]:
    """Return (anchor_indices, delta_reference_map).

    delta_reference_map: {month_index: nearest_anchor_index}

    Each non-anchor month references the NEAREST anchor (not the previous month).
    This means decoding any month requires exactly 1 anchor + 1 delta.

    Example: anchor_interval=6, n_months=12
        - Anchors: 0, 6
        - Month 1 → delta vs anchor 0
        - Month 4 → delta vs anchor 6 (closer to 6 than to 0)
        - Month 5 → delta vs anchor 6
    """
    # Compute anchor indices
    anchor_indices = list(range(0, n_months, anchor_interval))

    # Compute delta reference map (nearest anchor for each month)
    delta_reference: dict[int, int] = {}
    for month_idx in range(n_months):
        if month_idx in anchor_indices:
            continue  # Anchors don't have delta references

        # Find nearest anchor
        nearest_anchor = anchor_indices[0]
        min_distance = abs(month_idx - nearest_anchor)

        for anchor_idx in anchor_indices[1:]:
            distance = abs(month_idx - anchor_idx)
            if distance < min_distance:
                min_distance = distance
                nearest_anchor = anchor_idx

        delta_reference[month_idx] = nearest_anchor

    return anchor_indices, delta_reference


def _downsample_block_average(mosaic: np.ndarray, factor: int, nodata: int = 0) -> np.ndarray:
    """Downsample by integer factor using block averaging.

    Handles nodata (value 0) by excluding it from the mean.
    If a block is entirely zero, output zero.

    Args:
        mosaic: Array of shape (n_bands, H, W)
        factor: Integer downsampling factor
        nodata: Value to treat as nodata (default 0)

    Returns:
        Downsampled array of shape (n_bands, H//factor, W//factor)
    """
    n_bands, height, width = mosaic.shape

    # Pad to multiple of factor
    pad_h = (factor - height % factor) % factor
    pad_w = (factor - width % factor) % factor

    if pad_h or pad_w:
        mosaic = np.pad(mosaic, ((0, 0), (0, pad_h), (0, pad_w)), mode="edge")

    padded_h, padded_w = mosaic.shape[1], mosaic.shape[2]
    out_h = padded_h // factor
    out_w = padded_w // factor

    # Reshape for block averaging: (n_bands, out_h, factor, out_w, factor)
    reshaped = mosaic.reshape(n_bands, out_h, factor, out_w, factor)

    # Compute mean excluding nodata values
    # Create mask for valid (non-nodata) pixels
    mask = (reshaped != nodata).astype(np.uint32)
    values = reshaped.astype(np.uint32) * mask

    # Sum values and counts per block
    sum_values = values.sum(axis=(2, 4), dtype=np.uint32)
    sum_counts = mask.sum(axis=(2, 4), dtype=np.uint32)

    # Avoid division by zero - where count is 0, result is 0 (nodata)
    with np.errstate(divide="ignore", invalid="ignore"):
        result = np.where(sum_counts > 0, sum_values // sum_counts, 0)

    return result.astype(np.uint16)


def _scale_transform(transform: Affine, factor: int) -> Affine:
    """Scale an affine transform while preserving the top-left origin."""
    return Affine(
        transform.a * factor,
        transform.b,
        transform.c,
        transform.d,
        transform.e * factor,
        transform.f,
    )


def _compute_volatility(
    monthly_chunks: list[np.ndarray], anchor_indices: list[int], delta_reference: dict[int, int]
) -> float:
    """Compute volatility score for a cell at LOD 0.

    Volatility = mean absolute delta (across all delta months and bands),
    divided by REFLECTANCE_SCALE, clamped to [0, 1].

    Args:
        monthly_chunks: List of (n_bands, H, W) arrays for each month
        anchor_indices: List of anchor month indices
        delta_reference: Map of delta month index -> anchor index

    Returns:
        Volatility score in [0, 1]
    """
    total_abs_delta = 0.0
    n_delta_pixels = 0

    for month_idx, chunk in enumerate(monthly_chunks):
        if month_idx in anchor_indices:
            continue  # Skip anchors

        anchor_idx = delta_reference[month_idx]
        anchor = monthly_chunks[anchor_idx]

        # Compute delta as int32 to avoid overflow
        delta = chunk.astype(np.int32) - anchor.astype(np.int32)
        abs_delta = np.abs(delta)

        total_abs_delta += abs_delta.sum()
        n_delta_pixels += abs_delta.size

    if n_delta_pixels == 0:
        return 0.0

    mean_abs_delta = total_abs_delta / n_delta_pixels
    volatility = mean_abs_delta / REFLECTANCE_SCALE
    return float(np.clip(volatility, 0.0, 1.0))


def _write_zarr_stack(
    monthly_chunks: list[np.ndarray],
    anchor_indices: list[int],
    delta_reference: dict[int, int],
    zarr_path: Path,
    chunk_h: int,
    chunk_w: int,
) -> int:
    """Write a time-stacked Zarr array with star-delta encoding.

    Args:
        monthly_chunks: List of (n_bands, H, W) uint16 arrays
        anchor_indices: List of anchor month indices
        delta_reference: Map of delta month index -> anchor index
        zarr_path: Output path for the Zarr array
        chunk_h: Chunk height
        chunk_w: Chunk width

    Returns:
        Bytes written
    """
    n_months = len(monthly_chunks)
    n_bands = monthly_chunks[0].shape[0]

    # Create Zarr array: (n_months, n_bands, H, W), chunks (1, n_bands, H, W)
    z = zarr.open(
        str(zarr_path),
        mode="w",
        shape=(n_months, n_bands, chunk_h, chunk_w),
        dtype=np.uint16,
        compressor=COMPRESSOR,
        chunks=(1, n_bands, chunk_h, chunk_w),
    )

    # Write anchor months as uint16
    for anchor_idx in anchor_indices:
        z[anchor_idx] = monthly_chunks[anchor_idx]

    # Write delta months as int16 (stored as uint16 bytes)
    for month_idx in range(n_months):
        if month_idx in anchor_indices:
            continue

        anchor_idx = delta_reference[month_idx]
        anchor = monthly_chunks[anchor_idx].astype(np.int32)
        current = monthly_chunks[month_idx].astype(np.int32)

        # Compute delta and clip to int16 range
        delta = current - anchor
        delta_clipped = delta.clip(-32768, 32767).astype(np.int16)

        # Store int16 bytes reinterpreted as uint16
        delta_as_uint16 = delta_clipped.view(np.uint16)
        z[month_idx] = delta_as_uint16

    # Return bytes written
    if zarr_path.is_file():
        return zarr_path.stat().st_size
    return sum(f.stat().st_size for f in zarr_path.rglob("*") if f.is_file())


def encode_v1(
    monthly_mosaics: dict[str, np.ndarray],
    grid: ChunkGrid,
    store_dir: Path,
    anchor_interval: int = 6,
    n_lods: int = 7,
    lod_factor: int = 2,
) -> dict[str, Any]:
    """Encode monthly mosaics with star-delta temporal encoding and multiscale pyramid.

    Args:
        monthly_mosaics: "YYYY-MM" → (n_bands, H, W) uint16
        grid: Native-resolution chunk grid
        store_dir: Output directory
        anchor_interval: Months between anchor frames
        n_lods: Number of LOD levels (including LOD 0)
        lod_factor: Downsampling factor per level

    Returns:
        Dict with: total_bytes, bytes_per_lod, n_anchors, n_deltas, avg_volatility, months
    """
    store_dir.mkdir(parents=True, exist_ok=True)
    months = sorted(monthly_mosaics.keys())
    n_months = len(months)

    # Compute anchor schedule
    anchor_indices, delta_reference = compute_anchor_schedule(n_months, anchor_interval)
    n_anchors = len(anchor_indices)
    n_deltas = n_months - n_anchors

    # Prepare LOD 0 data
    lod0_mosaics = monthly_mosaics
    lod0_grid = grid

    bytes_per_lod: list[int] = []
    lod_metadata: list[dict] = []
    cell_volatility: dict[str, float] = {}

    # Process each LOD level
    for lod in range(n_lods):
        factor = lod_factor**lod
        resolution_m = abs(float(grid.transform.a)) * factor

        # Downsample mosaics for this LOD (LOD 0 uses original)
        if lod == 0:
            level_mosaics = lod0_mosaics
            level_grid = lod0_grid
        else:
            level_mosaics = {
                month: _downsample_block_average(mosaic, lod_factor)
                for month, mosaic in level_mosaics.items()
            }
            sample = next(iter(level_mosaics.values()))
            level_height, level_width = sample.shape[1], sample.shape[2]
            level_transform = _scale_transform(grid.transform, factor)
            level_grid = make_chunk_grid(
                level_height, level_width, level_transform, grid.epsg, grid.chunk_size
            )

        # Create LOD directory
        lod_dir = store_dir / "lod" / str(lod) / "chunks"
        lod_dir.mkdir(parents=True, exist_ok=True)

        level_bytes = 0

        # Process each chunk
        for row, col in level_grid.chunk_ids:
            chunk_id = level_grid.chunk_id_str(row, col)
            chunk_h, chunk_w = level_grid.chunk_shape(row, col)

            # Extract chunks for all months
            monthly_chunks = [
                extract_chunk(level_mosaics[month], level_grid, row, col) for month in months
            ]

            # Compute volatility at LOD 0 only
            if lod == 0:
                volatility = _compute_volatility(monthly_chunks, anchor_indices, delta_reference)
                cell_volatility[chunk_id] = volatility

            # Write Zarr stack
            zarr_path = lod_dir / chunk_id / "stack.zarr"
            zarr_path.parent.mkdir(parents=True, exist_ok=True)

            chunk_bytes = _write_zarr_stack(
                monthly_chunks,
                anchor_indices,
                delta_reference,
                zarr_path,
                chunk_h,
                chunk_w,
            )
            level_bytes += chunk_bytes

        bytes_per_lod.append(level_bytes)
        lod_metadata.append(
            {
                "level": lod,
                "resolution_m": resolution_m,
                "grid_rows": level_grid.n_rows,
                "grid_cols": level_grid.n_cols,
                "chunk_size": level_grid.chunk_size,
            }
        )

        logger.info(
            "LOD %d: %d chunks, %.2f MB",
            lod,
            level_grid.n_chunks,
            level_bytes / 1e6,
        )

    # Compute average volatility
    avg_volatility = (
        sum(cell_volatility.values()) / len(cell_volatility) if cell_volatility else 0.0
    )

    # Build manifest
    manifest = {
        "version": "1.0.0",
        "epsg": grid.epsg,
        "transform": list(grid.transform)[:6],
        "mosaic_height": grid.mosaic_height,
        "mosaic_width": grid.mosaic_width,
        "bands": ["B02", "B03", "B04", "B08"],  # Standard Sentinel-2 bands
        "dtype": "uint16",
        "nodata": 0,
        "months": months,
        "compressor": "zstd",
        "compressor_level": 5,
        "lod_levels": n_lods,
        "lod_factor": lod_factor,
        "temporal": {
            "encoding": "star-delta",
            "anchor_interval": anchor_interval,
            "anchor_indices": anchor_indices,
            "delta_reference": {str(k): v for k, v in delta_reference.items()},
        },
        "lods": lod_metadata,
        "cells": {chunk_id: {"volatility": vol} for chunk_id, vol in cell_volatility.items()},
    }

    with open(store_dir / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    total_bytes = sum(bytes_per_lod)

    logger.info(
        "V1 encode complete: %.2f MB total, %d anchors, %d deltas, avg volatility %.3f",
        total_bytes / 1e6,
        n_anchors,
        n_deltas,
        avg_volatility,
    )

    return {
        "total_bytes": total_bytes,
        "bytes_per_lod": bytes_per_lod,
        "n_anchors": n_anchors,
        "n_deltas": n_deltas,
        "avg_volatility": avg_volatility,
        "months": months,
    }


def decode_v1(store_dir: Path, lod: int, chunk_id: str, month_index: int) -> np.ndarray:
    """Decode a single chunk at a given LOD and month.

    Reads manifest to determine if anchor or delta. If delta, loads anchor and adds delta.

    Args:
        store_dir: Root of the v1 store
        lod: LOD level (0-6)
        chunk_id: Chunk identifier (e.g., "r000_c000")
        month_index: Month index (0-based)

    Returns:
        uint16 array of shape (n_bands, H, W)
    """
    # Load manifest
    with open(store_dir / "manifest.json") as f:
        manifest = json.load(f)

    temporal = manifest["temporal"]
    anchor_indices = temporal["anchor_indices"]
    delta_reference = {int(k): v for k, v in temporal["delta_reference"].items()}

    # Open Zarr array
    zarr_path = store_dir / "lod" / str(lod) / "chunks" / chunk_id / "stack.zarr"
    z = zarr.open(str(zarr_path), mode="r")

    # Check if this is an anchor month
    if month_index in anchor_indices:
        # Direct read for anchors
        return np.array(z[month_index])

    # Delta month: load anchor and delta, reconstruct
    anchor_idx = delta_reference.get(month_index)
    if anchor_idx is None:
        raise ValueError(f"Month {month_index} not found in manifest")

    # Load anchor (uint16)
    anchor = z[anchor_idx].astype(np.int32)

    # Load delta (stored as uint16 but is int16 bytes)
    delta_as_uint16 = z[month_index]
    delta = delta_as_uint16.view(np.int16).astype(np.int32)

    # Reconstruct: anchor + delta
    result = anchor + delta
    result = result.clip(0, 65535).astype(np.uint16)

    return result
