"""Timing test: one-chunk full-history ingest with pyramid.

Pulls all available Sentinel-2 months for a tiny AOI (~5km × 5km = 1 chunk),
encodes B2_chunked, builds pyramid levels, and reports timing per phase.

Usage:
    uv run python scripts/timing_test.py
    uv run python scripts/timing_test.py --skip-download   # re-encode only
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("timing_test")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# Tiny AOI: ~5km × 5km centered in the Iowa AOI
# At 42°N: 1° lat ≈ 111km, 1° lon ≈ 82km
# 5.12km → ~0.046° lat, ~0.063° lon
BBOX = (-93.71, 42.05, -93.65, 42.10)  # ~6.6km E-W × ~5.5km N-S
EPSG = 32615  # UTM 15N

# Full Sentinel-2 archive
START = "2015-07-01"
END = "2026-04-01"

CHUNK_SIZE = 512
AOI_NAME = "timing_test"


def phase1_stac_search() -> dict:
    """STAC search only — measure catalog query time."""
    from spacetime.catalog import search_scenes_by_month

    logger.info("Phase 1a: STAC search %s to %s", START, END)
    t0 = time.time()
    by_month = search_scenes_by_month(BBOX, START, END, max_cloud_pct=80.0)
    elapsed = time.time() - t0

    total_scenes = sum(len(v) for v in by_month.values())
    logger.info(
        "STAC search: %d months, %d total scenes, %.1fs",
        len(by_month),
        total_scenes,
        elapsed,
    )
    return {
        "by_month": by_month,
        "n_months": len(by_month),
        "n_scenes": total_scenes,
        "stac_search_s": elapsed,
    }


def phase1_mosaics(by_month: dict) -> tuple[dict[str, Path], list[dict]]:
    """Download and composite monthly mosaics, timing each month."""

    out_dir = DATA / "mosaics" / AOI_NAME
    per_month_times: list[dict] = []

    logger.info("Phase 1b: Building %d monthly mosaics", len(by_month))
    t0_total = time.time()

    # We'll time each month individually by wrapping build_monthly_mosaics
    # But to get per-month timing, we process one month at a time
    outputs: dict[str, Path] = {}
    months = sorted(by_month.keys())
    prev_composite = None

    from rasterio.crs import CRS

    from spacetime.mosaic import (
        compute_target_grid,
        load_mosaic,
        monthly_composite,
    )

    dst_crs = CRS.from_epsg(EPSG)
    dst_transform, dst_height, dst_width = compute_target_grid(BBOX, EPSG)

    for month_key in months:
        out_path = out_dir / f"{month_key}.npz"

        # Resume support
        if out_path.exists():
            logger.info("Skipping %s (exists)", month_key)
            data = load_mosaic(out_path)
            prev_composite = data["bands"]
            outputs[month_key] = out_path
            per_month_times.append(
                {
                    "month": month_key,
                    "n_scenes": len(by_month[month_key]),
                    "elapsed_s": 0,
                    "skipped": True,
                }
            )
            continue

        scenes = by_month[month_key]
        t0 = time.time()

        composite, coverage = monthly_composite(
            scenes, dst_transform, dst_crs, dst_height, dst_width
        )

        # Carry-forward
        if prev_composite is not None:
            gap_mask = np.all(composite == 0, axis=0)
            if gap_mask.any():
                composite[:, gap_mask] = prev_composite[:, gap_mask]

        prev_composite = composite.copy()

        out_dir.mkdir(parents=True, exist_ok=True)
        from spacetime.catalog import REQUIRED_BANDS

        np.savez_compressed(
            out_path,
            bands=composite,
            coverage=coverage,
            transform=np.array(list(dst_transform)[:6]),
            epsg=np.array(EPSG),
            band_names=np.array(list(REQUIRED_BANDS)),
        )

        elapsed = time.time() - t0
        outputs[month_key] = out_path
        per_month_times.append(
            {
                "month": month_key,
                "n_scenes": len(scenes),
                "elapsed_s": round(elapsed, 1),
                "skipped": False,
            }
        )
        logger.info("  %s: %d scenes, %.1fs", month_key, len(scenes), elapsed)

    total_elapsed = time.time() - t0_total
    logger.info("Phase 1b done: %d months, %.0fs total", len(outputs), total_elapsed)
    return outputs, per_month_times


def phase2_encode(mosaics: dict[str, np.ndarray]) -> dict:
    """Encode B2_chunked and build pyramid levels."""
    from spacetime.chunk import make_chunk_grid
    from spacetime.encode.baseline_b import encode_b2_chunked_time
    from spacetime.pyramid import build_pyramid

    sample = next(iter(mosaics.values()))
    first_month = sorted(mosaics.keys())[0]
    first_path = DATA / "mosaics" / AOI_NAME / f"{first_month}.npz"

    from spacetime.mosaic import load_mosaic

    m = load_mosaic(first_path)
    grid = make_chunk_grid(sample.shape[1], sample.shape[2], m["transform"], m["epsg"], CHUNK_SIZE)

    store_dir = DATA / "stores" / AOI_NAME

    # B2_chunked
    logger.info(
        "Phase 2a: Encoding B2_chunked (%d months, %d chunks)", len(mosaics), grid.n_chunks
    )
    t0 = time.time()
    metrics = encode_b2_chunked_time(mosaics, grid, store_dir)
    encode_s = time.time() - t0
    logger.info("B2_chunked: %.2f MB, %.1fs", metrics["total_bytes"] / 1e6, encode_s)

    # Pyramid
    logger.info("Phase 2b: Building pyramid levels")
    t0 = time.time()
    pyramid_meta = build_pyramid(mosaics, grid, store_dir, CHUNK_SIZE)
    pyramid_s = time.time() - t0
    logger.info("Pyramid: %d levels, %.1fs", len(pyramid_meta), pyramid_s)

    return {
        "encode_s": encode_s,
        "pyramid_s": pyramid_s,
        "b2_chunked_bytes": metrics["total_bytes"],
        "n_chunks": grid.n_chunks,
        "n_pyramid_levels": len(pyramid_meta),
        "pyramid_meta": pyramid_meta,
        "grid_rows": grid.n_rows,
        "grid_cols": grid.n_cols,
        "mosaic_h": sample.shape[1],
        "mosaic_w": sample.shape[2],
    }


def load_all_mosaics() -> dict[str, np.ndarray]:
    """Load all monthly mosaics from disk."""
    from spacetime.mosaic import load_mosaic

    mosaic_dir = DATA / "mosaics" / AOI_NAME
    mosaics = {}
    for npz in sorted(mosaic_dir.glob("*.npz")):
        m = load_mosaic(npz)
        mosaics[npz.stem] = m["bands"]
    logger.info("Loaded %d mosaics from %s", len(mosaics), mosaic_dir)
    return mosaics


def extrapolate(per_month_times: list[dict], encode_result: dict) -> dict:
    """Extrapolate timing to larger AOIs."""
    processed = [m for m in per_month_times if not m.get("skipped")]
    if not processed:
        return {}

    avg_s_per_month = np.mean([m["elapsed_s"] for m in processed])
    n_months = len(per_month_times)

    # Current AOI dimensions
    mosaic_h = encode_result.get("mosaic_h", 0)
    mosaic_w = encode_result.get("mosaic_w", 0)
    current_pixels = mosaic_h * mosaic_w

    # 25km AOI: ~2500 × 2500 px at 10m
    full_aoi_pixels = 2500 * 2500
    scale_factor = full_aoi_pixels / max(current_pixels, 1)

    return {
        "avg_s_per_month_small": round(avg_s_per_month, 1),
        "total_phase1_small_s": round(avg_s_per_month * n_months, 0),
        "n_months": n_months,
        "scale_factor_to_25km": round(scale_factor, 1),
        "estimated_phase1_25km_s": round(avg_s_per_month * n_months * scale_factor, 0),
        "estimated_phase1_25km_min": round(avg_s_per_month * n_months * scale_factor / 60, 1),
        "mosaic_pixels": f"{mosaic_h}×{mosaic_w}",
    }


def main():
    parser = argparse.ArgumentParser(description="Timing test for full-history ingest")
    parser.add_argument(
        "--skip-download", action="store_true", help="Skip Phase 1 (use existing mosaics)"
    )
    args = parser.parse_args()

    report = {"aoi": AOI_NAME, "bbox": BBOX, "epsg": EPSG, "start": START, "end": END}
    t0_total = time.time()

    if not args.skip_download:
        # Phase 1a: STAC search
        stac_result = phase1_stac_search()
        report["stac_search_s"] = stac_result["stac_search_s"]
        report["n_months_available"] = stac_result["n_months"]
        report["n_scenes_total"] = stac_result["n_scenes"]

        # Phase 1b: Download + composite
        outputs, per_month_times = phase1_mosaics(stac_result["by_month"])
        report["per_month_times"] = per_month_times
    else:
        per_month_times = []

    # Load all mosaics
    mosaics = load_all_mosaics()
    if not mosaics:
        logger.error("No mosaics found. Run without --skip-download first.")
        return

    report["n_months_processed"] = len(mosaics)

    # Phase 2: Encode + pyramid
    encode_result = phase2_encode(mosaics)
    report.update(encode_result)

    # Extrapolation
    if per_month_times:
        report["extrapolation"] = extrapolate(per_month_times, encode_result)

    report["total_elapsed_s"] = round(time.time() - t0_total, 1)

    # Save report
    report_path = DATA / "reports" / "timing_test.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    # Clean non-serializable keys
    clean = {k: v for k, v in report.items() if k != "pyramid_meta"}
    with open(report_path, "w") as f:
        json.dump(clean, f, indent=2, default=str)

    # Print summary
    print("\n" + "=" * 60)
    print("TIMING TEST RESULTS")
    print("=" * 60)
    print(f"AOI:          {BBOX}")
    print(
        f"Mosaic:       {encode_result.get('mosaic_h', '?')}×{encode_result.get('mosaic_w', '?')} px"
    )
    print(
        f"Chunks:       {encode_result.get('grid_rows', '?')}×{encode_result.get('grid_cols', '?')} = {encode_result.get('n_chunks', '?')}"
    )
    print(f"Months:       {len(mosaics)}")
    print(f"Pyramid:      {encode_result.get('n_pyramid_levels', 0)} levels")
    print()

    if "stac_search_s" in report:
        print(f"STAC search:  {report['stac_search_s']:.1f}s")

    if per_month_times:
        processed = [m for m in per_month_times if not m.get("skipped")]
        if processed:
            avg = np.mean([m["elapsed_s"] for m in processed])
            slowest = max(processed, key=lambda m: m["elapsed_s"])
            fastest = min(processed, key=lambda m: m["elapsed_s"])
            print(f"Phase 1 avg:  {avg:.1f}s/month")
            print(
                f"  fastest:    {fastest['month']} ({fastest['elapsed_s']:.1f}s, {fastest['n_scenes']} scenes)"
            )
            print(
                f"  slowest:    {slowest['month']} ({slowest['elapsed_s']:.1f}s, {slowest['n_scenes']} scenes)"
            )
            skipped = len(per_month_times) - len(processed)
            if skipped:
                print(f"  skipped:    {skipped} (already existed)")

    print(f"Encode:       {encode_result['encode_s']:.1f}s")
    print(f"Pyramid:      {encode_result['pyramid_s']:.1f}s")
    print(f"B2_chunked:   {encode_result['b2_chunked_bytes'] / 1e6:.2f} MB")
    print(
        f"Total:        {report['total_elapsed_s']:.1f}s ({report['total_elapsed_s'] / 60:.1f} min)"
    )

    if "extrapolation" in report:
        ex = report["extrapolation"]
        print()
        print("--- Extrapolation to 25km × 25km AOI ---")
        print(f"Scale factor: {ex['scale_factor_to_25km']}x (pixel area)")
        print(f"Est Phase 1:  {ex['estimated_phase1_25km_min']} min")
        print("Note: network time doesn't scale linearly with pixels —")
        print("      actual Phase 1 is ~same since COGs cover the same tiles.")
        print("      Compute (composite) scales with pixels but is <10% of time.")

    print(f"\nFull report: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
