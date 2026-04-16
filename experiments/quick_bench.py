"""Quick benchmark: tightened framing with three explicit hypotheses.

Hypotheses tested:
    H1: Product unification — one multiband payload serves multiple products
        without duplicating storage (B vs A)
    H2: Temporal encoding — keyframes + deltas reduce storage relative to
        independent monthly chunks (X vs B2_chunked, the real baseline)
    H3: Access-pattern — native representation reduces bytes read for
        realistic sessions, especially incremental time-step cost

Usage:
    uv run python experiments/quick_bench.py --aoi sahara_tamanrasset
    uv run python experiments/quick_bench.py --aoi sahara_tamanrasset --chunk-sizes 256,512,1024
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


def load_mosaics(aoi: str):
    from spacetime.mosaic import load_mosaic

    mosaic_dir = DATA / "mosaics" / aoi
    npz_files = sorted(mosaic_dir.glob("*.npz"))
    if len(npz_files) < 2:
        logger.error("Need >=2 months. Found %d in %s", len(npz_files), mosaic_dir)
        return None, None
    mosaics = {}
    meta = None
    for npz in npz_files:
        m = load_mosaic(npz)
        mosaics[npz.stem] = m["bands"]
        if meta is None:
            meta = m
    return mosaics, meta


def run_benchmark(aoi: str, mosaics: dict, meta: dict, chunk_size: int):
    from spacetime.bench import (
        init_db,
        record_change_analysis,
        record_incremental_cost,
    )
    from spacetime.chunk import extract_chunk, make_chunk_grid
    from spacetime.encode import baseline_a, baseline_b, experimental

    months = sorted(mosaics.keys())
    n_months = len(months)
    bands_shape = next(iter(mosaics.values())).shape
    grid = make_chunk_grid(
        bands_shape[1], bands_shape[2], meta["transform"], meta["epsg"], chunk_size
    )
    raw_size = sum(m.nbytes for m in mosaics.values())

    print(f"\n{'=' * 70}")
    print(f"AOI: {aoi}  |  Chunk size: {chunk_size}px  |  Months: {n_months}")
    print(f"Grid: {grid.n_rows}x{grid.n_cols} = {grid.n_chunks} chunks")
    print(f"Raw uncompressed: {raw_size / 1e6:.1f} MB")
    print(f"{'=' * 70}")

    store_root = DATA / "stores" / aoi / f"cs{chunk_size}"
    db = init_db(DATA / "reports" / "bench.duckdb")

    # ===== Change analysis (first-class) =====
    print("\n--- Temporal Change Analysis (sample chunks) ---")
    sample_chunks = [
        (0, 0),
        (grid.n_rows // 2, grid.n_cols // 2),
        (grid.n_rows - 1, grid.n_cols - 1),
    ]
    for r, c in sample_chunks:
        cid = grid.chunk_id_str(r, c)
        print(f"\n  Chunk {cid}:")
        for i in range(1, len(months)):
            prev = extract_chunk(mosaics[months[i - 1]], grid, r, c)
            curr = extract_chunk(mosaics[months[i]], grid, r, c)
            delta = curr.astype(np.int32) - prev.astype(np.int32)
            abs_d = np.abs(delta)

            # Changed pixels (any band > 50 reflectance units)
            changed = np.any(abs_d > 50, axis=0)
            pct_changed = changed.mean() * 100

            print(
                f"    {months[i - 1]}->{months[i]}: "
                f"changed={pct_changed:5.1f}%  "
                f"mean|d|={abs_d.mean():6.1f}  "
                f"max|d|={abs_d.max():5d}  "
                f"%<50={100 * (abs_d < 50).mean():5.1f}%"
            )

            record_change_analysis(db, aoi, cid, months[i - 1], months[i], prev, curr)

    # ===== Encode all representations =====
    results = {}

    # Baseline A
    logger.info("Encoding Baseline A...")
    t0 = time.time()
    results["A"] = baseline_a.encode(mosaics, grid, store_root / "baseline_a")
    logger.info("A: %.1fs", time.time() - t0)

    # Baseline B1
    logger.info("Encoding B1...")
    t0 = time.time()
    results["B1"] = baseline_b.encode_b1(mosaics, grid, store_root / "baseline_b")
    logger.info("B1: %.1fs", time.time() - t0)

    # B2_chunked (THE real baseline)
    logger.info("Encoding B2_chunked (real baseline)...")
    t0 = time.time()
    results["B2_chunked"] = baseline_b.encode_b2_chunked_time(
        mosaics, grid, store_root / "baseline_b"
    )
    logger.info("B2_chunked: %.1fs", time.time() - t0)

    # B2 bulk (for comparison)
    logger.info("Encoding B2_bulk...")
    t0 = time.time()
    results["B2_bulk"] = baseline_b.encode_b2(mosaics, grid, store_root / "baseline_b")
    logger.info("B2_bulk: %.1fs", time.time() - t0)

    # Experimental: sweep keyframe interval
    for kf in [1, 3, 6, 12]:
        if kf > n_months:
            continue
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

    # ===== H1: Product unification =====
    print("\n--- H1: Product Unification ---")
    a_bytes = results["A"]["total_bytes"]
    b1_bytes = results["B1"]["total_bytes"]
    print(f"  Baseline A (3 pre-rendered products): {a_bytes / 1e6:>8.2f} MB")
    print(f"  Baseline B1 (multiband, derive at render): {b1_bytes / 1e6:>8.2f} MB")
    print(f"  Product duplication cost: {a_bytes / max(b1_bytes, 1):.2f}x")
    print(
        f"  --> Storing bands once saves {(a_bytes - b1_bytes) / 1e6:.1f} MB "
        f"({100 * (1 - b1_bytes / max(a_bytes, 1)):.0f}%)"
    )

    # ===== H2: Temporal encoding vs B2_chunked =====
    print("\n--- H2: Temporal Encoding (vs B2_chunked) ---")
    b2c_bytes = results["B2_chunked"]["total_bytes"]
    hdr = f"  {'Rep':<15} {'Total MB':>10} {'vs B2c':>10} {'vs Raw':>10}"
    print(hdr)
    print(f"  {'-' * 48}")
    print(f"  {'Raw':<15} {raw_size / 1e6:>10.2f} {'1.0x':>10} {'1.0x':>10}")
    for label in ["B1", "B2_bulk", "B2_chunked"] + [k for k in results if k.startswith("X_kf")]:
        total = results[label]["total_bytes"]
        vs_b2c = b2c_bytes / max(total, 1)
        vs_raw = raw_size / max(total, 1)
        marker = " <-- real baseline" if label == "B2_chunked" else ""
        print(f"  {label:<15} {total / 1e6:>10.2f} {vs_b2c:>9.2f}x {vs_raw:>9.1f}x{marker}")

    # ===== H3: Incremental time-step cost (the killer metric) =====
    print("\n--- H3: Incremental Time-Step Cost ---")
    print("  If already viewing chunk at month t, marginal cost to move to t+1:")

    test_chunk = grid.chunk_id_str(0, 0)
    b_store = store_root / "baseline_b"

    # B1: each month is independent, so marginal = full tile
    b1_tile = baseline_b.bytes_for_b1_tile(b_store, test_chunk, months[1])
    print(f"\n  B1 (independent monthly): always {b1_tile:,} bytes (full tile)")

    # B2_chunked: also independent per-month chunks
    mid = n_months // 2
    b2c_tile = baseline_b.bytes_for_b2_chunked_tile(b_store, test_chunk, mid)
    print(f"  B2_chunked: always ~{b2c_tile:,} bytes (per-month Zarr chunk)")

    # Experimental: marginal cost = one delta read
    for kf in [1, 3, 6, 12]:
        if kf > n_months:
            continue
        label = f"X_kf{kf}"
        exp_store = store_root / f"experimental_kf{kf}"
        if not exp_store.exists():
            continue

        print(f"\n  {label}:")
        incremental_bytes = []
        for i in range(1, min(n_months, 13)):
            month_prev = months[i - 1]
            month_curr = months[i]

            # Already at t-1, cost to get t
            bytes_t = experimental.bytes_to_decode(exp_store, test_chunk, month_curr)
            bytes_prev = experimental.bytes_to_decode(exp_store, test_chunk, month_prev)

            # If viewer caches the keyframe + accumulated state, marginal cost
            # is just the delta for this month (or full keyframe if boundary)
            is_kf_boundary = i % kf == 0
            marginal = bytes_t if is_kf_boundary else bytes_t - bytes_prev

            t0 = time.perf_counter()
            _ = experimental.decode(exp_store, test_chunk, month_curr)
            decode_ms = (time.perf_counter() - t0) * 1000

            incremental_bytes.append(marginal)
            kf_tag = " [KF]" if is_kf_boundary else ""
            print(
                f"    {month_prev}->{month_curr}: "
                f"marginal={marginal:>8,} bytes  "
                f"decode={decode_ms:>6.1f}ms{kf_tag}"
            )

            record_incremental_cost(
                db,
                aoi,
                "experimental",
                label,
                test_chunk,
                month_prev,
                month_curr,
                marginal,
                decode_ms,
                is_kf_boundary,
            )

        non_kf = [b for i, b in enumerate(incremental_bytes) if (i + 1) % kf != 0]
        if non_kf:
            print(
                f"    Avg marginal (non-keyframe): {np.mean(non_kf):,.0f} bytes "
                f"vs B1={b1_tile:,} bytes "
                f"({np.mean(non_kf) / b1_tile * 100:.1f}%)"
            )

    # ===== Reconstruction quality =====
    print("\n--- Reconstruction Quality ---")
    for kf in [1, 3, 6, 12]:
        if kf > n_months:
            continue
        exp_store = store_root / f"experimental_kf{kf}"
        if not exp_store.exists():
            continue
        max_err = 0
        for month in months:
            original = extract_chunk(mosaics[month], grid, 0, 0)
            reconstructed = experimental.decode(exp_store, "r000_c000", month)
            diff = np.abs(original.astype(np.int32) - reconstructed.astype(np.int32))
            max_err = max(max_err, diff.max())
        status = "LOSSLESS" if max_err == 0 else f"LOSSY (max_err={max_err})"
        print(f"  kf={kf}: {status}")

    db.close()
    print()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--aoi", required=True)
    parser.add_argument(
        "--chunk-sizes",
        default="512",
        help="Comma-separated chunk sizes to sweep (default: 512)",
    )
    args = parser.parse_args()

    mosaics, meta = load_mosaics(args.aoi)
    if mosaics is None:
        return

    chunk_sizes = [int(s) for s in args.chunk_sizes.split(",")]
    for cs in chunk_sizes:
        run_benchmark(args.aoi, mosaics, meta, cs)


if __name__ == "__main__":
    main()
