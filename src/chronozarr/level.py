"""One pyramid level of an opened store, and the arrays that hold its values."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import zarr

from chronozarr.schema import Transform


@dataclass(frozen=True)
class LevelData:
    """The arrays of one level, read as (time, band, y, x).

    A chronozarr store keeps a level in one 4-D array. Stores written by others keep it in one
    array of 4, 3 or 2 dimensions, or in one array per band. `axes` names each dimension of every
    array, in storage order, as `time`, `band`, `y` or `x`. A level with no time axis is one
    timestep, and one with no band axis has one band per array.
    """

    arrays: tuple[zarr.Array, ...]
    axes: tuple[str, ...]

    @property
    def shape(self) -> tuple[int, int, int, int]:
        """(time, band, y, x)."""
        sizes = dict(zip(self.axes, self.arrays[0].shape, strict=True))
        n_band = sizes.get("band", 1) * len(self.arrays)
        return sizes.get("time", 1), n_band, sizes["y"], sizes["x"]

    @property
    def dtype(self) -> np.dtype:
        return self.arrays[0].dtype

    @property
    def chunks(self) -> tuple[int, ...]:
        """The chunk shape of the first array: inner chunks when it is sharded."""
        return tuple(self.arrays[0].chunks)

    @property
    def shards(self) -> tuple[int, ...] | None:
        shards = self.arrays[0].shards
        return None if shards is None else tuple(shards)

    @property
    def cell_shape(self) -> tuple[int, int]:
        """The (rows, columns) of one spatial chunk."""
        return int(self.arrays[0].chunks[-2]), int(self.arrays[0].chunks[-1])

    @property
    def time_chunk(self) -> int:
        """Timesteps per chunk; a level with no time axis has one timestep."""
        return int(self.arrays[0].chunks[0]) if self.axes[0] == "time" else 1

    def read(self, timesteps: Sequence[int], ys: slice, xs: slice) -> np.ndarray:
        """The stored values of `timesteps` over a window: (time, band, y, x)."""
        steps = np.asarray(timesteps, dtype=np.int64)
        parts = []
        for array in self.arrays:
            window = tuple(
                {"time": steps, "band": slice(None), "y": ys, "x": xs}[axis] for axis in self.axes
            )
            values = np.asarray(array.oindex[window])
            if "time" not in self.axes:
                values = np.repeat(values[None], len(steps), axis=0)
            if "band" not in self.axes:
                values = values[:, None]
            parts.append(values)
        return parts[0] if len(parts) == 1 else np.concatenate(parts, axis=1)


@dataclass(frozen=True)
class Level:
    """One pyramid level of a store. Level 0 is the finest."""

    index: int
    path: str
    shape: tuple[int, int, int, int]  # (time, band, y, x)
    transform: Transform
    resolution: float
    grid: tuple[int, int]  # (rows, cols) of cells
    data: LevelData
    nodata: int | float | None = None  # the stored value that marks a missing pixel, if any
    mask: zarr.Array | None = None
    coverage: zarr.Array | None = None

    @property
    def cell_shape(self) -> tuple[int, int]:
        """(rows, columns) of one cell: the spatial chunk of the data."""
        return self.data.cell_shape

    @property
    def shard_time(self) -> int | None:
        """Timesteps per shard along time; None for an unsharded array."""
        shards = self.data.shards
        return None if shards is None else int(shards[0])
