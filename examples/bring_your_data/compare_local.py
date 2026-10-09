"""Interleaved local full-frame reads of equivalent COG and Zarr level-0 numeric data.

Measures fresh reader handles on warm local storage, not browser delivery or cold CDN reads.
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import statistics
import time
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr

import chronozarr


def compare(
    manifest: Path, store_path: Path, plain_path: Path, output: Path, repeats: int
) -> None:
    with manifest.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    files = [manifest.parent / row["uri"] for row in rows]

    def cogs():
        values, masks = [], []
        for path in files:
            with rasterio.open(path) as source:
                values.append(source.read())
                masks.append(source.dataset_mask() > 0)
        return np.stack(values), np.stack(masks)

    def chrono():
        store = chronozarr.open_store(store_path)

        def mask_at(t: int) -> np.ndarray:
            mask = store.read_mask(t=t)
            if mask is None:
                raise ValueError(
                    f"{store_path} has no mask plane at t={t}; "
                    "the comparison needs the COG validity masks stored with the data"
                )
            return mask

        return (
            np.stack([store.read(t=t) for t in range(len(rows))]),
            np.stack([mask_at(t) for t in range(len(rows))]).astype(bool),
        )

    def plain():
        with xr.open_zarr(
            plain_path, group="0", zarr_format=3, chunks=None, mask_and_scale=False
        ) as ds:
            return ds.data.values, ds["mask"].values.astype(bool)

    readers = {"cog_rasterio": cogs, "chronozarr": chrono, "plain_zarr_xarray": plain}
    truth, valid = cogs()
    # Warm imports and OS file caches equally; subsequent handles are fresh for each read.
    for read in readers.values():
        values, mask = read()
        np.testing.assert_array_equal(values, truth)
        np.testing.assert_array_equal(mask, valid)
    timings = {name: [] for name in readers}
    names = list(readers)
    for rep in range(repeats):
        # Rotate order to reduce sensitivity to background load and warming effects.
        for name in names[rep % len(names) :] + names[: rep % len(names)]:
            started = time.perf_counter()
            values, mask = readers[name]()
            timings[name].append((time.perf_counter() - started) * 1000)
            np.testing.assert_array_equal(values, truth)
            np.testing.assert_array_equal(mask, valid)
    report = {
        "method": (
            "Full level-0 data and masks for all dates; fresh handles; warm local "
            "filesystem caches; rotated interleaving. Includes opening metadata, "
            "reading, decoding and assembling arrays. Exact-value assertions run "
            "outside timed intervals."
        ),
        "limits": (
            "Different reader implementations and codecs. No browser rendering, HTTP, "
            "CDN, private access, pixel-window latency, or continental scale is measured. "
            "Source COG overview values are not compared."
        ),
        "platform": platform.platform(),
        "shape": list(truth.shape),
        "repeats": repeats,
        "results": {
            name: {
                "median_ms": statistics.median(ms),
                "min_ms": min(ms),
                "max_ms": max(ms),
                "samples_ms": ms,
            }
            for name, ms in timings.items()
        },
        "all_values_and_masks_match": True,
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
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    compare(args.manifest, args.store, args.plain_store, args.report, args.repeats)
