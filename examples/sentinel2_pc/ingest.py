r"""Ingest one AOI from aois.yaml: Sentinel-2 monthly mosaics -> chronozarr store.

Phase 1 (download) searches Planetary Computer for Sentinel-2 L2A scenes, masks clouds with
the SCL band, and writes one monthly median composite per month to
<out-dir>/mosaics/<aoi>/YYYY-MM.tif, in strips of the AOI when a month does not fit the memory
budget. Months that already exist (.tif, or .npz from before 2026-10-11) are skipped.

Phase 2 (encode) reads those files as one lazy (time, band, y, x) uint16 array, one cell of
every month at a time, and writes it with chronozarr.encode to
<out-dir>/stores/<aoi>/chronozarr/, with band metadata, a coverage plane (the number of valid
scenes behind each pixel, saturated at 255; 0 where the value is carried forward or missing)
and provenance recorded in the store. With --stac it also writes a static STAC Collection and
Item to <out-dir>/stores/<aoi>/stac/.

Usage:
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta --skip-download
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta \
        --start 2020-01-01 --end 2024-12-31
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta --out-dir /path/to/scratch
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta --skip-download --stac
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta --diagnostics
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta --performance fixed --requests 8
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta --max-requests 32

Performance: by default (--performance auto) the download phase detects usable CPUs and memory
(including container, SLURM and rlimit caps) and runs 16 concurrent reads. It lowers that number
when the host fails or throttles reads and, with a --max-requests above 16, tries more while
throughput keeps rising. --performance fixed uses the given or default values without
adaptation, so runs are repeatable. --diagnostics prints the chosen
settings with reasons, and after the download the bottleneck and per-stage timings.
"""

from __future__ import annotations

import argparse
import json
import logging
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
import xarray as xr
import yaml
from catalog import PC_STAC_URL, S2_COLLECTION, search_scenes_by_month, sign_href
from mosaic import (
    RECORD_KEYS,
    Grid,
    MemoryBudgetError,
    RunReport,
    SceneReadError,
    build_monthly_mosaics,
    load_mosaic,
    month_record,
    run_summary,
    save_month,
)
from performance import AdaptiveLimiter, Settings, detect_resources, parse_bytes, plan_settings
from rasterio.crs import CRS  # ty: ignore[unresolved-import]  (compiled module, no stubs)
from rasterio.windows import Window
from xarray.backends import BackendArray
from xarray.core import indexing

import chronozarr
from chronozarr.schema import Band
from chronozarr.stac import write_stac

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("ingest")

HERE = Path(__file__).resolve().parent
DEFAULT_OUT_DIR = HERE.parents[1] / "data"

# Sentinel-2 L2A bands used here. Composites are stored as DN with the processing-baseline offset
# removed (mosaic.py), so reflectance = DN * 0.0001.
S2_COMMON_NAMES = {"B02": "blue", "B03": "green", "B04": "red", "B08": "nir"}
REFLECTANCE_SCALE = 0.0001
PROVENANCE = {
    "sources": [f"{PC_STAC_URL}/collections/{S2_COLLECTION}"],
    "composite": "monthly median",
    "gap_fill": "carry-forward",
    "notes": (
        "Sentinel-2 L2A scenes from Microsoft Planetary Computer with eo:cloud_cover < 80. Per "
        "pixel and month, the median of scenes whose SCL class is 4, 5, 6, 7 or 11 and whose "
        "bands are non-zero; the +1000 offset of processing baseline 04.00 and later is removed. "
        "A pixel with no valid scene takes the previous month's composite, and stays 0 if there "
        "is none. coverage is the number of valid scenes behind the pixel that month (saturated "
        "at 255), and 0 where the value is carried forward or missing."
    ),
}
STAC_LICENSE = "proprietary"


def load_aoi_config(aoi_name: str) -> dict:
    with open(HERE / "aois.yaml") as f:
        cfg = yaml.safe_load(f)
    if aoi_name not in cfg["aois"]:
        available = list(cfg["aois"].keys())
        raise ValueError(f"Unknown AOI '{aoi_name}'. Available: {available}")
    aoi = cfg["aois"][aoi_name]
    aoi["name"] = aoi_name
    return aoi


def download_mosaics(
    aoi: dict,
    start: str,
    end: str,
    mosaic_dir: Path,
    settings: Settings,
    cpus: int,
    diagnostics: bool = False,
    keep_going: bool = False,
    strip_rows: int | None = None,
) -> dict[str, Path]:
    bbox = tuple(aoi["bbox"])

    logger.info("Downloading monthly mosaics for %s (%s to %s)", aoi["name"], start, end)
    t0 = time.perf_counter()

    by_month = search_scenes_by_month(bbox, start, end, max_cloud_pct=80.0)
    logger.info("Scene search: %.1fs", time.perf_counter() - t0)
    report = RunReport()
    limiter = AdaptiveLimiter(settings.requests, settings.max_requests, adaptive=settings.adaptive)
    cpu0 = time.process_time()
    outputs = build_monthly_mosaics(
        by_month,
        bbox,
        target_epsg=aoi["epsg"],
        output_dir=mosaic_dir,
        settings=settings,
        sign=sign_href,
        report=report,
        limiter=limiter,
        keep_going=keep_going,
        strip_rows=strip_rows,
    )

    elapsed = time.perf_counter() - t0
    total_mb = sum(p.stat().st_size for p in outputs.values()) / 1e6
    logger.info("Download done: %d months, %.1f MB, %.0fs", len(outputs), total_mb, elapsed)
    summary = run_summary(report, limiter, settings, cpus, time.process_time() - cpu0)
    if report.months_incomplete or report.months_not_written:
        logger.warning(
            "--keep-going: %d months written without some scenes (%s) and %d months not "
            "written because every scene failed (%s). Encoding refuses the incomplete months "
            "unless --keep-going is given again; delete their .npz files and re-run to retry.",
            len(report.months_incomplete),
            ", ".join(report.months_incomplete) or "none",
            len(report.months_not_written),
            ", ".join(report.months_not_written) or "none",
        )
    if diagnostics:
        logger.info("Bottleneck: %s", summary["bottleneck"])
        logger.info("Run summary:\n%s", json.dumps(summary, indent=2))
    return outputs


class _MonthStack(BackendArray):
    """Monthly GeoTIFFs as one lazy array, read one window per month on access.

    `band_indexes` are the file bands (1-based) behind the band axis; without a band axis
    (`plane=True`) the array is (time, y, x) of the single band given, passed through
    `transform`.
    """

    def __init__(self, paths, band_indexes, height, width, plane=False, transform=None):
        self.paths = paths
        self.band_indexes = band_indexes
        self.plane = plane
        self.transform = transform
        if plane:
            self.shape = (len(paths), height, width)
        else:
            self.shape = (len(paths), len(band_indexes), height, width)
        self.dtype = np.dtype(np.uint8 if plane else np.uint16)

    def __getitem__(self, key):
        return indexing.explicit_indexing_adapter(
            key, self.shape, indexing.IndexingSupport.BASIC, self._read
        )

    def _read(self, key: tuple) -> np.ndarray:
        def span(k, n: int) -> tuple[slice, slice | int]:
            """(contiguous slice to read, selection to apply to what was read)."""
            if isinstance(k, int | np.integer):
                return slice(int(k), int(k) + 1), 0
            start, stop, step = k.indices(n)
            return slice(start, max(start, stop)), slice(None, None, step)

        times = range(self.shape[0])[key[0]]
        bands = [self.band_indexes[0]] if self.plane else self.band_indexes
        if not self.plane:
            bands = bands[key[1]] if isinstance(key[1], slice) else [bands[key[1]]]
        (ys, y_sel), (xs, x_sel) = span(key[-2], self.shape[-2]), span(key[-1], self.shape[-1])
        window = Window.from_slices(ys, xs)
        planes = []
        for t in times if isinstance(times, range) else [times]:
            with rasterio.open(self.paths[t]) as src:
                data = src.read(bands, window=window)[:, y_sel, x_sel]
            planes.append(data if self.transform is None else self.transform(data))
        out = np.stack(planes)
        if self.plane:
            out = out[:, 0]
        else:
            band_sel = slice(None) if isinstance(key[1], slice) else 0
            out = out[:, band_sel]
        return out[0] if isinstance(key[0], int | np.integer) else out


@dataclass
class MosaicStack:
    """The monthly files of an AOI as lazy arrays, ready for chronozarr.encode."""

    data: xr.DataArray  # (time, band, y, x) uint16
    coverage: xr.DataArray | None  # (time, y, x) uint8 valid scenes per pixel, max 255
    incomplete: dict[str, list[str]]  # months written without some scenes -> scene IDs


def month_files(mosaic_dir: Path) -> list[Path]:
    """The monthly files in `mosaic_dir` in time order: YYYY-MM.tif, or .npz from before."""
    by_month: dict[str, Path] = {}
    for path in sorted([*mosaic_dir.glob("*.tif"), *mosaic_dir.glob("*.npz")]):
        if path.stem in by_month:
            raise SystemExit(f"{path.stem} has both a .tif and an .npz in {mosaic_dir}")
        by_month[path.stem] = path
    if not by_month:
        raise SystemExit(f"No monthly mosaics in {mosaic_dir}; run without --skip-download first")
    return [by_month[m] for m in sorted(by_month)]


@contextmanager
def open_mosaic_stack(
    mosaic_dir: Path, keep_going: bool = False, scratch_dir: Path | None = None
) -> Iterator[MosaicStack]:
    """Every month in `mosaic_dir` as lazy (time, band, y, x) data and coverage counts.

    Nothing but the months' records is read here; encoding reads one cell of every month at a
    time. Months saved as .npz (before 2026-10-11) are first copied to GeoTIFFs, one at a time,
    in a temporary directory under `scratch_dir` (default: the system's), never in
    `mosaic_dir`.

    Refuses months that list failed scenes unless `keep_going`, and any month whose gaps were
    filled from something other than the month before it in the stack (a month re-run or
    removed after its successor was built).

    Coverage is the number of valid scenes per pixel, as chronozarr's coverage variable
    requires, saturated at 255. Months saved as .npz before `scenes_searched` was recorded hold
    only the valid fraction k / n without n, so the count cannot be recovered; a flag would be
    a wrong count, so then the store gets no coverage variable and a warning names the months.
    """
    paths = month_files(mosaic_dir)
    with tempfile.TemporaryDirectory(prefix=".npz-months-", dir=scratch_dir) as scratch:
        tifs: list[Path] = []
        records: list[dict] = []
        for path in paths:
            if path.suffix == ".npz":
                path = _npz_as_tif(path, Path(scratch))
            tifs.append(path)
            records.append(month_record(path))
        reference = records[0]
        for path, record in zip(paths, records, strict=True):
            for name in ("epsg", "transform", "shape", "band_names"):
                if record[name] != reference[name]:
                    raise SystemExit(f"{path} has a different {name} than {paths[0]}")

        incomplete: dict[str, list[str]] = {}
        unchecked: list[str] = []
        stale: list[str] = []
        for i, (path, record) in enumerate(zip(paths, records, strict=True)):
            if record["scenes_failed"]:
                incomplete[path.stem] = record["scenes_failed"]
            if record["carried_from"] is None:
                unchecked.append(path.stem)
                continue
            expected = [] if i == 0 else [paths[i - 1].stem, records[i - 1]["bands_sha256"]]
            if record["carried_from"] != expected:
                source = record["carried_from"][0] if record["carried_from"] else "nothing"
                before = paths[i - 1].stem if i else "nothing"
                stale.append(
                    f"{path.stem} (filled from {source}; the stack has {before} before it)"
                )
        if stale:
            raise SystemExit(
                "These months filled their gaps from a different month than the one before "
                f"them in {mosaic_dir}: {'; '.join(stale)}. Delete them and the months after "
                "them, then re-run the download."
            )
        if unchecked:
            logger.info(
                "%d months were written before carry-forward sources were recorded; their chain "
                "is not checked",
                len(unchecked),
            )
        if incomplete and not keep_going:
            listing = "; ".join(f"{m}: {', '.join(ids)}" for m, ids in incomplete.items())
            raise SystemExit(
                f"{len(incomplete)} months were written without some scenes (--keep-going): "
                f"{listing}. Delete those files and re-run the download to retry them, or "
                "pass --keep-going to encode them; the store's provenance notes then list them."
            )

        n_bands, height, width = reference["shape"]
        times = np.array([np.datetime64(f"{p.stem}-01", "s") for p in paths])
        stack = _MonthStack(tifs, list(range(1, n_bands + 1)), height, width)
        counts = _MonthStack(
            tifs,
            [n_bands + 1],
            height,
            width,
            plane=True,
            transform=lambda c: np.minimum(c, 255).astype(np.uint8),
        )
        data = xr.DataArray(
            xr.Variable(("time", "band", "y", "x"), indexing.LazilyIndexedArray(stack)),
            coords={"time": times, "band": list(reference["band_names"])},
            attrs={
                "crs": f"EPSG:{reference['epsg']}",
                "transform": tuple(reference["transform"])[:6],
            },
        )
        no_count = [
            p.stem for p, r in zip(paths, records, strict=True) if r["scenes_searched"] is None
        ]
        coverage = None
        if no_count:
            logger.warning(
                "%d months were saved without scenes_searched, so their valid fraction cannot "
                "become a scene count (%s); the store will have no coverage variable. Re-run "
                "the download of those months to get one.",
                len(no_count),
                ", ".join(no_count[:5]) + (", ..." if len(no_count) > 5 else ""),
            )
        else:
            coverage = xr.DataArray(
                xr.Variable(("time", "y", "x"), indexing.LazilyIndexedArray(counts)),
                coords={"time": times},
            )
        yield MosaicStack(data, coverage, incomplete)


def _npz_as_tif(path: Path, directory: Path) -> Path:
    """A month saved as .npz, rewritten as a GeoTIFF in `directory` with the same record."""
    month = load_mosaic(path)
    count = month["valid_count"]
    if count is None:  # before scenes_searched was saved: no count; the stack drops coverage
        count = np.zeros(month["coverage"].shape, dtype=np.uint16)
    _, height, width = month["bands"].shape
    grid = Grid(month["transform"], CRS.from_epsg(month["epsg"]), height, width)
    out = directory / f"{path.stem}.tif"
    save_month(
        out,
        grid,
        month["epsg"],
        month["band_names"],
        month["bands"],
        count,
        {k: month[k] for k in RECORD_KEYS},
    )
    return out


def band_metadata(names: list[str]) -> list[Band]:
    return [
        Band(name=n, common_name=S2_COMMON_NAMES.get(n), scale=REFLECTANCE_SCALE, offset=0.0)
        for n in names
    ]


def provenance_for(incomplete: dict[str, list[str]]) -> dict:
    """PROVENANCE, with the months encoded without some scenes named in the notes."""
    if not incomplete:
        return PROVENANCE
    listing = "; ".join(f"{m} without {', '.join(ids)}" for m, ids in incomplete.items())
    note = (
        f" INCOMPLETE MONTHS: {len(incomplete)} monthly composites were built without scenes "
        f"that could not be read (encoded with --keep-going): {listing}. Their medians are "
        "not the full month's composite."
    )
    return {**PROVENANCE, "notes": str(PROVENANCE["notes"]) + note}


def encode(mosaic_dir: Path, store_dir: Path, keep_going: bool = False) -> None:
    t0 = time.perf_counter()
    store_dir.parent.mkdir(parents=True, exist_ok=True)
    with open_mosaic_stack(mosaic_dir, keep_going, scratch_dir=store_dir.parent) as stack:
        da = stack.data
        logger.info(
            "Found %d monthly mosaics %s (%.2f GB uncompressed), %.1fs",
            da.sizes["time"],
            da.shape,
            da.size * 2 / 1e9,
            time.perf_counter() - t0,
        )
        t0 = time.perf_counter()
        report = chronozarr.encode(
            da,
            store_dir,
            bands=band_metadata([str(b) for b in da["band"].values]),
            coverage=stack.coverage,
            provenance=provenance_for(stack.incomplete),
        )
    logger.info(
        "Encoded %d levels (true values): %.2f MB in %d files, %.1fs -> %s",
        len(report.levels),
        report.total_bytes / 1e6,
        report.n_files,
        time.perf_counter() - t0,
        store_dir,
    )


def emit_stac(store_dir: Path, aoi_name: str) -> Path:
    """Write a static STAC Collection and Item next to the store; returns the catalog dir."""
    stac_dir = store_dir.parent / "stac"
    collection_path, item_path = write_stac(
        store_dir,
        stac_dir,
        id=f"{aoi_name}-sentinel-2-monthly",
        title=f"{aoi_name}: Sentinel-2 L2A monthly median composites",
        description=(
            f"Monthly median composites of Sentinel-2 L2A (B02, B03, B04, B08) over {aoi_name}, "
            "as a chronozarr store. Contains modified Copernicus Sentinel data."
        ),
        license=STAC_LICENSE,
    )
    logger.info("STAC: %s and %s", collection_path, item_path)
    return stac_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest one AOI: Sentinel-2 monthly mosaics to a chronozarr store"
    )
    parser.add_argument("--aoi", required=True, help="AOI name from aois.yaml")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument(
        "--stac",
        action="store_true",
        help="Also write a static STAC Collection and Item to <out-dir>/stores/<aoi>/stac/",
    )
    parser.add_argument("--start", default="2015-07-01", help="Start date (default: 2015-07-01)")
    parser.add_argument("--end", default="2026-04-01", help="End date (default: 2026-04-01)")
    perf = parser.add_argument_group("performance (download phase)")
    perf.add_argument(
        "--performance",
        choices=("auto", "fixed"),
        default="auto",
        help="auto: detect resources and adapt request concurrency (default); "
        "fixed: no adaptation, repeatable settings",
    )
    perf.add_argument(
        "--requests",
        type=int,
        help="concurrent remote reads: the start value in auto mode, the fixed value otherwise "
        "(default 16)",
    )
    perf.add_argument(
        "--max-requests", type=int, help="ceiling on concurrent remote reads (default 16)"
    )
    perf.add_argument(
        "--cpu-workers", type=int, help="threads for compositing (default: usable CPUs - 1)"
    )
    perf.add_argument(
        "--memory",
        type=parse_bytes,
        help="memory budget for month buffers and the GDAL cache, e.g. 4GB "
        "(default: half of available memory)",
    )
    perf.add_argument(
        "--strip-rows",
        type=int,
        help="rows per strip of the AOI grid composited at a time, a multiple of 512 (default: "
        "the whole grid when a month fits the memory budget, else the tallest strips of which "
        "two fit)",
    )
    perf.add_argument(
        "--keep-going",
        action="store_true",
        help="continue when a scene cannot be read: write months with failed scenes (listed in "
        "the .npz and, when encoded, in the store's provenance notes), skip months where every "
        "scene failed (retried by the next run). Without it the run stops at the first such "
        "month and encoding refuses incomplete months",
    )
    perf.add_argument(
        "--diagnostics",
        action="store_true",
        help="print the chosen settings with reasons, the bottleneck and per-stage timings",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT_DIR,
        help="Root for mosaics/ and stores/ (default: <repo>/data)",
    )
    args = parser.parse_args()

    aoi = load_aoi_config(args.aoi)
    mosaic_dir = args.out_dir / "mosaics" / args.aoi
    store_dir = args.out_dir / "stores" / args.aoi / "chronozarr"

    if store_dir.exists() and any(store_dir.iterdir()):
        raise SystemExit(
            f"{store_dir} already exists; stores are immutable, delete it to re-encode"
        )

    if not args.skip_download:
        resources = detect_resources()
        for name in ("requests", "max_requests", "cpu_workers"):
            value = getattr(args, name)
            if value is not None and value < 1:
                parser.error(f"--{name.replace('_', '-')} must be at least 1, got {value}")
        settings = plan_settings(
            resources,
            adaptive=args.performance == "auto",
            requests=args.requests,
            max_requests=args.max_requests,
            cpu_workers=args.cpu_workers,
            memory_budget=args.memory,
        )
        log = logger.info if args.diagnostics else logger.debug
        log("Resources: %s", resources)
        for reason in settings.reasons:
            log("Setting %s", reason)
        try:
            download_mosaics(
                aoi,
                args.start,
                args.end,
                mosaic_dir,
                settings,
                resources.cpus,
                diagnostics=args.diagnostics,
                keep_going=args.keep_going,
                strip_rows=args.strip_rows,
            )
        except (MemoryBudgetError, SceneReadError, ValueError) as e:
            raise SystemExit(f"error: {e}") from e

    encode(mosaic_dir, store_dir, keep_going=args.keep_going)
    if args.stac:
        emit_stac(store_dir, args.aoi)
    logger.info("Done: %s", args.aoi)


if __name__ == "__main__":
    main()
