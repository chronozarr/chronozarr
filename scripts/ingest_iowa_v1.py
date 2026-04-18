"""Ingest Iowa AOI as a v1 ChronoFabric store (2024, 12 months).

Usage:
    uv run python scripts/ingest_iowa_v1.py
    uv run python scripts/ingest_iowa_v1.py --skip-download
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ingest_iowa")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

AOI_NAME = "iowa_ames"
BBOX = (-93.80, 41.95, -93.55, 42.20)
EPSG = 32615
START = "2024-01-01"
END = "2024-12-31"
CHUNK_SIZE = 512


def download_mosaics() -> dict[str, Path]:
    from spacetime.catalog import search_scenes_by_month
    from spacetime.mosaic import build_monthly_mosaics

    out_dir = DATA / "mosaics" / AOI_NAME
    logger.info("Downloading monthly mosaics for %s (%s to %s)", AOI_NAME, START, END)
    t0 = time.time()

    by_month = search_scenes_by_month(BBOX, START, END, max_cloud_pct=80.0)
    outputs = build_monthly_mosaics(by_month, BBOX, target_epsg=EPSG, output_dir=out_dir)

    elapsed = time.time() - t0
    total_mb = sum(p.stat().st_size for p in outputs.values()) / 1e6
    logger.info("Download done: %d months, %.1f MB, %.0fs", len(outputs), total_mb, elapsed)
    return outputs


def encode_v1() -> None:
    from spacetime.chunk import make_chunk_grid
    from spacetime.encode.v1 import encode_v1
    from spacetime.mosaic import load_mosaic

    mosaic_dir = DATA / "mosaics" / AOI_NAME
    store_dir = DATA / "stores" / AOI_NAME / "v1"

    # Load all mosaics
    mosaics: dict[str, np.ndarray] = {}
    for npz in sorted(mosaic_dir.glob("*.npz")):
        month_key = npz.stem
        m = load_mosaic(npz)
        mosaics[month_key] = m["bands"]

    if not mosaics:
        raise RuntimeError(f"No mosaics found in {mosaic_dir}")

    logger.info("Loaded %d monthly mosaics", len(mosaics))

    # Build chunk grid from first mosaic
    first_path = sorted(mosaic_dir.glob("*.npz"))[0]
    m = load_mosaic(first_path)
    grid = make_chunk_grid(
        m["bands"].shape[1], m["bands"].shape[2], m["transform"], m["epsg"], CHUNK_SIZE
    )

    logger.info(
        "Grid: %dx%d chunks, mosaic %dx%d px",
        grid.n_rows,
        grid.n_cols,
        grid.mosaic_height,
        grid.mosaic_width,
    )

    # Encode v1
    t0 = time.time()
    result = encode_v1(mosaics, grid, store_dir, anchor_interval=6)
    elapsed = time.time() - t0

    logger.info(
        "V1 encode: %.2f MB, %d anchors, %d deltas, volatility=%.4f, %.0fs",
        result["total_bytes"] / 1e6,
        result["n_anchors"],
        result["n_deltas"],
        result["avg_volatility"],
        elapsed,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-download", action="store_true")
    args = parser.parse_args()

    if not args.skip_download:
        download_mosaics()

    encode_v1()
    logger.info("Done. Start server to benchmark.")


if __name__ == "__main__":
    main()
