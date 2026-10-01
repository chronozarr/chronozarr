"""Render monthly true-color PNG frames with world files, the way an exporter would hand them over.

Earth Engine thumbnails, QGIS "Save as image", matplotlib and drone pipelines all produce the same
thing: one georeferenced PNG per date and no GeoTIFF. These frames are rendered from the monthly
Sentinel-2 mosaics in data/mosaics/<aoi>/YYYY-MM.npz (the ingest in examples/sentinel2_pc) so that
`chronozarr convert` has something real to read. Nothing here is part of the format.

Each frame is a SIZE x SIZE pixel window of a mosaic, written as

    <out>/YYYY-MM.png   8-bit RGBA: red = B04, green = B03, blue = B02, alpha = 0 where the
                        mosaic's coverage is 0 (no valid scene that month); RGB is 0 there too
    <out>/YYYY-MM.pgw   ESRI world file: the north-up transform, no CRS (so `convert` needs --crs)
    <out>/manifest.csv  uri,datetime for the frames, which `chronozarr convert` reads

The stretch is one fixed linear map for every frame, not the viewer's tone mapping: reflectance DN
lo..hi goes to 0..255, where lo and hi are the 2nd and 98th percentile of the valid red, green and
blue values of all frames together. Because it is the same for every month, brightness is
comparable across the series; a stretch fitted per frame would not be. Whatever stretch an
exporter uses is baked into its PNGs the same way, and the store keeps these 8-bit values as they
are.

Usage:
    uv run python examples/png_frames/render_frames.py
    examples/png_frames/convert.sh        # frames -> data/stores/ucayali_santa_maria/png-1
"""

from __future__ import annotations

import argparse
import sys
import warnings
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import rasterio
from rasterio.errors import NotGeoreferencedWarning

ROOT = Path(__file__).resolve().parents[2]
RED, GREEN, BLUE = 2, 1, 0  # band indexes of B04, B03, B02 in the mosaics (B02, B03, B04, B08)
LOW_PERCENTILE, HIGH_PERCENTILE = 2.0, 98.0


@dataclass(frozen=True)
class Window:
    """One month of a mosaic window: DNs (3, size, size), coverage > 0, the window's north-up
    transform (a, b, c, d, e, f) and the mosaic's EPSG code."""

    rgb: np.ndarray
    valid: np.ndarray
    transform: tuple[float, ...]
    epsg: int


def month_names(start: str, count: int) -> list[str]:
    year, month = (int(part) for part in start.split("-"))
    names = []
    for _ in range(count):
        names.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return names


def read_window(path: Path, row0: int, col0: int, size: int) -> Window:
    with np.load(path) as mosaic:
        bands = mosaic["bands"]
        coverage = mosaic["coverage"]
        a, b, c, d, e, f = (float(v) for v in mosaic["transform"])
        height, width = coverage.shape
        if row0 + size > height or col0 + size > width:
            raise ValueError(
                f"{path}: the {size} px window at ({row0}, {col0}) leaves the "
                f"{height} x {width} px mosaic"
            )
        rows, cols = slice(row0, row0 + size), slice(col0, col0 + size)
        rgb = np.stack([bands[RED][rows, cols], bands[GREEN][rows, cols], bands[BLUE][rows, cols]])
        return Window(
            rgb,
            coverage[rows, cols] > 0,
            (a, b, c + col0 * a, d, e, f + row0 * e),
            int(mosaic["epsg"]),
        )


def stretch_limits(windows: list[Window]) -> tuple[int, int]:
    """DN at the 2nd and 98th percentile of every valid red, green and blue value, exactly, from a
    histogram with one bin per DN."""
    histogram = np.zeros(65536, dtype=np.int64)
    for window in windows:
        histogram += np.bincount(window.rgb[:, window.valid].ravel(), minlength=65536)
    cumulative = np.cumsum(histogram)
    total = int(cumulative[-1])
    low = int(np.searchsorted(cumulative, total * LOW_PERCENTILE / 100.0))
    high = int(np.searchsorted(cumulative, total * HIGH_PERCENTILE / 100.0))
    return low, high


def render(window: Window, low: int, high: int) -> np.ndarray:
    """(4, h, w) uint8: the stretched colours (0 where invalid) and alpha 255 where valid."""
    scaled = (window.rgb.astype(np.float32) - low) / float(high - low)
    colour = np.rint(np.clip(scaled, 0.0, 1.0) * 255.0).astype(np.uint8)
    colour[:, ~window.valid] = 0
    return np.concatenate([colour, np.where(window.valid, 255, 0).astype(np.uint8)[np.newaxis]])


def write_frame(path: Path, frame: np.ndarray, transform: tuple[float, ...]) -> None:
    """The PNG (GDAL's PNG writer, default compression) and its world file."""
    # The PNG is written without georeferencing on purpose: the world file below is the only
    # sidecar, as in an exporter's output. rasterio warns about exactly that.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", NotGeoreferencedWarning)
        with rasterio.open(
            path,
            "w",
            driver="PNG",
            height=frame.shape[1],
            width=frame.shape[2],
            count=4,
            dtype="uint8",
        ) as dst:
            dst.write(frame)
    a, b, c, d, e, f = transform
    # A world file locates the *centre* of the upper-left pixel, in the order A, D, B, E, C, F.
    values = [a, d, b, e, c + a / 2 + b / 2, f + d / 2 + e / 2]
    path.with_suffix(".pgw").write_text("".join(f"{value!r}\n" for value in values))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--aoi", default="ucayali_santa_maria", help="folder in data/mosaics")
    parser.add_argument("--start", default="2019-01", help="first month, YYYY-MM")
    parser.add_argument("--months", type=int, default=36, help="consecutive months to render")
    parser.add_argument("--row0", type=int, default=768, help="top row of the window")
    parser.add_argument("--col0", type=int, default=1280, help="left column of the window")
    parser.add_argument("--size", type=int, default=1024, help="window edge in pixels")
    parser.add_argument(
        "--out", type=Path, default=ROOT / "data/png_frames/ucayali", help="folder, must not exist"
    )
    args = parser.parse_args()

    months = month_names(args.start, args.months)
    mosaics = ROOT / "data/mosaics" / args.aoi
    missing = [m for m in months if not (mosaics / f"{m}.npz").is_file()]
    if missing:
        sys.exit(f"{mosaics} has no mosaic for {len(missing)} of {len(months)} months: {missing}")
    if args.out.exists():
        sys.exit(f"{args.out} exists; move it away or pass another --out")

    windows = [read_window(mosaics / f"{m}.npz", args.row0, args.col0, args.size) for m in months]
    low, high = stretch_limits(windows)
    print(f"stretch: DN {low}..{high} to 0..255, p2 and p98 of valid R, G, B over all months")

    args.out.mkdir(parents=True)
    rows = ["uri,datetime"]
    sizes = []
    for month, window in zip(months, windows, strict=True):
        path = args.out / f"{month}.png"
        write_frame(path, render(window, low, high), window.transform)
        sizes.append(path.stat().st_size)
        rows.append(f"{path.name},{month}-01")
    (args.out / "manifest.csv").write_text("\n".join(rows) + "\n")

    masked = [1.0 - float(w.valid.mean()) for w in windows]
    print(f"wrote {len(months)} frames of {args.size} x {args.size} px to {args.out}")
    print(f"on disk: {sum(sizes) / 1e6:.1f} MB, {np.mean(sizes) / 1e6:.2f} MB per frame")
    print(
        f"alpha 0: {min(masked):.1%} to {max(masked):.1%} of pixels per frame, "
        f"{sum(m > 0.03 for m in masked)} frames above 3 %"
    )
    print(f"window transform {windows[0].transform}, mosaic CRS EPSG:{windows[0].epsg}")
    print(f"next: uv run chronozarr convert {args.out / 'manifest.csv'} <store> --crs EPSG:code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
