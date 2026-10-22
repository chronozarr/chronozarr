"""Dump what a converted store holds, read through the store's own version of chronozarr.

Runs inside the venv that wrote the store (`python -I _dump_store.py STORE OUT.json`). It
writes the time axis, bands, CRS, grid and the physical values at the same random pixel
positions of three random timesteps, so that `_check_truth.py` can compare them with the
source files without importing chronozarr. Positions are drawn from a fixed seed, so versions
A and B see the same ones.
"""

from __future__ import annotations

import json
import sys

import numpy as np
import xarray as xr

SEED = 20261016
N_TIMES = 3
N_PIXELS = 600


def main(store: str, out: str) -> int:
    ds = xr.open_dataset(store, engine="chronozarr")
    times = ds["time"].values.astype("datetime64[ms]")
    rng = np.random.RandomState(SEED)
    n_t = len(times)
    t_idx = sorted(int(i) for i in rng.choice(n_t, size=min(N_TIMES, n_t), replace=False))
    ny, nx = ds.sizes["y"], ds.sizes["x"]
    rows = rng.randint(0, ny, N_PIXELS)
    cols = rng.randint(0, nx, N_PIXELS)
    samples = []
    for t in t_idx:
        plane = ds["data"].isel(time=t).values  # (band, y, x) physical float32
        samples.append(
            {
                "t": t,
                "values": [
                    [None if np.isnan(v) else float(v) for v in plane[b, rows, cols]]
                    for b in range(plane.shape[0])
                ],
            }
        )
    attrs = {
        k: (v if isinstance(v, str | int | float | bool) else np.asarray(v).tolist())
        for k, v in ds.attrs.items()
    }
    doc = {
        "times": [str(t) for t in times],
        "bands": [str(b) for b in ds["band"].values],
        "dims": {k: int(v) for k, v in ds.sizes.items()},
        "attrs": attrs,
        "x": [float(v) for v in ds["x"].values],
        "y": [float(v) for v in ds["y"].values],
        "rows": rows.tolist(),
        "cols": cols.tolist(),
        "samples": samples,
        "variables": sorted(str(name) for name in ds.variables),
    }
    with open(out, "w") as fh:
        json.dump(doc, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
