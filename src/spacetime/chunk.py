"""Spatial chunking grid and addressing.

Divides a mosaic into fixed-size spatial chunks (default 512x512 @ 10m = 5.12 km).
Chunk IDs are (row, col) tuples. The grid is deterministic for a given AOI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from rasterio.transform import Affine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ChunkGrid:
    """Defines a spatial chunking grid over a mosaic extent."""

    n_rows: int
    n_cols: int
    chunk_size: int  # pixels per side
    mosaic_height: int
    mosaic_width: int
    transform: Affine
    epsg: int

    @property
    def n_chunks(self) -> int:
        return self.n_rows * self.n_cols

    @property
    def chunk_ids(self) -> list[tuple[int, int]]:
        return [(r, c) for r in range(self.n_rows) for c in range(self.n_cols)]

    def chunk_slice(self, row: int, col: int) -> tuple[slice, slice]:
        """Return (y_slice, x_slice) for extracting this chunk from the mosaic."""
        y0 = row * self.chunk_size
        x0 = col * self.chunk_size
        y1 = min(y0 + self.chunk_size, self.mosaic_height)
        x1 = min(x0 + self.chunk_size, self.mosaic_width)
        return slice(y0, y1), slice(x0, x1)

    def chunk_shape(self, row: int, col: int) -> tuple[int, int]:
        """Return (height, width) for this chunk (may be smaller at edges)."""
        ys, xs = self.chunk_slice(row, col)
        return ys.stop - ys.start, xs.stop - xs.start

    def chunk_transform(self, row: int, col: int) -> Affine:
        """Return the affine transform for this chunk's origin."""
        y0 = row * self.chunk_size
        x0 = col * self.chunk_size
        return self.transform * Affine.translation(x0, y0)

    def chunk_id_str(self, row: int, col: int) -> str:
        """String identifier for a chunk: 'r{row:03d}_c{col:03d}'."""
        return f"r{row:03d}_c{col:03d}"


def make_chunk_grid(
    mosaic_height: int,
    mosaic_width: int,
    transform: Affine,
    epsg: int,
    chunk_size: int = 512,
) -> ChunkGrid:
    """Create a ChunkGrid for a mosaic.

    Edge chunks may be smaller than chunk_size if the mosaic dimensions
    are not exact multiples.
    """
    import math

    n_rows = math.ceil(mosaic_height / chunk_size)
    n_cols = math.ceil(mosaic_width / chunk_size)

    grid = ChunkGrid(
        n_rows=n_rows,
        n_cols=n_cols,
        chunk_size=chunk_size,
        mosaic_height=mosaic_height,
        mosaic_width=mosaic_width,
        transform=transform,
        epsg=epsg,
    )
    logger.info(
        "ChunkGrid: %d x %d = %d chunks (%d px), mosaic %d x %d",
        n_rows,
        n_cols,
        grid.n_chunks,
        chunk_size,
        mosaic_height,
        mosaic_width,
    )
    return grid


def extract_chunk(
    bands: np.ndarray,
    grid: ChunkGrid,
    row: int,
    col: int,
) -> np.ndarray:
    """Extract a single chunk from a mosaic.

    Args:
        bands: Array of shape (n_bands, height, width) or (height, width)
        grid: ChunkGrid instance
        row: Chunk row index
        col: Chunk col index

    Returns:
        Array with spatial dims sliced to the chunk extent.
    """
    ys, xs = grid.chunk_slice(row, col)
    if bands.ndim == 3:
        return bands[:, ys, xs].copy()
    return bands[ys, xs].copy()


def extract_all_chunks(
    bands: np.ndarray,
    grid: ChunkGrid,
) -> dict[tuple[int, int], np.ndarray]:
    """Extract all chunks from a mosaic.

    Returns:
        Dict mapping (row, col) to chunk array.
    """
    chunks = {}
    for row, col in grid.chunk_ids:
        chunks[(row, col)] = extract_chunk(bands, grid, row, col)
    return chunks


def reassemble_mosaic(
    chunks: dict[tuple[int, int], np.ndarray],
    grid: ChunkGrid,
    n_bands: int | None = None,
) -> np.ndarray:
    """Reassemble chunks back into a full mosaic array.

    Args:
        chunks: Dict mapping (row, col) to chunk arrays
        grid: ChunkGrid used for extraction
        n_bands: Number of bands (inferred from first chunk if None)

    Returns:
        Reassembled array matching original mosaic dimensions.
    """
    sample = next(iter(chunks.values()))
    if sample.ndim == 3:
        if n_bands is None:
            n_bands = sample.shape[0]
        out = np.zeros((n_bands, grid.mosaic_height, grid.mosaic_width), dtype=sample.dtype)
        for (row, col), chunk in chunks.items():
            ys, xs = grid.chunk_slice(row, col)
            out[:, ys, xs] = chunk
    else:
        out = np.zeros((grid.mosaic_height, grid.mosaic_width), dtype=sample.dtype)
        for (row, col), chunk in chunks.items():
            ys, xs = grid.chunk_slice(row, col)
            out[ys, xs] = chunk
    return out
