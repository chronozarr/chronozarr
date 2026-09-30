"""chronozarr v0.1 schema: attribute dataclasses, layout helpers, and store validation.

The layout is documented in spec/CHRONOZARR.md. This module owns everything both the writer and
the reader must agree on: attribute names, the anchor schedule, pyramid geometry, and the checks
that decide whether a Zarr v3 store is a conforming chronozarr store.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any, cast

import numpy as np
import zarr
from zarr.errors import GroupNotFoundError

SPEC_VERSION = "0.1.0"
ENCODING = "star-delta"
VARIABLE = "data"
VOLATILITY_PATH = "volatility"
NODATA = 0
DIMENSIONS = ("time", "band", "y", "x")
TIME_UNITS = "milliseconds since 1970-01-01T00:00:00"
TIME_CALENDAR = "proleptic_gregorian"

Transform = tuple[float, float, float, float, float, float]

_VERSION_RE = re.compile(r"^0\.1\.\d+$")


class SchemaError(ValueError):
    """A store or attribute block does not conform to chronozarr v0.1."""


# --- Layout helpers -------------------------------------------------------------------------


def compute_anchor_schedule(n_time: int, anchor_interval: int) -> tuple[list[int], dict[int, int]]:
    """Return (anchor_indices, delta_reference).

    Anchors are 0, k, 2k, ... below n_time. Every other timestep references the nearest anchor
    by index distance; ties go to the earlier anchor. Decoding any timestep needs one anchor
    and one delta.
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


# --- Attribute dataclasses ------------------------------------------------------------------


@dataclass(frozen=True)
class Temporal:
    anchor_interval: int
    anchor_indices: tuple[int, ...]
    delta_reference: Mapping[int, int]

    @classmethod
    def build(cls, n_time: int, anchor_interval: int) -> Temporal:
        anchors, reference = compute_anchor_schedule(n_time, anchor_interval)
        return cls(anchor_interval, tuple(anchors), reference)

    def to_attrs(self) -> dict[str, Any]:
        return {
            "encoding": ENCODING,
            "anchor_interval": self.anchor_interval,
            "anchor_indices": list(self.anchor_indices),
            "delta_reference": {str(t): a for t, a in self.delta_reference.items()},
        }


@dataclass(frozen=True)
class Chronozarr:
    """The `chronozarr` block of the root group attributes."""

    times: tuple[str, ...]
    bands: tuple[str, ...]
    crs: str
    temporal: Temporal
    spec_version: str = SPEC_VERSION
    variable: str = VARIABLE
    nodata: int = NODATA
    volatility_path: str = VOLATILITY_PATH

    def to_attrs(self) -> dict[str, Any]:
        return {
            "spec_version": self.spec_version,
            "variable": self.variable,
            "times": list(self.times),
            "bands": list(self.bands),
            "nodata": self.nodata,
            "crs": self.crs,
            "temporal": self.temporal.to_attrs(),
            "volatility_path": self.volatility_path,
        }


@dataclass(frozen=True)
class LevelRef:
    """One entry of `multiscales[0].datasets`."""

    path: str
    pixels_per_tile: int
    crs: str


@dataclass(frozen=True)
class RootAttrs:
    chronozarr: Chronozarr
    datasets: tuple[LevelRef, ...]

    def to_attrs(self) -> dict[str, Any]:
        return {
            "multiscales": [
                {
                    "datasets": [
                        {"path": d.path, "pixels_per_tile": d.pixels_per_tile, "crs": d.crs}
                        for d in self.datasets
                    ],
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


def data_array_attrs(crs: str, transform: Transform, height: int, width: int) -> dict[str, Any]:
    """Attributes of `{level}/data`, including the `proj:` and `spatial:` conventions."""
    return {
        "_ARRAY_DIMENSIONS": list(DIMENSIONS),
        "nodata": NODATA,
        "crs": crs,
        "transform": list(transform),
        "proj:code": crs,
        "spatial:dimensions": ["y", "x"],
        "spatial:shape": [height, width],
        "spatial:transform": list(transform),
        "spatial:bbox": list(bounds(transform, height, width)),
    }


# --- Parsing --------------------------------------------------------------------------------


def _fail(where: str, message: str) -> SchemaError:
    return SchemaError(f"{where}: {message}")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require(mapping: object, key: str, where: str) -> Any:
    if not isinstance(mapping, Mapping):
        raise _fail(where, f"expected an object, got {type(mapping).__name__}")
    entries = cast("Mapping[str, Any]", mapping)
    if key not in entries:
        raise _fail(where, f"missing required key '{key}'")
    return entries[key]


def _str_list(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise _fail(where, "expected a non-empty list of strings")
    strings = [v for v in value if isinstance(v, str)]
    if len(strings) != len(value):
        raise _fail(where, "expected a list of strings")
    return tuple(strings)


def _parse_temporal(block: object, n_time: int, where: str) -> Temporal:
    encoding = _require(block, "encoding", where)
    if encoding != ENCODING:
        raise _fail(f"{where}.encoding", f"expected '{ENCODING}', got {encoding!r}")
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

    anchors, expected = compute_anchor_schedule(n_time, interval)
    if raw_anchors != anchors:
        raise _fail(
            f"{where}.anchor_indices",
            f"expected {anchors} for n_time={n_time}, anchor_interval={interval}; "
            f"got {raw_anchors}",
        )
    if reference != expected:
        raise _fail(
            f"{where}.delta_reference",
            f"does not match the nearest-anchor schedule for n_time={n_time}, "
            f"anchor_interval={interval}",
        )
    return Temporal(interval, tuple(raw_anchors), reference)


def parse_chronozarr(block: object, where: str = "chronozarr") -> Chronozarr:
    """Validate and parse the `chronozarr` root attribute block."""
    version = _require(block, "spec_version", where)
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise _fail(f"{where}.spec_version", f"unsupported version {version!r}; expected 0.1.x")
    variable = _require(block, "variable", where)
    if not isinstance(variable, str) or not variable:
        raise _fail(f"{where}.variable", f"expected a non-empty array name, got {variable!r}")
    times = _str_list(_require(block, "times", where), f"{where}.times")
    parsed = [parse_time(t) for t in times]
    if any(later <= earlier for earlier, later in pairwise(parsed)):
        raise _fail(f"{where}.times", "must be strictly increasing")
    bands = _str_list(_require(block, "bands", where), f"{where}.bands")
    if len(set(bands)) != len(bands):
        raise _fail(f"{where}.bands", f"band names must be unique, got {list(bands)}")
    nodata = _require(block, "nodata", where)
    if nodata != NODATA or not _is_int(nodata):
        raise _fail(f"{where}.nodata", f"expected {NODATA} in v0.1, got {nodata!r}")
    crs = _require(block, "crs", where)
    if not isinstance(crs, str) or not crs:
        raise _fail(f"{where}.crs", "expected a non-empty string such as 'EPSG:32631'")
    volatility_path = _require(block, "volatility_path", where)
    if not isinstance(volatility_path, str) or not volatility_path:
        raise _fail(f"{where}.volatility_path", "expected a non-empty string")
    temporal = _parse_temporal(_require(block, "temporal", where), len(times), f"{where}.temporal")
    return Chronozarr(
        times=times,
        bands=bands,
        crs=crs,
        temporal=temporal,
        spec_version=version,
        variable=variable,
        nodata=nodata,
        volatility_path=volatility_path,
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
        tile = _require(entry, "pixels_per_tile", where)
        if not _is_int(tile) or tile < 1:
            raise _fail(f"{where}.pixels_per_tile", f"expected int >= 1, got {tile!r}")
        entry_crs = _require(entry, "crs", where)
        if entry_crs != crs:
            raise _fail(f"{where}.crs", f"expected '{crs}' (chronozarr.crs), got {entry_crs!r}")
        datasets.append(LevelRef(path, tile, entry_crs))
    if len({d.pixels_per_tile for d in datasets}) != 1:
        raise _fail("multiscales[0].datasets", "pixels_per_tile must be identical at every level")
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
    raw = _require(attrs, "transform", where)
    if (
        not isinstance(raw, list)
        or len(raw) != 6
        or not all(isinstance(v, int | float) and not isinstance(v, bool) for v in raw)
    ):
        raise _fail(f"{where}.transform", f"expected 6 numbers [a, b, c, d, e, f], got {raw!r}")
    transform: Transform = (
        float(raw[0]),
        float(raw[1]),
        float(raw[2]),
        float(raw[3]),
        float(raw[4]),
        float(raw[5]),
    )
    resolution = _require(attrs, "resolution", where)
    if not isinstance(resolution, int | float) or isinstance(resolution, bool) or resolution <= 0:
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


# --- Store validation -----------------------------------------------------------------------


def _same_numbers(a: Sequence[Any], b: Sequence[float]) -> bool:
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


def _check_level(
    root: zarr.Group,
    index: int,
    dataset: LevelRef,
    meta: Chronozarr,
    base: LevelAttrs | None,
    base_shape: tuple[int, int] | None,
    problems: list[str],
) -> tuple[LevelAttrs | None, tuple[int, int] | None]:
    """Check one level group. Returns its parsed attrs and spatial shape for later levels."""
    prefix = f"level {dataset.path}"
    try:
        group = get_group(root, dataset.path, "store")
        attrs = parse_level_attrs(group.attrs.asdict(), prefix)
    except SchemaError as exc:
        problems.append(str(exc))
        return None, None

    if attrs.crs != meta.crs:
        problems.append(f"{prefix}: crs {attrs.crs!r} differs from chronozarr.crs {meta.crs!r}")
    if base is not None and not _same_numbers(
        attrs.transform, scale_transform(base.transform, index)
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
        return attrs, None

    data = members[meta.variable]
    where = f"{prefix}/data"
    _check_dims(data, DIMENSIONS, where, problems)
    if data.dtype != np.dtype("uint16"):
        problems.append(f"{where}: dtype must be uint16, got {data.dtype}")
    if data.ndim != 4:
        problems.append(f"{where}: expected 4 dimensions (time, band, y, x), got {data.ndim}")
        return attrs, None
    n_time, n_band, height, width = data.shape
    if n_time != len(meta.times):
        problems.append(f"{where}: {n_time} timesteps but chronozarr.times has {len(meta.times)}")
    if n_band != len(meta.bands):
        problems.append(f"{where}: {n_band} bands but chronozarr.bands has {len(meta.bands)}")
    if base_shape is not None:
        expected = (math.ceil(base_shape[0] / 2**index), math.ceil(base_shape[1] / 2**index))
        if (height, width) != expected:
            problems.append(f"{where}: spatial shape {(height, width)} should be {expected}")
    cs = dataset.pixels_per_tile
    if tuple(data.chunks) != (1, n_band, cs, cs):
        problems.append(f"{where}: chunks must be {(1, n_band, cs, cs)}, got {tuple(data.chunks)}")
    if data.shards is not None and tuple(data.shards[1:]) != (n_band, cs, cs):
        problems.append(
            f"{where}: shards must be (n_time, {n_band}, {cs}, {cs}), got {data.shards}"
        )
    if data.fill_value != NODATA:
        problems.append(f"{where}: fill_value must equal nodata ({NODATA}), got {data.fill_value}")

    a = data.attrs.asdict()
    if a.get("nodata") != NODATA:
        problems.append(f"{where}: attribute nodata must be {NODATA}, got {a.get('nodata')!r}")
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
        if key in a and not (isinstance(got, list) and _same_numbers(got, value)):
            problems.append(f"{where}: attribute {key} must be {value}, got {got}")

    if "time" in members:
        stored = np.asarray(members["time"][:])
        expected_ms = np.array([parse_time(t) for t in meta.times], dtype="datetime64[ms]")
        if stored.shape != expected_ms.shape or not np.array_equal(
            stored.astype("datetime64[ms]"), expected_ms
        ):
            problems.append(f"{prefix}/time: values differ from chronozarr.times")
    if "band" in members and np.asarray(members["band"][:]).tolist() != list(meta.bands):
        problems.append(f"{prefix}/band: values differ from chronozarr.bands")
    if "x" in members and "y" in members:
        y, x = pixel_centers(attrs.transform, height, width)
        for name, expected_axis in (("y", y), ("x", x)):
            stored = np.asarray(members[name][:])
            if stored.shape != expected_axis.shape or not np.allclose(stored, expected_axis):
                problems.append(
                    f"{prefix}/{name}: values differ from pixel centres of the transform"
                )
    return attrs, (height, width)


def _check_consolidated(store: Any, direct: zarr.Group) -> list[str]:
    """Consolidated metadata must equal the per-array metadata it summarises."""
    consolidated = zarr.open_group(store, mode="r", zarr_format=3, use_consolidated=True)
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
    """Check a store against chronozarr v0.1. Returns a list of problems; empty means conforming.

    `store` is anything `zarr.open_group` accepts: a path, URL, or zarr Store. Per-node
    `zarr.json` files are checked directly; consolidated metadata, when present, must match them.
    """
    problems: list[str] = []
    try:
        root = zarr.open_group(store, mode="r", zarr_format=3, use_consolidated=False)
    except (GroupNotFoundError, FileNotFoundError):
        return [f"{store}: no Zarr v3 group found (is this a chronozarr store?)"]
    try:
        attrs = parse_root_attrs(root.attrs.asdict())
    except SchemaError as exc:
        return [str(exc)]

    base: LevelAttrs | None = None
    base_shape: tuple[int, int] | None = None
    for index, dataset in enumerate(attrs.datasets):
        level_attrs, shape = _check_level(
            root, index, dataset, attrs.chronozarr, base, base_shape, problems
        )
        if index == 0:
            base, base_shape = level_attrs, shape

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
        if base_shape is not None:
            expected_grid = grid_shape(*base_shape, attrs.datasets[0].pixels_per_tile)
            if volatility.shape != expected_grid:
                problems.append(
                    f"{path}: shape {volatility.shape} should equal the level-0 cell grid "
                    f"{expected_grid}"
                )
    return problems
