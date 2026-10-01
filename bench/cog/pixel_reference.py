"""Reads one pixel's complete history straight from a chronozarr store's Zarr arrays.

No chronozarr code is involved: the values are the reference that the two JavaScript readers
(geotiff.js on the COGs, the chronozarr reader on the store) are compared with.

    uv run python bench/cog/pixel_reference.py <store dir> <level> <row> <col> <out.json>

`row` and `col` are pixel coordinates in the level's array. Output: {"level", "row", "col",
"times": [...], "values": [[band0, band1, ...] per timestep]}.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import zarr


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print(__doc__, file=sys.stderr)
        return 2
    store_dir = Path(argv[1])
    level, row, col = int(argv[2]), int(argv[3]), int(argv[4])
    out = Path(argv[5])
    root = json.loads((store_dir / "zarr.json").read_text())
    times = list(root["attributes"]["chronozarr"]["times"])
    array = zarr.open_array(str(store_dir / str(level) / "data"), mode="r")
    values = np.asarray(array[:, :, row, col])
    if values.shape[0] != len(times):
        print(
            f"{store_dir}: {values.shape[0]} timesteps in the array, {len(times)} in the root",
            file=sys.stderr,
        )
        return 1
    payload = {
        "level": level,
        "row": row,
        "col": col,
        "times": times,
        "values": values.astype(int).tolist(),
    }
    out.write_text(json.dumps(payload))
    print(f"wrote {out}: {values.shape[0]} timesteps x {values.shape[1]} bands")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
