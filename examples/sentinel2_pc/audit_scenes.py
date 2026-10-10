r"""Check that saved monthly mosaics include every scene that had a valid pixel.

A month's `coverage` is k/n per pixel: k scenes valid there, n scenes searched. A scene that
failed to read still counts in n but adds nothing to k. This script searches the scenes again,
reads only their SCL bands (a few MB each), predicts k per pixel and attributes any shortfall
against the saved k to specific scenes. A scene is reported missing when at least 99.9 % of its
valid pixels show a shortfall and removing it leaves at most 0.05 % of them below zero (a scene
inside another's footprint would). Pixels where SCL is valid but a band is 0 are excluded by
the pipeline but not predicted here; they appear as a small positive residual.

Usage:
    uv run python examples/sentinel2_pc/audit_scenes.py --aoi ucayali_santa_maria
    uv run python examples/sentinel2_pc/audit_scenes.py --aoi lake_mead --months 2024-01,2024-02
"""

from __future__ import annotations

import argparse
import calendar
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from catalog import SceneRef, search_scenes_by_month, sign_href
from ingest import DEFAULT_OUT_DIR, load_aoi_config
from mosaic import (
    _SCL_LUT,
    GDAL_ENV,
    READ_ATTEMPTS,
    Grid,
    compute_target_grid,
    forget_url,
    read_asset,
)
from rasterio.crs import CRS  # ty: ignore[unresolved-import]  (compiled module, no stubs)
from rasterio.warp import Resampling

logger = logging.getLogger("audit")

COVERED = 0.999
NEGATIVE = 0.0005


def scl_valid(scene: SceneRef, grid: Grid) -> np.ndarray:
    error: Exception | None = None
    for _ in range(READ_ATTEMPTS):
        url = sign_href(scene.scl_href)
        try:
            out = np.zeros((grid.height, grid.width), dtype=np.uint8)
            read_asset(url, grid, out, Resampling.nearest, GDAL_ENV)
            return _SCL_LUT[out]
        except Exception as e:  # retried like the pipeline's reads
            forget_url(url)
            error = e
    assert error is not None
    raise error


def attribute(masks: list[np.ndarray], shortfall: np.ndarray) -> list[int]:
    """Indices of scenes whose valid pixels explain the shortfall, largest first."""
    missing: list[int] = []
    while True:
        best = None
        for i, mask in enumerate(masks):
            if i in missing or not mask.any():
                continue
            covered = float((shortfall[mask] >= 1).mean())
            negative = float(((shortfall - mask)[mask] < 0).mean())
            explains = covered >= COVERED and negative <= NEGATIVE
            if explains and (best is None or mask.sum() > masks[best].sum()):
                best = i
        if best is None:
            return missing
        missing.append(best)
        shortfall = shortfall - masks[best]


def audit_month(path: Path, scenes: list[SceneRef], grid: Grid, pool) -> dict:
    with np.load(path) as data:
        coverage = data["coverage"]
    n = len(scenes)
    saved = np.rint(coverage * n).astype(np.int32)
    if not np.allclose(coverage * n, saved, atol=1e-3):
        return {
            "error": f"coverage is not k/{n}; the scene list changed since the month was built"
        }
    masks = list(pool.map(lambda s: scl_valid(s, grid), scenes))
    shortfall = np.sum(masks, axis=0, dtype=np.int32) - saved
    missing = attribute(masks, shortfall)
    remaining = shortfall - np.sum([masks[i] for i in missing], axis=0, dtype=np.int32)
    return {
        "n": n,
        "missing": [scenes[i].item_id for i in missing],
        "missing_valid_px": [int(masks[i].sum()) for i in missing],
        "unexplained_shortfall_px": int((remaining > 0).sum()),
        "excess_px": int((shortfall < 0).sum()),
        "scenes_without_valid_px": [
            s.item_id for s, m in zip(scenes, masks, strict=True) if not m.any()
        ],
        "small_scenes_with_shortfall": [
            (s.item_id, int(m.sum()), int((remaining[m] >= 1).sum()))
            for s, m in zip(scenes, masks, strict=True)
            if 0 < m.sum() < 1000
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--aoi", required=True)
    parser.add_argument("--months", help="comma-separated YYYY-MM (default: every saved month)")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--report", type=Path, help="JSON report path")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for name in ("httpx", "catalog", "mosaic"):
        logging.getLogger(name).setLevel(logging.WARNING)

    aoi = load_aoi_config(args.aoi)
    mosaic_dir = args.out_dir / "mosaics" / args.aoi
    paths = {p.stem: p for p in sorted(mosaic_dir.glob("*.npz"))}
    if args.months:
        paths = {m: paths[m] for m in args.months.split(",")}
    if not paths:
        raise SystemExit(f"no saved months in {mosaic_dir}")
    months = sorted(paths)
    year, month_number = (int(v) for v in months[-1].split("-"))
    last_day = calendar.monthrange(year, month_number)[1]
    by_month = search_scenes_by_month(
        tuple(aoi["bbox"]), f"{months[0]}-01", f"{months[-1]}-{last_day}", max_cloud_pct=80.0
    )
    transform, height, width = compute_target_grid(tuple(aoi["bbox"]), aoi["epsg"])
    grid = Grid(transform, CRS.from_epsg(aoi["epsg"]), height, width)

    results = {}
    with ThreadPoolExecutor(16) as pool:
        for month in months:
            result = audit_month(paths[month], by_month.get(month, []), grid, pool)
            results[month] = result
            if result.get("error") or result.get("missing"):
                logger.info("%s: %s", month, result)
    missing = sum(len(r.get("missing", [])) for r in results.values())
    errors = [m for m, r in results.items() if r.get("error")]
    logger.info(
        "%d months, %d scenes: %d missing scenes with valid pixels in %d months; "
        "%d months could not be checked; largest unexplained shortfall %d px of %d",
        len(results),
        sum(r.get("n", 0) for r in results.values()),
        missing,
        sum(bool(r.get("missing")) for r in results.values()),
        len(errors),
        max((r.get("unexplained_shortfall_px", 0) for r in results.values()), default=0),
        height * width,
    )
    report = args.report or mosaic_dir.parent.parent / "audit" / f"{args.aoi}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(results, indent=1))
    logger.info("Report: %s", report)


if __name__ == "__main__":
    main()
