"""Zarr and NetCDF conversion adapter through xarray.

One Zarr store or NetCDF file is one source. The NetCDF files of a series (one file per day,
month or decade) are one source too: `XarraySource(files=...)` checks every file against the
first, collects what differs per file, and reads each timestep from the file that holds it.
"""

from __future__ import annotations

import importlib.util
import threading
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import xarray as xr

from chronozarr import schema
from chronozarr._calendar import (
    CalendarMapping,
    MissingCftimeError,
    calendar_name,
    check_rule,
    explain_missing_cftime,
    is_cftime,
    to_gregorian,
)
from chronozarr._convert_preflight import PreflightError, Problem
from chronozarr._convert_source import (
    Grid,
    Source,
    SourceInfo,
    Step,
    _crs_string,
    _decide_validity,
    _declared_nodata,
    _same_crs,
    _scaling,
    _settle_nan,
    _value_valid,
)
from chronozarr._install import extra_hint
from chronozarr.crs import wkt2_from_cf
from chronozarr.schema import Band
from chronozarr.store import as_store, redact_url

NETCDF_SUFFIXES = (".nc", ".nc4", ".cdf")
_HDF5_SIGNATURE = b"\x89HDF\r\n\x1a\n"


def _is_installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _is_hdf5_file(path: str | Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(_HDF5_SIGNATURE)) == _HDF5_SIGNATURE
    except OSError:
        return False


def open_netcdf(path: str | Path, **kwargs: Any) -> xr.Dataset:
    """`xr.open_dataset` for a NetCDF file; the error names the extra when no reader is installed.

    A NetCDF4 file is an HDF5 file. It needs netCDF4, or h5netcdf with h5py, which the `netcdf`
    extra installs. xarray's own errors for their absence ("found the following matches ... their
    dependencies may not be installed", or h5netcdf's ImportError for h5py) name no chronozarr
    extra. A time coordinate on a calendar that needs cftime (360_day, noleap, ...) names the
    `cftime` extra. Every other failure keeps the error of xarray, including a file that is not
    HDF5: a NetCDF3 file needs scipy or netCDF4, which the extra does not provide.
    """
    try:
        return xr.open_dataset(path, **kwargs)
    except (ValueError, ImportError) as exc:
        if isinstance(exc, ValueError):
            undecoded: dict[str, Any] = {**kwargs, "decode_times": False}
            explain_missing_cftime(exc, lambda: xr.open_dataset(path, **undecoded), str(path))
        has_reader = _is_installed("netCDF4") or (
            _is_installed("h5netcdf") and _is_installed("h5py")
        )
        # With a reader installed, the cause is another one, such as a calendar that needs cftime.
        if has_reader or not _is_hdf5_file(path):
            raise
        raise ValueError(
            f"{path}: reading a NetCDF4 file needs h5netcdf and h5py: {extra_hint('netcdf')}"
        ) from exc


_DIM_ALIASES = {
    "time": ("time", "t", "datetime", "valid_time"),
    "band": ("band", "bands", "channel", "variable"),
    "y": ("y", "lat", "latitude", "northing"),
    "x": ("x", "lon", "longitude", "easting"),
}


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


def open_zarr(source: str | Path, **kwargs: Any) -> xr.Dataset:
    """`xr.open_zarr` for a path or URL; a time coordinate that needs cftime names the extra."""
    store = as_store(source)
    try:
        return xr.open_zarr(store, **kwargs)
    except ValueError as exc:
        undecoded: dict[str, Any] = {**kwargs, "decode_times": False}
        explain_missing_cftime(
            exc, lambda: xr.open_zarr(store, **undecoded), redact_url(str(source))
        )
        raise


def _open_dataset(source: str) -> xr.Dataset:
    if source.lower().endswith(NETCDF_SUFFIXES):
        if not Path(source).is_file():
            raise FileNotFoundError(f"NetCDF file {source} does not exist")
        return open_netcdf(source, mask_and_scale=False)
    if not source.startswith(("http://", "https://")) and not Path(source).exists():
        raise FileNotFoundError(f"input {source} does not exist")
    try:
        return open_zarr(source, chunks=None, consolidated=True, mask_and_scale=False)
    except MissingCftimeError:
        raise
    except (KeyError, ValueError):
        if source.startswith(("http://", "https://")):
            raise ValueError(
                f"{source}: a remote Zarr input needs consolidated metadata (.zmetadata or "
                "consolidated zarr.json) because HTTP stores cannot be listed"
            ) from None
    # Local store without consolidated metadata: xarray falls back to listing and warns.
    return open_zarr(source, chunks=None, consolidated=False, mask_and_scale=False)


def _first_present(names: Sequence[Any], aliases: Sequence[str]) -> str | None:
    lowered = {str(n).lower(): str(n) for n in names}
    return next((lowered[a] for a in aliases if a in lowered), None)


def _show_value(value: Any) -> str:
    """A coordinate value as text, without float noise (a float32 0.1 is 0.1)."""
    return f"{value:.8g}" if isinstance(value, np.floating) else str(value)


def _show_time(moment: np.datetime64) -> str:
    midnight = moment == moment.astype("datetime64[D]")
    return str(np.datetime_as_string(moment, unit="D" if midnight else "s"))


def _squeeze_note(da: xr.DataArray, dropped: Sequence[str]) -> str:
    """The plan line for the singleton dimensions of `da` that are dropped, with their values."""
    parts = []
    for name in dropped:
        detail = "length 1"
        if name in da.coords:
            units = da[name].attrs.get("units")
            detail += f", value {_show_value(da[name].values.ravel()[0])}"
            detail += f" {units}" if units else ""
        parts.append(f"{name} ({detail})")
    return "squeezed:   " + ", ".join(parts)


def _unexpected_dims(variable: str, kept: Sequence[str], sizes: Any, has_band: bool) -> str:
    listed = ", ".join(f"{d!r} (length {sizes[d]})" for d in kept)
    select = ", ".join(f"{d}=0" for d in kept)
    if len(kept) == 1 and not has_band:
        return (
            f"{variable} has a dimension {listed} besides time, y and x. Pass --dims "
            f"band={kept[0]} to store its values as bands, or select one value first (xarray: "
            f"ds.isel({select})); only a dimension of length 1 is squeezed for you"
        )
    return (
        f"{variable} has the dimensions {listed} besides time, y, x and the band dimension. "
        f"Select one value of each first (xarray: ds.isel({select})); only a dimension of "
        "length 1 is squeezed for you"
    )


def _resolve_dims(
    da: xr.DataArray, dims: dict[str, str], variable: str
) -> tuple[dict[str, str], tuple[str, ...]]:
    """The dimensions of `da` that play time, y, x and band, and the length-1 ones left over.

    A dimension with none of those roles must have length 1 (OISST's `zlev`, a `height` of one):
    it is squeezed. A longer one needs `--dims band=NAME` or a selection first.
    """
    resolved: dict[str, str] = {}
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
    extra = [str(d) for d in da.dims if d not in resolved.values()]
    squeezed = tuple(d for d in extra if da.sizes[d] == 1)
    kept = [d for d in extra if d not in squeezed]
    if kept:
        raise ValueError(_unexpected_dims(variable, kept, da.sizes, "band" in resolved))
    return resolved, squeezed


def _time_values(da: xr.DataArray, name: str) -> tuple[np.ndarray, str | None]:
    """The time coordinate as numpy values, and its calendar name when they are cftime dates."""
    values = np.asarray(da[name].values)
    if is_cftime(values):
        return values, calendar_name(da[name])
    if not np.issubdtype(values.dtype, np.datetime64):
        raise ValueError(
            f"the time coordinate {name!r} is {values.dtype}, not datetimes; "
            "decode it (CF units) or convert it to datetime64"
        )
    return values, None


def _scalar_attr(da: xr.DataArray, key: str, default: float) -> float:
    raw = da.attrs.get(key)
    if raw is None:
        return default
    values = np.atleast_1d(raw)
    if values.size != 1 or values.dtype.kind not in "iuf":
        raise ValueError(
            f"{da.name}: {key} must be one number (CF attributes are per variable), got {raw!r}"
        )
    return float(values[0])


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
    mapping = da.attrs.get("grid_mapping", da.encoding.get("grid_mapping"))
    if isinstance(mapping, str) and mapping in dataset.variables:
        return wkt2_from_cf(dataset[mapping].attrs, mapping)
    return None


@dataclass(frozen=True, eq=False)
class _Facts:
    """What one file says about the variable. The files of a series agree on all of it."""

    dims: tuple[str, ...]
    x: np.ndarray
    y: np.ndarray
    dtype: np.dtype
    scale: float
    offset: float
    units: str | None
    declared: frozenset[float | int | str]  # nodata tokens of _FillValue and missing_value
    crs: str | None
    bands: tuple[str, ...]
    calendar: str | None  # a cftime calendar's name; None for dates that numpy holds


@dataclass(frozen=True, eq=False)
class _Inspected:
    da: xr.DataArray  # the variable, singleton dimensions squeezed
    dims: dict[str, str]  # the file's dimension for time, y, x and maybe band
    squeezed: tuple[str, ...]
    squeeze_note: str | None
    facts: _Facts
    times: np.ndarray  # datetime64, or cftime dates when `facts.calendar` is set


def _inspect(dataset: xr.Dataset, variable: str, dims: dict[str, str]) -> _Inspected:
    """What `dataset` says about `variable`; ValueError when it cannot be a step of a store."""
    raw = dataset[variable]
    resolved, squeezed = _resolve_dims(raw, dims, variable)
    if raw.dtype.name not in schema.DTYPES:
        raise ValueError(
            f"{variable} is {raw.dtype}; chronozarr stores hold {list(schema.DTYPES)}. "
            "Cast it first"
        )
    da = raw.isel({d: 0 for d in squeezed})
    times, calendar = _time_values(da, resolved["time"])
    units = da.attrs.get("units")
    if "band" in resolved:
        bands = tuple(str(b) for b in da[resolved["band"]].values)
    else:
        bands = (variable,)
    facts = _Facts(
        dims=tuple(str(d) for d in raw.dims),
        x=np.asarray(da[resolved["x"]].values, dtype=np.float64),
        y=np.asarray(da[resolved["y"]].values, dtype=np.float64),
        dtype=da.dtype,
        scale=_scalar_attr(da, "scale_factor", 1.0),
        offset=_scalar_attr(da, "add_offset", 0.0),
        units=str(units) if units else None,
        declared=frozenset(
            token
            for key in ("_FillValue", "missing_value")
            for value in np.atleast_1d(da.attrs.get(key, [])).tolist()
            if (token := _declared_nodata(value, da.dtype)) is not None
        ),
        crs=_crs_from_attrs(da, dataset),
        bands=bands,
        calendar=calendar,
    )
    note = _squeeze_note(raw, squeezed) if squeezed else None
    return _Inspected(da, resolved, squeezed, note, facts, times)


_FIX_DROP = "leave the file out of the folder or glob, or convert it in a store of its own"


def _same_axis(found: np.ndarray, wanted: np.ndarray) -> bool:
    step = abs(float(wanted[1] - wanted[0])) if len(wanted) > 1 else 1.0
    return found.shape == wanted.shape and bool(
        np.allclose(found, wanted, rtol=0.0, atol=1e-6 * step)
    )


def _axis_difference(name: str, found: np.ndarray, wanted: np.ndarray) -> str | None:
    if found.shape != wanted.shape:
        return f"{name} has {len(found)} values, not {len(wanted)}"
    if not _same_axis(found, wanted):
        return (
            f"{name} runs {found[0]:.10g} to {found[-1]:.10g}, not "
            f"{wanted[0]:.10g} to {wanted[-1]:.10g}"
        )
    return None


def _nodata_text(tokens: frozenset[float | int | str]) -> str:
    return ", ".join(sorted(str(t) for t in tokens)) if tokens else "none"


def _differences(
    found: _Facts, ref: _Facts, first: str, *, check_crs: bool, check_nodata: bool
) -> list[tuple[str, str | None]]:
    """How a file's facts differ from the first file's, as (what, fix) pairs."""
    if found.dims != ref.dims:
        return [
            (
                f"has dimensions {list(found.dims)}; {first} has {list(ref.dims)}. Every file "
                "of a series lays the variable out the same way",
                _FIX_DROP,
            )
        ]
    out: list[tuple[str, str | None]] = []
    axes = [
        d
        for d in (_axis_difference("x", found.x, ref.x), _axis_difference("y", found.y, ref.y))
        if d
    ]
    if axes:
        out.append(
            (
                f"is on another grid than {first}: {'; '.join(axes)}. A series is one grid",
                "regrid the file to the grid of the first (for example with xarray) or "
                + _FIX_DROP,
            )
        )
    if found.dtype != ref.dtype:
        out.append(
            (f"is {found.dtype}; {first} is {ref.dtype}", "cast one of them, or " + _FIX_DROP)
        )
    if (found.scale, found.offset) != (ref.scale, ref.offset):
        out.append(
            (
                f"has scale_factor {found.scale:g} and add_offset {found.offset:g}; {first} has "
                f"{ref.scale:g} and {ref.offset:g}. A store holds one value per band for every "
                "timestep, so the files must agree",
                "if the attributes are wrong, correct them; otherwise convert the files that "
                "share them in separate stores",
            )
        )
    if found.units != ref.units:
        out.append((f"has units {found.units!r}; {first} has {ref.units!r}", _FIX_DROP))
    if check_nodata and found.declared != ref.declared:
        out.append(
            (
                f"declares nodata {_nodata_text(found.declared)}; {first} declares "
                f"{_nodata_text(ref.declared)}",
                "if one of them is wrong, correct the attribute; `--nodata N` replaces what the "
                "files declare, and the declared values then stay data",
            )
        )
    if found.bands != ref.bands:
        out.append((f"has bands {list(found.bands)}; {first} has {list(ref.bands)}", _FIX_DROP))
    if found.calendar != ref.calendar:
        out.append(
            (
                f"uses the {found.calendar or 'Gregorian'} calendar; {first} uses "
                f"{ref.calendar or 'Gregorian'}. A store has one calendar",
                _FIX_DROP,
            )
        )
    if check_crs and found.crs != ref.crs and not _crs_agree(found.crs, ref.crs):
        out.append(
            (
                f"declares the CRS {_crs_text(found.crs)}; {first} declares {_crs_text(ref.crs)}",
                "pass --crs to name the CRS of all of them, or " + _FIX_DROP,
            )
        )
    return out


def _crs_text(text: str | None) -> str:
    if text is None:
        return "none"
    return text if len(text) <= 40 else f"{text[:37]}..."


def _crs_agree(found: str | None, wanted: str | None) -> bool:
    return found is not None and wanted is not None and _same_crs(found, wanted)


def _time_problems(
    files: Sequence[str], sizes: Sequence[int], stored: np.ndarray
) -> list[Problem]:
    """Files that have no steps, repeat a time, or share times with another file.

    `stored` is the time of every step of every file in turn, as datetime64[ms].
    """
    problems: list[Problem] = []
    spans: list[tuple[np.datetime64, np.datetime64, int]] = []
    start = 0
    for k, (path, size) in enumerate(zip(files, sizes, strict=True)):
        part = stored[start : start + size]
        start += size
        if size == 0:
            problems.append(Problem(path, "has no time steps", _FIX_DROP))
            continue
        if len(np.unique(part)) < size:
            problems.append(
                Problem(path, "its time coordinate has duplicate values", "remove the repeats")
            )
        spans.append((part.min(), part.max(), k))
    spans.sort()
    span_of = {
        k: _show_time(a) if a == b else f"{_show_time(a)} .. {_show_time(b)}" for a, b, k in spans
    }
    latest: tuple[np.datetime64, int] | None = None
    for first, last, k in spans:
        if latest is not None and first <= latest[0]:
            other = latest[1]
            problems.append(
                Problem(
                    files[k],
                    f"has times {span_of[k]} that overlap those of {files[other]} "
                    f"({span_of[other]}). Each time must come from one file",
                    "drop one of the two files, or narrow the glob so every time is in one file",
                )
            )
        if latest is None or last > latest[0]:
            latest = (last, k)
    return problems


@dataclass
class _OpenFile:
    dataset: xr.Dataset
    users: int = 0


class _OpenFiles:
    """The most recently used files of a series, open for reading and shared by the reader threads.

    Opening a NetCDF file takes about as long as reading a step from it (22 ms against 9 ms for
    an OISST day), and the steps of one file are read one after another. A few files stay open
    and the least recently used one is closed when another is needed; a file in use is never
    closed.
    """

    def __init__(self, files: Sequence[str], limit: int = 4) -> None:
        self._files = files
        self._limit = limit
        self._lock = threading.Lock()
        self._open: OrderedDict[int, _OpenFile] = OrderedDict()

    @contextmanager
    def use(self, k: int) -> Iterator[xr.Dataset]:
        with self._lock:
            entry = self._open.get(k)
            if entry is None:
                entry = self._open[k] = _OpenFile(_open_dataset(self._files[k]))
            self._open.move_to_end(k)
            entry.users += 1
            self._trim()
        try:
            yield entry.dataset
        finally:
            with self._lock:
                entry.users -= 1
                self._trim()

    def _trim(self) -> None:
        for k in list(self._open):
            if len(self._open) <= self._limit:
                break
            if self._open[k].users == 0:
                self._open.pop(k).dataset.close()


class XarraySource(Source):
    def __init__(
        self,
        source: str,
        variable: str | None,
        dims: dict[str, str],
        crs: str | None,
        nodata: float | int | None | str,
        mask_var: str | None = None,
        calendar_to_gregorian: str | None = None,
        files: Sequence[str] | None = None,
    ) -> None:
        """`source` is a NetCDF file or a Zarr store; `files` instead is a series of NetCDF
        files, found by `find_netcdf_files` for the folder or glob `source`."""
        check_rule(calendar_to_gregorian)
        self.files = tuple(files) if files is not None else (source,)
        self.source = source
        if files is not None:
            self.kind = "NetCDF files"
        else:
            self.kind = "NetCDF file" if source.lower().endswith(NETCDF_SUFFIXES) else "Zarr store"
        self._dataset: xr.Dataset | None = None
        self._open = _OpenFiles(self.files)
        dataset = _open_dataset(self.files[0])
        try:
            self._configure(dataset, variable, dims, crs, nodata, mask_var, calendar_to_gregorian)
        except BaseException:
            dataset.close()
            raise
        if files is None:
            self._dataset = dataset  # one file stays open, lazily, for every read
        else:
            dataset.close()  # a series opens the files it reads from as it goes

    def _configure(
        self,
        dataset: xr.Dataset,
        variable: str | None,
        dims: dict[str, str],
        crs: str | None,
        nodata: float | int | None | str,
        mask_var: str | None,
        calendar_to_gregorian: str | None,
    ) -> None:
        first = self.files[0]
        names = [n for n in dataset.data_vars if n != mask_var]
        if variable is None:
            if len(names) != 1:
                raise ValueError(f"{first} has variables {names}; choose one with --variable")
            variable = str(names[0])
        if variable not in dataset.data_vars:
            raise ValueError(f"variable {variable!r} is not in {first}: {names}")
        self.variable = variable
        self._dims_arg = dims
        ref = _inspect(dataset, variable, dims)
        facts = ref.facts
        resolved = self._dims = ref.dims
        self._squeezed = ref.squeezed

        parts = (
            self._check_series(ref, crs, nodata, mask_var) if len(self.files) > 1 else [ref.times]
        )
        self.notes = (
            *self._arrange_times(parts, ref, calendar_to_gregorian),
            *([] if ref.squeeze_note is None else [ref.squeeze_note]),
        )

        x, y = facts.x, facts.y
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
        crs_text = crs or facts.crs
        if crs_text is None:
            raise ValueError(
                f"{first} does not declare a CRS; pass --crs with an EPSG code or WKT2 text"
            )
        from rasterio.crs import CRS  # ty: ignore[unresolved-import]  # compiled, no stubs

        grid = Grid(_crs_string(CRS.from_user_input(crs_text)), transform, len(y), len(x))

        self._mask_name = mask_var
        if mask_var is not None:
            self._mask_variable(dataset, mask_var, variable, resolved, first)
        validity = _decide_validity(
            ref.da.dtype,
            facts.declared,
            override=nodata,
            explicit=[] if mask_var is None else [f"mask variable {mask_var}"],
            footprint=None,
        )
        self._sentinels: tuple[float | int, ...]
        if nodata == "auto":
            self._sentinels = tuple(t for t in facts.declared if not isinstance(t, str))
        else:
            self._sentinels = () if validity.nodata is None else (validity.nodata,)

        scale, offset = _scaling(facts.scale, facts.offset, f"variable {variable!r}")
        bands = tuple(
            Band(name=n, scale=scale, offset=offset, units=facts.units) for n in facts.bands
        )
        self.info = SourceInfo(
            grid,
            len(facts.bands),
            ref.da.dtype,
            validity.nodata,
            facts.bands,
            bands,
            mask=validity.mask,
            validity=validity.why,
        )

    def _arrange_times(
        self, parts: list[np.ndarray], ref: _Inspected, calendar_to_gregorian: str | None
    ) -> list[str]:
        """Join the time axes of the files, put every step in time order and say where each is.

        Sets `times` (sorted) and, for each step, its file and its index in that file. Refuses
        files that overlap in time. Returns the plan lines for a calendar that was mapped.
        """
        raw_times = np.concatenate(parts)
        notes: list[str] = []
        self.calendar: CalendarMapping | None = None
        if ref.facts.calendar is not None:
            self.calendar = to_gregorian(
                raw_times, ref.facts.calendar, calendar_to_gregorian, name=ref.dims["time"]
            )
            raw_times = self.calendar.stored
            notes.append(self.calendar.plan_line())
        stored = raw_times.astype("datetime64[ms]")
        sizes = [len(p) for p in parts]
        if len(self.files) > 1:
            if problems := _time_problems(self.files, sizes, stored):
                raise PreflightError(problems, len(self.files))
        elif len(stored) > 1 and len(np.unique(stored)) < len(stored):
            raise ValueError(f"the time coordinate of {self.variable} has duplicate values")
        order = np.argsort(stored, kind="stable")
        self._file_of = np.repeat(np.arange(len(parts)), sizes)[order]
        self._index_in_file = np.concatenate([np.arange(n) for n in sizes])[order]
        self.times = list(stored[order])
        offsets = np.cumsum([0, *sizes])
        self._spans = sorted(
            (
                stored[offsets[k] : offsets[k + 1]].min(),
                stored[offsets[k] : offsets[k + 1]].max(),
                n,
                path,
            )
            for k, (n, path) in enumerate(zip(sizes, self.files, strict=True))
            if n
        )
        return notes

    def _check_series(
        self,
        ref: _Inspected,
        crs: str | None,
        nodata: float | int | None | str,
        mask_var: str | None,
    ) -> list[np.ndarray]:
        """The time axis of every file, after checking all files against the first.

        Raises one PreflightError with everything that differs, per file, and with the files
        that share times (dates numpy holds; a cftime axis is compared once mapped).
        """
        times = {self.files[0]: ref.times}
        problems: list[Problem] = []
        for path in self.files[1:]:
            found, differences = self._check_member(path, ref, crs, nodata, mask_var)
            problems += differences
            if found is not None:
                times[path] = found
        if ref.facts.calendar is None:
            stored = np.concatenate(list(times.values())).astype("datetime64[ms]")
            problems += _time_problems(list(times), [len(t) for t in times.values()], stored)
        if problems:
            position = {path: i for i, path in enumerate(self.files)}
            problems.sort(key=lambda problem: position[problem.uri])
            raise PreflightError(problems, len(self.files))
        return list(times.values())

    def _check_member(
        self,
        path: str,
        ref: _Inspected,
        crs: str | None,
        nodata: float | int | None | str,
        mask_var: str | None,
    ) -> tuple[np.ndarray | None, list[Problem]]:
        """The times of another file of the series, and what differs from the first file."""
        try:
            with _open_dataset(path) as dataset:
                if self.variable not in dataset.data_vars:
                    names = [str(n) for n in dataset.data_vars if n != mask_var]
                    return None, [
                        Problem(
                            path,
                            f"has no variable {self.variable!r}; it has {names}",
                            "every file of a series holds the variable: " + _FIX_DROP,
                        )
                    ]
                found = _inspect(dataset, self.variable, self._dims_arg)
                problems = [
                    Problem(path, what, fix)
                    for what, fix in _differences(
                        found.facts,
                        ref.facts,
                        self.files[0],
                        check_crs=crs is None,
                        check_nodata=nodata == "auto",
                    )
                ]
                if mask_var is not None:
                    self._mask_variable(dataset, mask_var, self.variable, ref.dims, path)
                return found.times, problems
        except (ValueError, OSError) as exc:
            return None, [Problem(path, str(exc))]

    def _mask_variable(
        self, dataset: xr.Dataset, name: str, variable: str, resolved: dict[str, str], where: str
    ) -> xr.DataArray:
        if name not in dataset.variables:
            raise ValueError(
                f"mask variable {name!r} is not in {where}: "
                f"{sorted(str(v) for v in dataset.variables)}"
            )
        if name == variable:
            raise ValueError(f"mask variable {name!r} is the data variable; name another")
        mask = dataset[name]
        wanted = {resolved[k] for k in ("time", "y", "x")}
        mask = mask.isel({d: 0 for d in mask.dims if d not in wanted and mask.sizes[d] == 1})
        if len(mask.dims) != 3 or set(mask.dims) != wanted:
            raise ValueError(
                f"mask variable {name!r} has dims {list(mask.dims)}; it must have exactly "
                f"{sorted(wanted)} like {variable} (one plane per timestep, shared by all bands)"
            )
        if mask.dtype.kind not in "biu":
            raise ValueError(
                f"mask variable {name!r} is {mask.dtype}; use a boolean or integer variable "
                "where nonzero means valid (invert a 'nonzero = bad' flag first)"
            )
        return mask

    def file_lines(self) -> list[str]:
        """The files of a series with their times, in time order, for the dry run."""
        lines = []
        for first, last, count, path in self._spans:
            when = (
                _show_time(first) if count == 1 else f"{_show_time(first)} .. {_show_time(last)}"
            )
            lines.append(f"  {when}  {path}")
        return lines

    def fingerprint(self) -> Any:
        grid = self.info.grid
        return {
            "kind": "xarray",
            "source": self.source,
            **({"files": list(self.files)} if len(self.files) > 1 else {}),
            "variable": self.variable,
            "grid": [grid.crs, list(grid.transform), grid.height, grid.width],
            "bands": [b.to_attrs() for b in self.info.bands],
            "times": [str(t) for t in self.times],
            "dtype": self.info.dtype.name,
            "nodata": self.info.nodata,
            "mask": self.info.mask,
            "validity": self.info.validity,
            "mask_var": self._mask_name,
        }

    @contextmanager
    def _arrays(self, k: int) -> Iterator[tuple[xr.DataArray, xr.DataArray | None]]:
        """The variable and the mask variable of file `k`, open while the caller reads them."""
        if self._dataset is not None:
            yield self._select(self._dataset, self.files[k])
            return
        with self._open.use(k) as dataset:
            yield self._select(dataset, self.files[k])

    def _select(self, dataset: xr.Dataset, where: str) -> tuple[xr.DataArray, xr.DataArray | None]:
        da = dataset[self.variable].isel({d: 0 for d in self._squeezed})
        if self._mask_name is None:
            return da, None
        return da, self._mask_variable(dataset, self._mask_name, self.variable, self._dims, where)

    def read(self, t: int) -> Step:
        d = self._dims
        k, i = int(self._file_of[t]), int(self._index_in_file[t])
        order = [d["band"], d["y"], d["x"]] if "band" in d else [d["y"], d["x"]]
        with self._arrays(k) as (da, mask):
            values = np.asarray(da.isel({d["time"]: i}).transpose(*order).values)
            plane = None
            if mask is not None:
                plane = np.asarray(mask.isel({d["time"]: i}).transpose(d["y"], d["x"]).values)
        if "band" not in d:
            values = values[np.newaxis]
        if self._flip_y:
            values = values[:, ::-1, :]
        values = np.ascontiguousarray(values)
        if not values.flags.writeable:
            values = values.copy()
        valid = None
        if self.info.mask:
            ok = _value_valid(values, self._sentinels)
            if plane is not None:
                ok &= (plane[::-1] if self._flip_y else plane) != 0
            valid = ok.astype(np.uint8)
        _settle_nan(values, valid, self.info.nodata, f"{self.files[k]} timestep {t}")
        return Step(values, valid)
