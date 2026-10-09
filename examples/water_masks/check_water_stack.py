"""Check a water stack built by build_water_stack.py against its source mosaics.

1. `chronozarr.validate` finds no problem.
2. xarray opens the store with engine="chronozarr": `mask` and `coverage` are variables, stored
   values are int16, physical values are NDWI in -1..1 with NaN exactly where the mask is 0, and
   the band scale/offset/units are in the store attributes.
3. Three pixels of one month (a water pixel, a land pixel, a masked pixel) and then the whole
   month are recomputed in numpy from the npz and the month's threshold in the CSV, without
   importing the build script.
4. `water` is a fraction (0 or 10000 at level 0, scale 1e-4): level 1 equals the floor mean of
   level 0 over valid pixels, and the water share of valid pixels is printed for levels 0 to 3.

Usage:
    uv run python examples/water_masks/check_water_stack.py \
        --aoi ucayali_santa_maria --month 2019-03
    uv run python examples/water_masks/check_water_stack.py --aoi lake_mead \
        --boa-offset-from 2022-02 --month 2021-11
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import numpy as np
import xarray as xr

import chronozarr

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
    with np.load(npz_path, allow_pickle=False) as npz:
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--aoi", required=True)
    parser.add_argument("--store-name", default="water-1")
    parser.add_argument("--boa-offset-from", metavar="YYYY-MM")
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
    ndwi, water, valid = recompute(
        DATA / "mosaics" / args.aoi / f"{month}.npz", args.boa_offset_from, threshold
    )
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

    water_plane = stored["data"].isel(time=t).sel(band="water").values.astype(np.int64)
    padded = [
        np.pad(a, ((0, a.shape[0] % 2), (0, a.shape[1] % 2)), mode="edge")
        for a in (water_plane, mask.astype(np.int64))
    ]
    blocks = [a.reshape(a.shape[0] // 2, 2, a.shape[1] // 2, 2) for a in padded]
    count = blocks[1].sum(axis=(1, 3))
    total = (blocks[0] * blocks[1]).sum(axis=(1, 3))
    expected = np.where(count > 0, total // np.maximum(count, 1), 0)
    one = xr.open_dataset(store, engine="chronozarr", physical=False, lod=1)
    got = one["data"].isel(time=t).sel(band="water").values
    check(
        np.array_equal(got, expected),
        f"level 1 water equals the floor mean of level 0 over valid pixels ({got.size} blocks)",
    )

    # Share of water per level, two ways: the mean over the valid blocks of the level (what a click
    # on the overview sees), and the mean weighted by the valid level-0 pixels in each block, which
    # is the level-0 share up to the floor division. They differ where blocks are partly valid.
    mask0 = mask.astype(np.int64)
    share0 = float((water_plane * mask0).sum() / 1e4 / mask0.sum())
    print(f"\nwater share of valid pixels in {month} by pyramid level (mean of water / 10000):")
    print(f"  lod 0: {share0:.5f}")
    for lod in range(1, 4):
        factor = 2**lod
        level = xr.open_dataset(store, engine="chronozarr", physical=False, lod=lod)
        h, w = (n // factor * factor for n in mask0.shape)
        rows, cols = h // factor, w // factor
        weights = mask0[:h, :w].reshape(rows, factor, cols, factor).sum(axis=(1, 3))
        fractions = (
            level["data"].isel(time=t).sel(band="water").values[:rows, :cols].astype(np.int64)
        )
        valid_blocks = level["mask"].isel(time=t).values[:rows, :cols].astype(bool)
        weighted = float((fractions * weights).sum() / 1e4 / weights.sum())
        partial = int((fractions[valid_blocks] % 10_000 != 0).sum())
        print(
            f"  lod {lod}: {fractions[valid_blocks].mean() / 1e4:.5f} over valid blocks, "
            f"{weighted:.5f} weighted by valid level-0 pixels; {partial} blocks hold a fraction "
            f"strictly between 0 and 1"
        )
        check(
            abs(weighted - share0) < 1e-3,
            f"level {lod} weighted water share within 0.1 point of level 0",
        )
    if failures:
        raise SystemExit(f"{len(failures)} check(s) failed: {failures}")
    print("all checks passed")


if __name__ == "__main__":
    main()
