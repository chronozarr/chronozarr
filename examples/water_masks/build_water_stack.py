"""Derive a water-mask chronozarr store from an AOI's monthly Sentinel-2 mosaics.

Reads data/mosaics/<aoi>/YYYY-MM.npz (the ingest in examples/sentinel2_pc) and writes
data/stores/<aoi>/<store-name>/ on the AOI's native UTM grid with two int16 bands:

    ndwi   (green - nir) / (green + nir) from B03 and B08, stored x10000 (scale 1e-4)
    water  1 where ndwi > the month's threshold or the pixel is at the DN floor, else 0, stored
           as a fraction: 0 or 10000 with scale 1e-4, units "fraction"

plus a validity `mask` (0 where the mosaic's coverage is 0: no scene was valid that month, so the
pixel is a carry-forward copy of an earlier month or was never observed) and a `coverage` plane
(1 where at least one scene was valid; the mosaics keep the valid fraction, not a scene count).
Masked pixels hold 0 in both bands. Gaps are not filled.

Why `water` is a fraction and not a 0/1 flag: pyramid levels are block means. The mean of an
integer band is a floor division, so a 0/1 band floors to 0 unless every pixel of the block is
water and coarse levels undercount (Ucayali 2019-03: 12.1 % of valid pixels at level 0, 8.5 % at
level 3). Stored as 0 or 10000, the block mean is the water fraction of the block.

The threshold of a month is max(Otsu, floor). Otsu is computed in numpy on the histogram of the
month's valid ndwi values (exactly one bin per stored integer, so the threshold is a stored value).
The floor (`--floor`, default NDWI 0, the McFeeters convention for open water) stops a month with
little or no water from thresholding noise: on a unimodal histogram Otsu splits the land mode in
two and labels half the scene water. Where land is dense forest (Ucayali) Otsu separates forest
from everything else at about -0.3, never water from land, so there the floor is the threshold;
docs/user-zero.md has the sweep that picked -0.15 for that reach (turbid water sits near NDWI 0).

Dark pixels: L2A clips reflectance at DN 1, and in many pre-2022 winter months the whole of Lake
Mead is DN 1 in every band. Green and nir are then both at the floor and ndwi is 0 or noise, so
NDWI cannot see the water. A valid pixel with green <= DARK_DN and nir <= DARK_DN is water (zero
reflectance in green and nir) and is left out of the Otsu histogram; its stored ndwi stays the
value computed from the clipped DNs. So at level 0 `water == 10000 * (ndwi > threshold)` holds
except at dark pixels.

The CSV next to the store records per month the Otsu threshold, the applied threshold, whether the
floor was binding, the Otsu separability eta (between-class variance over total variance), the
valid fraction, the dark fraction of valid pixels, the water fraction of valid pixels at the
applied threshold and at the raw Otsu threshold (floor off), and the median blue DN (a haze
screen: clear months sit near 250-500 over forest, 800-1400 over desert).

Months with no valid pixel at all are not written (listed in the CSV as skipped).

Mosaics made before the Sentinel-2 processing baseline 04.00 fix (25 Jan 2022 adds +1000 to every
L2A digital number) still carry the offset. `--boa-offset-from YYYY-MM` subtracts 1000 from every
valid DN of the months from YYYY-MM on (clipped to >= 1, as the ingest does); a per-pixel median
commutes with that shift. Lake Mead needs it from 2022-02, Ucayali was corrected at ingest.

Usage:
    uv run python examples/water_masks/build_water_stack.py --aoi ucayali_santa_maria
    uv run python examples/water_masks/build_water_stack.py --aoi lake_mead \
        --boa-offset-from 2022-02
"""

from __future__ import annotations

import argparse
import csv
import itertools
import shutil
import struct
import time
import zlib
from collections.abc import Iterator
from dataclasses import asdict, dataclass, fields
from pathlib import Path

import numpy as np

import chronozarr
from chronozarr.schema import Band

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"

NDWI_SCALE = 1e-4
NDWI_LIMIT = 10_000  # |stored ndwi| <= 10000
DARK_DN = 5  # green and nir both at or below this DN (reflectance 0.0005) mark DN-floor water
BOA_OFFSET = 1000  # Sentinel-2 L2A processing baseline >= 04.00
GREEN, NIR, BLUE = "B03", "B08", "B02"
BANDS = [
    Band("ndwi", scale=NDWI_SCALE, offset=0.0, units="index"),
    Band("water", scale=NDWI_SCALE, offset=0.0, units="fraction"),
]


@dataclass(frozen=True)
class Grid:
    shape: tuple[int, int]  # (y, x)
    transform: tuple[float, ...]
    epsg: int


@dataclass
class Month:
    """One mosaic: the file, its time, and how many pixels any scene observed."""

    path: Path
    time: np.datetime64
    valid_px: int

    @property
    def label(self) -> str:
        return self.path.stem


@dataclass(frozen=True)
class Row:
    """What the CSV records about one written month."""

    month: str
    status: str
    valid_px: int
    valid_frac: float
    dark_frac: float
    otsu_threshold: float
    threshold: float
    floored: int
    eta: float
    water_frac: float
    water_frac_otsu: float
    water_km2: float
    blue_median_dn: int


@dataclass
class Derived:
    """A month's store planes and its CSV row."""

    stack: np.ndarray  # (2, y, x) int16: ndwi, water (0 or 10000)
    mask: np.ndarray  # (y, x) uint8
    coverage: np.ndarray  # (y, x) uint8
    row: Row
    blue: np.ndarray  # (y, x) uint16, for quicklooks
    green: np.ndarray
    red: np.ndarray


def read_grid(npz: np.lib.npyio.NpzFile) -> Grid:
    coverage = npz["coverage"]
    return Grid(
        shape=(int(coverage.shape[0]), int(coverage.shape[1])),
        transform=tuple(float(v) for v in npz["transform"]),
        epsg=int(npz["epsg"]),
    )


def scan(paths: list[Path]) -> tuple[Grid, list[Month]]:
    """Grid of the AOI and the valid-pixel count of every month, reading only `coverage`."""
    months: list[Month] = []
    grid: Grid | None = None
    for path in paths:
        with np.load(path, allow_pickle=False) as npz:
            here = read_grid(npz)
            if grid is None:
                grid = here
            elif here != grid:
                raise SystemExit(f"{path} has a different grid or CRS than {paths[0]}: {here}")
            months.append(
                Month(
                    path=path,
                    time=np.datetime64(f"{path.stem}-01", "s"),
                    valid_px=int(np.count_nonzero(npz["coverage"] > 0)),
                )
            )
    assert grid is not None
    return grid, months


def load_month(
    path: Path, boa_offset_from: str | None
) -> tuple[dict[str, np.ndarray], np.ndarray]:
    """The month's bands by name (uint16 DN, offset-corrected when asked) and its coverage."""
    with np.load(path, allow_pickle=False) as npz:
        names = [str(b) for b in npz["band_names"]]
        bands = npz["bands"]
        coverage = npz["coverage"]
    missing = {GREEN, NIR, BLUE} - set(names)
    if missing:
        raise SystemExit(f"{path} lacks bands {sorted(missing)}; it has {names}")
    if boa_offset_from is not None and path.stem >= boa_offset_from:
        shifted = np.maximum(bands.astype(np.int32) - BOA_OFFSET, 1)
        bands = np.where(bands > 0, shifted, 0).astype(np.uint16)
    return {name: bands[i] for i, name in enumerate(names)}, coverage


def otsu(counts: np.ndarray) -> tuple[int, float]:
    """Otsu threshold of a histogram with one bin per stored ndwi value, and its separability.

    `counts[i]` is the number of pixels whose stored ndwi is `i - NDWI_LIMIT`. Class 0 is
    `value <= threshold`, class 1 is `value > threshold`. When several thresholds tie (an empty
    gap between two modes) the middle of the tie is returned. Separability eta is the maximum
    between-class variance over the total variance: near 1 for two clean modes, lower when the
    split cuts through a single mode.
    """
    values = np.arange(-NDWI_LIMIT, NDWI_LIMIT + 1, dtype=np.float64)
    counts = counts.astype(np.float64)
    below = np.cumsum(counts)
    total = below[-1]
    above = total - below
    below_sum = np.cumsum(counts * values)
    mean = below_sum[-1] / total
    with np.errstate(divide="ignore", invalid="ignore"):
        mean_below = below_sum / below
        mean_above = (below_sum[-1] - below_sum) / above
        between = below * above * (mean_below - mean_above) ** 2 / total**2
    between = np.nan_to_num(between, nan=0.0)
    best = between.max()
    ties = np.flatnonzero(between == best)
    threshold = int(ties[len(ties) // 2]) - NDWI_LIMIT
    variance = float(counts @ (values - mean) ** 2) / total
    return threshold, float(best / variance) if variance > 0 else 0.0


def derive(month: Month, grid: Grid, boa_offset_from: str | None, floor: int) -> Derived:
    bands, coverage = load_month(month.path, boa_offset_from)
    valid = coverage > 0
    green, nir = bands[GREEN], bands[NIR]
    unobserved = valid & ((green == 0) | (nir == 0) | (bands[BLUE] == 0))
    if unobserved.any():
        raise SystemExit(
            f"{month.path}: coverage > 0 where a band is 0 at {int(unobserved.sum())} pixels; "
            "the mosaic is inconsistent, so there is no safe validity rule"
        )
    n_valid = int(valid.sum())
    if n_valid == 0:
        raise SystemExit(f"{month.path} has no valid pixel; the scan should have skipped it")

    ndwi = np.zeros(valid.shape, dtype=np.int16)
    g = green[valid].astype(np.float64)
    n = nir[valid].astype(np.float64)
    ndwi[valid] = np.rint((g - n) / (g + n) * NDWI_LIMIT).astype(np.int16)
    dark = valid & (green <= DARK_DN) & (nir <= DARK_DN)
    fitted = valid & ~dark
    if not fitted.any():
        raise SystemExit(
            f"{month.path}: every valid pixel is at the DN floor; no ndwi to threshold"
        )
    counts = np.bincount(ndwi[fitted].astype(np.int64) + NDWI_LIMIT, minlength=2 * NDWI_LIMIT + 1)
    otsu_threshold, eta = otsu(counts)
    threshold = max(otsu_threshold, floor)
    water = valid & (dark | (ndwi > threshold))
    n_water = int(water.sum())
    n_water_otsu = int(np.count_nonzero(valid & (dark | (ndwi > otsu_threshold))))
    pixel_area_km2 = abs(grid.transform[0] * grid.transform[4]) / 1e6
    mask = valid.astype(np.uint8)
    return Derived(
        stack=np.stack([ndwi, water.astype(np.int16) * np.int16(NDWI_LIMIT)]),
        mask=mask,
        coverage=mask.copy(),
        row=Row(
            month=month.label,
            status="written",
            valid_px=n_valid,
            valid_frac=round(n_valid / valid.size, 4),
            dark_frac=round(int(dark.sum()) / n_valid, 5),
            otsu_threshold=round(otsu_threshold * NDWI_SCALE, 4),
            threshold=round(threshold * NDWI_SCALE, 4),
            floored=int(otsu_threshold < floor),
            eta=round(eta, 3),
            water_frac=round(n_water / n_valid, 5),
            water_frac_otsu=round(n_water_otsu / n_valid, 5),
            water_km2=round(n_water * pixel_area_km2, 3),
            blue_median_dn=int(np.median(bands[BLUE][valid][::16])),
        ),
        blue=bands[BLUE],
        green=green,
        red=bands["B04"],
    )


# --- Quicklooks (stdlib PNG: matplotlib is not a dependency of the repo) ----------------------


def write_png(path: Path, rgb: np.ndarray) -> None:
    """Write an (h, w, 3) uint8 array as a PNG."""
    height, width, _ = rgb.shape
    raw = np.concatenate([np.zeros((height, 1), dtype=np.uint8), rgb.reshape(height, -1)], axis=1)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6))
        + chunk(b"IEND", b"")
    )


def block_mean(
    values: np.ndarray, valid: np.ndarray, factor: int
) -> tuple[np.ndarray, np.ndarray]:
    """Mean of the valid values in each factor x factor block, and whether any was valid."""
    h, w = (n // factor * factor for n in values.shape)
    v = valid[:h, :w].reshape(h // factor, factor, w // factor, factor)
    x = np.where(valid, values, 0).astype(np.float64)[:h, :w]
    x = x.reshape(h // factor, factor, w // factor, factor)
    count = v.sum(axis=(1, 3))
    return x.sum(axis=(1, 3)) / np.maximum(count, 1), count > 0


def quicklook(derived: Derived, path: Path) -> None:
    """True colour | ndwi (brown -1, white 0, blue +1) | water over land, side by side.

    Pixels are block means over valid pixels at 1/4 or so of the native resolution. Masked blocks
    are dark grey in every panel.
    """
    valid = derived.mask.astype(bool)
    factor = max(1, -(-max(valid.shape) // 700))
    grey = np.array([60, 60, 60], dtype=np.float64)

    def finish(rgb: np.ndarray, any_valid: np.ndarray) -> np.ndarray:
        return np.where(any_valid[..., None], rgb, grey).astype(np.uint8)

    channels = []
    for dn in (derived.red, derived.green, derived.blue):
        mean, any_valid = block_mean(dn, valid, factor)
        channels.append(255 * np.clip(mean / 3000.0, 0, 1) ** (1 / 2.2))
    true_colour = finish(np.stack(channels, axis=-1), any_valid)

    ndwi, any_valid = block_mean(derived.stack[0] * NDWI_SCALE, valid, factor)
    t = np.clip(ndwi, -1, 1)[..., None]
    brown, white, blue = (
        np.array(c, dtype=np.float64) for c in ((140, 90, 40), (245, 245, 245), (20, 70, 200))
    )
    ndwi_rgb = finish(
        np.where(t < 0, white + (brown - white) * -t, white + (blue - white) * t), any_valid
    )

    fraction, any_valid = block_mean(derived.stack[1] * NDWI_SCALE, valid, factor)
    land, water = (
        np.array([225, 225, 225], dtype=np.float64),
        np.array([20, 90, 200], dtype=np.float64),
    )
    water_rgb = finish(land + (water - land) * fraction[..., None], any_valid)

    gap = np.full((true_colour.shape[0], 8, 3), 255, dtype=np.uint8)
    write_png(path, np.concatenate([true_colour, gap, ndwi_rgb, gap, water_rgb], axis=1))


def pick_quicklook_months(rows: list[Row]) -> list[str]:
    """Months worth looking at: wettest and driest of the well observed ones, where the floor
    changes the water fraction most, the most DN-floor water, the least observed above 1 %."""
    observed = [r for r in rows if r.valid_frac >= 0.9] or rows
    sparse = [r for r in rows if r.valid_frac >= 0.01] or rows
    chosen = [
        max(observed, key=lambda r: r.water_frac),
        min(observed, key=lambda r: r.water_frac),
        max(rows, key=lambda r: abs(r.water_frac - r.water_frac_otsu)),
        min(sparse, key=lambda r: r.valid_frac),
    ]
    darkest = max(rows, key=lambda r: r.dark_frac)
    if darkest.dark_frac >= 0.01:
        chosen.append(darkest)
    return list(dict.fromkeys(r.month for r in chosen))


# --- Build ------------------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--aoi", required=True)
    parser.add_argument(
        "--store-name", default="water-1", help="directory under data/stores/<aoi>/"
    )
    parser.add_argument("--mosaic-root", type=Path, default=DATA / "mosaics")
    parser.add_argument("--out-root", type=Path, default=DATA / "stores")
    parser.add_argument("--reports-dir", type=Path, default=DATA / "reports")
    parser.add_argument("--months", type=int, default=None, help="use only the first N mosaics")
    parser.add_argument(
        "--boa-offset-from",
        metavar="YYYY-MM",
        help="subtract the +1000 DN baseline offset from here on",
    )
    parser.add_argument(
        "--quicklook",
        nargs="+",
        metavar="YYYY-MM",
        help="months to draw (default: four chosen from the CSV)",
    )
    parser.add_argument(
        "--floor",
        type=float,
        default=0.0,
        help="lowest NDWI threshold a month may use (default 0.0; Ucayali: -0.15)",
    )
    parser.add_argument(
        "--shard",
        action="store_true",
        help="one shard object per (time shard, cell) instead of one chunk object per (timestep, "
        "cell); default off",
    )
    parser.add_argument("--overwrite", action="store_true", help="replace an existing store")
    args = parser.parse_args()
    floor = round(args.floor / NDWI_SCALE)
    if not -NDWI_LIMIT <= floor <= NDWI_LIMIT:
        raise SystemExit(f"--floor {args.floor} is outside the NDWI range -1..1")

    paths = sorted((args.mosaic_root / args.aoi).glob("*.npz"))
    if not paths:
        raise SystemExit(f"no .npz mosaics in {args.mosaic_root / args.aoi}; run the ingest first")
    if args.months:
        paths = paths[: args.months]
    out = args.out_root / args.aoi / args.store_name
    csv_path = out.parent / f"{args.store_name}.months.csv"
    if out.exists():
        if not args.overwrite:
            raise SystemExit(f"{out} exists; pass --overwrite to replace it")
        shutil.rmtree(out)
    started = time.perf_counter()
    grid, months = scan(paths)
    kept = [m for m in months if m.valid_px > 0]
    skipped = [m for m in months if m.valid_px == 0]
    print(
        f"{len(months)} mosaics, {len(kept)} with a valid pixel; skipped: "
        f"{', '.join(m.label for m in skipped) or 'none'}; grid {grid.shape} EPSG:{grid.epsg}"
    )
    if not kept:
        raise SystemExit(
            f"no mosaic of {args.aoi} has a valid pixel (coverage > 0 everywhere is 0)"
        )

    rows: list[Row] = []

    def products() -> Iterator[Derived]:
        for month in kept:
            derived = derive(month, grid, args.boa_offset_from, floor)
            rows.append(derived.row)
            yield derived

    data_view, mask_view, coverage_view = itertools.tee(products(), 3)
    scan_s = time.perf_counter() - started
    notes = (
        "ndwi = (B03 - B08) / (B03 + B08) stored x10000 (int16, scale 1e-4); water = ndwi > "
        f"max(per-month Otsu threshold, {args.floor}), or 1 where B03 and B08 are both <= "
        f"{DARK_DN} DN (L2A floor, ndwi undefined), stored as a fraction (0 or 10000, scale 1e-4) "
        "so pyramid block means are water fractions. mask = 0 where no scene was valid "
        "(carry-forward or never observed), values there are 0. "
        "coverage = 1 where at least one scene was valid. "
        "Derived from monthly median mosaics (SCL classes 4, 5, 6, 7, 11 kept)."
    )
    if args.boa_offset_from:
        notes += f" 1000 DN baseline offset subtracted from months >= {args.boa_offset_from}."
    encode_started = time.perf_counter()
    report = chronozarr.encode(
        (d.stack for d in data_view),
        out,
        times=np.array([m.time for m in kept]),
        bands=BANDS,
        crs=f"EPSG:{grid.epsg}",
        transform=grid.transform,
        mask=(d.mask for d in mask_view),
        coverage=(d.coverage for d in coverage_view),
        shard=args.shard,
        provenance={
            "sources": ["sentinel-2-l2a"],
            "composite": "monthly median reflectance; NDWI and water derived per month",
            "gap_fill": "none",
            "notes": notes,
        },
    )
    encode_s = time.perf_counter() - encode_started

    by_month = {r.month: r for r in rows}
    with csv_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[f.name for f in fields(Row)])
        writer.writeheader()
        for month in months:
            row = by_month.get(month.label)
            writer.writerow(
                asdict(row)
                if row
                else {"month": month.label, "status": "skipped: no valid pixel", "valid_px": 0}
            )

    args.reports_dir.mkdir(parents=True, exist_ok=True)
    look = args.quicklook or pick_quicklook_months(rows)
    by_label = {m.label: m for m in months}
    for label in look:
        month = by_label[label]
        png = args.reports_dir / f"water_{args.aoi}_{label}.png"
        derived = derive(month, grid, args.boa_offset_from, floor)
        if derived.row != by_month[label]:
            raise SystemExit(f"{label}: recomputing the month gave a different row than the build")
        quicklook(derived, png)
        print(f"quicklook {png}")

    floored = sum(r.floored for r in rows)
    raw_bytes = len(kept) * 2 * grid.shape[0] * grid.shape[1] * 2
    print(f"\nencoding: {report.encoding}; codec {report.codec} level {report.level}")
    if report.selection:
        print(f"  auto selection: {report.selection}")
    else:
        print("  no auto measurement (int16 stores are always written plain; spec 4.3)")
    print(f"levels: {len(report.levels)}")
    for lvl in report.levels:
        print(
            f"  lod {lvl.level}: {'x'.join(str(n) for n in lvl.shape)}  {lvl.bytes / 1e6:8.1f} MB"
        )
    print(
        f"store: {report.total_bytes / 1e6:.1f} MB in {report.n_files} files "
        f"({raw_bytes / report.total_bytes:.2f}x vs {raw_bytes / 1e6:.0f} MB raw int16 bands)"
    )
    print(f"floor binding in {floored} of {len(rows)} months")
    print(f"time: scan {scan_s:.1f}s, derive + encode {encode_s:.1f}s")
    problems = chronozarr.validate(out)
    print("validate: " + ("conforms" if not problems else f"{len(problems)} problem(s)"))
    for problem in problems:
        print(f"  {problem}")
    print(f"wrote {out} and {csv_path}")
    if problems:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
