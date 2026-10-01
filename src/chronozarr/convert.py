"""Streaming conversion of COG manifests, Zarr variables and NetCDF files to a chronozarr store.

The source is read one timestep at a time and staged as a raw `.npy` per timestep in a work
directory, then handed to `encode()` as an iterable of timesteps, which spills and encodes cell
by cell. Peak memory is about one timestep times `read_ahead`; disk is the raw stack once in the
work directory, once more in `encode`'s cell-major spill, plus the output. Staged timesteps are
the unit of `resume`: an interrupted run keeps them, and a rerun reads only the missing ones.

Three input kinds are recognised from SOURCE:

* a manifest (`.csv` or `.json`) of COG or image-frame (PNG) URIs with timestamps, one URI per
  timestep;
* a Zarr store (local path or http(s) URL) with a chosen variable;
* a NetCDF file (`.nc`, `.nc4`, `.cdf`) with a chosen variable (needs an xarray NetCDF engine).

Needs rasterio (`chronozarr[geo]`) for COG manifests and for the CRS handling of all kinds.

Fidelity rules. The store carries the source's scale, offset, units and validity, or the
conversion fails; nothing is guessed.

Scale, offset and units:

1. COG: rasterio's per-band `scales`, `offsets`, `units` and `descriptions` become the band
   objects. Scale and offset are always written (1 and 0 when the source sets none). A store
   holds one value per band for every timestep, so each must be identical in all sources, band by
   band; the first source that differs fails the conversion and is named. Descriptions name the
   bands unless the manifest does, and must agree too. An alpha band (colour interpretation
   alpha) is not a data band: it is not stored as data and becomes the mask (rule 4).
2. Zarr and NetCDF: the variable's CF `scale_factor`, `add_offset` and `units` apply to every
   band (1 and 0 when absent); a scale that is zero or not finite fails.

Validity. A store marks invalid pixels either with one `nodata` sentinel, judged per band with no
mask, or with a `mask` variable shared by all bands (spec 2.3, 2.4).

3. Sentinel, only when faithful: every source declares the same nodata value (representable in
   the dtype and finite) on every data band and has no mask or alpha band, and no timestep is
   warped. The store's nodata is that value and there is no `mask`. A source that declares no
   nodata and has no mask yields a store with no nodata (every pixel valid, a stored 0 is data),
   not the encoder default of 0.
4. Mask otherwise: the store gets a `mask` variable when any source has an alpha band, an
   internal or per-dataset mask, or a nodata value that cannot be the store's (NaN, infinite,
   differing between sources or bands, or declared on only some of them); when a timestep is
   warped and the store has no nodata sentinel (pixels outside the source footprint are invalid);
   or when a Zarr/NetCDF variable names its mask with `--mask-var`. A store with a mask has no
   nodata (so a valid value equal to the old sentinel stays valid), unless `--nodata` gives one.
5. Mask content, 1 = valid. COG: the alpha band is nonzero (alpha wins over everything else, as
   in GDAL; a partly transparent pixel is valid), else the pixel is valid in every data band
   under GDAL's band masks (internal mask, per-dataset mask or nodata value). One plane cannot say
   that bands disagree, so a pixel invalid in any band is invalid for all of them; the stored
   values of the other bands are kept. Zarr/NetCDF: the `--mask-var` variable is nonzero (it must
   be boolean or integer with dims time, y, x; invert a "1 = bad" flag first) and no band
   holds a declared `_FillValue` or `missing_value`. valid_min, valid_max and valid_range are not
   applied; fold them into a `--mask-var`.
6. Values under a zero mask keep the source values, so the store loses no bytes, except NaN
   (never stored; spec 2.3), which becomes the fill value (the nodata, else 0), and except in a
   warped timestep, where the warper does not copy masked source pixels (they take the fill
   value). A float source
   with NaN in a sentinel store fails and names the timestep: declare NaN as the source nodata or
   pass `--nodata nan`, which writes a mask.
7. `--nodata N` replaces the nodata the sources declare: pixels equal to N are invalid and the
   sources' own nodata values are ordinary data (alpha and mask bands still apply). `--nodata none`
   ignores declared nodata values and stores none; `--nodata nan` (float data) marks NaN invalid
   through a mask.
8. Warped COG timesteps use the nearest-neighbour resampling of their validity plane, whatever
   `--resampling` says for the values.

Image frames. A sequence of rendered, georeferenced PNGs (Earth Engine thumbnails, QGIS and
matplotlib exports, drone pipelines) converts like COGs, with no GeoTIFF step; GDAL reads the
frames, so every rule above applies to them. A frame is display values, not measurements: nothing
is rescaled and the store holds exactly the 8-bit (or 16-bit) values of the file.

9. Georeferencing comes from the frame: a `.png.aux.xml` sidecar (CRS and geotransform), or a world
   file (`.pgw`, else `.wld`) beside it, which holds the transform and no CRS. Sidecars are found
   next to local and remote PNGs alike; for other formats they are not looked for. A frame whose
   CRS is missing takes `crs` (`--crs`), which is then both its CRS and the target CRS, so nothing
   is warped; a frame with a geotransform and no CRS and no `crs` fails.
10. A frame with no georeferencing at all fails, unless `bounds` (`--bounds west,south,east,north`,
   in the units of `crs`; or a `"bounds"` entry in a JSON manifest) gives the extent of every
   frame. The north-up transform is then derived from each frame's pixel size, so all frames must
   have the same size, and a frame that carries its own geotransform is refused (drop `bounds`, or
   remove the sidecar). `bounds` needs `crs` and applies to manifests only.
11. Bands. Red, green and blue colour bands are named red, green and blue and get those common
   names, so the viewer's True color product works; a band the manifest or the file's band
   description names keeps that name and gets no common name. Other bands are named by their
   1-based index. An alpha band is the mask (rule 5): an RGBA frame becomes three bands plus
   `mask`, 0 where alpha is 0.
12. A palette (indexed colour) PNG fails: its stored values are palette indices, not colours.
   Expand it first (`gdal_translate -expand rgba in.png out.png`) or save the frames as RGB.
"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import shutil
import time
import warnings
from collections import deque
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from itertools import pairwise
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

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
# Formats whose georeferencing lives in sidecar files (world file, .aux.xml); GDAL finds them only
# by probing for each candidate name, which GDAL_ENV switches off for everything else.
SIDECAR_SUFFIXES = (".png",)
NETCDF_SUFFIXES = (".nc", ".nc4", ".cdf")
RESAMPLING_METHODS = ("nearest", "bilinear", "cubic", "average", "mode", "min", "max", "med")
# Raw input bytes per second through `encode()` (spill, pyramid, zstd level 5, write). Measured
# 230 to 260 MB/s on the 117-month Ucayali stack (7.1 GB in 27 to 31 s) on a 16-thread M3 Max
# laptop with other work running, 2026-09-30; set lower so estimates err on the long side.
ENCODE_BYTES_PER_S = 150e6
SAMPLE_TIMESTEPS = 3
# The fidelity rules in the module docstring, as `chronozarr convert --help` text (click
# paragraphs: a backspace line keeps the lines of the block that follows).
FIDELITY_HELP = """\
Scale, offset, units and validity are carried over from the source, or the conversion fails.

\b
Scale and offset: COG per-band scales, offsets, units and descriptions become the band objects
and must be identical in every source; an alpha band is not data, it becomes the mask.
Zarr/NetCDF: the CF scale_factor, add_offset and units of the variable apply to every band.

\b
Validity is either one nodata sentinel (judged per band, no mask) or one mask shared by all bands:
  sentinel  every source declares the same finite nodata (or none), has no mask or alpha band,
            and no timestep is warped. No nodata declared means no nodata: a stored 0 is data.
  mask      any alpha band, internal mask or --mask-var; a nodata that cannot be the store's
            (NaN, infinite, differing between sources or bands); or warped sources with no
            nodata (pixels outside their footprint). The store then has no nodata unless
            --nodata gives one. A pixel invalid in any band is invalid for all.
  NaN is never stored: under a mask it becomes 0. A float source with undeclared NaN fails;
  --nodata nan treats NaN as invalid. --nodata N replaces the declared nodata; none ignores it.

\b
Image frames (PNG): GDAL reads them like COGs and the rules above apply; the values are stored
as the file holds them (display values, not measurements).
  georeferencing  a .png.aux.xml (CRS and transform) or a world file .pgw (transform only)
                  beside the frame. A frame without a CRS takes --crs.
  no sidecar      --bounds west,south,east,north (units of --crs) derives the north-up transform
                  from the image size; every frame must then have the same size and none may
                  carry its own transform. A JSON manifest can hold "bounds" instead.
  bands           red, green, blue (common names set); the alpha band becomes the mask.
  palette PNG     refused: expand it to RGB first (gdal_translate -expand rgba).
"""
_P = ParamSpec("_P")
_R = TypeVar("_R")
Bounds = tuple[float, float, float, float]  # west, south, east, north in the units of the CRS
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
    """One timestep of a manifest."""

    uri: str
    time: np.datetime64


@dataclass(frozen=True)
class Manifest:
    """A parsed manifest: entries sorted by time, the band names it gives (None: take them from
    the sources) and the `bounds` of a JSON manifest (None: none given)."""

    entries: list[Entry]
    bands: tuple[str, ...] | None
    bounds: Bounds | None


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

    def read(self, t: int) -> Step:
        """Timestep `t` on `info.grid`: data in `info.dtype`, plus validity when `info.mask`."""
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


def read_manifest(path: Path) -> Manifest:
    """The entries, band names and bounds of a CSV or JSON manifest.

    CSV: header `uri,datetime[,bands]`, band names separated by `;`. JSON: a list of
    `{"uri", "datetime", "bands"?}` objects, or `{"bands"?: [...], "bounds"?: [w, s, e, n],
    "items": [...]}`; `bounds` is the extent of every frame in the units of the CRS, for frames
    with no georeferencing (see `convert`). Relative URIs are resolved against the manifest's
    directory. Every row that names bands must agree.
    """
    if not path.is_file():
        raise FileNotFoundError(f"manifest {path} does not exist")
    rows: list[Mapping[str, Any]]
    top_bands: object = None
    top_bounds: object = None
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
            top_bounds = document.get("bounds")
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
    bounds = None if top_bounds is None else check_bounds(top_bounds, f"{path} bounds")
    return Manifest(entries, names, bounds)


# --- Grids --------------------------------------------------------------------------------------


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


def _frame_grid(src: Any, crs: str | None, bounds: Bounds | None) -> Grid:
    """The grid of one opened frame: its own CRS and geotransform, `crs` standing in for a
    missing CRS, or the grid `bounds` gives a frame that has no geotransform."""
    from rasterio.crs import CRS  # ty: ignore[unresolved-import]  # compiled, no stubs

    located = not src.transform.is_identity  # GDAL answers the identity when it finds nothing
    given = None if crs is None else _epsg(CRS.from_user_input(crs), "--crs")
    declared = None if src.crs is None else _epsg(src.crs, "the frame")
    if bounds is not None:
        if given is None:
            raise ValueError("--bounds needs --crs, the CRS the bounds are in")
        if located:
            raise ValueError(
                "it carries its own geotransform (a world file or .aux.xml) and --bounds "
                "was given; drop --bounds, or remove the sidecar of the frames that have one"
            )
        if declared is not None and declared != given:
            raise ValueError(f"it declares {declared} but --crs is {given}")
        return _bounds_grid(bounds, given, src.height, src.width)
    if not located:
        raise ValueError(
            "it has no georeferencing (no geotransform, no world file next to it, no .aux.xml). "
            "Add a .pgw world file or a .aux.xml beside the frame, or pass --bounds "
            "west,south,east,north together with --crs"
        )
    frame_crs = declared or given
    if frame_crs is None:
        raise ValueError(
            "it has a geotransform but no CRS (a world file carries none). Pass --crs EPSG:xxxxx"
        )
    return Grid(
        frame_crs, _check_north_up(tuple(src.transform)[:6], "the frame"), src.height, src.width
    )


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


# --- Validity and band metadata -----------------------------------------------------------------

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
        raise ValueError(
            f"{where} holds {int(nan.sum())} NaN values, but the source declares no NaN nodata "
            "and the store has no mask. Declare NaN as the source's nodata, or pass --nodata "
            "nan to mark NaN pixels invalid with a mask"
        )
    data[nan] = 0 if nodata is None else nodata


def _scaling(scale: float, offset: float, where: str) -> tuple[float, float]:
    if not (math.isfinite(scale) and scale != 0 and math.isfinite(offset)):
        raise ValueError(
            f"{where} has scale {scale} and offset {offset}; the scale must be finite and "
            "non-zero and the offset finite"
        )
    return scale, offset


# --- COG manifest source ------------------------------------------------------------------------


@dataclass(frozen=True)
class _CogHeader:
    driver: str  # GDAL driver short name: GTiff, PNG, ...
    grid: Grid
    own_grid: bool  # the file carries its own CRS and geotransform (else `grid` came from options)
    data_indexes: tuple[int, ...]  # 1-based indexes of the data bands, the alpha band excluded
    alpha: int | None  # 1-based index of the alpha band
    internal_mask: bool  # the data bands share an internal or per-dataset mask (no alpha)
    dtype: np.dtype
    nodata: tuple[float | int | str | None, ...]  # declared token per data band
    warp_nodata: float | int | None  # first data band's nodata, for the warper
    descriptions: tuple[str | None, ...]
    colors: tuple[str | None, ...]  # "red", "green" or "blue" for a band of that colour
    scales: tuple[float, ...]
    offsets: tuple[float, ...]
    units: tuple[str | None, ...]

    @property
    def count(self) -> int:
        return len(self.data_indexes)


def _gdal_env(uri: str) -> dict[str, str]:
    """GDAL options for opening `uri`: no directory listing or sidecar probes, which cost a
    request each over HTTP, except for formats that keep their georeferencing in sidecar files."""
    if uri.split("?", 1)[0].lower().endswith(SIDECAR_SUFFIXES):
        return {**GDAL_ENV, "GDAL_DISABLE_READDIR_ON_OPEN": "TRUE"}
    return GDAL_ENV


def _tolerate_unlocated_frames(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run `func` without rasterio's warning for a source that has no geotransform.

    Such a frame is valid input when `bounds` locates it, and an error with its own message
    otherwise, so the warning (an error under `-W error`) adds nothing. The filter is set once, by
    the calling thread; the reader threads share it (a filter set per thread would race).
    """

    @wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        from rasterio.errors import NotGeoreferencedWarning

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", NotGeoreferencedWarning)
            return func(*args, **kwargs)

    return wrapper


_COLOR_NAMES = ("red", "green", "blue")


def _read_header(uri: str, crs: str | None, bounds: Bounds | None) -> _CogHeader:
    import rasterio
    from rasterio.enums import ColorInterp, MaskFlags

    try:
        with rasterio.Env(**_gdal_env(uri)), rasterio.open(uri) as src:
            if src.driver == "PNG" and ColorInterp.palette in src.colorinterp:
                raise ValueError(
                    "it is a palette (indexed colour) PNG, so its values are palette indices, "
                    "not colours. Expand it to RGB first, for example "
                    "`gdal_translate -expand rgba in.png out.png`, or save the frames as RGB"
                )
            grid = _frame_grid(src, crs, bounds)
            alphas = [i for i, c in enumerate(src.colorinterp, start=1) if c == ColorInterp.alpha]
            if len(alphas) > 1:
                raise ValueError(f"it has {len(alphas)} alpha bands; chronozarr reads one")
            alpha = alphas[0] if alphas else None
            indexes = tuple(i for i in range(1, src.count + 1) if i != alpha)
            if not indexes:
                raise ValueError("it has an alpha band and no data band")
            dtypes = {src.dtypes[i - 1] for i in indexes}
            if len(dtypes) > 1:
                raise ValueError(f"its data bands have different dtypes {sorted(dtypes)}")
            dtype = np.dtype(dtypes.pop())
            internal_mask = alpha is None and any(
                MaskFlags.per_dataset in src.mask_flag_enums[i - 1] for i in indexes
            )
            tokens = tuple(_declared_nodata(src.nodatavals[i - 1], dtype) for i in indexes)
            scaling = [
                _scaling(float(src.scales[i - 1]), float(src.offsets[i - 1]), f"band {i}")
                for i in indexes
            ]
            return _CogHeader(
                driver=src.driver,
                grid=grid,
                own_grid=src.crs is not None and not src.transform.is_identity,
                data_indexes=indexes,
                alpha=alpha,
                internal_mask=internal_mask,
                dtype=dtype,
                nodata=tokens,
                warp_nodata=math.nan if isinstance(tokens[0], str) else tokens[0],
                descriptions=tuple(src.descriptions[i - 1] or None for i in indexes),
                colors=tuple(
                    c if (c := src.colorinterp[i - 1].name) in _COLOR_NAMES else None
                    for i in indexes
                ),
                scales=tuple(s for s, _ in scaling),
                offsets=tuple(o for _, o in scaling),
                units=tuple(src.units[i - 1] or None for i in indexes),
            )
    except (rasterio.errors.RasterioIOError, ValueError) as exc:
        raise ValueError(f"cannot open source {uri}: {exc}") from exc


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
        bounds: Bounds | None = None,
    ) -> None:
        self.entries = entries
        self.times = [e.time for e in entries]
        self.resampling = resampling
        self.chunk_size = chunk_size
        workers = min(8, len(entries))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            headers = list(pool.map(lambda e: _read_header(e.uri, target_crs, bounds), entries))
        first = headers[0]
        if all(h.driver == "PNG" for h in headers):
            self.kind = "manifest of PNG frames"
        if bounds is not None:
            self._check_same_size(headers)
        self.grid = self._target_grid(first, target_crs, target_transform, target_shape)
        self._check_consistent(headers, compare_labels=band_names is None)
        self._headers = headers
        if band_names is None:
            # a description names the band; else a colour band is named by its colour and says so
            labels = [
                (d, None) if d else (c, c) if c else (str(i + 1), None)
                for i, (d, c) in enumerate(zip(first.descriptions, first.colors, strict=True))
            ]
        else:
            labels = [(n, None) for n in band_names]
        names = tuple(name for name, _ in labels)
        common_names = tuple(common for _, common in labels)
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
        explicit = []
        if any(h.alpha is not None for h in headers):
            explicit.append("alpha band")
        if any(h.internal_mask for h in headers):
            explicit.append("internal mask")
        validity = _decide_validity(
            first.dtype,
            {token for h in headers for token in h.nodata},
            override=nodata,
            explicit=explicit,
            footprint=bool(self.warped),
        )
        self._auto_nodata = nodata == "auto"
        # Values equal to an explicit --nodata are invalid; declared nodata is GDAL's to judge.
        self._sentinels: tuple[float | int, ...] = (
            () if self._auto_nodata or validity.nodata is None else (validity.nodata,)
        )
        self.info = SourceInfo(
            grid=self.grid,
            n_band=first.count,
            dtype=first.dtype,
            nodata=validity.nodata,
            band_names=names,
            bands=tuple(
                Band(
                    name=n,
                    common_name=common_names[i],
                    scale=first.scales[i],
                    offset=first.offsets[i],
                    units=first.units[i],
                )
                for i, n in enumerate(names)
            ),
            mask=validity.mask,
            validity=validity.why,
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

    def _check_same_size(self, headers: list[_CogHeader]) -> None:
        """With `bounds` every frame covers the same extent, so a different size is a different
        pixel size: refuse it rather than guess."""
        first = headers[0].grid
        for entry, header in zip(self.entries, headers, strict=True):
            grid = header.grid
            if (grid.height, grid.width) != (first.height, first.width):
                raise ValueError(
                    f"{entry.uri} is {grid.height} x {grid.width} px; {self.entries[0].uri} is "
                    f"{first.height} x {first.width} px. With bounds every frame covers the "
                    "same extent, so all frames must have the same size"
                )

    def _check_consistent(self, headers: list[_CogHeader], *, compare_labels: bool) -> None:
        first = headers[0]
        first_uri = self.entries[0].uri
        for entry, header in zip(self.entries, headers, strict=True):
            if header.count != first.count:
                raise ValueError(
                    f"{entry.uri} has {header.count} bands; {first_uri} has {first.count}"
                )
            if header.dtype != first.dtype:
                raise ValueError(
                    f"{entry.uri} is {header.dtype}; {first_uri} is {first.dtype}. "
                    "All sources must share one dtype"
                )
            fields = [
                ("scales", header.scales, first.scales),
                ("offsets", header.offsets, first.offsets),
                ("units", header.units, first.units),
            ]
            if compare_labels:
                fields.append(("band descriptions", header.descriptions, first.descriptions))
                fields.append(("band colours", header.colors, first.colors))
            for what, found, wanted in fields:
                if found != wanted:
                    raise ValueError(
                        f"{entry.uri} has {what} {list(found)}; {first_uri} has {list(wanted)}. "
                        "A store holds one scale, offset and unit per band for every timestep, "
                        "so the sources must agree: rescale them to one first"
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
            "bands": [b.to_attrs() for b in self.info.bands],
            "dtype": self.info.dtype.name,
            "nodata": self.info.nodata,
            "mask": self.info.mask,
            "validity": self.info.validity,
            "resampling": self.resampling if self.warped else None,
            "warped": sorted(self.warped),
        }

    def read(self, t: int) -> Step:
        import rasterio

        entry, header = self.entries[t], self._headers[t]
        grid, info = self.grid, self.info
        data = np.empty((info.n_band, grid.height, grid.width), dtype=info.dtype)
        valid = np.empty((grid.height, grid.width), dtype=np.uint8) if info.mask else None
        try:
            with rasterio.Env(**_gdal_env(entry.uri)), rasterio.open(entry.uri) as src:
                if t in self.warped:
                    self._read_warped(src, header, data, valid)
                else:
                    for top in range(0, grid.height, self.chunk_size):
                        rows = min(self.chunk_size, grid.height - top)
                        window = ((top, top + rows), (0, grid.width))
                        block = src.read(list(header.data_indexes), window=window)
                        data[:, top : top + rows, :] = block
                        if valid is not None:
                            valid[top : top + rows] = self._declared_valid(src, header, window)
                            valid[top : top + rows] &= _value_valid(block, self._sentinels)
        except rasterio.errors.RasterioError as exc:
            raise OSError(f"reading {entry.uri} (timestep {t}): {exc}") from exc
        _settle_nan(data, valid, info.nodata, f"{entry.uri} (timestep {t})")
        return Step(data, valid)

    def _declared_valid(
        self,
        src: Any,
        header: _CogHeader,
        window: tuple[tuple[int, int], tuple[int, int]] | None,
    ) -> np.ndarray:
        """(rows, cols) bool of what the source itself declares valid.

        Its alpha band if it has one (nonzero), else its internal or per-dataset mask, else, with
        an automatic nodata, GDAL's band masks from the nodata value; valid in every data band.
        """
        if header.alpha is not None:
            return src.read(header.alpha, window=window) != 0
        if header.internal_mask or (
            self._auto_nodata and any(token is not None for token in header.nodata)
        ):
            masks = src.read_masks(list(header.data_indexes), window=window)
            return (masks != 0).all(axis=0)
        if window is None:
            return np.ones((src.height, src.width), dtype=bool)
        (row0, row1), (col0, col1) = window
        return np.ones((row1 - row0, col1 - col0), dtype=bool)

    @staticmethod
    def _source_grid(src: Any, header: _CogHeader) -> dict[str, Any]:
        """`reproject` keywords for the source grid: the file's own or the one from options."""
        from rasterio.transform import Affine

        if header.own_grid:
            return {"src_transform": src.transform, "src_crs": src.crs}
        return {"src_transform": Affine(*header.grid.transform), "src_crs": header.grid.crs}

    def _read_warped(
        self, src: Any, header: _CogHeader, data: np.ndarray, valid: np.ndarray | None
    ) -> None:
        import rasterio
        from rasterio.enums import Resampling
        from rasterio.transform import Affine
        from rasterio.warp import reproject

        assert self.resampling is not None
        grid = self.grid
        fill = 0 if self.info.nodata is None else self.info.nodata
        data[:] = fill
        if self._auto_nodata:
            src_nodata = header.warp_nodata
        else:
            src_nodata = self._sentinels[0] if self._sentinels else None
        # A frame whose CRS or transform came from --crs, a world file or --bounds has them only in
        # `header.grid`, so its pixels are handed over with that grid instead of as a dataset band.
        source_grid = self._source_grid(src, header)
        if header.own_grid:
            source, source_options = rasterio.band(src, list(header.data_indexes)), {}
        else:
            source, source_options = src.read(list(header.data_indexes)), source_grid
        reproject(
            source=source,
            destination=data,
            src_nodata=src_nodata,
            **source_options,
            dst_transform=Affine(*grid.transform),
            dst_crs=grid.crs,
            dst_nodata=fill,
            resampling=_parse_resampling(self.resampling),
        )
        if valid is not None:
            # Validity is resampled by nearest neighbour; outside the footprint stays 0.
            warped = np.zeros(valid.shape, dtype=np.uint8)
            reproject(
                source=self._declared_valid(src, header, None).astype(np.uint8),
                destination=warped,
                **source_grid,
                src_nodata=None,
                dst_transform=Affine(*grid.transform),
                dst_crs=grid.crs,
                dst_nodata=0,
                resampling=Resampling.nearest,
            )
            valid[:] = (warped != 0) & _value_valid(data, self._sentinels)


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
        mask_var: str | None = None,
    ) -> None:
        self.kind = "NetCDF file" if source.lower().endswith(NETCDF_SUFFIXES) else "Zarr store"
        self.source = source
        self.variable_name = variable
        dataset = _open_dataset(source)
        names = [n for n in dataset.data_vars if n != mask_var]
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

        self._mask = None
        if mask_var is not None:
            self._mask = self._mask_variable(dataset, mask_var, variable, resolved)
        declared = {
            token
            for key in ("_FillValue", "missing_value")
            for value in np.atleast_1d(da.attrs.get(key, [])).tolist()
            if (token := _declared_nodata(value, da.dtype)) is not None
        }
        validity = _decide_validity(
            da.dtype,
            declared,
            override=nodata,
            explicit=[] if mask_var is None else [f"mask variable {mask_var}"],
            footprint=False,
        )
        self._sentinels: tuple[float | int, ...]
        if nodata == "auto":
            self._sentinels = tuple(t for t in declared if not isinstance(t, str))
        else:
            self._sentinels = () if validity.nodata is None else (validity.nodata,)

        if "band" in resolved:
            band_names = tuple(str(b) for b in da[resolved["band"]].values)
        else:
            band_names = (variable,)
        scale, offset = _scaling(
            self._scalar_attr(da, "scale_factor", 1.0),
            self._scalar_attr(da, "add_offset", 0.0),
            f"variable {variable!r}",
        )
        units = da.attrs.get("units")
        bands = tuple(
            Band(name=n, scale=scale, offset=offset, units=str(units) if units else None)
            for n in band_names
        )
        self.da = da
        self.info = SourceInfo(
            grid,
            len(band_names),
            da.dtype,
            validity.nodata,
            band_names,
            bands,
            mask=validity.mask,
            validity=validity.why,
        )

    @staticmethod
    def _scalar_attr(da: xr.DataArray, key: str, default: float) -> float:
        raw = da.attrs.get(key)
        if raw is None:
            return default
        values = np.atleast_1d(raw)
        if values.size != 1 or values.dtype.kind not in "iuf":
            raise ValueError(
                f"{da.name}: {key} must be one number (CF attributes are per variable), "
                f"got {raw!r}"
            )
        return float(values[0])

    def _mask_variable(
        self, dataset: xr.Dataset, name: str, variable: str, resolved: dict[str, str]
    ) -> xr.DataArray:
        if name not in dataset.variables:
            raise ValueError(
                f"mask variable {name!r} is not in {self.source}: "
                f"{sorted(str(v) for v in dataset.variables)}"
            )
        if name == variable:
            raise ValueError(f"mask variable {name!r} is the data variable; name another")
        mask = dataset[name]
        wanted = {resolved[k] for k in ("time", "y", "x")}
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
            "bands": [b.to_attrs() for b in self.info.bands],
            "times": [str(t) for t in self.times],
            "dtype": self.info.dtype.name,
            "nodata": self.info.nodata,
            "mask": self.info.mask,
            "validity": self.info.validity,
            "mask_var": None if self._mask is None else self._mask.name,
        }

    def read(self, t: int) -> Step:
        d = self._dims
        piece = self.da.isel({d["time"]: self._order[t]})
        order = [d["band"], d["y"], d["x"]] if "band" in d else [d["y"], d["x"]]
        values = np.asarray(piece.transpose(*order).values)
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
            if self._mask is not None:
                plane = self._mask.isel({d["time"]: self._order[t]}).transpose(d["y"], d["x"])
                ok &= (plane.values[::-1] if self._flip_y else plane.values) != 0
            valid = ok.astype(np.uint8)
        _settle_nan(values, valid, self.info.nodata, f"{self.source} timestep {t}")
        return Step(values, valid)


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
    samples: dict[int, Step] = field(default_factory=dict, repr=False)

    def lines(self, read_ahead: int = 2) -> list[str]:
        info = self.source.info
        times = self.source.times
        out = [
            f"source:     {self.source.kind}, {self.n_time} timesteps "
            f"({np.datetime_as_string(times[0], unit='D')} .. "
            f"{np.datetime_as_string(times[-1], unit='D')})",
            f"grid:       {info.grid.describe()}",
            f"data:       {info.n_band} bands ({', '.join(info.band_names)}), {info.dtype.name}",
            "scaling:    "
            + ", ".join(
                f"{b.name} = stored * {b.scale:g} {b.offset:+g}"
                + (f" [{b.units}]" if b.units else "")
                for b in info.bands
            ),
            f"validity:   {info.validity}",
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


def _sample_ratio(samples: Sequence[Step], chunk_size: int) -> float:
    """Mean zstd-5 compressed/raw ratio of the top-left cell of each sampled timestep."""
    codec = Zstd(level=5)
    ratios = []
    for step in samples:
        window = np.ascontiguousarray(step.data[:, :chunk_size, :chunk_size])
        ratios.append(len(codec.encode(window)) / window.nbytes)
    return float(np.mean(ratios))


def _sample_indices(n: int) -> list[int]:
    return sorted({0, n // 2, n - 1})[:SAMPLE_TIMESTEPS]


@_tolerate_unlocated_frames
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
    mask_var: str | None = None,
    bounds: Sequence[float] | None = None,
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
        if variable is not None or dims is not None or mask_var is not None:
            raise ValueError(
                "--variable, --dims and --mask-var apply to Zarr and NetCDF input, not manifests"
            )
        manifest = read_manifest(Path(text))
        if bounds is not None and manifest.bounds is not None:
            raise ValueError(
                f"{text} has bounds and bounds were also passed (--bounds); give them once"
            )
        frame_bounds = manifest.bounds if bounds is None else check_bounds(bounds, "--bounds")
        if frame_bounds is not None and crs is None:
            raise ValueError("bounds need crs (--crs EPSG:xxxxx), the CRS they are in")
        source = CogManifestSource(
            manifest.entries,
            manifest.bands,
            target_crs=crs,
            target_transform=None
            if transform is None
            else _check_north_up(transform, "--transform"),
            target_shape=shape,
            resampling=resampling,
            nodata=nodata,
            chunk_size=chunk_size,
            bounds=frame_bounds,
        )
    else:
        if transform is not None or shape is not None or resampling is not None or bounds:
            raise ValueError(
                "--transform, --shape, --bounds and --resampling apply to manifests of COGs or "
                "image frames; a Zarr or NetCDF input keeps the grid of its x/y coordinates "
                "(--crs only declares its CRS)"
            )
        source = XarraySource(text, variable, _parse_dims(dims), crs, nodata, mask_var)

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


def _read_checked(source: Source, t: int) -> Step:
    info = source.info
    step = source.read(t)
    expected = (info.n_band, info.grid.height, info.grid.width)
    if step.data.shape != expected or step.data.dtype != info.dtype:
        raise ValueError(
            f"timestep {t} came back as {step.data.dtype}{step.data.shape}; "
            f"expected {info.dtype}{expected}"
        )
    plane = (info.grid.height, info.grid.width)
    if info.mask != (step.valid is not None) or (
        step.valid is not None and (step.valid.shape != plane or step.valid.dtype != np.uint8)
    ):
        raise ValueError(
            f"timestep {t} came back with the wrong validity plane for: {info.validity}"
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


def _staged_mask_path(work: Path, t: int) -> Path:
    return work / f"m{t:06d}.npy"


def _is_valid_file(path: Path, shape: tuple[int, ...], dtype: np.dtype) -> bool:
    if not path.is_file():
        return False
    try:
        staged = np.load(path, mmap_mode="r")
    except (ValueError, OSError):
        return False
    return staged.shape == shape and staged.dtype == dtype


def _is_staged(work: Path, t: int, plan: Plan) -> bool:
    info = plan.source.info
    plane = (info.grid.height, info.grid.width)
    return _is_valid_file(_staged_path(work, t), (info.n_band, *plane), info.dtype) and (
        not info.mask or _is_valid_file(_staged_mask_path(work, t), plane, np.dtype(np.uint8))
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
    todo = [t for t in range(plan.n_time) if not _is_staged(work, t, plan)]
    reused = plan.n_time - len(todo)
    if progress is not None:
        progress(reused, plan.n_time)

    def read_one(t: int) -> Step:
        if t in plan.samples:
            return plan.samples.pop(t)
        return _read_checked(plan.source, t)

    def save(target: Path, array: np.ndarray) -> None:
        temporary = target.with_suffix(".npy.part")
        with temporary.open("wb") as handle:
            np.save(handle, array)
        temporary.replace(target)

    done = reused
    with ThreadPoolExecutor(max_workers=read_ahead) as pool:
        pending: deque[tuple[int, Future[Step]]] = deque()
        queue = iter(todo)
        for t in queue:
            pending.append((t, pool.submit(read_one, t)))
            if len(pending) >= read_ahead:
                break
        while pending:
            t, future = pending.popleft()
            step = future.result()
            if step.valid is not None:
                save(_staged_mask_path(work, t), step.valid)
            save(_staged_path(work, t), step.data)
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


def _staged_masks(plan: Plan, work: Path) -> Iterator[np.ndarray]:
    for t in range(plan.n_time):
        yield np.load(_staged_mask_path(work, t))


@_tolerate_unlocated_frames
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
    mask_var: str | None = None,
    bounds: Sequence[float] | None = None,
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
    case sources off the grid are warped with the explicit `resampling`.

    Image frames (PNG) are manifest sources. `crs` names the CRS of frames that carry none (a
    world file has none). `bounds` is `(west, south, east, north)` in the units of `crs`, the
    extent of every frame, for frames with no geotransform (no world file, no `.aux.xml`): the
    transform is derived from the pixel size, all frames must have the same size, and a frame
    with its own geotransform is refused. A JSON manifest can hold `"bounds"` instead.

    Scale, offset, units and validity follow the fidelity rules of the module docstring.
    `nodata` is "auto" (what the sources declare; none declared means no nodata, not 0), a
    number that replaces the declared nodata, None (no nodata) or NaN (float data: NaN pixels are
    invalid, through a mask). `mask_var` names a boolean or integer (time, y, x) variable of a
    Zarr or NetCDF source whose nonzero values are valid; the store then gets a mask.

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
        mask_var=mask_var,
        bounds=bounds,
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
            nodata=info.nodata,
            mask=_staged_masks(plan, work) if info.mask else None,
            **encode_options,
        )
        encode_s = time.perf_counter() - started
    except BaseException as exc:
        exc.add_note(f"staged timesteps are kept in {work}; rerun with --resume to reuse them")
        raise
    shutil.rmtree(work)
    return ConvertReport(plan, report, staged, reused, read_s, encode_s)
