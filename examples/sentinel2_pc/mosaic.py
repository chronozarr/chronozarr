"""Monthly median composite mosaics from Sentinel-2 L2A scenes.

Reads COGs over HTTPS, applies SCL cloud masking, and writes monthly median composites in the
native UTM CRS of the AOI.

Per scene the SCL band is read first. A scene with no valid pixel in the AOI reads no bands. When
a scene shares the AOI's CRS and its pixel grid lines up with the AOI grid (the usual case for
Sentinel-2 in its own UTM zone), bands are read as windows on the native grid, and only the
source blocks that hold a valid pixel are fetched. That read is the same pixel for pixel as the
bilinear `reproject` used for every other scene (bilinear on an aligned grid returns the source
pixel, nearest 20 m -> 10 m returns the covering source pixel; tests/test_ingest_mosaic.py checks
both against `reproject`).

Months overlap: the reads of later months start while earlier months are composited and saved,
within a memory budget, and months are finished in calendar order because a month's gaps are
filled from the month before it.
"""

from __future__ import annotations

import ctypes
import hashlib
import itertools
import json
import logging
import math
import os
import random
import re
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

import numpy as np
import rasterio
from catalog import REQUIRED_BANDS, SceneRef
from performance import AdaptiveLimiter, Settings
from rasterio.crs import CRS  # ty: ignore[unresolved-import]  (compiled module, no stubs)
from rasterio.transform import Affine, from_bounds
from rasterio.warp import Resampling, reproject, transform
from rasterio.windows import Window

logger = logging.getLogger(__name__)

# SCL values to KEEP (everything else is masked)
# 4=vegetation, 5=bare_soil, 6=water, 7=unclassified (low prob cloud)
# 11=snow/ice (kept: snow cover is a real surface state)
SCL_VALID = {4, 5, 6, 7, 11}
_SCL_LUT = np.zeros(256, dtype=bool)
_SCL_LUT[list(SCL_VALID)] = True

# GDAL environment for efficient COG reads over HTTPS. The first two settings remove the
# directory listing and sidecar probes that otherwise cost ~10 extra requests per file.
GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MERGE_CONSECUTIVE_RANGES": "YES",
    "GDAL_HTTP_MULTIPLEX": "YES",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.TIF",
    "VSI_CACHE": "TRUE",
    "VSI_CACHE_SIZE": "5000000",
    # Fail a stalled transfer instead of waiting forever; the read is then retried.
    "GDAL_HTTP_CONNECTTIMEOUT": "30",
    "GDAL_HTTP_LOW_SPEED_TIME": "60",
    "GDAL_HTTP_LOW_SPEED_LIMIT": "1024",
}

# Attempts per asset read after GDAL's own HTTP retries. Failed attempts back off exponentially
# (2, 4, 8, 16, 30 s with jitter, about a minute in all), so a scene is dropped from its month
# only when its host keeps failing; under throttling the request limit drops meanwhile.
READ_ATTEMPTS = 6

# GDAL's warp splits its output into chunks above this many MB of buffers. It allocates only
# what a chunk needs, so this is a threshold, not an allocation (see read_asset).
WARP_MEMORY_MB = 8192

# Error of GDAL's approximate warp transformer in source pixels for band warps (rasterio's
# `tolerance`). Bands only carry interpolation error: 0.01 pixels gives p99 2 DN against the
# exact transformer instead of 20 DN at GDAL's default of 0.125, at the same cost; exact band
# warps would cost 7 times (10 real scenes, 2026-10-10). rasterio before 1.5 passes `tolerance`
# to GDAL as a warp option, which GDAL ignores, so there band warps keep the default. The SCL
# band is not warped by GDAL: see read_nearest.
BAND_WARP_TOLERANCE = 0.01
# Grid rows whose source coordinates or indices are computed at once (bounds temporaries).
COORDINATE_ROWS = 128
READ_BACKOFF_SECONDS = 2.0
READ_BACKOFF_MAX_SECONDS = 30.0

# libcurl 8.16 to 8.21 (rasterio 1.5 wheels bundle 8.17) stores a failed name resolve, transient
# or not, for half of its 60 s DNS cache timeout, per connection cache; GDAL keeps one per
# thread, and a retry runs on the same thread. A retry sooner than this fails at once without
# asking the resolver: three reads failed this way for about 40 s on Planetary Computer
# (2026-10-10) while other threads read the same host. Such a failure is local, not the host
# pushing back, so it does not lower the request limit.
RESOLVE_FAILURE = "Could not resolve host"
RESOLVE_RETRY_SECONDS = 31.0


_clear_cache_function: Callable[[bytes], None] | None = None
_clear_cache_searched = False


def _gdal_clear_cache_function() -> Callable[[bytes], None] | None:
    """GDAL's VSICurlPartialClearCache from the libgdal that rasterio loaded, or None."""
    global _clear_cache_function, _clear_cache_searched
    if _clear_cache_searched:
        return _clear_cache_function
    _clear_cache_searched = True
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
        _clear_cache_function = function
        return function
    logger.warning(
        "GDAL's VSICurlPartialClearCache is not reachable from this rasterio build; a retry of "
        "a URL whose open failed may fail again without contacting the host"
    )
    return None


def forget_url(url: str) -> None:
    """Drop GDAL's cached state for `url`, including the record of a failed open.

    GDAL remembers a failed open of a URL for the life of the process, so a retry of the same
    URL (a signed URL is the same until its token is refreshed) fails at once without a request
    (checked against a local server, GDAL 3.12, 2026-10-10). Clearing the entry lets the retry
    reach the host.
    """
    function = _gdal_clear_cache_function()
    if function is not None:
        function(f"/vsicurl/{url}".encode())


class HttpThrottleCounter(logging.Filter):
    """Counts GDAL's warnings about HTTP 429 and 5xx responses that it retries by itself.

    Attached to rasterio's `rasterio._env` logger, where GDAL warnings arrive. It never blocks:
    a logging hook that takes a lock can deadlock against GDAL's worker threads.
    """

    PATTERN = re.compile(r"HTTP error code: (429|5\d\d)")

    def __init__(self) -> None:
        super().__init__()
        self._count = itertools.count(1)
        self.events = 0

    def filter(self, record: logging.LogRecord) -> bool:
        if self.PATTERN.search(record.getMessage()):
            self.events = next(self._count)
        return True


@dataclass(frozen=True)
class Grid:
    """The AOI's output grid."""

    transform: Affine
    crs: CRS
    height: int
    width: int


def compute_target_grid(
    bbox_wgs84: tuple[float, float, float, float],
    target_epsg: int,
    resolution: float = 10.0,
) -> tuple[Affine, int, int]:
    """Compute a pixel-aligned target grid for the AOI.

    Args:
        bbox_wgs84: (lon_min, lat_min, lon_max, lat_max)
        target_epsg: EPSG code for target CRS (UTM)
        resolution: Pixel size in meters

    Returns:
        (transform, height, width) for the target grid
    """
    from rasterio.warp import transform_bounds

    src_crs = CRS.from_epsg(4326)
    dst_crs = CRS.from_epsg(target_epsg)

    # Transform bbox to target CRS
    left, bottom, right, top = transform_bounds(src_crs, dst_crs, *bbox_wgs84)

    # Snap to resolution grid
    left = np.floor(left / resolution) * resolution
    bottom = np.floor(bottom / resolution) * resolution
    right = np.ceil(right / resolution) * resolution
    top = np.ceil(top / resolution) * resolution

    width = int((right - left) / resolution)
    height = int((top - bottom) / resolution)
    transform = from_bounds(left, bottom, right, top, width, height)

    logger.info(
        "Target grid: %d x %d pixels @ %.0fm, EPSG:%d", width, height, resolution, target_epsg
    )
    return transform, height, width


# --- reading one asset -------------------------------------------------------------------------


def native_offset(src_transform: Affine, src_crs: CRS, grid: Grid) -> tuple[int, int, int] | None:
    """(row, col, factor) when the source grid lines up with `grid`, else None.

    `factor` is the source pixel size in grid pixels; grid pixel (r, c) then lies in source pixel
    ((row + r) // factor, (col + c) // factor).
    """
    s, g = src_transform, grid.transform
    if src_crs != grid.crs or s.b or s.d or g.b or g.d or s.e != -s.a or g.e != -g.a:
        return None
    factor = s.a / g.a
    col = (g.c - s.c) / g.a
    row = (s.f - g.f) / g.a
    if not all(abs(v - round(v)) < 1e-9 for v in (factor, col, row)) or round(factor) < 1:
        return None
    return round(row), round(col), round(factor)


def _clip_range(start: int, size: int, factor: int, src_size: int) -> tuple[int, int]:
    """Grid index range [a, b) of the `size` grid pixels from `start` inside the source."""
    a = max(0, -start)
    b = min(size, factor * src_size - start)
    return a, max(a, b)


def _block_rects(
    needed: np.ndarray, row: int, col: int, rows: tuple[int, int], cols: tuple[int, int], block
) -> list[tuple[int, int, int, int]]:
    """Grid rectangles (r0, r1, c0, c1) covering every source block that holds a needed pixel.

    Factor-1 grids only. Adjacent needed blocks in a block row form one rectangle, and block rows
    with the same column runs are merged, so a fully needed window is one rectangle.
    """
    bh, bw = block
    r_edges = sorted({rows[0], *(e for e in range((-row) % bh, rows[1], bh) if e > rows[0])})
    c_edges = sorted({cols[0], *(e for e in range((-col) % bw, cols[1], bw) if e > cols[0])})
    sub = needed[rows[0] : rows[1], cols[0] : cols[1]]
    hit = np.logical_or.reduceat(
        np.logical_or.reduceat(sub, [e - rows[0] for e in r_edges], axis=0),
        [e - cols[0] for e in c_edges],
        axis=1,
    )
    r_bounds = [*r_edges, rows[1]]
    c_bounds = [*c_edges, cols[1]]
    rects: list[tuple[int, int, int, int]] = []
    previous: list[tuple[int, int]] = []
    open_rects: list[int] = []
    for i, line in enumerate(hit):
        runs: list[tuple[int, int]] = []
        j = 0
        while j < len(line):
            if line[j]:
                k = j
                while k + 1 < len(line) and line[k + 1]:
                    k += 1
                runs.append((c_bounds[j], c_bounds[k + 1]))
                j = k + 1
            else:
                j += 1
        if runs and runs == previous:
            for idx in open_rects:
                r0, _, c0, c1 = rects[idx]
                rects[idx] = (r0, r_bounds[i + 1], c0, c1)
        else:
            open_rects = []
            for c0, c1 in runs:
                open_rects.append(len(rects))
                rects.append((r_bounds[i], r_bounds[i + 1], c0, c1))
        previous = runs
    return rects


def read_native(
    src, grid: Grid, offset: tuple[int, int, int], out: np.ndarray, needed: np.ndarray | None
) -> int:
    """Read band 1 of `src` into `out` on the grid; returns the number of grid pixels read.

    With `needed`, only source blocks that hold a needed pixel are read (factor 1 only).
    """
    row, col, factor = offset
    rows = _clip_range(row, grid.height, factor, src.height)
    cols = _clip_range(col, grid.width, factor, src.width)
    if rows[0] == rows[1] or cols[0] == cols[1]:
        return 0
    if needed is not None and factor == 1:
        rects = _block_rects(needed, row, col, rows, cols, src.block_shapes[0])
    else:
        rects = [(rows[0], rows[1], cols[0], cols[1])]
    pixels = 0
    for r0, r1, c0, c1 in rects:
        s_r0, s_r1 = (row + r0) // factor, (row + r1 - 1) // factor + 1
        s_c0, s_c1 = (col + c0) // factor, (col + c1 - 1) // factor + 1
        window = Window.from_slices((s_r0, s_r1), (s_c0, s_c1))
        data = src.read(1, window=window)
        if factor == 1:
            out[r0:r1, c0:c1] = data
        else:
            ri = (row + np.arange(r0, r1)) // factor - s_r0
            ci = (col + np.arange(c0, c1)) // factor - s_c0
            out[r0:r1, c0:c1] = data[np.ix_(ri, ci)]
        pixels += (r1 - r0) * (c1 - c0)
    return pixels


Coordinates = tuple[np.ndarray, np.ndarray]


def source_coordinates(grid: Grid, src_crs: CRS) -> Coordinates:
    """Exact (x, y) in `src_crs` of every pixel centre of `grid`, float64 arrays of grid shape.

    GDAL's coordinate transformation point by point, in row blocks to bound its Python lists.
    """
    t = grid.transform
    cols = np.arange(grid.width) + 0.5
    x = np.empty((grid.height, grid.width))
    y = np.empty((grid.height, grid.width))
    for r0 in range(0, grid.height, COORDINATE_ROWS):
        r1 = min(grid.height, r0 + COORDINATE_ROWS)
        rows = (np.arange(r0, r1) + 0.5)[:, np.newaxis]
        gx = t.c + t.a * cols + t.b * rows
        gy = t.f + t.d * cols + t.e * rows
        if src_crs == grid.crs:
            x[r0:r1], y[r0:r1] = gx, gy
            continue
        tx, ty = transform(grid.crs, src_crs, gx.ravel(), gy.ravel())
        x[r0:r1] = np.asarray(tx).reshape(gx.shape)
        y[r0:r1] = np.asarray(ty).reshape(gx.shape)
    return x, y


def read_nearest(src, coordinates: Coordinates, out: np.ndarray) -> int:
    """Fill `out` with the source pixel that contains each grid pixel's centre; returns the
    number of grid pixels inside the source.

    `coordinates` are the exact centres in the source CRS (source_coordinates). This is what
    GDAL's nearest-neighbour warp computes with its exact transformer; its default approximate
    transformer (up to 0.125 source pixels off) put 0.12 % of warped SCL pixels in the
    neighbouring source pixel and changed validity on 0.02 % (10 real scenes, 2026-10-10). The
    SCL decides which scenes count at a pixel, so it is read this way, which also costs one
    coordinate transformation per strip and source CRS instead of one per scene.
    """
    x, y = coordinates
    inv = ~src.transform

    def indices(r0: int, r1: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        # GDAL's order of terms for an inverse geotransform.
        col = np.floor(inv.c + x[r0:r1] * inv.a + y[r0:r1] * inv.b)
        row = np.floor(inv.f + x[r0:r1] * inv.d + y[r0:r1] * inv.e)
        inside = (col >= 0) & (col < src.width) & (row >= 0) & (row < src.height)
        return row, col, inside

    blocks = [
        (r0, min(out.shape[0], r0 + COORDINATE_ROWS))
        for r0 in range(0, out.shape[0], COORDINATE_ROWS)
    ]
    lo, hi = [src.height, src.width], [-1, -1]
    for r0, r1 in blocks:
        row, col, inside = indices(r0, r1)
        if inside.any():
            lo = [min(lo[0], int(row[inside].min())), min(lo[1], int(col[inside].min()))]
            hi = [max(hi[0], int(row[inside].max())), max(hi[1], int(col[inside].max()))]
    if hi[0] < 0:
        return 0
    data = src.read(1, window=Window.from_slices((lo[0], hi[0] + 1), (lo[1], hi[1] + 1)))
    pixels = 0
    for r0, r1 in blocks:
        row, col, inside = indices(r0, r1)
        block = out[r0:r1]
        block[inside] = data[
            row[inside].astype(np.intp) - lo[0], col[inside].astype(np.intp) - lo[1]
        ]
        pixels += int(inside.sum())
    return pixels


def read_asset(
    href: str,
    grid: Grid,
    out: np.ndarray,
    resampling: Resampling,
    env: dict,
    needed: np.ndarray | None = None,
    coordinates: Callable[[CRS], Coordinates] | None = None,
) -> tuple[int, bool]:
    """Read one single-band asset into `out` (grid shape). Returns (pixels read, native path).

    A nearest-neighbour read off the native grid takes the source pixel under each grid pixel
    centre (read_nearest), with centres from `coordinates(src_crs)` when given.
    """
    with rasterio.Env(**env), rasterio.open(href) as src:
        offset = native_offset(src.transform, src.crs, grid)
        exact = offset is not None and (offset[2] == 1 or resampling == Resampling.nearest)
        if exact:
            assert offset is not None
            return read_native(src, grid, offset, out, needed), True
        if resampling == Resampling.nearest:
            centres = coordinates(src.crs) if coordinates else source_coordinates(grid, src.crs)
            read_nearest(src, centres, out)
            return out.size, False
        # A warp must give the same pixel whatever strip of the grid it fills. GDAL derives
        # the bilinear scale of each warp chunk from the chunk's shape (up to 1049 DN apart on
        # a real scene in 256-row strips), so the scale is pinned to the ratio of pixel sizes
        # (XSCALE, YSCALE). Its approximate transformer interpolates along each destination
        # row over the chunk's width, so GDAL must not split a strip into narrower chunks:
        # the memory threshold is set far above any strip. Then every full-width strip equals
        # the whole grid bit for bit (checked on real scenes of 2834 and 9202 pixels square),
        # and the whole grid equals GDAL's default warp wherever that warp was not split
        # (2834 pixels square: identical; 9202: 0.4 % of pixels differ, by GDAL's split).
        scale = abs(src.transform.a) / grid.transform.a
        reproject(
            source=rasterio.band(src, 1),
            destination=out,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            resampling=resampling,
            XSCALE=scale,
            YSCALE=scale,
            warp_mem_limit=WARP_MEMORY_MB,
            SRC_FILL_RATIO_HEURISTICS="NO",
            tolerance=BAND_WARP_TOLERANCE,
        )
        return out.size, False


# --- compositing -------------------------------------------------------------------------------


# Size of the arrays one compositing task works on (a row block of a scene or of the stack). Every
# CPU worker holds a few times this in temporaries, whatever the AOI width or scene count.
WORK_BYTES = 2 * 1024**2
# Temporaries of one task as a multiple of WORK_BYTES: masking with an offset holds the int32
# block, its clipped copy, the np.where result and the uint16 cast (3.6 x); the median a sorted
# copy and its index arrays (1.5 x).
WORK_TEMPORARIES = 4


def apply_scene_mask(bands: np.ndarray, valid: np.ndarray, boa_offset: int) -> int:
    """Apply the BOA offset and zero every band where the scene is not valid, in place.

    `bands` is (n_bands, H, W) uint16 and `valid` the SCL mask; a pixel is also invalid where
    any band is 0. Returns the number of valid pixels.
    """
    rows = max(1, WORK_BYTES // (bands.shape[0] * bands.shape[2] * 4))  # int32 row blocks
    for r0 in range(0, bands.shape[1], rows):
        block = bands[:, r0 : r0 + rows]
        if boa_offset:
            shifted = block.astype(np.int32) + boa_offset
            np.copyto(block, np.where(block > 0, np.clip(shifted, 1, 65535), 0).astype(np.uint16))
        block_valid = valid[r0 : r0 + rows]
        block_valid &= np.all(block > 0, axis=0)
        block[:, ~block_valid] = 0
    return int(valid.sum())


def median_rows(stack: np.ndarray, r0: int, r1: int) -> tuple[np.ndarray, np.ndarray]:
    """Median over scenes for grid rows [r0, r1) of a (n_scenes, n_bands, H, W) uint16 stack.

    Invalid observations are 0 and valid ones are >= 1, so after sorting along the scene axis
    the k valid values are the last k. For even k the median is the mean of the two middle
    values rounded down, which is what float32 nanmedian followed by a uint16 cast gives
    (sums up to 131070 are exact in float32). Returns (composite rows, valid counts).
    """
    block = stack[:, :, r0:r1, :]
    n = block.shape[0]
    k = np.count_nonzero(block[:, 0], axis=0)
    ordered = np.sort(block, axis=0)
    lo = np.clip(n - k + (k - 1) // 2, 0, n - 1)
    hi = np.clip(n - k + k // 2, 0, n - 1)
    a = np.take_along_axis(ordered, lo[np.newaxis, np.newaxis], axis=0)[0]
    b = np.take_along_axis(ordered, hi[np.newaxis, np.newaxis], axis=0)[0]
    composite = ((a.astype(np.uint32) + b) // 2).astype(np.uint16)
    composite[:, k == 0] = 0
    return composite, k


def median_composite(
    stack: np.ndarray, pool: ThreadPoolExecutor | None = None, rows: int | None = None
) -> tuple[np.ndarray, np.ndarray]:
    """(composite uint16 (n_bands, H, W), valid scenes per pixel uint16 (H, W)).

    Row blocks of about WORK_BYTES of the stack run in parallel on `pool`.
    """
    n_scenes, n_bands, height, width = stack.shape
    if rows is None:
        rows = max(1, WORK_BYTES // max(1, n_scenes * n_bands * width * 2))
    composite = np.empty((n_bands, height, width), dtype=np.uint16)
    count = np.empty((height, width), dtype=np.uint16)

    def run(r0: int) -> None:
        r1 = min(height, r0 + rows)
        composite[:, r0:r1], count[r0:r1] = median_rows(stack, r0, r1)

    starts = range(0, height, rows)
    if pool is None:
        for r0 in starts:
            run(r0)
    else:
        for future in [pool.submit(run, r0) for r0 in starts]:
            future.result()
    return composite, count


# --- strips and memory -------------------------------------------------------------------------

# Strip edges fall on multiples of this many grid rows: the chronozarr chunk edge and the
# Sentinel-2 COG block edge, so a strip never splits an output chunk.
CELL = 512


@dataclass(frozen=True)
class AoiWindow:
    """Grid rows [r0, r1) and columns [c0, c1), composited as one unit of work."""

    r0: int
    r1: int
    c0: int
    c1: int

    @property
    def height(self) -> int:
        return self.r1 - self.r0

    @property
    def width(self) -> int:
        return self.c1 - self.c0

    def subgrid(self, grid: Grid) -> Grid:
        """The window as a grid of its own: same CRS and pixels, origin moved."""
        transform = grid.transform * Affine.translation(self.c0, self.r0)
        return Grid(transform, grid.crs, self.height, self.width)

    def slices(self) -> tuple[slice, slice]:
        return slice(self.r0, self.r1), slice(self.c0, self.c1)

    def bounds(self) -> list[int]:
        return [self.r0, self.r1, self.c0, self.c1]


def split_grid(height: int, width: int, rows: int) -> list[AoiWindow]:
    """Full-width strips of at most `rows` rows covering the grid, top to bottom.

    Only full-width strips: a narrower window would change warped pixels (see read_asset).
    """
    return [AoiWindow(r0, min(height, r0 + rows), 0, width) for r0 in range(0, height, rows)]


# Interpreter, numpy, rasterio, GDAL and allocator slack. Set so that the estimate for a
# 20-scene Ucayali month (2.41 GB) matches the peak RSS measured in Docker (2.39-2.43 GB,
# 2026-10-10, 1 and 2 CPUs).
PROCESS_BASE_BYTES = 512 * 1024**2


def month_bytes(n_scenes: int, n_bands: int, height: int, width: int, warp: bool = False) -> int:
    """Memory one window of a month holds while it is read and composited.

    With `warp`, also the exact source coordinates of the window's pixels for the SCL reads
    (float64 x and y), for up to two source CRSs.
    """
    plane = height * width
    stack = n_scenes * n_bands * plane * 2
    masks = n_scenes * plane  # SCL masks held until each scene is masked
    composite = n_bands * plane * 2 + plane * 8  # composite, valid counts
    coordinates = 2 * 16 * plane if warp else 0
    return stack + masks + composite + coordinates


def read_bytes(height: int, width: int, warp: bool) -> int:
    """Memory one read in flight holds for a strip of height x width.

    A native read decodes the SCL into a uint8 plane and builds a bool mask, or copies band
    blocks of at most a uint16 plane. A warp also holds GDAL's destination and source buffers
    for the whole strip, which read_asset keeps in one chunk.
    """
    return (7 if warp else 2) * height * width


def fixed_bytes(
    settings: Settings, n_bands: int, height: int, width: int, warp: bool = False
) -> int:
    """Memory used whatever the months, for strips of height x width.

    The process, the GDAL block cache and the temporaries of the CPU workers, then per read in
    flight GDAL's per-file cache (VSI_CACHE_SIZE) and the buffers of `read_bytes`, then the
    finished strip kept for the next month's carry-forward and one finished strip waiting for
    the writer.
    """
    plane = height * width
    workers = settings.cpu_workers * WORK_TEMPORARIES * WORK_BYTES
    per_read = read_bytes(height, width, warp) + int(GDAL_ENV["VSI_CACHE_SIZE"])
    reads = settings.max_requests * per_read
    finished = 2 * (n_bands * plane * 2 + plane * 4)
    return PROCESS_BASE_BYTES + settings.gdal_cache + workers + reads + finished


class MemoryBudgetError(RuntimeError):
    """Not even the smallest window of the largest month fits in the memory budget."""


class SceneReadError(RuntimeError):
    """A scene still failed after every read attempt."""


def _budget_error(
    key: str,
    n: int,
    n_bands: int,
    height: int,
    width: int,
    settings: Settings,
    warp: bool,
    what: str,
) -> MemoryBudgetError:
    gib = 1024**3
    month = month_bytes(n, n_bands, height, width, warp)
    base = fixed_bytes(settings, n_bands, height, width, warp)
    need = base + month
    process = PROCESS_BASE_BYTES + settings.gdal_cache
    workers = settings.cpu_workers * WORK_TEMPORARIES * WORK_BYTES
    per_read = read_bytes(height, width, warp) + int(GDAL_ENV["VSI_CACHE_SIZE"])
    reads = settings.max_requests * per_read
    finished = base - process - workers - reads
    return MemoryBudgetError(
        f"{key} has {n} scenes and {what} ({width} x {height} pixels) needs about "
        f"{need / gib:.2f} GiB, more than the memory budget of {settings.memory_budget / gib:.2f} "
        f"GiB ({settings.memory_budget_source}). Nothing was downloaded.\n"
        f"  strip buffers ({n} scenes x {n_bands} bands): {month / gib:.2f} GiB\n"
        f"  process and GDAL cache: {process / gib:.2f} GiB\n"
        f"  {settings.cpu_workers} CPU workers' temporaries: {workers / gib:.2f} GiB\n"
        f"  {settings.max_requests} concurrent reads at {per_read / gib:.3f} GiB"
        f"{' (warped)' if warp else ''}: {reads / gib:.2f} GiB\n"
        f"  finished strips kept for carry-forward and writing: {finished / gib:.2f} GiB\n"
        "Options: if this much memory is free, pass --memory with at least "
        f"{math.ceil(need / gib * 10) / 10:.1f}GB (the default budget is half of the available "
        "memory, or three quarters of a cgroup or SLURM limit); lower --max-requests or "
        "--cpu-workers; or split the AOI into narrower ones (a strip spans the AOI's full "
        "width)."
    )


@dataclass(frozen=True)
class WindowPlan:
    rows: int  # rows per strip; the grid height when the whole month fits
    need: int  # bytes for one strip of the largest month, with the fixed costs
    reason: str


def plan_window(
    scenes_by_month: dict[str, list[SceneRef]],
    settings: Settings,
    height: int,
    width: int,
    rows: int | None = None,
    warp: bool = False,
) -> WindowPlan:
    """Rows per strip for a run: the whole grid when the largest month fits the budget.

    Otherwise the tallest strips that fit, with heights in multiples of CELL and as equal as
    that allows: a source block cut by a strip edge is read once for each strip, so taller
    strips read fewer bytes (512-row strips read 55 % more than the whole grid on the
    20-scene lab month, 1024-row strips 18 %). Reads of the next strip still start while one
    is composited whenever the budget has room. Raises MemoryBudgetError,
    before any download, when a strip of CELL rows (or of `rows`) does not fit. `warp`: some
    scene is on another CRS and is warped.
    """
    n_bands = len(REQUIRED_BANDS)
    key = max(scenes_by_month, key=lambda m: len(scenes_by_month[m]))
    n = len(scenes_by_month[key])

    def need(r: int) -> int:
        return fixed_bytes(settings, n_bands, r, width, warp) + month_bytes(
            n, n_bands, r, width, warp
        )

    def error(r: int, what: str) -> MemoryBudgetError:
        return _budget_error(key, n, n_bands, r, width, settings, warp, what)

    if rows is not None:
        rows = min(rows, height)
        if rows < 1 or (rows % CELL and rows != height):
            raise ValueError(f"strip rows must be a multiple of {CELL} or the grid height")
        if need(rows) > settings.memory_budget:
            raise error(rows, "a strip of the set size")
        return WindowPlan(rows, need(rows), "set by user")
    if need(height) <= settings.memory_budget:
        return WindowPlan(height, need(height), "the whole month fits the budget")
    smallest = min(CELL, height)
    if need(smallest) > settings.memory_budget:
        raise error(smallest, "its smallest strip")
    per_row = need(2) - need(1)  # the need is linear in the strip's rows
    limit = max(CELL, (settings.memory_budget - (need(1) - per_row)) // per_row // CELL * CELL)
    pieces = math.ceil(height / limit)
    rows = min(limit, math.ceil(math.ceil(height / pieces) / CELL) * CELL)
    gib = 1024**3
    reason = (
        f"{key} ({n} scenes) needs {need(height) / gib:.2f} GiB for the whole grid, more than "
        f"the budget of {settings.memory_budget / gib:.2f} GiB ({settings.memory_budget_source}); "
        f"a strip of {rows} rows needs {need(rows) / gib:.2f} GiB"
    )
    return WindowPlan(rows, need(rows), reason)


def check_memory(
    scenes_by_month: dict[str, list[SceneRef]], settings: Settings, height: int, width: int
) -> int:
    """Bytes the run needs at its strip size; MemoryBudgetError if no strip fits."""
    return plan_window(scenes_by_month, settings, height, width).need


# --- monthly files -----------------------------------------------------------------------------

# A month is a tiled GeoTIFF: the bands (uint16 DN), then the number of valid scenes per pixel,
# with the month's record in GDAL metadata. It is written window by window to a temporary name
# and renamed when complete. Months from before 2026-10-11 are .npz files and are still read.
MONTH_FORMAT = "chronozarr-s2-month/1"
COUNT_BAND = "valid_count"


def month_path(directory: Path, key: str) -> Path | None:
    """The saved file of month `key` in `directory`, GeoTIFF or legacy .npz, or None."""
    for suffix in (".tif", ".npz"):
        path = directory / f"{key}{suffix}"
        if path.exists():
            return path
    return None


def _cell_digests(bands: np.ndarray, r0: int, c0: int) -> dict[tuple[int, int], bytes]:
    """SHA-256 of each CELL x CELL cell of `bands` (n_bands, h, w) placed at grid (r0, c0)."""
    _, h, w = bands.shape
    return {
        ((r0 + r) // CELL, (c0 + c) // CELL): hashlib.sha256(
            np.ascontiguousarray(bands[:, r : r + CELL, c : c + CELL]).tobytes()
        ).digest()
        for r in range(0, h, CELL)
        for c in range(0, w, CELL)
    }


def _combine(digests: dict[tuple[int, int], bytes]) -> str:
    return hashlib.sha256(b"".join(digests[k] for k in sorted(digests))).hexdigest()


def bands_sha256(bands: np.ndarray) -> str:
    """Identity of a month's composite, recorded by the month whose gaps it filled.

    SHA-256 over the SHA-256 of each CELL x CELL cell in row-major order, so a month written
    window by window gets the same value whatever the window size.
    """
    return _combine(_cell_digests(bands, 0, 0))


class _MonthWriter:
    """Writes one month window by window; every call runs on the single writer thread."""

    def __init__(
        self, path: Path, grid: Grid, epsg: int, band_names: list[str], threads: int
    ) -> None:
        self.path = path
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        os.close(fd)
        self.tmp = Path(tmp)
        self.digests: dict[tuple[int, int], bytes] = {}
        self.dataset = rasterio.open(
            self.tmp,
            "w",
            driver="GTiff",
            height=grid.height,
            width=grid.width,
            count=len(band_names) + 1,
            dtype="uint16",
            crs=CRS.from_epsg(epsg),
            transform=grid.transform,
            tiled=True,
            blockxsize=CELL,
            blockysize=CELL,
            compress="zstd",
            zstd_level=1,
            predictor=2,
            interleave="band",
            bigtiff="if_safer",
            num_threads=threads,
        )
        for i, name in enumerate([*band_names, COUNT_BAND], start=1):
            self.dataset.set_band_description(i, name)

    def write(self, window: AoiWindow, bands: np.ndarray, count: np.ndarray) -> None:
        rio_window = Window.from_slices(*window.slices())
        n_bands = bands.shape[0]
        self.dataset.write(bands, indexes=list(range(1, n_bands + 1)), window=rio_window)
        self.dataset.write(count.astype(np.uint16), n_bands + 1, window=rio_window)
        self.digests.update(_cell_digests(bands, window.r0, window.c0))

    def finish(self, record: dict) -> str:
        """Close, record the month, rename into place; returns the month's bands_sha256."""
        digest = _combine(self.digests)
        tags = {k: json.dumps(v) for k, v in record.items()}
        self.dataset.update_tags(format=MONTH_FORMAT, bands_sha256=digest, **tags)
        self.dataset.close()
        os.replace(self.tmp, self.path)
        return digest

    def discard(self) -> None:
        if not self.dataset.closed:
            self.dataset.close()
        self.tmp.unlink(missing_ok=True)


def save_month(
    path: Path,
    grid: Grid,
    epsg: int,
    band_names: list[str],
    bands: np.ndarray,
    count: np.ndarray,
    record: dict,
) -> str:
    """Write a whole month at once (tests and conversion of .npz months)."""
    writer = _MonthWriter(path, grid, epsg, band_names, threads=1)
    try:
        writer.write(AoiWindow(0, grid.height, 0, grid.width), bands, count)
        return writer.finish(record)
    except BaseException:
        writer.discard()
        raise


def read_month_window(path: Path, window: AoiWindow, n_bands: int) -> np.ndarray:
    """The bands of a saved month inside `window`."""
    if path.suffix == ".npz":
        with np.load(path, allow_pickle=False) as data:
            return data["bands"][(slice(None), *window.slices())]
    with rasterio.open(path) as src:
        return src.read(list(range(1, n_bands + 1)), window=Window.from_slices(*window.slices()))


def month_record(path: Path) -> dict:
    """A saved month's record without its arrays: band_names, scenes_searched,
    scenes_failed, scenes_failed_windows, carried_from and bands_sha256.

    A legacy .npz has no bands_sha256 and is loaded to compute it. Fields a file predates are
    None: scenes_failed and carried_from (before 2026-10-10), scenes_searched.
    """
    if path.suffix == ".npz":
        loaded = load_mosaic(path)
        return {k: v for k, v in loaded.items() if k not in ("bands", "coverage", "valid_count")}
    with rasterio.open(path) as src:
        tags = src.tags()
        if tags.get("format") != MONTH_FORMAT:
            raise ValueError(f"{path} is not a monthly mosaic ({MONTH_FORMAT})")
        record: dict = {k: json.loads(tags[k]) for k in RECORD_KEYS}
        record["bands_sha256"] = tags["bands_sha256"]
        record["transform"] = src.transform
        record["epsg"] = src.crs.to_epsg()
        record["shape"] = (src.count - 1, src.height, src.width)
    return record


RECORD_KEYS = (
    "band_names",
    "scenes_searched",
    "scenes_failed",
    "scenes_failed_windows",
    "carried_from",
)


def load_mosaic(path: Path) -> dict:
    """Load a saved month, GeoTIFF or legacy .npz.

    Returns the record of `month_record` plus bands (uint16, n_bands x H x W), valid_count
    (uint16, H x W; None for an .npz without scenes_searched) and coverage (float32 k / n,
    exactly the array the .npz files stored).
    """
    if path.suffix == ".npz":
        with np.load(path, allow_pickle=False) as data:
            coeffs = data["transform"]
            n = int(data["scenes_searched"]) if "scenes_searched" in data else None
            bands = data["bands"]
            coverage = data["coverage"]
            loaded = {
                "bands": bands,
                "coverage": coverage,
                "valid_count": None
                if n is None
                else np.rint(coverage.astype(np.float64) * n).astype(np.uint16),
                "transform": Affine(*coeffs),
                "epsg": int(data["epsg"]),
                "shape": bands.shape,
                "band_names": [str(v) for v in data["band_names"]],
                "scenes_searched": n,
                "scenes_failed": [str(v) for v in data["scenes_failed"]]
                if "scenes_failed" in data
                else None,
                "scenes_failed_windows": None,
                "carried_from": [str(v) for v in data["carried_from"]]
                if "carried_from" in data
                else None,
                "bands_sha256": bands_sha256(bands),
            }
        return loaded
    record = month_record(path)
    with rasterio.open(path) as src:
        stack = src.read()
    bands, count = stack[:-1], stack[-1]
    n = record["scenes_searched"]
    coverage = count.astype(np.float32) / n if n else np.zeros(count.shape, dtype=np.float32)
    return {**record, "bands": bands, "valid_count": count, "coverage": coverage}


# --- the pipeline ------------------------------------------------------------------------------


@dataclass
class RunReport:
    """Counters and timings of one `build_monthly_mosaics` run (for diagnostics)."""

    months: int = 0
    months_skipped: int = 0
    scenes: int = 0
    scenes_no_valid: int = 0
    scenes_failed: int = 0
    reads: int = 0
    reads_native: int = 0
    read_retries: int = 0
    http_throttled: int = 0  # 429/5xx responses GDAL retried by itself
    pixels_read: int = 0
    pixels_window: int = 0
    window: tuple[int, int] | None = None  # (rows, cols) of each unit of work
    windows: int = 0  # windows per month
    window_reason: str = ""
    memory_estimate: int = 0  # bytes for one strip of the largest month with the fixed costs
    seconds: dict[str, float] = field(default_factory=dict)
    first_window_seconds: float | None = None
    first_month_seconds: float | None = None
    wall_seconds: float = 0.0
    months_not_written: list[str] = field(default_factory=list)  # --keep-going, all scenes failed
    months_incomplete: list[str] = field(default_factory=list)  # --keep-going, some scenes failed
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, stage: str | None, seconds: float = 0.0, **counts: int) -> None:
        with self._lock:
            if stage is not None:
                self.seconds[stage] = self.seconds.get(stage, 0.0) + seconds
            for name, value in counts.items():
                setattr(self, name, getattr(self, name) + value)


def run_summary(
    report: RunReport, limiter: AdaptiveLimiter, settings: Settings, cpus: int, cpu_seconds: float
) -> dict:
    """Numbers and a bottleneck verdict for one run (diagnostics and benchmark records)."""
    wall = max(report.wall_seconds, 1e-9)
    # Time-weighted mean of the request limit over the run.
    points = [(e.t, e.limit) for e in limiter.events] + [(wall, limiter.limit)]
    mean_limit = sum((t1 - t0) * lim for (t0, lim), (t1, _) in pairwise(points)) / wall
    occupancy = limiter.busy_seconds / (wall * max(mean_limit, 1e-9))
    cpu_util = cpu_seconds / (wall * cpus)
    if cpu_util >= 0.75:
        verdict = f"CPU: the process used {cpu_util:.0%} of {cpus} CPUs"
    elif occupancy >= 0.7:
        verdict = (
            f"remote reads: request slots were busy {occupancy:.0%} of the run while CPU use was "
            f"{cpu_util:.0%}, so the network or the host limits throughput"
        )
    else:
        verdict = (
            f"neither saturated (request slots busy {occupancy:.0%}, CPU {cpu_util:.0%}): "
            "latency, few scenes per month, or a small AOI"
        )

    def rounded(value: float | None) -> float | None:
        return None if value is None else round(value, 3)

    return {
        "wall_seconds": round(report.wall_seconds, 3),
        "first_window_seconds": rounded(report.first_window_seconds),
        "first_month_seconds": rounded(report.first_month_seconds),
        "months": report.months,
        "months_skipped": report.months_skipped,
        "window": None if report.window is None else list(report.window),
        "windows_per_month": report.windows,
        "window_reason": report.window_reason,
        "memory_estimate_bytes": report.memory_estimate,
        "scenes": report.scenes,
        "scenes_no_valid": report.scenes_no_valid,
        "scenes_failed": report.scenes_failed,
        "reads": report.reads,
        "reads_native": report.reads_native,
        "read_retries": report.read_retries,
        "http_throttled": report.http_throttled,
        "pixel_fraction_read": round(report.pixels_read / max(report.pixels_window, 1), 4),
        "stage_thread_seconds": {k: round(v, 2) for k, v in report.seconds.items()},
        "cpu_seconds": round(cpu_seconds, 2),
        "cpu_utilisation": round(cpu_util, 3),
        "request_slot_occupancy": round(occupancy, 3),
        "mean_request_limit": round(mean_limit, 2),
        "final_request_limit": limiter.limit,
        "limiter_events": [
            {
                "t": round(e.t, 2),
                "limit": e.limit,
                "rate_MBps": None if e.rate is None else round(e.rate / 1e6, 2),
                "reason": e.reason,
            }
            for e in limiter.events
        ],
        "months_not_written": report.months_not_written,
        "months_incomplete": report.months_incomplete,
        "bottleneck": verdict,
    }


@dataclass
class _Unit:
    """One window of one month: its scene stack while it is read and composited."""

    key: str
    index: int  # window index
    window: AoiWindow
    grid: Grid
    scenes: list[SceneRef]
    stack: np.ndarray
    nbytes: int
    pending: int  # scenes not yet finished
    bands_left: dict[int, int] = field(default_factory=dict)
    failed: set[int] = field(default_factory=set)
    errors: dict[int, str] = field(default_factory=dict)
    valid: dict[int, np.ndarray] = field(default_factory=dict)
    result: tuple[np.ndarray, np.ndarray] | None = None  # composite, valid counts
    coordinates: dict = field(default_factory=dict)  # source CRS -> exact pixel centres
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def centres(self, src_crs: CRS) -> Coordinates:
        """The window's pixel centres in `src_crs`, computed once for all its scenes."""
        with self.lock:
            key = src_crs.to_wkt()
            if key not in self.coordinates:
                self.coordinates[key] = source_coordinates(self.grid, src_crs)
            return self.coordinates[key]


@dataclass
class _MonthState:
    """What the windows of one month found, kept until the month's file is complete."""

    writer: _MonthWriter | None = None
    failed_windows: dict[str, list[list[int]]] = field(default_factory=dict)
    first_error: str = ""
    window_all_failed: bool = False  # some window lost every scene: the month is not written
    carried_from: list[str] = field(default_factory=list)
    valid_observations: int = 0


def build_monthly_mosaics(
    scenes_by_month: dict[str, list[SceneRef]],
    bbox_wgs84: tuple[float, float, float, float],
    target_epsg: int,
    output_dir: Path,
    *,
    settings: Settings,
    sign: Callable[[str], str],
    carry_forward: bool = True,
    report: RunReport | None = None,
    limiter: AdaptiveLimiter | None = None,
    keep_going: bool = False,
    strip_rows: int | None = None,
) -> dict[str, Path]:
    """Build monthly composites and save them as GeoTIFFs (YYYY-MM.tif).

    Each month is composited in full-width strips of the AOI grid (`plan_window`): the whole
    grid when the largest month fits the memory budget, otherwise strips whose heights are
    multiples of CELL. Every step after the read is per pixel and the reads are exact on any
    strip, so the output does not depend on the strip height.

    Args:
        scenes_by_month: Dict mapping "YYYY-MM" to scene lists
        bbox_wgs84: AOI bounding box in WGS84
        target_epsg: Target EPSG for output grid
        output_dir: Directory for the monthly files
        settings: Concurrency and memory settings (performance.plan_settings)
        sign: Turns an asset href into a readable URL (e.g. adds a SAS token); called right
            before each open so expiring tokens are refreshed
        carry_forward: If True, fill gaps with previous month's data
        report: Filled with counters and timings when given
        limiter: Concurrent read limiter; built from `settings` when omitted
        keep_going: By default a scene that still fails after every attempt raises
            SceneReadError when its window is finished; earlier months stay saved and the
            month's partial file is removed. With keep_going, a month with failed scenes is
            written and lists them, with the windows they are missing from, and a month where
            some window lost every scene is not written (it is retried by the next run)
            instead of being filled from the month before.
        strip_rows: rows per strip instead of the planned number.

    Raises:
        MemoryBudgetError: before any download, when no window fits the budget.
        SceneReadError: see `keep_going`.

    Returns:
        Dict mapping "YYYY-MM" to output file path (existing .npz months keep their path).

    Each file holds the bands (uint16 DN) and a last band with the number of valid scenes per
    pixel, and in its metadata: band_names, scenes_searched (the n of coverage = k / n),
    scenes_failed (item IDs that could not be read; empty unless keep_going),
    scenes_failed_windows ({item ID: [[r0, r1, c0, c1], ...]}: the grid windows it is missing
    from), carried_from (empty or [month, bands_sha256 of that month]: the month whose
    composite filled this month's gaps) and bands_sha256 (this month's own identity).
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    report = report if report is not None else RunReport()
    limiter = limiter or AdaptiveLimiter(
        settings.requests, settings.max_requests, adaptive=settings.adaptive
    )
    transform, height, width = compute_target_grid(bbox_wgs84, target_epsg)
    grid = Grid(transform, CRS.from_epsg(target_epsg), height, width)
    env = {
        **GDAL_ENV,
        "GDAL_CACHEMAX": settings.gdal_cache,
    }
    band_names = list(REQUIRED_BANDS)
    n_bands = len(band_names)
    t_start = time.perf_counter()

    months = sorted(scenes_by_month)
    outputs: dict[str, Path] = {}
    for m in months:
        existing = month_path(output_dir, m)
        if existing is not None:
            outputs[m] = existing
            logger.info("Skipping %s (already exists)", m)
    todo = [m for m in months if m not in outputs]
    report.months, report.months_skipped = len(months), len(months) - len(todo)
    if not todo:
        return dict(sorted(outputs.items()))
    warp = any(s.epsg != target_epsg for m in todo for s in scenes_by_month[m])
    plan = plan_window(
        {m: scenes_by_month[m] for m in todo}, settings, height, width, strip_rows, warp
    )
    windows = split_grid(height, width, plan.rows)
    report.window, report.windows = (plan.rows, width), len(windows)
    report.window_reason = plan.reason
    report.memory_estimate = plan.need
    if len(windows) > 1:
        logger.info(
            "Compositing each month in %d strips of %d rows: %s",
            len(windows),
            plan.rows,
            plan.reason,
        )
    units = [(m, w) for m in todo for w in range(len(windows))]

    first_href = next((s.scl_href for m in todo for s in scenes_by_month[m]), None)
    if first_href is not None:
        sign(first_href)  # fetch the first token once, before threads race for it

    stop = threading.Event()
    throttle = HttpThrottleCounter()
    gdal_logger = logging.getLogger("rasterio._env")
    gdal_logger.addFilter(throttle)

    def read(
        href: str, target: Grid, out: np.ndarray, resampling: Resampling, needed=None, centres=None
    ):
        for attempt in range(READ_ATTEMPTS):
            if stop.is_set():
                raise RuntimeError("cancelled")
            t0 = time.perf_counter()
            if attempt:
                out[...] = 0  # a failed attempt may have filled part of it
            url = sign(href)
            try:
                with limiter.slot():
                    pixels, native = read_asset(
                        url, target, out, resampling, env, needed=needed, coordinates=centres
                    )
            except Exception as e:  # network, auth expiry, throttling, truncated data
                forget_url(url)
                resolve_failure = RESOLVE_FAILURE in str(e)
                limiter.throttled(throttle.events)
                if not resolve_failure:
                    limiter.record(False)
                report.add("read", time.perf_counter() - t0, read_retries=1)
                if attempt == READ_ATTEMPTS - 1:
                    raise
                wait_s = min(READ_BACKOFF_MAX_SECONDS, READ_BACKOFF_SECONDS * 2**attempt)
                wait_s *= 0.5 + random.random()
                if resolve_failure:
                    wait_s = max(wait_s, RESOLVE_RETRY_SECONDS)
                logger.warning(
                    "Read failed (%s), attempt %d/%d, retrying in %.1fs: %s",
                    href.rsplit("/", 1)[-1],
                    attempt + 1,
                    READ_ATTEMPTS,
                    wait_s,
                    e,
                )
                time.sleep(wait_s)
                continue
            limiter.throttled(throttle.events)
            limiter.record(True, pixels * out.itemsize)
            report.add(
                "read",
                time.perf_counter() - t0,
                reads=1,
                reads_native=int(native),
                pixels_read=pixels,
                pixels_window=out.size,
            )
            return

    def read_scl(unit: _Unit, scene: SceneRef) -> np.ndarray:
        scl = np.zeros((unit.grid.height, unit.grid.width), dtype=np.uint8)
        read(scene.scl_href, unit.grid, scl, Resampling.nearest, centres=unit.centres)
        return _SCL_LUT[scl]

    def read_band(unit: _Unit, i: int, b: int, needed: np.ndarray) -> None:
        # stack[i, b] is a contiguous zeroed plane; the read fills it in place.
        href = unit.scenes[i].asset_hrefs[REQUIRED_BANDS[b]]
        read(href, unit.grid, unit.stack[i, b], Resampling.bilinear, needed)

    def assemble(unit: _Unit, i: int) -> int:
        t0 = time.perf_counter()
        n_valid = apply_scene_mask(unit.stack[i], unit.valid.pop(i), unit.scenes[i].boa_offset)
        report.add("mask", time.perf_counter() - t0)
        return n_valid

    def composite(unit: _Unit) -> tuple[np.ndarray, np.ndarray]:
        t0 = time.perf_counter()
        result = median_composite(unit.stack, cpu_pool)
        report.add("median", time.perf_counter() - t0)
        return result

    def write_window(state: _MonthState, key: str, unit: _Unit, bands, count) -> None:
        t0 = time.perf_counter()
        if state.writer is None:
            state.writer = _MonthWriter(
                output_dir / f"{key}.tif", grid, target_epsg, band_names, settings.cpu_workers
            )
        state.writer.write(unit.window, bands, count)
        report.add("write", time.perf_counter() - t0)

    def finish_month(state: _MonthState, key: str) -> str:
        t0 = time.perf_counter()
        assert state.writer is not None
        failed = sorted(state.failed_windows)
        digest = state.writer.finish(
            {
                "band_names": band_names,
                "scenes_searched": len(scenes_by_month[key]),
                "scenes_failed": failed,
                "scenes_failed_windows": state.failed_windows,
                "carried_from": state.carried_from,
            }
        )
        report.add("write", time.perf_counter() - t0)
        logger.info(
            "Saved %s (%.2f MB)", state.writer.path, state.writer.path.stat().st_size / 1e6
        )
        return digest

    def discard_month(state: _MonthState) -> None:
        if state.writer is not None:
            state.writer.discard()

    fetch_pool = ThreadPoolExecutor(settings.max_requests, thread_name_prefix="fetch")
    cpu_pool = ThreadPoolExecutor(settings.cpu_workers, thread_name_prefix="cpu")
    finish_pool = ThreadPoolExecutor(1, thread_name_prefix="median")
    write_pool = ThreadPoolExecutor(1, thread_name_prefix="write")
    tasks: dict[Future, tuple] = {}
    base_bytes = fixed_bytes(settings, n_bands, plan.rows, width, warp)
    active: dict[tuple[str, int], _Unit] = {}
    states: dict[str, _MonthState] = {}
    closing: dict[str, Future] = {}  # month -> future of its finish_month (returns its digest)
    digests: dict[str, str] = {}
    in_flight = 0
    next_admit = 0
    next_finish = 0
    write_future: Future | None = None
    last_final: tuple[str, int, np.ndarray] | None = None  # (month, window, bands) just finished
    failure_seen = False

    def admit() -> None:
        nonlocal next_admit, in_flight
        while next_admit < len(units):
            if failure_seen and not keep_going:
                return  # the run stops at the failed month; do not start later windows
            # Keep the read queue fed but bounded: about two reads waiting per slot.
            queued = sum(1 for kind, _, _ in tasks.values() if kind in ("scl", "band"))
            if active and queued >= 2 * settings.max_requests:
                return
            key, w = units[next_admit]
            scenes = scenes_by_month[key]
            win = windows[w]
            need = month_bytes(len(scenes), n_bands, win.height, win.width, warp)
            if active and base_bytes + in_flight + need > settings.memory_budget:
                return
            stack = np.zeros((len(scenes), n_bands, win.height, win.width), dtype=np.uint16)
            unit = _Unit(key, w, win, win.subgrid(grid), scenes, stack, need, len(scenes))
            active[key, w] = unit
            in_flight += need
            next_admit += 1
            if w == 0:
                states[key] = _MonthState()
                logger.info("=== Processing %s (%d scenes) ===", key, len(scenes))
                report.add(None, scenes=len(scenes))
            if not scenes:  # nothing to read: an all-zero month, as before
                unit.result = (
                    np.zeros((n_bands, win.height, win.width), dtype=np.uint16),
                    np.zeros((win.height, win.width), dtype=np.uint16),
                )
            for i, scene in enumerate(scenes):
                tasks[fetch_pool.submit(read_scl, unit, scene)] = ("scl", unit, i)

    def scene_done(unit: _Unit) -> None:
        unit.pending -= 1
        if unit.pending == 0:
            tasks[finish_pool.submit(composite, unit)] = ("median", unit, -1)

    def scene_failed(unit: _Unit, i: int, error: Exception) -> None:
        nonlocal failure_seen
        logger.warning("Scene %s failed: %s", unit.scenes[i].item_id, error)
        unit.failed.add(i)
        unit.errors[i] = str(error)
        failure_seen = True
        report.add(None, scenes_failed=1)

    def month_digest(key: str) -> str:
        if key not in digests:
            future = closing.get(key)
            digests[key] = (
                future.result()
                if future is not None
                else month_record(outputs[key])["bands_sha256"]
            )
        return digests[key]

    def previous_window(key: str, unit: _Unit) -> tuple[str, np.ndarray] | None:
        """The same window of the latest earlier month with a file: the carry-forward source."""
        earlier = [m for m in outputs if m < key]
        if not earlier:
            return None
        prev_key = max(earlier)
        if last_final is not None and last_final[:2] == (prev_key, unit.index):
            return prev_key, last_final[2]
        month_digest(prev_key)  # waits for the file to be complete
        return prev_key, read_month_window(outputs[prev_key], unit.window, n_bands)

    try:
        admit()
        while tasks:
            done, _ = wait(list(tasks), return_when=FIRST_COMPLETED)
            for future in done:
                kind, unit, i = tasks.pop(future)
                if kind == "scl":
                    try:
                        valid = future.result()
                    except Exception as e:
                        scene_failed(unit, i, e)
                        scene_done(unit)
                        continue
                    if not valid.any():
                        report.add(None, scenes_no_valid=1)
                        scene_done(unit)
                        continue
                    unit.valid[i] = valid
                    unit.bands_left[i] = n_bands
                    for b in range(n_bands):
                        tasks[fetch_pool.submit(read_band, unit, i, b, valid)] = ("band", unit, i)
                elif kind == "band":
                    try:
                        future.result()
                    except Exception as e:
                        if i not in unit.failed:
                            scene_failed(unit, i, e)
                    unit.bands_left[i] -= 1
                    if unit.bands_left[i] > 0:
                        continue
                    if i in unit.failed:
                        unit.stack[i] = 0
                        unit.valid.pop(i, None)
                        scene_done(unit)
                    else:
                        tasks[cpu_pool.submit(assemble, unit, i)] = ("mask", unit, i)
                elif kind == "mask":
                    future.result()
                    scene_done(unit)
                elif kind == "median":
                    unit.result = future.result()
                    unit.stack = np.empty(0, dtype=np.uint16)  # release the scene stack
                    unit.coordinates.clear()

            # Finish windows in order: carry-forward needs the same window of the month before.
            while next_finish < len(units):
                key, w = units[next_finish]
                unit = active.get((key, w))
                if unit is None or unit.result is None:
                    break
                state = states[key]
                for i in sorted(unit.failed):
                    item = unit.scenes[i].item_id
                    state.failed_windows.setdefault(item, []).append(unit.window.bounds())
                    state.first_error = state.first_error or unit.errors[i]
                if unit.failed and len(unit.failed) == len(unit.scenes):
                    state.window_all_failed = True
                if unit.failed and not keep_going:
                    ids = ", ".join(sorted(unit.scenes[i].item_id for i in unit.failed))
                    where = "" if len(windows) == 1 else f" (window {unit.window.bounds()})"
                    raise SceneReadError(
                        f"{len(unit.failed)} of {len(unit.scenes)} scenes of {key}{where} could "
                        f"not be read after {READ_ATTEMPTS} attempts each ({ids}; first error: "
                        f"{state.first_error}). A median without them would not be the month's "
                        "composite, so nothing was written for it; earlier months are saved. "
                        "Re-run to resume from this month. --keep-going writes such months "
                        "with the failed scenes listed in the file, and skips months where "
                        "every scene failed."
                    )
                bands, count = unit.result
                state.valid_observations += int(count.sum(dtype=np.int64))
                if carry_forward:
                    prev = previous_window(key, unit)
                    if prev is not None:
                        prev_key, prev_bands = prev
                        state.carried_from = [prev_key, month_digest(prev_key)]
                        gap_mask = np.all(bands == 0, axis=0)
                        if gap_mask.any():
                            bands[:, gap_mask] = prev_bands[:, gap_mask]
                last_final = (key, w, bands)
                if write_future is not None:
                    write_future.result()  # at most one window waiting to be written
                write_future = write_pool.submit(write_window, state, key, unit, bands, count)
                if report.first_window_seconds is None:
                    write_future.result()
                    report.first_window_seconds = time.perf_counter() - t_start
                del active[key, w]
                in_flight -= unit.nbytes
                next_finish += 1
                if w < len(windows) - 1:
                    continue

                # The month's last window: complete its file, or drop it.
                failed_ids = sorted(state.failed_windows)
                if state.window_all_failed:
                    logger.warning(
                        "Every scene of %s failed in at least one window; not writing it (the "
                        "next run retries it)",
                        key,
                    )
                    write_future = write_pool.submit(discard_month, state)
                    report.months_not_written.append(key)
                    last_final = None
                    del states[key]
                    continue
                if failed_ids:
                    logger.warning(
                        "Writing %s without %d of %d scenes (listed in the file): %s",
                        key,
                        len(failed_ids),
                        len(unit.scenes),
                        ", ".join(failed_ids),
                    )
                    report.months_incomplete.append(key)
                logger.info(
                    "Monthly composite %s: %d scenes, mean coverage %.1f%%",
                    key,
                    len(unit.scenes),
                    100.0 * state.valid_observations / max(1, len(unit.scenes) * height * width),
                )
                write_future = closing[key] = write_pool.submit(finish_month, state, key)
                outputs[key] = output_dir / f"{key}.tif"
                if report.first_month_seconds is None:
                    month_digest(key)
                    report.first_month_seconds = time.perf_counter() - t_start
                del states[key]
            admit()
        if next_finish < len(units):
            raise RuntimeError(
                f"internal error: {units[next_finish]} was never finished; no tasks are left"
            )
        for key in closing:
            month_digest(key)
        if write_future is not None:
            write_future.result()
    except BaseException:
        stop.set()
        for future in tasks:
            future.cancel()
        raise
    finally:
        for pool in (fetch_pool, cpu_pool, finish_pool, write_pool):
            pool.shutdown(wait=True, cancel_futures=True)
        for state in states.values():  # months left unfinished by an error
            discard_month(state)
        gdal_logger.removeFilter(throttle)
        report.http_throttled = throttle.events
        report.wall_seconds = time.perf_counter() - t_start
    return dict(sorted(outputs.items()))
