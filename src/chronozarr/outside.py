"""Stores that chronozarr did not write, read as a pyramid of (time, band, y, x) levels.

A store with no `chronozarr` block decodes when its root `multiscales` lists the levels and each
level says where it is on the ground. The levels come from a multiscales `layout` (object form)
or from ndpyramid's `datasets` list. A level is a group of arrays: one array of 4, 3 or 2
dimensions, or one array per band. The geometry is a `spatial:transform` on the array, the level
group, the layout entry or the root; without one, the x and y coordinate arrays give it. The CRS
is `proj:code`, `proj:wkt2` or `proj:projjson` the same way, else a `crs` on the layout entry,
else a CF grid mapping variable.

Whatever the store does not say becomes an assumption, and every assumption is a note on the
opened store: scale and offset 1 and 0, no nodata, one timestep dated 1970-01-01 when there is no
time axis. Values, geometry and validity are never guessed at: a level whose geometry, CRS, time
axis or dimension names cannot be read is refused with the reason.
"""

from __future__ import annotations

import base64
import math
import re
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import zarr
from xarray.coding.times import decode_cf_datetime

from chronozarr import schema
from chronozarr._calendar import is_cftime
from chronozarr.crs import Crs, same_crs, wkt2_from_cf
from chronozarr.level import Level, LevelData
from chronozarr.schema import Band, Chronozarr, Findings, SchemaError, Transform

TIME_NAMES = ("time", "t", "date", "datetime")
BAND_NAMES = ("band", "bands", "channel")
Y_NAMES = ("y", "lat", "latitude")
X_NAMES = ("x", "lon", "longitude")
LEADS = ((), ("time",), ("band",), ("time", "band"))
PLACEHOLDER_TIME = np.datetime64("1970-01-01T00:00:00", "ms")
_AUTHORITY_RE = re.compile(r'AUTHORITY\["([A-Za-z]+)","([^"]+)"\]\s*\]\s*$')
_DIGITS_RE = re.compile(r"(\d+)")
_PROJ_KEYS = ("proj:code", "proj:wkt2", "proj:projjson")


@dataclass(frozen=True)
class Outside:
    """A store with no chronozarr block, read as chronozarr reads its own."""

    attrs: Chronozarr
    times: np.ndarray
    levels: tuple[Level, ...]
    crs: Crs


@dataclass(frozen=True)
class _Arrays:
    """The data arrays of one level and what they hold."""

    arrays: dict[str, zarr.Array]  # by name, in band order
    axes: tuple[str, ...]  # time, band, y, x for each dimension of every array
    dims: tuple[str, ...]  # the names the store gives those dimensions

    @property
    def first(self) -> zarr.Array:
        return next(iter(self.arrays.values()))


def read_outside(group: zarr.Group, found: Findings) -> Outside:
    """Read the levels of `group` from its multiscales. Raises SchemaError when it cannot."""
    found.notes.insert(
        0,
        "no chronozarr block in the root attributes: read from the multiscales layout, the proj "
        "and spatial attributes and the arrays",
    )
    attrs = group.attrs.asdict()
    entries, resampling = _layout(attrs)
    levels = []
    for path, entry in entries:
        levels.append(_read_level(group, path, entry, attrs, found))
    order = sorted(range(len(levels)), key=lambda i: levels[i].resolution)
    if len({levels[i].resolution for i in order}) != len(levels):
        raise SchemaError("two levels of the multiscales layout have the same pixel size")
    if order != list(range(len(levels))):
        found.should(
            "multiscales",
            "the layout does not list the levels finest first (a reprojected pyramid runs coarse "
            f"to fine): level 0 here is {levels[order[0]].path}",
        )
    ordered = [
        _with_validity(level, found) for level in _common_bands([levels[i] for i in order], found)
    ]
    first = ordered[0]
    for other in ordered[1:]:
        _same_axes(first, other)
    _note_ladder(ordered, found)
    if len({level.nodata for level in ordered}) > 1:
        markers: dict[str, list[str]] = {}
        for level in ordered:
            markers.setdefault(_marker(level.nodata), []).append(level.path)
        found.should(
            "multiscales",
            "the levels mark missing pixels differently ("
            + "; ".join(f"{m} at {', '.join(paths)}" for m, paths in markers.items())
            + "): each level's own marker is used",
        )
    if len(ordered) > 1:
        declared = "not declared" if resampling is None else f"declared as {resampling!r}"
        found.should(
            "multiscales",
            f"the overview levels come from the writer's own resampling ({declared}), not from "
            "chronozarr's block means (spec 5)",
        )
    times = _read_times(ordered[0], found)
    for other in ordered[1:]:
        if not np.array_equal(_read_times(other, Findings()), times):
            raise SchemaError(f"levels {first.path} and {other.path} have different times")
    return _assemble(group, ordered, times, found)


# --- Levels -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Read:
    """A level before the levels are compared: its arrays, geometry and CRS."""

    path: str
    group: zarr.Group
    arrays: _Arrays
    transform: Transform
    crs: Crs
    nodata: int | float | None = None
    scales: tuple[tuple[float | None, float | None], ...] = ()  # per array

    @property
    def resolution(self) -> float:
        return self.transform[0]

    @property
    def first(self) -> zarr.Array:
        return self.arrays.first


def _layout(attrs: Mapping[str, Any]) -> tuple[list[tuple[str, Mapping[str, Any]]], Any]:
    """(path, layout entry) of each level in layout order, and the declared resampling method."""
    multiscales = attrs.get("multiscales")
    if isinstance(multiscales, list) and multiscales and isinstance(multiscales[0], Mapping):
        multiscales = multiscales[0]  # ndpyramid 0.4 writes a list of one multiscales object
    if isinstance(multiscales, Mapping):
        key, name = ("layout", "asset") if "layout" in multiscales else ("datasets", "path")
        raw = multiscales.get(key)
        if isinstance(raw, list) and raw:
            entries = []
            for entry in raw:
                if not isinstance(entry, Mapping) or not isinstance(entry.get(name), str):
                    raise SchemaError(f"multiscales.{key}: every entry needs a '{name}' path")
                entries.append((str(entry[name]), entry))
            method = multiscales.get("resampling_method")
            if method is None and isinstance(multiscales.get("metadata"), Mapping):
                kwargs = multiscales["metadata"].get("kwargs")
                method = kwargs.get("resampling") if isinstance(kwargs, Mapping) else None
            return entries, method
    raise SchemaError(
        "no chronozarr block and no multiscales layout in the root attributes: chronozarr reads a "
        "store that lists its levels with multiscales (a layout of assets, or ndpyramid's "
        "datasets)"
    )


def _read_level(
    root: zarr.Group,
    path: str,
    entry: Mapping[str, Any],
    attrs: Mapping[str, Any],
    found: Findings,
) -> _Read:
    where = f"level {path}"
    try:
        group = root if path in (".", "") else schema.get_group(root, path, "store")
    except SchemaError as exc:
        raise SchemaError(f"multiscales lists {path!r}, but {exc}") from exc
    arrays = _data_arrays(group, where, found)
    first = arrays.first
    if first.dtype.name not in schema.DTYPES:
        raise SchemaError(
            f"{where}: the data is {first.dtype}; chronozarr reads "
            f"{', '.join(schema.DTYPES[:-1])} or {schema.DTYPES[-1]}"
        )
    scopes = [first.attrs.asdict(), group.attrs.asdict(), dict(entry), dict(attrs)]
    transform = _transform(group, arrays, scopes, where, found)
    _north_up(transform, where)
    crs = _crs(group, first, scopes, entry, where, found)
    height, width = first.shape[-2], first.shape[-1]
    declared = _nearest(scopes, "spatial:shape")
    if declared is not None and list(declared) != [height, width]:
        raise SchemaError(
            f"{where}: spatial:shape {list(declared)} differs from the array shape "
            f"{[height, width]}"
        )
    return _Read(path, group, arrays, transform, crs)


def _data_arrays(group: zarr.Group, where: str, found: Findings) -> _Arrays:
    """The arrays that hold the values: the largest set that shares dimensions, shape and dtype."""
    try:
        members = sorted(group.arrays(), key=lambda item: _natural(item[0]))
    except NotImplementedError as exc:
        raise SchemaError(
            f"{where}: the server cannot list the arrays and the store has no consolidated "
            "metadata. Open a local copy, or write consolidated metadata"
        ) from exc
    spatial = _nearest([group.attrs.asdict()], "spatial:dimensions")
    candidates: dict[tuple, list[tuple[str, zarr.Array]]] = {}
    unnamed, skipped = [], []
    for name, array in members:
        if array.ndim < 2:
            continue
        dims = _dim_names(array)
        if dims is None:
            unnamed.append(name)
            continue
        axes = _axes(dims, spatial)
        if axes is None:
            skipped.append(name)
            continue
        candidates.setdefault((axes, dims, array.shape, array.dtype.str), []).append((name, array))
    if not candidates:
        reason = (
            f"{', '.join(unnamed)} have no dimension names, and chronozarr needs them to tell "
            "time, band, y and x apart"
            if unnamed
            else "no array has time, band, y and x dimensions"
        )
        raise SchemaError(f"{where}: no data arrays: {reason}")
    # An array with more axes holds more of the store than one with fewer: a time and band cube
    # over a plane of flags. Among arrays of equal axes the larger set is the bands.
    (axes, dims, _, _), chosen = max(
        candidates.items(), key=lambda item: (len(item[0][0]), len(item[1]))
    )
    left_out = [n for key, items in candidates.items() for n, _ in items if items is not chosen]
    left_out += [*skipped, *unnamed]
    if left_out:
        found.should(
            where,
            f"arrays left out of the bands: {', '.join(left_out)} (other shape, dtype or "
            "dimensions)",
        )
    return _Arrays(dict(chosen), axes, dims)


def _dim_names(array: zarr.Array) -> tuple[str, ...] | None:
    names = getattr(array.metadata, "dimension_names", None)
    if names is None:
        names = array.attrs.get("_ARRAY_DIMENSIONS")
    if isinstance(names, list | tuple) and all(isinstance(n, str) for n in names):
        return tuple(str(n) for n in names)
    return None


def _axes(dims: tuple[str, ...], spatial: Any) -> tuple[str, ...] | None:
    """time, band, y and x for each dimension, or None when the dimensions are not those."""
    *lead, ydim, xdim = dims
    if isinstance(spatial, list) and len(spatial) == 2:
        if (ydim, xdim) != tuple(spatial):
            return None
    elif ydim.lower() not in Y_NAMES or xdim.lower() not in X_NAMES:
        return None
    roles = tuple(
        "time" if d.lower() in TIME_NAMES else "band" if d.lower() in BAND_NAMES else d
        for d in lead
    )
    return (*roles, "y", "x") if roles in LEADS else None


def _natural(name: str) -> list[Any]:
    """A sort key that puts b2 before b10 and b8a after b07."""
    return [int(t) if t.isdigit() else t for t in _DIGITS_RE.split(name)]


def _nearest(scopes: Sequence[Mapping[str, Any]], key: str) -> Any:
    """The value of `key` in the first scope that has it. Scopes run from nearest to farthest."""
    return next((s[key] for s in scopes if s.get(key) is not None), None)


# --- Geometry and CRS -------------------------------------------------------------------------


def _transform(
    group: zarr.Group,
    arrays: _Arrays,
    scopes: Sequence[Mapping[str, Any]],
    where: str,
    found: Findings,
) -> Transform:
    raw = _nearest(scopes, "spatial:transform")
    if raw is not None:
        transform = schema.parse_transform(raw, f"{where}: spatial:transform")
        if _nearest(scopes, "spatial:registration") == "node":
            a, _, c, _, e, f = transform
            transform = (a, 0.0, c - a / 2, 0.0, e, f - e / 2)
            found.should(
                where,
                "spatial:registration is 'node': the transform names the centre of the first "
                "pixel, so the origin moves half a pixel",
            )
        return transform
    ydim, xdim = arrays.dims[-2], arrays.dims[-1]
    try:
        y, x = (
            np.asarray(schema.get_array(group, d, where)[:], dtype="float64") for d in (ydim, xdim)
        )
    except SchemaError as exc:
        raise SchemaError(
            f"{where}: no geometry: there is no spatial:transform, and no coordinate array "
            f"{ydim!r} or {xdim!r}"
        ) from exc
    first = arrays.first
    if len(y) != first.shape[-2] or len(x) != first.shape[-1] or min(len(x), len(y)) < 2:
        raise SchemaError(
            f"{where}: the coordinate arrays {ydim!r} and {xdim!r} do not match the data"
        )
    step_x, step_y = (x[-1] - x[0]) / (len(x) - 1), (y[-1] - y[0]) / (len(y) - 1)
    for name, values, step in ((xdim, x, step_x), (ydim, y, step_y)):
        if not np.allclose(np.diff(values), step, rtol=1e-3, atol=abs(step) * 1e-3):
            raise SchemaError(f"{where}: the coordinate array {name!r} is not evenly spaced")
    found.should(
        where,
        f"geometry read from the {xdim} and {ydim} coordinate arrays (no spatial:transform), as "
        "pixel centres",
    )
    return (step_x, 0.0, x[0] - step_x / 2, 0.0, step_y, y[0] - step_y / 2)


def _north_up(transform: Transform, where: str) -> None:
    a, b, _, d, e, _ = transform
    if not (math.isfinite(a) and math.isfinite(e) and a > 0 > e and b == d == 0):
        raise SchemaError(
            f"{where}: the transform {list(transform)} is not north-up (positive x step, "
            "negative y step, no rotation); chronozarr reads north-up grids only"
        )


def _crs(
    group: zarr.Group,
    array: zarr.Array,
    scopes: Sequence[Mapping[str, Any]],
    entry: Mapping[str, Any],
    where: str,
    found: Findings,
) -> Crs:
    for scope in scopes:
        if any(scope.get(k) is not None for k in _PROJ_KEYS):
            return schema.parse_crs(scope, where)
    if isinstance(entry.get("crs"), str):
        found.should(
            where, "CRS read from the crs of the multiscales layout (no proj: attributes)"
        )
        return _crs_text(entry["crs"], where)
    for name in (
        array.attrs.get("grid_mapping"),
        group.attrs.get("grid_mapping"),
        "spatial_ref",
        "crs",
    ):
        if isinstance(name, str) and name in group:
            mapping = group[name]
            if isinstance(mapping, zarr.Array):
                attrs = mapping.attrs.asdict()
                text = attrs.get("crs_wkt") or attrs.get("spatial_ref")
                if isinstance(text, str) and text.strip():
                    found.should(
                        where, f"CRS read from the CF grid mapping {name} (no proj: attributes)"
                    )
                    return _crs_text(text, where)
                if attrs.get("grid_mapping_name"):
                    found.should(
                        where, f"CRS read from the CF grid mapping {name} (no proj: attributes)"
                    )
                    return Crs(wkt2=wkt2_from_cf(attrs, name))
    raise SchemaError(
        f"{where}: no CRS: the level, its group and the multiscales layout carry no proj:code, "
        "proj:wkt2 or proj:projjson, and no CF grid mapping variable has a crs_wkt"
    )


def _crs_text(text: str, where: str) -> Crs:
    """A CRS written as text. WKT1 needs pyproj; without it the authority that ends it names it."""
    try:
        return Crs.from_string(text)
    except ValueError as exc:
        match = _AUTHORITY_RE.search(text)
        if match is None:
            raise SchemaError(f"{where}: {exc}") from exc
        return Crs(code=f"{match.group(1).upper()}:{match.group(2)}")


# --- Validity, scale, bands and time ----------------------------------------------------------


def _common_bands(levels: list[_Read], found: Findings) -> list[_Read]:
    """The levels with only the bands that every level has, in the order of the finest level.

    A store can hold other bands at other resolutions: Sentinel-2 has four bands at 10 m and ten
    at 20 m. A chronozarr store has one set of bands, so a band that some level lacks is left out.
    """
    if any("band" in level.arrays.axes for level in levels):
        return levels  # one array holds the bands, and the levels are compared by their count
    common = [
        n for n in levels[0].arrays.arrays if all(n in lv.arrays.arrays for lv in levels[1:])
    ]
    if not common:
        raise SchemaError("no band is present at every level")
    left_out = [
        f"{', '.join(missing)} at {level.path}"
        for level in levels
        if (missing := [n for n in level.arrays.arrays if n not in common])
    ]
    if left_out:
        found.should(
            "multiscales",
            f"bands that not every level has are left out: {'; '.join(left_out)}",
        )
    return [
        replace(
            level, arrays=replace(level.arrays, arrays={n: level.arrays.arrays[n] for n in common})
        )
        for level in levels
    ]


def _with_validity(level: _Read, found: Findings) -> _Read:
    """The level with its nodata value and the scale and offset of each of its arrays."""
    where = f"level {level.path}"
    fills = {_fill(array, where, found) for array in level.arrays.arrays.values()}
    if len(fills) > 1:
        found.should(where, f"the arrays disagree on the fill value {sorted(map(str, fills))}")
    scales = tuple(_scale(array, where, found) for array in level.arrays.arrays.values())
    return replace(level, nodata=next(iter(fills)), scales=scales)


def _fill(array: zarr.Array, where: str, found: Findings) -> int | float | None:
    """The value that marks a missing pixel: `_FillValue`, else a non-default `fill_value`."""
    declared = _decode_fill(array.attrs.get("_FillValue"), array.dtype)
    if declared is not None:
        return None if math.isnan(declared) else _plain(declared, array.dtype)
    fill = array.fill_value
    if isinstance(fill, float | np.floating) and math.isnan(fill):
        return None
    if fill == 0:
        found.should(
            where,
            "no _FillValue: every stored value is valid (the array fill_value 0 is Zarr's "
            "default, and is not read as nodata)",
        )
        return None
    found.should(
        where,
        f"validity assumed from the array fill_value {fill} (no _FillValue); that value marks "
        "missing pixels",
    )
    return _plain(float(fill), array.dtype)


def _plain(value: float, dtype: np.dtype) -> int | float:
    return int(value) if dtype.kind in "iu" else float(value)


def _decode_fill(raw: Any, dtype: np.dtype) -> float | None:
    """`_FillValue` as a number: a number as it is, a string as xarray writes it (base64)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        return float(raw)
    if not isinstance(raw, str):
        return None
    if raw.lower() == "nan":
        return math.nan
    try:
        packed = base64.standard_b64decode(raw)
    except ValueError:
        return None
    formats = {"f": {8: "<d", 4: "<f"}, "i": {8: "<q", 4: "<i"}, "u": {8: "<Q", 4: "<I"}}
    layout = formats.get(dtype.kind, {}).get(len(packed))
    return None if layout is None else float(struct.unpack(layout, packed)[0])


def _scale(array: zarr.Array, where: str, found: Findings) -> tuple[float | None, float | None]:
    """CF `scale_factor` and `add_offset` of the array, which holds the encoded values.

    EOPF keeps them in `_eopf_attrs` together with the `dtype` of the encoded values. They apply
    only when the array still has that dtype: a float32 array under `<u2` attributes holds values
    that were decoded already, and scaling them again would be wrong.
    """

    def find(source: Mapping[str, Any], key: str) -> float | None:
        value = source.get(key)
        return (
            float(value)
            if isinstance(value, int | float) and not isinstance(value, bool)
            else None
        )

    scale, offset = find(array.attrs, "scale_factor"), find(array.attrs, "add_offset")
    eopf = array.attrs.get("_eopf_attrs")
    if isinstance(eopf, Mapping) and (scale, offset) == (None, None):
        declared = find(eopf, "scale_factor"), find(eopf, "add_offset")
        if declared != (None, None):
            if _encoded_dtype(eopf) == array.dtype:
                return declared
            found.should(
                where,
                "_eopf_attrs scale_factor and add_offset are not applied: the array holds "
                f"{array.dtype}, and they describe the {eopf.get('dtype')} values it was "
                "decoded from",
            )
    return scale, offset


def _encoded_dtype(eopf: Mapping[str, Any]) -> np.dtype | None:
    try:
        return np.dtype(str(eopf["dtype"]))
    except (KeyError, TypeError):
        return None


def _band_names(group: zarr.Group, arrays: _Arrays) -> list[str]:
    """One name per band: the band coordinate, else the array names, else `band_0`, `band_1`."""
    if "band" in arrays.axes:
        n_band = arrays.first.shape[arrays.axes.index("band")]
        name = arrays.dims[arrays.axes.index("band")]
        if name in group:
            values = np.asarray(schema.get_array(group, name, "store")[:]).tolist()
            if len(values) == n_band:
                return [str(v) for v in values]
        return [f"band_{i}" for i in range(n_band)]
    return list(arrays.arrays)


def _read_times(level: _Read, found: Findings) -> np.ndarray:
    """Timestamps as datetime64[ms]: the time array decoded with its CF units, else one."""
    arrays = level.arrays
    if "time" not in arrays.axes:
        found.should(
            f"level {level.path}",
            "no time axis: one timestep dated 1970-01-01T00:00:00Z as a placeholder (assumed)",
        )
        return np.array([PLACEHOLDER_TIME], dtype="datetime64[ms]")
    name = arrays.dims[arrays.axes.index("time")]
    try:
        coordinate = schema.get_array(level.group, name, f"level {level.path}")
    except SchemaError:
        coordinate = None
    if coordinate is None or coordinate.ndim != 1:
        raise SchemaError(f"level {level.path}: dimension {name!r} has no coordinate array")
    values = np.asarray(coordinate[:])
    if values.dtype.kind == "M":
        return values.astype("datetime64[ms]")
    units = coordinate.attrs.get("units")
    calendar = str(coordinate.attrs.get("calendar", "standard"))
    if not isinstance(units, str) or " since " not in units:
        raise SchemaError(
            f"level {level.path}: the time array {name!r} has no CF units such as "
            "'days since 2020-01-01', so its values cannot be dated"
        )
    try:
        decoded = decode_cf_datetime(values, units, calendar)
    except (ValueError, ImportError, OverflowError) as exc:
        raise SchemaError(
            f"level {level.path}: cannot decode the time array with units {units!r} on the "
            f"{calendar} calendar ({exc}); a store on a calendar other than Gregorian goes "
            "through chronozarr convert"
        ) from exc
    if is_cftime(decoded):
        raise SchemaError(
            f"level {level.path}: the time array is on the {calendar} calendar, which a "
            "chronozarr store cannot hold; chronozarr convert maps it with "
            "--calendar-to-gregorian"
        )
    return np.asarray(decoded).astype("datetime64[ms]")


# --- Putting the levels together --------------------------------------------------------------


def _same_axes(first: _Read, other: _Read) -> None:
    for what, a, b in (
        ("bands", len(first.arrays.arrays), len(other.arrays.arrays)),
        ("data types", first.first.dtype, other.first.dtype),
        ("axes", first.arrays.axes, other.arrays.axes),
    ):
        if a != b:
            raise SchemaError(f"levels {first.path} and {other.path} have different {what}")
    if same_crs(first.crs, other.crs).same is False:
        raise SchemaError(f"levels {first.path} and {other.path} have different CRSs")


def _marker(nodata: int | float | None) -> str:
    return "NaN or no marker" if nodata is None else f"{nodata:g}"


def _note_ladder(levels: Sequence[_Read], found: Findings) -> None:
    """List the levels whose pixel size is not a power of two times the finest level's."""
    odd = []
    for level in levels[1:]:
        ratio = level.resolution / levels[0].resolution
        if abs(math.log2(ratio) - round(math.log2(ratio))) > 1e-6:
            odd.append(f"{level.path} ({ratio:g}x)")
    if odd:
        found.should(
            "multiscales",
            f"levels that are not a factor-two ladder of level 0: {', '.join(odd)}; the browser "
            "viewer reads only factor-two pyramids",
        )


def _assemble(
    root: zarr.Group, ordered: Sequence[_Read], times: np.ndarray, found: Findings
) -> Outside:
    first = ordered[0]
    names = _band_names(first.group, first.arrays)
    scales = _band_scales(first, names, found)
    levels = []
    for index, level in enumerate(ordered):
        data = LevelData(tuple(level.arrays.arrays.values()), level.arrays.axes)
        rows, cols = data.cell_shape
        height, width = data.shape[2], data.shape[3]
        levels.append(
            Level(
                index=index,
                path=level.path,
                shape=data.shape,
                transform=level.transform,
                resolution=level.resolution,
                grid=(math.ceil(height / rows), math.ceil(width / cols)),
                data=data,
                nodata=level.nodata,
            )
        )
    if levels[0].shape[1] != len(names):
        raise SchemaError(f"level {first.path}: {levels[0].shape[1]} bands but {len(names)} names")
    iso = [f"{s}Z" for s in np.datetime_as_string(times, unit="s")]
    only = len(first.arrays.arrays) == 1
    variable = next(iter(first.arrays.arrays)) if only else schema.VARIABLE
    attrs = Chronozarr(
        times=tuple(iso),
        bands=tuple(Band(n, scale=s, offset=o) for n, (s, o) in zip(names, scales, strict=True)),
        crs=None,
        spec_version=None,
        variable=variable,
        nodata=first.nodata,
    )
    return Outside(attrs, times, tuple(levels), first.crs)


def _band_scales(
    first: _Read, names: Sequence[str], found: Findings
) -> list[tuple[float | None, float | None]]:
    """(scale, offset) per band; an array holding several bands gives each of them its own."""
    per_array = list(first.scales)
    scales = per_array * len(names) if len(per_array) == 1 else per_array
    declared = [s for s in scales if s != (None, None)]
    if not declared:
        found.should(
            f"level {first.path}",
            "scale and offset are not declared (no scale_factor or add_offset): stored values "
            "are read as physical values (assumed)",
        )
    elif len(declared) != len(scales):
        found.should(
            f"level {first.path}",
            "scale and offset are declared for some bands only: the others are read as stored "
            "values (assumed)",
        )
    return scales
