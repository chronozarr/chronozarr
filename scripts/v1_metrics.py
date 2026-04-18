"""Compute storage metrics for a v1 ChronoFabric store.

Usage:
    uv run python scripts/v1_metrics.py data/stores/sahara_tamanrasset/v1
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def chunk_file_sizes(store_dir: Path, lod: int) -> list[tuple[str, int, int]]:
    """Return (chunk_id, month_index, file_bytes) for all chunks at a LOD."""
    chunks_dir = store_dir / "lod" / str(lod) / "chunks"
    if not chunks_dir.is_dir():
        return []
    results = []
    for chunk_dir in sorted(chunks_dir.iterdir()):
        if not chunk_dir.is_dir():
            continue
        zarr_dir = chunk_dir / "stack.zarr"
        if not zarr_dir.is_dir():
            continue
        for chunk_file in sorted(zarr_dir.iterdir()):
            if chunk_file.name.startswith("."):
                continue
            parts = chunk_file.name.split(".")
            if len(parts) >= 1 and parts[0].isdigit():
                month_idx = int(parts[0])
                results.append((chunk_dir.name, month_idx, chunk_file.stat().st_size))
    return results


def analyze_store(store_dir: Path) -> None:
    store_dir = Path(store_dir)
    manifest_path = store_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest.json in {store_dir}")
        sys.exit(1)

    with open(manifest_path) as f:
        manifest = json.load(f)

    months = manifest["months"]
    n_months = len(months)
    anchor_indices = set(manifest["temporal"]["anchor_indices"])
    mosaic_h = manifest["mosaic_height"]
    mosaic_w = manifest["mosaic_width"]
    lod_factor = manifest["lod_factor"]

    print(f"Store: {store_dir}")
    print(f"Months: {n_months} ({months[0]} .. {months[-1]})")
    print(f"Mosaic: {mosaic_w} x {mosaic_h} px at LOD 0")
    print(f"Anchors: {len(anchor_indices)} / {n_months} months")
    print(f"Encoding: {manifest['temporal']['encoding']}")
    print(f"Compressor: {manifest['compressor']} (level {manifest['compressor_level']})")
    print()

    total_store_bytes = 0

    for lod_meta in manifest["lods"]:
        lod = lod_meta["level"]
        grid_rows = lod_meta["grid_rows"]
        grid_cols = lod_meta["grid_cols"]
        res_m = lod_meta["resolution_m"]
        factor = lod_factor**lod

        lod_mosaic_w = -(-mosaic_w // factor)  # ceil div
        lod_mosaic_h = -(-mosaic_h // factor)
        lod_pixels = lod_mosaic_w * lod_mosaic_h

        files = chunk_file_sizes(store_dir, lod)
        if not files:
            continue

        sizes = [f[2] for f in files]
        anchor_sizes = [f[2] for f in files if f[1] in anchor_indices]
        delta_sizes = [f[2] for f in files if f[1] not in anchor_indices]

        lod_total = sum(sizes)
        total_store_bytes += lod_total

        n_chunks = grid_rows * grid_cols
        pixel_months = lod_pixels * n_months

        print(f"LOD {lod} ({res_m}m, {grid_rows}x{grid_cols} = {n_chunks} chunks)")
        print(f"  Total:         {lod_total / 1e6:>8.2f} MB")
        print(f"  Chunks/month:  {n_chunks:>8d}")
        print(f"  Pixels:        {lod_pixels:>8,}")
        print(f"  B/px-month:    {lod_total / pixel_months:>8.2f}")
        if anchor_sizes:
            avg_a = sum(anchor_sizes) / len(anchor_sizes)
            print(f"  Anchor avg:    {avg_a / 1024:>8.1f} KB  ({len(anchor_sizes)} files)")
        if delta_sizes:
            avg_d = sum(delta_sizes) / len(delta_sizes)
            print(f"  Delta avg:     {avg_d / 1024:>8.1f} KB  ({len(delta_sizes)} files)")
            if anchor_sizes:
                avg_a = sum(anchor_sizes) / len(anchor_sizes)
                print(f"  Delta/anchor:  {avg_d / avg_a:>8.1%}")
        print(f"  Chunk min:     {min(sizes) / 1024:>8.1f} KB")
        print(f"  Chunk max:     {max(sizes) / 1024:>8.1f} KB")
        print(f"  Chunk avg:     {sum(sizes) / len(sizes) / 1024:>8.1f} KB")
        print()

    # Grand totals
    total_pixels_lod0 = mosaic_w * mosaic_h
    pixel_months_lod0 = total_pixels_lod0 * n_months

    print("TOTALS")
    print(f"  Store size:    {total_store_bytes / 1e6:>8.2f} MB")
    print(f"  LOD 0 pixels:  {total_pixels_lod0:>8,}")
    lod0_bytes = sum(s for _, _, s in chunk_file_sizes(store_dir, 0))
    print(f"  B/px-month (LOD 0 only): {lod0_bytes / pixel_months_lod0:.2f}")
    print(f"  B/px-month (all LODs):   {total_store_bytes / pixel_months_lod0:.2f}")

    # Manifest metadata
    cells = manifest.get("cells", {})
    if cells:
        vols = [c["volatility"] for c in cells.values()]
        print("\nVolatility (LOD 0 cells)")
        print(f"  Min:  {min(vols):.4f}")
        print(f"  Max:  {max(vols):.4f}")
        print(f"  Mean: {sum(vols) / len(vols):.4f}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <store_dir>")
        sys.exit(1)
    analyze_store(Path(sys.argv[1]))
