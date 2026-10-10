"""Reference pixel values for the like-for-like check, read with no chronozarr code and no browser.

    uv run python bench/v03/reference.py STORE_DIR COG_DIR '{"pixels": [[row, col], ...], "times": [t, ...]}'

Prints a JSON list with, for every (time, pixel), the four band values of level 0 read straight from the Zarr array
(zarr-python) and from that date's COG (rasterio/GDAL). The COGs are written from the store by `chronozarr export-cog`,
so a difference between the two lists would be a bug of the export, not of a reader.
"""

import json
import sys
from pathlib import Path

import rasterio
import zarr


def main(store_dir: str, cog_dir: str, spec: dict) -> list[dict]:
    group = zarr.open_group(store_dir, mode="r")
    times = group.attrs["chronozarr"]["times"]
    data = group["0"]["data"]
    rows = []
    for t in spec["times"]:
        cog_path = Path(cog_dir) / f"L0_{times[t][:10]}.tif"
        with rasterio.open(cog_path) as src:
            for row, col in spec["pixels"]:
                from_store = [int(v) for v in data[t, :, row, col]]
                from_cog = [
                    int(v) for v in src.read(window=((row, row + 1), (col, col + 1))).reshape(-1)
                ]
                rows.append(
                    {
                        "t": t,
                        "row": row,
                        "col": col,
                        "date": times[t][:10],
                        "store": from_store,
                        "cog": from_cog,
                    }
                )
    return rows


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    json.dump(main(sys.argv[1], sys.argv[2], json.loads(sys.argv[3])), sys.stdout)
