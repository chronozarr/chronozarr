"""Compressed size of one date in the store and in COGs written with different codecs.

The store holds each date, cell and level as one zstd level 5 chunk. `chronozarr export-cog` writes
DEFLATE with predictor 2. To see how much of any difference between the two is codec and how much
is layout, this re-encodes the exported COG's pixels as COGs with ZSTD level 5 (no predictor, like
the store) and with ZSTD level 5 plus predictor 2, and compares file sizes with the store's chunk
sizes for the same date (read from the shard indexes).

    uv run python bench/cog/codec_sizes.py STORE_DIR COG TIMESTEP SCRATCH_DIR OUT_JSON
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import rasterio
import rasterio.shutil  # ty: ignore[unresolved-import]  # compiled module, no stub
from rasterio.io import MemoryFile

INDEX_ENTRY_BYTES = 16
CRC_BYTES = 4


def store_chunk_bytes(store_dir: Path, timestep: int) -> dict[str, int]:
    """Compressed bytes of every cell of every level at one timestep, from the shard indexes."""
    root = json.loads((store_dir / "zarr.json").read_text())
    levels = root["attributes"]["chronozarr"]["levels"]
    n_time = len(root["attributes"]["chronozarr"]["times"])
    out: dict[str, int] = {}
    for level in levels:
        rows, cols = level["grid"]
        total = 0
        for row in range(rows):
            for col in range(cols):
                shard = store_dir / level["path"] / "data" / "c" / "0" / "0" / str(row) / str(col)
                with shard.open("rb") as f:
                    f.seek(-(n_time * INDEX_ENTRY_BYTES + CRC_BYTES), 2)
                    index = np.frombuffer(f.read(n_time * INDEX_ENTRY_BYTES), dtype="<u8")
                total += int(index.reshape(n_time, 2)[timestep, 1])
        out[f"level{level['path']}"] = total
    out["all levels"] = sum(out.values())
    return out


def reencode(src_path: Path, scratch: Path, name: str, **options: object) -> int:
    with rasterio.open(src_path) as src:
        profile = src.profile
        data = src.read()
        descriptions = src.descriptions
    profile.update(driver="GTiff", tiled=True, blockxsize=512, blockysize=512)
    profile.pop("compress", None)
    profile.pop("predictor", None)
    target = scratch / name
    with MemoryFile() as memory:
        with memory.open(**profile) as dst:
            dst.write(data)
            dst.descriptions = descriptions
        with memory.open() as mem:
            rasterio.shutil.copy(  # ty: ignore[unresolved-attribute]  # compiled module, no stub
                mem, target, driver="COG", blocksize=512, overview_resampling="AVERAGE", **options
            )
    return target.stat().st_size


def main(argv: list[str]) -> int:
    if len(argv) != 6:
        print(__doc__, file=sys.stderr)
        return 2
    store_dir, cog, scratch, out = Path(argv[1]), Path(argv[2]), Path(argv[4]), Path(argv[5])
    timestep = int(argv[3])
    scratch.mkdir(parents=True, exist_ok=True)
    result = {
        "timestep": timestep,
        "store (zstd 5, chunks of all levels)": store_chunk_bytes(store_dir, timestep),
        "COG as exported (DEFLATE, predictor 2), file bytes": cog.stat().st_size,
        "COG ZSTD 5, no predictor, file bytes": reencode(
            cog, scratch, "zstd5.tif", compress="ZSTD", level=5
        ),
        "COG ZSTD 5, predictor 2, file bytes": reencode(
            cog, scratch, "zstd5_pred2.tif", compress="ZSTD", level=5, predictor="YES"
        ),
    }
    out.write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
