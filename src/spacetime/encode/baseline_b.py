"""Baseline B: multiband monthly chunks stored as Zarr.

Products are derived at render time from the stored bands, not pre-rendered.
This already eliminates per-product storage duplication.

Two sub-variants:
    B1 — independent: each month is a separate Zarr array per chunk
    B2 — stacked: all months for a chunk in one time-stacked Zarr array

Storage structure:
    B1: store_dir/b1/{chunk_id}/{month}.zarr
    B2: store_dir/b2/{chunk_id}/stack.zarr  (time × bands × H × W)
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import zarr
from numcodecs import Blosc

from spacetime.chunk import ChunkGrid, extract_chunk

logger = logging.getLogger(__name__)

COMPRESSOR = Blosc(cname="zstd", clevel=5, shuffle=Blosc.BITSHUFFLE)


def encode_b1(
    monthly_mosaics: dict[str, np.ndarray],
    grid: ChunkGrid,
    store_dir: Path,
) -> dict[str, int]:
    """Encode as independent multiband Zarr per chunk per month.

    Each file: (n_bands, chunk_h, chunk_w) uint16, blosc/zstd compressed.
    """
    root = store_dir / "b1"
    root.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    n_files = 0

    for month_key in sorted(monthly_mosaics.keys()):
        bands = monthly_mosaics[month_key]
        for row, col in grid.chunk_ids:
            chunk = extract_chunk(bands, grid, row, col)
            chunk_id = grid.chunk_id_str(row, col)

            chunk_dir = root / chunk_id
            chunk_dir.mkdir(parents=True, exist_ok=True)
            arr_path = chunk_dir / f"{month_key}.zarr"

            z = zarr.open(
                str(arr_path),
                mode="w",
                shape=chunk.shape,
                dtype=chunk.dtype,
                compressor=COMPRESSOR,
                chunks=chunk.shape,  # single chunk = entire array
            )
            z[:] = chunk

            fsize = _dir_size(arr_path)
            total_bytes += fsize
            n_files += 1

    logger.info("Baseline B1: %d arrays, %.2f MB total", n_files, total_bytes / 1e6)
    return {
        "total_bytes": total_bytes,
        "n_files": n_files,
        "n_months": len(monthly_mosaics),
        "n_chunks": grid.n_chunks,
        "variant": "b1",
    }


def encode_b2(
    monthly_mosaics: dict[str, np.ndarray],
    grid: ChunkGrid,
    store_dir: Path,
) -> dict[str, int]:
    """Encode as time-stacked multiband Zarr per chunk.

    Each file: (n_months, n_bands, chunk_h, chunk_w) uint16, blosc/zstd compressed.
    Time is the outermost dimension so zstd sees temporal correlation.
    """
    root = store_dir / "b2"
    root.mkdir(parents=True, exist_ok=True)

    months = sorted(monthly_mosaics.keys())
    n_months = len(months)
    n_bands = next(iter(monthly_mosaics.values())).shape[0]
    total_bytes = 0

    for row, col in grid.chunk_ids:
        chunk_id = grid.chunk_id_str(row, col)
        ch, cw = grid.chunk_shape(row, col)

        arr_path = root / chunk_id / "stack.zarr"
        arr_path.parent.mkdir(parents=True, exist_ok=True)

        z = zarr.open(
            str(arr_path),
            mode="w",
            shape=(n_months, n_bands, ch, cw),
            dtype=np.uint16,
            compressor=COMPRESSOR,
            # Chunk along time: each time-step is one chunk for independent access
            # But the entire time stack is one blob for bulk compression
            chunks=(n_months, n_bands, ch, cw),
        )

        for t, month_key in enumerate(months):
            chunk = extract_chunk(monthly_mosaics[month_key], grid, row, col)
            z[t] = chunk

        fsize = _dir_size(arr_path)
        total_bytes += fsize

    logger.info("Baseline B2: %d chunks, %.2f MB total", grid.n_chunks, total_bytes / 1e6)
    return {
        "total_bytes": total_bytes,
        "n_chunks": grid.n_chunks,
        "n_months": n_months,
        "variant": "b2",
        "months": months,
    }


def encode_b2_chunked_time(
    monthly_mosaics: dict[str, np.ndarray],
    grid: ChunkGrid,
    store_dir: Path,
) -> dict[str, int]:
    """B2 variant with per-month Zarr chunks (allows partial time access).

    Same data as B2, but chunked as (1, n_bands, ch, cw) so each month
    can be read independently without decompressing the full stack.
    """
    root = store_dir / "b2_chunked"
    root.mkdir(parents=True, exist_ok=True)

    months = sorted(monthly_mosaics.keys())
    n_months = len(months)
    n_bands = next(iter(monthly_mosaics.values())).shape[0]
    total_bytes = 0

    for row, col in grid.chunk_ids:
        chunk_id = grid.chunk_id_str(row, col)
        ch, cw = grid.chunk_shape(row, col)

        arr_path = root / chunk_id / "stack.zarr"
        arr_path.parent.mkdir(parents=True, exist_ok=True)

        z = zarr.open(
            str(arr_path),
            mode="w",
            shape=(n_months, n_bands, ch, cw),
            dtype=np.uint16,
            compressor=COMPRESSOR,
            chunks=(1, n_bands, ch, cw),  # per-month chunks
        )

        for t, month_key in enumerate(months):
            chunk = extract_chunk(monthly_mosaics[month_key], grid, row, col)
            z[t] = chunk

        fsize = _dir_size(arr_path)
        total_bytes += fsize

    logger.info(
        "Baseline B2-chunked: %d chunks, %.2f MB total", grid.n_chunks, total_bytes / 1e6
    )
    return {
        "total_bytes": total_bytes,
        "n_chunks": grid.n_chunks,
        "n_months": n_months,
        "variant": "b2_chunked",
        "months": months,
    }


def decode_b1(store_dir: Path, chunk_id: str, month: str) -> np.ndarray:
    """Read a single chunk × month from B1 store.

    Returns:
        uint16 array of shape (n_bands, H, W)
    """
    arr_path = store_dir / "b1" / chunk_id / f"{month}.zarr"
    z = zarr.open(str(arr_path), mode="r")
    return np.array(z)


def decode_b2(store_dir: Path, chunk_id: str, month_index: int) -> np.ndarray:
    """Read a single time-step from B2 store.

    Note: this reads the entire time-stacked blob from disk (since it's
    one Zarr chunk), then returns only the requested month.
    This models the worst-case access pattern for time-stacked storage.

    Returns:
        uint16 array of shape (n_bands, H, W)
    """
    arr_path = store_dir / "b2" / chunk_id / "stack.zarr"
    z = zarr.open(str(arr_path), mode="r")
    return np.array(z[month_index])


def decode_b2_chunked(store_dir: Path, chunk_id: str, month_index: int) -> np.ndarray:
    """Read a single time-step from B2-chunked store.

    Only decompresses the single month requested.

    Returns:
        uint16 array of shape (n_bands, H, W)
    """
    arr_path = store_dir / "b2_chunked" / chunk_id / "stack.zarr"
    z = zarr.open(str(arr_path), mode="r")
    return np.array(z[month_index])


def bytes_for_b1_tile(store_dir: Path, chunk_id: str, month: str) -> int:
    """Bytes needed to fetch one B1 tile."""
    arr_path = store_dir / "b1" / chunk_id / f"{month}.zarr"
    return _dir_size(arr_path)


def bytes_for_b2_tile(store_dir: Path, chunk_id: str) -> int:
    """Bytes needed to fetch one B2 tile (entire time stack)."""
    arr_path = store_dir / "b2" / chunk_id / "stack.zarr"
    return _dir_size(arr_path)


def bytes_for_b2_chunked_tile(store_dir: Path, chunk_id: str, month_index: int) -> int:
    """Bytes needed to fetch one month from B2-chunked store.

    Approximation: total store size / n_months (Zarr chunks are roughly equal).
    For accurate measurement, we'd inspect the actual chunk file sizes.
    """
    arr_path = store_dir / "b2_chunked" / chunk_id / "stack.zarr"
    z = zarr.open(str(arr_path), mode="r")
    n_months = z.shape[0]
    return _dir_size(arr_path) // n_months


def _dir_size(path: Path) -> int:
    """Total bytes in a directory tree."""
    if path.is_file():
        return path.stat().st_size
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
