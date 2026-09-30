"""chronozarr writer: optional star-delta temporal encoding plus a block-mean pyramid, as Zarr v3.

Layout (see spec/CHRONOZARR.md and spec/CHANGES-0.2.md): a root group with `multiscales`,
`chronozarr` and `volatility`; one group per pyramid level holding `data` (time, band, y, x) in
one of four dtypes, optional `mask` and `coverage` planes (time, y, x), and coordinate arrays.

With star-delta encoding, anchor timesteps store true values and the others store
(value - nearest anchor) modulo 2^bits in the same array and dtype (uint8 or uint16 only). With
encoding "none" the array holds true values. `encoding="auto"` measures both on a sample of
level-0 cells and keeps star-delta only when it is clearly smaller.

Memory is bounded by the cell size, not the raster size. The pyramid is built depth first: each
cell of level k is assembled from the four cells of level k-1 beneath it, so a level-0 cell
(every timestep of one 512 x 512 window) is read once, written, downsampled into its parent
and dropped. Input is a DataArray (numpy or dask, read one cell at a time) or an iterable of
per-timestep arrays, which is spilled once to cell-major temp files and read back per cell.
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
import threading
import time
import warnings
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

import numcodecs
import numcodecs.abc
import numpy as np
import xarray as xr
import zarr
from zarr.codecs import BloscCodec, ZstdCodec
from zarr.errors import ZarrUserWarning

from chronozarr import schema
from chronozarr.schema import (
    Band,
    Chronozarr,
    LevelRef,
    LevelSummary,
    RootAttrs,
    Selection,
    Temporal,
    Transform,
)

VOLATILITY_SCALE = 10000  # fixed normalisation for every dtype (Sentinel-2 reflectance scale)
AUTO_RATIO_LIMIT = 0.85  # star-delta must be at most this fraction of the plain bytes
AUTO_MIN_CELLS = 3
AUTO_SAMPLE_FRACTION = 0.1
DEFAULT_LEVEL = {"zstd": 5, "blosc-zstd-shuffle": 1}
LEVEL_RANGE = {"zstd": (1, 22), "blosc-zstd-shuffle": (0, 9)}
DEFAULT_CELLS_IN_FLIGHT = 4

Encoding = Literal["auto", "none", "star-delta"]
Codec = Literal["zstd", "blosc-zstd-shuffle"]


@dataclass(frozen=True)
class LevelReport:
    level: int
    shape: tuple[int, int, int, int]
    n_cells: int
    downsample_s: float  # producing this level from the one below, summed over threads
    encode_s: float  # star-delta residuals + volatility, summed over threads
    write_s: float  # zarr write incl. compression, summed over threads
    bytes: int


@dataclass(frozen=True)
class EncodeReport:
    levels: tuple[LevelReport, ...]
    total_bytes: int
    n_files: int
    encoding: str  # the resolved temporal encoding: "none" or "star-delta"
    selection: Selection | None  # the auto measurement, if one was made
    codec: str
    level: int  # compression level


# --- Pixel blocks and the pyramid reduction ---------------------------------------------------


@dataclass
class Block:
    """One spatial window across every timestep: data (T, B, h, w), planes (T, h, w)."""

    data: np.ndarray
    mask: np.ndarray | None = None
    coverage: np.ndarray | None = None

    @property
    def height(self) -> int:
        return self.data.shape[2]

    @property
    def width(self) -> int:
        return self.data.shape[3]


def _accumulator(dtype: np.dtype) -> type[np.generic]:
    if dtype.kind == "u":
        return np.uint32
    if dtype.kind == "i":
        return np.int32
    return np.float64


def _pad_even(array: np.ndarray) -> np.ndarray:
    """Edge-replicate the last two axes up to even sizes."""
    height, width = array.shape[-2:]
    if height % 2 == 0 and width % 2 == 0:
        return array
    pad = [(0, 0)] * (array.ndim - 2) + [(0, height % 2), (0, width % 2)]
    return np.pad(array, pad, mode="edge")


def downsample_2x(
    values: np.ndarray,
    *,
    nodata: int | float | None = schema.NODATA,
    valid: np.ndarray | None = None,
) -> np.ndarray:
    """Block-mean the last two axes by 2 over valid pixels only.

    A pixel is valid where `valid` is true; without `valid`, where it differs from `nodata`
    (every pixel is valid when nodata is None). Odd sizes are padded by edge replication first,
    so the output is ceil(size / 2). Integer means floor-divide the exact sum (uint32 for
    uint8/uint16, int32 for int16); float32 accumulates in float64. A block with no valid pixel
    becomes `nodata` (0 when nodata is None). `valid` may broadcast against `values` (for
    example (1, y, x) against (band, y, x)).
    """
    values = _pad_even(values)
    if valid is not None:
        valid = _pad_even(valid)
    out_shape = (*values.shape[:-2], values.shape[-2] // 2, values.shape[-1] // 2)
    total = np.zeros(out_shape, dtype=_accumulator(values.dtype))
    count = np.zeros(out_shape, dtype=np.uint8)
    zero_is_nodata = valid is None and nodata == 0 and values.dtype.kind == "u"
    for i in (0, 1):
        for j in (0, 1):
            q = values[..., i::2, j::2]
            if valid is not None:
                ok = valid[..., i::2, j::2]
            elif nodata is not None:
                ok = q != nodata
            else:
                ok = None
            if ok is None:
                total += q
                count += 1
            elif zero_is_nodata:
                total += q  # nodata 0 adds nothing to the sum
                count += ok
            else:
                np.add(total, q, out=total, where=ok)
                count += ok
    has_valid = count > 0
    if values.dtype.kind == "f":
        mean = np.zeros_like(total)
        np.divide(total, count, out=mean, where=has_valid)
    else:
        mean = np.zeros_like(total)
        np.floor_divide(total, count, out=mean, where=has_valid)
    if nodata:
        mean[~has_valid] = nodata
    return mean.astype(values.dtype)


def _downsample_plane_pair(
    mask: np.ndarray | None, coverage: np.ndarray | None
) -> tuple[np.ndarray | None, np.ndarray | None]:
    """Reduce one timestep of mask (any valid) and coverage (rounded mean) by 2."""
    reduced_mask = reduced_coverage = None
    if mask is not None:
        padded = _pad_even(mask)
        reduced_mask = padded[0::2, 0::2]
        for i, j in ((0, 1), (1, 0), (1, 1)):
            reduced_mask = np.maximum(reduced_mask, padded[i::2, j::2])
    if coverage is not None:
        padded = _pad_even(coverage)
        total = np.zeros((padded.shape[0] // 2, padded.shape[1] // 2), dtype=np.uint16)
        for i in (0, 1):
            for j in (0, 1):
                total += padded[i::2, j::2]
        reduced_coverage = ((total + 2) // 4).astype(np.uint8)
    return reduced_mask, reduced_coverage


def downsample_block(
    block: Block, nodata: int | float | None, pool: ThreadPoolExecutor | None = None
) -> Block:
    """The next pyramid level of a block: data, mask and coverage reduced by 2 per timestep."""
    n_time, n_band, height, width = block.data.shape
    out_h, out_w = -(-height // 2), -(-width // 2)
    data = np.empty((n_time, n_band, out_h, out_w), dtype=block.data.dtype)
    mask = None if block.mask is None else np.empty((n_time, out_h, out_w), dtype=np.uint8)
    coverage = None if block.coverage is None else np.empty((n_time, out_h, out_w), dtype=np.uint8)

    def one(t: int) -> None:
        valid = None if block.mask is None else block.mask[t].astype(bool)[None]
        data[t] = downsample_2x(block.data[t], nodata=nodata, valid=valid)
        reduced_mask, reduced_coverage = _downsample_plane_pair(
            None if block.mask is None else block.mask[t],
            None if block.coverage is None else block.coverage[t],
        )
        if mask is not None:
            mask[t] = reduced_mask
        if coverage is not None:
            coverage[t] = reduced_coverage

    if pool is None:
        for t in range(n_time):
            one(t)
    else:
        list(pool.map(one, range(n_time)))
    return Block(data, mask, coverage)


# --- Input normalisation ----------------------------------------------------------------------


def _transform_from_coords(da: xr.DataArray) -> Transform:
    """Affine transform from regularly spaced, north-up pixel-centre x/y coordinates."""
    if "x" not in da.coords or "y" not in da.coords or da.sizes["x"] < 2 or da.sizes["y"] < 2:
        raise ValueError(
            "no transform: pass transform=(a, b, c, d, e, f), set da.attrs['transform'], or "
            "provide x/y coordinates with at least 2 pixels per axis"
        )
    x = da["x"].values.astype(np.float64)
    y = da["y"].values.astype(np.float64)
    dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
    if dx <= 0 or dy >= 0:
        raise ValueError(
            "x must increase and y must decrease (north-up); "
            "flip with da.isel(y=slice(None, None, -1))"
        )
    if not (np.allclose(np.diff(x), dx) and np.allclose(np.diff(y), dy)):
        raise ValueError("x/y coordinates are not regularly spaced; pass an explicit transform")
    return (dx, 0.0, float(x[0]) - dx / 2, 0.0, dy, float(y[0]) - dy / 2)


def _resolve_transform(raw: Sequence[float] | None, da: xr.DataArray | None) -> Transform:
    if raw is None and da is not None:
        raw = da.attrs.get("transform")
    if raw is None:
        if da is None:
            raise ValueError("no transform: pass transform=(a, b, c, d, e, f)")
        return _transform_from_coords(da)
    values = [float(v) for v in list(raw)[:6]]
    if len(values) != 6:
        raise ValueError(f"transform needs 6 coefficients (a, b, c, d, e, f), got {raw!r}")
    a, b, _, d, e, _ = values
    if b != 0 or d != 0:
        raise ValueError("rotated transforms are not supported; reproject to a north-up grid")
    if a <= 0 or e >= 0:
        raise ValueError(f"transform must be north-up (a > 0, e < 0), got a={a}, e={e}")
    return (values[0], values[1], values[2], values[3], values[4], values[5])


def _iso_times(times: np.ndarray) -> tuple[list[str], np.ndarray]:
    """ISO-8601 strings and int64 milliseconds since epoch for a datetime64 axis."""
    if not np.issubdtype(times.dtype, np.datetime64):
        raise ValueError(
            f"the time coordinate must be datetime64, got {times.dtype}; "
            "assign da['time'] = pd.to_datetime(...) or similar"
        )
    if np.isnat(times).any():
        raise ValueError("the time coordinate contains NaT")
    ms = times.astype("datetime64[ms]")
    if len(ms) > 1 and not (np.diff(ms.astype(np.int64)) > 0).all():
        raise ValueError("the time coordinate must be strictly increasing; use da.sortby('time')")
    unit = "s" if (ms.astype(np.int64) % 1000 == 0).all() else "ms"
    iso = [f"{s}Z" for s in np.datetime_as_string(ms, unit=unit)]
    return iso, ms.astype(np.int64)


def _resolve_bands(
    bands: Sequence[str | Band | Mapping] | None, coords: Sequence[str] | None, n_band: int
) -> tuple[Band, ...]:
    """Band objects from the `bands` argument and/or the DataArray band coordinate."""
    if bands is None:
        if coords is None:
            raise ValueError("no band names: pass bands=[...] (names or band objects)")
        parsed = tuple(Band(str(name)) for name in coords)
    else:
        parsed = tuple(
            b if isinstance(b, Band) else schema.parse_band(b, f"bands[{i}]")
            for i, b in enumerate(bands)
        )
    if len(parsed) != n_band:
        raise ValueError(f"{len(parsed)} band names for {n_band} bands")
    if bands is not None and coords is not None and [b.name for b in parsed] != list(coords):
        raise ValueError(
            f"bands names {[b.name for b in parsed]} differ from the band coordinate "
            f"{list(coords)}"
        )
    names = [b.name for b in parsed]
    if len(set(names)) != len(names):
        raise ValueError(f"band names must be unique, got {names}")
    return parsed


def _resolve_nodata(nodata: int | float | None | str, dtype: np.dtype) -> int | float | None:
    """The nodata value for `dtype`: 'default' is 0 for uint8/uint16 and None otherwise."""
    if nodata == "default":
        return schema.NODATA if dtype.name in schema.TEMPORAL_DTYPES else None
    if nodata is None:
        return None
    if isinstance(nodata, bool) or not isinstance(nodata, int | float | np.number):
        raise ValueError(f"nodata must be a number or None, got {nodata!r}")
    if not math.isfinite(float(nodata)):
        raise ValueError(
            f"nodata must be finite, got {nodata!r}; flag invalid pixels with a mask instead"
        )
    if dtype.kind == "f":
        return float(np.float32(nodata))
    info = np.iinfo(dtype)
    if float(nodata) != int(nodata) or not info.min <= int(nodata) <= info.max:
        raise ValueError(f"nodata {nodata!r} is not an integer within {dtype.name} range")
    return int(nodata)


# --- Cell sources -----------------------------------------------------------------------------


class _Source(Protocol):
    def read_cell(self, row: int, col: int) -> Block: ...


def _plane(array: np.ndarray, name: str, *, binary: bool) -> np.ndarray:
    if array.dtype == np.bool_:
        array = array.astype(np.uint8)
    if array.dtype != np.uint8:
        raise ValueError(f"{name} must be uint8 (or bool), got {array.dtype}")
    if binary and array.size and array.max() > 1:
        raise ValueError(f"{name} must contain only 0 (invalid) and 1 (valid)")
    return array


class _ArraySource:
    """Cells of a DataArray (numpy or dask) read one window at a time."""

    def __init__(
        self,
        data: xr.DataArray,
        mask: xr.DataArray | None,
        coverage: xr.DataArray | None,
        chunk_size: int,
    ) -> None:
        self.data, self.mask, self.coverage, self.cs = data, mask, coverage, chunk_size

    def read_cell(self, row: int, col: int) -> Block:
        window = {
            "y": slice(row * self.cs, (row + 1) * self.cs),
            "x": slice(col * self.cs, (col + 1) * self.cs),
        }
        data = np.asarray(self.data.isel(window).values)
        mask = coverage = None
        if self.mask is not None:
            mask = _plane(np.asarray(self.mask.isel(window).values), "mask", binary=True)
        if self.coverage is not None:
            coverage = _plane(
                np.asarray(self.coverage.isel(window).values), "coverage", binary=False
            )
        return Block(data, mask, coverage)


class _SpillSource:
    """Per-timestep arrays transposed to cell-major temp files, read back one cell at a time.

    Each cell has one file per variable holding its timesteps back to back, so a cell is one
    sequential read and a timestep write is one 2 MB write per cell. The page cache is not part
    of the process RSS (no mmap).
    """

    def __init__(
        self,
        directory: Path,
        *,
        n_time: int,
        n_band: int,
        height: int,
        width: int,
        dtype: np.dtype,
        chunk_size: int,
        has_mask: bool,
        has_coverage: bool,
    ) -> None:
        self.dir = directory
        self.n_time, self.n_band, self.height, self.width = n_time, n_band, height, width
        self.dtype, self.cs = dtype, chunk_size
        self.has_mask, self.has_coverage = has_mask, has_coverage
        self.rows, self.cols = schema.grid_shape(height, width, chunk_size)

    def _window(self, row: int, col: int) -> tuple[slice, slice]:
        cs = self.cs
        return (
            slice(row * cs, min((row + 1) * cs, self.height)),
            slice(col * cs, min((col + 1) * cs, self.width)),
        )

    def _path(self, kind: str, row: int, col: int) -> Path:
        return self.dir / f"{kind}_{row}_{col}.bin"

    def _put(self, kind: str, row: int, col: int, t: int, array: np.ndarray) -> None:
        contiguous = np.ascontiguousarray(array)
        path = self._path(kind, row, col)
        with open(path, "r+b" if path.exists() else "wb") as f:
            f.seek(t * contiguous.nbytes)
            contiguous.tofile(f)

    def write_timestep(
        self,
        t: int,
        data: np.ndarray,
        mask: np.ndarray | None,
        coverage: np.ndarray | None,
        pool: ThreadPoolExecutor,
    ) -> None:
        def one(cell: tuple[int, int]) -> None:
            row, col = cell
            ys, xs = self._window(row, col)
            self._put("d", row, col, t, data[:, ys, xs])
            if mask is not None:
                self._put("m", row, col, t, mask[ys, xs])
            if coverage is not None:
                self._put("v", row, col, t, coverage[ys, xs])

        cells = [(r, c) for r in range(self.rows) for c in range(self.cols)]
        list(pool.map(one, cells))

    def read_cell(self, row: int, col: int) -> Block:
        ys, xs = self._window(row, col)
        h, w = ys.stop - ys.start, xs.stop - xs.start
        data = np.fromfile(self._path("d", row, col), dtype=self.dtype)
        block = Block(data.reshape(self.n_time, self.n_band, h, w))
        if self.has_mask:
            mask = np.fromfile(self._path("m", row, col), dtype=np.uint8)
            block.mask = mask.reshape(self.n_time, h, w)
        if self.has_coverage:
            coverage = np.fromfile(self._path("v", row, col), dtype=np.uint8)
            block.coverage = coverage.reshape(self.n_time, h, w)
        return block


# --- Encoding choice --------------------------------------------------------------------------


def _compressor_for(codec: str, level: int) -> numcodecs.abc.Codec:
    if codec == "zstd":
        return numcodecs.Zstd(level=level)
    return numcodecs.Blosc(cname="zstd", clevel=level, shuffle=numcodecs.Blosc.SHUFFLE)


def _compressed_size(compressor: numcodecs.abc.Codec, array: np.ndarray) -> int:
    return len(compressor.encode(np.ascontiguousarray(array)))


def _sample_cells(grid: tuple[int, int]) -> list[tuple[int, int]]:
    """Evenly spread level-0 cells: max(3, 10 %) of them, or all if there are fewer than 3."""
    rows, cols = grid
    n_cells = rows * cols
    wanted = min(n_cells, max(AUTO_MIN_CELLS, math.ceil(AUTO_SAMPLE_FRACTION * n_cells)))
    picks = sorted({round(i) for i in np.linspace(0, n_cells - 1, wanted)})
    return [(p // cols, p % cols) for p in picks]


def _measure_star_delta(
    source: _Source,
    grid: tuple[int, int],
    anchors: Sequence[int],
    reference: Mapping[int, int],
    codec: str,
    level: int,
    pool: ThreadPoolExecutor,
) -> Selection:
    """Compressed bytes of star-delta over plain on a sample of level-0 cells.

    Both layouts compress the same inner chunks the store would hold: one (band, y, x) chunk
    per timestep. Anchors are true values in both, so they count equally.
    """
    compressor = _compressor_for(codec, level)
    plain_bytes = delta_bytes = 0
    cells = _sample_cells(grid)
    for row, col in cells:
        block = source.read_cell(row, col)

        def sizes(t: int, block: Block = block) -> tuple[int, int]:
            plain = _compressed_size(compressor, block.data[t])
            if t not in reference:
                return plain, plain
            residual = np.subtract(block.data[t], block.data[reference[t]])
            return plain, _compressed_size(compressor, residual)

        for plain, delta in pool.map(sizes, range(block.data.shape[0])):
            plain_bytes += plain
            delta_bytes += delta
    ratio = delta_bytes / plain_bytes if plain_bytes else 1.0
    return Selection(sampled_cells=len(cells), ratio=round(ratio, 4))


# --- Cell encode and write --------------------------------------------------------------------


@dataclass(frozen=True)
class _CellResult:
    encode_s: float
    write_s: float
    abs_delta_sum: float
    n_delta_values: int


@dataclass
class _LevelArrays:
    data: zarr.Array
    mask: zarr.Array | None
    coverage: zarr.Array | None


def _residuals(
    block: np.ndarray, anchors: Sequence[int], reference: Mapping[int, int]
) -> np.ndarray:
    """Star-delta layout: anchors as-is, others (value - anchor) modulo 2^bits in the dtype."""
    out = np.empty(block.shape, dtype=block.dtype)
    for anchor in anchors:
        out[anchor] = block[anchor]
    for t, anchor in reference.items():
        np.subtract(block[t], block[anchor], out=out[t])  # unsigned arrays wrap silently
    return out


def _mean_abs_delta(block: np.ndarray, reference: Mapping[int, int]) -> tuple[float, int]:
    """Sum of |value - anchor| over every delta timestep (true difference, not modular)."""
    wide = np.float64 if block.dtype.kind == "f" else np.int32
    by_anchor: dict[int, list[int]] = {}
    for t, anchor in reference.items():
        by_anchor.setdefault(anchor, []).append(t)
    total = 0.0
    count = 0
    for anchor, steps in by_anchor.items():
        base = block[anchor].astype(wide)
        for t in steps:
            diff = block[t].astype(wide)
            diff -= base
            total += float(np.abs(diff).sum(dtype=np.float64))
            count += diff.size
    return total, count


def _encode_cell(
    block: Block,
    arrays: _LevelArrays,
    ys: slice,
    xs: slice,
    *,
    star_delta: bool,
    anchors: Sequence[int],
    reference: Mapping[int, int],
    shard_time: int,
    want_volatility: bool,
) -> _CellResult:
    """Encode one cell (every timestep) and write it, one time shard at a time."""
    started = time.perf_counter()
    out = _residuals(block.data, anchors, reference) if star_delta else block.data
    total, count = _mean_abs_delta(block.data, reference) if want_volatility else (0.0, 0)
    encoded = time.perf_counter()

    n_time = block.data.shape[0]
    for t0 in range(0, n_time, shard_time):
        window = slice(t0, min(t0 + shard_time, n_time))
        arrays.data[window, :, ys, xs] = out[window]
        if arrays.mask is not None and block.mask is not None:
            arrays.mask[window, ys, xs] = block.mask[window]
        if arrays.coverage is not None and block.coverage is not None:
            arrays.coverage[window, ys, xs] = block.coverage[window]
    written = time.perf_counter()
    return _CellResult(encoded - started, written - encoded, total, count)


class _CellWriter:
    """Runs cell encodes on a pool with at most `limit` cells (and their buffers) in flight."""

    def __init__(self, limit: int) -> None:
        self.pool = ThreadPoolExecutor(max_workers=limit)
        self.slots = threading.BoundedSemaphore(limit)
        self.futures: list[tuple[int, int, int, Future[_CellResult]]] = []

    def submit(self, level: int, row: int, col: int, run: Callable[[], _CellResult]) -> None:
        self._raise_finished()
        self.slots.acquire()

        def job() -> _CellResult:
            try:
                return run()
            finally:
                self.slots.release()

        self.futures.append((level, row, col, self.pool.submit(job)))

    def _raise_finished(self) -> None:
        for _, _, _, future in self.futures:
            if future.done() and (error := future.exception()) is not None:
                raise error

    def results(self) -> list[tuple[int, int, int, _CellResult]]:
        return [(lvl, r, c, f.result()) for lvl, r, c, f in self.futures]

    def shutdown(self) -> None:
        for _, _, _, future in self.futures:
            future.cancel()
        self.pool.shutdown(wait=True)


# --- Store writing ----------------------------------------------------------------------------


def _tree_stats(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()]
    return sum(p.stat().st_size for p in files), len(files)


def _write_coords(
    group: zarr.Group,
    transform: Transform,
    height: int,
    width: int,
    times_ms: np.ndarray,
    bands: Sequence[str],
) -> None:
    def put(name: str, values: np.ndarray, dtype: type | str, attrs: dict | None = None) -> None:
        array = group.create_array(
            name=name,
            shape=values.shape,
            chunks=values.shape,
            dtype=dtype,
            dimension_names=(name,),
        )
        array[:] = values
        array.attrs.update({"_ARRAY_DIMENSIONS": [name], **(attrs or {})})

    put(
        "time",
        times_ms,
        "int64",
        {"units": schema.TIME_UNITS, "calendar": schema.TIME_CALENDAR},
    )
    put("band", np.array(bands, dtype=object), str)
    y, x = schema.pixel_centers(transform, height, width)
    put("x", x, "float64")
    put("y", y, "float64")


def _compressors(codec: str, level: int) -> ZstdCodec | BloscCodec:
    if codec == "zstd":
        return ZstdCodec(level=level)
    return BloscCodec(cname="zstd", clevel=level, shuffle="shuffle")


@dataclass(frozen=True)
class _Layout:
    """Everything fixed before the first cell is written."""

    n_time: int
    n_band: int
    dtype: np.dtype
    nodata: int | float | None
    shapes: list[tuple[int, int]]
    chunk_size: int
    shard: bool
    shard_time: int
    crs: str
    transform: Transform
    times_iso: list[str]
    times_ms: np.ndarray
    bands: tuple[Band, ...]
    has_mask: bool
    has_coverage: bool
    codec: str
    level: int
    anchor_interval: int

    @property
    def fill_value(self) -> int | float:
        return self.nodata if self.nodata is not None else 0


def _create_level(root: zarr.Group, k: int, layout: _Layout) -> _LevelArrays:
    """Create level group `k` with its data, optional mask/coverage, and coordinate arrays."""
    height, width = layout.shapes[k]
    cs = layout.chunk_size
    transform = schema.scale_transform(layout.transform, k)
    group = root.create_group(str(k))
    group.attrs.update(schema.LevelAttrs(layout.crs, transform, transform[0]).to_attrs())
    compressor = _compressors(layout.codec, layout.level)

    def create(name: str, lead: tuple[int, ...], dtype: np.dtype, fill: int | float, dims: tuple):
        shape = (layout.n_time, *lead, height, width)
        array = group.create_array(
            name=name,
            shape=shape,
            dtype=dtype,
            fill_value=fill,
            dimension_names=dims,
            chunks=(1, *lead, cs, cs),
            shards=(layout.shard_time, *lead, cs, cs) if layout.shard else None,
            compressors=compressor,
            filters=None,
        )
        attrs = schema.data_array_attrs(layout.crs, transform, height, width, dimensions=dims)
        return array, attrs

    data, attrs = create(
        schema.VARIABLE, (layout.n_band,), layout.dtype, layout.fill_value, schema.DIMENSIONS
    )
    attrs["nodata"] = layout.nodata
    data.attrs.update(attrs)
    planes: list[zarr.Array | None] = []
    for name, wanted in (
        (schema.MASK_VARIABLE, layout.has_mask),
        (schema.COVERAGE_VARIABLE, layout.has_coverage),
    ):
        if not wanted:
            planes.append(None)
            continue
        array, attrs = create(name, (), np.dtype("uint8"), 0, schema.PLANE_DIMENSIONS)
        del attrs["nodata"]
        array.attrs.update(attrs)
        planes.append(array)
    _write_coords(group, transform, height, width, layout.times_ms, [b.name for b in layout.bands])
    return _LevelArrays(data, planes[0], planes[1])


def _shard_bytes(out: Path, layout: _Layout, n_levels: int) -> dict[str, dict[str, int]]:
    """Byte length of every shard object of the data array, keyed by level then t/row/col."""
    sizes: dict[str, dict[str, int]] = {}
    for k in range(n_levels):
        base = out / str(k) / schema.VARIABLE / "c"
        entries = {}
        for path in sorted(base.glob("*/0/*/*")):
            t_shard, _, row, col = path.relative_to(base).parts
            entries[f"{t_shard}/{row}/{col}"] = path.stat().st_size
        sizes[str(k)] = entries
    return sizes


def _write_store(
    out: Path,
    layout: _Layout,
    source: _Source,
    *,
    star_delta: bool,
    selection: Selection | None,
    provenance: dict | None,
    cells_in_flight: int,
) -> EncodeReport:
    cs = layout.chunk_size
    schedule = schema.compute_anchor_schedule(layout.n_time, layout.anchor_interval)
    anchors, reference = schedule
    root = zarr.open_group(str(out), mode="w", zarr_format=3)
    arrays = [_create_level(root, k, layout) for k in range(len(layout.shapes))]
    grids = [schema.grid_shape(h, w, cs) for h, w in layout.shapes]
    volatility = np.zeros(grids[0], dtype=np.float32)
    downsample_s = [0.0] * len(layout.shapes)
    writer = _CellWriter(cells_in_flight)
    compute = ThreadPoolExecutor(max_workers=os.cpu_count() or 1)

    def submit(k: int, row: int, col: int, block: Block) -> None:
        ys = slice(row * cs, row * cs + block.height)
        xs = slice(col * cs, col * cs + block.width)
        writer.submit(
            k,
            row,
            col,
            lambda: _encode_cell(
                block,
                arrays[k],
                ys,
                xs,
                star_delta=star_delta,
                anchors=anchors,
                reference=reference,
                shard_time=layout.shard_time,
                want_volatility=k == 0,
            ),
        )

    def produce(k: int, row: int, col: int) -> Block:
        """Level-k cell (row, col): read at level 0, else assembled from its four children."""
        block = source.read_cell(row, col) if k == 0 else assemble(k, row, col)
        submit(k, row, col, block)
        return block

    def assemble(k: int, row: int, col: int) -> Block:
        height, width = layout.shapes[k]
        h, w = min(cs, height - row * cs), min(cs, width - col * cs)
        n_time, n_band = layout.n_time, layout.n_band
        parent = Block(np.empty((n_time, n_band, h, w), dtype=layout.dtype))
        half = cs // 2
        filled = 0
        for i in (0, 1):
            for j in (0, 1):
                child_row, child_col = 2 * row + i, 2 * col + j
                if child_row >= grids[k - 1][0] or child_col >= grids[k - 1][1]:
                    continue
                child = produce(k - 1, child_row, child_col)
                started = time.perf_counter()
                small = downsample_block(child, layout.nodata, compute)
                ys = slice(i * half, i * half + small.height)
                xs = slice(j * half, j * half + small.width)
                if parent.mask is None and small.mask is not None:
                    parent.mask = np.empty((n_time, h, w), dtype=np.uint8)
                if parent.coverage is None and small.coverage is not None:
                    parent.coverage = np.empty((n_time, h, w), dtype=np.uint8)
                parent.data[:, :, ys, xs] = small.data
                if small.mask is not None and parent.mask is not None:
                    parent.mask[:, ys, xs] = small.mask
                if small.coverage is not None and parent.coverage is not None:
                    parent.coverage[:, ys, xs] = small.coverage
                filled += small.height * small.width
                downsample_s[k] += time.perf_counter() - started
        if filled != h * w:
            raise AssertionError(
                f"level {k} cell ({row}, {col}): children cover {filled} of {h * w} pixels"
            )
        return parent

    top = len(layout.shapes) - 1
    try:
        for row in range(grids[top][0]):
            for col in range(grids[top][1]):
                produce(top, row, col)
        results = writer.results()
    except BaseException:
        writer.shutdown()
        raise
    finally:
        compute.shutdown(wait=True)
    writer.shutdown()

    per_level: dict[int, list[_CellResult]] = {k: [] for k in range(len(layout.shapes))}
    for k, row, col, result in results:
        per_level[k].append(result)
        if k == 0 and result.n_delta_values:
            mean_abs = result.abs_delta_sum / result.n_delta_values
            volatility[row, col] = min(max(mean_abs / VOLATILITY_SCALE, 0.0), 1.0)

    vol = root.create_array(
        name=schema.VOLATILITY_PATH,
        shape=volatility.shape,
        chunks=volatility.shape,
        dtype="float32",
        fill_value=0.0,
        dimension_names=("row", "col"),
    )
    vol[:] = volatility
    vol.attrs["_ARRAY_DIMENSIONS"] = ["row", "col"]

    summaries = tuple(
        LevelSummary(
            path=str(k),
            resolution=schema.scale_transform(layout.transform, k)[0],
            transform=schema.scale_transform(layout.transform, k),
            shape=(layout.n_time, layout.n_band, layout.shapes[k][0], layout.shapes[k][1]),
            grid=grids[k],
        )
        for k in range(len(layout.shapes))
    )
    temporal = (
        Temporal.build(layout.n_time, layout.anchor_interval, selection)
        if star_delta
        else Temporal.plain(layout.n_time, selection)
    )
    meta = Chronozarr(
        times=tuple(layout.times_iso),
        bands=layout.bands,
        crs=layout.crs,
        temporal=temporal,
        nodata=layout.nodata,
        mask_variable=schema.MASK_VARIABLE if layout.has_mask else None,
        coverage_variable=schema.COVERAGE_VARIABLE if layout.has_coverage else None,
        provenance=provenance,
        levels=summaries,
        shard_bytes=_shard_bytes(out, layout, len(layout.shapes)) if layout.shard else None,
    )
    datasets = tuple(LevelRef(str(k), cs, layout.crs) for k in range(len(layout.shapes)))
    root.attrs.update(RootAttrs(meta, datasets).to_attrs())
    with warnings.catch_warnings():
        # zarr-python warns that consolidated metadata is outside the Zarr v3 spec. chronozarr
        # writes it deliberately (spec 3.1) so a reader learns every array in one GET.
        warnings.simplefilter("ignore", ZarrUserWarning)
        zarr.consolidate_metadata(str(out))

    total_bytes, n_files = _tree_stats(out)
    return EncodeReport(
        levels=tuple(
            LevelReport(
                level=k,
                shape=summaries[k].shape,
                n_cells=len(per_level[k]),
                downsample_s=downsample_s[k],
                encode_s=sum(r.encode_s for r in per_level[k]),
                write_s=sum(r.write_s for r in per_level[k]),
                bytes=_tree_stats(out / str(k))[0],
            )
            for k in range(len(layout.shapes))
        ),
        total_bytes=total_bytes,
        n_files=n_files,
        encoding=schema.STAR_DELTA if star_delta else schema.NONE,
        selection=selection,
        codec=layout.codec,
        level=layout.level,
    )


# --- Public entry point -----------------------------------------------------------------------


@dataclass
class _Input:
    """Validated description of the input plus a way to get its cells.

    A DataArray input sets `da` and DataArray planes; an iterable input sets `timesteps` and
    iterable planes.
    """

    n_time: int
    n_band: int
    height: int
    width: int
    dtype: np.dtype
    times: np.ndarray
    band_coords: list[str] | None
    attrs: Mapping
    da: xr.DataArray | None = None
    timesteps: Iterable[np.ndarray] | None = None
    mask: xr.DataArray | Iterable[np.ndarray] | None = None
    coverage: xr.DataArray | Iterable[np.ndarray] | None = None


def _as_plane_da(plane: object, name: str, da: xr.DataArray) -> xr.DataArray:
    if not isinstance(plane, xr.DataArray | np.ndarray):
        raise ValueError(
            f"{name} must be a (time, y, x) DataArray or ndarray when data is a DataArray, "
            f"got {type(plane).__name__}"
        )
    if isinstance(plane, np.ndarray):
        plane = xr.DataArray(plane, dims=schema.PLANE_DIMENSIONS)
    if plane.dims != schema.PLANE_DIMENSIONS:
        raise ValueError(f"{name} must have dims {schema.PLANE_DIMENSIONS}, got {plane.dims}")
    if plane.shape != (da.sizes["time"], da.sizes["y"], da.sizes["x"]):
        raise ValueError(f"{name} shape {plane.shape} differs from the data (time, y, x)")
    return plane


def _prepare_input(
    data: xr.DataArray | Iterable[np.ndarray],
    times: Sequence | np.ndarray | None,
    mask: object,
    coverage: object,
) -> _Input:
    if isinstance(data, xr.DataArray):
        if times is not None:
            raise ValueError("times= is only for iterable input; a DataArray carries its own")
        if data.dims != schema.DIMENSIONS:
            raise ValueError(
                f"expected dims {schema.DIMENSIONS}, got {data.dims}; use da.transpose()"
            )
        if data.dtype.name not in schema.DTYPES:
            raise ValueError(
                f"unsupported dtype {data.dtype}: expected one of {list(schema.DTYPES)}; "
                "chronozarr never converts dtype"
            )
        if 0 in data.shape:
            raise ValueError(f"empty input: shape {data.shape}")
        n_time, n_band, height, width = data.shape
        coords = [str(b) for b in data["band"].values] if "band" in data.coords else None
        return _Input(
            n_time=n_time,
            n_band=n_band,
            height=height,
            width=width,
            dtype=data.dtype,
            times=data["time"].values,
            band_coords=coords,
            attrs=data.attrs,
            da=data,
            mask=None if mask is None else _as_plane_da(mask, "mask", data),
            coverage=None if coverage is None else _as_plane_da(coverage, "coverage", data),
        )
    if times is None:
        raise ValueError("iterable input needs times= (datetime64, one per timestep)")
    iterator = iter(data)
    first = next(iterator, None)
    if first is None:
        raise ValueError("empty input: no timesteps")
    first = np.asarray(first)
    if first.ndim != 3 or 0 in first.shape:
        raise ValueError(
            f"each timestep must be a non-empty (band, y, x) array, got {first.shape}"
        )
    if first.dtype.name not in schema.DTYPES:
        raise ValueError(
            f"unsupported dtype {first.dtype}: expected one of {list(schema.DTYPES)}; "
            "chronozarr never converts dtype"
        )
    times_array = np.asarray(times)
    n_band, height, width = first.shape

    def chained() -> Iterable[np.ndarray]:
        yield first
        yield from iterator

    return _Input(
        n_time=len(times_array),
        n_band=n_band,
        height=height,
        width=width,
        dtype=first.dtype,
        times=times_array,
        band_coords=None,
        attrs={},
        timesteps=chained(),
        mask=cast("Iterable[np.ndarray] | None", mask),
        coverage=cast("Iterable[np.ndarray] | None", coverage),
    )


def _check_codec(codec: str, level: int | None) -> int:
    if codec not in DEFAULT_LEVEL:
        raise ValueError(f"codec must be one of {list(DEFAULT_LEVEL)}, got {codec!r}")
    resolved = DEFAULT_LEVEL[codec] if level is None else level
    low, high = LEVEL_RANGE[codec]
    if not isinstance(resolved, int) or not low <= resolved <= high:
        raise ValueError(f"level for {codec} must be an int in {low}..{high}, got {level!r}")
    return resolved


def encode(
    data: xr.DataArray | Iterable[np.ndarray],
    out: str | Path,
    *,
    crs: str | None = None,
    transform: Sequence[float] | None = None,
    times: Sequence | np.ndarray | None = None,
    bands: Sequence[str | Band | Mapping] | None = None,
    encoding: Encoding = "auto",
    anchor_interval: int = 6,
    codec: Codec = "zstd",
    level: int | None = None,
    nodata: int | float | str | None = "default",
    mask: object = None,
    coverage: object = None,
    provenance: Mapping | None = None,
    chunk_size: int = 512,
    shard: bool = True,
    shard_time: int | None = None,
    n_lods: int | None = None,
    workers: int | None = None,
    spill_dir: str | Path | None = None,
) -> EncodeReport:
    """Write `data` as a chronozarr v0.2 store at `out`.

    Args:
        data: Either a DataArray with dims (time, band, y, x) whose dtype is uint8, uint16,
            int16 or float32 (numpy, or dask/lazy: one spatial cell is read at a time), or an
            iterable of per-timestep (band, y, x) arrays in time order, which needs `times`,
            `crs`, `transform` and `bands`. An iterable is written once to cell-major temp
            files, then encoded cell by cell, so memory does not grow with the raster.
        out: Directory to create. Must not exist or must be empty (stores are immutable).
        crs: CRS string such as "EPSG:32631". Falls back to `data.attrs["crs"]`.
        transform: Affine coefficients (a, b, c, d, e, f) of the north-up level-0 grid. Falls
            back to `data.attrs["transform"]`, then to the x/y pixel-centre coordinates.
        times: datetime64 timestamps, strictly increasing (iterable input only).
        bands: Band names, or Band objects / dicts {name, common_name, scale, offset, units}.
            Default: the DataArray band coordinate.
        encoding: "auto" measures star-delta against plain storage on a sample of level-0 cells
            and keeps star-delta only when its compressed bytes are at most 0.85 x the plain
            bytes; "none" and "star-delta" force one. star-delta needs uint8 or uint16.
        anchor_interval: Timesteps between anchors (1 = every timestep is an anchor).
        codec: "zstd" (default) or "blosc-zstd-shuffle" (blosc, zstd inside, byte shuffle).
        level: Compression level. Default 5 for zstd (1..22), 1 for blosc (0..9).
        nodata: Value marking invalid pixels, or None for none. "default" is 0 for uint8 and
            uint16 and None for int16 and float32.
        mask: Optional uint8 validity plane (1 = valid), (time, y, x) DataArray, or an iterable
            of per-timestep (y, x) arrays when `data` is an iterable. Overrides nodata for
            pyramid means and readers.
        coverage: Optional uint8 count of valid observations per pixel, same shape rules.
        provenance: Optional {"sources": [...], "composite": str, "gap_fill": "carry-forward" |
            "none", "notes": str}.
        chunk_size: Spatial chunk (and cell) edge in pixels; even. 512 (default) and 256 are
            the spec values; smaller even sizes exist for tests.
        shard: One shard object per (time shard, cell) (default), or one object per chunk.
        shard_time: Timesteps per shard along time (default and maximum: all of them).
        n_lods: Number of pyramid levels including level 0. Default: stop at the first level
            whose cell grid is 1 x 1.
        workers: Cells encoded concurrently (each holds about two copies of a cell in memory;
            zarr compresses a cell's chunks in parallel). Default 4.
        spill_dir: Directory for the temp files of iterable input. Default: next to `out`.

    Removes the partially written store if encoding fails.
    """
    if chunk_size < 2 or chunk_size % 2:
        raise ValueError(f"chunk_size must be an even number, got {chunk_size}")
    if anchor_interval < 1:
        raise ValueError(f"anchor_interval must be >= 1, got {anchor_interval}")
    if encoding not in ("auto", schema.NONE, schema.STAR_DELTA):
        raise ValueError(f"encoding must be 'auto', 'none' or 'star-delta', got {encoding!r}")
    resolved_level = _check_codec(codec, level)
    cells_in_flight = workers if workers is not None else DEFAULT_CELLS_IN_FLIGHT
    if cells_in_flight < 1:
        raise ValueError(f"workers must be >= 1, got {cells_in_flight}")

    prepared = _prepare_input(data, times, mask, coverage)
    resolved_crs = crs if crs is not None else prepared.attrs.get("crs")
    if not resolved_crs:
        raise ValueError("no crs: pass crs='EPSG:xxxxx' or set da.attrs['crs']")
    base_transform = _resolve_transform(transform, prepared.da)
    times_iso, times_ms = _iso_times(prepared.times)
    if len(times_iso) != prepared.n_time:
        raise ValueError(f"{len(times_iso)} times for {prepared.n_time} timesteps")
    resolved_bands = _resolve_bands(bands, prepared.band_coords, prepared.n_band)
    resolved_nodata = _resolve_nodata(nodata, prepared.dtype)
    if encoding == schema.STAR_DELTA and prepared.dtype.name not in schema.TEMPORAL_DTYPES:
        raise ValueError(
            f"star-delta needs uint8 or uint16 data, got {prepared.dtype}; use encoding='none'"
        )
    resolved_provenance = None if provenance is None else schema.parse_provenance(provenance)
    if shard_time is not None and not 1 <= shard_time <= prepared.n_time:
        raise ValueError(f"shard_time must be in 1..{prepared.n_time}, got {shard_time}")

    layout = _Layout(
        n_time=prepared.n_time,
        n_band=prepared.n_band,
        dtype=prepared.dtype,
        nodata=resolved_nodata,
        shapes=schema.level_shapes(prepared.height, prepared.width, chunk_size, n_lods),
        chunk_size=chunk_size,
        shard=shard,
        shard_time=shard_time if shard_time is not None else prepared.n_time,
        crs=str(resolved_crs),
        transform=base_transform,
        times_iso=times_iso,
        times_ms=times_ms,
        bands=resolved_bands,
        has_mask=prepared.mask is not None,
        has_coverage=prepared.coverage is not None,
        codec=codec,
        level=resolved_level,
        anchor_interval=anchor_interval,
    )

    out = Path(out)
    existed = out.exists()
    if existed and any(out.iterdir()):
        raise FileExistsError(
            f"{out} already exists and is not empty; stores are immutable, write to a new path"
        )

    spill: Path | None = None
    try:
        if prepared.da is not None:
            source: _Source = _ArraySource(
                prepared.da,
                prepared.mask if isinstance(prepared.mask, xr.DataArray) else None,
                prepared.coverage if isinstance(prepared.coverage, xr.DataArray) else None,
                chunk_size,
            )
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            spill = Path(
                tempfile.mkdtemp(
                    prefix=f".{out.name}-spill-", dir=str(spill_dir) if spill_dir else out.parent
                )
            )
            source = _spill_timesteps(prepared, layout, spill)
        return _encode(out, layout, source, encoding, cells_in_flight, resolved_provenance)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        if existed:
            out.mkdir()
        raise
    finally:
        if spill is not None:
            shutil.rmtree(spill, ignore_errors=True)


def _spill_timesteps(prepared: _Input, layout: _Layout, directory: Path) -> _SpillSource:
    """Consume the per-timestep iterables into cell-major files."""
    assert prepared.timesteps is not None
    spill = _SpillSource(
        directory,
        n_time=layout.n_time,
        n_band=layout.n_band,
        height=prepared.height,
        width=prepared.width,
        dtype=layout.dtype,
        chunk_size=layout.chunk_size,
        has_mask=layout.has_mask,
        has_coverage=layout.has_coverage,
    )
    masks = iter(prepared.mask) if prepared.mask is not None else None
    coverages = iter(prepared.coverage) if prepared.coverage is not None else None
    expected = (layout.n_band, prepared.height, prepared.width)
    plane_shape = (prepared.height, prepared.width)
    seen = 0
    with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
        for t, step in enumerate(prepared.timesteps):
            if t >= layout.n_time:
                raise ValueError(f"the input has more than the {layout.n_time} timesteps in times")
            step = np.asarray(step)
            if step.shape != expected or step.dtype != layout.dtype:
                raise ValueError(
                    f"timestep {t} is {step.dtype}{step.shape}; expected "
                    f"{layout.dtype}{expected} like timestep 0"
                )
            planes: list[np.ndarray | None] = []
            for name, iterator, binary in (("mask", masks, True), ("coverage", coverages, False)):
                if iterator is None:
                    planes.append(None)
                    continue
                plane = next(iterator, None)
                if plane is None:
                    raise ValueError(f"{name} ended at timestep {t}; it needs one array per step")
                plane = _plane(np.asarray(plane), name, binary=binary)
                if plane.shape != plane_shape:
                    raise ValueError(
                        f"{name} at timestep {t} is {plane.shape}; expected {plane_shape}"
                    )
                planes.append(plane)
            spill.write_timestep(t, step, planes[0], planes[1], pool)
            seen += 1
    if seen != layout.n_time:
        raise ValueError(f"the input has {seen} timesteps but times has {layout.n_time}")
    return spill


def _encode(
    out: Path,
    layout: _Layout,
    source: _Source,
    encoding: str,
    cells_in_flight: int,
    provenance: dict | None,
) -> EncodeReport:
    anchors, reference = schema.compute_anchor_schedule(layout.n_time, layout.anchor_interval)
    selection: Selection | None = None
    if encoding == schema.STAR_DELTA:
        star_delta = True
    elif (
        encoding == schema.NONE or layout.dtype.name not in schema.TEMPORAL_DTYPES or not reference
    ):
        star_delta = False  # auto with nothing to measure: no deltas, or dtype not eligible
    else:
        grid = schema.grid_shape(*layout.shapes[0], layout.chunk_size)
        with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
            selection = _measure_star_delta(
                source, grid, anchors, reference, layout.codec, layout.level, pool
            )
        star_delta = selection.ratio <= AUTO_RATIO_LIMIT
    return _write_store(
        out,
        layout,
        source,
        star_delta=star_delta,
        selection=selection,
        provenance=provenance,
        cells_in_flight=cells_in_flight,
    )
