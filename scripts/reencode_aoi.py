"""Re-encode one AOI's monthly mosaics as a chronozarr store.

Reads data/mosaics/<aoi>/YYYY-MM.npz (bands, transform, epsg, band_names) and writes
data/stores/<aoi>/chronozarr/. Prints wall time per phase.

Usage:
    uv run python scripts/reencode_aoi.py --aoi sahara_tamanrasset
    uv run python scripts/reencode_aoi.py --aoi sahara_tamanrasset --workers 1 --overwrite
"""

from __future__ import annotations

import argparse
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import xarray as xr

import chronozarr

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def load_mosaics(mosaic_dir: Path, workers: int) -> xr.DataArray:
    """Stack every YYYY-MM.npz in `mosaic_dir` into a (time, band, y, x) uint16 DataArray."""
    paths = sorted(mosaic_dir.glob("*.npz"))
    if not paths:
        raise SystemExit(f"no .npz mosaics in {mosaic_dir}; run scripts/ingest_v1.py first")

    with np.load(paths[0], allow_pickle=False) as first:
        n_band, height, width = first["bands"].shape
        transform = tuple(float(v) for v in first["transform"])
        epsg = int(first["epsg"])
        band_names = [str(b) for b in first["band_names"]]

    stack = np.empty((len(paths), n_band, height, width), dtype=np.uint16)

    def load(i: int) -> None:
        with np.load(paths[i], allow_pickle=False) as npz:
            same_grid = (
                tuple(float(v) for v in npz["transform"]) == transform
                and int(npz["epsg"]) == epsg
                and [str(b) for b in npz["band_names"]] == band_names
                and npz["bands"].shape == (n_band, height, width)
            )
            if not same_grid:
                raise SystemExit(f"{paths[i]} has a different grid, CRS or bands than {paths[0]}")
            stack[i] = npz["bands"]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(load, range(len(paths))))

    times = np.array([np.datetime64(f"{p.stem}-01", "s") for p in paths])
    return xr.DataArray(
        stack,
        dims=("time", "band", "y", "x"),
        coords={"time": times, "band": band_names},
        attrs={"crs": f"EPSG:{epsg}", "transform": transform},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--aoi", required=True)
    parser.add_argument("--n-lods", type=int, default=None, help="default: until a 1x1 cell grid")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument(
        "--overwrite", action="store_true", help="replace an existing output store"
    )
    args = parser.parse_args()

    mosaic_dir = DATA / "mosaics" / args.aoi
    out = DATA / "stores" / args.aoi / "chronozarr"
    if out.exists():
        if not args.overwrite:
            raise SystemExit(f"{out} exists; pass --overwrite to replace it")
        shutil.rmtree(out)

    started = time.perf_counter()
    da = load_mosaics(mosaic_dir, args.workers)
    load_s = time.perf_counter() - started
    raw_bytes = da.nbytes
    print(f"load: {load_s:.1f}s  {da.shape} uint16, {raw_bytes / 1e9:.2f} GB raw")

    started = time.perf_counter()
    report = chronozarr.encode(da, out, n_lods=args.n_lods, workers=args.workers)
    encode_write_s = time.perf_counter() - started

    print(f"\nworkers={args.workers}   times in seconds")
    print(
        f"{'lod':>3} {'shape':>18} {'downsample':>10} {'cells wall':>10} "
        f"{'encode/cpu':>10} {'write/cpu':>10} {'level wall':>10} {'MB':>8}"
    )
    for lvl in report.levels:
        shape = "x".join(str(n) for n in lvl.shape[2:])
        print(
            f"{lvl.level:>3} {shape:>18} {lvl.downsample_s:>10.2f} {lvl.cells_s:>10.2f} "
            f"{lvl.encode_thread_s:>10.2f} {lvl.write_thread_s:>10.2f} "
            f"{lvl.downsample_s + lvl.cells_s:>10.2f} {lvl.bytes / 1e6:>8.1f}"
        )
    print(f"\nencode+write total: {encode_write_s:.1f}s (includes metadata + consolidation)")
    print(
        f"store: {report.total_bytes / 1e6:.1f} MB in {report.n_files} files "
        f"({raw_bytes / report.total_bytes:.2f}x vs raw)"
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
