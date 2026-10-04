"""Verify a local COG manifest against converted stores and write bounded adoption evidence."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr

import chronozarr


def inventory(path: Path) -> dict:
    files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    return {"objects": len(files), "bytes": sum(p.stat().st_size for p in files)}


def verify(manifest: Path, store_path: Path, plain_path: Path, output: Path) -> None:
    with manifest.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    source_files = [manifest.parent / row["uri"] for row in rows]
    store = chronozarr.open_store(store_path)
    source_valid, cogs = [], []
    with (
        xr.open_dataset(store_path, engine="chronozarr") as ds,
        xr.open_zarr(
            plain_path, group="0", zarr_format=3, chunks=None, mask_and_scale=False
        ) as plain,
    ):
        assert len(rows) == len(store.times) == plain.sizes["time"]
        for t, path in enumerate(source_files):
            with rasterio.open(path) as source:
                truth = source.read()
                valid = source.dataset_mask() > 0
                np.testing.assert_array_equal(store.read(t=t), truth)
                np.testing.assert_array_equal(store.read_mask(t=t), valid)
                np.testing.assert_array_equal(plain[store.attrs.variable].isel(time=t), truth)
                np.testing.assert_array_equal(plain[store.attrs.mask_variable].isel(time=t), valid)
                assert str(store.times[t].astype("datetime64[D]")) == rows[t]["datetime"]
                expected = truth.astype(np.float32)
                for band, (scale, offset) in enumerate(
                    zip(source.scales, source.offsets, strict=True)
                ):
                    expected[band] = expected[band] * np.float32(scale) + np.float32(offset)
                expected[:, ~valid] = np.nan
                np.testing.assert_allclose(ds[store.attrs.variable].isel(time=t), expected)
                source_valid.append(valid)
                cogs.append(
                    {"file": path.name, **inventory(path), "overviews": source.overviews(1)}
                )
        common_valid = np.logical_and.reduce(source_valid)
        assert common_valid.any(), "No pixel is valid across all dates"
        row, col = np.argwhere(common_valid)[len(np.argwhere(common_valid)) // 2]
        history = ds[store.attrs.variable].isel(y=int(row), x=int(col)).values.tolist()
        report = {
            "scope": (
                "Local observations: adoption and level-0 fidelity; "
                "not a scale or delivery benchmark."
            ),
            "dates": [row["datetime"] for row in rows],
            "shape": list(store.levels[0].shape),
            "valid_fractions": [float(v.mean()) for v in source_valid],
            "spec_version": store.attrs.spec_version,
            "checks": {
                "stored_values": "exact",
                "validity_masks": "exact",
                "physical_values": "passed",
                "native_zarr_level0": "exact",
            },
            "pixel_history": {"row": int(row), "col": int(col), "physical_values": history},
            "storage": {
                "source_cogs": cogs,
                "chronozarr": inventory(store_path),
                "plain_zarr": inventory(plain_path),
            },
            "comparison_limits": (
                "COGs use DEFLATE; both Zarr layouts use zstd 5. COG overview values and "
                "masks were not reconciled with the Zarr pyramids. Bytes include each "
                "representation's metadata and overviews; they do not establish a format "
                "or bandwidth advantage."
            ),
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("store", type=Path)
    parser.add_argument("plain_store", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    verify(args.manifest, args.store, args.plain_store, args.report)
