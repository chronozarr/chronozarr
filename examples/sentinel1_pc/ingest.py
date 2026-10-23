r"""Ingest a Sentinel-1 RTC backscatter time series from Planetary Computer as monthly composites.

One box (--bbox, WGS84) and one relative orbit: every acquisition sees the box from the same
geometry, so the series compares like with like. The scenes are Planetary Computer's
`sentinel-1-rtc` items (IW GRD, VV+VH, gamma0 power with radiometric terrain correction, float32
on a 10 m grid in the scene's UTM zone). By default the relative orbit with the most acquisition
dates over the box is taken; --orbit pins one. The slices of one pass (one absolute orbit) are
mosaicked into one acquisition; where slices overlap, the earlier slice is kept.

Grid. The box is snapped outwards to whole 10 m pixels in the UTM zone of its centre, as the
Sentinel-2 ingest builds its grid (examples/sentinel2_pc), and the store's grid is that grid
coarsened 2x: 20 m pixels from the same corner, so the store lines up with level 1 of the
Sentinel-2 store of the same box. Each scene is read as a window of its own 10 m grid, with no
warp; a scene in another CRS, or one not offset from the grid by whole pixels, stops the run
before anything is read.

Values. A 10 m sample is valid when VV and VH are both finite, above zero and not the product's
nodata (-32768). Each 20 m pixel is the mean linear power of its 2 x 2 samples (a 2 x 2
multilook; IW GRD resolution is about 20 x 22 m) and is valid when all four are. A month is the
mean linear power of that month's acquisitions that are valid at the pixel, valid where at least
one is. VV and VH are stored as 10 * log10(power) in dB at 0.01 dB as uint16: stored =
(dB + 60) * 100, so the band metadata (scale 0.01, offset -60) gives dB back; values below
-60 dB are clipped to -60 dB. Invalid pixels store 0 and the store's mask is 0 there. Coarser
levels average dB (the spec's mean pyramid), the geometric mean of power. A month without an
acquisition is left out of the time axis and named in the manifest; only complete months are
composited, so --end defaults to the last day of the previous month.

Usage:
    uv run --extra ingest python examples/sentinel1_pc/ingest.py \
        --bbox -75.13 -7.76 -74.88 -7.51 --name ucayali_santa_maria --dry-run
    uv run --extra ingest python examples/sentinel1_pc/ingest.py \
        --bbox -75.13 -7.76 -74.88 -7.51 --name ucayali_santa_maria --start 2017-01-01

Output: <out-dir>/stores/<name>/<store> (the store; `--store` names it, s1-rtc-monthly-v1 by
default, and a re-encode takes a new name because a published store is immutable),
<out-dir>/stores/<name>/<store>.scenes.json (the manifest: query, orbit, grid, every acquisition
and every month) and <out-dir>/cache/sentinel1/<name>/<slice id>.npz (the 20 m VV and VH power
of each acquisition with its manifest record beside it, which a rerun reuses instead of reading
the scenes again).
"""

from __future__ import annotations

import argparse
import ctypes
import functools
import json
import logging
import math
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np

import chronozarr
from chronozarr.schema import Band

ROOT = Path(__file__).resolve().parents[2]
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
COLLECTION = "sentinel-1-rtc"
STORE_NAME = "s1-rtc-monthly-v1"
PIXEL = 10.0  # metres, the product grid
LOOKS = 2  # 2 x 2 multilook: 20 m store pixels
NODATA = -32768.0
DB_SCALE = 0.01
DB_OFFSET = -60.0
ATTEMPTS = 2
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tiff",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "GDAL_HTTP_CONNECTTIMEOUT": "30",
    "GDAL_HTTP_LOW_SPEED_TIME": "60",
    "GDAL_HTTP_LOW_SPEED_LIMIT": "1024",
}

logger = logging.getLogger("sentinel1_ingest")

BBox = tuple[float, float, float, float]


@dataclass(frozen=True)
class Grid:
    """The store's grid: `width` x `height` pixels of LOOKS * PIXEL metres from (left, top)."""

    epsg: int
    left: float
    top: float
    width: int
    height: int

    @property
    def transform(self) -> list[float]:
        size = PIXEL * LOOKS
        return [size, 0.0, self.left, 0.0, -size, self.top]

    @property
    def spatial_identity(self) -> dict:
        """The cache's grid identity, with the same types before and after JSON storage."""
        return {
            "crs": f"EPSG:{self.epsg}",
            "transform": self.transform,
            "width": self.width,
            "height": self.height,
        }


@dataclass(frozen=True)
class Slice:
    """One sentinel-1-rtc item: a slice of one pass."""

    id: str
    start: str  # ISO 8601 UTC
    platform: str
    absolute_orbit: int
    relative_orbit: int
    direction: str  # ascending or descending
    epsg: int
    transform: tuple[float, float, float, float, float, float]
    shape: tuple[int, int]  # rows, columns
    vv: str  # asset URLs, unsigned
    vh: str


@dataclass(frozen=True)
class Acquisition:
    """The slices of one pass over the box, in time order."""

    platform: str
    absolute_orbit: int
    slices: tuple[Slice, ...]

    @property
    def start(self) -> str:
        return self.slices[0].start

    @property
    def key(self) -> str:
        """The first slice's id: names the cached window."""
        return self.slices[0].id


def utm_epsg(bbox: BBox) -> int:
    """EPSG code of the standard UTM zone of the box centre, as the Sentinel-2 ingest picks it."""
    west, south, east, north = bbox
    zone = int(((west + east) / 2 + 180) // 6) + 1
    return (32600 if (south + north) / 2 >= 0 else 32700) + zone


def grid_for(bbox: BBox) -> Grid:
    """The box snapped outwards to whole 10 m pixels, coarsened LOOKS times from its top-left."""
    from rasterio.crs import CRS  # ty: ignore[unresolved-import]  (compiled module, no stubs)
    from rasterio.warp import transform_bounds

    epsg = utm_epsg(bbox)
    left, bottom, right, top = transform_bounds(CRS.from_epsg(4326), CRS.from_epsg(epsg), *bbox)
    left = math.floor(left / PIXEL) * PIXEL
    bottom = math.floor(bottom / PIXEL) * PIXEL
    right = math.ceil(right / PIXEL) * PIXEL
    top = math.ceil(top / PIXEL) * PIXEL
    width = round((right - left) / PIXEL)
    height = round((top - bottom) / PIXEL)
    return Grid(epsg, left, top, math.ceil(width / LOOKS), math.ceil(height / LOOKS))


def parse_item(item_id: str, properties: dict, assets: dict) -> Slice | None:
    """A Slice from a STAC item, or None when it is not an IW scene with VV and VH."""
    if properties.get("sar:instrument_mode") != "IW":
        return None
    if not {"VV", "VH"} <= set(properties.get("sar:polarizations", ())):
        return None
    if "vv" not in assets or "vh" not in assets:
        return None
    code = properties.get("proj:code") or f"EPSG:{properties['proj:epsg']}"
    a, b, c, d, e, f = (float(v) for v in properties["proj:transform"][:6])
    rows, cols = properties["proj:shape"]
    return Slice(
        id=item_id,
        start=properties.get("start_datetime") or properties["datetime"],
        platform=properties["platform"].lower(),
        absolute_orbit=int(properties["sat:absolute_orbit"]),
        relative_orbit=int(properties["sat:relative_orbit"]),
        direction=properties["sat:orbit_state"],
        epsg=int(code.removeprefix("EPSG:")),
        transform=(a, b, c, d, e, f),
        shape=(int(rows), int(cols)),
        vv=assets["vv"]["href"],
        vh=assets["vh"]["href"],
    )


def discover(bbox: BBox, start: str, end: str) -> list[Slice]:
    import pystac_client

    catalog = pystac_client.Client.open(STAC_URL)
    search = catalog.search(collections=[COLLECTION], bbox=list(bbox), datetime=f"{start}/{end}")
    slices = {}
    for item in search.items_as_dicts():
        parsed = parse_item(item["id"], item["properties"], item["assets"])
        if parsed is not None:
            slices[parsed.id] = parsed
    return list(slices.values())


def select_series(
    slices: list[Slice], orbit: int | None = None
) -> tuple[tuple[int, str], list[Acquisition]]:
    """One relative orbit's acquisitions in time order, each the slices of one pass.

    Without a pin, the relative orbit with the most distinct acquisition dates wins.
    """
    pinned = [s for s in slices if orbit is None or s.relative_orbit == orbit]
    if not pinned:
        raise SystemExit(f"no Sentinel-1 RTC scene on relative orbit {orbit} over the box")
    passes: dict[tuple, list[Slice]] = defaultdict(list)
    for s in pinned:
        passes[(s.relative_orbit, s.direction, s.platform, s.absolute_orbit)].append(s)
    dates: dict[tuple[int, str], set[str]] = defaultdict(set)
    for (relative, direction, _, _), group in passes.items():
        dates[(relative, direction)].add(min(s.start for s in group)[:10])
    series = max(dates, key=lambda k: (len(dates[k]), -k[0]))
    acquisitions = [
        Acquisition(platform, absolute, tuple(sorted(group, key=lambda s: s.start)))
        for (relative, direction, platform, absolute), group in passes.items()
        if (relative, direction) == series
    ]
    return series, sorted(acquisitions, key=lambda a: a.start)


def misaligned(grid: Grid, s: Slice) -> str | None:
    """Why the slice cannot be read as a window on the grid, or None when it can."""
    a, b, c, d, e, f = s.transform
    if s.epsg != grid.epsg:
        return f"{s.id}: EPSG:{s.epsg}, the grid is EPSG:{grid.epsg}"
    if (a, b, d, e) != (PIXEL, 0.0, 0.0, -PIXEL):
        return f"{s.id}: not a north-up {PIXEL:g} m grid ({a}, {b}, {d}, {e})"
    col = (grid.left - c) / PIXEL
    row = (f - grid.top) / PIXEL
    if col != round(col) or row != round(row):
        return f"{s.id}: offset from the grid by a fraction of a pixel ({col}, {row})"
    return None


def stac_problems(grid: Grid, slices: list[Slice]) -> tuple[list[str], list[Slice]]:
    """Slices that cannot be read on the grid by their STAC metadata, and slices whose
    proj:transform is not a north-up 10 m grid at all (seen on Planetary Computer items whose
    files are north-up), left to the check on the file header when they are read."""
    problems, deferred = [], []
    for s in slices:
        a, b, _, d, e, _ = s.transform
        if s.epsg == grid.epsg and (a, b, d, e) != (PIXEL, 0.0, 0.0, -PIXEL):
            deferred.append(s)
        elif (problem := misaligned(grid, s)) is not None:
            problems.append(problem)
    return problems, deferred


def window_of(grid: Grid, s: Slice) -> tuple[slice, slice, slice, slice] | None:
    """(source rows, source columns, target rows, target columns) of the box's 10 m window in
    the slice, or None when they do not overlap."""
    _, _, c, _, _, f = s.transform
    height, width = grid.height * LOOKS, grid.width * LOOKS
    col0 = round((grid.left - c) / PIXEL)
    row0 = round((f - grid.top) / PIXEL)
    r0, r1 = max(row0, 0), min(row0 + height, s.shape[0])
    c0, c1 = max(col0, 0), min(col0 + width, s.shape[1])
    if r0 >= r1 or c0 >= c1:
        return None
    return slice(r0, r1), slice(c0, c1), slice(r0 - row0, r1 - row0), slice(c0 - col0, c1 - col0)


@functools.cache
def _gdal_clear_cache() -> Callable[[bytes], None] | None:
    """GDAL's VSICurlPartialClearCache from the libgdal that rasterio loaded, or None."""
    import rasterio

    package = Path(rasterio.__file__).parent
    candidates: list[str | None] = [None]  # None: symbols already visible in this process
    for directory in (package.parent / "rasterio.libs", package / ".dylibs", package / ".libs"):
        candidates += sorted(str(p) for p in directory.glob("*gdal*") if p.is_file())
    for candidate in candidates:
        try:
            function = ctypes.CDLL(candidate).VSICurlPartialClearCache
        except (OSError, AttributeError, TypeError):
            continue
        function.argtypes = [ctypes.c_char_p]
        function.restype = None
        return function
    logger.warning("GDAL's VSICurlPartialClearCache is not reachable; a retry may not re-read")
    return None


def forget_url(url: str) -> None:
    """Drop GDAL's record of a failed open of `url`, which it keeps for the life of the process
    (see `forget_url` in examples/sentinel2_pc/mosaic.py), so a retry reaches the host."""
    function = _gdal_clear_cache()
    if function is not None:
        function(f"/vsicurl/{url}".encode())


def read_band(href: str, s: Slice, grid: Grid) -> np.ndarray:
    """Linear power of one polarization on the box's 10 m window; NaN where the slice has no
    valid sample."""
    import planetary_computer
    import rasterio
    from rasterio.errors import RasterioIOError

    out = np.full((grid.height * LOOKS, grid.width * LOOKS), np.nan, dtype=np.float32)
    url = planetary_computer.sign(href)
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with rasterio.Env(**GDAL_ENV), rasterio.open(url) as src:
                # The file's own grid: some items' proj:transform does not describe the file.
                on_file = replace(
                    s, epsg=src.crs.to_epsg(), transform=tuple(src.transform)[:6], shape=src.shape
                )
                problem = misaligned(grid, on_file)
                if problem is not None:
                    raise RuntimeError(problem)
                window = window_of(grid, on_file)
                if window is None:
                    return out
                rows, cols, out_rows, out_cols = window
                values = src.read(1, window=((rows.start, rows.stop), (cols.start, cols.stop)))
            break
        except RasterioIOError as error:
            if attempt == ATTEMPTS:
                raise RuntimeError(f"{s.id}: read failed {ATTEMPTS} times: {error}") from error
            logger.warning("%s: %s; reading again", s.id, error)
            forget_url(url)
            url = planetary_computer.sign(href)
    values[(values == NODATA) | ~np.isfinite(values) | (values <= 0)] = np.nan
    out[out_rows, out_cols] = values
    return out


def multilook(power: np.ndarray) -> np.ndarray:
    """Mean of each LOOKS x LOOKS block; NaN where any sample of the block is NaN."""
    h, w = power.shape
    return power.reshape(h // LOOKS, LOOKS, w // LOOKS, LOOKS).mean(axis=(1, 3))


def read_acquisition(acquisition: Acquisition, grid: Grid) -> tuple[np.ndarray, np.ndarray]:
    """VV and VH power of one pass on the store's grid, NaN where invalid."""
    shape = (grid.height * LOOKS, grid.width * LOOKS)
    vv = np.full(shape, np.nan, dtype=np.float32)
    vh = np.full(shape, np.nan, dtype=np.float32)
    for s in acquisition.slices:
        slice_vv = read_band(s.vv, s, grid)
        slice_vh = read_band(s.vh, s, grid)
        take = np.isnan(vv) & np.isfinite(slice_vv) & np.isfinite(slice_vh)
        vv[take] = slice_vv[take]
        vh[take] = slice_vh[take]
    return multilook(vv), multilook(vh)


def db_percentiles(power: np.ndarray, valid: np.ndarray) -> list[float]:
    return [round(float(v), 2) for v in 10 * np.log10(np.percentile(power[valid], [2, 50, 98]))]


def extract(acquisition: Acquisition, grid: Grid, out: Path) -> dict:
    """Read one pass into `out` (.npz, 20 m power) and return its record for the manifest."""
    t0 = time.perf_counter()
    vv, vh = read_acquisition(acquisition, grid)
    valid = np.isfinite(vv)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez(out, vv=vv, vh=vh)
    record = {
        "start": acquisition.start,
        "platform": acquisition.platform,
        "absolute_orbit": acquisition.absolute_orbit,
        "slices": [s.id for s in acquisition.slices],
        "grid": grid.spatial_identity,
        "valid_fraction": float(valid.mean()),
        "read_seconds": round(time.perf_counter() - t0, 1),
    }
    if valid.any():
        record["VV_dB_p2_p50_p98"] = db_percentiles(vv, valid)
        record["VH_dB_p2_p50_p98"] = db_percentiles(vh, valid)
    # The sidecar names the window file, not its path: the cache directory may move.
    sidecar_of(out).write_text(json.dumps({**record, "npz": out.name}, indent=2))
    return {**record, "npz": str(out)}


def sidecar_of(npz: Path) -> Path:
    """The manifest record of a cached acquisition, written beside it."""
    return npz.with_suffix(".json")


def cached_record(acquisition: Acquisition, grid: Grid, path: Path) -> dict | None:
    """The record of an acquisition on this grid with the same slices, or None if uncached."""
    if not path.is_file():
        return None
    sidecar = sidecar_of(path)
    record = json.loads(sidecar.read_text()) if sidecar.is_file() else {}
    if record.get("grid") != grid.spatial_identity:
        raise SystemExit(
            f"{path}: cached acquisition has missing or different spatial identity; "
            "use another --name or --out-dir for a new cache"
        )
    if record["slices"] != [s.id for s in acquisition.slices]:
        return None
    return {**record, "npz": str(path), "cached": True}


def extract_all(
    acquisitions: list[Acquisition], grid: Grid, cache: Path, workers: int
) -> list[dict]:
    records: dict[str, dict] = {}
    pending = []
    for acquisition in acquisitions:
        path = cache / f"{acquisition.key}.npz"
        record = cached_record(acquisition, grid, path)
        if record is None:
            pending.append((acquisition, path))
        else:
            records[acquisition.key] = record
    logger.info("%d acquisitions cached, %d to read", len(records), len(pending))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(extract, a, grid, path): a for a, path in pending}
        for done, future in enumerate(as_completed(futures), start=1):
            acquisition = futures[future]
            record = future.result()
            records[acquisition.key] = record
            logger.info(
                "%d/%d %s %s: valid %.1f %%, %s s",
                done,
                len(pending),
                acquisition.start[:10],
                acquisition.platform,
                100 * record["valid_fraction"],
                record["read_seconds"],
            )
    return [records[a.key] for a in acquisitions]


def stored_db(power: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """uint16 dB at 0.01 dB (stored = (dB + 60) * 100, clipped at -60 dB); 0 where invalid."""
    db = np.zeros(power.shape, dtype=np.float32)
    np.log10(power, out=db, where=valid)
    stored = np.zeros(power.shape, dtype=np.uint16)
    quantized = np.rint((db[valid] * 10.0 - DB_OFFSET) / DB_SCALE)
    stored[valid] = np.clip(quantized, 0, np.iinfo(np.uint16).max).astype(np.uint16)
    return stored


def month_mean(stacks: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, ...]:
    """Mean VV and VH power over the acquisitions valid at each pixel, and the validity."""
    shape = stacks[0][0].shape
    total_vv = np.zeros(shape, dtype=np.float64)
    total_vh = np.zeros(shape, dtype=np.float64)
    count = np.zeros(shape, dtype=np.int32)
    for vv, vh in stacks:
        ok = np.isfinite(vv) & np.isfinite(vh)
        total_vv[ok] += vv[ok]
        total_vh[ok] += vh[ok]
        count[ok] += 1
    valid = count > 0
    divisor = np.maximum(count, 1)
    return total_vv / divisor, total_vh / divisor, valid


def month_range(start: str, end: str) -> list[str]:
    """Every YYYY-MM from the month of `start` to the month of `end`."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    months = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def composite_months(records: list[dict], months_dir: Path) -> list[dict]:
    """Write each month's stored VV, VH and mask to `months_dir`; one summary per month."""
    by_month: dict[str, list[dict]] = defaultdict(list)
    for record in records:
        if record["valid_fraction"] > 0:
            by_month[record["start"][:7]].append(record)
    months_dir.mkdir(parents=True, exist_ok=True)
    summaries = []
    for month in sorted(by_month):
        stacks = []
        for record in by_month[month]:
            with np.load(record["npz"]) as z:
                stacks.append((z["vv"], z["vh"]))
        vv, vh, valid = month_mean(stacks)
        path = months_dir / f"{month}.npz"
        np.savez(path, VV=stored_db(vv, valid), VH=stored_db(vh, valid), mask=valid)
        summary = {
            "month": month,
            "acquisitions": [r["start"] for r in by_month[month]],
            "valid_fraction": float(valid.mean()),
            "npz": str(path),
        }
        if valid.any():
            summary["VV_dB_p2_p50_p98"] = db_percentiles(vv, valid)
            summary["VH_dB_p2_p50_p98"] = db_percentiles(vh, valid)
        summaries.append(summary)
    return summaries


def encode_store(months: list[dict], grid: Grid, store: Path, manifest: dict) -> None:
    if not months:
        raise SystemExit("no acquisition holds a valid pixel in the box")
    times = np.array([f"{m['month']}-01" for m in months], dtype="datetime64[ns]")

    def frames():
        for month in months:
            with np.load(month["npz"]) as z:
                yield np.stack([z["VV"], z["VH"]])

    def masks():
        for month in months:
            with np.load(month["npz"]) as z:
                yield z["mask"].astype(np.uint8)

    series = manifest["series"]
    empty = manifest["empty_months"]
    notes = (
        f"Planetary Computer {COLLECTION} (IW GRD, gamma0 RTC), relative orbit "
        f"{series['relative_orbit']} {series['direction']}, read on the product's 10 m UTM grid "
        "with no warp. Each pixel is a 2x2 multilook (mean linear power) at 20 m, valid when all "
        "four samples are; each month is the mean linear power of its acquisitions valid at the "
        "pixel. VV and VH are 10*log10(power) in dB at 0.01 dB: stored = (dB + 60) * 100, "
        "clipped at -60 dB; invalid pixels store 0. Coarser levels average dB (geometric mean of "
        f"power). Access {manifest['access_date']}."
    )
    if empty:
        notes += f" Months without an acquisition, left out: {', '.join(empty)}."
    report = chronozarr.encode(
        frames(),
        store,
        crs=f"EPSG:{grid.epsg}",
        transform=grid.transform,
        times=times,
        bands=[
            Band(name="VV", scale=DB_SCALE, offset=DB_OFFSET, units="dB"),
            Band(name="VH", scale=DB_SCALE, offset=DB_OFFSET, units="dB"),
        ],
        mask=masks(),
        provenance={
            "sources": [COLLECTION],
            "composite": "monthly mean of linear power",
            "gap_fill": "none",
            "notes": notes,
        },
    )
    manifest["store"] = {
        "path": f"{store.parent.name}/{store.name}",
        "timesteps": len(months),
        "levels": len(report.levels),
        "files": report.n_files,
        "bytes": report.total_bytes,
    }
    logger.info(
        "Encoded %d months, %d levels: %.1f MB in %d files -> %s",
        len(months),
        len(report.levels),
        report.total_bytes / 1e6,
        report.n_files,
        store,
    )


def last_complete_month_end(today: date) -> date:
    return today.replace(day=1) - timedelta(days=1)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--bbox", nargs=4, type=float, required=True, metavar=("W", "S", "E", "N"))
    parser.add_argument("--name", required=True, help="store directory under <out-dir>/stores")
    parser.add_argument(
        "--store", default=STORE_NAME, help="store name under <out-dir>/stores/NAME"
    )
    parser.add_argument("--start", default="2017-01-01")
    parser.add_argument("--end", default=last_complete_month_end(date.today()).isoformat())
    parser.add_argument("--orbit", type=int, help="relative orbit (default: most dates)")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--workers", type=int, default=8, help="acquisitions read at once")
    parser.add_argument(
        "--dry-run", action="store_true", help="list the acquisitions and months, write nothing"
    )
    args = parser.parse_args(argv)
    west, south, east, north = args.bbox
    if not (-180 <= west < east <= 180 and -80 <= south < north <= 84):
        parser.error("--bbox is west south east north in degrees, inside the UTM latitudes")
    for option, value in (("--name", args.name), ("--store", args.store)):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            parser.error(f"{option} may use letters, digits, '_', '-' and '.'")
    for option, value in (("--start", args.start), ("--end", args.end)):
        try:
            date.fromisoformat(value)
        except ValueError:
            parser.error(f"{option} is a date, YYYY-MM-DD")
    if args.start > args.end:
        parser.error("--start is after --end")
    return args


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(message)s", datefmt="%H:%M:%S"
    )
    logger.setLevel(logging.INFO)
    args = parse_args(argv)
    bbox: BBox = (args.bbox[0], args.bbox[1], args.bbox[2], args.bbox[3])
    store = args.out_dir / "stores" / args.name / args.store
    manifest_path = store.with_name(f"{args.store}.scenes.json")
    cache = args.out_dir / "cache" / "sentinel1" / args.name
    if store.exists() and any(store.iterdir()):
        raise SystemExit(f"{store} exists; stores are immutable: another --store or --out-dir")

    t0 = time.perf_counter()
    grid = grid_for(bbox)
    slices = discover(bbox, args.start, args.end)
    if not slices:
        raise SystemExit(f"no Sentinel-1 RTC VV+VH scene over the box, {args.start}..{args.end}")
    (relative, direction), acquisitions = select_series(slices, args.orbit)
    problems, deferred = stac_problems(grid, [s for a in acquisitions for s in a.slices])
    if problems:
        raise SystemExit("scenes off the grid:\n" + "\n".join(problems[:20]))
    if deferred:
        logger.info(
            "%d slices carry a proj:transform that is not north-up; their file headers are "
            "checked when read: %s",
            len(deferred),
            ", ".join(s.id for s in deferred),
        )
    with_dates = {a.start[:7] for a in acquisitions}
    empty = [m for m in month_range(args.start, args.end) if m not in with_dates]
    logger.info(
        "%d slices over the box; relative orbit %d %s: %d acquisitions (%d slices) %s .. %s "
        "(%.1f s)",
        len(slices),
        relative,
        direction,
        len(acquisitions),
        sum(len(a.slices) for a in acquisitions),
        acquisitions[0].start[:10],
        acquisitions[-1].start[:10],
        time.perf_counter() - t0,
    )
    logger.info(
        "grid: EPSG:%d, %d x %d px at %g m from (%g, %g); %d months, %d without an acquisition",
        grid.epsg,
        grid.width,
        grid.height,
        PIXEL * LOOKS,
        grid.left,
        grid.top,
        len(with_dates),
        len(empty),
    )
    if args.dry_run:
        for a in acquisitions:
            print(a.start[:19], a.platform, a.absolute_orbit, len(a.slices), "slice(s)")
        print("months without an acquisition:", ", ".join(empty) or "none")
        return

    records = extract_all(acquisitions, grid, cache, args.workers)
    months = composite_months(records, cache / f"months-{args.store}")

    def portable(entries: list[dict]) -> list[dict]:
        """The entries without their local cache paths."""
        return [{k: v for k, v in e.items() if k not in ("npz", "cached")} for e in entries]

    manifest = {
        "query": {
            "collection": COLLECTION,
            "stac": STAC_URL,
            "bbox": list(bbox),
            "start": args.start,
            "end": args.end,
        },
        "series": {"relative_orbit": relative, "direction": direction},
        "grid": {**asdict(grid), "transform": grid.transform, "looks": LOOKS},
        "access_date": datetime.now(UTC).date().isoformat(),
        "empty_months": empty,
        "months": portable(months),
        "acquisitions": portable(records),
    }
    encode_store(months, grid, store, manifest)
    manifest_path.write_text(json.dumps(manifest, indent=2))
    logger.info("manifest -> %s", manifest_path)


if __name__ == "__main__":
    main(sys.argv[1:])
