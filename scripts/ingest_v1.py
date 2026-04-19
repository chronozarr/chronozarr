"""Ingest any AOI from aois.yaml as a v1 ChronoFabric store.

Usage:
    uv run python scripts/ingest_v1.py --aoi nile_delta
    uv run python scripts/ingest_v1.py --aoi nile_delta --skip-download
    uv run python scripts/ingest_v1.py --aoi nile_delta --start 2020-01-01 --end 2024-12-31
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import numpy as np
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ingest_v1")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CHUNK_SIZE = 512


def load_aoi_config(aoi_name: str) -> dict:
    with open(ROOT / "experiments" / "aois.yaml") as f:
        cfg = yaml.safe_load(f)
    if aoi_name not in cfg["aois"]:
        available = list(cfg["aois"].keys())
        raise ValueError(f"Unknown AOI '{aoi_name}'. Available: {available}")
    aoi = cfg["aois"][aoi_name]
    aoi["name"] = aoi_name
    return aoi


def download_mosaics(aoi: dict, start: str, end: str) -> dict[str, Path]:
    from spacetime.catalog import search_scenes_by_month
    from spacetime.mosaic import build_monthly_mosaics

    name = aoi["name"]
    bbox = tuple(aoi["bbox"])
    epsg = aoi["epsg"]
    out_dir = DATA / "mosaics" / name

    logger.info("Downloading monthly mosaics for %s (%s to %s)", name, start, end)
    t0 = time.time()

    by_month = search_scenes_by_month(bbox, start, end, max_cloud_pct=80.0)
    outputs = build_monthly_mosaics(by_month, bbox, target_epsg=epsg, output_dir=out_dir)

    elapsed = time.time() - t0
    total_mb = sum(p.stat().st_size for p in outputs.values()) / 1e6
    logger.info("Download done: %d months, %.1f MB, %.0fs", len(outputs), total_mb, elapsed)
    return outputs


def encode(aoi: dict) -> None:
    from spacetime.chunk import make_chunk_grid
    from spacetime.encode.v1 import encode_v1
    from spacetime.mosaic import load_mosaic

    name = aoi["name"]
    mosaic_dir = DATA / "mosaics" / name
    store_dir = DATA / "stores" / name / "v1"

    mosaics: dict[str, np.ndarray] = {}
    for npz in sorted(mosaic_dir.glob("*.npz")):
        m = load_mosaic(npz)
        mosaics[npz.stem] = m["bands"]

    if not mosaics:
        raise RuntimeError(f"No mosaics found in {mosaic_dir}")

    logger.info("Loaded %d monthly mosaics", len(mosaics))

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
    parser = argparse.ArgumentParser(description="Ingest AOI as v1 ChronoFabric store")
    parser.add_argument("--aoi", required=True, help="AOI name from aois.yaml")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--start", default="2015-07-01", help="Start date (default: 2015-07-01)")
    parser.add_argument("--end", default="2026-04-01", help="End date (default: 2026-04-01)")
    args = parser.parse_args()

    aoi = load_aoi_config(args.aoi)
    start = args.start
    end = args.end

    if not args.skip_download:
        download_mosaics(aoi, start, end)

    encode(aoi)
    logger.info("Done: %s", args.aoi)


if __name__ == "__main__":
    main()
