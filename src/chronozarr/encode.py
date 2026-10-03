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
import time
import warnings
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numcodecs
import numcodecs.abc
import numpy as np
import xarray as xr
import zarr
from zarr.codecs import BloscCodec, ZstdCodec
from zarr.errors import ZarrUserWarning

from chronozarr import schema
from chronozarr._writer import (
    DEFAULT_CELLS_IN_FLIGHT as DEFAULT_CELLS_IN_FLIGHT,
)
from chronozarr._writer import (
    VOLATILITY_SCALE as VOLATILITY_SCALE,
)
from chronozarr._writer import (
    Block as Block,
)
from chronozarr._writer import (
    _ArraySource,
    _CellWriter,
    _iso_times,
    _LevelArrays,
    _prepare_input,
    _put_coord,
    _Pyramid,
    _resolve_bands,
    _resolve_nodata,
    _resolve_transform,
    _shard_bytes,
    _Source,
    _spill_timesteps,
    _write_time_coord,
)
from chronozarr._writer import (
    _downsample_plane_pair as _downsample_plane_pair,
)
from chronozarr._writer import (
    downsample_2x as downsample_2x,
)
from chronozarr._writer import (
    downsample_block as downsample_block,
)
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

AUTO_RATIO_LIMIT = 0.85  # star-delta must be at most this fraction of the plain bytes
AUTO_MIN_CELLS = 3
AUTO_SAMPLE_FRACTION = 0.1
DEFAULT_LEVEL = {"zstd": 5, "blosc-zstd-shuffle": 1}
LEVEL_RANGE = {"zstd": (1, 22), "blosc-zstd-shuffle": (0, 9)}


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
    _write_time_coord(group, times_ms)
    _put_coord(group, "band", np.array(bands, dtype=object), str)
    y, x = schema.pixel_centers(transform, height, width)
    _put_coord(group, "x", x, "float64")
    _put_coord(group, "y", y, "float64")


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
    writer = _CellWriter[_CellResult](cells_in_flight)
    compute = ThreadPoolExecutor(max_workers=os.cpu_count() or 1)
    pyramid = _Pyramid(
        layout.shapes,
        cs,
        n_time=layout.n_time,
        n_band=layout.n_band,
        dtype=layout.dtype,
        nodata=layout.nodata,
        source=source,
        compute=compute,
    )

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

    try:
        pyramid.walk(submit)
        results = writer.results()
    except BaseException:
        writer.shutdown()
        raise
    finally:
        compute.shutdown(wait=True)
    writer.shutdown()
    downsample_s = pyramid.downsample_s

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
        shard_bytes=_shard_bytes(out, len(layout.shapes)) if layout.shard else None,
    )
    datasets = tuple(LevelRef(str(k), layout.crs) for k in range(len(layout.shapes)))
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
    shard: bool = False,
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
            uint16 and None for int16 and float32, and None whenever `mask` is given.
        mask: Optional uint8 validity plane (1 = valid), (time, y, x) DataArray, or an iterable
            of per-timestep (y, x) arrays when `data` is an iterable. Overrides nodata for
            pyramid means and readers.
        coverage: Optional uint8 count of valid observations per pixel, same shape rules.
        provenance: Optional {"sources": [...], "composite": str, "gap_fill": "carry-forward" |
            "none", "notes": str}.
        chunk_size: Spatial chunk (and cell) edge in pixels; even. 512 (default) and 256 are
            the spec values; smaller even sizes exist for tests.
        shard: False (default): one object per (timestep, cell, level), no shard index, a CDN
            miss costs one chunk and an append writes only new objects. True: one object per
            (time shard, cell, level), far fewer objects, but a miss costs a whole shard and an
            append rewrites the trailing one.
        shard_time: Timesteps per shard along time; needs `shard=True`. Default: all of them. A
            value larger than the timesteps given is allowed: the first shard then holds them
            and any appended later.
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
    resolved_nodata = _resolve_nodata(nodata, prepared.dtype, has_mask=prepared.mask is not None)
    if encoding == schema.STAR_DELTA and prepared.dtype.name not in schema.TEMPORAL_DTYPES:
        raise ValueError(
            f"star-delta needs uint8 or uint16 data, got {prepared.dtype}; use encoding='none'"
        )
    resolved_provenance = None if provenance is None else schema.parse_provenance(provenance)
    if shard_time is not None and shard_time < 1:
        raise ValueError(f"shard_time must be at least 1, got {shard_time}")
    if shard_time is not None and not shard:
        raise ValueError(f"shard_time={shard_time} applies to sharded stores; pass shard=True")

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
            source = _spill_timesteps(
                prepared,
                spill,
                chunk_size=chunk_size,
                has_mask=layout.has_mask,
                has_coverage=layout.has_coverage,
            )
        return _encode(out, layout, source, encoding, cells_in_flight, resolved_provenance)
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        if existed:
            out.mkdir()
        raise
    finally:
        if spill is not None:
            shutil.rmtree(spill, ignore_errors=True)


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
