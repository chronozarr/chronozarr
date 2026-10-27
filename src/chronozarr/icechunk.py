"""Explicit, snapshot-pinned Icechunk inputs for the ordinary conversion pipeline.

The maintained Icechunk reader resolves references and Zarr decodes its codecs. This module
selects a bounded variable before the converter reads pixels; it does not rechunk the source
or promise that a small selection means a small download. No ambient cloud credentials are
used. Virtual source prefixes require an explicit anonymous-access allowlist.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import numpy as np
import xarray as xr

from chronozarr._install import extra_hint
from chronozarr.store import redact_url

IDENTITY = "_chronozarr_icechunk"


def parse_indexers(entries: tuple[str, ...]) -> dict[str, int | slice]:
    """Parse repeated CLI DIM=INDEX or DIM=START:STOP (exclusive stop) selections."""
    result: dict[str, int | slice] = {}
    for entry in entries:
        name, sep, value = entry.partition("=")
        if not sep or not name or name in result:
            raise ValueError(
                f"invalid or repeated --isel {entry!r}; use DIM=INDEX or DIM=START:STOP"
            )
        try:
            if ":" in value:
                start, stop = value.split(":")
                result[name] = slice(int(start), int(stop))
            else:
                result[name] = int(value)
        except ValueError as exc:
            raise ValueError(f"invalid --isel {entry!r}; use DIM=INDEX or DIM=START:STOP") from exc
    return result


def _selection(indexers: Mapping[str, int | slice]) -> dict[str, int | list[int]]:
    return {k: [v.start, v.stop] if isinstance(v, slice) else v for k, v in indexers.items()}


@dataclass(frozen=True)
class IcechunkInput:
    """An anonymous HTTPS or local repository, version selector and finite integer window.

    Every dimension of the selected data variable must have an explicit index or slice.
    Choose one run for a forecast: its valid times become init_time + lead_time. float64
    values are refused unless ``cast_float32=True`` explicitly requests rounding at staging.
    Limits cover selected decoded output and a decoded source chunk, not total wire traffic
    or process RSS. The source's original chunking remains the unit of physical work.
    """

    repository: str | Path
    indexers: Mapping[str, int | slice]
    branch: str | None = None
    tag: str | None = None
    snapshot: str | None = None
    virtual_prefixes: tuple[str, ...] = ()
    cast_float32: bool = False
    max_output_bytes: int = 512 * 1024 * 1024
    max_chunk_bytes: int = 128 * 1024 * 1024
    encoded_cache_bytes: int = 32 * 1024 * 1024

    def __post_init__(self) -> None:
        if sum(v is not None for v in (self.branch, self.tag, self.snapshot)) > 1:
            raise ValueError("choose one Icechunk branch, tag or snapshot")
        for name, value in self.indexers.items():
            if not isinstance(name, str) or not name:
                raise ValueError("Icechunk dimension names must be nonempty strings")
            if isinstance(value, slice):
                if (
                    type(value.start) is not int
                    or type(value.stop) is not int
                    or value.start < 0
                    or value.stop <= value.start
                    or value.step not in (None, 1)
                ):
                    raise ValueError(
                        f"{name}: give a finite nonempty START:STOP slice with step 1"
                    )
            elif type(value) is not int or value < 0:
                raise ValueError(f"{name}: give a nonnegative integer index or finite slice")
        object.__setattr__(self, "indexers", MappingProxyType(dict(self.indexers)))
        object.__setattr__(self, "virtual_prefixes", tuple(self.virtual_prefixes))
        if type(self.cast_float32) is not bool:
            raise ValueError("cast_float32 must explicitly be True or False")
        if type(self.max_output_bytes) is not int or self.max_output_bytes <= 0:
            raise ValueError("max_output_bytes must be a positive integer")
        if type(self.max_chunk_bytes) is not int or self.max_chunk_bytes <= 0:
            raise ValueError("max_chunk_bytes must be a positive integer")
        if type(self.encoded_cache_bytes) is not int or self.encoded_cache_bytes < 0:
            raise ValueError("encoded_cache_bytes must be a nonnegative integer")
        text = str(self.repository)
        url = urlsplit(text)
        if url.scheme and url.scheme != "https":
            raise ValueError("Icechunk repositories use anonymous HTTPS or a local path")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError(
                "Icechunk repository URLs must not contain credentials, query or fragment"
            )
        if any(
            not p or p.isspace() for p in (self.branch, self.tag, self.snapshot) if p is not None
        ):
            raise ValueError("Icechunk version selectors must be nonempty")
        for prefix in self.virtual_prefixes:
            parsed = urlsplit(prefix)
            if (
                parsed.scheme not in ("s3", "https")
                or not parsed.netloc
                or not prefix.endswith("/")
            ):
                raise ValueError(
                    "virtual prefixes must be explicit s3:// or https:// paths ending /"
                )
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(
                    "virtual prefixes must not contain credentials, query or fragment"
                )

    def __str__(self) -> str:
        return str(self.repository)


def _open_repository(source: IcechunkInput) -> Any:
    try:
        icechunk = import_module("icechunk")
    except ImportError as exc:
        raise ValueError(
            "Icechunk conversion requires Python 3.12+ and " + extra_hint("icechunk")
        ) from exc
    text = str(source.repository)
    storage = (
        icechunk.http_storage(text)
        if text.startswith("https://")
        else icechunk.local_filesystem_storage(text)
    )
    credentials: dict[str, Any] = {
        prefix: (
            icechunk.Credentials.S3(icechunk.s3_anonymous_credentials())
            if prefix.startswith("s3://")
            else icechunk.Credentials.HttpAccess()
        )
        for prefix in source.virtual_prefixes
    }
    # The maintained cache is byte-weighted and keyed by immutable ChunkId + exact range.
    # It reuses encoded logical payloads and shard indexes without requesting whole shards.
    # Virtual external bytes have no captured object identity, so keep their cache disabled.
    config = icechunk.RepositoryConfig(
        caching=icechunk.CachingConfig(
            num_bytes_chunks=0 if source.virtual_prefixes else source.encoded_cache_bytes
        )
    )
    return icechunk.Repository.open(
        storage, config=config, authorize_virtual_chunk_access=credentials
    )


def _forecast_times(dataset: xr.Dataset, variable: str) -> tuple[xr.Dataset, str | None]:
    da = dataset[variable]
    if "lead_time" not in da.dims:
        return dataset, None
    if "init_time" not in dataset.coords or dataset.init_time.size != 1:
        raise ValueError(
            "forecast import needs one explicit init_time index; lead_time is not a date"
        )
    init = np.asarray(dataset.init_time.values).reshape(-1)[0]
    lead = np.asarray(dataset.lead_time.values)
    if not np.issubdtype(np.asarray(init).dtype, np.datetime64) or not np.issubdtype(
        lead.dtype, np.timedelta64
    ):
        raise ValueError(
            "forecast init_time and lead_time must decode to datetimes and timedeltas"
        )
    if "valid_time" in dataset.dims or "valid_time" in dataset.variables:
        raise ValueError(
            "forecast already declares valid_time; select/prepare this source explicitly"
        )
    dataset = dataset.assign_coords(valid_time=("lead_time", init + lead)).swap_dims(
        {"lead_time": "valid_time"}
    )
    return (
        dataset,
        f"forecast valid_time = init_time ({init}) + lead_time; one run, no compositing",
    )


def open_input(source: IcechunkInput, variable: str | None, mask_var: str | None) -> xr.Dataset:
    """Resolve one snapshot and return a lazy, selected dataset with its immutable identity."""
    if variable is None:
        raise ValueError("Icechunk conversion needs an explicit variable (--variable)")
    repo = _open_repository(source)
    session = repo.readonly_session(
        branch=source.branch
        or ("main" if source.tag is None and source.snapshot is None else None),
        tag=source.tag,
        snapshot_id=source.snapshot,
    )
    # gribberish registers its maintained Zarr codec through its installed entry point.
    dataset = xr.open_zarr(
        session.store,
        chunks=None,
        consolidated=False,
        mask_and_scale=False,
        decode_timedelta=True,
    )
    try:
        if variable not in dataset.data_vars:
            raise ValueError(
                f"Icechunk variable {variable!r} not found; choose {list(dataset.data_vars)}"
            )
        original = dataset[variable]
        if original.dtype == np.dtype("float64") and not source.cast_float32:
            raise ValueError(
                f"{variable} is float64, which chronozarr cannot store; explicitly request "
                "cast_float32=True (--cast-float32) to round it, or choose another source"
            )
        missing = set(original.dims) - set(source.indexers)
        unknown = set(source.indexers) - set(original.dims)
        if missing or unknown:
            raise ValueError(
                f"Icechunk needs an explicit index or bounded slice for every variable dimension; "
                f"missing {sorted(missing)}, unknown {sorted(unknown)}"
            )
        for name, index in source.indexers.items():
            stop = index.stop if isinstance(index, slice) else index + 1
            if stop > original.sizes[name]:
                raise ValueError(
                    f"{name}: selection ends at {stop}, dimension has {original.sizes[name]}"
                )
        selected_shape = [
            index.stop - index.start
            for index in source.indexers.values()
            if isinstance(index, slice)
        ]
        out_dtype = np.dtype("float32") if source.cast_float32 else original.dtype
        selected_bytes = math.prod(selected_shape) * out_dtype.itemsize
        chunks = tuple(original.encoding.get("chunks", original.shape))
        source_chunk_bytes = math.prod(chunks) * original.dtype.itemsize
        if mask_var is not None:
            if mask_var not in dataset.variables:
                raise ValueError(f"mask variable {mask_var!r} not found in Icechunk snapshot")
            mask = dataset[mask_var]
            if not set(mask.dims) <= set(source.indexers):
                raise ValueError("Icechunk mask has dimensions outside the selected data variable")
            mask_shape = [
                index.stop - index.start
                for d in mask.dims
                if isinstance(index := source.indexers[d], slice)
            ]
            selected_bytes += math.prod(mask_shape) * mask.dtype.itemsize
            mask_chunks = tuple(mask.encoding.get("chunks", mask.shape))
            if math.prod(mask_chunks) * mask.dtype.itemsize > source.max_chunk_bytes:
                raise ValueError("decoded source mask chunk exceeds max_chunk_bytes")
        if selected_bytes > source.max_output_bytes:
            raise ValueError(
                f"selected decoded output {selected_bytes} exceeds "
                f"max_output_bytes {source.max_output_bytes}"
            )
        if source_chunk_bytes > source.max_chunk_bytes:
            raise ValueError(
                f"decoded source chunk {source_chunk_bytes} exceeds "
                f"max_chunk_bytes {source.max_chunk_bytes}"
            )
        if source.cast_float32 and original.dtype != np.dtype("float64"):
            raise ValueError("cast_float32 is only for an explicitly rounded float64 source")
        names = [variable] + ([mask_var] if mask_var is not None else [])
        mapping = original.attrs.get("grid_mapping", original.encoding.get("grid_mapping"))
        names += [
            n for n in (mapping, "spatial_ref", "crs") if isinstance(n, str) and n in dataset
        ]
        selected = dataset[list(dict.fromkeys(names))].isel(dict(source.indexers))
        selected, time_note = _forecast_times(selected, variable)
        identity = {
            "repository": redact_url(str(source.repository)),
            "snapshot": str(session.snapshot_id),
            "requested_version": {
                "branch": source.branch
                or ("main" if not source.tag and not source.snapshot else None),
                "tag": source.tag,
                "snapshot": source.snapshot,
            },
            "variable": variable,
            "indexers": _selection(source.indexers),
            "source_dtype": original.dtype.name,
            "output_dtype": out_dtype.name,
            "source_chunk_shape": list(chunks),
            "decoded_source_chunk_bytes": source_chunk_bytes,
            "selected_output_bytes": selected_bytes,
            "virtual_prefixes": list(source.virtual_prefixes),
            "encoded_cache_bytes": 0 if source.virtual_prefixes else source.encoded_cache_bytes,
            "external_objects": (
                "identity not captured; snapshot pins references, not external object bytes"
                if source.virtual_prefixes
                else "no virtual access authorized"
            ),
            "forecast": time_note,
        }
        selected.attrs = {**selected.attrs, IDENTITY: identity}
        # Keep the store/session alive through the backend arrays. Closing a selected Dataset
        # otherwise loses xarray's original closer after selection in some xarray releases.
        selected.set_close(dataset.close)
        return selected
    except BaseException:
        dataset.close()
        raise


def identity_note(identity: Mapping[str, Any]) -> str:
    """Stable JSON records snapshot, selection and explicit conversion in output provenance."""
    return "Icechunk import: " + json.dumps(dict(identity), sort_keys=True, separators=(",", ":"))
