"""chronozarr schema: attribute dataclasses, layout helpers, and store validation.

The layout is documented in spec/CHRONOZARR.md and spec/CHANGES-0.2.md. This module owns
everything both the writer and the reader must agree on: attribute names, the anchor schedule,
pyramid geometry, dtype profiles, and the checks that decide whether a Zarr v3 store is a
conforming chronozarr store. New stores are v0.2; v0.1 stores stay readable.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, cast

import numpy as np
import zarr
from zarr.core.sync import sync
from zarr.errors import GroupNotFoundError

SPEC_VERSION = "0.2.0"
STAR_DELTA = "star-delta"
NONE = "none"
ENCODINGS = (NONE, STAR_DELTA)
VARIABLE = "data"
MASK_VARIABLE = "mask"
COVERAGE_VARIABLE = "coverage"
VOLATILITY_PATH = "volatility"
NODATA = 0
DIMENSIONS = ("time", "band", "y", "x")
PLANE_DIMENSIONS = ("time", "y", "x")  # the mask and coverage variables
DTYPES = ("uint8", "uint16", "int16", "float32")
TEMPORAL_DTYPES = ("uint8", "uint16")  # star-delta residuals need modular unsigned arithmetic
GAP_FILLS = ("carry-forward", "none")
CODECS = ("zstd", "gzip", "blosc")
TIME_UNITS = "milliseconds since 1970-01-01T00:00:00"
TIME_CALENDAR = "proleptic_gregorian"

Transform = tuple[float, float, float, float, float, float]

_VERSION_RE = re.compile(r"^0\.[12]\.\d+$")


class SchemaError(ValueError):
    """A store or attribute block does not conform to chronozarr."""


# --- Layout helpers -------------------------------------------------------------------------


def compute_anchor_schedule(n_time: int, anchor_interval: int) -> tuple[list[int], dict[int, int]]:
    """Return (anchor_indices, delta_reference) as a fresh encode writes them.

    Anchors are 0, k, 2k, ... below n_time. Every other timestep references the nearest anchor
    by index distance; ties go to the earlier anchor. Decoding any timestep needs one anchor
    and one delta. The anchors depend only on a timestep's position; the references are the
    writer's default for new timesteps, not a validity rule (`delta_reference_problem`): an
    append keeps the references already recorded.
    """
    anchors = list(range(0, n_time, anchor_interval))
    reference: dict[int, int] = {}
    for t in range(n_time):
        if t % anchor_interval == 0:
            continue
        earlier = t - t % anchor_interval
        later = earlier + anchor_interval
        reference[t] = later if later < n_time and later - t < t - earlier else earlier
    return anchors, reference


def delta_reference_problem(
    reference: Mapping[int, int], anchors: Sequence[int], n_time: int, interval: int
) -> str | None:
    """Why `reference` is not a valid star-delta reference map, or None when it is valid.

    Valid means: every non-anchor timestep is a key, no anchor is, and each value is an anchor
    at index distance 0 < d < `interval` from its key. Nothing requires the nearest anchor.
    """
    anchor_set = set(anchors)
    non_anchors = set(range(n_time)) - anchor_set
    missing = sorted(non_anchors - reference.keys())
    if missing:
        return f"timesteps {missing[:5]} are neither anchors nor listed"
    unexpected = sorted(reference.keys() - non_anchors)
    if unexpected:
        return f"keys {unexpected[:5]} are anchors or outside 0..{n_time - 1}"
    for t in sorted(reference):
        anchor = reference[t]
        if anchor not in anchor_set:
            return f"timestep {t} references {anchor}, which is not an anchor"
        if abs(anchor - t) >= interval:
            return (
                f"timestep {t} references anchor {anchor} at distance {abs(anchor - t)}; "
                f"the distance must be below anchor_interval {interval}"
            )
    return None


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


def time_shard_count(n_time: int, shard_time: int) -> int:
    """Number of shards along the time axis."""
    return math.ceil(n_time / shard_time)


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
class Selection:
    """The auto encoding measurement: star-delta bytes over plain bytes on sampled cells."""

    sampled_cells: int
    ratio: float
    mode: str = "auto"

    def to_attrs(self) -> dict[str, Any]:
        return {"mode": self.mode, "sampled_cells": self.sampled_cells, "ratio": self.ratio}


@dataclass(frozen=True)
class Temporal:
    """Temporal encoding. `none` is represented as a schedule where every timestep is an anchor
    (anchor_interval 1, no deltas), so readers share one code path."""

    anchor_interval: int
    anchor_indices: tuple[int, ...]
    delta_reference: Mapping[int, int]
    encoding: str = STAR_DELTA
    selection: Selection | None = None

    @classmethod
    def build(
        cls, n_time: int, anchor_interval: int, selection: Selection | None = None
    ) -> Temporal:
        anchors, reference = compute_anchor_schedule(n_time, anchor_interval)
        return cls(anchor_interval, tuple(anchors), reference, STAR_DELTA, selection)

    @classmethod
    def plain(cls, n_time: int, selection: Selection | None = None) -> Temporal:
        return cls(1, tuple(range(n_time)), {}, NONE, selection)

    def to_attrs(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {"encoding": self.encoding}
        if self.encoding == STAR_DELTA:
            attrs["anchor_interval"] = self.anchor_interval
            attrs["anchor_indices"] = list(self.anchor_indices)
            attrs["delta_reference"] = {str(t): a for t, a in self.delta_reference.items()}
        if self.selection is not None:
            attrs["selection"] = self.selection.to_attrs()
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
    crs: str
    temporal: Temporal
    spec_version: str = SPEC_VERSION
    variable: str = VARIABLE
    nodata: int | float | None = NODATA
    volatility_path: str = VOLATILITY_PATH
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
            "crs": self.crs,
            "temporal": self.temporal.to_attrs(),
            "volatility_path": self.volatility_path,
        }
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
    """One entry of `multiscales[0].datasets`.

    Writers emit only `path` and `crs`. A `pixels_per_tile` key left by an earlier writer is
    ignored (spec 3.4): zarr-layer reads its presence as "global Web Mercator pyramid", and the
    cell size is the chunk shape of the data arrays (`cell_size`).
    """

    path: str
    crs: str


@dataclass(frozen=True)
class RootAttrs:
    chronozarr: Chronozarr
    datasets: tuple[LevelRef, ...]

    def to_attrs(self) -> dict[str, Any]:
        return {
            "multiscales": [
                {
                    "datasets": [{"path": d.path, "crs": d.crs} for d in self.datasets],
                    "type": "reduce",
                    "metadata": {
                        "method": "block_mean",
                        "version": f"chronozarr {SPEC_VERSION}",
                        "args": [],
                    },
                }
            ],
            "chronozarr": self.chronozarr.to_attrs(),
        }


@dataclass(frozen=True)
class LevelAttrs:
    """Attributes of a level group (`{level}/zarr.json`)."""

    crs: str
    transform: Transform
    resolution: float

    def to_attrs(self) -> dict[str, Any]:
        return {"crs": self.crs, "transform": list(self.transform), "resolution": self.resolution}


_EPSG_RE = re.compile(r"^EPSG:(\d+)$")


def crs_attr(crs: str) -> dict[str, str] | None:
    """The `_CRS` array attribute GDAL's Zarr driver reads: an OGC URL, plus WKT if pyproj exists.

    Only `EPSG:<code>` strings map to a URL; any other CRS string returns None (no `_CRS`).
    """
    match = _EPSG_RE.match(crs)
    if match is None:
        return None
    code = int(match.group(1))
    attr = {"url": f"http://www.opengis.net/def/crs/EPSG/0/{code}"}
    try:
        # pyproj is optional and not installed in every environment (ty cannot resolve it).
        from pyproj import CRS  # ty: ignore[unresolved-import]
        from pyproj.exceptions import CRSError  # ty: ignore[unresolved-import]
    except ImportError:
        return attr
    with suppress(CRSError):  # an EPSG code pyproj does not know: the URL alone still names it
        attr["wkt"] = CRS.from_epsg(code).to_wkt()
    return attr


def data_array_attrs(
    crs: str,
    transform: Transform,
    height: int,
    width: int,
    nodata: int | float | None = NODATA,
    *,
    dimensions: Sequence[str] = DIMENSIONS,
) -> dict[str, Any]:
    """Attributes of a level's data-like array: `proj:`, `spatial:` and GDAL's `_CRS`.

    `nodata` is omitted for arrays that are not the data variable (mask, coverage).
    """
    attrs: dict[str, Any] = {
        "_ARRAY_DIMENSIONS": list(dimensions),
        "nodata": nodata,
        "crs": crs,
        "transform": list(transform),
        "proj:code": crs,
        "spatial:dimensions": ["y", "x"],
        "spatial:shape": [height, width],
        "spatial:transform": list(transform),
        "spatial:bbox": list(bounds(transform, height, width)),
    }
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


def parse_band(raw: Any, where: str) -> Band:
    """A band given as a name (v0.1) or as an object with optional scale/offset/units."""
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


def _parse_selection(raw: Any, where: str) -> Selection:
    mode = _require(raw, "mode", where)
    if mode != "auto":
        raise _fail(f"{where}.mode", f"expected 'auto', got {mode!r}")
    sampled = _require(raw, "sampled_cells", where)
    if not _is_int(sampled) or sampled < 0:
        raise _fail(f"{where}.sampled_cells", f"expected int >= 0, got {sampled!r}")
    ratio = _require(raw, "ratio", where)
    if not _is_number(ratio) or ratio < 0:
        raise _fail(f"{where}.ratio", f"expected a number >= 0, got {ratio!r}")
    return Selection(int(sampled), float(ratio), mode)


def _parse_temporal(block: Any, n_time: int, where: str) -> Temporal:
    encoding = _require(block, "encoding", where)
    if encoding not in ENCODINGS:
        raise _fail(f"{where}.encoding", f"expected one of {list(ENCODINGS)}, got {encoding!r}")
    selection = (
        _parse_selection(block["selection"], f"{where}.selection")
        if "selection" in block
        else None
    )
    if encoding == NONE:
        return Temporal.plain(n_time, selection)
    interval = _require(block, "anchor_interval", where)
    if not _is_int(interval) or interval < 1:
        raise _fail(f"{where}.anchor_interval", f"expected int >= 1, got {interval!r}")
    raw_anchors = _require(block, "anchor_indices", where)
    raw_reference = _require(block, "delta_reference", where)
    if not isinstance(raw_anchors, list) or not all(_is_int(a) for a in raw_anchors):
        raise _fail(f"{where}.anchor_indices", "expected a list of ints")
    if not isinstance(raw_reference, Mapping):
        raise _fail(f"{where}.delta_reference", "expected an object keyed by timestep index")
    try:
        reference = {int(t): a for t, a in raw_reference.items()}
    except ValueError as exc:
        raise _fail(f"{where}.delta_reference", "keys must be decimal timestep indices") from exc

    anchors, _ = compute_anchor_schedule(n_time, interval)
    if raw_anchors != anchors:
        raise _fail(
            f"{where}.anchor_indices",
            f"expected {anchors} for n_time={n_time}, anchor_interval={interval}; "
            f"got {raw_anchors}",
        )
    if (problem := delta_reference_problem(reference, anchors, n_time, interval)) is not None:
        raise _fail(f"{where}.delta_reference", problem)
    return Temporal(interval, tuple(raw_anchors), reference, STAR_DELTA, selection)


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
    """Validate and parse the `chronozarr` root attribute block (v0.1 or v0.2)."""
    version = _require(block, "spec_version", where)
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise _fail(
            f"{where}.spec_version", f"unsupported version {version!r}; expected 0.1.x or 0.2.x"
        )
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
    crs = _require(block, "crs", where)
    if not isinstance(crs, str) or not crs:
        raise _fail(f"{where}.crs", "expected a non-empty string such as 'EPSG:32631'")
    volatility_path = _require(block, "volatility_path", where)
    if not isinstance(volatility_path, str) or not volatility_path:
        raise _fail(f"{where}.volatility_path", "expected a non-empty string")
    temporal = _parse_temporal(_require(block, "temporal", where), len(times), f"{where}.temporal")
    provenance = (
        parse_provenance(block["provenance"], f"{where}.provenance")
        if block.get("provenance") is not None
        else None
    )
    return Chronozarr(
        times=times,
        bands=bands,
        crs=crs,
        temporal=temporal,
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


def _parse_multiscales(attrs: Mapping[str, Any], crs: str) -> tuple[LevelRef, ...]:
    multiscales = _require(attrs, "multiscales", "root attributes")
    if not isinstance(multiscales, list) or len(multiscales) != 1:
        raise _fail("multiscales", "expected a list with exactly one entry")
    raw = _require(multiscales[0], "datasets", "multiscales[0]")
    if not isinstance(raw, list) or not raw:
        raise _fail("multiscales[0].datasets", "expected a non-empty list")
    datasets = []
    for i, entry in enumerate(raw):
        where = f"multiscales[0].datasets[{i}]"
        path = _require(entry, "path", where)
        if path != str(i):
            raise _fail(
                where, f"levels must be listed as '0', '1', ... in order; got path {path!r}"
            )
        entry_crs = _require(entry, "crs", where)
        if entry_crs != crs:
            raise _fail(f"{where}.crs", f"expected '{crs}' (chronozarr.crs), got {entry_crs!r}")
        datasets.append(LevelRef(path, entry_crs))
    return tuple(datasets)


def parse_root_attrs(attrs: Mapping[str, Any]) -> RootAttrs:
    """Validate and parse the root group attributes (`multiscales` + `chronozarr`)."""
    block = parse_chronozarr(_require(attrs, "chronozarr", "root attributes"))
    return RootAttrs(chronozarr=block, datasets=_parse_multiscales(attrs, block.crs))


def parse_level_attrs(attrs: Mapping[str, Any], where: str) -> LevelAttrs:
    """Validate and parse a level group's attributes."""
    crs = _require(attrs, "crs", where)
    if not isinstance(crs, str) or not crs:
        raise _fail(f"{where}.crs", "expected a non-empty string")
    transform = _parse_transform(_require(attrs, "transform", where), f"{where}.transform")
    resolution = _require(attrs, "resolution", where)
    if not _is_number(resolution) or resolution <= 0:
        raise _fail(f"{where}.resolution", f"expected a positive number, got {resolution!r}")
    return LevelAttrs(crs=crs, transform=transform, resolution=float(resolution))


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


@dataclass
class _LevelState:
    """What later levels compare against: level 0 values and the time shard length."""

    attrs: LevelAttrs | None = None
    shape: tuple[int, int] | None = None
    cs: int | None = None
    shard_time: int | None = None
    sharded: bool | None = None


def _check_crs_attr(attrs: Mapping[str, Any], crs: str, where: str, problems: list[str]) -> None:
    """`_CRS` (GDAL), when present, must name the store CRS by URL and carry WKT as a string."""
    if "_CRS" not in attrs:
        return
    value = attrs["_CRS"]
    expected = crs_attr(crs)
    if not isinstance(value, Mapping):
        problems.append(f"{where}: attribute _CRS must be an object, got {value!r}")
    elif expected is not None and value.get("url") != expected["url"]:
        problems.append(
            f"{where}: attribute _CRS url must be {expected['url']!r}, got {value.get('url')!r}"
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
    crs: str,
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
    if meta.temporal.encoding == STAR_DELTA and data.dtype.name not in TEMPORAL_DTYPES:
        problems.append(
            f"{where}: star-delta needs one of {list(TEMPORAL_DTYPES)}, got {data.dtype}"
        )
    if data.ndim != 4:
        problems.append(f"{where}: expected 4 dimensions (time, band, y, x), got {data.ndim}")
        return None
    n_time, n_band, height, width = data.shape
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
    _check_layout(data, where, (n_band,), state.cs, state, problems)
    names = _codec_names(data)
    if len(names) != 2 or names[0] != "bytes" or names[1] not in CODECS:
        problems.append(f"{where}: codecs must be bytes plus one of {list(CODECS)}, got {names}")
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

    if attrs.crs != meta.crs:
        problems.append(f"{prefix}: crs {attrs.crs!r} differs from chronozarr.crs {meta.crs!r}")
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
                meta.crs,
                state,
                prefix,
                problems,
            )

    a = data.attrs.asdict()
    if "nodata" not in a or a["nodata"] != meta.nodata:
        problems.append(
            f"{where}: attribute nodata must be {meta.nodata!r}, got {a.get('nodata')!r}"
        )
    # proj:/spatial: (and the duplicated crs/transform) are optional: readers must not require
    # them, so they are checked only when present.
    optional_values = {
        "crs": meta.crs,
        "proj:code": meta.crs,
        "spatial:dimensions": ["y", "x"],
        "spatial:shape": [height, width],
    }
    for key, value in optional_values.items():
        if key in a and a[key] != value:
            problems.append(f"{where}: attribute {key} must be {value!r}, got {a[key]!r}")
    optional_numbers = {
        "transform": list(attrs.transform),
        "spatial:transform": list(attrs.transform),
        "spatial:bbox": list(bounds(attrs.transform, height, width)),
    }
    for key, value in optional_numbers.items():
        got = a.get(key)
        if key in a and not (isinstance(got, list) and same_numbers(got, value)):
            problems.append(f"{where}: attribute {key} must be {value}, got {got}")
    _check_crs_attr(a, meta.crs, where, problems)

    if "time" in members:
        stored = np.asarray(members["time"][:])
        expected_ms = np.array([parse_time(t) for t in meta.times], dtype="datetime64[ms]")
        if stored.shape != expected_ms.shape or not np.array_equal(
            stored.astype("datetime64[ms]"), expected_ms
        ):
            problems.append(f"{prefix}/time: values differ from chronozarr.times")
    if "band" in members and np.asarray(members["band"][:]).tolist() != list(meta.band_names):
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


def validate(store: Any) -> list[str]:
    """Check a store against chronozarr. Returns a list of problems; empty means conforming.

    `store` is anything `zarr.open_group` accepts: a path, URL, or zarr Store. Per-node
    `zarr.json` files are checked directly; consolidated metadata, when present, must match them.
    Both v0.1 and v0.2 stores are accepted.
    """
    from chronozarr.decode import as_store  # decode imports this module

    store = as_store(store)
    problems: list[str] = []
    try:
        root = zarr.open_group(store, mode="r", zarr_format=3, use_consolidated=False)
    except (GroupNotFoundError, FileNotFoundError):
        return [f"{store}: no Zarr v3 group found (is this a chronozarr store?)"]
    try:
        attrs = parse_root_attrs(root.attrs.asdict())
    except SchemaError as exc:
        return [str(exc)]

    state = _LevelState()
    for index, dataset in enumerate(attrs.datasets):
        _check_level(root, index, dataset, attrs.chronozarr, state, problems)

    _check_levels_attr(root, attrs, problems)
    _check_shard_bytes(root, attrs, problems)
    problems.extend(_check_consolidated(store, root))

    path = attrs.chronozarr.volatility_path
    try:
        volatility = get_array(root, path, "store")
    except SchemaError as exc:
        problems.append(str(exc))
    else:
        _check_dims(volatility, ("row", "col"), path, problems)
        if volatility.dtype != np.dtype("float32"):
            problems.append(f"{path}: dtype must be float32, got {volatility.dtype}")
        if state.shape is not None and state.cs is not None:
            expected_grid = grid_shape(*state.shape, state.cs)
            if volatility.shape != expected_grid:
                problems.append(
                    f"{path}: shape {volatility.shape} should equal the level-0 cell grid "
                    f"{expected_grid}"
                )
    return problems
