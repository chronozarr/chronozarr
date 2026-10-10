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
import logging
import math
import os
import random
import re
import tempfile
import threading
import time
import zipfile
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
from rasterio.warp import Resampling, reproject
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
READ_BACKOFF_SECONDS = 2.0
READ_BACKOFF_MAX_SECONDS = 30.0


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


def read_asset(
    href: str,
    grid: Grid,
    out: np.ndarray,
    resampling: Resampling,
    env: dict,
    needed: np.ndarray | None = None,
) -> tuple[int, bool]:
    """Read one single-band asset into `out` (grid shape). Returns (pixels read, native path)."""
    with rasterio.Env(**env), rasterio.open(href) as src:
        offset = native_offset(src.transform, src.crs, grid)
        exact = offset is not None and (offset[2] == 1 or resampling == Resampling.nearest)
        if exact:
            assert offset is not None
            return read_native(src, grid, offset, out, needed), True
        reproject(
            source=rasterio.band(src, 1),
            destination=out,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=grid.transform,
            dst_crs=grid.crs,
            resampling=resampling,
        )
        return out.size, False


# --- compositing -------------------------------------------------------------------------------


def apply_scene_mask(bands: np.ndarray, valid: np.ndarray, boa_offset: int) -> int:
    """Apply the BOA offset and zero every band where the scene is not valid, in place.

    `bands` is (n_bands, H, W) uint16 and `valid` the SCL mask; a pixel is also invalid where
    any band is 0. Returns the number of valid pixels.
    """
    rows = 256  # row blocks keep temporaries to a few MB per call
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
    stack: np.ndarray, n_scenes: int, pool: ThreadPoolExecutor | None = None, rows: int = 128
) -> tuple[np.ndarray, np.ndarray]:
    """(composite uint16 (n_bands, H, W), coverage float32 (H, W)) from a masked stack."""
    _, n_bands, height, width = stack.shape
    composite = np.empty((n_bands, height, width), dtype=np.uint16)
    count = np.empty((height, width), dtype=np.int32)

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
    coverage = count.astype(np.float32) / n_scenes
    return composite, coverage


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
    seconds: dict[str, float] = field(default_factory=dict)
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
    return {
        "wall_seconds": round(report.wall_seconds, 3),
        "first_month_seconds": None
        if report.first_month_seconds is None
        else round(report.first_month_seconds, 3),
        "months": report.months,
        "months_skipped": report.months_skipped,
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
class _Month:
    key: str
    scenes: list[SceneRef]
    stack: np.ndarray
    nbytes: int
    pending: int  # scenes not yet finished
    bands_left: dict[int, int] = field(default_factory=dict)
    failed: set[int] = field(default_factory=set)
    errors: dict[int, str] = field(default_factory=dict)
    valid: dict[int, np.ndarray] = field(default_factory=dict)
    result: tuple[np.ndarray, np.ndarray] | None = None


class MemoryBudgetError(RuntimeError):
    """The largest month cannot be composited within the memory budget."""


class SceneReadError(RuntimeError):
    """A scene still failed after every read attempt."""


# Interpreter, numpy, rasterio, GDAL and allocator slack. Set so that the estimate for a
# 20-scene Ucayali month (2.41 GB) matches the peak RSS measured in Docker (2.39-2.43 GB,
# 2026-10-10, 1 and 2 CPUs).
PROCESS_BASE_BYTES = 512 * 1024**2


def month_bytes(n_scenes: int, n_bands: int, height: int, width: int) -> int:
    """Memory one month holds while it is read and composited."""
    plane = height * width
    stack = n_scenes * n_bands * plane * 2
    masks = n_scenes * plane  # SCL masks held until each scene is masked
    composite = n_bands * plane * 2 + plane * 8  # composite, coverage, valid counts
    return stack + masks + composite


def fixed_bytes(settings: Settings, n_bands: int, height: int, width: int) -> int:
    """Memory used whatever the months: process, GDAL cache, read buffers, finished months.

    Each read in flight holds at most one grid-sized buffer: the SCL read decodes into a uint8
    plane and builds a bool mask, a band read copies windows of at most a uint16 plane. The
    month before the one being finished is kept for carry-forward, and one finished month may
    wait for the writer.
    """
    plane = height * width
    reads = settings.max_requests * 2 * plane
    finished = 2 * (n_bands * plane * 2 + plane * 4)
    return PROCESS_BASE_BYTES + settings.gdal_cache + reads + finished


def check_memory(
    scenes_by_month: dict[str, list[SceneRef]], settings: Settings, height: int, width: int
) -> int:
    """Raise MemoryBudgetError when the largest month needs more than the budget.

    Returns the bytes the largest month needs. The estimate was checked against measured peak
    RSS (bench/s2-ingest/portability.md); it scales with scenes per month and AOI pixels.
    """
    n_bands = len(REQUIRED_BANDS)
    plane = height * width
    base = fixed_bytes(settings, n_bands, height, width)
    key = max(scenes_by_month, key=lambda m: len(scenes_by_month[m]))
    n = len(scenes_by_month[key])
    month = month_bytes(n, n_bands, height, width)
    need = base + month
    if need <= settings.memory_budget:
        return need
    gib = 1024**3
    process = PROCESS_BASE_BYTES + settings.gdal_cache
    per_read = 2 * plane
    reads = settings.max_requests * per_read
    finished = base - process - reads
    raise MemoryBudgetError(
        f"{key} has {n} scenes on a {width} x {height} grid and needs about {need / gib:.2f} "
        f"GiB, more than the memory budget of {settings.memory_budget / gib:.2f} GiB "
        f"({settings.memory_budget_source}). Nothing was downloaded.\n"
        f"  month buffers ({n} scenes x {n_bands} bands): {month / gib:.2f} GiB\n"
        f"  process and GDAL cache: {process / gib:.2f} GiB\n"
        f"  {settings.max_requests} concurrent reads at {per_read / gib:.3f} GiB: "
        f"{reads / gib:.2f} GiB\n"
        f"  finished months kept for carry-forward and writing: {finished / gib:.2f} GiB\n"
        "Options: if this much memory is free, pass --memory with at least "
        f"{math.ceil(need / gib * 10) / 10:.1f}GB (the default budget is half of the available "
        "memory); lower "
        "--max-requests; or split the AOI or date range. A month is held in memory whole, so "
        "a large AOI with many scenes per month needs it all at once."
    )


def _save_npz(path: Path, arrays: dict[str, np.ndarray]) -> None:
    """Write an .npz atomically so an interrupted run never leaves a half-written month.

    The file is what `np.savez_compressed` writes (a deflated zip of .npy members), at zlib
    level 1 instead of 6: 0.8 s instead of 2.6 s for a 2765 x 2759 month, 2 % larger.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
    try:
        with (
            os.fdopen(fd, "wb") as f,
            zipfile.ZipFile(f, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive,
        ):
            for name, array in arrays.items():
                with archive.open(f"{name}.npy", "w", force_zip64=True) as member:
                    np.lib.format.write_array(member, np.asanyarray(array), allow_pickle=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
) -> dict[str, Path]:
    """Build monthly composites and save them as .npz files.

    Args:
        scenes_by_month: Dict mapping "YYYY-MM" to scene lists
        bbox_wgs84: AOI bounding box in WGS84
        target_epsg: Target EPSG for output grid
        output_dir: Directory to write monthly .npz files
        settings: Concurrency and memory settings (performance.plan_settings)
        sign: Turns an asset href into a readable URL (e.g. adds a SAS token); called right
            before each open so expiring tokens are refreshed
        carry_forward: If True, fill gaps with previous month's data
        report: Filled with counters and timings when given
        limiter: Concurrent read limiter; built from `settings` when omitted
        keep_going: By default a scene that still fails after every attempt raises
            SceneReadError when its month is finished; earlier months stay saved. With
            keep_going, a month with some failed scenes is written and lists them in
            `scenes_failed`, and a month whose scenes all failed is not written (it is retried
            by the next run) instead of being filled from the month before.

    Raises:
        MemoryBudgetError: before any download, when the largest month does not fit.
        SceneReadError: see `keep_going`.

    Returns:
        Dict mapping "YYYY-MM" to output file path.

    Each .npz file contains:
        - bands: uint16 (n_bands, height, width)
        - coverage: float32 (height, width)
        - transform: 6 affine coefficients
        - epsg: int
        - band_names: string array
        - scenes_searched: int, scenes of the month (the n in coverage = k / n)
        - scenes_failed: string array, item IDs that could not be read (empty unless keep_going)
        - carried_from: string array, empty or [month, sha256 of its bands]: the month whose
          composite filled this month's gaps, so a stale chain can be detected
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
    n_bands = len(REQUIRED_BANDS)
    t_start = time.perf_counter()

    months = sorted(scenes_by_month)
    paths = {m: output_dir / f"{m}.npz" for m in months}
    todo = [m for m in months if not paths[m].exists()]
    outputs = {m: p for m, p in paths.items() if p.exists()}
    report.months, report.months_skipped = len(months), len(months) - len(todo)
    for m in months:
        if m not in todo:
            logger.info("Skipping %s (already exists)", m)
    if not todo:
        return dict(sorted(outputs.items()))
    check_memory({m: scenes_by_month[m] for m in todo}, settings, height, width)

    first_href = next((s.scl_href for m in todo for s in scenes_by_month[m]), None)
    if first_href is not None:
        sign(first_href)  # fetch the first token once, before threads race for it

    stop = threading.Event()
    throttle = HttpThrottleCounter()
    gdal_logger = logging.getLogger("rasterio._env")
    gdal_logger.addFilter(throttle)

    def read(href: str, out: np.ndarray, resampling: Resampling, needed=None) -> None:
        for attempt in range(READ_ATTEMPTS):
            if stop.is_set():
                raise RuntimeError("cancelled")
            t0 = time.perf_counter()
            if attempt:
                out[...] = 0  # a failed attempt may have filled part of it
            url = sign(href)
            try:
                with limiter.slot():
                    pixels, native = read_asset(url, grid, out, resampling, env, needed=needed)
            except Exception as e:  # network, auth expiry, throttling, truncated data
                forget_url(url)
                limiter.throttled(throttle.events)
                limiter.record(False)
                report.add("read", time.perf_counter() - t0, read_retries=1)
                if attempt == READ_ATTEMPTS - 1:
                    raise
                wait_s = min(READ_BACKOFF_MAX_SECONDS, READ_BACKOFF_SECONDS * 2**attempt)
                wait_s *= 0.5 + random.random()
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

    def read_scl(scene: SceneRef) -> np.ndarray:
        scl = np.zeros((height, width), dtype=np.uint8)
        read(scene.scl_href, scl, Resampling.nearest)
        return _SCL_LUT[scl]

    def read_band(month: _Month, i: int, b: int, needed: np.ndarray) -> None:
        # stack[i, b] is a contiguous zeroed plane; the read fills it in place.
        read(
            month.scenes[i].asset_hrefs[REQUIRED_BANDS[b]],
            month.stack[i, b],
            Resampling.bilinear,
            needed,
        )

    def assemble(month: _Month, i: int) -> int:
        t0 = time.perf_counter()
        n_valid = apply_scene_mask(month.stack[i], month.valid.pop(i), month.scenes[i].boa_offset)
        report.add("mask", time.perf_counter() - t0)
        return n_valid

    def composite(month: _Month) -> tuple[np.ndarray, np.ndarray]:
        t0 = time.perf_counter()
        result = median_composite(month.stack, len(month.scenes), cpu_pool)
        report.add("median", time.perf_counter() - t0)
        return result

    def save(
        key: str,
        bands: np.ndarray,
        coverage: np.ndarray,
        failed_ids: list[str],
        carried_from: list[str],
    ) -> Path:
        t0 = time.perf_counter()
        _save_npz(
            paths[key],
            {
                "bands": bands,
                "coverage": coverage,
                "transform": np.array(list(transform)[:6]),
                "epsg": np.array(target_epsg),
                "band_names": np.array(list(REQUIRED_BANDS)),
                "scenes_searched": np.array(len(scenes_by_month[key])),
                "scenes_failed": np.array(failed_ids, dtype=str),
                "carried_from": np.array(carried_from, dtype=str),
            },
        )
        report.add("write", time.perf_counter() - t0)
        logger.info("Saved %s (%.2f MB)", paths[key], paths[key].stat().st_size / 1e6)
        return paths[key]

    fetch_pool = ThreadPoolExecutor(settings.max_requests, thread_name_prefix="fetch")
    cpu_pool = ThreadPoolExecutor(settings.cpu_workers, thread_name_prefix="cpu")
    finish_pool = ThreadPoolExecutor(1, thread_name_prefix="median")
    write_pool = ThreadPoolExecutor(1, thread_name_prefix="write")
    tasks: dict[Future, tuple] = {}
    base_bytes = fixed_bytes(settings, n_bands, height, width)
    active: dict[str, _Month] = {}
    in_flight = 0
    next_admit = 0
    next_finish = 0
    write_future: Future | None = None
    last_written: tuple[str, np.ndarray] | None = None  # cached previous month for carry-forward
    failure_seen = False

    def admit() -> None:
        nonlocal next_admit, in_flight
        while next_admit < len(todo):
            if failure_seen and not keep_going:
                return  # the run stops at the failed month; do not start later ones
            # Keep the read queue fed but bounded: about two reads waiting per slot.
            queued = sum(1 for kind, _, _ in tasks.values() if kind in ("scl", "band"))
            if active and queued >= 2 * settings.max_requests:
                return
            key = todo[next_admit]
            scenes = scenes_by_month[key]
            need = month_bytes(len(scenes), n_bands, height, width)
            if active and base_bytes + in_flight + need > settings.memory_budget:
                return
            stack = np.zeros((len(scenes), n_bands, height, width), dtype=np.uint16)
            month = _Month(key, scenes, stack, need, pending=len(scenes))
            active[key] = month
            in_flight += need
            next_admit += 1
            logger.info("=== Processing %s (%d scenes) ===", key, len(scenes))
            report.add(None, scenes=len(scenes))
            if not scenes:  # nothing to read: an all-zero month, as before
                month.result = (
                    np.zeros((n_bands, height, width), dtype=np.uint16),
                    np.zeros((height, width), dtype=np.float32),
                )
            for i, scene in enumerate(scenes):
                tasks[fetch_pool.submit(read_scl, scene)] = ("scl", month, i)

    def scene_done(month: _Month, i: int) -> None:
        month.pending -= 1
        if month.pending == 0:
            tasks[finish_pool.submit(composite, month)] = ("median", month, -1)

    def previous_month(key: str) -> tuple[str, np.ndarray] | None:
        """The latest month before `key` that has a file: the source of carry-forward."""
        earlier = [m for m in outputs if m < key]
        if not earlier:
            return None
        prev_key = max(earlier)
        if last_written is not None and last_written[0] == prev_key:
            return last_written
        return prev_key, load_mosaic(paths[prev_key])["bands"]

    try:
        admit()
        while tasks:
            done, _ = wait(list(tasks), return_when=FIRST_COMPLETED)
            for future in done:
                kind, month, i = tasks.pop(future)
                if kind == "scl":
                    try:
                        valid = future.result()
                    except Exception as e:
                        logger.warning("Scene %s failed: %s", month.scenes[i].item_id, e)
                        month.failed.add(i)
                        month.errors[i] = str(e)
                        failure_seen = True
                        report.add(None, scenes_failed=1)
                        scene_done(month, i)
                        continue
                    if not valid.any():
                        report.add(None, scenes_no_valid=1)
                        scene_done(month, i)
                        continue
                    month.valid[i] = valid
                    month.bands_left[i] = n_bands
                    for b in range(n_bands):
                        tasks[fetch_pool.submit(read_band, month, i, b, valid)] = (
                            "band",
                            month,
                            i,
                        )
                elif kind == "band":
                    try:
                        future.result()
                    except Exception as e:
                        if i not in month.failed:
                            logger.warning("Scene %s failed: %s", month.scenes[i].item_id, e)
                            month.failed.add(i)
                            month.errors[i] = str(e)
                            failure_seen = True
                            report.add(None, scenes_failed=1)
                    month.bands_left[i] -= 1
                    if month.bands_left[i] > 0:
                        continue
                    if i in month.failed:
                        month.stack[i] = 0
                        month.valid.pop(i, None)
                        scene_done(month, i)
                    else:
                        tasks[cpu_pool.submit(assemble, month, i)] = ("mask", month, i)
                elif kind == "mask":
                    n_valid = future.result()
                    logger.info(
                        "Loaded %s: %.1f%% valid pixels",
                        month.scenes[i].item_id[:40],
                        100.0 * n_valid / (height * width),
                    )
                    scene_done(month, i)
                elif kind == "median":
                    month.result = future.result()
                    month.stack = np.empty(0, dtype=np.uint16)  # release the scene stack

            # Finish months in calendar order: carry-forward needs the month before.
            while next_finish < len(todo) and active.get(todo[next_finish]) is not None:
                month = active[todo[next_finish]]
                if month.result is None:
                    break
                failed_ids = sorted(month.scenes[i].item_id for i in month.failed)
                if failed_ids and not keep_going:
                    first = month.errors[min(month.failed)]
                    ids = ", ".join(failed_ids)
                    raise SceneReadError(
                        f"{len(failed_ids)} of {len(month.scenes)} scenes of {month.key} could "
                        f"not be read after {READ_ATTEMPTS} attempts each ({ids}; "
                        f"first error: {first}). A median without them would not be the month's "
                        "composite, so nothing was written for it; earlier months are saved. "
                        "Re-run to resume from this month. --keep-going writes such months "
                        "with the failed scenes listed in the file, and skips months where "
                        "every scene failed."
                    )
                if failed_ids and len(failed_ids) == len(month.scenes):
                    logger.warning(
                        "Every scene of %s failed; not writing it (the next run retries it)",
                        month.key,
                    )
                    report.months_not_written.append(month.key)
                    del active[month.key]
                    in_flight -= month.nbytes
                    next_finish += 1
                    continue
                if failed_ids:
                    logger.warning(
                        "Writing %s without %d of %d scenes (listed in the file): %s",
                        month.key,
                        len(failed_ids),
                        len(month.scenes),
                        ", ".join(failed_ids),
                    )
                    report.months_incomplete.append(month.key)
                bands, coverage = month.result
                logger.info(
                    "Monthly composite %s: %d scenes, mean coverage %.1f%%",
                    month.key,
                    len(month.scenes),
                    100.0 * coverage.mean(),
                )
                carried_from: list[str] = []
                if carry_forward:
                    prev = previous_month(month.key)
                    if prev is not None:
                        prev_key, prev_bands = prev
                        carried_from = [prev_key, bands_sha256(prev_bands)]
                        gap_mask = np.all(bands == 0, axis=0)
                        if gap_mask.any():
                            logger.info(
                                "Carry-forward: filling %d pixels from %s",
                                int(gap_mask.sum()),
                                prev_key,
                            )
                            bands[:, gap_mask] = prev_bands[:, gap_mask]
                last_written = (month.key, bands)
                if write_future is not None:
                    write_future.result()  # at most one month waiting to be written
                write_future = write_pool.submit(
                    save, month.key, bands, coverage, failed_ids, carried_from
                )
                outputs[month.key] = paths[month.key]
                if report.first_month_seconds is None:
                    write_future.result()
                    report.first_month_seconds = time.perf_counter() - t_start
                del active[month.key]
                in_flight -= month.nbytes
                next_finish += 1
            admit()
        if next_finish < len(todo):
            raise RuntimeError(
                f"internal error: {todo[next_finish]} was never finished; no tasks are left"
            )
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
        gdal_logger.removeFilter(throttle)
        report.http_throttled = throttle.events
        report.wall_seconds = time.perf_counter() - t_start
    return dict(sorted(outputs.items()))


def bands_sha256(bands: np.ndarray) -> str:
    """Identity of a month's composite, recorded by the month whose gaps it filled."""
    return hashlib.sha256(np.ascontiguousarray(bands).tobytes()).hexdigest()


def load_mosaic(path: Path) -> dict:
    """Load a saved monthly mosaic .npz file.

    Returns:
        Dict with keys: bands, coverage, transform, epsg, band_names, scenes_failed and
        carried_from. Files written before 2026-10-10 have no record of failed scenes or of the
        carry-forward source: their scenes_failed and carried_from are None.
    """
    with np.load(path, allow_pickle=False) as data:
        coeffs = data["transform"]
        return {
            "bands": data["bands"],
            "coverage": data["coverage"],
            "transform": Affine(*coeffs),
            "epsg": int(data["epsg"]),
            "band_names": list(data["band_names"]),
            "scenes_failed": [str(v) for v in data["scenes_failed"]]
            if "scenes_failed" in data
            else None,
            "carried_from": [str(v) for v in data["carried_from"]]
            if "carried_from" in data
            else None,
        }
