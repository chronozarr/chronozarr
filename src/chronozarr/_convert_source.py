"""Conversion source contract, grid checks, and pixel validity."""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from chronozarr.crs import Crs, is_code_text
from chronozarr.schema import Band, Transform, axis_unit

Bounds = tuple[float, float, float, float]  # west, south, east, north in the units of the CRS
_EDGE_TOLERANCE = 1e-6  # degrees; a pixel edge at 180 that arithmetic puts a hair beyond


@dataclass(frozen=True)
class Grid:
    """Target pixel grid: a CRS (`EPSG:<code>`, or WKT2 text when it has no code), a north-up
    transform and a shape."""

    crs: str
    transform: Transform
    height: int
    width: int

    def describe(self) -> str:
        return (
            f"{_crs_label(self.crs)}, {self.height} x {self.width} px, "
            f"{abs(self.transform[0]):.10g} {axis_unit(self.crs)} pixels (transform origin "
            f"{self.transform[2]:.10g}, {self.transform[5]:.10g})"
        )

    def longitude_note(self) -> str | None:
        """A plan line when the axes are degrees and x runs outside -180 to 180, else None.

        Such a store is placed correctly by GDAL and zarr-layer, but a lookup with a longitude in
        the other convention finds no column and `sel(method="nearest")` returns an edge one.
        """
        if axis_unit(self.crs) != "degree":
            return None
        west = self.transform[2]
        east = west + self.transform[0] * self.width
        if west >= -180 - _EDGE_TOLERANCE and east <= 180 + _EDGE_TOLERANCE:
            return None
        return (
            f"x runs {west:.10g} to {east:.10g} degrees, outside -180 to 180, and is stored as "
            "given. Look longitudes up in the same convention (shift by 360): "
            'sel(x=-80, method="nearest") returns an edge column, with no error'
        )


@dataclass(frozen=True)
class SourceInfo:
    """What a source reports about itself before any pixel is read.

    `nodata` is the store's sentinel (None for none). `mask` says the store gets a `mask`
    variable and every `Step` carries its plane; `validity` explains the choice in one line.
    """

    grid: Grid
    n_band: int
    dtype: np.dtype
    nodata: float | int | None
    band_names: tuple[str, ...]
    bands: tuple[Band, ...]
    mask: bool = False
    validity: str = ""


@dataclass
class Step:
    """One timestep: data (band, y, x) in the source dtype and, for a mask store, the (y, x)
    uint8 validity plane (1 = valid)."""

    data: np.ndarray
    valid: np.ndarray | None = None


class Source:
    """A timestep-addressable input on a known grid."""

    kind: str
    times: list[np.datetime64]
    info: SourceInfo
    # Indices of timesteps that are not on the target grid and must be warped (COG only).
    warped: frozenset[int] = frozenset()
    # Findings that do not stop the conversion but change what it writes, one plan line each.
    notes: tuple[str, ...] = ()

    def read(self, t: int) -> Step:
        """Timestep `t` on `info.grid`: data in `info.dtype`, plus validity when `info.mask`."""
        raise NotImplementedError

    def fingerprint(self) -> Any:
        """JSON-able identity of what `read` will produce, for resume."""
        raise NotImplementedError


def check_bounds(values: object, where: str) -> Bounds:
    """`west, south, east, north` as floats; ValueError unless four finite numbers with
    west < east and south < north."""
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        raise ValueError(f"{where}: bounds must be four numbers west,south,east,north")
    try:
        west, south, east, north = (float(v) for v in values)
    except (TypeError, ValueError):
        raise ValueError(f"{where}: bounds must be four numbers, got {list(values)}") from None
    if not all(math.isfinite(v) for v in (west, south, east, north)):
        raise ValueError(f"{where}: bounds must be finite, got {list(values)}")
    if not (west < east and south < north):
        raise ValueError(
            f"{where}: bounds need west < east and south < north, got "
            f"west={west:g}, south={south:g}, east={east:g}, north={north:g}"
        )
    return (west, south, east, north)


def _crs_string(crs: Any) -> str:
    """A rasterio CRS as the text a `Grid` carries: `EPSG:<code>` if it has a code, else WKT2."""
    code = crs.to_epsg()
    return f"EPSG:{code}" if code is not None else crs.to_wkt(version="WKT2_2019")


def _crs_label(crs: str) -> str:
    """A short name of a `Grid` CRS for messages: the code, or the name in its WKT2."""
    return crs if is_code_text(crs) else Crs(wkt2=crs).describe()


def _same_crs(a: str, b: str) -> bool:
    """Whether two `Grid` CRS texts name the same CRS; rasterio compares them, names aside."""
    if a == b:
        return True
    from rasterio.crs import CRS  # ty: ignore[unresolved-import]  # compiled, no stubs

    return bool(CRS.from_user_input(a) == CRS.from_user_input(b))


def _bounds_grid(bounds: Bounds, crs: str, height: int, width: int) -> Grid:
    """The north-up grid that `bounds` covers when divided into `height` x `width` pixels."""
    west, south, east, north = bounds
    transform = ((east - west) / width, 0.0, west, 0.0, -(north - south) / height, north)
    return Grid(crs, transform, height, width)


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
    if (a.height, a.width) != (b.height, b.width) or not _same_crs(a.crs, b.crs):
        return False
    tolerance = 1e-6 * abs(a.transform[0])
    return all(abs(x - y) <= tolerance for x, y in zip(a.transform, b.transform, strict=True))


def _grid_difference(found: Grid, wanted: Grid) -> str:
    parts = []
    if not _same_crs(found.crs, wanted.crs):
        parts.append(f"CRS {_crs_label(found.crs)} vs {_crs_label(wanted.crs)}")
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


_NAN = "nan"  # token for a declared NaN nodata; the other tokens are None or a number


def _declared_nodata(value: float | None, dtype: np.dtype) -> float | int | str | None:
    """A declared nodata value as a token, or None when unset or when no pixel can hold it.

    A number is what `dtype` stores it as (float32-rounded; int for integer dtypes); NaN is
    `_NAN`. A value outside the dtype's range can never match a pixel, so it counts as unset.
    """
    if value is None:
        return None
    number = float(value)
    if math.isnan(number):
        return _NAN if dtype.kind == "f" else None
    if dtype.kind == "f":
        if math.isinf(number):
            return number
        if abs(number) > float(np.finfo(np.float32).max):
            return None
        return float(np.float32(number))
    info = np.iinfo(dtype)
    if not number.is_integer() or not info.min <= number <= info.max:
        return None
    return int(number)


def _checked_nodata(value: float | int, dtype: np.dtype) -> float | int:
    """The explicit `nodata` as `dtype` stores it; ValueError if it cannot be a store nodata."""
    if not math.isfinite(float(value)):
        raise ValueError(f"nodata must be finite, got {value!r}; NaN needs a mask (use nan)")
    token = _declared_nodata(value, dtype)
    if token is None:
        raise ValueError(
            f"nodata {value!r} cannot be held by {dtype.name} data; choose a value of that "
            "dtype or flag invalid pixels with a mask"
        )
    assert isinstance(token, int | float)  # finite and in range, so a number
    return token


@dataclass(frozen=True)
class Validity:
    """How the store carries the sources' validity (rules 3 to 7 of the module docstring)."""

    mask: bool
    nodata: float | int | None
    why: str


def _decide_validity(
    dtype: np.dtype,
    declared: Collection[float | int | str | None],
    *,
    override: float | int | None | str,
    explicit: Sequence[str],
    footprint: str | None,
) -> Validity:
    """Choose between a nodata sentinel and a mask.

    `declared` are the nodata tokens of every source and band (None where a band declares
    none); `override` is "auto", None or the explicit nodata; `explicit` names the sources'
    own masks (alpha band, internal mask, `--mask-var`); `footprint` says why some pixels have
    no source behind them (warped timesteps, tiles that leave a gap), or is None.
    """
    reasons = list(explicit)
    candidate: float | int | None = None
    if override == "auto":
        tokens = set(declared) or {None}
        if len(tokens) > 1:
            shown = ", ".join("none" if t is None else str(t) for t in sorted(tokens, key=str))
            reasons.append(f"the declared nodata values differ ({shown})")
        else:
            (token,) = tokens
            if token == _NAN or (isinstance(token, float) and math.isinf(token)):
                reasons.append(f"nodata {token} cannot be a store nodata")
            else:
                assert not isinstance(token, str)
                candidate = token
    elif isinstance(override, str):
        raise ValueError(f"nodata must be a number, None or 'auto', got {override!r}")
    elif override is not None:
        if math.isnan(float(override)):
            if dtype.kind != "f":
                raise ValueError(f"nodata nan needs float data; the sources are {dtype.name}")
            reasons.append("nodata nan was requested")
        else:
            candidate = _checked_nodata(override, dtype)
    if footprint is not None and candidate is None:
        reasons.append(footprint)
    if not reasons:
        if candidate is None:
            what = "no nodata declared" if override == "auto" else "no nodata (--nodata none)"
            return Validity(False, None, f"{what} and no mask: every pixel is stored as data")
        return Validity(False, candidate, f"nodata {candidate} sentinel, no mask")
    kept = None if override == "auto" else candidate
    note = "no nodata" if kept is None else f"nodata {kept}"
    return Validity(True, kept, f"mask ({'; '.join(reasons)}), {note}")


def _value_valid(data: np.ndarray, sentinels: Sequence[float | int]) -> np.ndarray:
    """(y, x) bool from the values alone: no band equals a sentinel, and no float band is NaN."""
    valid = np.ones(data.shape[1:], dtype=bool)
    for sentinel in sentinels:
        valid &= (data != np.asarray(sentinel, dtype=data.dtype)).all(axis=0)
    if data.dtype.kind == "f":
        valid &= ~np.isnan(data).any(axis=0)
    return valid


class UndeclaredNaNError(ValueError):
    """A float source holds NaN, but declares no NaN nodata and the store has no mask for it.

    The message is the `convert` one, which can pass `--nodata nan`; `encode` and `append` read
    `where` and `count` to say what they can do instead.
    """

    def __init__(self, where: str, count: int) -> None:
        self.where = where
        self.count = count
        super().__init__(
            f"{where} holds {count} NaN values, but the source declares no NaN nodata "
            "and the store has no mask. Declare NaN as the source's nodata, or pass --nodata "
            "nan to mark NaN pixels invalid with a mask"
        )


def _settle_nan(
    data: np.ndarray, valid: np.ndarray | None, nodata: float | int | None, where: str
) -> None:
    """NaN is never stored (spec 2.3). In a mask store NaN pixels are already invalid and become
    the fill value; in a sentinel store NaN is an error."""
    if data.dtype.kind != "f":
        return
    nan = np.isnan(data)
    if not nan.any():
        return
    if valid is None:
        raise UndeclaredNaNError(where, int(nan.sum()))
    data[nan] = 0 if nodata is None else nodata


def _scaling(scale: float, offset: float, where: str) -> tuple[float, float]:
    if not (math.isfinite(scale) and scale != 0 and math.isfinite(offset)):
        raise ValueError(
            f"{where} has scale {scale} and offset {offset}; the scale must be finite and "
            "non-zero and the offset finite"
        )
    return scale, offset


def override_scaling(
    bands: Sequence[Band], scale: float | None, offset: float | None
) -> tuple[tuple[Band, ...], str]:
    """`bands` with the scale and offset the user gave in place of the sources' own, and the plan
    line that says so. A value left as None keeps what the sources declare; ValueError when a given
    scale is zero or not finite, or a given offset is not finite."""
    if scale is not None and not (math.isfinite(scale) and scale != 0):
        raise ValueError(f"--scale must be finite and non-zero, got {scale}")
    if offset is not None and not math.isfinite(offset):
        raise ValueError(f"--offset must be finite, got {offset}")

    def declared(values: Sequence[float | None], none: float) -> str:
        return ", ".join(dict.fromkeys(f"{none if v is None else v:.10g}" for v in values))

    parts = []
    if scale is not None:
        parts.append(
            f"--scale {scale:.10g}; the files declare {declared([b.scale for b in bands], 1.0)}"
        )
    if offset is not None:
        parts.append(
            f"--offset {offset:.10g}; the files declare {declared([b.offset for b in bands], 0.0)}"
        )
    changed = tuple(
        replace(
            band,
            scale=band.scale if scale is None else scale,
            offset=band.offset if offset is None else offset,
        )
        for band in bands
    )
    return changed, ". ".join(parts)
