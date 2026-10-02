"""chronozarr reader: open a store, undo star-delta, and return raw or physical values."""

from __future__ import annotations

import asyncio
import builtins
import time
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import numpy as np
import xarray as xr
import zarr
from zarr.abc.store import (
    ByteRequest,
    OffsetByteRequest,
    RangeByteRequest,
    Store,
)
from zarr.core.buffer import Buffer, BufferPrototype
from zarr.errors import GroupNotFoundError

from chronozarr import schema
from chronozarr.schema import Chronozarr, SchemaError, Transform


@dataclass(frozen=True)
class Level:
    """One pyramid level of a store."""

    index: int
    shape: tuple[int, int, int, int]  # (time, band, y, x)
    transform: Transform
    resolution: float
    chunk_size: int
    grid: tuple[int, int]  # (rows, cols) of cells
    data: zarr.Array
    mask: zarr.Array | None = None
    coverage: zarr.Array | None = None

    @property
    def shard_time(self) -> int | None:
        """Timesteps per shard along time; None for an unsharded array."""
        return None if self.data.shards is None else int(self.data.shards[0])


class ChronoStore:
    """A chronozarr store opened for reading. Use `open_store` to create one."""

    def __init__(self, group: zarr.Group, source: str) -> None:
        root = schema.parse_root_attrs(group.attrs.asdict())
        self.source = source
        self.attrs: Chronozarr = root.chronozarr
        self.times: np.ndarray = np.array(
            [schema.parse_time(t) for t in root.chronozarr.times], dtype="datetime64[ms]"
        )
        self.bands: tuple[str, ...] = root.chronozarr.band_names
        levels = []
        for index, dataset in enumerate(root.datasets):
            where = f"level {dataset.path}"
            level_group = schema.get_group(group, dataset.path, "store")
            data = schema.get_array(level_group, root.chronozarr.variable, where)
            level_attrs = schema.parse_level_attrs(level_group.attrs.asdict(), where)
            cs = schema.cell_size(data, f"{where}/data")
            shape = (data.shape[0], data.shape[1], data.shape[2], data.shape[3])
            levels.append(
                Level(
                    index=index,
                    shape=shape,
                    transform=level_attrs.transform,
                    resolution=level_attrs.resolution,
                    chunk_size=cs,
                    grid=schema.grid_shape(shape[2], shape[3], cs),
                    data=data,
                    mask=_plane(level_group, root.chronozarr.mask_variable, where),
                    coverage=_plane(level_group, root.chronozarr.coverage_variable, where),
                )
            )
        self.levels: tuple[Level, ...] = tuple(levels)
        self.dtype: np.dtype = levels[0].data.dtype
        if self.attrs.temporal.encoding == schema.STAR_DELTA and (
            self.dtype.name not in schema.TEMPORAL_DTYPES
        ):
            raise SchemaError(f"star-delta needs uint8 or uint16 data, got {self.dtype}")
        self.nodata: int | float | None = self.attrs.nodata
        self._scale = np.array(
            [1.0 if b.scale is None else b.scale for b in self.attrs.bands], dtype=np.float32
        )
        self._offset = np.array(
            [0.0 if b.offset is None else b.offset for b in self.attrs.bands], dtype=np.float32
        )

    def _level(self, lod: int) -> Level:
        if not 0 <= lod < len(self.levels):
            raise IndexError(f"lod {lod} out of range: store has levels 0..{len(self.levels) - 1}")
        return self.levels[lod]

    def _check_time(self, t: int) -> int:
        if not 0 <= t < len(self.times):
            raise IndexError(f"timestep {t} out of range: store has {len(self.times)} timesteps")
        return int(t)

    def _window(self, level: Level, row: int, col: int, lod: int) -> tuple[slice, slice]:
        rows, cols = level.grid
        if not (0 <= row < rows and 0 <= col < cols):
            raise IndexError(
                f"cell ({row}, {col}) out of range: level {lod} has a {rows} x {cols} cell grid"
            )
        cs = level.chunk_size
        return (
            slice(row * cs, min((row + 1) * cs, level.shape[2])),
            slice(col * cs, min((col + 1) * cs, level.shape[3])),
        )

    def _read_region(
        self, level: Level, timesteps: Sequence[int], ys: slice, xs: slice
    ) -> np.ndarray:
        """Decode `timesteps` over a spatial window: (len(timesteps), band, y, x) raw values.

        Reads each needed anchor once plus each requested delta, in a single Zarr read. A delta
        timestep adds its residual to the anchor modulo 2^bits (unsigned arithmetic wraps).
        """
        reference = self.attrs.temporal.delta_reference
        needed = sorted({*timesteps, *(reference[t] for t in timesteps if t in reference)})
        raw = np.asarray(level.data.oindex[np.asarray(needed), :, ys, xs])
        row = {t: i for i, t in enumerate(needed)}
        out = np.empty((len(timesteps), *raw.shape[1:]), dtype=raw.dtype)
        for i, t in enumerate(timesteps):
            if t in reference:
                np.add(raw[row[reference[t]]], raw[row[t]], out=out[i])
            else:
                out[i] = raw[row[t]]
        return out

    def _read_plane(
        self, array: zarr.Array, timesteps: Sequence[int], ys: slice, xs: slice
    ) -> np.ndarray:
        """Mask or coverage over a window: (len(timesteps), y, x) uint8."""
        return np.asarray(array.oindex[np.asarray(list(timesteps)), ys, xs])

    def _physical_region(
        self, level: Level, timesteps: Sequence[int], ys: slice, xs: slice
    ) -> np.ndarray:
        """Physical values over a window as float32 (time, band, y, x), NaN where invalid."""
        raw = self._read_region(level, timesteps, ys, xs)
        if level.mask is not None:
            valid = self._read_plane(level.mask, timesteps, ys, xs).astype(bool)[:, None]
        elif self.nodata is not None:
            valid = raw != self.dtype.type(self.nodata)
        else:
            valid = None
        values = raw.astype(np.float32)
        values *= self._scale[None, :, None, None]
        values += self._offset[None, :, None, None]
        if valid is not None:
            np.putmask(values, ~np.broadcast_to(valid, values.shape), np.float32(np.nan))
        return values

    def read(self, t: int, lod: int = 0) -> np.ndarray:
        """Decode timestep `t` of level `lod` as a (band, y, x) array of the stored dtype."""
        level = self._level(lod)
        window = slice(None)
        return self._read_region(level, [self._check_time(t)], window, window)[0]

    def read_cell(self, t: int, row: int, col: int, lod: int = 0) -> np.ndarray:
        """Decode one cell of timestep `t`: a (band, y, x) array of the stored dtype.

        Costs at most two chunk reads (the anchor and the delta). Edge cells are smaller than
        `chunk_size`; padding beyond the level shape is never returned.
        """
        level = self._level(lod)
        ys, xs = self._window(level, row, col, lod)
        return self._read_region(level, [self._check_time(t)], ys, xs)[0]

    def physical(self, t: int, lod: int = 0) -> np.ndarray:
        """Timestep `t` of level `lod` as float32 (band, y, x) physical values.

        value = stored * scale + offset per band; NaN where the pixel is invalid (mask is 0,
        or without a mask the stored value equals nodata).
        """
        level = self._level(lod)
        window = slice(None)
        return self._physical_region(level, [self._check_time(t)], window, window)[0]

    def read_mask(self, t: int, lod: int = 0) -> np.ndarray | None:
        """The (y, x) uint8 validity plane of timestep `t` (1 = valid), or None if absent."""
        level = self._level(lod)
        if level.mask is None:
            return None
        window = slice(None)
        return self._read_plane(level.mask, [self._check_time(t)], window, window)[0]

    def read_coverage(self, t: int, lod: int = 0) -> np.ndarray | None:
        """The (y, x) uint8 observation count of timestep `t`, or None if absent."""
        level = self._level(lod)
        if level.coverage is None:
            return None
        window = slice(None)
        return self._read_plane(level.coverage, [self._check_time(t)], window, window)[0]

    def _band_metadata(self, *, physical: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        coords: dict[str, Any] = {}
        attrs: dict[str, Any] = {}
        bands = self.attrs.bands
        if any(b.units for b in bands):
            coords["band_units"] = ("band", [b.units or "" for b in bands])
            units = {b.units for b in bands}
            unscaled = all(b.scale in (None, 1) and b.offset in (None, 0) for b in bands)
            if len(units) == 1 and (physical or unscaled):
                attrs["units"] = bands[0].units
        return coords, attrs

    def to_xarray(
        self, lod: int = 0, times: Sequence[int] | None = None, *, physical: bool = False
    ) -> xr.DataArray:
        """Decode a level into a DataArray with dims (time, band, y, x) and full coordinates.

        `times` selects timestep indices (default: all). Values are the stored dtype, or float32
        physical values with NaN for invalid pixels when `physical` is true. The result is
        loaded into memory. When the store has a mask, validity is carried by a `mask` coordinate
        (uint8, dims time/y/x, 1 = valid) and no `nodata` attribute is set.
        """
        level = self._level(lod)
        selected = (
            list(range(len(self.times))) if times is None else [self._check_time(t) for t in times]
        )
        window = slice(None)
        read = self._physical_region if physical else self._read_region
        values = read(level, selected, window, window)
        y, x = schema.pixel_centers(level.transform, level.shape[2], level.shape[3])
        attrs: dict[str, Any] = {"crs": self.attrs.crs, "transform": list(level.transform)}
        coords: dict[str, Any] = {
            "time": self.times[selected].astype("datetime64[ns]"),
            "band": list(self.bands),
            "y": y,
            "x": x,
        }
        band_coords, band_attrs = self._band_metadata(physical=physical)
        coords.update(band_coords)
        attrs.update(band_attrs)
        if level.mask is not None:
            coords["mask"] = (
                schema.PLANE_DIMENSIONS,
                self._read_plane(level.mask, selected, window, window),
            )
        elif self.nodata is not None and not physical:
            attrs["nodata"] = self.nodata
        return xr.DataArray(
            values,
            dims=schema.DIMENSIONS,
            coords=coords,
            name=self.attrs.variable,
            attrs=attrs,
        )


# --- HTTP reading without fsspec --------------------------------------------------------------

_RETRY_DELAYS_S = (0.2, 0.6, 1.5)
# Some CDNs reject urllib's default "Python-urllib" agent with a 403.
_USER_AGENT = "chronozarr (+https://github.com/chronozarr/chronozarr)"


def _range_header(byte_range: ByteRequest) -> str:
    if isinstance(byte_range, RangeByteRequest):
        return f"bytes={byte_range.start}-{byte_range.end - 1}"
    if isinstance(byte_range, OffsetByteRequest):
        return f"bytes={byte_range.offset}-"
    return f"bytes=-{byte_range.suffix}"


def _slice_body(body: bytes, byte_range: ByteRequest | None) -> bytes:
    """Apply a byte range locally, for a server that ignored the Range header."""
    if byte_range is None:
        return body
    if isinstance(byte_range, RangeByteRequest):
        return body[byte_range.start : byte_range.end]
    if isinstance(byte_range, OffsetByteRequest):
        return body[byte_range.offset :]
    return body[-byte_range.suffix :] if byte_range.suffix else b""


class HttpStore(Store):
    """A read-only Zarr store over HTTP(S), standard library only (no fsspec or aiohttp).

    Sharded arrays are read with `Range` requests, so the server must honour them (206). A
    missing object (404) is a missing key. 5xx, 429 and connection errors are retried three
    times; any other failure raises OSError naming the URL. The store cannot be listed.
    """

    def __init__(self, url: str, *, timeout: float = 30.0) -> None:
        super().__init__(read_only=True)
        self.url = url.rstrip("/")
        self.timeout = timeout

    @property
    def supports_writes(self) -> bool:
        return False

    @property
    def supports_deletes(self) -> bool:
        return False

    @property
    def supports_listing(self) -> bool:
        return False

    def __eq__(self, value: object) -> bool:
        return isinstance(value, HttpStore) and value.url == self.url

    def __hash__(self) -> int:
        return hash(self.url)

    def __repr__(self) -> str:
        return f"HttpStore({self.url!r})"

    def _fetch(
        self, key: str, method: str, byte_range: ByteRequest | None = None
    ) -> tuple[bytes, Any] | None:
        """(body, headers) of one request, or None on 404. Retries transient failures."""
        url = f"{self.url}/{quote(key, safe='/')}"
        headers = {"User-Agent": _USER_AGENT}
        if byte_range is not None:
            headers["Range"] = _range_header(byte_range)
        request = urllib.request.Request(url, method=method, headers=headers)
        for attempt in range(len(_RETRY_DELAYS_S) + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read() if method == "GET" else b""
                    if method == "GET" and response.status == 200:
                        body = _slice_body(body, byte_range)
                    return body, response.headers
            except urllib.error.HTTPError as error:
                code, reason = error.code, error.reason
                error.close()  # an HTTPError owns the response socket
                if code in (404, 416):
                    return None
                transient = code == 429 or code >= 500
                if not transient or attempt == len(_RETRY_DELAYS_S):
                    raise OSError(f"{method} {url}: HTTP {code} {reason}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
                if attempt == len(_RETRY_DELAYS_S):
                    raise OSError(f"{method} {url}: {error}") from error
            time.sleep(_RETRY_DELAYS_S[attempt])
        raise AssertionError("unreachable")  # the loop returns or raises on its last attempt

    async def get(
        self, key: str, prototype: BufferPrototype, byte_range: ByteRequest | None = None
    ) -> Buffer | None:
        fetched = await asyncio.to_thread(self._fetch, key, "GET", byte_range)
        return None if fetched is None else prototype.buffer.from_bytes(fetched[0])

    async def get_partial_values(
        self, prototype: BufferPrototype, key_ranges: Iterable[tuple[str, ByteRequest | None]]
    ) -> builtins.list[Buffer | None]:
        return builtins.list(
            await asyncio.gather(*(self.get(k, prototype, r) for k, r in key_ranges))
        )

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._fetch, key, "HEAD") is not None

    async def getsize(self, key: str) -> int:
        fetched = await asyncio.to_thread(self._fetch, key, "HEAD")
        if fetched is None:
            raise FileNotFoundError(key)
        length = fetched[1].get("Content-Length")
        if length is None:
            raise OSError(f"HEAD {self.url}/{key}: no Content-Length header")
        return int(length)

    async def set(self, key: str, value: Buffer) -> None:
        raise PermissionError("HttpStore is read-only")

    async def delete(self, key: str) -> None:
        raise PermissionError("HttpStore is read-only")

    async def list(self) -> AsyncIterator[str]:
        raise NotImplementedError("HttpStore cannot list keys")
        yield ""  # pragma: no cover - makes this an async generator

    async def list_prefix(self, prefix: str) -> AsyncIterator[str]:
        raise NotImplementedError("HttpStore cannot list keys")
        yield ""  # pragma: no cover

    async def list_dir(self, prefix: str) -> AsyncIterator[str]:
        raise NotImplementedError("HttpStore cannot list keys")
        yield ""  # pragma: no cover


def as_store(path_or_url: Any) -> Any:
    """A zarr store argument: http(s) URL strings become an HttpStore, anything else is kept."""
    if isinstance(path_or_url, str) and path_or_url.startswith(("http://", "https://")):
        return HttpStore(path_or_url)
    return path_or_url


def _plane(group: zarr.Group, name: str | None, where: str) -> zarr.Array | None:
    return None if name is None else schema.get_array(group, name, where)


def open_store(path_or_url: Any) -> ChronoStore:
    """Open a chronozarr store from a path, http(s) URL, or zarr Store.

    Raises SchemaError if the store is not a conforming chronozarr store (v0.1 or v0.2). An
    http(s) URL is read with `HttpStore` (Range requests, no fsspec needed).
    """
    try:
        group = zarr.open_group(as_store(path_or_url), mode="r", zarr_format=3)
    except (GroupNotFoundError, FileNotFoundError) as exc:
        raise SchemaError(
            f"{path_or_url}: no Zarr v3 group found; is this a chronozarr store?"
        ) from exc
    return ChronoStore(group, str(path_or_url))
