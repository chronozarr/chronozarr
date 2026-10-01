"""Check that a store converted from the PNG frames holds every frame exactly.

For each frame in the manifest, the store's level-0 bands must equal the PNG's red, green and blue
values, and the store's mask must be the PNG's alpha band being nonzero. Values under a zero mask
are not checked specially: they are the PNG's values too, which render_frames.py sets to 0.

Usage:
    uv run python examples/png_frames/check_store.py \
        [--frames data/png_frames/ucayali] [--store data/stores/ucayali_santa_maria/png-1]
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import rasterio

import chronozarr

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=Path, default=ROOT / "data/png_frames/ucayali")
    parser.add_argument(
        "--store", type=Path, default=ROOT / "data/stores/ucayali_santa_maria/png-1"
    )
    args = parser.parse_args()

    with (args.frames / "manifest.csv").open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    store = chronozarr.open_store(args.store)
    if len(rows) != len(store.times):
        sys.exit(f"{len(rows)} frames in the manifest, {len(store.times)} timesteps in the store")

    failures = []
    masked = []
    for t, row in enumerate(sorted(rows, key=lambda r: r["datetime"])):
        with rasterio.open(args.frames / row["uri"]) as png:
            pixels = png.read()
        data_ok = np.array_equal(store.read(t), pixels[:3])
        plane = store.read_mask(t)
        if plane is None:
            sys.exit(f"{args.store} has no mask; the frames' alpha band should have become one")
        mask = plane.astype(bool)
        mask_ok = np.array_equal(mask, pixels[3] != 0)
        masked.append(1.0 - float(mask.mean()))
        if not (data_ok and mask_ok):
            failures.append(
                f"{row['uri']}: data {'ok' if data_ok else 'DIFFERS'}, "
                f"mask {'ok' if mask_ok else 'DIFFERS'}"
            )
    if failures:
        sys.exit("\n".join(failures))
    print(
        f"{len(rows)} frames: red, green, blue and the alpha mask are bit-exact in {args.store}; "
        f"{sum(m > 0 for m in masked)} frames have masked pixels (up to {max(masked):.1%})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
