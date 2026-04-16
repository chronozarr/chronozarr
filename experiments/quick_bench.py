"""Quick benchmark: run encoding + comparison on whatever mosaics exist.

Usage:
    uv run python experiments/quick_bench.py --aoi sahara_tamanrasset
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("quick_bench")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--aoi", required=True)
    parser.add_argument("--chunk-size", type=int, default=512)
    args = parser.parse_args()

    from spacetime.chunk import extract_chunk, make_chunk_grid
    from spacetime.encode import baseline_b, experimental
    from spacetime.mosaic import load_mosaic

    mosaic_dir = DATA / "mosaics" / args.aoi
    npz_files = sorted(mosaic_dir.glob("*.npz"))
    if len(npz_files) < 2:
        logger.error("Need at least 2 months. Found %d in %s", len(npz_files), mosaic_dir)
        return

    logger.info("Loading %d mosaics from %s", len(npz_files), mosaic_dir)
    mosaics = {}
    for npz in npz_files:
        m = load_mosaic(npz)
        mosaics[npz.stem] = m["bands"]

    # Grid from first mosaic
    m0 = load_mosaic(npz_files[0])
    grid = make_chunk_grid(
        m0["bands"].shape[1],
        m0["bands"].shape[2],
        m0["transform"],
        m0["epsg"],
        args.chunk_size,
    )

    months = sorted(mosaics.keys())
    n_months = len(months)
    raw_size = sum(m.nbytes for m in mosaics.values())

    print(f"\n{'=' * 60}")
    print(f"AOI: {args.aoi}")
    print(f"Months: {n_months} ({months[0]} .. {months[-1]})")
    print(f"Grid: {grid.n_rows}x{grid.n_cols} = {grid.n_chunks} chunks")
    print(f"Raw uncompressed: {raw_size / 1e6:.1f} MB")
    print(f"{'=' * 60}")

    store_root = DATA / "stores" / args.aoi

    # --- Temporal redundancy analysis (chunk 0,0) ---
    print("\n--- Temporal Redundancy Analysis (chunk 0,0) ---")
    c00_series = [extract_chunk(mosaics[m], grid, 0, 0) for m in months]
    for i in range(1, len(c00_series)):
        delta = c00_series[i].astype(np.int32) - c00_series[i - 1].astype(np.int32)
        abs_d = np.abs(delta)
        print(
            f"  {months[i - 1]}→{months[i]}: "
            f"mean|Δ|={abs_d.mean():.1f}, max|Δ|={abs_d.max()}, "
            f"%zero={100 * (delta == 0).mean():.1f}%, "
            f"%<50={100 * (abs_d < 50).mean():.1f}%, "
            f"%<200={100 * (abs_d < 200).mean():.1f}%"
        )

    # --- Encode all representations ---
    results = {}

    # B1
    logger.info("Encoding B1...")
    t0 = time.time()
    results["B1"] = baseline_b.encode_b1(mosaics, grid, store_root / "baseline_b")
    logger.info("B1: %.1fs", time.time() - t0)

    # B2
    logger.info("Encoding B2 (bulk)...")
    t0 = time.time()
    results["B2_bulk"] = baseline_b.encode_b2(mosaics, grid, store_root / "baseline_b")
    logger.info("B2_bulk: %.1fs", time.time() - t0)

    # B2-chunked
    logger.info("Encoding B2 (chunked-time)...")
    t0 = time.time()
    results["B2_chunked"] = baseline_b.encode_b2_chunked_time(
        mosaics, grid, store_root / "baseline_b"
    )
    logger.info("B2_chunked: %.1fs", time.time() - t0)

    # Experimental at various keyframe intervals
    for kf in [3, 6, 12]:
        label = f"X_kf{kf}"
        logger.info("Encoding %s...", label)
        t0 = time.time()
        results[label] = experimental.encode(
            mosaics,
            grid,
            store_root / f"experimental_kf{kf}",
            keyframe_interval=kf,
        )
        logger.info("%s: %.1fs", label, time.time() - t0)

    # --- Storage comparison ---
    print("\n--- Storage Comparison ---")
    hdr = f"{'Rep':<20} {'Total MB':>10} {'Per Chunk/Mo':>14} {'vs Raw':>10} {'vs B1':>10}"
    print(hdr)
    print("-" * 70)
    b1_bytes = results["B1"]["total_bytes"]
    for label, m in results.items():
        total = m["total_bytes"]
        n = m.get("n_months", n_months) * grid.n_chunks
        per = total / max(n, 1)
        vs_raw = raw_size / max(total, 1)
        vs_b1 = b1_bytes / max(total, 1)
        print(f"{label:<20} {total / 1e6:>10.2f} {per:>14.0f} {vs_raw:>9.1f}x {vs_b1:>9.2f}x")

    # --- Reconstruction quality check ---
    print("\n--- Reconstruction Quality ---")
    for kf in [3, 6, 12]:
        label = f"experimental_kf{kf}"
        exp_store = store_root / label
        max_err = 0
        for month in months:
            original = extract_chunk(mosaics[month], grid, 0, 0)
            reconstructed = experimental.decode(exp_store, "r000_c000", month)
            diff = np.abs(original.astype(np.int32) - reconstructed.astype(np.int32))
            max_err = max(max_err, diff.max())
        print(f"  kf={kf}: max_error={max_err} ({'LOSSLESS' if max_err == 0 else 'LOSSY'})")

    # --- Access cost simulation ---
    print("\n--- Access Cost: fetch month t for chunk (0,0) ---")
    mid = len(months) // 2
    test_month = months[mid]
    print(f"  Test month: {test_month} (index {mid})")

    b_store = store_root / "baseline_b"
    b1_bytes_tile = baseline_b.bytes_for_b1_tile(b_store, "r000_c000", test_month)
    print(f"  B1 (1 tile):     {b1_bytes_tile:>10,} bytes")

    b2_bytes_tile = baseline_b.bytes_for_b2_tile(store_root / "baseline_b", "r000_c000")
    print(f"  B2_bulk (full stack): {b2_bytes_tile:>10,} bytes")

    b2c_bytes_tile = baseline_b.bytes_for_b2_chunked_tile(
        store_root / "baseline_b", "r000_c000", mid
    )
    print(f"  B2_chunked (1 month): {b2c_bytes_tile:>10,} bytes")

    for kf in [3, 6, 12]:
        exp_bytes = experimental.bytes_to_decode(
            store_root / f"experimental_kf{kf}", "r000_c000", test_month
        )
        print(f"  X_kf{kf} (kf+deltas):  {exp_bytes:>10,} bytes")

    print()


if __name__ == "__main__":
    main()
