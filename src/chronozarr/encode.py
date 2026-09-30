"""chronozarr writer: star-delta temporal encoding plus a block-average pyramid, as Zarr v3.

Layout (see spec/CHRONOZARR.md): root group with `multiscales`, `chronozarr` and `volatility`;
one group per pyramid level holding `data` (time, band, y, x) uint16 and coordinate arrays.
Anchor timesteps store true uint16 values; other timesteps store (value - nearest anchor) as
int16 viewed as uint16, in the same array. A residual outside the int16 range cannot be stored
losslessly, so the encoder fails instead of clipping.
"""

from __future__ import annotations

import os
import shutil
import time
import warnings
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path

import numpy as np
import xarray as xr
import zarr
from zarr.codecs import ZstdCodec
from zarr.errors import ZarrUserWarning

from chronozarr import schema
from chronozarr.schema import Chronozarr, LevelRef, RootAttrs, Temporal, Transform

ZSTD_LEVEL = 5
VOLATILITY_SCALE = 10000  # Sentinel-2 reflectance scale; fixed normalisation in v0.1
INT16_MIN, INT16_MAX = -32768, 32767


@dataclass(frozen=True)
class LevelReport:
    level: int
    shape: tuple[int, int, int, int]
    downsample_s: float  # wall time producing this level from the previous one
    cells_s: float  # wall time of the per-cell star-delta encode + write phase
    encode_thread_s: float  # star-delta compute, summed over worker threads
    write_thread_s: float  # zarr write incl. zstd, summed over worker threads
    bytes: int


@dataclass(frozen=True)
class EncodeReport:
    levels: tuple[LevelReport, ...]
    total_bytes: int
    n_files: int


def downsample_2x(block: np.ndarray) -> np.ndarray:
    """Block-average the last two axes by 2, excluding nodata (0) pixels.

    Odd sizes are padded by edge replication first, so the output is ceil(size / 2). The mean is
    integer arithmetic (uint32 sum, floor division by valid count); a block with no valid pixels
    is nodata.
    """
    height, width = block.shape[-2:]
    if height % 2 or width % 2:
        pad = [(0, 0)] * (block.ndim - 2) + [(0, height % 2), (0, width % 2)]
        block = np.pad(block, pad, mode="edge")
    quadrants = [block[..., i::2, j::2] for i in (0, 1) for j in (0, 1)]
    total = quadrants[0].astype(np.uint32)
    count = (quadrants[0] != schema.NODATA).astype(np.uint8)
    for q in quadrants[1:]:
        total += q
        count += q != schema.NODATA
    mean = np.zeros_like(total)
    np.floor_divide(total, count, out=mean, where=count > 0)
    return mean.astype(np.uint16)


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


def _resolve_transform(da: xr.DataArray, transform: Sequence[float] | None) -> Transform:
    raw = transform if transform is not None else da.attrs.get("transform")
    if raw is None:
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


def _create_data_array(
    group: zarr.Group, shape: tuple[int, int, int, int], chunk_size: int, shard: bool
) -> zarr.Array:
    """Create `{level}/data`; sharded arrays use zarr's default sharding codec (index at end)."""
    n_time, n_band = shape[:2]
    return group.create_array(
        name=schema.VARIABLE,
        shape=shape,
        dtype="uint16",
        fill_value=schema.NODATA,
        dimension_names=schema.DIMENSIONS,
        chunks=(1, n_band, chunk_size, chunk_size),
        shards=(n_time, n_band, chunk_size, chunk_size) if shard else None,
        compressors=ZstdCodec(level=ZSTD_LEVEL),
        filters=None,
    )


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


@dataclass(frozen=True)
class _CellResult:
    encode_s: float
    write_s: float
    abs_delta_sum: int
    n_delta_values: int


def _encode_cell(
    level: np.ndarray,
    array: zarr.Array,
    row: int,
    col: int,
    lod: int,
    chunk_size: int,
    temporal: Temporal,
    want_volatility: bool,
) -> _CellResult:
    """Star-delta encode one spatial cell across all timesteps and write it."""
    started = time.perf_counter()
    height, width = level.shape[2:]
    ys = slice(row * chunk_size, min((row + 1) * chunk_size, height))
    xs = slice(col * chunk_size, min((col + 1) * chunk_size, width))
    block = level[:, :, ys, xs]
    out = np.empty(block.shape, dtype=np.uint16)
    out_signed = out.view(np.int16)

    for anchor in temporal.anchor_indices:
        out[anchor] = block[anchor]

    anchors32: dict[int, np.ndarray] = {}
    abs_delta_sum = n_delta_values = 0
    for t, anchor in temporal.delta_reference.items():
        if anchor not in anchors32:
            anchors32[anchor] = block[anchor].astype(np.int32)
        delta = block[t].astype(np.int32)
        delta -= anchors32[anchor]
        if delta.min() < INT16_MIN or delta.max() > INT16_MAX:
            n_over = int(np.count_nonzero((delta < INT16_MIN) | (delta > INT16_MAX)))
            raise ValueError(
                f"level {lod} cell (row {row}, col {col}): {n_over} pixel(s) at timestep {t} "
                f"differ from anchor {anchor} by more than the int16 range "
                f"({INT16_MIN}..{INT16_MAX}), which star-delta cannot store losslessly. "
                "Rescale the data so values stay within 32767 of each other, or use "
                "anchor_interval=1 to store every timestep as an anchor."
            )
        if want_volatility:
            abs_delta_sum += int(np.abs(delta).sum(dtype=np.int64))
            n_delta_values += delta.size
        out_signed[t] = delta
    encoded = time.perf_counter()

    array[:, :, ys, xs] = out
    written = time.perf_counter()
    return _CellResult(encoded - started, written - encoded, abs_delta_sum, n_delta_values)


def _downsample_level(level: np.ndarray, pool: ThreadPoolExecutor) -> np.ndarray:
    n_time, n_band, height, width = level.shape
    out = np.empty((n_time, n_band, -(-height // 2), -(-width // 2)), dtype=np.uint16)

    def one(t: int) -> None:
        out[t] = downsample_2x(level[t])

    list(pool.map(one, range(n_time)))
    return out


def _tree_stats(path: Path) -> tuple[int, int]:
    files = [p for p in path.rglob("*") if p.is_file()]
    return sum(p.stat().st_size for p in files), len(files)


def encode(
    da: xr.DataArray,
    out: str | Path,
    *,
    crs: str | None = None,
    transform: Sequence[float] | None = None,
    chunk_size: int = 512,
    anchor_interval: int = 6,
    n_lods: int | None = None,
    shard: bool = True,
    workers: int | None = None,
) -> EncodeReport:
    """Write `da` as a chronozarr v0.1 store at `out`.

    Args:
        da: uint16 DataArray with dims (time, band, y, x); `time` must be datetime64. It is
            loaded into memory in full.
        out: Directory to create. Must not exist or must be empty (stores are immutable).
        crs: CRS string such as "EPSG:32631". Falls back to `da.attrs["crs"]`.
        transform: Affine coefficients (a, b, c, d, e, f) of the north-up level-0 grid. Falls
            back to `da.attrs["transform"]`, then to the x/y pixel-centre coordinates.
        chunk_size: Spatial chunk (and cell) edge in pixels.
        anchor_interval: Timesteps between anchors (1 = every timestep is an anchor).
        n_lods: Number of pyramid levels including level 0. Default: stop at the first level
            whose cell grid is 1 x 1.
        shard: One shard per spatial cell holding the whole time axis (default), or one Zarr
            chunk object per (timestep, cell).
        workers: Worker threads. Default: CPU count.

    Raises ValueError if any pixel differs from its anchor by more than the int16 range, and
    removes the partially written store. Use anchor_interval=1 for data with such jumps.
    """
    if da.dims != schema.DIMENSIONS:
        raise ValueError(f"expected dims {schema.DIMENSIONS}, got {da.dims}; use da.transpose()")
    if da.dtype != np.uint16:
        raise ValueError(f"expected uint16 data, got {da.dtype}; chronozarr never converts dtype")
    if 0 in da.shape:
        raise ValueError(f"empty input: shape {da.shape}")
    if chunk_size < 1 or anchor_interval < 1:
        raise ValueError("chunk_size and anchor_interval must be >= 1")
    workers = workers or os.cpu_count() or 1
    if workers < 1:
        raise ValueError(f"workers must be >= 1, got {workers}")
    resolved_crs = crs if crs is not None else da.attrs.get("crs")
    if not resolved_crs:
        raise ValueError("no crs: pass crs='EPSG:xxxxx' or set da.attrs['crs']")
    resolved_crs = str(resolved_crs)
    base_transform = _resolve_transform(da, transform)
    times_iso, times_ms = _iso_times(da["time"].values)
    bands = tuple(str(b) for b in da["band"].values)
    if len(set(bands)) != len(bands):
        raise ValueError(f"band names must be unique, got {list(bands)}")

    out = Path(out)
    existed = out.exists()
    if existed and any(out.iterdir()):
        raise FileExistsError(
            f"{out} already exists and is not empty; stores are immutable, write to a new path"
        )

    data = np.ascontiguousarray(da.values)
    shapes = schema.level_shapes(data.shape[2], data.shape[3], chunk_size, n_lods)
    try:
        return _write_store(
            data,
            out,
            shapes=shapes,
            crs=resolved_crs,
            transform=base_transform,
            times_iso=times_iso,
            times_ms=times_ms,
            bands=bands,
            chunk_size=chunk_size,
            temporal=Temporal.build(data.shape[0], anchor_interval),
            shard=shard,
            workers=workers,
        )
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        if existed:
            out.mkdir()
        raise


def _write_store(
    data: np.ndarray,
    out: Path,
    *,
    shapes: list[tuple[int, int]],
    crs: str,
    transform: Transform,
    times_iso: list[str],
    times_ms: np.ndarray,
    bands: tuple[str, ...],
    chunk_size: int,
    temporal: Temporal,
    shard: bool,
    workers: int,
) -> EncodeReport:
    n_time, n_band, height, width = data.shape
    root = zarr.open_group(str(out), mode="w", zarr_format=3)
    rows, cols = schema.grid_shape(height, width, chunk_size)
    volatility = np.zeros((rows, cols), dtype=np.float32)
    reports: list[LevelReport] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        level_data = data
        for k, (level_h, level_w) in enumerate(shapes):
            downsample_s = 0.0
            if k > 0:
                started = time.perf_counter()
                level_data = _downsample_level(level_data, pool)
                downsample_s = time.perf_counter() - started

            level_transform = schema.scale_transform(transform, k)
            group = root.create_group(str(k))
            group.attrs.update(
                schema.LevelAttrs(crs, level_transform, level_transform[0]).to_attrs()
            )
            array = _create_data_array(group, level_data.shape, chunk_size, shard)
            array.attrs.update(schema.data_array_attrs(crs, level_transform, level_h, level_w))
            _write_coords(group, level_transform, level_h, level_w, times_ms, bands)

            level_rows, level_cols = schema.grid_shape(level_h, level_w, chunk_size)
            cells = [(r, c) for r in range(level_rows) for c in range(level_cols)]
            started = time.perf_counter()
            futures = [
                pool.submit(_encode_cell, level_data, array, r, c, k, chunk_size, temporal, k == 0)
                for r, c in cells
            ]
            try:
                results = [f.result() for f in futures]
            except BaseException:
                for f in futures:
                    f.cancel()
                raise
            cells_s = time.perf_counter() - started

            if k == 0:
                for (r, c), res in zip(cells, results, strict=True):
                    if res.n_delta_values:
                        mean_abs = res.abs_delta_sum / res.n_delta_values
                        volatility[r, c] = min(max(mean_abs / VOLATILITY_SCALE, 0.0), 1.0)
            reports.append(
                LevelReport(
                    level=k,
                    shape=(n_time, n_band, level_h, level_w),
                    downsample_s=downsample_s,
                    cells_s=cells_s,
                    encode_thread_s=sum(r.encode_s for r in results),
                    write_thread_s=sum(r.write_s for r in results),
                    bytes=0,
                )
            )

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

    meta = Chronozarr(times=tuple(times_iso), bands=bands, crs=crs, temporal=temporal)
    datasets = tuple(LevelRef(str(k), chunk_size, crs) for k in range(len(shapes)))
    root.attrs.update(RootAttrs(meta, datasets).to_attrs())
    with warnings.catch_warnings():
        # zarr-python warns that consolidated metadata is outside the Zarr v3 spec. chronozarr
        # writes it deliberately (spec 3.1) so a reader learns every array in one GET.
        warnings.simplefilter("ignore", ZarrUserWarning)
        zarr.consolidate_metadata(str(out))

    total_bytes, n_files = _tree_stats(out)
    return EncodeReport(
        levels=tuple(replace(r, bytes=_tree_stats(out / str(r.level))[0]) for r in reports),
        total_bytes=total_bytes,
        n_files=n_files,
    )
