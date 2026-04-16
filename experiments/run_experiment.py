"""Main experiment runner: end-to-end pipeline for one AOI.

Usage:
    uv run python experiments/run_experiment.py --aoi sahara_tamanrasset
    uv run python experiments/run_experiment.py --aoi iowa_ames
    uv run python experiments/run_experiment.py --aoi sahara_tamanrasset --skip-download
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
logger = logging.getLogger("experiment")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_config(aoi_name: str) -> dict:
    with open(ROOT / "experiments" / "aois.yaml") as f:
        cfg = yaml.safe_load(f)
    aoi = cfg["aois"][aoi_name]
    aoi["name"] = aoi_name
    aoi["time_range"] = cfg["time_range"]
    aoi["bands"] = cfg["bands"]
    aoi["chunk_size"] = cfg["chunk_size"]
    return aoi


def phase1_mosaics(cfg: dict) -> dict[str, Path]:
    """Download and composite monthly mosaics."""
    from spacetime.catalog import search_scenes_by_month
    from spacetime.mosaic import build_monthly_mosaics

    aoi_name = cfg["name"]
    bbox = tuple(cfg["bbox"])
    start = cfg["time_range"]["start"]
    end = cfg["time_range"]["end"]
    epsg = cfg["epsg"]
    out_dir = DATA / "mosaics" / aoi_name

    logger.info("Phase 1: Monthly mosaics for %s", aoi_name)
    t0 = time.time()

    by_month = search_scenes_by_month(bbox, start, end, max_cloud_pct=80.0)
    outputs = build_monthly_mosaics(by_month, bbox, target_epsg=epsg, output_dir=out_dir)

    elapsed = time.time() - t0
    total_mb = sum(p.stat().st_size for p in outputs.values()) / 1e6
    logger.info(
        "Phase 1 done: %d months, %.1f MB total, %.0fs elapsed",
        len(outputs),
        total_mb,
        elapsed,
    )
    return outputs


def load_all_mosaics(cfg: dict) -> dict[str, np.ndarray]:
    """Load all monthly mosaics from disk."""
    from spacetime.mosaic import load_mosaic

    mosaic_dir = DATA / "mosaics" / cfg["name"]
    mosaics = {}
    for npz in sorted(mosaic_dir.glob("*.npz")):
        month_key = npz.stem
        m = load_mosaic(npz)
        mosaics[month_key] = m["bands"]
    logger.info("Loaded %d monthly mosaics from %s", len(mosaics), mosaic_dir)
    return mosaics


def phase2_encode(cfg: dict, mosaics: dict[str, np.ndarray]) -> dict:
    """Encode all representations and record metrics."""
    from spacetime.bench import init_db, record_delta_stats, record_storage
    from spacetime.chunk import make_chunk_grid
    from spacetime.encode import baseline_a, baseline_b, experimental
    from spacetime.mosaic import load_mosaic

    aoi_name = cfg["name"]
    chunk_size = cfg["chunk_size"]

    # Get grid dimensions from first mosaic
    first_mosaic_path = DATA / "mosaics" / aoi_name / f"{sorted(mosaics.keys())[0]}.npz"
    m = load_mosaic(first_mosaic_path)
    bands = m["bands"]
    grid = make_chunk_grid(bands.shape[1], bands.shape[2], m["transform"], m["epsg"], chunk_size)

    # Raw uncompressed size per chunk per month: 4 bands x chunk_h x chunk_w x 2 bytes
    raw_chunk_bytes = 4 * chunk_size * chunk_size * 2  # ~2 MB for 512x512

    db = init_db(DATA / "reports" / "bench.duckdb")
    store_root = DATA / "stores" / aoi_name
    all_metrics = {}

    # --- Baseline A: per-product PNGs ---
    logger.info("Encoding Baseline A (per-product PNGs)...")
    t0 = time.time()
    metrics_a = baseline_a.encode(mosaics, grid, store_root / "baseline_a")
    logger.info("Baseline A: %.1fs", time.time() - t0)
    record_storage(db, aoi_name, "baseline_a", metrics_a, raw_chunk_bytes)
    all_metrics["baseline_a"] = metrics_a

    # --- Baseline B1: independent multiband Zarr ---
    logger.info("Encoding Baseline B1 (independent multiband Zarr)...")
    t0 = time.time()
    metrics_b1 = baseline_b.encode_b1(mosaics, grid, store_root / "baseline_b")
    logger.info("Baseline B1: %.1fs", time.time() - t0)
    record_storage(db, aoi_name, "baseline_b", metrics_b1, raw_chunk_bytes)
    all_metrics["baseline_b1"] = metrics_b1

    # --- Baseline B2: time-stacked Zarr (bulk compressed) ---
    logger.info("Encoding Baseline B2 (time-stacked Zarr)...")
    t0 = time.time()
    metrics_b2 = baseline_b.encode_b2(mosaics, grid, store_root / "baseline_b")
    logger.info("Baseline B2: %.1fs", time.time() - t0)
    record_storage(db, aoi_name, "baseline_b", metrics_b2, raw_chunk_bytes)
    all_metrics["baseline_b2"] = metrics_b2

    # --- Baseline B2-chunked: per-month accessible ---
    logger.info("Encoding Baseline B2-chunked...")
    t0 = time.time()
    metrics_b2c = baseline_b.encode_b2_chunked_time(mosaics, grid, store_root / "baseline_b")
    logger.info("Baseline B2-chunked: %.1fs", time.time() - t0)
    record_storage(db, aoi_name, "baseline_b", metrics_b2c, raw_chunk_bytes)
    all_metrics["baseline_b2_chunked"] = metrics_b2c

    # --- Experimental: keyframe + delta at various intervals ---
    for kf_interval in [3, 6, 12]:
        label = f"experimental_kf{kf_interval}"
        logger.info("Encoding %s...", label)
        t0 = time.time()
        metrics_x = experimental.encode(
            mosaics,
            grid,
            store_root / label,
            keyframe_interval=kf_interval,
        )
        logger.info("%s: %.1fs", label, time.time() - t0)
        record_storage(db, aoi_name, "experimental", metrics_x, raw_chunk_bytes)
        record_delta_stats(db, aoi_name, kf_interval, metrics_x.get("delta_stats", []))
        all_metrics[label] = metrics_x

    db.close()
    return {"grid": grid, "mosaic_meta": m, "metrics": all_metrics}


def phase3_quality(cfg: dict, mosaics: dict[str, np.ndarray], encode_result: dict) -> None:
    """Verify reconstruction quality and generate QC outputs."""
    from spacetime.bench import init_db, record_quality
    from spacetime.chunk import extract_chunk
    from spacetime.encode import experimental
    from spacetime.qc import save_comparison_panel, save_delta_stats_plot

    aoi_name = cfg["name"]
    grid = encode_result["grid"]
    store_root = DATA / "stores" / aoi_name
    qc_dir = DATA / "reports" / "qc" / aoi_name
    db = init_db(DATA / "reports" / "bench.duckdb")

    months = sorted(mosaics.keys())
    # Check reconstruction for a sample of chunks and months
    sample_chunks = [(0, 0), (grid.n_rows // 2, grid.n_cols // 2)]
    sample_months = [
        months[0],
        months[len(months) // 4],
        months[len(months) // 2],
        months[3 * len(months) // 4],
        months[-1],
    ]

    for kf_interval in [3, 6, 12]:
        label = f"experimental_kf{kf_interval}"
        exp_store = store_root / label

        for r, c in sample_chunks:
            chunk_id = grid.chunk_id_str(r, c)
            for month in sample_months:
                original = extract_chunk(mosaics[month], grid, r, c)
                reconstructed = experimental.decode(exp_store, chunk_id, month)

                # Record metrics
                record_quality(
                    db, aoi_name, "experimental", label, month, chunk_id, original, reconstructed
                )

                # Visual comparison for kf=6 only (avoid too many PNGs)
                if kf_interval == 6:
                    save_comparison_panel(
                        original,
                        reconstructed,
                        month,
                        chunk_id,
                        qc_dir / "comparisons",
                    )

        # Delta stats plot
        metrics = encode_result["metrics"].get(label, {})
        delta_stats = metrics.get("delta_stats", [])
        if delta_stats:
            save_delta_stats_plot(delta_stats, aoi_name, kf_interval, qc_dir)

    db.close()
    logger.info("Phase 3 done: QC outputs in %s", qc_dir)


def _log_access(label: str, product: str, r) -> None:
    logger.info(
        "%s cold_viewport %s: %d bytes, %.1fms",
        label,
        product,
        r.bytes_fetched,
        r.decode_time_ms,
    )


def phase4_access_sim(cfg: dict, mosaics: dict[str, np.ndarray], encode_result: dict) -> None:
    """Run access pattern simulations and record results."""
    from spacetime.access import (
        sim_cold_viewport_baseline_a,
        sim_cold_viewport_baseline_b1,
        sim_cold_viewport_experimental,
        sim_product_switch,
        sim_time_scrub_experimental,
    )
    from spacetime.bench import init_db, record_access

    aoi_name = cfg["name"]
    grid = encode_result["grid"]
    store_root = DATA / "stores" / aoi_name
    db = init_db(DATA / "reports" / "bench.duckdb")

    months = sorted(mosaics.keys())
    # Center viewport in the middle of the grid
    cr, cc = grid.n_rows // 2, grid.n_cols // 2
    test_month = months[len(months) // 2]

    logger.info("Access sim: center=(%d,%d), month=%s", cr, cc, test_month)

    # Cold viewport tests
    for product in ["true_color", "ndvi_rgb"]:
        # Baseline A
        r = sim_cold_viewport_baseline_a(
            store_root / "baseline_a", grid, cr, cc, test_month, product
        )
        record_access(db, aoi_name, "baseline_a", "", r)
        _log_access("A", product, r)
        record_access(db, aoi_name, "baseline_a", "", r)

        # Baseline B1
        r = sim_cold_viewport_baseline_b1(
            store_root / "baseline_b", grid, cr, cc, test_month, product
        )
        _log_access("B1", product, r)
        record_access(db, aoi_name, "baseline_b", "b1", r)

        # Experimental (kf=6)
        r = sim_cold_viewport_experimental(
            store_root / "experimental_kf6", grid, cr, cc, test_month, product
        )
        _log_access("X(kf6)", product, r)
        record_access(db, aoi_name, "experimental", "kf6", r)

    # Time scrub: 6 consecutive months
    scrub_start = max(0, len(months) // 2 - 3)
    scrub_months = months[scrub_start : scrub_start + 6]
    results = sim_time_scrub_experimental(
        store_root / "experimental_kf6", grid, cr, cc, scrub_months, "true_color"
    )
    for r in results:
        record_access(db, aoi_name, "experimental", "kf6", r)
    logger.info(
        "X(kf6) time_scrub: avg %d bytes/step, avg %.1fms/step",
        np.mean([r.bytes_fetched for r in results]),
        np.mean([r.decode_time_ms for r in results]),
    )

    # Product switch
    products = ["true_color", "false_color", "ndvi_rgb"]
    for rep, var, store_path in [
        ("baseline_a", "", store_root / "baseline_a"),
        ("baseline_b", "b1", store_root / "baseline_b"),
        ("experimental", "kf6", store_root / "experimental_kf6"),
    ]:
        rep_name = rep if rep != "baseline_b" else "baseline_b1"
        results = sim_product_switch(store_path, grid, cr, cc, test_month, products, rep_name)
        for r in results:
            record_access(db, aoi_name, rep, var, r)
        total_bytes = sum(r.bytes_fetched for r in results)
        logger.info(
            "%s product_switch (%s): %d total bytes for %d products",
            rep,
            var,
            total_bytes,
            len(products),
        )

    db.close()
    logger.info("Phase 4 done: access simulation complete")


def phase5_report(cfg: dict) -> None:
    """Generate summary reports from the benchmark database."""
    from spacetime.bench import init_db, print_storage_summary

    db = init_db(DATA / "reports" / "bench.duckdb")
    print_storage_summary(db)

    # Access summary
    result = db.execute(
        """
        SELECT representation, variant, pattern, product,
               AVG(bytes_fetched) as avg_bytes,
               AVG(decode_time_ms) as avg_ms
        FROM access_metrics
        WHERE aoi = ?
        GROUP BY representation, variant, pattern, product
        ORDER BY pattern, representation, variant
    """,
        [cfg["name"]],
    ).fetchall()

    print("\n=== Access Pattern Summary ===")
    print(
        f"{'Rep':<15} {'Variant':<10} {'Pattern':<15} {'Product':<15} "
        f"{'Avg Bytes':>12} {'Avg ms':>10}"
    )
    print("-" * 80)
    for row in result:
        rep, var, pat, prod, avg_b, avg_ms = row
        print(f"{rep:<15} {var:<10} {pat:<15} {prod:<15} {avg_b:>12,.0f} {avg_ms:>10.1f}")

    # Quality summary
    result = db.execute(
        """
        SELECT variant, AVG(psnr) as avg_psnr, AVG(ssim) as avg_ssim,
               MAX(max_abs_error) as worst_error
        FROM quality_metrics
        WHERE aoi = ?
        GROUP BY variant
        ORDER BY variant
    """,
        [cfg["name"]],
    ).fetchall()

    print("\n=== Reconstruction Quality ===")
    print(f"{'Variant':<25} {'Avg PSNR':>10} {'Avg SSIM':>10} {'Worst Error':>12}")
    print("-" * 60)
    for row in result:
        var, psnr, ssim, worst = row
        psnr_str = f"{psnr:.1f}" if psnr != float("inf") else "inf"
        print(f"{var:<25} {psnr_str:>10} {ssim:.6f}{'':>3} {worst:>12}")

    # Delta stats summary
    result = db.execute(
        """
        SELECT keyframe_interval,
               AVG(mean_abs_delta) as avg_mean_delta,
               AVG(pct_zero) as avg_pct_zero,
               AVG(pct_under_50) as avg_pct_under_50
        FROM delta_stats
        WHERE aoi = ?
        GROUP BY keyframe_interval
        ORDER BY keyframe_interval
    """,
        [cfg["name"]],
    ).fetchall()

    if result:
        print("\n=== Delta Statistics (chunk 0,0) ===")
        print(f"{'KF Interval':>12} {'Avg |delta|':>12} {'% zero':>10} {'% < 50':>10}")
        print("-" * 50)
        for row in result:
            kf, avg_d, pz, p50 = row
            print(f"{kf:>12} {avg_d:>12.1f} {pz:>10.1f} {p50:>10.1f}")

    db.close()


def main():
    parser = argparse.ArgumentParser(description="Spacetime chunk experiment runner")
    parser.add_argument("--aoi", required=True, help="AOI name from aois.yaml")
    parser.add_argument(
        "--skip-download", action="store_true", help="Skip Phase 1 (use existing mosaics)"
    )
    parser.add_argument(
        "--phases", default="1,2,3,4,5", help="Comma-separated phase numbers to run"
    )
    args = parser.parse_args()

    phases = {int(p) for p in args.phases.split(",")}
    cfg = load_config(args.aoi)
    logger.info("Running experiment for %s (phases: %s)", args.aoi, phases)

    # Phase 1: Download and composite
    if 1 in phases and not args.skip_download:
        phase1_mosaics(cfg)

    # Load mosaics
    mosaics = load_all_mosaics(cfg)
    if not mosaics:
        logger.error("No mosaics found. Run Phase 1 first.")
        return

    # Phase 2+3: Encode all representations
    encode_result = None
    if 2 in phases:
        encode_result = phase2_encode(cfg, mosaics)

    # Phase 3: Quality verification
    if 3 in phases:
        if encode_result is None:
            # Need grid info even if we skipped encoding
            from spacetime.chunk import make_chunk_grid
            from spacetime.mosaic import load_mosaic

            first_path = DATA / "mosaics" / cfg["name"] / f"{sorted(mosaics.keys())[0]}.npz"
            m = load_mosaic(first_path)
            grid = make_chunk_grid(
                m["bands"].shape[1],
                m["bands"].shape[2],
                m["transform"],
                m["epsg"],
                cfg["chunk_size"],
            )
            encode_result = {"grid": grid, "mosaic_meta": m, "metrics": {}}
        phase3_quality(cfg, mosaics, encode_result)

    # Phase 4: Access simulation
    if 4 in phases:
        if encode_result is None:
            from spacetime.chunk import make_chunk_grid
            from spacetime.mosaic import load_mosaic

            first_path = DATA / "mosaics" / cfg["name"] / f"{sorted(mosaics.keys())[0]}.npz"
            m = load_mosaic(first_path)
            grid = make_chunk_grid(
                m["bands"].shape[1],
                m["bands"].shape[2],
                m["transform"],
                m["epsg"],
                cfg["chunk_size"],
            )
            encode_result = {"grid": grid, "mosaic_meta": m, "metrics": {}}
        phase4_access_sim(cfg, mosaics, encode_result)

    # Phase 5: Report
    if 5 in phases:
        phase5_report(cfg)


if __name__ == "__main__":
    main()
