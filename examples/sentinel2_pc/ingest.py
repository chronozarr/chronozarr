r"""Ingest one AOI from aois.yaml: Sentinel-2 monthly mosaics -> chronozarr store.

Phase 1 (download) searches Planetary Computer for Sentinel-2 L2A scenes, masks clouds with
the SCL band, and writes one monthly median composite per month to
<out-dir>/mosaics/<aoi>/YYYY-MM.npz. Months that already exist are skipped.

Phase 2 (encode) stacks those .npz files into a (time, band, y, x) uint16 array and writes it
with chronozarr.encode to <out-dir>/stores/<aoi>/chronozarr/, with band metadata, a coverage
plane (1 where at least one scene was valid, 0 where the value is carried forward or missing)
and provenance recorded in the store. With --stac it also writes a static STAC Collection and
Item to <out-dir>/stores/<aoi>/stac/.

Usage:
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta --skip-download
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta \
        --start 2020-01-01 --end 2024-12-31
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta --out-dir /path/to/scratch
    uv run python examples/sentinel2_pc/ingest.py --aoi nile_delta --skip-download --stac
"""

from __future__ import annotations

import argparse
import logging
import time
from pathlib import Path

import numpy as np
import xarray as xr
import yaml
from catalog import PC_STAC_URL, S2_COLLECTION, search_scenes_by_month
from mosaic import build_monthly_mosaics

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
        "is none. coverage is 1 where at least one scene was valid that month and 0 where the "
        "value is carried forward or missing (the monthly files store the valid fraction, not a "
        "scene count)."
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


def download_mosaics(aoi: dict, start: str, end: str, mosaic_dir: Path) -> dict[str, Path]:
    bbox = tuple(aoi["bbox"])

    logger.info("Downloading monthly mosaics for %s (%s to %s)", aoi["name"], start, end)
    t0 = time.perf_counter()

    by_month = search_scenes_by_month(bbox, start, end, max_cloud_pct=80.0)
    outputs = build_monthly_mosaics(by_month, bbox, target_epsg=aoi["epsg"], output_dir=mosaic_dir)

    elapsed = time.perf_counter() - t0
    total_mb = sum(p.stat().st_size for p in outputs.values()) / 1e6
    logger.info("Download done: %d months, %.1f MB, %.0fs", len(outputs), total_mb, elapsed)
    return outputs


def _grid(npz: np.lib.npyio.NpzFile) -> tuple:
    """CRS, affine transform and band names of one mosaic; identical across months."""
    return (
        int(npz["epsg"]),
        tuple(float(v) for v in npz["transform"]),
        tuple(str(b) for b in npz["band_names"]),
    )


def load_mosaic_stack(mosaic_dir: Path) -> tuple[xr.DataArray, xr.DataArray | None]:
    """Stack every YYYY-MM.npz in `mosaic_dir`.

    Returns the (time, band, y, x) uint16 data and, when every file has a `coverage` plane, a
    (time, y, x) uint8 coverage flag (1 where any scene was valid, 0 otherwise).
    """
    paths = sorted(mosaic_dir.glob("*.npz"))
    if not paths:
        raise SystemExit(f"No .npz mosaics in {mosaic_dir}; run without --skip-download first")

    with np.load(paths[0], allow_pickle=False) as first:
        reference = _grid(first)
        band_shape = first["bands"].shape
    epsg, transform, band_names = reference

    stack = np.empty((len(paths), *band_shape), dtype=np.uint16)
    coverage = np.empty((len(paths), *band_shape[1:]), dtype=np.uint8)
    has_coverage = True
    for i, path in enumerate(paths):
        with np.load(path, allow_pickle=False) as npz:
            bands = npz["bands"]
            if _grid(npz) != reference or bands.shape != band_shape:
                raise SystemExit(f"{path} has a different grid, CRS or bands than {paths[0]}")
            stack[i] = bands
            if "coverage" in npz:
                coverage[i] = npz["coverage"] > 0
            else:
                has_coverage = False
    if not has_coverage:
        logger.warning("Some mosaics have no coverage plane; the store will not have one")

    times = np.array([np.datetime64(f"{p.stem}-01", "s") for p in paths])
    data = xr.DataArray(
        stack,
        dims=("time", "band", "y", "x"),
        coords={"time": times, "band": list(band_names)},
        attrs={"crs": f"EPSG:{epsg}", "transform": transform},
    )
    plane = (
        xr.DataArray(coverage, dims=("time", "y", "x"), coords={"time": times})
        if has_coverage
        else None
    )
    return data, plane


def band_metadata(names: list[str]) -> list[Band]:
    return [
        Band(name=n, common_name=S2_COMMON_NAMES.get(n), scale=REFLECTANCE_SCALE, offset=0.0)
        for n in names
    ]


def encode(mosaic_dir: Path, store_dir: Path) -> None:
    t0 = time.perf_counter()
    da, coverage = load_mosaic_stack(mosaic_dir)
    logger.info(
        "Loaded %d monthly mosaics %s, %.2f GB, %.1fs",
        da.sizes["time"],
        da.shape,
        da.nbytes / 1e9,
        time.perf_counter() - t0,
    )

    t0 = time.perf_counter()
    report = chronozarr.encode(
        da,
        store_dir,
        bands=band_metadata([str(b) for b in da["band"].values]),
        coverage=coverage,
        provenance=PROVENANCE,
    )
    logger.info(
        "Encoded %d levels (%s): %.2f MB in %d files, %.1fs -> %s",
        len(report.levels),
        report.encoding,
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
        download_mosaics(aoi, args.start, args.end, mosaic_dir)

    encode(mosaic_dir, store_dir)
    if args.stac:
        emit_stac(store_dir, args.aoi)
    logger.info("Done: %s", args.aoi)


if __name__ == "__main__":
    main()
