"""Read the monthly mosaics of an AOI, in either format, without the ingest's dependencies.

The ingest writes YYYY-MM.tif (mosaic.py: bands, then the valid-scene count, with the month's
record in GDAL metadata); months written before 2026-10-11 are YYYY-MM.npz. Scripts that read
the mosaics (water masks, PNG frames, re-encoding) use `month_files` and `month_arrays`, so a
directory holding both formats is read whole.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import rasterio


def month_path(directory: Path, key: str) -> Path | None:
    """The saved file of month `key` in `directory`, GeoTIFF or legacy .npz, or None."""
    for suffix in (".tif", ".npz"):
        path = directory / f"{key}{suffix}"
        if path.exists():
            return path
    return None


def month_files(directory: Path) -> list[Path]:
    """The monthly files in `directory` in time order; a month saved in both formats is an
    error, since either could be stale."""
    by_month: dict[str, Path] = {}
    for path in sorted([*directory.glob("*.tif"), *directory.glob("*.npz")]):
        if path.stem in by_month:
            raise ValueError(f"{path.stem} has both a .tif and an .npz in {directory}")
        by_month[path.stem] = path
    return [by_month[m] for m in sorted(by_month)]


def month_arrays(path: Path) -> dict:
    """bands (uint16, n_bands x H x W), coverage (float32, valid fraction k / n), transform
    (6 affine coefficients), epsg and band_names of a month: the arrays of the .npz format,
    read from either format."""
    if path.suffix == ".npz":
        with np.load(path, allow_pickle=False) as data:
            return {
                "bands": data["bands"],
                "coverage": data["coverage"],
                "transform": np.asarray(data["transform"], dtype=np.float64),
                "epsg": int(data["epsg"]),
                "band_names": [str(v) for v in data["band_names"]],
            }
    with rasterio.open(path) as src:
        stack = src.read()
        tags = src.tags()
        transform = np.array(list(src.transform)[:6], dtype=np.float64)
        epsg = src.crs.to_epsg()
    count = stack[-1]
    n = json.loads(tags["scenes_searched"])
    coverage = count.astype(np.float32) / n if n else np.zeros(count.shape, dtype=np.float32)
    return {
        "bands": stack[:-1],
        "coverage": coverage,
        "transform": transform,
        "epsg": int(epsg),
        "band_names": json.loads(tags["band_names"]),
    }
