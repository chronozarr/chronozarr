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
    # kf=1 is effectively "all keyframes" = no delta advantage baseline
    for kf_interval in [1, 3, 6, 12]:
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
    """Verify reconstruction quality, change analysis, and QC outputs."""
    from spacetime.bench import init_db, record_change_analysis, record_quality
    from spacetime.chunk import extract_chunk
    from spacetime.encode import experimental
    from spacetime.qc import save_comparison_panel, save_delta_stats_plot

    aoi_name = cfg["name"]
    grid = encode_result["grid"]
    store_root = DATA / "stores" / aoi_name
    qc_dir = DATA / "reports" / "qc" / aoi_name
    db = init_db(DATA / "reports" / "bench.duckdb")

    months = sorted(mosaics.keys())

    # --- Change analysis: all chunks, consecutive months ---
    logger.info("Running change analysis...")
    for r, c in grid.chunk_ids:
        cid = grid.chunk_id_str(r, c)
        for i in range(1, len(months)):
            prev = extract_chunk(mosaics[months[i - 1]], grid, r, c)
            curr = extract_chunk(mosaics[months[i]], grid, r, c)
            record_change_analysis(db, aoi_name, cid, months[i - 1], months[i], prev, curr)

    # --- Reconstruction quality ---
    sample_chunks = [(0, 0), (grid.n_rows // 2, grid.n_cols // 2)]
    sample_months = [
        months[0],
        months[len(months) // 4],
        months[len(months) // 2],
        months[3 * len(months) // 4],
        months[-1],
    ]

    for kf_interval in [1, 3, 6, 12]:
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
    """Generate summary reports framed around three hypotheses."""
    from spacetime.bench import init_db

    aoi = cfg["name"]
    db = init_db(DATA / "reports" / "bench.duckdb")

    # ===== H1: Product Unification =====
    print("\n" + "=" * 70)
    print(f"RESULTS FOR: {aoi}")
    print("=" * 70)

    result = db.execute(
        """
        SELECT representation, variant, total_bytes, n_months, n_chunks
        FROM storage_metrics WHERE aoi = ?
        ORDER BY total_bytes
    """,
        [aoi],
    ).fetchall()

    if result:
        print("\n--- H1: Product Unification (A vs B) ---")
        for row in result:
            rep, var, total, _nm, _nc = row
            label = f"{rep}/{var}" if var else rep
            print(f"  {label:<30} {total / 1e6:>10.2f} MB")

    # ===== H2: Temporal Encoding vs B2_chunked =====
    b2c_row = db.execute(
        """
        SELECT total_bytes FROM storage_metrics
        WHERE aoi = ? AND variant = 'b2_chunked'
    """,
        [aoi],
    ).fetchone()

    if b2c_row:
        b2c_bytes = b2c_row[0]
        print(f"\n--- H2: Temporal Encoding (vs B2_chunked = {b2c_bytes / 1e6:.2f} MB) ---")
        exp_rows = db.execute(
            """
            SELECT variant, keyframe_interval, total_bytes
            FROM storage_metrics
            WHERE aoi = ? AND representation = 'experimental'
            ORDER BY keyframe_interval
        """,
            [aoi],
        ).fetchall()
        for _var, kf, total in exp_rows:
            ratio = b2c_bytes / max(total, 1)
            savings = (1 - total / max(b2c_bytes, 1)) * 100
            print(f"  kf={kf:>2}: {total / 1e6:>8.2f} MB  ({ratio:.2f}x B2c, {savings:+.1f}%)")

    # ===== H3: Access Pattern =====
    result = db.execute(
        """
        SELECT representation, variant, pattern, product,
               AVG(bytes_fetched) as avg_bytes,
               AVG(decode_time_ms) as avg_ms
        FROM access_metrics WHERE aoi = ?
        GROUP BY representation, variant, pattern, product
        ORDER BY pattern, avg_bytes
    """,
        [aoi],
    ).fetchall()

    if result:
        print("\n--- H3: Access Patterns ---")
        hdr = f"  {'Rep/Var':<25} {'Pattern':<15} {'Product':<12} {'Bytes':>12} {'ms':>8}"
        print(hdr)
        print(f"  {'-' * 75}")
        for row in result:
            rep, var, pat, prod, ab, ams = row
            label = f"{rep}/{var}" if var else rep
            print(f"  {label:<25} {pat:<15} {prod:<12} {ab:>12,.0f} {ams:>8.1f}")

    # ===== Incremental Time-Step Cost =====
    inc_rows = db.execute(
        """
        SELECT variant, is_keyframe_boundary,
               AVG(bytes_marginal) as avg_marginal,
               AVG(decode_time_ms) as avg_ms
        FROM incremental_cost WHERE aoi = ?
        GROUP BY variant, is_keyframe_boundary
        ORDER BY variant, is_keyframe_boundary
    """,
        [aoi],
    ).fetchall()

    if inc_rows:
        print("\n--- Incremental Time-Step Cost ---")
        for var, is_kf, avg_m, avg_ms in inc_rows:
            tag = "keyframe boundary" if is_kf else "delta step"
            print(f"  {var:<20} {tag:<20} avg={avg_m:>10,.0f} bytes  {avg_ms:>6.1f}ms")

    # ===== Change Analysis Summary =====
    change_rows = db.execute(
        """
        SELECT
            AVG(pct_changed) as avg_changed,
            AVG(mean_abs_delta) as avg_delta,
            AVG(delta_entropy) as avg_entropy,
            MIN(pct_changed) as min_changed,
            MAX(pct_changed) as max_changed
        FROM change_analysis WHERE aoi = ?
    """,
        [aoi],
    ).fetchone()

    if change_rows and change_rows[0] is not None:
        avg_ch, avg_d, avg_e, min_ch, max_ch = change_rows
        print("\n--- Temporal Change Profile ---")
        print(f"  Avg changed pixels/month: {avg_ch:.1f}%")
        print(f"  Range: {min_ch:.1f}% - {max_ch:.1f}%")
        print(f"  Avg |delta|: {avg_d:.1f}")
        print(f"  Avg delta entropy ratio: {avg_e:.3f}")

    # ===== Reconstruction Quality =====
    q_rows = db.execute(
        """
        SELECT variant, AVG(psnr), AVG(ssim), MAX(max_abs_error)
        FROM quality_metrics WHERE aoi = ?
        GROUP BY variant ORDER BY variant
    """,
        [aoi],
    ).fetchall()

    if q_rows:
        print("\n--- Reconstruction Quality ---")
        for var, psnr, ssim_val, worst in q_rows:
            p = "inf" if psnr == float("inf") else f"{psnr:.1f}"
            print(f"  {var:<25} PSNR={p:>6}  SSIM={ssim_val:.6f}  worst={worst}")

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
