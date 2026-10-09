"""Cut the example window out of a NISAR L2 GCOV granule; the granule is read only.

The output is the extract that build_demo.py reads: an .npz with x, y, epsg, HHHH, HVHV and
mask, taken from /science/LSAR/GCOV/grids/frequencyA.
"""

import argparse
import re
from pathlib import Path

import h5py
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
GROUP = "science/LSAR/GCOV/grids/frequencyA"
EPSG = 32618
# Pixel-centre coordinates in metres on the 20 m grid: 559 columns by 823 rows.
X_RANGE = (512270.0, 523430.0)
Y_RANGE = (888070.0, 904510.0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("granule", type=Path, help="a NISAR_L2_PR_GCOV_*.h5 file")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data/examples/nisar")
    args = parser.parse_args()
    stamp = re.search(r"_(\d{8})T\d{6}_\d{8}T\d{6}_", args.granule.name)
    if stamp is None:
        raise ValueError(
            f"{args.granule.name} has no start and end time; expected a GCOV file name"
        )
    out = args.out_dir / f"nisar_{stamp[1]}_frequencyA.npz"
    with h5py.File(args.granule, "r") as h5:
        grid = h5[GROUP]
        x, y = grid["xCoordinates"][:], grid["yCoordinates"][:]
        columns = np.flatnonzero((x >= X_RANGE[0]) & (x <= X_RANGE[1]))
        rows = np.flatnonzero((y >= Y_RANGE[0]) & (y <= Y_RANGE[1]))
        if (
            columns.size == 0
            or rows.size == 0
            or not np.allclose([x[columns[0]], x[columns[-1]]], X_RANGE)
            or not np.allclose([y[rows[-1]], y[rows[0]]], Y_RANGE)
        ):
            raise ValueError(
                f"{args.granule.name} does not cover x {X_RANGE} and y {Y_RANGE} (EPSG:{EPSG}). "
                "Use the granules that examples/nisar/README.md lists."
            )
        window = (slice(rows[0], rows[-1] + 1), slice(columns[0], columns[-1] + 1))
        arrays = {
            "HHHH": grid["HHHH"][window].astype("float32"),
            "HVHV": grid["HVHV"][window].astype("float32"),
            "mask": grid["mask"][window],
        }
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, x=x[columns], y=y[rows], epsg=np.int64(EPSG), **arrays)
    print(f"Wrote {out}: {columns.size} columns x {rows.size} rows")


if __name__ == "__main__":
    main()
