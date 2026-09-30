"""chronozarr reader: open a store and reconstruct star-delta timesteps."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import xarray as xr
import zarr
from zarr.errors import GroupNotFoundError

from chronozarr import schema
from chronozarr.schema import Chronozarr, SchemaError, Transform

_UINT16_MAX = 65535


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


class ChronoStore:
    """A chronozarr store opened for reading. Use `open_store` to create one."""

    def __init__(self, group: zarr.Group, source: str) -> None:
        root = schema.parse_root_attrs(group.attrs.asdict())
        self.source = source
        self.attrs: Chronozarr = root.chronozarr
        self.times: np.ndarray = np.array(
            [schema.parse_time(t) for t in root.chronozarr.times], dtype="datetime64[ms]"
        )
        self.bands: tuple[str, ...] = root.chronozarr.bands
        levels = []
        for index, dataset in enumerate(root.datasets):
            where = f"level {dataset.path}"
            level_group = schema.get_group(group, dataset.path, "store")
            data = schema.get_array(level_group, root.chronozarr.variable, where)
            level_attrs = schema.parse_level_attrs(level_group.attrs.asdict(), where)
            if len(data.shape) != 4:
                raise SchemaError(f"{where}/data: expected 4 dimensions, got shape {data.shape}")
            shape = (data.shape[0], data.shape[1], data.shape[2], data.shape[3])
            levels.append(
                Level(
                    index=index,
                    shape=shape,
                    transform=level_attrs.transform,
                    resolution=level_attrs.resolution,
                    chunk_size=dataset.pixels_per_tile,
                    grid=schema.grid_shape(shape[2], shape[3], dataset.pixels_per_tile),
                    data=data,
                )
            )
        self.levels: tuple[Level, ...] = tuple(levels)

    def _level(self, lod: int) -> Level:
        if not 0 <= lod < len(self.levels):
            raise IndexError(f"lod {lod} out of range: store has levels 0..{len(self.levels) - 1}")
        return self.levels[lod]

    def _check_time(self, t: int) -> int:
        if not 0 <= t < len(self.times):
            raise IndexError(f"timestep {t} out of range: store has {len(self.times)} timesteps")
        return int(t)

    def _read_region(
        self, level: Level, timesteps: Sequence[int], ys: slice, xs: slice
    ) -> np.ndarray:
        """Decode `timesteps` over a spatial window: (len(timesteps), band, y, x) uint16.

        Reads each needed anchor once plus each requested delta, in a single Zarr read.
        """
        reference = self.attrs.temporal.delta_reference
        needed = sorted({*timesteps, *(reference[t] for t in timesteps if t in reference)})
        raw = np.asarray(level.data.oindex[np.asarray(needed), :, ys, xs])
        row = {t: i for i, t in enumerate(needed)}
        out = np.empty((len(timesteps), *raw.shape[1:]), dtype=np.uint16)
        for i, t in enumerate(timesteps):
            if t not in reference:
                out[i] = raw[row[t]]
                continue
            reconstructed = raw[row[reference[t]]].astype(np.int32)
            reconstructed += raw[row[t]].view(np.int16)
            np.clip(reconstructed, 0, _UINT16_MAX, out=reconstructed)
            out[i] = reconstructed
        return out

    def read(self, t: int, lod: int = 0) -> np.ndarray:
        """Decode timestep `t` of level `lod` as a (band, y, x) uint16 array."""
        level = self._level(lod)
        window = slice(None)
        return self._read_region(level, [self._check_time(t)], window, window)[0]

    def read_cell(self, t: int, row: int, col: int, lod: int = 0) -> np.ndarray:
        """Decode one cell of timestep `t`: a (band, y, x) uint16 array.

        Costs at most two chunk reads (the anchor and the delta). Edge cells are smaller than
        `chunk_size`; padding beyond the level shape is never returned.
        """
        level = self._level(lod)
        rows, cols = level.grid
        if not (0 <= row < rows and 0 <= col < cols):
            raise IndexError(
                f"cell ({row}, {col}) out of range: level {lod} has a {rows} x {cols} cell grid"
            )
        cs = level.chunk_size
        ys = slice(row * cs, min((row + 1) * cs, level.shape[2]))
        xs = slice(col * cs, min((col + 1) * cs, level.shape[3]))
        return self._read_region(level, [self._check_time(t)], ys, xs)[0]

    def to_xarray(self, lod: int = 0, times: Sequence[int] | None = None) -> xr.DataArray:
        """Decode a level into a DataArray with dims (time, band, y, x) and full coordinates.

        `times` selects timestep indices (default: all). The result is loaded into memory.
        """
        level = self._level(lod)
        selected = (
            list(range(len(self.times))) if times is None else [self._check_time(t) for t in times]
        )
        values = self._read_region(level, selected, slice(None), slice(None))
        y, x = schema.pixel_centers(level.transform, level.shape[2], level.shape[3])
        return xr.DataArray(
            values,
            dims=schema.DIMENSIONS,
            coords={
                "time": self.times[selected].astype("datetime64[ns]"),
                "band": list(self.bands),
                "y": y,
                "x": x,
            },
            name=self.attrs.variable,
            attrs={
                "crs": self.attrs.crs,
                "transform": list(level.transform),
                "nodata": self.attrs.nodata,
            },
        )


def open_store(path_or_url: Any) -> ChronoStore:
    """Open a chronozarr store from a path, URL, or zarr Store.

    Raises SchemaError if the store is not a conforming chronozarr v0.1 store. Remote URLs need
    fsspec (and aiohttp for http/https).
    """
    try:
        group = zarr.open_group(path_or_url, mode="r", zarr_format=3)
    except (GroupNotFoundError, FileNotFoundError) as exc:
        raise SchemaError(
            f"{path_or_url}: no Zarr v3 group found; is this a chronozarr store?"
        ) from exc
    return ChronoStore(group, str(path_or_url))
