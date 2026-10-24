"""Check a water stack built by build_water_stack.py against its source mosaics.

1. `chronozarr.validate` finds no problem.
2. xarray opens the store with engine="chronozarr": `mask` and `coverage` are variables, stored
   values are int16, physical values are NDWI in -1..1 with NaN exactly where the mask is 0, and
   the band scale/offset/units are in the store attributes.
3. Three pixels of one month (a water pixel, a land pixel, a masked pixel) and then the whole
   month are recomputed in numpy from the npz and the month's threshold in the CSV, without
   importing the build script.
4. `water` is 0 or 10000 at level 0 (scale 1e-4). Each overview is checked against recursively
   averaged valid child pixels, rounded down, with the same edge replication as the writer.
   Printed coarse means are display summaries, not conserved level-0 water-area statistics.

Usage:
    uv run python examples/water_masks/check_water_stack.py \
        --aoi ucayali_santa_maria --month 2019-03
    uv run python examples/water_masks/check_water_stack.py --aoi lake_mead \
        --month 2021-11

Use --boa-offset-from only when the store was built with that option from verified uncorrected
legacy mosaics. Current Sentinel-2 ingest already corrects the offset.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import xarray as xr

import chronozarr

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sentinel2_pc"))
from months import month_arrays, month_path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
DARK_DN = 5
failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"[{'ok' if ok else 'FAIL'}] {label}")
    if not ok:
        failures.append(label)


def recompute(
    npz_path: Path, boa_offset_from: str | None, threshold: int
) -> tuple[np.ndarray, ...]:
    """(ndwi int16, water int16 0 or 10000, valid bool) of one mosaic, straight from its arrays."""
    npz = month_arrays(npz_path)
    names = [str(b) for b in npz["band_names"]]
    bands = npz["bands"].astype(np.int32)
    valid = npz["coverage"] > 0
    if boa_offset_from is not None and npz_path.stem >= boa_offset_from:
        bands = np.where(bands > 0, np.maximum(bands - 1000, 1), 0)
    green, nir = bands[names.index("B03")], bands[names.index("B08")]
    ndwi = np.zeros(valid.shape, dtype=np.int16)
    ndwi[valid] = np.rint((green[valid] - nir[valid]) / (green[valid] + nir[valid]) * 1e4)
    dark = valid & (green <= DARK_DN) & (nir <= DARK_DN)
    water = valid & (dark | (ndwi > threshold))
    return ndwi, water.astype(np.int16) * np.int16(10_000), valid


def next_overview(values: np.ndarray, valid: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Independent reference for one 2x reduction: floor mean of valid children, any-valid mask."""
    padded_values, padded_valid = (
        np.pad(a, ((0, a.shape[0] % 2), (0, a.shape[1] % 2)), mode="edge")
        for a in (values.astype(np.int64), valid.astype(bool))
    )
    height, width = padded_values.shape
    blocks = padded_values.reshape(height // 2, 2, width // 2, 2)
    masks = padded_valid.reshape(height // 2, 2, width // 2, 2)
    count = masks.sum(axis=(1, 3))
    total = (blocks * masks).sum(axis=(1, 3))
    return np.where(count > 0, total // np.maximum(count, 1), 0), count > 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--aoi", required=True)
    parser.add_argument("--store-name", default="water-1")
    parser.add_argument(
        "--boa-offset-from",
        metavar="YYYY-MM",
        help="match a legacy store's correction only for verified uncorrected mosaics; "
        "current ingest already corrects it",
    )
    parser.add_argument(
        "--month", help="month to recompute (default: wettest with 90 %% valid pixels)"
    )
    args = parser.parse_args()
    store = DATA / "stores" / args.aoi / args.store_name
    with (store.parent / f"{args.store_name}.months.csv").open() as handle:
        rows = {r["month"]: r for r in csv.DictReader(handle) if r["status"] == "written"}

    problems = chronozarr.validate(store)
    check(not problems, f"validate: {problems or 'conforms'}")

    stored = xr.open_dataset(store, engine="chronozarr", physical=False)
    physical = xr.open_dataset(store, engine="chronozarr")
    info = chronozarr.open_store(store)
    check(
        set(stored.data_vars) == {"data", "mask", "coverage"},
        f"variables {sorted(str(v) for v in stored.data_vars)}",
    )
    check(stored["data"].dtype == np.int16, f"stored dtype {stored['data'].dtype}")
    check(physical["data"].dtype == np.float32, f"physical dtype {physical['data'].dtype}")
    check(stored["mask"].dims == ("time", "y", "x"), f"mask dims {stored['mask'].dims}")
    declared = {b.name: (b.scale, b.offset, b.units) for b in info.attrs.bands}
    check(
        declared == {"ndwi": (1e-4, 0.0, "index"), "water": (1e-4, 0.0, "fraction")},
        f"band scale/offset/units in the store attributes: {declared}",
    )
    print(f"xarray dataset attrs: {dict(stored.attrs)}")

    month = args.month or max(
        (m for m, r in rows.items() if float(r["valid_frac"]) >= 0.9),
        key=lambda m: float(rows[m]["water_frac"]),
    )
    t = list(stored["time"].dt.strftime("%Y-%m").values).index(month)
    threshold = round(float(rows[month]["threshold"]) * 1e4)
    path = month_path(DATA / "mosaics" / args.aoi, month)
    if path is None:
        raise SystemExit(f"no mosaic for {month} in {DATA / 'mosaics' / args.aoi}")
    ndwi, water, valid = recompute(path, args.boa_offset_from, threshold)
    print(f"month {month} (t={t}): threshold {threshold} stored units, {valid.sum()} valid pixels")

    window = physical.isel(time=t, band=0).data.values
    mask = stored["mask"].isel(time=t).values
    check(
        np.array_equal(np.isnan(window), mask == 0), "physical NDWI is NaN exactly where mask == 0"
    )
    finite = window[~np.isnan(window)]
    check(
        float(finite.min()) >= -1 and float(finite.max()) <= 1,
        f"physical NDWI range {finite.min():.4f}..{finite.max():.4f}",
    )
    check(np.array_equal(mask.astype(bool), valid), "mask equals coverage > 0 of the npz")

    dataset = stored["data"].isel(time=t)
    store_ndwi, store_water = dataset.sel(band="ndwi").values, dataset.sel(band="water").values
    picks = {
        "water": tuple(np.argwhere(water == 10_000)[len(np.argwhere(water == 10_000)) // 2]),
        "land": tuple(
            np.argwhere((water == 0) & valid)[len(np.argwhere((water == 0) & valid)) // 3]
        ),
    }
    gaps = np.argwhere(~valid)
    if len(gaps):
        picks["masked"] = tuple(gaps[len(gaps) // 2])
    for kind, (row, col) in picks.items():
        got = (int(store_water[row, col]), int(store_ndwi[row, col]), int(mask[row, col]))
        want = (int(water[row, col]), int(ndwi[row, col]), int(valid[row, col]))
        check(
            got == want,
            f"pixel ({row}, {col}) [{kind}] store (water, ndwi, mask) {got} vs numpy {want}",
        )
    check(
        np.array_equal(store_water, water), f"whole month: water equal at all {water.size} pixels"
    )
    check(np.array_equal(store_ndwi, ndwi), "whole month: ndwi equal at all pixels")
    check(
        bool(np.isin(store_water[mask == 0], 0).all() and np.isin(store_ndwi[mask == 0], 0).all()),
        "masked pixels hold 0",
    )

    expected = store_water.astype(np.int64)
    expected_mask = mask.astype(bool)
    share0 = float(expected[expected_mask].mean() / 1e4)
    print(f"\nlevel-0 water share of valid pixels in {month}: {share0:.5f}")
    print("overview means are recursive display summaries, not water-area estimates:")
    for lod in range(1, min(4, len(info.levels))):
        expected, expected_mask = next_overview(expected, expected_mask)
        with xr.open_dataset(store, engine="chronozarr", physical=False, lod=lod) as level:
            fractions = level["data"].isel(time=t).sel(band="water").values
            valid_blocks = level["mask"].isel(time=t).values.astype(bool)
        check(np.array_equal(valid_blocks, expected_mask), f"level {lod} any-valid child mask")
        check(
            np.array_equal(fractions, expected),
            f"level {lod} water equals the recursive floor mean of valid child blocks",
        )
        partial = int((fractions[valid_blocks] % 10_000 != 0).sum())
        print(
            f"  lod {lod}: {fractions[valid_blocks].mean() / 1e4:.5f} over valid blocks, "
            f"{partial} blocks hold a fraction "
            f"strictly between 0 and 1"
        )
    if failures:
        raise SystemExit(f"{len(failures)} check(s) failed: {failures}")
    print("all checks passed")


if __name__ == "__main__":
    main()
