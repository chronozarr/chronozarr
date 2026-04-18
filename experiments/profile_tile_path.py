"""Profile the tile-serving path from single tiles to session patterns.

Measures the production load -> render -> encode stages using the same helper
that backs the API route.

Usage:
    uv run --extra dev --extra serve python experiments/profile_tile_path.py --synthetic
    uv run --extra dev --extra serve python experiments/profile_tile_path.py \
      --stores-root /abs/path/to/stores --aoi sahara_tamanrasset
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import zarr

from spacetime.api.v1 import TileProfile, profile_tile_render

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STORES_ROOT = ROOT / "data" / "stores"


@dataclass(frozen=True)
class TileRequest:
    """One tile request in a benchmark sequence."""

    chunk_id: str
    month_index: int
    product: str


def _parse_csv(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",")]
    return [item for item in items if item]


def _chunk_id(row: int, col: int) -> str:
    return f"r{row:03d}_c{col:03d}"


def _chunk_window_ids(
    n_rows: int,
    n_cols: int,
    center_row: int,
    center_col: int,
    window_rows: int,
    window_cols: int,
) -> list[str]:
    """Return chunk ids for an explicit window centered near (row, col)."""
    if window_rows < 1 or window_cols < 1:
        raise ValueError("window_rows and window_cols must both be >= 1")

    start_row = center_row - window_rows // 2
    start_col = center_col - window_cols // 2
    start_row = min(max(start_row, 0), max(n_rows - window_rows, 0))
    start_col = min(max(start_col, 0), max(n_cols - window_cols, 0))
    stop_row = min(start_row + window_rows, n_rows)
    stop_col = min(start_col + window_cols, n_cols)

    chunk_ids = []
    for row in range(start_row, stop_row):
        for col in range(start_col, stop_col):
            chunk_ids.append(_chunk_id(row, col))
    return chunk_ids


def _write_synthetic_store(
    stores_root: Path,
    *,
    chunk_size: int,
    chunk_dim: int,
    n_months: int,
    grid_size: int,
) -> tuple[str, str, str]:
    """Create a temporary benchmark store with a configurable chunk grid."""
    aoi_name = f"synthetic_profile_cs{chunk_size}"
    store_dir = stores_root / aoi_name / f"cs{chunk_size}"
    store_dir.mkdir(parents=True, exist_ok=True)

    months = [f"2024-{month:02d}" for month in range(1, n_months + 1)]
    manifest = {
        "aoi": aoi_name,
        "label": f"Synthetic Profile AOI cs{chunk_size}",
        "chunk_size": chunk_size,
        "n_rows": grid_size,
        "n_cols": grid_size,
        "months": months,
        "epsg": 32631,
        "transform": [10.0, 0.0, 500000.0, 0.0, -10.0, 2600000.0],
        "mosaic_height": grid_size * chunk_dim,
        "mosaic_width": grid_size * chunk_dim,
        "source": "Synthetic benchmark data",
        "composite_method": "synthetic seasonal gradient",
    }
    (store_dir / "manifest.json").write_text(json.dumps(manifest))

    y, x = np.mgrid[0:chunk_dim, 0:chunk_dim].astype(np.float32)
    x_norm = x / max(chunk_dim - 1, 1)
    y_norm = y / max(chunk_dim - 1, 1)

    for row in range(grid_size):
        for col in range(grid_size):
            chunk_dir = store_dir / _chunk_id(row, col)
            chunk_dir.mkdir(parents=True, exist_ok=True)
            row_offset = 180 * row
            col_offset = 140 * col

            blue = 700 + 1000 * x_norm + row_offset
            green = 900 + 1400 * y_norm + col_offset
            red = 1100 + 1200 * (1.0 - x_norm) + row_offset
            nir = 1800 + 2600 * (x_norm * y_norm) + row_offset + col_offset

            data = np.zeros((n_months, 4, chunk_dim, chunk_dim), dtype=np.uint16)
            for month_index in range(n_months):
                seasonal = 300 * np.sin((month_index / max(n_months, 1)) * 2 * np.pi)
                data[month_index, 0] = np.clip(blue + 0.3 * seasonal, 0, 10000).astype(np.uint16)
                data[month_index, 1] = np.clip(green + 0.5 * seasonal, 0, 10000).astype(np.uint16)
                data[month_index, 2] = np.clip(red - 0.4 * seasonal, 0, 10000).astype(np.uint16)
                data[month_index, 3] = np.clip(nir + seasonal, 0, 10000).astype(np.uint16)

            z = zarr.open(
                str(chunk_dir / "stack.zarr"),
                mode="w",
                shape=data.shape,
                chunks=(1, 4, chunk_dim, chunk_dim),
                dtype="u2",
            )
            z[:] = data

    center = grid_size // 2
    return (aoi_name, months[0], _chunk_id(center, center))


def _discover_aois(stores_root: Path) -> dict[str, dict]:
    """Discover AOIs from a stores directory using the app's loader."""
    import spacetime.serve as serve_module

    previous = serve_module.STORES_ROOT
    serve_module.STORES_ROOT = stores_root
    try:
        return serve_module._discover_aois()
    finally:
        serve_module.STORES_ROOT = previous


def _pick_target(
    catalog: dict[str, dict],
    *,
    aoi: str | None,
    month: str | None,
    chunk_id: str | None,
) -> tuple[str, dict, str, str]:
    """Select AOI, month, and chunk to profile."""
    if not catalog:
        raise SystemExit("No AOI stores found. Pass --synthetic or point --stores-root at data.")

    if aoi is None:
        aoi_key = sorted(catalog)[0]
    else:
        aoi_key = aoi if aoi in catalog else f"{aoi}/cs512"
        if aoi_key not in catalog:
            matches = [key for key, meta in catalog.items() if meta["aoi"] == aoi]
            if len(matches) == 1:
                aoi_key = matches[0]
            else:
                raise SystemExit(f"AOI '{aoi}' not found in {sorted(catalog)}")

    meta = catalog[aoi_key]
    month_value = month or meta["months"][0]
    if month_value not in meta["months"]:
        raise SystemExit(
            f"Month '{month_value}' not available for {meta['aoi']}: {meta['months']}"
        )

    chunk_value = chunk_id or meta["chunk_ids"][0]
    if chunk_value not in meta["chunk_ids"]:
        raise SystemExit(
            f"Chunk '{chunk_value}' not available for {meta['aoi']}: {meta['chunk_ids']}"
        )

    return (aoi_key, meta, month_value, chunk_value)


def _run_sequence(
    meta: dict,
    requests: list[TileRequest],
    *,
    fmt: str,
    level: int,
    clear_cache_first: bool,
) -> list[TileProfile]:
    """Run a tile request sequence against one AOI store."""
    profiles = []
    for index, request in enumerate(requests):
        profiles.append(
            profile_tile_render(
                meta["store_dir"],
                request.chunk_id,
                request.month_index,
                request.product,
                fmt=fmt,
                level=level,
                clear_cache=clear_cache_first and index == 0,
            )
        )
    return profiles


def _sequence_summary(profiles: list[TileProfile]) -> dict[str, float]:
    """Summarize one benchmark sequence."""
    if not profiles:
        raise ValueError("Cannot summarize an empty profile list")

    cache_bytes_est = [profile.cache_entries * profile.band_bytes for profile in profiles]
    summary = {
        "requests": len(profiles),
        "cache_hit_rate": sum(profile.cache_hit for profile in profiles) / len(profiles),
        "image_bytes_mean": statistics.fmean(len(profile.img_bytes) for profile in profiles),
        "image_bytes_total": float(sum(len(profile.img_bytes) for profile in profiles)),
        "band_bytes_per_tile": float(profiles[0].band_bytes),
        "cache_entries_peak": float(max(profile.cache_entries for profile in profiles)),
        "cache_bytes_est_peak": float(max(cache_bytes_est)),
    }
    for field in ("zarr_ms", "render_ms", "encode_ms", "total_ms"):
        values = [getattr(profile, field) for profile in profiles]
        summary[f"{field}_mean"] = statistics.fmean(values)
        summary[f"{field}_median"] = statistics.median(values)
        summary[f"{field}_total"] = float(sum(values))
    return summary


def _single_tile_sequences(
    meta: dict, month_index: int, chunk_id: str, product: str
) -> dict[str, list[TileRequest]]:
    request = TileRequest(chunk_id=chunk_id, month_index=month_index, product=product)
    return {"tile_cold": [request], "tile_warm": [request]}


def _session_sequences(
    meta: dict,
    *,
    month_index: int,
    product: str,
    products: list[str],
    time_steps: int,
    reference_chunk_size: int,
    reference_viewport_chunks: int,
) -> dict[str, tuple[list[TileRequest], list[TileRequest] | None]]:
    """Build session-shaped tile request sequences.

    Returns:
        Mapping of scenario -> (target_sequence, optional primer_sequence)
    """
    center_row = meta["n_rows"] // 2
    center_col = meta["n_cols"] // 2
    viewport_footprint_px = reference_chunk_size * reference_viewport_chunks
    viewport_chunks = max(1, int(np.ceil(viewport_footprint_px / meta["chunk_size"])))
    pan_shift_chunks = max(1, int(np.ceil(reference_chunk_size / meta["chunk_size"])))

    viewport = _chunk_window_ids(
        meta["n_rows"],
        meta["n_cols"],
        center_row,
        center_col,
        viewport_chunks,
        viewport_chunks,
    )
    pan_viewport = _chunk_window_ids(
        meta["n_rows"],
        meta["n_cols"],
        center_row,
        min(center_col + pan_shift_chunks, meta["n_cols"] - 1),
        viewport_chunks,
        viewport_chunks,
    )

    cold_viewport = [
        TileRequest(chunk_id=chunk_id, month_index=month_index, product=product)
        for chunk_id in viewport
    ]
    pan_primer = cold_viewport
    pan_target = [
        TileRequest(chunk_id=chunk_id, month_index=month_index, product=product)
        for chunk_id in pan_viewport
    ]
    switch_target = [
        TileRequest(chunk_id=chunk_id, month_index=month_index, product=switch_product)
        for switch_product in products
        for chunk_id in viewport
    ]

    available_steps = min(time_steps, len(meta["months"]))
    time_target = [
        TileRequest(chunk_id=chunk_id, month_index=step, product=product)
        for step in range(available_steps)
        for chunk_id in viewport
    ]

    return {
        "cold_viewport": (cold_viewport, None),
        "pan": (pan_target, pan_primer),
        "product_switch": (switch_target, None),
        "time_scrub": (time_target, None),
    }


def _row(
    *,
    store_kind: str,
    aoi_key: str,
    chunk_size: int,
    scenario: str,
    mode: str,
    product: str,
    fmt: str,
    summary: dict[str, float] | None = None,
    skipped_reason: str | None = None,
) -> dict[str, str | float | int]:
    row: dict[str, str | float | int] = {
        "store_kind": store_kind,
        "aoi": aoi_key,
        "chunk_size": chunk_size,
        "scenario": scenario,
        "mode": mode,
        "product": product,
        "fmt": fmt,
        "status": "skipped" if skipped_reason else "ok",
        "skipped_reason": skipped_reason or "",
    }
    if summary is not None:
        row.update(summary)
    return row


def _print_rows(rows: list[dict[str, str | float | int]]) -> None:
    """Print a human-readable summary table."""
    print(
        "scenario        mode   product        fmt  hit% total_ms "
        "zarr_ms render_ms encode_ms cache_mb image_kb status"
    )
    print(
        "--------------- ------ -------------- ---- ---- -------- "
        "------- --------- --------- -------- -------- ------"
    )
    for row in rows:
        if row["status"] != "ok":
            print(
                f"{row['scenario']:<15} {row['mode']:<6} {row['product']:<14} "
                f"{row['fmt']:<4} {'-':>4} {'-':>8} {'-':>7} "
                f"{'-':>9} {'-':>9} {'-':>8} {'-':>8} {row['status']}"
            )
            continue

        cache_mb = float(row["cache_bytes_est_peak"]) / (1024 * 1024)
        image_kb = float(row["image_bytes_mean"]) / 1024
        print(
            f"{row['scenario']:<15} {row['mode']:<6} {row['product']:<14} "
            f"{row['fmt']:<4} "
            f"{float(row['cache_hit_rate']) * 100:>4.0f} "
            f"{float(row['total_ms_mean']):>8.2f} "
            f"{float(row['zarr_ms_mean']):>7.2f} "
            f"{float(row['render_ms_mean']):>9.2f} "
            f"{float(row['encode_ms_mean']):>9.2f} "
            f"{cache_mb:>8.2f} "
            f"{image_kb:>8.1f} "
            f"{row['status']}"
        )


def _write_json(rows: list[dict[str, str | float | int]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2))


def _write_csv(rows: list[dict[str, str | float | int]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = sorted({key for row in rows for key in row})
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stores-root", type=Path, default=DEFAULT_STORES_ROOT)
    parser.add_argument("--aoi", help="AOI id or AOI key (for example sahara_tamanrasset/cs512)")
    parser.add_argument("--month", help="Month label, for example 2024-01")
    parser.add_argument("--chunk-id", help="Chunk id, for example r000_c000")
    parser.add_argument("--products", default="true_color,ndvi,water")
    parser.add_argument("--formats", default="jpeg,png")
    parser.add_argument(
        "--patterns",
        default="tile_cold,tile_warm,cold_viewport,pan,time_scrub,product_switch",
    )
    parser.add_argument("--level", type=int, default=0)
    parser.add_argument("--cold-runs", type=int, default=5)
    parser.add_argument("--warm-runs", type=int, default=10)
    parser.add_argument("--time-steps", type=int, default=4)
    parser.add_argument(
        "--reference-viewport-chunks",
        type=int,
        default=3,
        help="Viewport width/height in reference-size chunks for equal-area session comparisons",
    )
    parser.add_argument(
        "--reference-chunk-size",
        type=int,
        help=(
            "Reference chunk size in pixels for equal-area session comparisons; "
            "defaults to largest target chunk size"
        ),
    )
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--synthetic-grid-size", type=int, default=3)
    parser.add_argument("--synthetic-dim", type=int)
    parser.add_argument("--synthetic-months", type=int, default=12)
    parser.add_argument("--synthetic-chunk-sizes", default="512")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--csv-out", type=Path)
    args = parser.parse_args()

    products = _parse_csv(args.products)
    formats = _parse_csv(args.formats)
    patterns = set(_parse_csv(args.patterns))
    synthetic_chunk_sizes = [int(value) for value in _parse_csv(args.synthetic_chunk_sizes)]
    if args.cold_runs < 1 or args.warm_runs < 1:
        raise SystemExit("cold and warm runs must both be >= 1")
    if args.time_steps < 1:
        raise SystemExit("--time-steps must be >= 1")
    if not products or not formats:
        raise SystemExit("products and formats must both be non-empty")

    rows: list[dict[str, str | float | int]] = []
    with tempfile.TemporaryDirectory(prefix="tileripper-profile-") as temp_dir:
        synthetic_root = Path(temp_dir) / "stores"
        synthetic_targets: dict[int, tuple[str, str, str]] = {}

        if args.synthetic:
            synthetic_root.mkdir(parents=True, exist_ok=True)
            for chunk_size in synthetic_chunk_sizes:
                chunk_dim = args.synthetic_dim or chunk_size
                synthetic_targets[chunk_size] = _write_synthetic_store(
                    synthetic_root,
                    chunk_size=chunk_size,
                    chunk_dim=chunk_dim,
                    n_months=args.synthetic_months,
                    grid_size=args.synthetic_grid_size,
                )
            stores_root = synthetic_root
        else:
            stores_root = args.stores_root

        catalog = _discover_aois(stores_root)
        targets: list[tuple[str, dict, str, str, str]] = []

        if args.synthetic:
            for _chunk_size, target in synthetic_targets.items():
                target_aoi, target_month, target_chunk = target
                aoi_key, meta, month, chunk_id = _pick_target(
                    catalog,
                    aoi=target_aoi,
                    month=target_month,
                    chunk_id=target_chunk,
                )
                targets.append(("synthetic", meta, aoi_key, month, chunk_id))
        else:
            aoi_key, meta, month, chunk_id = _pick_target(
                catalog,
                aoi=args.aoi,
                month=args.month,
                chunk_id=args.chunk_id,
            )
            targets.append(("real", meta, aoi_key, month, chunk_id))

        reference_chunk_size = args.reference_chunk_size
        if reference_chunk_size is None:
            reference_chunk_size = max(meta["chunk_size"] for _kind, meta, *_rest in targets)

        for store_kind, meta, aoi_key, month, chunk_id in targets:
            month_index = meta["months"].index(month)
            print()
            print(f"stores_root: {stores_root}")
            print(f"store_kind:  {store_kind}")
            print(f"aoi:         {aoi_key}")
            print(f"month:       {month}")
            print(f"chunk_id:    {chunk_id}")
            print(f"chunk_size:  {meta['chunk_size']}")
            print(f"grid:        {meta['n_rows']} x {meta['n_cols']}")
            print(
                "session_ref: "
                f"{args.reference_viewport_chunks} x {args.reference_viewport_chunks} "
                f"chunks @ cs{reference_chunk_size}"
            )

            session_sequences = _session_sequences(
                meta,
                month_index=month_index,
                product=products[0],
                products=products,
                time_steps=args.time_steps,
                reference_chunk_size=reference_chunk_size,
                reference_viewport_chunks=args.reference_viewport_chunks,
            )

            target_rows: list[dict[str, str | float | int]] = []
            for fmt in formats:
                for product in products:
                    tile_sequences = _single_tile_sequences(
                        meta,
                        month_index,
                        chunk_id,
                        product,
                    )
                    if "tile_cold" in patterns:
                        scenario = "tile_cold"
                        profiles = [
                            _run_sequence(
                                meta,
                                tile_sequences[scenario],
                                fmt=fmt,
                                level=args.level,
                                clear_cache_first=True,
                            )[0]
                            for _ in range(args.cold_runs)
                        ]
                        target_rows.append(
                            _row(
                                store_kind=store_kind,
                                aoi_key=aoi_key,
                                chunk_size=meta["chunk_size"],
                                scenario=scenario,
                                mode="cold",
                                product=product,
                                fmt=fmt,
                                summary=_sequence_summary(profiles),
                            )
                        )

                    if "tile_warm" in patterns:
                        scenario = "tile_warm"
                        _run_sequence(
                            meta,
                            tile_sequences[scenario],
                            fmt=fmt,
                            level=args.level,
                            clear_cache_first=True,
                        )
                        profiles = [
                            _run_sequence(
                                meta,
                                tile_sequences[scenario],
                                fmt=fmt,
                                level=args.level,
                                clear_cache_first=False,
                            )[0]
                            for _ in range(args.warm_runs)
                        ]
                        target_rows.append(
                            _row(
                                store_kind=store_kind,
                                aoi_key=aoi_key,
                                chunk_size=meta["chunk_size"],
                                scenario=scenario,
                                mode="warm",
                                product=product,
                                fmt=fmt,
                                summary=_sequence_summary(profiles),
                            )
                        )

                for scenario in ("cold_viewport", "pan", "time_scrub", "product_switch"):
                    if scenario not in patterns:
                        continue
                    target, primer = session_sequences[scenario]
                    product_label = (
                        products[0] if scenario != "product_switch" else ",".join(products)
                    )
                    repeats = args.cold_runs if scenario == "cold_viewport" else args.warm_runs
                    profiles = []
                    for _ in range(repeats):
                        if primer is not None:
                            _run_sequence(
                                meta,
                                primer,
                                fmt=fmt,
                                level=args.level,
                                clear_cache_first=True,
                            )
                            profiles.extend(
                                _run_sequence(
                                    meta,
                                    target,
                                    fmt=fmt,
                                    level=args.level,
                                    clear_cache_first=False,
                                )
                            )
                        else:
                            profiles.extend(
                                _run_sequence(
                                    meta,
                                    target,
                                    fmt=fmt,
                                    level=args.level,
                                    clear_cache_first=True,
                                )
                            )

                    target_rows.append(
                        _row(
                            store_kind=store_kind,
                            aoi_key=aoi_key,
                            chunk_size=meta["chunk_size"],
                            scenario=scenario,
                            mode="session",
                            product=product_label,
                            fmt=fmt,
                            summary=_sequence_summary(profiles),
                        )
                    )

            _print_rows(target_rows)
            rows.extend(target_rows)

    if args.json_out is not None:
        _write_json(rows, args.json_out)
    if args.csv_out is not None:
        _write_csv(rows, args.csv_out)


if __name__ == "__main__":
    main()
