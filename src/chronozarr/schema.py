"""chronozarr schema: attribute dataclasses, layout helpers, and store validation.

The layout is documented in spec/CHRONOZARR.md. This module owns
everything both the writer and the reader must agree on: attribute names,
pyramid geometry, dtype profiles, and the checks that decide whether a Zarr v3 store is a
conforming chronozarr store. Versions 0.3.0 and 0.4.0 are accepted; older stores need explicit
conversion.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from itertools import pairwise
from typing import Any, cast

import numpy as np
import zarr
from zarr.core.sync import sync
from zarr.errors import GroupNotFoundError

from chronozarr.crs import CODE_RE, Crs, same_crs
from chronozarr.store import IndexStore, as_store, check_extensions

SPEC_VERSION = "0.3.0"
SPEC_VERSION_ANY_CRS = "0.4.0"
SPEC_VERSIONS = (SPEC_VERSION, SPEC_VERSION_ANY_CRS)  # what readers accept
VARIABLE = "data"
MASK_VARIABLE = "mask"
COVERAGE_VARIABLE = "coverage"
VOLATILITY_PATH = "volatility"
NODATA = 0
DIMENSIONS = ("time", "band", "y", "x")
PLANE_DIMENSIONS = ("time", "y", "x")  # the mask and coverage variables
DTYPES = ("uint8", "uint16", "int16", "float32")
GAP_FILLS = ("carry-forward", "none")
CODECS = ("zstd", "gzip", "blosc")
TIME_UNITS = "milliseconds since 1970-01-01T00:00:00"
TIME_CALENDAR = "proleptic_gregorian"

Transform = tuple[float, float, float, float, float, float]


class SchemaError(ValueError):
    """A store or attribute block does not conform to chronozarr."""


# --- Layout helpers -------------------------------------------------------------------------


def comparison_schedule(n_time: int, interval: int = 6) -> dict[int, int]:
    """Nominal volatility comparisons; independent of storage and decoding."""
    comparisons = list(range(0, n_time, interval))
    return {
        t: min(comparisons, key=lambda q: (abs(q - t), q))
        for t in range(n_time)
        if t not in comparisons
    }


def scale_transform(transform: Transform, level: int) -> Transform:
    """Transform of pyramid level `level`: pixel size doubles per level, origin is preserved."""
    a, b, c, d, e, f = transform
    factor = 2**level
    return (a * factor, b, c, d, e * factor, f)


def grid_shape(height: int, width: int, chunk_size: int) -> tuple[int, int]:
    """Number of (rows, cols) of cells covering a height x width level."""
    return math.ceil(height / chunk_size), math.ceil(width / chunk_size)


def level_shapes(
    height: int, width: int, chunk_size: int, n_lods: int | None = None
) -> list[tuple[int, int]]:
    """Spatial (height, width) of each pyramid level.

    Each level is ceil(previous / 2). With n_lods=None the pyramid stops at the first level whose
    cell grid is 1 x 1 (that level is included).
    """
    shapes = [(height, width)]
    while True:
        if n_lods is None:
            if grid_shape(*shapes[-1], chunk_size) == (1, 1):
                return shapes
        elif len(shapes) == n_lods:
            return shapes
        elif shapes[-1] == (1, 1):
            raise ValueError(
                f"n_lods={n_lods} is too large: level {len(shapes) - 1} is already 1 x 1 pixel"
            )
        prev_h, prev_w = shapes[-1]
        shapes.append((math.ceil(prev_h / 2), math.ceil(prev_w / 2)))


def bounds(transform: Transform, height: int, width: int) -> tuple[float, float, float, float]:
    """Projected (xmin, ymin, xmax, ymax) of a north-up grid."""
    a, _, c, _, e, f = transform
    return (c, f + e * height, c + a * width, f)


def pixel_centers(transform: Transform, height: int, width: int) -> tuple[np.ndarray, np.ndarray]:
    """Projected (y, x) coordinates of pixel centres for a north-up grid."""
    a, _, c, _, e, f = transform
    x = c + (np.arange(width) + 0.5) * a
    y = f + (np.arange(height) + 0.5) * e
    return y, x


def parse_time(value: str) -> np.datetime64:
    """Parse an ISO-8601 time string as written by the encoder (`...Z` suffix allowed)."""
    try:
        return np.datetime64(value.removesuffix("Z"), "ms")
    except ValueError as exc:
        raise SchemaError(f"time '{value}' is not an ISO-8601 date or datetime") from exc


def shard_key(level: str, variable: str, t_shard: int, row: int, col: int) -> str:
    """Store key of one shard object of a sharded array."""
    return f"{level}/{variable}/c/{t_shard}/0/{row}/{col}"


# --- Attribute dataclasses ------------------------------------------------------------------


@dataclass(frozen=True)
class Band:
    """One band: name plus optional physical-value metadata (value = stored * scale + offset)."""

    name: str
    common_name: str | None = None
    scale: float | None = None
    offset: float | None = None
    units: str | None = None

    def to_attrs(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"name": self.name}
        for key in ("common_name", "scale", "offset", "units"):
            value = getattr(self, key)
            if value is not None:
                attrs[key] = value
        return attrs


@dataclass(frozen=True)
class LevelSummary:
    """One entry of `chronozarr.levels`, mirroring the level attributes and array shape."""

    path: str
    resolution: float
    transform: Transform
    shape: tuple[int, int, int, int]
    grid: tuple[int, int]

    def to_attrs(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "resolution": self.resolution,
            "transform": list(self.transform),
            "shape": list(self.shape),
            "grid": list(self.grid),
        }


@dataclass(frozen=True)
class Chronozarr:
    """The `chronozarr` block of the root group attributes."""

    times: tuple[str, ...]
    bands: tuple[Band, ...]
    crs: str | None  # the code as a mirror of proj:code; None when the store carries no mirror
    spec_version: str = SPEC_VERSION
    variable: str = VARIABLE
    nodata: int | float | None = NODATA
    volatility_path: str | None = None
    mask_variable: str | None = None
    coverage_variable: str | None = None
    provenance: Mapping[str, Any] | None = None
    levels: tuple[LevelSummary, ...] | None = None
    shard_bytes: Mapping[str, Mapping[str, int]] | None = None

    @property
    def band_names(self) -> tuple[str, ...]:
        return tuple(b.name for b in self.bands)

    def to_attrs(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "spec_version": self.spec_version,
            "variable": self.variable,
            "times": list(self.times),
            "bands": [b.to_attrs() for b in self.bands],
            "band_names": list(self.band_names),
            "nodata": self.nodata,
        }
        if self.crs is not None:
            attrs["crs"] = self.crs
        if self.volatility_path is not None:
            attrs["volatility_path"] = self.volatility_path
        if self.mask_variable is not None:
            attrs["mask_variable"] = self.mask_variable
        if self.coverage_variable is not None:
            attrs["coverage_variable"] = self.coverage_variable
        if self.provenance is not None:
            attrs["provenance"] = dict(self.provenance)
        if self.levels is not None:
            attrs["levels"] = [lv.to_attrs() for lv in self.levels]
        if self.shard_bytes is not None:
            attrs["shard_bytes"] = {k: dict(v) for k, v in self.shard_bytes.items()}
        return attrs


@dataclass(frozen=True)
class LevelRef:
    """One ordered multiscales layout asset.

    Writers emit only `path` and `crs`. A `pixels_per_tile` key left by an earlier writer is
    ignored (spec 3.4): zarr-layer reads its presence as "global Web Mercator pyramid", and the
    cell size is the chunk shape of the data arrays (`cell_size`).
    """

    path: str
    crs: str | None


@dataclass(frozen=True)
class RootAttrs:
    chronozarr: Chronozarr
    datasets: tuple[LevelRef, ...]

    def to_attrs(self) -> dict[str, Any]:
        layout = [
            {"asset": d.path}
            if i == 0
            else {
                "asset": d.path,
                "derived_from": self.datasets[i - 1].path,
                "transform": {"scale": [2, 2], "translation": [0, 0]},
            }
            for i, d in enumerate(self.datasets)
        ]
        return {
            "zarr_conventions": [
                {"name": "chronozarr", "spec_url": PROFILE_URL},
                registration("multiscales"),
            ],
            "multiscales": {"layout": layout, "resampling_method": "average"},
            "chronozarr": self.chronozarr.to_attrs(),
        }


PROFILE_URL = "https://github.com/chronozarr/chronozarr/blob/main/spec/CHRONOZARR.md"
CONVENTIONS = {
    "multiscales": "d35379db-88df-4056-af3a-620245f8e347",
    "proj": "f17cb550-5864-4468-aeb7-f3180cfb622f",
    "spatial": "689b58e2-cf7b-45e0-9fff-9cfc0883d6b4",
}


def registration(name: str) -> dict[str, str]:
    return {
        "name": name,
        "uuid": CONVENTIONS[name],
        "schema_url": f"https://raw.githubusercontent.com/zarr-conventions/{name}/refs/tags/v0.1/schema.json",
        "spec_url": f"https://github.com/zarr-conventions/{name}/blob/v0.1/README.md",
    }


def check_registrations(attrs: Mapping[str, Any], names: Sequence[str], where: str) -> None:
    registrations = attrs.get("zarr_conventions")
    if not isinstance(registrations, list):
        raise _fail(where, "missing zarr_conventions registrations")
    for name in names:
        expected = (
            registration(name) if name != "chronozarr" else {"name": name, "spec_url": PROFILE_URL}
        )
        matches = [r for r in registrations if isinstance(r, dict) and r.get("name") == name]
        if len(matches) != 1 or any(matches[0].get(k) != v for k, v in expected.items()):
            raise _fail(where, f"invalid {name} registration; expected {expected}")


@dataclass(frozen=True)
class LevelAttrs:
    """Attributes of a level group (`{level}/zarr.json`); `crs` is the optional code mirror."""

    crs: str | None
    transform: Transform
    resolution: float

    def to_attrs(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "transform": list(self.transform),
            "resolution": self.resolution,
        }
        if self.crs is not None:
            attrs = {"crs": self.crs, **attrs}
        return attrs


@dataclass(frozen=True)
class LevelGeometry:
    """What a level's data array says about where it is: the stored CRS and the transform."""

    crs: Crs
    transform: Transform

    @property
    def resolution(self) -> float:
        return self.transform[0]


_EPSG_RE = re.compile(r"^EPSG:(\d+)$")


def spec_version_for(crs: Crs) -> str:
    """The version a writer declares: 0.3.0 for a CRS with an EPSG code, else 0.4.0.

    Readers up to chronozarr 0.4.1 refuse any other version, and they need an EPSG code, so a
    store keeps the version they open whenever it can.
    """
    return SPEC_VERSION if crs.epsg is not None else SPEC_VERSION_ANY_CRS


def _epsg_url(epsg: int) -> str:
    return f"http://www.opengis.net/def/crs/EPSG/0/{epsg}"


def crs_attr(crs: Crs) -> dict[str, str] | None:
    """The `_CRS` array attribute GDAL's Zarr driver reads: an OGC URL for an EPSG code, and WKT.

    The WKT is the CRS's WKT2 when it has one. A CRS with neither returns None (no `_CRS`).
    """
    attr: dict[str, str] = {}
    if crs.epsg is not None:
        attr["url"] = _epsg_url(crs.epsg)
    if crs.wkt2 is not None:
        attr["wkt"] = crs.wkt2
    return attr or None


_UNIT_SYMBOLS = {"metre": "m", "meter": "m"}


def axis_unit(crs: str) -> str:
    """The unit of the CRS's horizontal axes as the CRS names it: `m`, `degree`, `US survey foot`.

    rasterio reads it from PROJ; metres are written `m` and every other unit as named. Without
    rasterio (`chronozarr[geo]`) the unit is not known, and `CRS-unit` says so instead of a guess.
    """
    try:
        from rasterio.crs import CRS  # ty: ignore[unresolved-import]  # compiled, no stubs
    except ImportError:
        return "CRS-unit"
    unit = CRS.from_user_input(crs).units_factor[0]
    return _UNIT_SYMBOLS.get(unit, unit)


def data_array_attrs(
    crs: Crs,
    transform: Transform,
    height: int,
    width: int,
    nodata: int | float | None = NODATA,
    *,
    dimensions: Sequence[str] = DIMENSIONS,
) -> dict[str, Any]:
    """Attributes of a level's data-like array: `proj:`, `spatial:` and GDAL's `_CRS`.

    `proj:wkt2` is written whenever the CRS has one and `proj:code` and the `crs` alias whenever
    it has a code. `nodata` is omitted for arrays that are not the data variable (mask, coverage).
    """
    attrs: dict[str, Any] = {"_ARRAY_DIMENSIONS": list(dimensions), "nodata": nodata}
    if crs.code is not None:
        attrs["crs"] = crs.code
    attrs["transform"] = list(transform)
    if crs.code is not None:
        attrs["proj:code"] = crs.code
    if crs.wkt2 is not None:
        attrs["proj:wkt2"] = crs.wkt2
    if crs.projjson is not None:
        attrs["proj:projjson"] = dict(crs.projjson)
    attrs.update(
        {
            "zarr_conventions": [registration("proj"), registration("spatial")],
            "spatial:registration": "pixel",
            "spatial:transform_type": "affine",
            "spatial:dimensions": ["y", "x"],
            "spatial:shape": [height, width],
            "spatial:transform": list(transform),
            "spatial:bbox": list(bounds(transform, height, width)),
        }
    )
    if nodata is None:
        attrs.pop("nodata")
    gdal_crs = crs_attr(crs)
    if gdal_crs is not None:
        attrs["_CRS"] = dict(gdal_crs)
    return attrs


# --- Parsing --------------------------------------------------------------------------------


def _fail(where: str, message: str) -> SchemaError:
    return SchemaError(f"{where}: {message}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _require(mapping: object, key: str, where: str) -> Any:
    if not isinstance(mapping, Mapping):
        raise _fail(where, f"expected an object, got {type(mapping).__name__}")
    entries = cast("Mapping[str, Any]", mapping)
    if key not in entries:
        raise _fail(where, f"missing required key '{key}'")
    return entries[key]


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _fail(where, "expected a non-empty list of strings")
    strings = [v for v in value if isinstance(v, str)]
    if len(strings) != len(value):
        raise _fail(where, "expected a list of strings")
    return tuple(strings)


def _optional_str(mapping: Mapping[str, Any], key: str, where: str) -> str | None:
    value = mapping.get(key)
    if value is not None and (not isinstance(value, str) or not value):
        raise _fail(f"{where}.{key}", f"expected a non-empty string, got {value!r}")
    return value


def _optional_number(mapping: Mapping[str, Any], key: str, where: str) -> float | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not _is_number(value) or not math.isfinite(value):
        raise _fail(f"{where}.{key}", f"expected a finite number, got {value!r}")
    return float(value)


def parse_crs(attrs: Mapping[str, Any], where: str) -> Crs:
    """The CRS of an array's attributes: `proj:code`, `proj:wkt2` and `proj:projjson` (any).

    A key that is absent or null is not given. proj v0.1 asks for at least one of the three.
    """
    code = attrs.get("proj:code")
    wkt2 = attrs.get("proj:wkt2")
    projjson = attrs.get("proj:projjson")
    if code is None and wkt2 is None and projjson is None:
        raise _fail(where, "needs one of proj:code, proj:wkt2 or proj:projjson")
    if code is not None and (not isinstance(code, str) or CODE_RE.fullmatch(code) is None):
        raise _fail(
            where, f"proj:code must be 'authority:code' such as 'EPSG:32631', got {code!r}"
        )
    if wkt2 is not None and (not isinstance(wkt2, str) or not wkt2.strip()):
        raise _fail(where, "proj:wkt2 must be a non-empty string")
    if projjson is not None and not isinstance(projjson, Mapping):
        raise _fail(where, f"proj:projjson must be an object, got {type(projjson).__name__}")
    return Crs(
        code=code,
        wkt2=wkt2,
        projjson=None if projjson is None else dict(cast("Mapping[str, Any]", projjson)),
    )


def check_crs_mirror(
    label: str, mirror: object, reference: Crs, problems: list[str], notes: list[str]
) -> None:
    """A `crs` mirror (a code string) must not contradict the stored CRS.

    A proven difference is a problem. A comparison that needs pyproj, which is not installed, is
    a note: the mirror may still be right.
    """
    if not isinstance(mirror, str) or not mirror:
        problems.append(f"{label}: expected a non-empty string, got {mirror!r}")
        return
    match = same_crs(Crs(code=mirror), reference)
    if match.same is False:
        problems.append(
            f"{label} {mirror!r} differs from the stored CRS (compared by {match.method})"
        )
    elif match.same is None:
        notes.append(
            f"{label} {mirror!r} was not compared with the stored CRS: a code and a WKT2 need "
            "pyproj, which is not installed"
        )


def parse_band(raw: Any, where: str) -> Band:
    """A band given as a name (encoder input) or as an object with optional scale/offset/units."""
    if isinstance(raw, str):
        if not raw:
            raise _fail(where, "band name must not be empty")
        return Band(raw)
    if not isinstance(raw, Mapping):
        raise _fail(where, f"expected a band name or an object, got {raw!r}")
    entries = cast("Mapping[str, Any]", raw)
    name = _require(entries, "name", where)
    if not isinstance(name, str) or not name:
        raise _fail(f"{where}.name", f"expected a non-empty string, got {name!r}")
    return Band(
        name=name,
        common_name=_optional_str(entries, "common_name", where),
        scale=_optional_number(entries, "scale", where),
        offset=_optional_number(entries, "offset", where),
        units=_optional_str(entries, "units", where),
    )


def parse_bands(value: Any, where: str) -> tuple[Band, ...]:
    """Bands as a non-empty list of names or band objects, with unique names."""
    if not isinstance(value, list) or not value:
        raise _fail(where, "expected a non-empty list of band names or band objects")
    bands = tuple(parse_band(v, f"{where}[{i}]") for i, v in enumerate(value))
    names = [b.name for b in bands]
    if len(set(names)) != len(names):
        raise _fail(where, f"band names must be unique, got {names}")
    return bands


def parse_provenance(raw: Any, where: str = "chronozarr.provenance") -> dict[str, Any]:
    """Validate a provenance object: sources, composite and gap_fill, plus optional notes."""
    if not isinstance(raw, Mapping):
        raise _fail(where, f"expected an object, got {type(raw).__name__}")
    entries = cast("Mapping[str, Any]", raw)
    sources = _str_list(_require(entries, "sources", where), f"{where}.sources")
    composite = _require(entries, "composite", where)
    if not isinstance(composite, str) or not composite:
        raise _fail(f"{where}.composite", f"expected a non-empty string, got {composite!r}")
    gap_fill = _require(entries, "gap_fill", where)
    if gap_fill not in GAP_FILLS:
        raise _fail(f"{where}.gap_fill", f"expected one of {list(GAP_FILLS)}, got {gap_fill!r}")
    parsed: dict[str, Any] = {"sources": list(sources), "composite": composite}
    parsed["gap_fill"] = gap_fill
    notes = entries.get("notes")
    if notes is not None:
        if not isinstance(notes, str):
            raise _fail(f"{where}.notes", f"expected a string, got {notes!r}")
        parsed["notes"] = notes
    return parsed


def _parse_transform(raw: Any, where: str) -> Transform:
    if not isinstance(raw, list) or len(raw) != 6 or not all(_is_number(v) for v in raw):
        raise _fail(where, f"expected 6 numbers [a, b, c, d, e, f], got {raw!r}")
    return (
        float(raw[0]),
        float(raw[1]),
        float(raw[2]),
        float(raw[3]),
        float(raw[4]),
        float(raw[5]),
    )


def _parse_int_pair(raw: Any, size: int, where: str) -> tuple[int, ...]:
    if (
        not isinstance(raw, list)
        or len(raw) != size
        or not all(_is_int(v) and v >= 0 for v in raw)
    ):
        raise _fail(where, f"expected {size} non-negative ints, got {raw!r}")
    return tuple(int(v) for v in raw)


def _parse_levels(raw: Any, where: str) -> tuple[LevelSummary, ...]:
    if not isinstance(raw, list) or not raw:
        raise _fail(where, "expected a non-empty list")
    levels = []
    for i, entry in enumerate(raw):
        at = f"{where}[{i}]"
        path = _require(entry, "path", at)
        if path != str(i):
            raise _fail(at, f"levels must be listed as '0', '1', ... in order; got path {path!r}")
        resolution = _require(entry, "resolution", at)
        if not _is_number(resolution) or resolution <= 0:
            raise _fail(f"{at}.resolution", f"expected a positive number, got {resolution!r}")
        shape = _parse_int_pair(_require(entry, "shape", at), 4, f"{at}.shape")
        grid = _parse_int_pair(_require(entry, "grid", at), 2, f"{at}.grid")
        levels.append(
            LevelSummary(
                path=path,
                resolution=float(resolution),
                transform=_parse_transform(_require(entry, "transform", at), f"{at}.transform"),
                shape=cast("tuple[int, int, int, int]", shape),
                grid=cast("tuple[int, int]", grid),
            )
        )
    return tuple(levels)


def _parse_shard_bytes(raw: Any, where: str) -> dict[str, dict[str, int]]:
    if not isinstance(raw, Mapping):
        raise _fail(where, "expected an object keyed by level path")
    parsed: dict[str, dict[str, int]] = {}
    for level, shards in raw.items():
        if not isinstance(shards, Mapping):
            raise _fail(f"{where}.{level}", "expected an object keyed by 't_shard/row/col'")
        parsed[str(level)] = {}
        for key, size in shards.items():
            parts = str(key).split("/")
            if len(parts) != 3 or not all(p.isdecimal() for p in parts):
                raise _fail(f"{where}.{level}", f"key {key!r} is not 't_shard/row/col'")
            if not _is_int(size) or size < 1:
                raise _fail(f"{where}.{level}.{key}", f"expected a byte length > 0, got {size!r}")
            parsed[str(level)][str(key)] = int(size)
    return parsed


def _parse_nodata(value: Any, where: str) -> int | float | None:
    if value is None:
        return None
    if not _is_number(value) or not math.isfinite(cast("float", value)):
        raise _fail(where, f"expected a finite number or null, got {value!r}")
    return cast("int | float", value)


def parse_chronozarr(block: Any, where: str = "chronozarr") -> Chronozarr:
    """Validate and parse the `chronozarr` root attribute block (spec 0.3.0 and 0.4.0)."""
    version = block.get("spec_version") if isinstance(block, Mapping) else None
    if version not in SPEC_VERSIONS:
        raise _fail(
            f"{where}.spec_version",
            f"unsupported or missing version {version!r}; expected {' or '.join(SPEC_VERSIONS)}; "
            "a v0.2 store needs chronozarr convert, a newer store needs a newer chronozarr",
        )
    if "temporal" in block:
        raise _fail(where, "temporal encoding is outside v0.3; use chronozarr convert")
    variable = _require(block, "variable", where)
    if not isinstance(variable, str) or not variable:
        raise _fail(f"{where}.variable", f"expected a non-empty array name, got {variable!r}")
    times = _str_list(_require(block, "times", where), f"{where}.times")
    parsed = [parse_time(t) for t in times]
    if any(later <= earlier for earlier, later in pairwise(parsed)):
        raise _fail(f"{where}.times", "must be strictly increasing")
    bands = parse_bands(_require(block, "bands", where), f"{where}.bands")
    if "band_names" in block and list(block["band_names"] or []) != [b.name for b in bands]:
        raise _fail(f"{where}.band_names", "must list the names of chronozarr.bands in order")
    nodata = _parse_nodata(_require(block, "nodata", where), f"{where}.nodata")
    crs = block.get("crs")  # a mirror of proj:code; readers do not need it
    if crs is not None and (not isinstance(crs, str) or not crs):
        raise _fail(f"{where}.crs", "expected a non-empty string such as 'EPSG:32631'")
    if not all(isinstance(b, Mapping) for b in block["bands"]):
        raise _fail(f"{where}.bands", "v0.3 requires band objects")
    volatility_path = _optional_str(block, "volatility_path", where)
    if volatility_path not in (None, VOLATILITY_PATH):
        raise _fail(where, "volatility_path must be volatility")
    for key, expected in (
        ("mask_variable", MASK_VARIABLE),
        ("coverage_variable", COVERAGE_VARIABLE),
    ):
        if key in block and block[key] != expected:
            raise _fail(where, f"{key} must be {expected}")
    provenance = (
        parse_provenance(block["provenance"], f"{where}.provenance")
        if block.get("provenance") is not None
        else None
    )
    return Chronozarr(
        times=times,
        bands=bands,
        crs=crs,
        spec_version=version,
        variable=variable,
        nodata=nodata,
        volatility_path=volatility_path,
        mask_variable=_optional_str(block, "mask_variable", where),
        coverage_variable=_optional_str(block, "coverage_variable", where),
        provenance=provenance,
        levels=_parse_levels(block["levels"], f"{where}.levels") if "levels" in block else None,
        shard_bytes=(
            _parse_shard_bytes(block["shard_bytes"], f"{where}.shard_bytes")
            if "shard_bytes" in block
            else None
        ),
    )


def _parse_multiscales(attrs: Mapping[str, Any], crs: str | None) -> tuple[LevelRef, ...]:
    multiscales = _require(attrs, "multiscales", "root attributes")
    if not isinstance(multiscales, Mapping):
        raise _fail("multiscales", "expected a layout object")
    raw = _require(multiscales, "layout", "multiscales")
    if not isinstance(raw, list) or not raw:
        raise _fail("multiscales.layout", "expected a non-empty list")
    if multiscales.get("resampling_method") != "average":
        raise _fail("multiscales", "resampling_method must be average")
    datasets = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, Mapping) or entry.get("asset") != str(i):
            raise _fail("multiscales.layout", "assets must be consecutive group paths 0, 1, ...")
        if i and (
            entry.get("derived_from") != str(i - 1)
            or entry.get("transform") != {"scale": [2, 2], "translation": [0, 0]}
        ):
            raise _fail(
                "multiscales.layout", "overview must derive from previous level at scale 2"
            )
        datasets.append(LevelRef(str(i), crs))
    return tuple(datasets)


def parse_root_attrs(attrs: Mapping[str, Any]) -> RootAttrs:
    """Validate and parse the root group attributes (`multiscales` + `chronozarr`)."""
    block = parse_chronozarr(_require(attrs, "chronozarr", "root attributes"))
    check_registrations(attrs, ("chronozarr", "multiscales"), "root attributes")
    return RootAttrs(chronozarr=block, datasets=_parse_multiscales(attrs, block.crs))


def parse_level_attrs(attrs: Mapping[str, Any], where: str) -> LevelAttrs:
    """Validate and parse a level group's attributes; `crs` is an optional mirror."""
    crs = attrs.get("crs")
    if crs is not None and (not isinstance(crs, str) or not crs):
        raise _fail(f"{where}.crs", "expected a non-empty string")
    transform = _parse_transform(_require(attrs, "transform", where), f"{where}.transform")
    resolution = _require(attrs, "resolution", where)
    if not _is_number(resolution) or resolution <= 0:
        raise _fail(f"{where}.resolution", f"expected a positive number, got {resolution!r}")
    return LevelAttrs(crs=crs, transform=transform, resolution=float(resolution))


def canonical_level(data: zarr.Array, where: str) -> LevelGeometry:
    """The CRS and transform a level's data array declares with `proj:` and `spatial:`."""
    a = data.attrs.asdict()
    check_registrations(a, ("proj", "spatial"), where)
    crs = parse_crs(a, where)
    transform = _parse_transform(_require(a, "spatial:transform", where), where)
    if not all(math.isfinite(v) for v in transform) or not (
        transform[0] > 0 and transform[4] < 0 and transform[1] == transform[3] == 0
    ):
        raise _fail(where, "spatial:transform must be finite and north-up")
    if a.get("spatial:dimensions") != ["y", "x"] or a.get("spatial:registration") != "pixel":
        raise _fail(where, "spatial dimensions must be y/x with pixel registration")
    if a.get("spatial:transform_type", "affine") != "affine":
        raise _fail(where, "spatial transform type must be affine")
    return LevelGeometry(crs, transform)


def _check_spatial(
    data: zarr.Array,
    crs: Crs | None,
    transform: Transform,
    h: int,
    w: int,
    where: str,
    problems: list[str],
    notes: list[str],
) -> None:
    """Checks on a data-like array's geometry. `crs` is the level-0 data array's, or None."""
    attrs = data.attrs.asdict()
    try:
        canonical = canonical_level(data, where)
        if not same_numbers(canonical.transform, transform):
            problems.append(f"{where}: canonical geometry differs from level/root mirrors")
        if crs is not None:
            match = same_crs(canonical.crs, crs)
            if match.same is False:
                problems.append(f"{where}: CRS differs from level 0 (compared by {match.method})")
            if "crs" in attrs:
                check_crs_mirror(f"{where}: crs", attrs["crs"], canonical.crs, problems, notes)
    except SchemaError as exc:
        problems.append(str(exc))
    for key, expected in (
        ("transform", list(transform)),
        ("spatial:shape", [h, w]),
        ("proj:shape", [h, w]),
        ("spatial:bbox", list(bounds(transform, h, w))),
        ("proj:bbox", list(bounds(transform, h, w))),
    ):
        if key in attrs and attrs[key] != expected:
            problems.append(f"{where}: {key} differs from geometry")
    try:
        check_codecs(data, where)
    except SchemaError as exc:
        problems.append(str(exc))
    if "scale_factor" in attrs or "add_offset" in attrs:
        problems.append(f"{where}: CF automatic scaling is forbidden")
    _check_crs_attr(attrs, crs, where, problems)


def get_group(parent: zarr.Group, name: str, where: str) -> zarr.Group:
    """Child group `name`, or SchemaError."""
    try:
        member = parent[name]
    except KeyError as exc:
        raise SchemaError(f"{where}: group '{name}' is missing") from exc
    if not isinstance(member, zarr.Group):
        raise SchemaError(f"{where}: '{name}' is not a group")
    return member


def get_array(parent: zarr.Group, name: str, where: str) -> zarr.Array:
    """Child array `name`, or SchemaError."""
    try:
        member = parent[name]
    except KeyError as exc:
        raise SchemaError(f"{where}: array '{name}' is missing") from exc
    if not isinstance(member, zarr.Array):
        raise SchemaError(f"{where}: '{name}' is not an array")
    return member


def cell_size(data: zarr.Array, where: str) -> int:
    """Cell edge `cs` in pixels: the spatial chunk size of a level's data array.

    A sharded array's inner chunks count, so the chunk shape must be `(1, n_band, cs, cs)`.
    The cell size is read from the arrays and never from `pixels_per_tile` (spec 2.1).
    """
    if data.ndim != 4:
        raise SchemaError(
            f"{where}: expected 4 dimensions (time, band, y, x), got shape {data.shape}"
        )
    chunks = tuple(data.chunks)
    cs = chunks[2]
    if chunks != (1, data.shape[1], cs, cs):
        raise SchemaError(f"{where}: chunks must be (1, {data.shape[1]}, cs, cs), got {chunks}")
    return int(cs)


# --- Store validation -----------------------------------------------------------------------


def same_numbers(a: Sequence[Any], b: Sequence[float]) -> bool:
    return len(a) == len(b) and all(
        math.isclose(x, y, rel_tol=1e-12, abs_tol=1e-9) for x, y in zip(a, b, strict=True)
    )


def _check_dims(
    array: zarr.Array, expected: Sequence[str], name: str, problems: list[str]
) -> None:
    names = getattr(array.metadata, "dimension_names", None)  # absent on Zarr v2 metadata
    if names is None or tuple(names) != tuple(expected):
        problems.append(f"{name}: dimension_names must be {list(expected)}, got {names}")
    legacy = array.attrs.get("_ARRAY_DIMENSIONS")
    if legacy != list(expected):
        problems.append(
            f"{name}: attribute _ARRAY_DIMENSIONS must be {list(expected)}, got {legacy}"
        )


def _codec_names(array: zarr.Array) -> list[str]:
    """Names of the byte-level codecs of an array, looking inside a sharding codec."""
    codecs = cast("list[dict[str, Any]]", array.metadata.to_dict().get("codecs", []))
    for codec in codecs:
        if codec.get("name") == "sharding_indexed":
            codecs = cast("list[dict[str, Any]]", codec["configuration"]["codecs"])
    return [str(c.get("name")) for c in codecs]


def check_codecs(array: zarr.Array, where: str) -> None:
    codecs = cast("list[dict[str, Any]]", array.metadata.to_dict()["codecs"])
    if len(codecs) == 1 and codecs[0]["name"] == "sharding_indexed":
        codecs = codecs[0]["configuration"]["codecs"]
    string = array.dtype.kind in "OTU"
    serializer = "vlen-utf8" if string else "bytes"
    if len(codecs) != 2 or codecs[0]["name"] != serializer or codecs[1]["name"] not in CODECS:
        raise _fail(where, f"unsupported codec chain {[c['name'] for c in codecs]}")
    if (
        not string
        and array.dtype.itemsize > 1
        and codecs[0].get("configuration", {}).get("endian") != "little"
    ):
        raise _fail(where, "bytes codec must be little endian")
    codec = codecs[1]
    config = codec.get("configuration", {})
    if codec["name"] == "blosc" and (
        config.get("cname") not in ("zstd", "lz4")
        or config.get("shuffle") not in ("shuffle", "noshuffle")
        or not 0 <= config.get("clevel", -1) <= 9
        or config.get("typesize") != array.dtype.itemsize
        or config.get("blocksize") != 0
    ):
        raise _fail(where, "unsupported blosc configuration")
    if codec["name"] == "gzip" and not 1 <= config.get("level", 0) <= 9:
        raise _fail(where, "gzip level must be 1..9")


@dataclass
class _LevelState:
    """What later levels compare against: level 0 values and the time shard length.

    `crs` is level 0's stored CRS. `notes` collects what is worth saying but does not fail
    validation; `absent` names the places a SHOULD-level `crs` mirror is missing from.
    """

    attrs: LevelAttrs | None = None
    shape: tuple[int, int] | None = None
    cs: int | None = None
    shard_time: int | None = None
    sharded: bool | None = None
    dtype: str | None = None
    crs: Crs | None = None
    notes: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)


def _check_crs_attr(
    attrs: Mapping[str, Any], crs: Crs | None, where: str, problems: list[str]
) -> None:
    """`_CRS` (GDAL), when present, must name the store CRS by URL and carry WKT as a string."""
    if "_CRS" not in attrs:
        return
    value = attrs["_CRS"]
    expected_url = _epsg_url(crs.epsg) if crs is not None and crs.epsg is not None else None
    if not isinstance(value, Mapping):
        problems.append(f"{where}: attribute _CRS must be an object, got {value!r}")
    elif expected_url is not None and "url" in value and value["url"] != expected_url:
        problems.append(
            f"{where}: attribute _CRS url must be {expected_url!r}, got {value['url']!r}"
        )
    elif "wkt" in value and not isinstance(value["wkt"], str):
        problems.append(f"{where}: attribute _CRS wkt must be a string, got {value['wkt']!r}")


def _check_layout(
    array: zarr.Array,
    name: str,
    lead: tuple[int, ...],
    cs: int,
    state: _LevelState,
    problems: list[str],
) -> None:
    """Chunks (1, *lead, cs, cs) and, if sharded, shards (shard_time, *lead, cs, cs)."""
    metadata = array.metadata.to_dict()
    if metadata.get("chunk_grid", {}).get("name") != "regular":
        problems.append(f"{name}: grid must be regular")
    if metadata.get("chunk_key_encoding") != {
        "name": "default",
        "configuration": {"separator": "/"},
    }:
        problems.append(f"{name}: chunk keys must use default slash encoding")
    chunks = (1, *lead, cs, cs)
    if tuple(array.chunks) != chunks:
        problems.append(f"{name}: chunks must be {chunks}, got {tuple(array.chunks)}")
    if array.shards is None:
        if state.sharded:
            problems.append(f"{name}: must be sharded like the data array")
        return
    if state.sharded is False:
        problems.append(f"{name}: must not be sharded when the data array is not")
    shards = tuple(array.shards)
    if shards[1:] != (*lead, cs, cs) or shards[0] < 1:
        problems.append(
            f"{name}: shards must be (shard_time, {', '.join(str(n) for n in (*lead, cs, cs))}) "
            f"with shard_time >= 1, got {shards}"
        )
    elif state.shard_time is not None and shards[0] != state.shard_time:
        problems.append(
            f"{name}: shard_time {shards[0]} differs from level 0 ({state.shard_time})"
        )


def _check_plane(
    group: zarr.Group,
    variable: str,
    shape: tuple[int, int, int],
    cs: int,
    crs: Crs | None,
    state: _LevelState,
    prefix: str,
    problems: list[str],
) -> None:
    """A mask or coverage variable: uint8 (time, y, x), chunked like the data array."""
    where = f"{prefix}/{variable}"
    try:
        array = get_array(group, variable, prefix)
    except SchemaError as exc:
        problems.append(str(exc))
        return
    _check_dims(array, PLANE_DIMENSIONS, where, problems)
    if array.dtype != np.dtype("uint8"):
        problems.append(f"{where}: dtype must be uint8, got {array.dtype}")
    if tuple(array.shape) != shape:
        problems.append(f"{where}: shape must be {shape}, got {tuple(array.shape)}")
        return
    _check_layout(array, where, (), cs, state, problems)
    if array.fill_value != 0:
        problems.append(f"{where}: fill_value must be 0, got {array.fill_value}")
    _check_crs_attr(array.attrs.asdict(), crs, where, problems)
    if not set(_codec_names(array)) <= {"bytes", *CODECS} or len(_codec_names(array)) != 2:
        problems.append(f"{where}: codecs must be bytes plus one of {list(CODECS)}")


def _check_data_array(
    data: zarr.Array,
    where: str,
    meta: Chronozarr,
    base_shape: tuple[int, int] | None,
    index: int,
    state: _LevelState,
    problems: list[str],
) -> tuple[int, int] | None:
    """Checks on `{level}/data`. Returns its spatial shape, or None if it is not 4-D."""
    _check_dims(data, DIMENSIONS, where, problems)
    if data.dtype.name not in DTYPES:
        problems.append(f"{where}: dtype must be one of {list(DTYPES)}, got {data.dtype}")
    if data.ndim != 4:
        problems.append(f"{where}: expected 4 dimensions (time, band, y, x), got {data.ndim}")
        return None
    if state.dtype is None:
        state.dtype = data.dtype.name
    elif data.dtype.name != state.dtype:
        problems.append(f"{where}: dtype differs from level 0")
    n_time, n_band, height, width = data.shape
    if min(data.shape) <= 0:
        problems.append(f"{where}: axis lengths must be positive")
    if n_time != len(meta.times):
        problems.append(f"{where}: {n_time} timesteps but chronozarr.times has {len(meta.times)}")
    if n_band != len(meta.bands):
        problems.append(f"{where}: {n_band} bands but chronozarr.bands has {len(meta.bands)}")
    if base_shape is not None:
        expected = (math.ceil(base_shape[0] / 2**index), math.ceil(base_shape[1] / 2**index))
        if (height, width) != expected:
            problems.append(f"{where}: spatial shape {(height, width)} should be {expected}")
    if index == 0:
        state.sharded = data.shards is not None
        state.shard_time = data.shards[0] if data.shards is not None else None
    if state.cs is None:
        state.cs = int(data.chunks[2])  # the cell size is the chunk size of the first level read
    if state.cs < 2 or state.cs % 2:
        problems.append(f"{where}: cell size must be positive and even")
    _check_layout(data, where, (n_band,), state.cs, state, problems)
    names = _codec_names(data)
    if len(names) != 2 or names[0] != "bytes" or names[1] not in CODECS:
        problems.append(f"{where}: codecs must be bytes plus one of {list(CODECS)}, got {names}")
    if meta.nodata is not None:
        if data.dtype.kind in "ui":
            limits = np.iinfo(data.dtype)
            if int(meta.nodata) != meta.nodata or not limits.min <= meta.nodata <= limits.max:
                problems.append(f"{where}: nodata is not representable in dtype")
        elif (
            not np.isfinite(np.float64(meta.nodata)) or abs(meta.nodata) > np.finfo(np.float32).max
        ):
            problems.append(f"{where}: nodata is not representable in float32")
    fill = meta.nodata if meta.nodata is not None else 0
    if data.fill_value != fill:
        problems.append(f"{where}: fill_value must equal nodata ({fill}), got {data.fill_value}")
    return height, width


def _check_level(
    root: zarr.Group,
    index: int,
    dataset: LevelRef,
    meta: Chronozarr,
    state: _LevelState,
    problems: list[str],
) -> None:
    """Check one level group; level 0 fills `state` for the later levels."""
    prefix = f"level {dataset.path}"
    try:
        group = get_group(root, dataset.path, "store")
        attrs = parse_level_attrs(group.attrs.asdict(), prefix)
    except SchemaError as exc:
        problems.append(str(exc))
        return

    for name, declaration in (
        (MASK_VARIABLE, meta.mask_variable),
        (COVERAGE_VARIABLE, meta.coverage_variable),
    ):
        if name in group and declaration is None:
            problems.append(f"{prefix}/{name}: array present without root declaration")
    if attrs.crs is None:
        state.absent.append(prefix)
    if state.attrs is not None and not same_numbers(
        attrs.transform, scale_transform(state.attrs.transform, index)
    ):
        problems.append(
            f"{prefix}: transform {list(attrs.transform)} is not the level-0 transform scaled "
            f"by 2^{index}"
        )

    members: dict[str, zarr.Array] = {}
    for name in (meta.variable, "time", "band", "x", "y"):
        try:
            members[name] = get_array(group, name, prefix)
        except SchemaError as exc:
            problems.append(str(exc))
    for name in ("time", "band", "x", "y"):
        if name in members:
            _check_dims(members[name], (name,), f"{prefix}/{name}", problems)
    if meta.variable not in members:
        if index == 0:
            state.attrs = attrs
        return

    data = members[meta.variable]
    where = f"{prefix}/data"
    if index == 0:
        with suppress(SchemaError):  # `_check_spatial` reports why the CRS cannot be read
            state.crs = canonical_level(data, where).crs
    if attrs.crs is not None and state.crs is not None:
        check_crs_mirror(f"{prefix}: crs", attrs.crs, state.crs, problems, state.notes)
    shape = _check_data_array(data, where, meta, state.shape, index, state, problems)
    if index == 0:
        state.attrs, state.shape = attrs, (shape if shape is not None else None)
    if shape is None or state.cs is None:
        return
    height, width = shape
    n_time = data.shape[0]
    for variable in (meta.mask_variable, meta.coverage_variable):
        if variable is not None:
            _check_plane(
                group,
                variable,
                (n_time, height, width),
                state.cs,
                state.crs,
                state,
                prefix,
                problems,
            )

    a = data.attrs.asdict()
    if (meta.nodata is None and "nodata" in a) or (
        meta.nodata is not None and a.get("nodata") != meta.nodata
    ):
        problems.append(
            f"{where}: attribute nodata must be {meta.nodata!r}, got {a.get('nodata')!r}"
        )
    for variable in (meta.variable, meta.mask_variable, meta.coverage_variable):
        if variable is not None and variable in group:
            array = get_array(group, variable, prefix)
            array_where = f"{prefix}/{variable}"
            _check_spatial(
                array,
                state.crs,
                attrs.transform,
                height,
                width,
                array_where,
                problems,
                state.notes,
            )
            with suppress(SchemaError):  # `_check_spatial` reports why the CRS cannot be read
                stored = parse_crs(array.attrs.asdict(), array_where)
                problems.extend(f"{array_where}: {m}" for m in stored.disagreements())
    if attrs.resolution != attrs.transform[0]:
        problems.append(f"{prefix}: resolution differs from spatial transform")
    for name in ("time", "band", "x", "y"):
        if name not in members:
            continue
        coordinate = members[name]
        if name == "time" and (
            coordinate.dtype != np.dtype("int64")
            or coordinate.attrs.get("units") != TIME_UNITS
            or coordinate.attrs.get("calendar") != TIME_CALENDAR
        ):
            problems.append(f"{prefix}/time: requires int64 CF epoch-ms units and calendar")
        if name in ("x", "y") and coordinate.dtype != np.dtype("float64"):
            problems.append(f"{prefix}/{name}: dtype must be float64")
        if (
            name == "band"
            and coordinate.dtype != np.dtype("int32")
            and coordinate.dtype.kind not in "OTU"
        ):
            problems.append(f"{prefix}/band: dtype must be int32 or string")
        if any(
            isinstance(r, dict) and r.get("name") == "spatial"
            for r in coordinate.attrs.get("zarr_conventions", [])
        ):
            problems.append(f"{prefix}/{name}: coordinates must not register spatial")
        try:
            check_codecs(coordinate, f"{prefix}/{name}")
        except SchemaError as exc:
            problems.append(str(exc))
    for variable in (meta.mask_variable, meta.coverage_variable):
        if variable is not None and variable in group:
            plane = get_array(group, variable, prefix)
            if _compression_config(plane) != _compression_config(data):
                problems.append(f"{prefix}/{variable}: compression differs from data")
            if variable == meta.mask_variable:
                for t in range(plane.shape[0]):
                    values = np.asarray(plane[t])
                    if not np.isin(values, [0, 1]).all():
                        problems.append(f"{prefix}/{variable}: mask must contain only 0 and 1")
                        break
    if "time" in members:
        stored = np.asarray(members["time"][:])
        expected_ms = np.array([parse_time(t) for t in meta.times], dtype="datetime64[ms]")
        if stored.shape != expected_ms.shape or not np.array_equal(
            stored.astype("datetime64[ms]"), expected_ms
        ):
            problems.append(f"{prefix}/time: values differ from chronozarr.times")
    if "band" in members and np.asarray(members["band"][:]).tolist() != (
        list(range(len(meta.bands)))
        if members["band"].dtype == np.dtype("int32")
        else list(meta.band_names)
    ):
        problems.append(f"{prefix}/band: values differ from chronozarr.bands")
    if "x" in members and "y" in members:
        y, x = pixel_centers(attrs.transform, height, width)
        for name, expected_axis in (("y", y), ("x", x)):
            stored = np.asarray(members[name][:])
            if stored.shape != expected_axis.shape or not np.allclose(stored, expected_axis):
                problems.append(
                    f"{prefix}/{name}: values differ from pixel centres of the transform"
                )


def _check_levels_attr(root: zarr.Group, attrs: RootAttrs, problems: list[str]) -> None:
    """`chronozarr.levels`, when present, must mirror the level groups and data arrays."""
    summaries = attrs.chronozarr.levels
    if summaries is None:
        return
    if len(summaries) != len(attrs.datasets):
        problems.append(
            f"chronozarr.levels: {len(summaries)} entries but multiscales lists "
            f"{len(attrs.datasets)} levels"
        )
        return
    for summary, dataset in zip(summaries, attrs.datasets, strict=True):
        where = f"chronozarr.levels[{summary.path}]"
        try:
            group = get_group(root, dataset.path, "store")
            level_attrs = parse_level_attrs(group.attrs.asdict(), where)
            data = get_array(group, attrs.chronozarr.variable, where)
        except SchemaError:
            continue  # reported by the level checks
        if not same_numbers(summary.transform, level_attrs.transform):
            problems.append(f"{where}: transform differs from the level group's")
        if not math.isclose(summary.resolution, level_attrs.resolution):
            problems.append(f"{where}: resolution differs from the level group's")
        if tuple(data.shape) != summary.shape:
            problems.append(
                f"{where}: shape {list(summary.shape)} differs from {list(data.shape)}"
            )
        elif summary.grid != grid_shape(summary.shape[2], summary.shape[3], int(data.chunks[2])):
            problems.append(f"{where}: grid {list(summary.grid)} does not match shape and chunks")


def _check_shard_bytes(root: zarr.Group, attrs: RootAttrs, problems: list[str]) -> None:
    """`chronozarr.shard_bytes`, when present, must match the stored shard object sizes."""
    shard_bytes = attrs.chronozarr.shard_bytes
    if shard_bytes is None:
        return
    variable = attrs.chronozarr.variable
    for level, shards in shard_bytes.items():
        if level not in {d.path for d in attrs.datasets}:
            problems.append(f"chronozarr.shard_bytes: unknown level '{level}'")
            continue
        for key, expected in shards.items():
            t_shard, row, col = (int(p) for p in key.split("/"))
            store_key = shard_key(level, variable, t_shard, row, col)
            try:
                actual = sync(root.store.getsize(store_key))
            except (FileNotFoundError, KeyError):
                problems.append(f"chronozarr.shard_bytes[{level}][{key}]: {store_key} is missing")
                continue
            if actual != expected:
                problems.append(
                    f"chronozarr.shard_bytes[{level}][{key}]: {expected} bytes listed, "
                    f"{store_key} has {actual}"
                )


def _check_consolidated(store: Any, direct: zarr.Group) -> list[str]:
    """Consolidated metadata must equal the per-array metadata it summarises."""
    try:
        consolidated = zarr.open_group(store, mode="r", zarr_format=3, use_consolidated=True)
    except ValueError:
        return []  # consolidated metadata is optional (spec 3.1: SHOULD)
    if consolidated.metadata.consolidated_metadata is None:
        return []
    problems = []
    for path, node in consolidated.members(max_depth=None):
        if not isinstance(node, zarr.Array):
            continue
        try:
            actual = get_array(direct, path, "store")
        except SchemaError:
            problems.append(f"{path}: listed in consolidated metadata but missing on disk")
            continue
        if node.metadata.to_dict() != actual.metadata.to_dict():
            problems.append(
                f"{path}: consolidated metadata is stale (differs from {path}/zarr.json)"
            )
    return problems


@dataclass(frozen=True)
class ValidationReport:
    """What `validate_report` found: `problems` fail conformance, `notes` do not."""

    problems: list[str]
    notes: list[str]
    spec_version: str | None  # of the store, None when its root attributes could not be read


def validate(store: Any) -> list[str]:
    """Check a store against chronozarr. Returns a list of problems; empty means conforming.

    `store` is anything `zarr.open_group` accepts: a path, URL, or zarr Store. Per-node
    `zarr.json` files are checked directly; consolidated metadata, when present, must match them.
    Spec versions 0.3.0 and 0.4.0 are accepted. `validate_report` also returns the notes.
    """
    return validate_report(store).problems


def validate_report(store: Any) -> ValidationReport:
    """`validate`, plus notes: things to improve that a conforming store may still have.

    A note is what a SHOULD rule leaves behind: a `crs` mirror that is absent, or a mirror that
    could not be compared with the stored CRS because that needs pyproj.
    """
    problems: list[str] = []
    notes: list[str] = []
    version = _validate_into(store, problems, notes)
    return ValidationReport(problems, notes, version)


def _crs_notes(attrs: RootAttrs, state: _LevelState) -> list[str]:
    """Notes on the CRS: an absent mirror, and a 0.3.0 store that released readers refuse."""
    notes = list(state.notes)
    reference = state.crs
    if reference is None:
        return notes
    absent = [*(["chronozarr"] if attrs.chronozarr.crs is None else []), *state.absent]
    version = attrs.chronozarr.spec_version
    has_epsg = reference.epsg is not None
    if absent and reference.code is not None:
        refused = (
            " Readers up to chronozarr 0.4.1 refuse a 0.3.0 store without them."
            if version == "0.3.0"
            else ""
        )
        notes.append(
            f'crs mirror absent from {", ".join(absent)}: write "crs": "{reference.code}" '
            f"there (SHOULD).{refused}"
        )
    if version == "0.3.0" and not has_epsg:
        notes.append(
            "spec_version 0.3.0 without an EPSG proj:code: readers up to chronozarr 0.4.1 refuse "
            "this store; use 0.4.0 for a CRS that has no EPSG code"
        )
    return notes


def _validate_into(store: Any, problems: list[str], notes: list[str]) -> str | None:
    """Fill `problems` and `notes` for `store`. Returns its spec_version when readable."""
    store = as_store(store)
    from pathlib import Path

    from zarr.storage import LocalStore

    if isinstance(store, str | Path):
        store = LocalStore(store, read_only=True)
    store = IndexStore(store)
    try:
        root = zarr.open_group(store, mode="r", zarr_format=3, use_consolidated=False)
    except (GroupNotFoundError, FileNotFoundError):
        problems.append(f"{store}: no Zarr v3 group found (is this a chronozarr store?)")
        return None
    except ValueError as exc:
        problems.append(str(exc))
        return None
    try:
        import json

        from zarr.core.buffer import default_buffer_prototype

        manifest = sync(root.store.get("zarr.json", default_buffer_prototype()))
        if manifest is not None:
            check_extensions(json.loads(manifest.to_bytes()))
        attrs = parse_root_attrs(root.attrs.asdict())
    except ValueError as exc:
        problems.append(str(exc))
        return None

    raw_meta = cast("dict[str, Any]", root.attrs.asdict()["chronozarr"])
    for key in ("band_names", "levels"):
        if key not in raw_meta:
            problems.append(
                f"chronozarr: writer must emit {key}; reader fallback remains available"
            )
    state = _LevelState()
    for index, dataset in enumerate(attrs.datasets):
        try:
            _check_level(root, index, dataset, attrs.chronozarr, state, problems)
        except (ValueError, KeyError, TypeError) as exc:
            problems.append(f"level {dataset.path}: {exc}")
    if attrs.chronozarr.crs is not None and state.crs is not None:
        check_crs_mirror("chronozarr.crs", attrs.chronozarr.crs, state.crs, problems, state.notes)
    notes.extend(_crs_notes(attrs, state))

    _check_levels_attr(root, attrs, problems)
    _check_shard_bytes(root, attrs, problems)
    problems.extend(_check_consolidated(store, root))

    version = attrs.chronozarr.spec_version
    path = attrs.chronozarr.volatility_path
    if path is None:
        if VOLATILITY_PATH in root:
            problems.append("volatility: array present without declaration")
        return version
    try:
        volatility = get_array(root, path, "store")
    except SchemaError as exc:
        problems.append(str(exc))
    else:
        _check_dims(volatility, ("row", "col"), path, problems)
        if tuple(volatility.chunks) != tuple(volatility.shape):
            problems.append(f"{path}: must occupy a single chunk")
        if "spatial:dimensions" in volatility.attrs:
            problems.append(f"{path}: must not be georeferenced")
        try:
            check_codecs(volatility, path)
        except SchemaError as exc:
            problems.append(str(exc))
        values = np.asarray(volatility[:])
        if not (np.isfinite(values).all() and (values >= 0).all() and (values <= 1).all()):
            problems.append(f"{path}: values must be finite in [0,1]")
        if volatility.dtype != np.dtype("float32"):
            problems.append(f"{path}: dtype must be float32, got {volatility.dtype}")
        if state.shape is not None and state.cs is not None:
            expected_grid = grid_shape(*state.shape, state.cs)
            if volatility.shape != expected_grid:
                problems.append(
                    f"{path}: shape {volatility.shape} should equal the level-0 cell grid "
                    f"{expected_grid}"
                )
    return version


def _compression_config(array: zarr.Array) -> dict[str, Any]:
    codecs = cast("list[dict[str, Any]]", array.metadata.to_dict()["codecs"])
    if codecs[0]["name"] == "sharding_indexed":
        codecs = codecs[0]["configuration"]["codecs"]
    configuration = dict(codecs[-1])
    if configuration["name"] == "blosc":
        configuration["configuration"] = {
            k: v for k, v in configuration["configuration"].items() if k != "typesize"
        }
    return configuration
