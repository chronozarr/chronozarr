"""Multiscale Zarr pyramid construction for chunked mosaics."""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import zarr
from numcodecs import Blosc
from rasterio.transform import Affine

from spacetime.chunk import ChunkGrid, extract_chunk, make_chunk_grid

logger = logging.getLogger(__name__)

COMPRESSOR = Blosc(cname="zstd", clevel=5, shuffle=Blosc.BITSHUFFLE)


def downsample_2x(mosaic: np.ndarray) -> np.ndarray:
    """Downsample a multiband mosaic by 2x using integer area averaging."""
    n_bands, height, width = mosaic.shape
    pad_h = height % 2
    pad_w = width % 2

    if pad_h or pad_w:
        mosaic = np.pad(mosaic, ((0, 0), (0, pad_h), (0, pad_w)), mode="edge")

    padded = mosaic.astype(np.uint32, copy=False)
    out_height = padded.shape[1] // 2
    out_width = padded.shape[2] // 2
    reshaped = padded.reshape(n_bands, out_height, 2, out_width, 2)
    summed = reshaped.sum(axis=(2, 4), dtype=np.uint32)
    return ((summed + 2) // 4).astype(np.uint16)


def compute_pyramid_levels(
    mosaic_height: int,
    mosaic_width: int,
    chunk_size: int = 512,
) -> int:
    """Return the number of additional levels needed to fit within one chunk."""
    levels = 0
    height = mosaic_height
    width = mosaic_width

    while height > chunk_size or width > chunk_size:
        height = (height + 1) // 2
        width = (width + 1) // 2
        levels += 1

    return levels


def scale_transform(transform: Affine, factor: int) -> Affine:
    """Scale an affine transform while preserving the top-left origin."""
    return Affine(
        transform.a * factor,
        transform.b,
        transform.c,
        transform.d,
        transform.e * factor,
        transform.f,
    )


def write_zarr_level(
    monthly_mosaics: dict[str, np.ndarray],
    grid: ChunkGrid,
    root_dir: Path,
) -> int:
    """Write a flat-layout Zarr level under ``root_dir/{chunk_id}/stack.zarr``."""
    root_dir.mkdir(parents=True, exist_ok=True)

    months = sorted(monthly_mosaics)
    if not months:
        return 0

    n_months = len(months)
    n_bands = monthly_mosaics[months[0]].shape[0]
    total_bytes = 0

    for row, col in grid.chunk_ids:
        chunk_id = grid.chunk_id_str(row, col)
        chunk_h, chunk_w = grid.chunk_shape(row, col)
        zarr_path = root_dir / chunk_id / "stack.zarr"
        zarr_path.parent.mkdir(parents=True, exist_ok=True)

        z = zarr.open(
            str(zarr_path),
            mode="w",
            shape=(n_months, n_bands, chunk_h, chunk_w),
            dtype=np.uint16,
            compressor=COMPRESSOR,
            chunks=(1, n_bands, chunk_h, chunk_w),
        )

        for month_index, month_key in enumerate(months):
            z[month_index] = extract_chunk(monthly_mosaics[month_key], grid, row, col)

        total_bytes += _dir_size(zarr_path)

    logger.info(
        "Wrote level under %s: %d chunks, %.2f MB",
        root_dir,
        grid.n_chunks,
        total_bytes / 1e6,
    )
    return total_bytes


def build_pyramid(
    monthly_mosaics: dict[str, np.ndarray],
    grid: ChunkGrid,
    store_dir: Path,
    chunk_size: int = 512,
) -> list[dict]:
    """Build downsampled pyramid levels and return per-level metadata."""
    n_levels = compute_pyramid_levels(grid.mosaic_height, grid.mosaic_width, chunk_size)
    if n_levels == 0:
        return []

    base_resolution_m = float(grid.transform.a)
    metadata: list[dict] = []
    level_input = monthly_mosaics

    for level in range(1, n_levels + 1):
        level_mosaics = {
            month_key: downsample_2x(mosaic) for month_key, mosaic in level_input.items()
        }
        sample = next(iter(level_mosaics.values()))
        level_height = int(sample.shape[1])
        level_width = int(sample.shape[2])
        level_transform = scale_transform(grid.transform, 2**level)
        level_grid = make_chunk_grid(
            level_height,
            level_width,
            level_transform,
            grid.epsg,
            chunk_size,
        )
        level_root = store_dir / "pyramid" / str(level)
        bytes_written = write_zarr_level(level_mosaics, level_grid, level_root)

        metadata.append(
            {
                "level": level,
                "mosaic_height": level_height,
                "mosaic_width": level_width,
                "n_rows": level_grid.n_rows,
                "n_cols": level_grid.n_cols,
                "resolution_m": base_resolution_m * (2**level),
                "transform": list(level_transform)[:6],
            }
        )
        logger.info(
            "Built pyramid level %d: %dx%d, %d chunks, %.2f MB",
            level,
            level_height,
            level_width,
            level_grid.n_chunks,
            bytes_written / 1e6,
        )
        level_input = level_mosaics

    return metadata


def _dir_size(path: Path) -> int:
    """Return total bytes for a file or directory tree."""
    if path.is_file():
        return path.stat().st_size
    return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
