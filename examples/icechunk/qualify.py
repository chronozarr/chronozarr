"""Reproduce pinned live imports; comparison uses a direct maintained Icechunk/xarray read.

Run from the repository with geo and icechunk extras:
    uv run --extra geo --extra icechunk python examples/icechunk/qualify.py NEW_DIR
The two products differ: each import is checked against its own snapshot.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import icechunk
import numpy as np
import xarray as xr

from chronozarr import IcechunkInput, open_store
from chronozarr.convert import convert

CASES = (
    ("materialized", "noaa-gfs-analysis", "6K6H23PYR6QXA9VM0C6G", False),
    ("virtual", "noaa-gfs-analysis-virtual", "SJSV5KG1GSWR2XQD2SW0", True),
)


def qualify(directory: Path) -> dict:
    directory.mkdir(parents=True, exist_ok=False)
    receipt = {
        "checked_at": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "icechunk": icechunk.__version__,
        "sources": [],
        "limits": "Two time steps, a 2x2 window; same-library reference, no viewer speed claim. "
        "Elapsed import includes metadata, sampling, source reads and encoding; no wire counters.",
    }
    indexes = {
        "time": slice(48072, 48074),
        "latitude": slice(224, 226),
        "longitude": slice(316, 318),
    }
    for name, dataset, snapshot, cast in CASES:
        root = f"https://dynamical-noaa-gfs.s3.us-west-2.amazonaws.com/{dataset}/v0.1.0.icechunk"
        auth = (
            {
                "s3://noaa-gfs-bdp-pds/": icechunk.Credentials.S3(
                    icechunk.s3_anonymous_credentials()
                )
            }
            if cast
            else {}
        )
        repo = icechunk.Repository.open(
            icechunk.http_storage(root), authorize_virtual_chunk_access=auth
        )
        session = repo.readonly_session(snapshot_id=snapshot)
        with xr.open_zarr(
            session.store, chunks=None, consolidated=False, mask_and_scale=False
        ) as ds:
            reference = ds.temperature_2m.isel(indexes).values
            units = ds.temperature_2m.attrs["units"]
            crs = ds.spatial_ref.attrs["crs_wkt"]
        source = IcechunkInput(
            root,
            indexes,
            snapshot=snapshot,
            virtual_prefixes=("s3://noaa-gfs-bdp-pds/",) if cast else (),
            cast_float32=cast,
        )
        started = time.perf_counter()
        report = convert(
            source,
            directory / name,
            variable="temperature_2m",
            nodata=float("nan"),
            read_ahead=1,
            chunk_size=16,
            n_lods=1,
        )
        elapsed = time.perf_counter() - started
        opened = open_store(directory / name)
        expected = reference.astype("float32")
        np.testing.assert_array_equal(opened.to_xarray().values[:, 0], expected)
        assert opened.attrs.bands[0].units == units
        assert "6371229" in opened.crs.wkt2
        receipt["sources"].append(
            {
                "name": name,
                "repository": root,
                "snapshot": snapshot,
                "indexers": {k: [v.start, v.stop] for k, v in indexes.items()},
                "source_dtype": str(reference.dtype),
                "output_dtype": "float32",
                "explicit_rounding": cast,
                "units": units,
                "source_crs_wkt": crs,
                "source_values": reference.tolist(),
                "expected_float32": expected.tolist(),
                "output": name,
                "exact_expected_values": True,
                "import_seconds": elapsed,
                "plan": report.plan.lines(),
            }
        )
    target = directory / "receipt.json"
    target.write_text(json.dumps(receipt, indent=2) + "\n")
    checked = subprocess.run(
        ["node", str(Path(__file__).with_name("check-reader.mjs")), str(directory)],
        check=True,
        text=True,
        capture_output=True,
    )
    receipt["javascript"] = json.loads(checked.stdout)
    target.write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    qualify(args.directory)
    print(args.directory / "receipt.json")
