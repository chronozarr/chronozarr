"""Streaming conversion of COG manifests, Zarr variables and NetCDF files to a chronozarr store.

The source is read one timestep at a time and staged as a raw `.npy` per timestep in a work
directory, then handed to `encode()` as an iterable of timesteps, which spills and encodes cell
by cell. Peak memory is about one timestep times `read_ahead`; disk is the raw stack once in the
work directory, once more in `encode`'s cell-major spill, plus the output. Staged timesteps are
the unit of `resume`: an interrupted run keeps them, and a rerun reads only the missing ones.

Three input kinds are recognised from SOURCE:

* a manifest (`.csv` or `.json`) of COG URIs with timestamps, one URI per timestep;
* a Zarr store (local path or http(s) URL) with a chosen variable;
* a NetCDF file (`.nc`, `.nc4`, `.cdf`) with a chosen variable (needs an xarray NetCDF engine).

Needs rasterio (`chronozarr[geo]`) for COG manifests and for the CRS handling of all kinds.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr
from numcodecs import Zstd

from chronozarr import schema
from chronozarr.decode import as_store
from chronozarr.encode import EncodeReport, encode
from chronozarr.schema import Band, Transform

GDAL_ENV = {
    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    "GDAL_HTTP_MAX_RETRY": "3",
    "GDAL_HTTP_RETRY_DELAY": "1",
}
NETCDF_SUFFIXES = (".nc", ".nc4", ".cdf")
RESAMPLING_METHODS = ("nearest", "bilinear", "cubic", "average", "mode", "min", "max", "med")
# Raw input bytes per second through `encode()` (spill, pyramid, zstd level 5, write). Measured
# 230 to 260 MB/s on the 117-month Ucayali stack (7.1 GB in 27 to 31 s) on a 16-thread M3 Max
# laptop with other work running, 2026-09-30; set lower so estimates err on the long side.
ENCODE_BYTES_PER_S = 150e6
SAMPLE_TIMESTEPS = 3
_DIM_ALIASES = {
    "time": ("time", "t", "datetime", "valid_time"),
    "band": ("band", "bands", "channel", "variable"),
    "y": ("y", "lat", "latitude", "northing"),
    "x": ("x", "lon", "longitude", "easting"),
}


@dataclass(frozen=True)
class Grid:
    """Target pixel grid: a CRS (EPSG string), a north-up transform and a shape."""

    crs: str
    transform: Transform
    height: int
    width: int

    def describe(self) -> str:
        return (
            f"{self.crs}, {self.height} x {self.width} px, "
            f"{abs(self.transform[0]):.10g} m pixels (transform origin "
            f"{self.transform[2]:.10g}, {self.transform[5]:.10g})"
        )


@dataclass(frozen=True)
class Entry:
    """One timestep of a COG manifest."""

    uri: str
    time: np.datetime64


@dataclass(frozen=True)
class SourceInfo:
    """What a source reports about itself before any pixel is read."""

    grid: Grid
    n_band: int
    dtype: np.dtype
    nodata: float | int | None
    band_names: tuple[str, ...]
    bands: tuple[Band, ...]


class Source:
    """A timestep-addressable input on a known grid."""

    kind: str
    times: list[np.datetime64]
    info: SourceInfo
    # Indices of timesteps that are not on the target grid and must be warped (COG only).
    warped: frozenset[int] = frozenset()

    def read(self, t: int) -> np.ndarray:
        """Timestep `t` as (band, y, x) in `info.dtype` on `info.grid`."""
        raise NotImplementedError

    def fingerprint(self) -> Any:
        """JSON-able identity of what `read` will produce, for resume."""
        raise NotImplementedError


# --- Time and manifest parsing ------------------------------------------------------------------


def parse_time(text: str, where: str) -> np.datetime64:
    """ISO-8601 date or datetime (Z or an offset allowed) as UTC milliseconds."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise ValueError(
            f"{where}: cannot parse datetime {text!r}; use ISO-8601 like 2024-03-01 or "
            "2024-03-01T10:30:00Z"
        ) from exc
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC).replace(tzinfo=None)
    return np.datetime64(moment, "ms")


def _resolve_uri(uri: str, base: Path) -> str:
    if "://" in uri or uri.startswith("/vsi") or Path(uri).is_absolute():
        return uri
    return str((base / uri).resolve())


def _split_bands(value: object, where: str) -> tuple[str, ...] | None:
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, str):
        names = [n.strip() for n in value.split(";") if n.strip()]
    elif isinstance(value, list) and all(isinstance(n, str) for n in value):
        names = [str(n).strip() for n in value]
    else:
        raise ValueError(f"{where}: bands must be a list of names (CSV: separated by ';')")
    return tuple(names)


def read_manifest(path: Path) -> tuple[list[Entry], tuple[str, ...] | None]:
    """Entries sorted by time and the optional band names of a CSV or JSON manifest.

    CSV: header `uri,datetime[,bands]`, band names separated by `;`. JSON: a list of
    `{"uri", "datetime", "bands"?}` objects, or `{"bands"?: [...], "items": [...]}`. Relative
    URIs are resolved against the manifest's directory. Every row that names bands must agree.
    """
    if not path.is_file():
        raise FileNotFoundError(f"manifest {path} does not exist")
    rows: list[Mapping[str, Any]]
    top_bands: object = None
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            missing = {"uri", "datetime"} - set(reader.fieldnames or [])
            if missing:
                raise ValueError(
                    f"{path}: CSV header must contain uri and datetime (optionally bands); "
                    f"missing {sorted(missing)}, found {reader.fieldnames}"
                )
            rows = list(reader)
    elif path.suffix.lower() == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(document, dict):
            top_bands = document.get("bands")
            rows = document.get("items", [])
        else:
            rows = document
        if not isinstance(rows, list):
            raise ValueError(f"{path}: expected a list of items or an object with 'items'")
    else:
        raise ValueError(f"{path}: a manifest must be .csv or .json")
    if not rows:
        raise ValueError(f"{path}: the manifest has no rows")

    names = _split_bands(top_bands, f"{path} bands")
    entries = []
    for i, row in enumerate(rows, start=1):
        where = f"{path} row {i}"
        if not isinstance(row, Mapping) or not row.get("uri") or not row.get("datetime"):
            raise ValueError(f"{where}: needs both 'uri' and 'datetime'")
        row_names = _split_bands(row.get("bands"), where)
        if row_names is not None:
            if names is not None and row_names != names:
                raise ValueError(
                    f"{where}: bands {list(row_names)} differ from {list(names)}; every row must "
                    "name the same bands in the same order"
                )
            names = row_names
        entries.append(
            Entry(
                _resolve_uri(str(row["uri"]), path.resolve().parent),
                parse_time(str(row["datetime"]), where),
            )
        )
    entries.sort(key=lambda e: e.time)
    for earlier, later in pairwise(entries):
        if earlier.time == later.time:
            raise ValueError(
                f"{path}: two rows share the datetime {later.time}: {earlier.uri} and "
                f"{later.uri}; a store has one timestep per time"
            )
    return entries, names


# --- Grids --------------------------------------------------------------------------------------


def _epsg(crs: Any, where: str) -> str:
    code = crs.to_epsg() if crs is not None else None
    if code is None:
        raise ValueError(
            f"{where}: the CRS has no EPSG code; chronozarr stores declare EPSG:<code>. "
            "Pass --crs EPSG:xxxxx"
        )
    return f"EPSG:{code}"


def _check_north_up(transform: Sequence[float], where: str) -> Transform:
    a, b, c, d, e, f = (float(v) for v in list(transform)[:6])
    if b != 0 or d != 0 or a <= 0 or e >= 0:
        raise ValueError(
            f"{where}: the grid is rotated or not north-up (a={a}, b={b}, d={d}, e={e}); "
            "chronozarr needs north-up grids. Warp to one with --crs, --transform, --shape and "
            "--resampling"
        )
    return (a, b, c, d, e, f)


def _same_grid(a: Grid, b: Grid) -> bool:
    if a.crs != b.crs or (a.height, a.width) != (b.height, b.width):
        return False
    tolerance = 1e-6 * abs(a.transform[0])
    return all(abs(x - y) <= tolerance for x, y in zip(a.transform, b.transform, strict=True))


def _grid_difference(found: Grid, wanted: Grid) -> str:
    parts = []
    if found.crs != wanted.crs:
        parts.append(f"CRS {found.crs} vs {wanted.crs}")
    if (found.height, found.width) != (wanted.height, wanted.width):
        parts.append(f"shape {found.height}x{found.width} vs {wanted.height}x{wanted.width}")
    if not all(
        abs(x - y) <= 1e-6 * abs(wanted.transform[0])
        for x, y in zip(found.transform, wanted.transform, strict=True)
    ):
        parts.append(
            f"transform {[round(v, 6) for v in found.transform]} vs "
            f"{[round(v, 6) for v in wanted.transform]}"
        )
    return "; ".join(parts)


# --- COG manifest source ------------------------------------------------------------------------


@dataclass(frozen=True)
class _CogHeader:
    grid: Grid
    count: int
    dtype: np.dtype
    nodata: float | int | None
    descriptions: tuple[str | None, ...]


def _read_header(uri: str) -> _CogHeader:
    import rasterio

    try:
        with rasterio.Env(**GDAL_ENV), rasterio.open(uri) as src:
            if src.crs is None:
                raise ValueError("no CRS")
            transform = tuple(src.transform)[:6]
            nodata = src.nodata
            return _CogHeader(
                Grid(_epsg(src.crs, uri), _check_north_up(transform, uri), src.height, src.width),
                src.count,
                np.dtype(src.dtypes[0]),
                _plain_nodata(nodata, np.dtype(src.dtypes[0])),
                tuple(src.descriptions),
            )
    except (rasterio.errors.RasterioIOError, ValueError) as exc:
        raise ValueError(f"cannot open source {uri}: {exc}") from exc


def _plain_nodata(value: float | None, dtype: np.dtype) -> float | int | None:
    """A finite nodata value, as an int for integer dtypes (rasterio reports 0.0 for 0)."""
    if value is None or not math.isfinite(value):
        return None
    return int(value) if dtype.kind in "iu" and float(value).is_integer() else value


def _duration(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 120 else f"{seconds / 60:.1f} min"


def _parse_resampling(name: str) -> Any:
    from rasterio.enums import Resampling

    if name not in RESAMPLING_METHODS:
        raise ValueError(f"unknown resampling {name!r}; choose one of {list(RESAMPLING_METHODS)}")
    return Resampling[name]


class CogManifestSource(Source):
    kind = "manifest of COGs"

    def __init__(
        self,
        entries: list[Entry],
        band_names: tuple[str, ...] | None,
        *,
        target_crs: str | None,
        target_transform: Transform | None,
        target_shape: tuple[int, int] | None,
        resampling: str | None,
        nodata: float | int | None | str,
        chunk_size: int,
    ) -> None:
        self.entries = entries
        self.times = [e.time for e in entries]
        self.resampling = resampling
        self.chunk_size = chunk_size
        workers = min(8, len(entries))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            headers = list(pool.map(lambda e: _read_header(e.uri), entries))
        first = headers[0]
        self.grid = self._target_grid(first, target_crs, target_transform, target_shape)
        self._check_consistent(headers, nodata)
        names = band_names or tuple(d or str(i + 1) for i, d in enumerate(first.descriptions))
        if len(names) != first.count:
            raise ValueError(
                f"{entries[0].uri} has {first.count} bands but {len(names)} band names were "
                f"given: {list(names)}"
            )
        if len(set(names)) != len(names):
            raise ValueError(f"band names must be unique, got {list(names)}")
        self.warped = frozenset(
            i for i, h in enumerate(headers) if not _same_grid(h.grid, self.grid)
        )
        if self.warped:
            if resampling is None:
                self._explain_mismatch(headers)
            assert resampling is not None
            _parse_resampling(resampling)
        source_nodata = first.nodata if nodata == "auto" else nodata
        self.info = SourceInfo(
            grid=self.grid,
            n_band=first.count,
            dtype=first.dtype,
            nodata=source_nodata if isinstance(source_nodata, int | float) else None,
            band_names=names,
            bands=tuple(Band(name=n) for n in names),
        )

    @staticmethod
    def _target_grid(
        first: _CogHeader,
        crs: str | None,
        transform: Transform | None,
        shape: tuple[int, int] | None,
    ) -> Grid:
        from rasterio.crs import CRS  # ty: ignore[unresolved-import]  # compiled, no stubs
        from rasterio.warp import calculate_default_transform

        if transform is not None:
            if shape is None or crs is None:
                raise ValueError("--transform needs --shape and --crs as well")
            return Grid(crs, _check_north_up(transform, "--transform"), shape[0], shape[1])
        if crs is None or CRS.from_user_input(crs) == CRS.from_user_input(first.grid.crs):
            if shape is not None:
                raise ValueError("--shape is only meaningful together with --transform")
            return first.grid
        if shape is not None:
            raise ValueError("--shape is only meaningful together with --transform")
        source = first.grid
        west = source.transform[2]
        north = source.transform[5]
        east = west + source.transform[0] * source.width
        south = north + source.transform[4] * source.height
        dst, width, height = calculate_default_transform(
            source.crs, crs, source.width, source.height, west, south, east, north
        )
        return Grid(
            _epsg(CRS.from_user_input(crs), "--crs"),
            _check_north_up(tuple(dst)[:6], "--crs"),
            height,
            width,
        )

    def _check_consistent(
        self, headers: list[_CogHeader], nodata: float | int | None | str
    ) -> None:
        first = headers[0]
        for entry, header in zip(self.entries, headers, strict=True):
            if header.count != first.count:
                raise ValueError(
                    f"{entry.uri} has {header.count} bands; {self.entries[0].uri} has "
                    f"{first.count}"
                )
            if header.dtype != first.dtype:
                raise ValueError(
                    f"{entry.uri} is {header.dtype}; {self.entries[0].uri} is {first.dtype}. "
                    "All sources must share one dtype"
                )
            if nodata == "auto" and header.nodata != first.nodata:
                raise ValueError(
                    f"{entry.uri} has nodata {header.nodata}; {self.entries[0].uri} has "
                    f"{first.nodata}. Pass --nodata to force one value (or 'none')"
                )
        if first.dtype.name not in schema.DTYPES:
            raise ValueError(
                f"sources are {first.dtype}; chronozarr stores hold {list(schema.DTYPES)}. "
                "Convert the sources first (for example with gdal_translate -ot)"
            )

    def _explain_mismatch(self, headers: list[_CogHeader]) -> None:
        offenders = [
            (self.entries[i].uri, _grid_difference(headers[i].grid, self.grid))
            for i in sorted(self.warped)
        ]
        shown = "; ".join(f"{uri}: {why}" for uri, why in offenders[:3])
        more = f" (and {len(offenders) - 3} more)" if len(offenders) > 3 else ""
        raise ValueError(
            f"{len(offenders)} of {len(self.entries)} sources are not on the target grid "
            f"({self.grid.describe()}): {shown}{more}. Pass --resampling "
            f"{'|'.join(RESAMPLING_METHODS)} to warp them onto it, or choose the grid with "
            "--crs/--transform/--shape"
        )

    def fingerprint(self) -> Any:
        return {
            "kind": "manifest",
            "entries": [[e.uri, str(e.time)] for e in self.entries],
            "grid": [self.grid.crs, list(self.grid.transform), self.grid.height, self.grid.width],
            "bands": list(self.info.band_names),
            "dtype": self.info.dtype.name,
            "nodata": self.info.nodata,
            "resampling": self.resampling if self.warped else None,
            "warped": sorted(self.warped),
        }

    def read(self, t: int) -> np.ndarray:
        import rasterio
        from rasterio.transform import Affine

        entry = self.entries[t]
        grid = self.grid
        out = np.empty((self.info.n_band, grid.height, grid.width), dtype=self.info.dtype)
        try:
            with rasterio.Env(**GDAL_ENV), rasterio.open(entry.uri) as src:
                if t in self.warped:
                    from rasterio.warp import reproject

                    fill = self.info.nodata if self.info.nodata is not None else 0
                    out[:] = fill
                    assert self.resampling is not None
                    reproject(
                        source=rasterio.band(src, list(range(1, src.count + 1))),
                        destination=out,
                        src_nodata=src.nodata,
                        dst_transform=Affine(*grid.transform),
                        dst_crs=grid.crs,
                        dst_nodata=fill,
                        resampling=_parse_resampling(self.resampling),
                    )
                else:
                    for top in range(0, grid.height, self.chunk_size):
                        rows = min(self.chunk_size, grid.height - top)
                        out[:, top : top + rows, :] = src.read(
                            window=((top, top + rows), (0, grid.width))
                        )
        except rasterio.errors.RasterioError as exc:
            raise OSError(f"reading {entry.uri} (timestep {t}): {exc}") from exc
        return out


# --- xarray (Zarr / NetCDF) source --------------------------------------------------------------


def _parse_dims(spec: str | None) -> dict[str, str]:
    if not spec:
        return {}
    out: dict[str, str] = {}
    for part in spec.split(","):
        key, sep, name = part.partition("=")
        if not sep or key.strip() not in _DIM_ALIASES or not name.strip():
            raise ValueError(
                f"bad --dims entry {part!r}: use time=NAME,y=NAME,x=NAME[,band=NAME] "
                f"(keys {list(_DIM_ALIASES)})"
            )
        out[key.strip()] = name.strip()
    return out


def _open_dataset(source: str) -> xr.Dataset:
    if source.lower().endswith(NETCDF_SUFFIXES):
        if not Path(source).is_file():
            raise FileNotFoundError(f"NetCDF file {source} does not exist")
        return xr.open_dataset(source, mask_and_scale=False)
    if not source.startswith(("http://", "https://")) and not Path(source).exists():
        raise FileNotFoundError(f"input {source} does not exist")
    store = as_store(source)
    try:
        return xr.open_zarr(store, chunks=None, consolidated=True, mask_and_scale=False)
    except (KeyError, ValueError):
        if source.startswith(("http://", "https://")):
            raise ValueError(
                f"{source}: a remote Zarr input needs consolidated metadata (.zmetadata or "
                "consolidated zarr.json) because HTTP stores cannot be listed"
            ) from None
    # Local store without consolidated metadata: xarray falls back to listing and warns.
    return xr.open_zarr(store, chunks=None, consolidated=False, mask_and_scale=False)


def _first_present(names: Sequence[Any], aliases: Sequence[str]) -> str | None:
    lowered = {str(n).lower(): str(n) for n in names}
    return next((lowered[a] for a in aliases if a in lowered), None)


class XarraySource(Source):
    def __init__(
        self,
        source: str,
        variable: str | None,
        dims: dict[str, str],
        crs: str | None,
        nodata: float | int | None | str,
    ) -> None:
        self.kind = "NetCDF file" if source.lower().endswith(NETCDF_SUFFIXES) else "Zarr store"
        self.source = source
        self.variable_name = variable
        dataset = _open_dataset(source)
        names = list(dataset.data_vars)
        if variable is None:
            if len(names) != 1:
                raise ValueError(f"{source} has variables {names}; choose one with --variable")
            variable = str(names[0])
        if variable not in dataset.data_vars:
            raise ValueError(f"variable {variable!r} is not in {source}: {names}")
        da = dataset[variable]
        self.variable = variable
        resolved = {}
        for key, aliases in _DIM_ALIASES.items():
            found = dims.get(key) or _first_present(list(da.dims), aliases)
            if found is not None and found not in da.dims:
                raise ValueError(f"--dims {key}={found}: {variable} has dims {list(da.dims)}")
            if found is not None:
                resolved[key] = found
        for key in ("time", "y", "x"):
            if key not in resolved:
                raise ValueError(
                    f"cannot find the {key} dimension of {variable} (dims {list(da.dims)}); "
                    f"name it with --dims {key}=NAME"
                )
        if "band" not in resolved and len(da.dims) != 3:
            raise ValueError(
                f"{variable} has dims {list(da.dims)}; expected time, y, x and optionally a band "
                "dimension (name it with --dims band=NAME)"
            )
        if len(da.dims) != len(resolved):
            extra = sorted(str(d) for d in set(da.dims) - set(resolved.values()))
            raise ValueError(
                f"{variable} has unexpected dimensions {extra}; select or squeeze them first"
            )
        self._dims = resolved
        if da.dtype.name not in schema.DTYPES:
            raise ValueError(
                f"{variable} is {da.dtype}; chronozarr stores hold {list(schema.DTYPES)}. "
                "Cast it first"
            )

        time_values = np.asarray(da[resolved["time"]].values)
        if not np.issubdtype(time_values.dtype, np.datetime64):
            raise ValueError(
                f"the time coordinate {resolved['time']!r} is {time_values.dtype}, not datetimes; "
                "decode it (CF units) or convert it to datetime64"
            )
        order = np.argsort(time_values, kind="stable")
        self._order = [int(i) for i in order]
        sorted_times = time_values[order].astype("datetime64[ms]")
        if len(sorted_times) > 1 and not (np.diff(sorted_times.astype(np.int64)) > 0).all():
            raise ValueError(f"the time coordinate of {variable} has duplicate values")
        self.times = list(sorted_times)

        x = np.asarray(da[resolved["x"]].values, dtype=np.float64)
        y = np.asarray(da[resolved["y"]].values, dtype=np.float64)
        if len(x) < 2 or len(y) < 2:
            raise ValueError("need at least 2 pixels along x and y to derive the grid")
        dx, dy = float(x[1] - x[0]), float(y[1] - y[0])
        if dx <= 0:
            raise ValueError("x must increase; flip the data first")
        if not (np.allclose(np.diff(x), dx) and np.allclose(np.diff(y), dy)):
            raise ValueError("x/y coordinates are not regularly spaced")
        self._flip_y = dy > 0
        y_top = float(y[-1]) if self._flip_y else float(y[0])
        pixel_h = abs(dy)
        transform = (dx, 0.0, float(x[0]) - dx / 2, 0.0, -pixel_h, y_top + pixel_h / 2)
        crs_text = crs or self._crs_from_attrs(da, dataset)
        if crs_text is None:
            raise ValueError(f"{source} does not declare a CRS; pass --crs EPSG:xxxxx")
        from rasterio.crs import CRS  # ty: ignore[unresolved-import]  # compiled, no stubs

        grid = Grid(_epsg(CRS.from_user_input(crs_text), "CRS"), transform, len(y), len(x))

        raw_fill = da.attrs.get("_FillValue")
        if nodata == "auto":
            chosen = raw_fill if raw_fill is not None and np.isfinite(raw_fill) else None
            resolved_nodata = chosen.item() if isinstance(chosen, np.generic) else chosen
        else:
            resolved_nodata = nodata if isinstance(nodata, int | float) else None

        if "band" in resolved:
            band_names = tuple(str(b) for b in da[resolved["band"]].values)
        else:
            band_names = (variable,)
        scale, offset = da.attrs.get("scale_factor"), da.attrs.get("add_offset")
        units = da.attrs.get("units")
        bands = tuple(
            Band(
                name=n,
                scale=float(scale) if scale is not None else None,
                offset=float(offset) if offset is not None else None,
                units=str(units) if units else None,
            )
            for n in band_names
        )
        self.da = da
        self.info = SourceInfo(grid, len(band_names), da.dtype, resolved_nodata, band_names, bands)

    @staticmethod
    def _crs_from_attrs(da: xr.DataArray, dataset: xr.Dataset) -> str | None:
        for holder in (da.attrs, dataset.attrs):
            for key in ("crs", "crs_wkt", "spatial_ref"):
                if holder.get(key):
                    return str(holder[key])
        for name in ("spatial_ref", "crs"):
            if name in dataset.variables:
                attrs = dataset[name].attrs
                for key in ("crs_wkt", "spatial_ref", "crs"):
                    if attrs.get(key):
                        return str(attrs[key])
        return None

    def fingerprint(self) -> Any:
        grid = self.info.grid
        return {
            "kind": "xarray",
            "source": self.source,
            "variable": self.variable,
            "grid": [grid.crs, list(grid.transform), grid.height, grid.width],
            "bands": list(self.info.band_names),
            "times": [str(t) for t in self.times],
            "dtype": self.info.dtype.name,
            "nodata": self.info.nodata,
        }

    def read(self, t: int) -> np.ndarray:
        d = self._dims
        piece = self.da.isel({d["time"]: self._order[t]})
        order = [d["band"], d["y"], d["x"]] if "band" in d else [d["y"], d["x"]]
        values = np.asarray(piece.transpose(*order).values)
        if "band" not in d:
            values = values[np.newaxis]
        if self._flip_y:
            values = values[:, ::-1, :]
        return np.ascontiguousarray(values)


# --- Plan ---------------------------------------------------------------------------------------


@dataclass
class Plan:
    """Everything `convert` decided before reading pixels, including size and time estimates."""

    source: Source
    n_time: int
    timestep_bytes: int
    raw_bytes: int
    n_levels: int
    sample_ratio: float | None
    est_output_bytes: int | None
    sample_read_s: float | None
    est_encode_s: float
    warped: int
    samples: dict[int, np.ndarray] = field(default_factory=dict, repr=False)

    def lines(self, read_ahead: int = 2) -> list[str]:
        info = self.source.info
        times = self.source.times
        out = [
            f"source:     {self.source.kind}, {self.n_time} timesteps "
            f"({np.datetime_as_string(times[0], unit='D')} .. "
            f"{np.datetime_as_string(times[-1], unit='D')})",
            f"grid:       {info.grid.describe()}",
            f"data:       {info.n_band} bands ({', '.join(info.band_names)}), {info.dtype.name}, "
            f"nodata {info.nodata}",
        ]
        if self.warped:
            out.append(
                f"resampling: {self.warped} of {self.n_time} timesteps are warped onto the grid"
            )
        else:
            out.append("resampling: none needed, every source is on the target grid")
        out.append(
            f"raw size:   {self.raw_bytes / 1e9:.2f} GB "
            f"({self.timestep_bytes / 1e6:.0f} MB per timestep; memory about "
            f"{(1 + read_ahead) * self.timestep_bytes / 1e6:.0f} MB while staging)"
        )
        if self.est_output_bytes is not None and self.sample_ratio is not None:
            out.append(
                f"output:     about {self.est_output_bytes / 1e9:.2f} GB in "
                f"{self.n_levels} levels "
                f"(zstd 5 ratio {self.sample_ratio:.2f} on {len(self.samples) or SAMPLE_TIMESTEPS}"
                " sampled cells, before any star-delta saving)"
            )
        if self.sample_read_s is not None:
            out.append(
                f"time:       read about {self.sample_read_s:.2f} s per timestep "
                f"({_duration(self.sample_read_s * self.n_time)} sequential, less with "
                f"--read-ahead), encode about {_duration(self.est_encode_s)}"
            )
        return out


def _sample_ratio(samples: Sequence[np.ndarray], chunk_size: int) -> float:
    """Mean zstd-5 compressed/raw ratio of the top-left cell of each sampled timestep."""
    codec = Zstd(level=5)
    ratios = []
    for step in samples:
        window = np.ascontiguousarray(step[:, :chunk_size, :chunk_size])
        ratios.append(len(codec.encode(window)) / window.nbytes)
    return float(np.mean(ratios))


def _sample_indices(n: int) -> list[int]:
    return sorted({0, n // 2, n - 1})[:SAMPLE_TIMESTEPS]


def plan_conversion(
    source_path: str | Path,
    *,
    variable: str | None = None,
    dims: str | None = None,
    crs: str | None = None,
    transform: Sequence[float] | None = None,
    shape: tuple[int, int] | None = None,
    resampling: str | None = None,
    nodata: float | int | str | None = "auto",
    chunk_size: int = 512,
    n_lods: int | None = None,
    sample: bool = True,
) -> Plan:
    """Open the source, check it is consistent and estimate the conversion.

    Reads `SAMPLE_TIMESTEPS` timesteps (when `sample`) to measure read time and compression.
    """
    text = str(source_path)
    suffix = Path(text).suffix.lower()
    is_manifest = suffix in (".csv", ".json")
    source: Source
    if is_manifest:
        if variable is not None or dims is not None:
            raise ValueError("--variable and --dims apply to Zarr and NetCDF input, not manifests")
        entries, names = read_manifest(Path(text))
        source = CogManifestSource(
            entries,
            names,
            target_crs=crs,
            target_transform=None
            if transform is None
            else _check_north_up(transform, "--transform"),
            target_shape=shape,
            resampling=resampling,
            nodata=nodata,
            chunk_size=chunk_size,
        )
    else:
        if transform is not None or shape is not None or resampling is not None:
            raise ValueError(
                "--transform, --shape and --resampling apply to COG manifests; a Zarr or NetCDF "
                "input keeps the grid of its x/y coordinates (--crs only declares its CRS)"
            )
        source = XarraySource(text, variable, _parse_dims(dims), crs, nodata)

    info = source.info
    n_time = len(source.times)
    timestep_bytes = info.n_band * info.grid.height * info.grid.width * info.dtype.itemsize
    shapes = schema.level_shapes(info.grid.height, info.grid.width, chunk_size, n_lods)
    pyramid = sum(h * w for h, w in shapes) / (info.grid.height * info.grid.width)
    plan = Plan(
        source=source,
        n_time=n_time,
        timestep_bytes=timestep_bytes,
        raw_bytes=timestep_bytes * n_time,
        n_levels=len(shapes),
        sample_ratio=None,
        est_output_bytes=None,
        sample_read_s=None,
        est_encode_s=timestep_bytes * n_time / ENCODE_BYTES_PER_S,
        warped=len(source.warped),
    )
    if sample:
        seconds = []
        for t in _sample_indices(n_time):
            started = time.perf_counter()
            plan.samples[t] = _read_checked(source, t)
            seconds.append(time.perf_counter() - started)
        plan.sample_read_s = float(np.mean(seconds))
        plan.sample_ratio = _sample_ratio(list(plan.samples.values()), chunk_size)
        plan.est_output_bytes = int(plan.raw_bytes * plan.sample_ratio * pyramid)
    return plan


def _read_checked(source: Source, t: int) -> np.ndarray:
    info = source.info
    step = source.read(t)
    expected = (info.n_band, info.grid.height, info.grid.width)
    if step.shape != expected or step.dtype != info.dtype:
        raise ValueError(
            f"timestep {t} came back as {step.dtype}{step.shape}; expected {info.dtype}{expected}"
        )
    return step


# --- Staging and conversion ---------------------------------------------------------------


@dataclass(frozen=True)
class ConvertReport:
    plan: Plan
    encode: EncodeReport | None  # None for a dry run
    n_staged: int  # timesteps read from the source in this run
    n_reused: int  # timesteps found already staged (resume)
    read_s: float
    encode_s: float

    @property
    def total_s(self) -> float:
        return self.read_s + self.encode_s


def _staged_path(work: Path, t: int) -> Path:
    return work / f"t{t:06d}.npy"


def _is_staged(path: Path, plan: Plan) -> bool:
    if not path.is_file():
        return False
    info = plan.source.info
    try:
        staged = np.load(path, mmap_mode="r")
    except (ValueError, OSError):
        return False
    return staged.shape == (info.n_band, info.grid.height, info.grid.width) and (
        staged.dtype == info.dtype
    )


def _check_work_dir(work: Path, plan: Plan, resume: bool) -> None:
    plan_file = work / "plan.json"
    fingerprint = json.dumps(plan.source.fingerprint(), sort_keys=True)
    digest = hashlib.sha256(fingerprint.encode()).hexdigest()
    if work.exists() and any(work.iterdir()):
        if not resume:
            raise FileExistsError(
                f"{work} holds timesteps staged by an earlier run. Pass --resume to reuse them, "
                "or delete the directory to start over"
            )
        recorded = json.loads(plan_file.read_text()) if plan_file.is_file() else {}
        if recorded.get("sha256") != digest:
            raise ValueError(
                f"{work} was staged for a different input or options (plan hash "
                f"{str(recorded.get('sha256'))[:12]} vs {digest[:12]}); delete it or rerun "
                "without --resume in a fresh work directory"
            )
    work.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(json.dumps({"sha256": digest, "plan": json.loads(fingerprint)}))


def _stage(
    plan: Plan,
    work: Path,
    read_ahead: int,
    progress: Callable[[int, int], None] | None,
) -> tuple[int, int]:
    """Read every missing timestep into `work`. Returns (staged, reused)."""
    todo = [t for t in range(plan.n_time) if not _is_staged(_staged_path(work, t), plan)]
    reused = plan.n_time - len(todo)
    if progress is not None:
        progress(reused, plan.n_time)

    def read_one(t: int) -> np.ndarray:
        if t in plan.samples:
            return plan.samples.pop(t)
        return _read_checked(plan.source, t)

    done = reused
    with ThreadPoolExecutor(max_workers=read_ahead) as pool:
        pending: deque[tuple[int, Future[np.ndarray]]] = deque()
        queue = iter(todo)
        for t in queue:
            pending.append((t, pool.submit(read_one, t)))
            if len(pending) >= read_ahead:
                break
        while pending:
            t, future = pending.popleft()
            step = future.result()
            target = _staged_path(work, t)
            temporary = target.with_suffix(".npy.part")
            with temporary.open("wb") as handle:
                np.save(handle, step)
            temporary.replace(target)
            del step
            done += 1
            if progress is not None:
                progress(done, plan.n_time)
            following = next(queue, None)
            if following is not None:
                pending.append((following, pool.submit(read_one, following)))
    return len(todo), reused


def _staged_timesteps(plan: Plan, work: Path) -> Iterator[np.ndarray]:
    for t in range(plan.n_time):
        yield np.load(_staged_path(work, t))


def convert(
    source: str | Path,
    out: str | Path,
    *,
    variable: str | None = None,
    dims: str | None = None,
    crs: str | None = None,
    transform: Sequence[float] | None = None,
    shape: tuple[int, int] | None = None,
    resampling: str | None = None,
    nodata: float | int | str | None = "auto",
    work_dir: str | Path | None = None,
    resume: bool = False,
    dry_run: bool = False,
    read_ahead: int = 2,
    on_plan: Callable[[Plan], None] | None = None,
    progress: Callable[[int, int], None] | None = None,
    **encode_options: Any,
) -> ConvertReport:
    """Convert `source` into a chronozarr store at `out` without holding the whole stack.

    `source` is a manifest (`.csv`/`.json`), a Zarr store (path or URL) or a NetCDF file; see the
    module docstring. The grid comes from the first source (manifests) or the x/y coordinates
    (Zarr, NetCDF) unless `crs`, `transform` and `shape` override it for a manifest, in which
    case sources off the grid are warped with the explicit `resampling`. `nodata` is "auto"
    (the source's value, else `encode`'s default), a number, or None.

    `on_plan` receives the `Plan` (sizes, estimates) before any data is staged; `dry_run` stops
    there. Timesteps are staged under `work_dir` (default `<out>.convert-work` beside `out`):
    it is removed on success and kept on failure, and `resume=True` reuses its timesteps.
    `encode_options` go to `chronozarr.encode`: chunk_size, encoding, codec, level,
    anchor_interval, shard, shard_time, n_lods, workers, provenance.
    """
    if read_ahead < 1:
        raise ValueError(f"read_ahead must be >= 1, got {read_ahead}")
    out_path = Path(out)
    if not dry_run and out_path.exists() and any(out_path.iterdir()):
        raise FileExistsError(
            f"{out_path} already exists and is not empty; stores are immutable, write to a "
            "new path"
        )
    plan = plan_conversion(
        source,
        variable=variable,
        dims=dims,
        crs=crs,
        transform=transform,
        shape=shape,
        resampling=resampling,
        nodata=nodata,
        chunk_size=encode_options.get("chunk_size", 512),
        n_lods=encode_options.get("n_lods"),
    )
    if on_plan is not None:
        on_plan(plan)
    if dry_run:
        return ConvertReport(plan, None, 0, 0, 0.0, 0.0)

    work = (
        Path(work_dir)
        if work_dir is not None
        else out_path.parent / f"{out_path.name}.convert-work"
    )
    _check_work_dir(work, plan, resume)
    info = plan.source.info
    encode_nodata = "default" if info.nodata is None and nodata == "auto" else info.nodata
    try:
        started = time.perf_counter()
        staged, reused = _stage(plan, work, read_ahead, progress)
        read_s = time.perf_counter() - started

        started = time.perf_counter()
        report = encode(
            _staged_timesteps(plan, work),
            out_path,
            times=np.array(plan.source.times, dtype="datetime64[ms]"),
            bands=list(info.bands),
            crs=info.grid.crs,
            transform=info.grid.transform,
            nodata=encode_nodata,
            **encode_options,
        )
        encode_s = time.perf_counter() - started
    except BaseException as exc:
        exc.add_note(f"staged timesteps are kept in {work}; rerun with --resume to reuse them")
        raise
    shutil.rmtree(work)
    return ConvertReport(plan, report, staged, reused, read_s, encode_s)
