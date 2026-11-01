"""Import completed local Tile-Ripper COG observations without an intermediate cube.

This deliberately admits the qualified per-observation Zarr v2 writer only. Science
bands are explicit, QA stays a separate observation, and every selected timestep
must identify one source item on one exact native grid. No mosaicking, reprojection,
compositing, dtype conversion or calibration application occurs.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import numpy as np
import zarr

from chronozarr.encode import EncodeReport, encode
from chronozarr.schema import DTYPES, Band

_MANIFEST_LIMIT = 32 * 2**20
_METADATA_LIMIT = 2**20
_METADATA_TOTAL = 64 * 2**20
_FILE_LIMIT = 262_144
_TIME = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(Z|[+-]\d\d:\d\d)$")
_WRITER_OPTIONS = {"chunk_size", "codec", "level", "n_lods", "workers", "spill_dir"}


class _Watch:
    """Bounded local stat snapshots; these detect changes, not malicious replacement."""

    def __init__(self) -> None:
        self.files: dict[Path, tuple[int, ...]] = {}
        self.metadata_bytes = 0
        self.decoded_chunk_peak = 0
        self.encoded_chunk_peak = 0

    def add(self, path: Path) -> None:
        if path.is_symlink():
            raise ValueError(f"{path}: symlinks are not supported in an import")
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if path in self.files and self.files[path] != identity:
            raise ValueError(f"{path}: input changed during import")
        self.files[path] = identity
        if len(self.files) > _FILE_LIMIT:
            raise ValueError(f"input exceeds {_FILE_LIMIT} selected filesystem entries")

    def read_json(self, path: Path, *, limit: int = _METADATA_LIMIT) -> tuple[Any, bytes]:
        self.add(path)
        if path.stat().st_size > limit:
            raise ValueError(f"{path}: metadata exceeds {limit} bytes")
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError(f"{path}: metadata exceeds {limit} bytes")
        self.metadata_bytes += len(data)
        if self.metadata_bytes > _METADATA_TOTAL:
            raise ValueError("selected input metadata exceeds 64 MiB")
        self.add(path)
        try:
            return json.loads(data), data
        except (ValueError, UnicodeError) as exc:
            raise ValueError(f"{path}: invalid JSON: {exc}") from exc

    def check(self) -> None:
        for path in tuple(self.files):
            self.add(path)


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}: expected a nonempty string")
    return value


def _object(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{where}: expected a JSON object")
    return value


def _sequence(value: Any, where: str) -> list | tuple:
    if not isinstance(value, list | tuple):
        raise ValueError(f"{where}: expected a JSON array")
    return value


def _dtype(value: Any, where: str) -> np.dtype:
    if not isinstance(value, str):
        raise ValueError(f"{where}: dtype must be declared explicitly")
    try:
        return np.dtype(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{where}: invalid dtype {value!r}") from exc


def _time_ns(value: Any, where: str) -> int:
    text = _text(value, where)
    match = _TIME.fullmatch(text)
    if match is None:
        raise ValueError(f"{where}: expected an RFC3339 datetime with a timezone")
    base, fraction, zone = match.groups()
    try:
        moment = datetime.fromisoformat(base + zone.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{where}: invalid datetime {text!r}: {exc}") from exc
    elapsed = moment.astimezone(UTC) - datetime(1970, 1, 1, tzinfo=UTC)
    return (elapsed.days * 86400 + elapsed.seconds) * 10**9 + int((fraction or "").ljust(9, "0"))


def _scalar(attrs: dict, key: str, default: float, where: str) -> float:
    value = attrs.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{where}: {key} must be a finite scalar")
    if key == "scale_factor" and value == 0:
        raise ValueError(f"{where}: scale_factor must be nonzero")
    return float(value)


def _group_path(root: Path, name: Any) -> Path:
    name = _text(name, "output group")
    parts = PurePosixPath(name)
    if parts.is_absolute() or ".." in parts.parts or str(parts) != name:
        raise ValueError(f"output group {name!r} must be a normalized relative path")
    path = root.joinpath(*parts.parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"{name}: group escapes the input directory")
    return path


def _array(
    path: Path, shape: tuple[int, ...], dtype: np.dtype, watch: _Watch, payload_limit: int
) -> zarr.Array:
    metadata, _ = watch.read_json(path / ".zarray")
    attrs, _ = watch.read_json(path / ".zattrs")
    metadata = _object(metadata, str(path / ".zarray"))
    attrs = _object(attrs, str(path / ".zattrs"))
    if metadata.get("zarr_format") != 2:
        raise ValueError(f"{path}: only the per-observation Zarr v2 writer is supported")
    if metadata.get("compressor") is not None or metadata.get("order") != "C":
        raise ValueError(f"{path}: bridge requires uncompressed C-order source chunks")
    if (
        tuple(_sequence(metadata.get("shape"), f"{path} shape")) != shape
        or _dtype(metadata.get("dtype"), str(path)) != dtype
    ):
        raise ValueError(f"{path}: array shape/dtype disagrees with the manifest")
    expected_dims = [] if not shape else ["time"] if path.name == "time" else [path.name]
    if len(shape) == 3:
        expected_dims = ["time", "y", "x"]
    if attrs.get("_ARRAY_DIMENSIONS") != expected_dims:
        raise ValueError(f"{path}: unexpected _ARRAY_DIMENSIONS")
    chunks = _sequence(metadata.get("chunks"), f"{path} chunks")
    if len(chunks) != len(shape) or any(type(n) is not int or n < 1 for n in chunks):
        raise ValueError(f"{path}: invalid chunk shape")
    decoded_chunk_bytes = math.prod(chunks) * dtype.itemsize
    if decoded_chunk_bytes > payload_limit:
        raise ValueError(f"{path}: decoded source chunk exceeds max_timestep_bytes")
    if metadata.get("filters") is not None:
        raise ValueError(f"{path}: filters are outside the qualified COG output profile")
    watch.add(path)
    encoded_chunk_bytes = 0
    for entry in path.rglob("*"):
        watch.add(entry)
        if entry.is_file() and entry.name not in (".zarray", ".zattrs"):
            size = entry.stat().st_size
            if size > payload_limit:
                raise ValueError(f"{entry}: encoded source chunk exceeds max_timestep_bytes")
            if size != decoded_chunk_bytes:
                raise ValueError(
                    f"{entry}: uncompressed chunk byte length {size} differs from "
                    f"stored chunk shape ({decoded_chunk_bytes} bytes)"
                )
            encoded_chunk_bytes = max(encoded_chunk_bytes, size)
    # One chunk is decoded at a time. Reserve compressed bytes and conservative
    # decoded/result/coordinate-check scratch, including out-of-shape padding.
    if 4 * decoded_chunk_bytes + encoded_chunk_bytes > payload_limit:
        raise ValueError(f"{path}: source chunk decode/result scratch exceeds max_timestep_bytes")
    watch.decoded_chunk_peak = max(watch.decoded_chunk_peak, decoded_chunk_bytes)
    watch.encoded_chunk_peak = max(watch.encoded_chunk_peak, encoded_chunk_bytes)
    # Null fill means uninitialized storage in v2. Missing chunks must not become zeros.
    if metadata.get("fill_value") is None:
        separator = metadata.get("dimension_separator", ".")
        if separator not in (".", "/"):
            raise ValueError(f"{path}: unsupported chunk separator {separator!r}")
        ranges = [range(math.ceil(s / c)) for s, c in zip(shape, chunks, strict=True)]
        for index in itertools.product(*ranges):
            key = separator.join(map(str, index)) if index else "0"
            if not (path / key).is_file():
                raise ValueError(f"{path / key}: missing uninitialized source chunk")
    try:
        return zarr.open_array(str(path), mode="r", zarr_format=2)
    except Exception as exc:
        raise ValueError(f"{path}: cannot open source array: {exc}") from exc


@dataclass(frozen=True)
class _Observation:
    group: str
    record: dict
    source: dict
    instant: int
    crs: str
    transform: tuple[float, ...]
    shape: tuple[int, int, int]
    values: zarr.Array
    valid: zarr.Array
    aoi: zarr.Array
    band: Band

    def scene(self) -> str:
        # These are item-level fields, unlike asset-specific pins and band metadata.
        fields = (
            "collection",
            "item_id",
            "product",
            "product_version",
            "processing",
            "platform",
            "acquisition",
        )
        return json.dumps({k: self.source.get(k) for k in fields}, sort_keys=True)


def _observation(
    root: Path, record: dict, sources: dict, watch: _Watch, payload_limit: int
) -> _Observation:
    group = _text(record.get("group"), "output group")
    if record.get("file") != "." or record.get("dataset") != "values":
        raise ValueError(f"{group}: expected this store's per-observation values dataset")
    path = _group_path(root, group)
    attrs, _ = watch.read_json(path / ".zattrs")
    attrs = _object(attrs, f"{group} attributes")
    source = attrs.get("source")
    if not isinstance(source, dict):
        raise ValueError(f"{group}: missing source descriptor")
    asset = _text(record.get("asset_id"), f"{group} asset_id")
    if source.get("asset_id") != asset or attrs.get("asset_id") != asset:
        raise ValueError(f"{group}: source/manifest asset identities disagree")
    if asset not in sources or source != sources[asset]:
        raise ValueError(f"{group}: source descriptor differs from the completion manifest")
    for field in ("collection", "item_id", "requested_band"):
        _text(source.get(field), f"{group} source.{field}")
    identity = source.get("object_identity")
    etag = identity.get("etag") if isinstance(identity, dict) else None
    version = source.get("object_version")
    pins = [pin for pin in (etag, version) if isinstance(pin, str) and pin]
    if not pins and not source.get("local_identity"):
        raise ValueError(f"{group}: source has no pinned object or local identity")
    if len(set(pins)) > 1:
        raise ValueError(f"{group}: conflicting source object pins")
    if record.get("time_coordinate_role") is not None or attrs.get("time_coordinate_role"):
        raise ValueError(f"{group}: annual/time-bound summaries are not supported")
    instant = _time_ns(record.get("datetime"), f"{group} datetime")
    if _time_ns(attrs.get("datetime"), f"{group} group datetime") != instant:
        raise ValueError(f"{group}: group/manifest datetimes disagree")
    if _time_ns(source.get("datetime"), f"{group} source datetime") != instant:
        raise ValueError(f"{group}: source/manifest datetimes disagree")
    dimensions = _object(record.get("dimensions"), f"{group} dimensions")
    shape = tuple(_sequence(dimensions.get("shape"), f"{group} shape"))
    if (
        dimensions.get("names") != ["time", "y", "x"]
        or len(shape) != 3
        or shape[0] != 1
        or any(type(n) is not int or n < 1 for n in shape)
    ):
        raise ValueError(f"{group}: expected one positive [time,y,x] observation")
    dtype = _dtype(record.get("dtype"), group)
    if dtype.name not in DTYPES:
        raise ValueError(f"{group}: unsupported dtype {dtype}")
    if math.prod(shape) * (dtype.itemsize + 12) > payload_limit:
        raise ValueError(f"{group}: source plane exceeds max_timestep_bytes")
    grid = _object(record.get("native_grid"), f"{group} native_grid")
    crs = _text(grid.get("crs"), f"{group} CRS")
    transform = tuple(_sequence(grid.get("transform"), f"{group} transform"))
    if (
        len(transform) != 6
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in transform)
        or transform[1] <= 0
        or transform[5] >= 0
        or transform[2] != 0
        or transform[4] != 0
    ):
        raise ValueError(f"{group}: expected a finite north-up native transform")
    if (
        attrs.get("crs") != crs
        or tuple(_sequence(attrs.get("transform"), f"{group} group transform")) != transform
    ):
        raise ValueError(f"{group}: group/manifest native grids disagree")
    values = _array(path / "values", shape, dtype, watch, payload_limit)
    nodata = record.get("nodata")
    if nodata is not None and (
        type(nodata) not in (int, float)
        or not math.isfinite(nodata)
        or values.fill_value != nodata
    ):
        raise ValueError(f"{group}: array fill disagrees with manifest nodata")
    valid = _array(path / "valid", shape, np.dtype("uint8"), watch, payload_limit)
    aoi = _array(path / "aoi_mask", shape, np.dtype("uint8"), watch, payload_limit)
    for axis, length, origin, step in (
        ("x", shape[2], transform[0], transform[1]),
        ("y", shape[1], transform[3], transform[5]),
    ):
        array = _array(path / axis, (length,), np.dtype("float64"), watch, payload_limit)
        _check_axis(array, origin, step, f"{group}/{axis}")
    spatial = _array(path / "spatial_ref", (), np.dtype("int32"), watch, payload_limit)
    if spatial.attrs.get("crs", spatial.attrs.get("crs_wkt")) != crs:
        raise ValueError(f"{group}: spatial_ref/manifest CRS disagrees")
    time = _array(path / "time", (1,), np.dtype("int64"), watch, payload_limit)
    if (
        time.attrs.get("units") != "nanoseconds since 1970-01-01 00:00:00"
        or int(np.asarray(time[0]).item()) != instant
    ):
        raise ValueError(f"{group}: time coordinate disagrees with manifest datetime")
    value_attrs = dict(values.attrs)
    scale = _scalar(value_attrs, "scale_factor", 1.0, group)
    offset = _scalar(value_attrs, "add_offset", 0.0, group)
    units = value_attrs.get("units")
    if units is not None and not isinstance(units, str):
        raise ValueError(f"{group}: units must be a string")
    band = Band(name=source["requested_band"], scale=scale, offset=offset, units=units)
    policies = _sequence(record.get("derived_masks", []), f"{group} derived_masks")
    if any(not isinstance(p, dict) for p in policies):
        raise ValueError(f"{group}: derived_masks must contain policy objects")
    return _Observation(
        group, record, source, instant, crs, transform, shape, values, valid, aoi, band
    )


def _check_axis(array: zarr.Array, origin: float, step: float, where: str) -> None:
    """Validate one aligned coordinate chunk at a time, without cross-chunk reads."""
    length = array.shape[0]
    for start in range(0, length, array.chunks[0]):
        stop = min(start + array.chunks[0], length)
        try:
            found = np.asarray(array[start:stop])
        except Exception as exc:
            raise ValueError(f"{where}: cannot read coordinate samples: {exc}") from exc
        expected = origin + (np.arange(start, stop, dtype=np.float64) + 0.5) * step
        if not np.array_equal(found, expected):
            raise ValueError(f"{where}: coordinates disagree with native grid")


def _read(array: zarr.Array, where: str, *, binary: bool = False) -> np.ndarray:
    """Read a plane serially so Zarr never receives a selection spanning chunks."""
    height, width = array.shape[1:]
    plane = np.empty((height, width), dtype=array.dtype)
    try:
        for y in range(0, height, array.chunks[1]):
            ys = slice(y, min(y + array.chunks[1], height))
            for x in range(0, width, array.chunks[2]):
                xs = slice(x, min(x + array.chunks[2], width))
                plane[ys, xs] = array[0, ys, xs]
    except Exception as exc:
        raise ValueError(f"{where}: cannot read source samples: {exc}") from exc
    if binary and not np.isin(plane, [0, 1]).all():
        raise ValueError(f"{where}: expected a binary 0/1 mask")
    return plane


def convert_tile_ripper(
    source: str | Path,
    out: str | Path,
    *,
    bands: Sequence[str],
    quality_policy: str | None = None,
    groups: Sequence[str] | None = None,
    max_timestep_bytes: int = 128 * 2**20,
    **encode_options: Any,
) -> EncodeReport:
    """Write a Chronozarr store from a completed local COG fetch, without a stacked copy.

    ``bands`` names exact ``source.requested_band`` values in output order. ``groups``
    can explicitly restrict observation groups when the fetch includes other grids or
    ambiguous scenes; it must include QA observations when a ``quality_policy`` is
    selected. That policy must already exist in every source item's ``derived_masks``.
    Omitting it means physical/product/AOI validity only, with no cloud screening.

    Every selected band has fixed dtype/calibration through time, while different
    bands may have different scales/offsets/units. Values remain in their native dtype,
    including NaN payloads; NaN float samples are masked. Infinite values keep their
    declared source validity unless the selected QA policy excludes them. The shared mask is the
    intersection of all requested bands' valid/AOI planes and the chosen item's QA.

    ``max_timestep_bytes`` admits an estimate of adapter payload (two data frames,
    an input plane, masks and encoded/decode/result scratch for one serial chunk).
    Coordinate validation also reads one aligned source chunk at a time. The payload
    reservation is not a process RSS ceiling:
    the existing encoder has its own bounded cell/slab buffers and a temporary spill.
    Pixel payload memory does not grow with the number of timesteps. Metadata and
    file bookkeeping have separate byte/count limits. Encoder options supported here
    are ``chunk_size``, ``codec``, ``level``, ``n_lods``, ``workers`` and ``spill_dir``.
    The output uses the encoder's unsharded default. Returns its ``EncodeReport``.
    """
    root = Path(source).resolve(strict=True)
    destination = Path(out).resolve()
    if destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError("output must be outside the input directory and its ancestors")
    if not root.is_dir():
        raise ValueError("Tile-Ripper input must be a local completed output directory")
    if (
        isinstance(bands, str)
        or not bands
        or any(not isinstance(b, str) or not b for b in bands)
        or len(set(bands)) != len(bands)
    ):
        raise ValueError("bands must be a nonempty sequence of unique exact band names")
    if type(max_timestep_bytes) is not int or max_timestep_bytes < 1:
        raise ValueError("max_timestep_bytes must be positive")
    if unknown := set(encode_options) - _WRITER_OPTIONS:
        raise ValueError(f"unsupported encoder options for Tile-Ripper import: {sorted(unknown)}")
    if (spill := encode_options.get("spill_dir")) and Path(spill).resolve().is_relative_to(root):
        raise ValueError("spill_dir must be outside the input directory")
    if quality_policy is not None:
        _text(quality_policy, "quality_policy")
    watch = _Watch()
    manifest, manifest_bytes = watch.read_json(root / "manifest.json", limit=_MANIFEST_LIMIT)
    if (
        not isinstance(manifest, dict)
        or manifest.get("document_type") != "acquisition_result"
        or manifest.get("status") != "complete"
        or manifest.get("format") != "zarr_v2"
    ):
        raise ValueError("expected a complete acquisition_result from the Zarr v2 COG writer")
    root_attrs, _ = watch.read_json(root / ".zattrs")
    if _object(root_attrs, "input root attributes").get("status") != "complete":
        raise ValueError("input root is not marked complete")
    records = manifest.get("outputs")
    if not isinstance(records, list) or not records or len(records) > 65536:
        raise ValueError("manifest needs 1..65536 output records")
    for record in records:
        _text(_object(record, "manifest output record").get("group"), "output group")
    if groups is not None and (
        isinstance(groups, str)
        or not isinstance(groups, Sequence)
        or any(not isinstance(g, str) for g in groups)
    ):
        raise ValueError("groups must be a sequence of exact observation group names")
    selected = None if groups is None else set(groups)
    if selected is not None and (
        not selected or not selected <= {r.get("group") for r in records}
    ):
        raise ValueError("groups must select existing observation groups")
    raw_sources = manifest.get("sources")
    if not isinstance(raw_sources, list) or any(not isinstance(s, dict) for s in raw_sources):
        raise ValueError("manifest sources must be a list of source descriptors")
    sources = {_text(s.get("asset_id"), "manifest source asset_id"): s for s in raw_sources}
    if len(sources) != len(raw_sources):
        raise ValueError("manifest has duplicate source asset identities")
    observations = []
    seen_groups = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("manifest output records must be objects")
        group = record.get("group")
        if group in seen_groups:
            raise ValueError(f"duplicate output group {group!r}")
        seen_groups.add(group)
        if selected is None or group in selected:
            observations.append(_observation(root, record, sources, watch, max_timestep_bytes))
    science = [o for o in observations if o.band.name in bands]
    if not science:
        raise ValueError(f"no observations of requested bands {list(bands)}")
    first = science[0]
    grid = (first.crs, first.transform, first.shape)
    dtype = first.values.dtype
    by_time: dict[int, dict[str, _Observation]] = {
        o.instant // 10**6: {}
        for o in observations
        if o.source["collection"] == first.source["collection"]
        and (o.crs, o.transform, o.shape) == grid
    }
    for observation in science:
        for field in ("collection", "product", "product_version"):
            if observation.source.get(field) != first.source.get(field):
                raise ValueError(
                    f"{observation.group}: selected science series spans different {field} values"
                )
        if (observation.crs, observation.transform, observation.shape) != grid:
            raise ValueError(
                f"{observation.group}: science bands span different native grids; "
                "select one grid with groups"
            )
        if observation.values.dtype != dtype:
            raise ValueError("selected science bands have different native dtypes")
        slot = by_time.setdefault(observation.instant // 10**6, {})
        if observation.band.name in slot or (
            slot and observation.scene() != next(iter(slot.values())).scene()
        ):
            raise ValueError(f"{observation.group}: ambiguous observations at one millisecond")
        slot[observation.band.name] = observation
    ordered = []
    descriptions: dict[str, Band] = {}
    quality_arrays = []
    used: dict[str, _Observation] = {}
    for ms, slot in sorted(by_time.items()):
        if missing := set(bands) - set(slot):
            raise ValueError(f"timestep {ms}: missing requested bands {sorted(missing)}")
        members = [slot[b] for b in bands]
        if len({o.instant for o in members}) != 1:
            raise ValueError(
                f"timestep {ms}: band acquisition times disagree below one millisecond"
            )
        for observation in members:
            if observation.band != descriptions.setdefault(
                observation.band.name, observation.band
            ):
                raise ValueError(
                    f"{observation.group}: calibration/units changed for band "
                    f"{observation.band.name}; select a uniform series"
                )
            used[observation.group] = observation
        quality = None
        if quality_policy is not None:
            matches = []
            for o in observations:
                for policy in o.record.get("derived_masks", []):
                    if (
                        policy.get("policy") == quality_policy
                        and o.instant == members[0].instant
                        and o.scene() == members[0].scene()
                    ):
                        matches.append((o, policy))
            if len(matches) != 1:
                raise ValueError(
                    f"timestep {ms}: expected exactly one matching {quality_policy} "
                    f"QA observation, found {len(matches)}"
                )
            qa, policy = matches[0]
            if (qa.crs, qa.transform, qa.shape) != grid:
                raise ValueError(f"{qa.group}: QA and reflectance grids disagree")
            name = policy.get("dataset")
            if name != f"quality_{quality_policy}":
                raise ValueError(f"{qa.group}: unexpected QA dataset {name!r}")
            quality = _array(
                _group_path(root, qa.group) / name,
                qa.shape,
                np.dtype("uint8"),
                watch,
                max_timestep_bytes,
            )
            if quality.attrs.get("quality_policy") != quality_policy:
                raise ValueError(f"{qa.group}/{name}: policy metadata disagrees")
            used[qa.group] = qa
            quality = (qa, quality)
        ordered.append(members)
        quality_arrays.append(quality)
    height, width = first.shape[1:]
    source_scratch = 4 * watch.decoded_chunk_peak + watch.encoded_chunk_peak
    reservation = (
        height * width * (2 * len(bands) * dtype.itemsize + dtype.itemsize + 12) + source_scratch
    )
    if reservation > max_timestep_bytes:
        raise ValueError(
            f"adapter payload reservation {reservation} exceeds max_timestep_bytes "
            f"{max_timestep_bytes}; narrow the AOI or raise the explicit limit"
        )
    notes = {
        "importer": "tile_ripper",
        "input": str(root),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "identity_scope": "manifest hash identifies metadata, not payload; local size/mtime/"
        "ctime/inode snapshots checked through the final source read",
        "quality_policy": quality_policy,
        "mask": "AND of selected band valid/AOI planes "
        "and selected item's QA; NaN float samples excluded; "
        "infinite values retain source validity",
        "adapter_payload_bytes": reservation,
        "processing": "native values and dtype; no scale application, mosaic, resampling or "
        "composite; invalid NaN payloads preserved",
        "observations": [
            {
                "group": o.group,
                "datetime": o.record["datetime"],
                "source": o.source,
                "value_attributes": dict(o.values.attrs),
                "derived_masks": o.record.get("derived_masks", []),
            }
            for o in used.values()
        ],
    }
    pending: list[np.ndarray] = []
    reference_aoi: np.ndarray | None = None
    # encode() spills each frame synchronously before requesting the next one.
    # Reuse its storage so the iterable's retained first frame cannot multiply
    # full-frame memory across subsequent generator/consumer iterations.
    data = np.empty((len(bands), height, width), dtype=dtype)

    def timesteps() -> Iterator[np.ndarray]:
        nonlocal reference_aoi
        for members, quality in zip(ordered, quality_arrays, strict=True):
            if pending:
                raise RuntimeError("encoder requested data before consuming the preceding mask")
            mask = np.ones((height, width), dtype=np.uint8)
            for b, observation in enumerate(members):
                data[b] = _read(observation.values, f"{observation.group}/values")
                aoi = _read(observation.aoi, f"{observation.group}/aoi_mask", binary=True)
                if reference_aoi is None:
                    reference_aoi = aoi.copy()
                elif not np.array_equal(aoi, reference_aoi):
                    raise ValueError(
                        f"{observation.group}: AOI mask varies within the selected grid"
                    )
                valid = _read(observation.valid, f"{observation.group}/valid", binary=True)
                if np.any(valid > aoi):
                    raise ValueError(f"{observation.group}: valid includes pixels outside the AOI")
                nodata = observation.record.get("nodata")
                if nodata is not None and np.any(valid & (data[b] == nodata)):
                    raise ValueError(f"{observation.group}: valid includes source nodata samples")
                mask &= valid
                mask &= aoi
                if dtype.kind == "f":
                    mask &= ~np.isnan(data[b])
            if quality is not None:
                qa, quality_array = quality
                qa_aoi = _read(qa.aoi, f"{qa.group}/aoi_mask", binary=True)
                assert reference_aoi is not None
                if not np.array_equal(qa_aoi, reference_aoi):
                    raise ValueError(f"{qa.group}: QA AOI mask differs from science observations")
                qa_valid = _read(qa.valid, f"{qa.group}/valid", binary=True)
                accepted = _read(quality_array, str(quality_array.path), binary=True)
                if np.any(qa_valid > qa_aoi) or np.any(accepted > qa_valid):
                    raise ValueError(f"{qa.group}: quality mask includes invalid QA samples")
                mask &= qa_aoi
                mask &= qa_valid
                mask &= accepted
            pending.append(mask)
            yield data
        watch.check()

    def masks() -> Iterator[np.ndarray]:
        for _ in ordered:
            if not pending:
                raise RuntimeError("encoder requested a mask before reading its timestep")
            yield pending.pop()

    return encode(
        timesteps(),
        out,
        times=np.array(sorted(by_time), dtype="datetime64[ms]"),
        bands=[descriptions[b] for b in bands],
        crs=first.crs,
        transform=(
            first.transform[1],
            first.transform[2],
            first.transform[0],
            first.transform[4],
            first.transform[5],
            first.transform[3],
        ),
        nodata=None,
        mask=masks(),
        provenance={
            "sources": list(
                dict.fromkeys(
                    o.source.get("href") or o.source.get("reference") or o.source["collection"]
                    for o in used.values()
                )
            ),
            "composite": "none: one source item per timestep",
            "gap_fill": "none",
            "notes": json.dumps(notes, sort_keys=True),
        },
        **encode_options,
    )
