"""Verify already-open HTTP snapshots using the twelve existing real inputs."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr

import chronozarr
from chronozarr.convert import convert
from chronozarr.view import StoreServer


def run(source: Path, output: Path) -> None:
    output.mkdir(parents=True, exist_ok=False)
    with (source / "observations.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 12
    manifests = []
    for name, subset in [("head", rows[:11]), ("tail", rows[11:])]:
        manifest = output / f"{name}.csv"
        with manifest.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["uri", "datetime"])
            writer.writeheader()
            writer.writerows(
                {**row, "uri": str((source / row["uri"]).resolve())} for row in subset
            )
        manifests.append(manifest)
    published, tail, stage = output / "published", output / "tail", output / "stage"
    convert(manifests[0], published)
    convert(manifests[1], tail)
    server = StoreServer(published, port=0)
    try:
        old = chronozarr.open_store(server.url)
        backend = xr.open_dataset(server.url, engine="chronozarr", physical=False)
        shutil.copytree(published, stage)
        chronozarr.append(stage, tail)
        for source_file in sorted(stage.rglob("*"), key=lambda p: p == stage / "zarr.json"):
            if not source_file.is_file():
                continue
            target = published / source_file.relative_to(stage)
            target.parent.mkdir(parents=True, exist_ok=True)
            upload = target.with_name(target.name + ".upload")
            shutil.copyfile(source_file, upload)
            os.replace(upload, target)
        fresh = chronozarr.open_store(server.url)
        assert len(old.times) == backend.sizes["time"] == 11
        assert len(fresh.times) == 12
        try:
            old.read(11)
        except IndexError:
            pass
        else:
            raise AssertionError("Old snapshot accepted new date")
        variable = old.attrs.variable
        for t, row in enumerate(rows):
            with rasterio.open(source / row["uri"]) as cog:
                values, valid = cog.read(), cog.dataset_mask() > 0
            np.testing.assert_array_equal(fresh.read(t), values)
            np.testing.assert_array_equal(fresh.read_mask(t), valid)
            if t < 11:
                np.testing.assert_array_equal(old.read(t), values)
                np.testing.assert_array_equal(old.read_mask(t), valid)
                np.testing.assert_array_equal(backend[variable].isel(time=t).values, values)
        backend.close()
    finally:
        server.close()
    result = {
        "source": str(source.resolve()),
        "old_http_axis": 11,
        "old_lazy_backend_axis": 11,
        "reopened_http_axis": 12,
        "old_new_date": "IndexError",
        "uncached_old_values_and_masks": "all eleven exact",
        "reopened_values_and_masks": "all twelve exact",
        "publication": "atomic file replacement, root metadata last",
        "limits": "Same-host unsharded real small grid; shard/failure cases synthetic.",
    }
    (output / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    run(args.source, args.output)
