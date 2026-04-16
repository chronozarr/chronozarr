"""Baseline A: per-product, per-month, per-chunk rendered PNGs.

This is the "conventional precomputed basemap" strawman.
Each chunk × month × product is stored as an independent PNG file.

Storage structure:
    store_dir/
        {product}/
            {month}/
                {chunk_id}.png

This is the worst-case for storage but the best-case for single-tile latency
(just serve a file, no decode needed beyond PNG).
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from spacetime.chunk import ChunkGrid, extract_chunk
from spacetime.render import render_product, _stretch_to_uint8

logger = logging.getLogger(__name__)

PRODUCTS = ("true_color", "false_color", "ndvi_rgb")


def encode(
    monthly_mosaics: dict[str, np.ndarray],
    grid: ChunkGrid,
    store_dir: Path,
    products: tuple[str, ...] = PRODUCTS,
) -> dict[str, int]:
    """Encode all months × chunks × products as PNGs.

    Args:
        monthly_mosaics: Dict mapping "YYYY-MM" to (n_bands, H, W) uint16 arrays
        grid: ChunkGrid for spatial chunking
        store_dir: Root directory for the store
        products: Which products to render and store

    Returns:
        Dict of metrics: total_bytes, n_files, bytes_per_product, etc.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    store_dir.mkdir(parents=True, exist_ok=True)
    total_bytes = 0
    n_files = 0
    bytes_by_product: dict[str, int] = {p: 0 for p in products}

    for month_key in sorted(monthly_mosaics.keys()):
        bands = monthly_mosaics[month_key]
        for product in products:
            product_dir = store_dir / product / month_key
            product_dir.mkdir(parents=True, exist_ok=True)

            for row, col in grid.chunk_ids:
                chunk = extract_chunk(bands, grid, row, col)
                rendered = render_product(chunk, product)

                chunk_id = grid.chunk_id_str(row, col)
                out_path = product_dir / f"{chunk_id}.png"

                if rendered.ndim == 3 and rendered.dtype == np.uint8:
                    plt.imsave(str(out_path), rendered)
                else:
                    # Float index → colormap → PNG
                    plt.imsave(str(out_path), rendered, cmap="RdYlGn", vmin=-1, vmax=1)

                fsize = out_path.stat().st_size
                total_bytes += fsize
                bytes_by_product[product] += fsize
                n_files += 1

    metrics = {
        "total_bytes": total_bytes,
        "n_files": n_files,
        "n_months": len(monthly_mosaics),
        "n_chunks": grid.n_chunks,
        "n_products": len(products),
    }
    metrics.update({f"bytes_{p}": v for p, v in bytes_by_product.items()})

    logger.info(
        "Baseline A: %d files, %.2f MB total",
        n_files,
        total_bytes / 1e6,
    )
    return metrics


def decode_tile(store_dir: Path, product: str, month: str, chunk_id: str) -> np.ndarray:
    """Read a single tile (for access simulation).

    Returns:
        uint8 (H, W, 3) RGB array
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    path = store_dir / product / month / f"{chunk_id}.png"
    img = plt.imread(str(path))
    if img.dtype == np.float32:
        img = (img * 255).astype(np.uint8)
    # Drop alpha channel if present
    if img.ndim == 3 and img.shape[2] == 4:
        img = img[:, :, :3]
    return img


def bytes_for_tile(store_dir: Path, product: str, month: str, chunk_id: str) -> int:
    """Return the file size for a single tile (for access simulation)."""
    path = store_dir / product / month / f"{chunk_id}.png"
    return path.stat().st_size
