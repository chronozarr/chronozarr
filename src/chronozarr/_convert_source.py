"""Conversion source contract, grid checks, and pixel validity."""

from __future__ import annotations

import math
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from chronozarr.schema import Band, Transform

Bounds = tuple[float, float, float, float]  # west, south, east, north in the units of the CRS


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


def _epsg(crs: Any, where: str) -> str:
    code = crs.to_epsg() if crs is not None else None
    if code is None:
        raise ValueError(
            f"{where}: the CRS has no EPSG code; chronozarr stores declare EPSG:<code>. "
            "Pass --crs EPSG:xxxxx"
        )
    return f"EPSG:{code}"


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
    footprint: bool,
) -> Validity:
    """Choose between a nodata sentinel and a mask.

    `declared` are the nodata tokens of every source and band (None where a band declares
    none); `override` is "auto", None or the explicit nodata; `explicit` names the sources'
    own masks (alpha band, internal mask, `--mask-var`); `footprint` says warped timesteps
    leave pixels with no source behind them.
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
    if footprint and candidate is None:
        reasons.append("warped timesteps leave pixels outside the source footprint")
    if not reasons:
        if candidate is None:
            return Validity(False, None, "no nodata and no mask: every pixel is valid")
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
